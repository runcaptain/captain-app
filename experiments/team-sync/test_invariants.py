"""Independent failure-path checks. PostgreSQL cases skip on SQLite."""
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import event, func, select

from server import changes, receipts, records, versions, workspaces
from test_client import clients
from test_server import mutation, team


def test_pg_delayed_event_commit_blocks_reader_then_resumes_without_loss(team):
    c, app, workspace, alice, bob, _, headers = team
    engine = app.state.engine
    if engine.dialect.name != 'postgresql':
        pytest.skip('Requires PostgreSQL transaction isolation and row locks')
    root = f'/v1/workspaces/{workspace}'
    baseline = c.get(root + '/snapshot', headers=headers[bob]).json()
    staged = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()

    def pause_after_receipt(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith('INSERT INTO SYNC_RECEIPTS'):
            # Domain row, version, event and receipt exist inside the transaction,
            # but none may be visible until the workspace transaction commits.
            staged.set()
            if not release.wait(10):
                raise RuntimeError('Test timed out waiting to release commit')

    def catch_up():
        reader_started.set()
        return c.get(root + '/changes', headers=headers[bob], params={
            'after': baseline['cursor'], 'epoch': baseline['epoch']})

    event.listen(engine, 'after_cursor_execute', pause_after_receipt)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(c.post, root + '/mutations', headers=headers[alice],
                                 json=mutation('delayed-event'))
            try:
                assert staged.wait(5), 'Writer did not reach its uncommitted receipt'
                reader = pool.submit(catch_up)
                assert reader_started.wait(5)
                # A separate DB connection sees neither payload nor high cursor.
                with engine.connect() as db:
                    assert db.execute(select(workspaces.c.cursor).where(
                        workspaces.c.id == workspace)).scalar_one() == 0
                    assert db.execute(select(func.count()).select_from(changes).where(
                        changes.c.workspace == workspace)).scalar_one() == 0
                assert not reader.done(), 'Reader returned across an uncommitted workspace mutation'
            finally:
                release.set()
            assert writer.result(timeout=5).status_code == 200
            response = reader.result(timeout=5)
            assert response.status_code == 200
            page = response.json()
            assert page['cursor'] == 1
            assert [x['record_id'] for x in page['changes']] == ['delayed-event']
            assert not page['has_more']
    finally:
        release.set()
        event.remove(engine, 'after_cursor_execute', pause_after_receipt)


def test_pg_failure_after_receipt_rolls_back_entire_mutation(team):
    c, app, workspace, alice, _, _, headers = team
    engine = app.state.engine
    if engine.dialect.name != 'postgresql':
        pytest.skip('Requires PostgreSQL rollback acceptance test')
    root = f'/v1/workspaces/{workspace}'
    payload = mutation('rollback-event')

    def fail_after_receipt(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith('INSERT INTO SYNC_RECEIPTS'):
            raise RuntimeError('Injected failure after receipt insertion')

    event.listen(engine, 'after_cursor_execute', fail_after_receipt)
    try:
        with pytest.raises(RuntimeError, match='Injected failure'):
            c.post(root + '/mutations', headers=headers[alice], json=payload)
    finally:
        event.remove(engine, 'after_cursor_execute', fail_after_receipt)
    with engine.connect() as db:
        for table in (records, versions, changes, receipts):
            assert db.execute(select(func.count()).select_from(table).where(
                table.c.workspace == workspace)).scalar_one() == 0
        assert db.execute(select(workspaces.c.cursor).where(
            workspaces.c.id == workspace)).scalar_one() == 0
    # Retry the identical ID after rollback: no phantom receipt or revision.
    response = c.post(root + '/mutations', headers=headers[alice], json=payload)
    assert response.status_code == 200
    assert response.json()['revision'] == response.json()['cursor'] == 1


def test_failed_bootstrap_after_epoch_reset_hides_drafts(clients, monkeypatch):
    alice, bob, _, _ = clients
    alice.queue_edit('shared', 'config', {'value': 1}, 'workspace')
    alice.flush()
    bob.pull()
    bob.queue_edit('shared', 'config', {'value': 'offline'})
    alice.queue_edit('shared', 'config', {'value': 'private'}, 'private')
    alice.flush()
    original_get = bob.http.get

    def disconnect_snapshot(url, *args, **kwargs):
        if str(url).endswith('/snapshot'):
            raise httpx.ConnectError('Injected snapshot disconnect')
        return original_get(url, *args, **kwargs)

    monkeypatch.setattr(bob.http, 'get', disconnect_snapshot)
    with pytest.raises(httpx.ConnectError):
        bob.pull()
    assert bob.get('shared') is None
    assert bob.drafts() == []
    assert bob._get('cursor') is None
    assert bob._get('permission_reset_pending') is True
    monkeypatch.setattr(bob.http, 'get', original_get)
    bob.pull()
    assert bob.drafts() == []
    assert bob.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


def test_offline_create_id_collision_cannot_overwrite_private_record(clients):
    alice, bob, a, b = clients
    bob.queue_edit('collision', 'config', {'value': 'offline'}, 'workspace')
    alice.queue_edit('collision', 'config', {'value': 'secret'}, 'private')
    alice.flush()
    payload = bob.drafts()[0]['mutation']
    response = b.post('/v1/workspaces/team/mutations', json=payload)
    assert response.status_code == 404
    assert 'secret' not in response.text
    bob.flush()
    assert bob.get('collision') is None and bob.drafts() == []
    alice.pull()
    assert alice.get('collision')['body'] == {'value': 'secret'}


def test_reused_mutation_id_cannot_redirect_write_to_private_record(clients):
    alice, bob, a, b = clients
    bob.queue_edit('public', 'config', {'value': 'public'}, 'workspace')
    original = bob.drafts()[0]['mutation']
    bob.flush()
    alice.queue_edit('secret', 'config', {'value': 'private-body'}, 'private')
    alice.flush()
    collision = dict(original, record_id='secret', expected_revision=1)
    response = b.post('/v1/workspaces/team/mutations', json=collision)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'idempotency_key_reused'
    assert 'private-body' not in response.text
    alice.pull()
    assert alice.get('secret')['revision'] == 1
