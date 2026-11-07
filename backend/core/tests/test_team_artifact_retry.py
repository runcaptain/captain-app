"""Accepted retries retain one stage; rejected contenders remove only their own bytes."""
import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import boto3
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import event, select, update

from captain_search.collaboration.artifacts import S3ArtifactStore, uploads
from captain_search.collaboration.server import create_app, members

DATA = b'synthetic retry bytes'
HEADERS = {'Authorization':'Bearer retry-token'}
ROOT = '/v1/workspaces/team'


@pytest.fixture(params=['local', 's3'])
def service(request, tmp_path):
    with mock_aws() if request.param == 's3' else nullcontext():
        store = None
        if request.param == 's3':
            s3 = boto3.client('s3', region_name='us-east-1')
            s3.create_bucket(Bucket='synthetic-retry-artifacts')
            store = S3ArtifactStore('synthetic-retry-artifacts', client=s3)
        root = tmp_path/'artifacts'
        app = create_app('sqlite:///'+str(tmp_path/'team.db'), {
            'users':[{'id':'alice', 'token':'retry-token'}],
            'workspaces':[{'id':'team', 'members':{'alice':'admin'}}]},
            root, allow_dev_tokens=True, artifact_store=store)
        with TestClient(app, raise_server_exceptions=False) as client:
            result = client.post(ROOT+'/artifacts/uploads', headers=HEADERS, json={
                'mutation_id':uuid.uuid4().hex, 'artifact_id':'result',
                'size':len(DATA), 'sha256':hashlib.sha256(DATA).hexdigest()})
            assert result.status_code == 200
            upload = result.json()['upload_id']
            yield app, client, ROOT+'/artifacts/uploads/'+upload, root, store
        app.state.engine.dispose()


def stage_count(root, store):
    if store:
        result = store.client.list_objects_v2(Bucket=store.bucket, Prefix=store.prefix+'/staging/')
        return result.get('KeyCount', 0)
    return len(list((root/'staging').iterdir()))


def test_accepted_retries_reuse_one_verified_stage(service):
    app, client, endpoint, root, store = service
    for _ in range(5):
        response = client.put(endpoint, headers=HEADERS, content=DATA)
        assert response.status_code == 200, response.text
        assert stage_count(root, store) == 1
    with app.state.engine.connect() as db:
        filename = db.execute(select(uploads.c.staged_file)).scalar()
    assert filename
    final = client.post(endpoint+'/finalize', headers=HEADERS, json={'mutation_id':uuid.uuid4().hex})
    assert final.status_code == 200, final.text
    assert client.get(ROOT+'/artifacts/result/content', headers=HEADERS).content == DATA


def test_concurrent_upload_loser_keeps_winner_and_leaves_one_stage(service):
    app, _, endpoint, root, store = service
    ready = threading.Barrier(2)
    def upload():
        def chunks():
            ready.wait(timeout=10)
            yield DATA
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.put(endpoint, headers=HEADERS, content=chunks())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(), range(2)))
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert stage_count(root, store) == 1
    if store:
        assert list((root/'staging').iterdir()) == []


def test_database_failure_after_staging_removes_unaccepted_bytes(service):
    app, client, endpoint, root, store = service
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE sync_artifact_uploads SET'):
            raise RuntimeError('synthetic DB acceptance failure')
    event.listen(app.state.engine, 'before_cursor_execute', fail)
    try:
        response = client.put(endpoint, headers=HEADERS, content=DATA)
        assert response.status_code == 500
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', fail)
    assert stage_count(root, store) == 0
    assert list((root/'staging').iterdir()) == []
    with app.state.engine.connect() as db:
        assert db.execute(select(uploads.c.state)).scalar() == 'staged'
    assert client.put(endpoint, headers=HEADERS, content=DATA).status_code == 200


def test_revocation_during_stream_creates_no_accepted_stage(service):
    app, client, endpoint, root, store = service
    def chunks():
        with app.state.engine.begin() as db:
            db.execute(update(members).values(role='revoked'))
        yield DATA
    response = client.put(endpoint, headers=HEADERS, content=chunks())
    assert response.status_code == 403
    assert stage_count(root, store) == 0
    assert list((root/'staging').iterdir()) == []


def test_lost_commit_acknowledgement_never_deletes_accepted_bytes(service):
    app, client, endpoint, root, store = service
    staged = False
    def mark(connection, cursor, statement, parameters, context, executemany):
        nonlocal staged
        if statement.startswith('UPDATE sync_artifact_uploads SET'):
            staged = True
    def lost_ack(connection):
        nonlocal staged
        if staged:
            staged = False
            connection.connection.dbapi_connection.commit()
            raise RuntimeError('synthetic lost commit acknowledgement')
    event.listen(app.state.engine, 'before_cursor_execute', mark)
    event.listen(app.state.engine, 'commit', lost_ack)
    try:
        assert client.put(endpoint, headers=HEADERS, content=DATA).status_code == 500
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', mark)
        event.remove(app.state.engine, 'commit', lost_ack)
    assert stage_count(root, store) == 1
    if store:
        assert list((root/'staging').iterdir()) == []
    assert client.put(endpoint, headers=HEADERS, content=DATA).status_code == 200
    final = client.post(endpoint+'/finalize', headers=HEADERS, json={'mutation_id':uuid.uuid4().hex})
    assert final.status_code == 200, final.text
    assert client.get(ROOT+'/artifacts/result/content', headers=HEADERS).content == DATA


def test_s3_put_succeeded_but_response_failed_is_cleaned(service, monkeypatch):
    _, client, endpoint, root, store = service
    if store is None:
        pytest.skip('S3-only ambiguous upload response')
    original = store.stage
    def fail(*args):
        original(*args)
        raise HTTPException(503, 'artifact_storage_unavailable')
    monkeypatch.setattr(store, 'stage', fail)
    assert client.put(endpoint, headers=HEADERS, content=DATA).status_code == 503
    assert stage_count(root, store) == 0
    assert list((root/'staging').iterdir()) == []
