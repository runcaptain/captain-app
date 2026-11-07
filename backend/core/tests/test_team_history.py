"""Restore history must invalidate cursors and proposals, even after cursor reuse."""
import json
import shutil
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text, update

from captain_search.collaboration.client import SyncClient
from captain_search.collaboration.migrations import LATEST_VERSION, migrate, require_current
from captain_search.collaboration.server import create_app, metadata, workspaces


def test_v3_upgrade_adds_history_without_changing_records(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'v3.db'))
    # Start from real tables, then remove only the version-4 columns.
    from captain_search.collaboration import artifacts, tokens  # registers all tables
    metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(text('ALTER TABLE sync_workspaces DROP COLUMN history_id'))
        db.execute(text('ALTER TABLE sync_workspaces DROP COLUMN history_required'))
        if 'history_id' in {column['name'] for column in inspect(db).get_columns('sync_artifact_uploads')}:
            db.execute(text('ALTER TABLE sync_artifact_uploads DROP COLUMN history_id'))
        db.execute(text("INSERT INTO sync_workspaces VALUES ('team', 12, 3)"))
        db.execute(text('CREATE TABLE sync_schema_versions (version INTEGER PRIMARY KEY)'))
        db.execute(text('INSERT INTO sync_schema_versions VALUES (1), (2), (3)'))
    with pytest.raises(ValueError):
        require_current(engine)
    assert migrate(engine) == LATEST_VERSION
    with engine.connect() as db:
        before = dict(db.execute(select(workspaces)).mappings().one())
    assert before['cursor'] == 12 and before['epoch'] == 3
    assert len(before['history_id']) == 32 and not before['history_required']
    assert migrate(engine) == LATEST_VERSION
    with engine.connect() as db:
        assert dict(db.execute(select(workspaces)).mappings().one()) == before
    engine.dispose()


def test_restored_cursor_catchup_rebases_cache_and_blocks_old_drafts(tmp_path):
    path = tmp_path / 'server.db'
    app = create_app('sqlite:///' + str(path), {
        'users': [{'id': 'alice', 'token': 'synthetic'}],
        'workspaces': [{'id': 'team', 'members': {'alice': 'admin'}}]},
        allow_dev_tokens=True, artifact_root=tmp_path / 'artifacts')
    with TestClient(app, headers={'Authorization': 'Bearer synthetic'}) as http:
        cache = SyncClient(tmp_path / 'cache.db', http, 'team', 'alice')
        base = '/v1/workspaces/team'
        def write(record, revision, title, history=None):
            value = {'mutation_id': uuid.uuid4().hex, 'record_id': record, 'kind': 'config',
                     'expected_revision': revision, 'body': {'title': title}}
            if history:
                value['history_id'] = history
            response = http.post(base + '/mutations', json=value)
            assert response.status_code == 200, response.text
            return value
        first = write('shared', 0, 'backup')
        cache.pull()
        original_history = cache._get('history_id')
        backup = tmp_path / 'backup.db'
        app.state.engine.dispose()
        shutil.copyfile(path, backup)
        write('shared', 1, 'discarded branch')
        write('lost-new', 0, 'discarded new record')
        cache.pull()
        assert cache._get('cursor') == 3
        cache.queue_edit('shared', 'config', {'title': 'old proposal'})
        cache.queue_edit('local-new', 'config', {'title': 'new proposal'})
        def restore():
            app.state.engine.dispose()
            shutil.copyfile(backup, path)
            history = uuid.uuid4().hex
            with app.state.engine.begin() as db:
                db.execute(update(workspaces).values(history_id=history, history_required=True))
            return history
        try:
            previous_history = original_history
            for attempt in range(2):
                history = restore()
                assert history != previous_history
                write('shared', 1, 'restored branch ' + str(attempt), history)
                write('replacement-new', 0, 'restored new record', history)
                assert http.get(base + '/snapshot').json()['cursor'] == cache._get('cursor') == 3
                # Missing history from a legacy client cannot pass cursor equality.
                assert http.get(base + '/changes', params={'after': 3, 'epoch': 1}).status_code == 409
                old = http.post(base + '/mutations', json=dict(first, history_id=previous_history))
                assert old.status_code == 409 and old.json()['detail']['code'] == 'history_reset_required'
                cache.flush()
                assert cache._get('history_id') == history
                assert cache.get('shared')['body']['title'] == 'restored branch ' + str(attempt)
                assert cache.get('lost-new') is None
                assert len(cache.drafts()) == 2
                assert all(d['state'] == 'conflict' and d['error'] == 'server_history_changed' for d in cache.drafts())
                assert http.get(base + '/snapshot').json()['cursor'] == 3, 'flush must not publish old-history proposals'
                previous_history = history
            proposal = next(d for d in cache.drafts() if d['mutation']['record_id'] == 'shared')
            cache.resolve(proposal['mutation']['mutation_id'], {'title': 'explicitly reviewed'},
                          expected_revision=cache.get('shared')['revision'], expected_history=history)
            cache.flush()
            assert cache.get('shared')['body'] == {'title': 'explicitly reviewed'}
            assert cache.get('shared')['revision'] == 3
            assert len(cache.drafts()) == 1 and cache.drafts()[0]['mutation']['record_id'] == 'local-new'
        finally:
            cache.close()
    app.state.engine.dispose()


def test_postgres_v3_history_upgrade_when_configured():
    import os
    url = os.environ.get('DATABASE_URL', '')
    from sqlalchemy.engine import make_url
    if (not url.startswith('postgresql') or os.environ.get('CST_TEAM_TEST_HISTORY_UPGRADE') != 'synthetic-only'
            or not str(make_url(url).query.get('host', '')).startswith('/tmp/cst-pg-')):
        pytest.skip('requires explicitly opted-in private PostgreSQL history-migration cluster')
    engine = create_engine(url)
    migrate(engine)
    with engine.begin() as db:
        before = list(db.execute(select(workspaces.c.id, workspaces.c.cursor, workspaces.c.epoch).order_by(workspaces.c.id)))
        db.execute(text('ALTER TABLE sync_workspaces DROP COLUMN history_id'))
        db.execute(text('ALTER TABLE sync_workspaces DROP COLUMN history_required'))
        db.execute(text('ALTER TABLE sync_artifact_uploads DROP COLUMN history_id'))
        db.execute(text('DELETE FROM sync_schema_versions WHERE version = 4'))
    with pytest.raises(ValueError):
        require_current(engine)
    assert migrate(engine) == LATEST_VERSION
    with engine.connect() as db:
        assert list(db.execute(select(workspaces.c.id, workspaces.c.cursor, workspaces.c.epoch).order_by(workspaces.c.id))) == before
        assert not db.execute(text("SELECT count(*) FROM sync_workspaces WHERE length(history_id) <> 32 OR history_required")).scalar()
        assert not db.execute(text('SELECT count(*) FROM sync_artifact_uploads WHERE history_id IS NULL')).scalar()
    engine.dispose()
