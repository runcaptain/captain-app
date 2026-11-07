import hashlib
import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, update
from typer.testing import CliRunner

from captain_search.cli.main import app as cli
from captain_search.collaboration.migrations import migrate, versions
from captain_search.collaboration.operator import grant, revoke
from captain_search.collaboration.server import create_app, from_env, users
from captain_search.collaboration.tokens import issue, list_tokens, revoke_token, tokens


@pytest.fixture
def production(tmp_path):
    url = 'sqlite:///' + str(tmp_path/'team.db')
    engine = create_engine(url)
    migrate(engine)
    grant(engine, user_id='alice', workspace='team', role='admin')
    service = create_app(url, artifact_root=tmp_path/'artifacts', auth_mode='tokens')
    with TestClient(service) as client:
        yield engine, client
    service.state.engine.dispose()
    engine.dispose()


def header(path):
    return {'Authorization':'Bearer '+path.read_text().strip()}


def test_token_auth_rotation_expiry_and_revocation(production, tmp_path):
    engine, client = production
    path = tmp_path/'token'
    original = issue(engine, user_id='alice', output=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(path.read_text().strip()) == 68
    identity = '/v1/workspaces/team/identity'
    assert client.get(identity, headers=header(path)).json()['user_id'] == 'alice'
    assert client.get(identity, headers={'Authorization':'Bearer wrong'}).status_code == 401
    replacement = tmp_path/'replacement'
    rotated = issue(engine, rotate_id=original['id'], output=replacement)
    assert client.get(identity, headers=header(path)).status_code == 401
    assert client.get(identity, headers=header(replacement)).json()['user_id'] == 'alice'
    with engine.begin() as db:
        db.execute(update(tokens).where(tokens.c.id == rotated['id']).values(expires_at=int(time.time())-1))
    assert client.get(identity, headers=header(replacement)).status_code == 401
    third_path = tmp_path/'third'
    third = issue(engine, user_id='alice', output=third_path)
    revoke_token(engine, third['id'])
    revoke_token(engine, third['id'])
    revoke(engine, workspace='team', user_id='alice')
    grant(engine, user_id='alice', workspace='team', role='admin')
    assert client.get(identity, headers=header(third_path)).status_code == 401
    with engine.connect() as db:
        stored = db.execute(select(tokens).where(tokens.c.id == original['id'])).mappings().one()
    assert stored['token_hash'] == hashlib.sha256(path.read_text().strip().encode()).hexdigest()
    assert path.read_text().strip() not in str(stored)
    assert 'token_hash' not in str(list_tokens(engine))


def test_membership_denial_is_separate_from_token_denial(production, tmp_path):
    engine, client = production
    path = tmp_path/'token'
    issue(engine, user_id='alice', output=path)
    revoke(engine, workspace='team', user_id='alice')
    response = client.get('/v1/workspaces/team/identity', headers=header(path))
    assert response.status_code == 403
    assert response.json()['detail'] == 'workspace_access_denied'


def test_failed_rotation_preserves_old_token_and_existing_file(production, tmp_path, monkeypatch):
    engine, client = production
    path = tmp_path/'token'
    original = issue(engine, user_id='alice', output=path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='new file'):
        issue(engine, rotate_id=original['id'], output=path)
    assert path.read_bytes() == before
    symlink = tmp_path/'link'
    symlink.symlink_to(path)
    with pytest.raises(ValueError, match='new file'):
        issue(engine, rotate_id=original['id'], output=symlink)
    assert path.read_bytes() == before
    def fail(_):
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(os, 'fsync', fail)
    failed = tmp_path/'failed'
    with pytest.raises(OSError):
        issue(engine, rotate_id=original['id'], output=failed)
    assert not failed.exists()
    assert len(list_tokens(engine)) == 1
    assert client.get('/v1/workspaces/team/identity', headers=header(path)).status_code == 200


def test_cli_never_outputs_token_or_hash(tmp_path, monkeypatch):
    monkeypatch.setenv('CST_TEAM_DATABASE_URL', 'sqlite:///'+str(tmp_path/'cli.db'))
    runner = CliRunner()
    assert runner.invoke(cli, ['team-server','migrate']).exit_code == 0
    assert runner.invoke(cli, ['team-server','grant','alice','team','--role','admin']).exit_code == 0
    path = tmp_path/'credential'
    issued = runner.invoke(cli, ['team-server','token','issue','alice','--output',str(path)])
    assert issued.exit_code == 0, issued.output
    token = path.read_text().strip()
    hashed = hashlib.sha256(token.encode()).hexdigest()
    token_id = json.loads(issued.stdout)['id']
    results = [issued,
        runner.invoke(cli, ['team-server','token','list']),
        runner.invoke(cli, ['team-server','token','rotate',token_id,'--output',str(path)]),
        runner.invoke(cli, ['team-server','token','revoke',token_id])]
    for result in results:
        assert token not in result.output
        assert hashed not in result.output
    assert results[2].exit_code != 0
    assert results[3].exit_code == 0


def test_rotation_database_failure_rolls_back_file_and_old_revoke(production, tmp_path):
    from sqlalchemy import event
    engine, client = production
    path = tmp_path/'token'
    original = issue(engine, user_id='alice', output=path)
    output = tmp_path/'failed-commit'
    def fail_commit(connection):
        raise RuntimeError('synthetic database commit failure')
    event.listen(engine, 'commit', fail_commit)
    try:
        with pytest.raises(RuntimeError, match='synthetic database'):
            issue(engine, rotate_id=original['id'], output=output)
    finally:
        event.remove(engine, 'commit', fail_commit)
    assert not output.exists()
    assert len(list_tokens(engine)) == 1
    assert client.get('/v1/workspaces/team/identity', headers=header(path)).status_code == 200


def test_simultaneous_rotations_only_activate_one_replacement(production, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    engine, _ = production
    original = issue(engine, user_id='alice', output=tmp_path/'original')
    def rotate(number):
        try:
            return issue(engine, rotate_id=original['id'], output=tmp_path/f'new-{number}')
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rotate, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert len(list(tmp_path.glob('new-*'))) == 1
    assert sum(row['revoked_at'] is None for row in list_tokens(engine)) == 1


def test_production_mode_rejects_legacy_fixture_tokens(production, tmp_path, monkeypatch):
    engine, client = production
    with engine.begin() as db:
        db.execute(update(users).where(users.c.id == 'alice').values(token_hash=hashlib.sha256(b'legacy').hexdigest()))
    assert client.get('/v1/workspaces/team/identity', headers={'Authorization':'Bearer legacy'}).status_code == 401
    monkeypatch.setenv('CST_TEAM_DATABASE_URL', str(engine.url))
    monkeypatch.setenv('CST_TEAM_ARTIFACT_ROOT', str(tmp_path/'env-artifacts'))
    monkeypatch.setenv('CST_TEAM_AUTH_MODE', 'tokens')
    service = from_env()
    service.state.engine.dispose()
    monkeypatch.setenv('CST_TEAM_DEV_TOKENS', 'synthetic-only')
    with pytest.raises(ValueError, match='Conflicting'):
        from_env()
    with pytest.raises(ValueError):
        create_app(str(engine.url), auth_mode='tokens', allow_dev_tokens=True)
    with pytest.raises(ValueError):
        create_app(str(engine.url), fixture={'users':[]}, auth_mode='tokens')


def test_upgrade_v2_preserves_user_and_adds_no_tokens(tmp_path):
    engine = create_engine('sqlite:///'+str(tmp_path/'v2.db'))
    migrate(engine)
    with engine.begin() as db:
        tokens.drop(db)
        db.execute(versions.delete().where(versions.c.version == 3))
        db.execute(insert(users).values(id='old', token_hash='legacy-hash'))
    assert migrate(engine) == 3
    assert list_tokens(engine) == []
    with engine.connect() as db:
        assert db.execute(select(users.c.token_hash)).scalar() == 'legacy-hash'
    engine.dispose()
