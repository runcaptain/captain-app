import pytest
from sqlalchemy import create_engine, insert, select
from typer.testing import CliRunner

from captain_search.cli.main import app
from captain_search.collaboration.migrations import LATEST_VERSION, migrate, require_current, versions
from captain_search.collaboration.operator import enroll, list_members, revoke
from captain_search.collaboration.server import identities, metadata, records, users, workspaces


def test_upgrade_preserves_existing_rows_and_repeats(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    metadata.create_all(engine, tables=[t for t in metadata.sorted_tables if t is not identities])
    with engine.begin() as db:
        db.execute(insert(users).values(id='legacy', token_hash='unchanged'))
        db.execute(insert(workspaces).values(id='team', cursor=9, epoch=3))
        db.execute(insert(records).values(workspace='team', id='draft', kind='config', owner_id='legacy', visibility='private', revision=4, body={'keep':True}, deleted=False))
    assert migrate(engine) == migrate(engine) == LATEST_VERSION
    require_current(engine)
    with engine.connect() as db:
        assert db.execute(select(users.c.token_hash)).scalar() == 'unchanged'
        assert db.execute(select(records.c.body)).scalar() == {'keep':True}
        assert db.execute(select(workspaces.c.cursor)).scalar() == 9
        assert list(db.execute(select(versions.c.version)).scalars()) == list(range(1, LATEST_VERSION + 1))


def test_immutable_binding_and_revoke(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'fresh.db'))
    migrate(engine)
    args = dict(issuer='https://issuer.example/', subject='subject', user_id='alice', workspace='team', role='admin')
    enroll(engine, **args)
    enroll(engine, **args)
    with pytest.raises(ValueError, match='immutable'):
        enroll(engine, **{**args, 'user_id':'bob'})
    assert list_members(engine, 'team') == [{'user_id':'alice','role':'admin'}]
    revoke(engine, workspace='team', user_id='alice')
    revoke(engine, workspace='team', user_id='alice')
    with engine.connect() as db:
        assert db.execute(select(workspaces.c.epoch)).scalar() == 2
        assert db.execute(select(identities.c.user_id)).scalar() == 'alice'
    assert list_members(engine, 'team')[0]['role'] == 'revoked'
    enroll(engine, **args)
    with engine.connect() as db:
        assert db.execute(select(workspaces.c.epoch)).scalar() == 3


def test_unknown_version_fails_closed(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'future.db'))
    migrate(engine)
    with engine.begin() as db:
        db.execute(insert(versions).values(version=99))
    with pytest.raises(ValueError):
        migrate(engine)
    with pytest.raises(ValueError):
        require_current(engine)


def test_cli_operator_flow(tmp_path, monkeypatch):
    monkeypatch.setenv('CST_TEAM_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'cli.db'))
    runner = CliRunner()
    assert runner.invoke(app, ['team-server','migrate']).exit_code == 0
    assert runner.invoke(app, ['team-server','enroll','https://issuer.example/','subject','alice','team','--role','admin']).exit_code == 0
    listed = runner.invoke(app, ['team-server','list','team'])
    assert listed.exit_code == 0 and 'admin' in listed.stdout and 'subject' not in listed.stdout
    assert runner.invoke(app, ['team-server','revoke','team','alice']).exit_code == 0
    assert 'revoked' in runner.invoke(app, ['team-server','list','team']).stdout


def test_incompatible_legacy_schema_not_adopted(tmp_path):
    from sqlalchemy import inspect, text
    engine = create_engine('sqlite:///' + str(tmp_path / 'broken.db'))
    with engine.begin() as db:
        db.execute(text('CREATE TABLE sync_users (id TEXT PRIMARY KEY)'))
        db.execute(text("INSERT INTO sync_users VALUES ('keep-me')"))
    with pytest.raises(ValueError, match='incompatible'):
        migrate(engine)
    assert not inspect(engine).has_table('sync_schema_versions')
    with engine.connect() as db:
        assert db.execute(text('SELECT id FROM sync_users')).scalar() == 'keep-me'


def test_bad_database_configuration_does_not_print_credentials(monkeypatch):
    monkeypatch.setenv('CST_TEAM_DATABASE_URL', 'synthetic-secret-not-a-url')
    result = CliRunner().invoke(app, ['team-server', 'migrate'])
    assert result.exit_code != 0
    assert 'synthetic-secret' not in result.output


def test_concurrent_migration_serializes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    url = 'sqlite:///' + str(tmp_path / 'parallel.db')
    def upgrade():
        engine = create_engine(url)
        try:
            return migrate(engine)
        finally:
            engine.dispose()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: upgrade(), range(4))) == [LATEST_VERSION] * 4
    engine = create_engine(url)
    require_current(engine)
    with engine.connect() as db:
        assert list(db.execute(select(versions.c.version)).scalars()) == list(range(1, LATEST_VERSION + 1))


def test_postgres_operator_transactions_when_configured():
    import os
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    url = os.environ.get('DATABASE_URL', '')
    if not url.startswith('postgresql'):
        pytest.skip('requires isolated PostgreSQL')
    engine = create_engine(url)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: migrate(engine), range(4))) == [LATEST_VERSION] * 4
    suffix = uuid.uuid4().hex
    args = dict(issuer='https://synthetic.example/', subject=suffix, user_id=suffix, workspace=suffix, role='admin')
    enroll(engine, **args)
    with pytest.raises(ValueError, match='immutable'):
        enroll(engine, **{**args, 'user_id':'wrong-'+suffix})
    revoke(engine, workspace=suffix, user_id=suffix)
    assert list_members(engine, suffix) == [{'user_id':suffix,'role':'revoked'}]
    enroll(engine, **args)
    with engine.connect() as db:
        assert db.execute(select(workspaces.c.epoch).where(workspaces.c.id == suffix)).scalar() == 3
    engine.dispose()
