"""Synthetic S3 service contracts using moto, not live AWS evidence."""
import hashlib
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

from captain_search.collaboration.artifacts import MAX_BYTES, S3ArtifactStore
from captain_search.collaboration.server import create_app

DATA = b'synthetic artifact bytes'
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client('s3', region_name='us-east-1')
        client.create_bucket(Bucket='synthetic-artifacts')
        yield S3ArtifactStore('synthetic-artifacts', client=client)


def test_immutable_workspace_scoped_blobs(store, tmp_path):
    path = tmp_path/'upload'
    path.write_bytes(DATA)
    name = uuid.uuid4().hex + '.upload'
    store.stage('../../workspace', name, path)
    store.publish('../../workspace', name, len(DATA), SHA)
    store.publish('../../workspace', name, len(DATA), SHA)
    assert store.download('../../workspace', len(DATA), SHA).body == DATA
    key = store.key('../../workspace', SHA)
    assert '..' not in key
    assert store.client.head_object(Bucket=store.bucket, Key=key)['ServerSideEncryption'] == 'AES256'
    with pytest.raises(HTTPException, match='artifact_unavailable'):
        store.download('another-workspace', len(DATA), SHA)
    with pytest.raises(HTTPException, match='stored_artifact_integrity_failure'):
        store.put(key, b'different')
    assert store.read(key) == DATA


def test_tampered_stage_missing_blob_and_bounds(store, tmp_path):
    path = tmp_path/'upload'
    path.write_bytes(b'corrupt')
    name = uuid.uuid4().hex + '.upload'
    store.stage('team', name, path)
    with pytest.raises(HTTPException, match='artifact_integrity_mismatch'):
        store.publish('team', name, len(DATA), SHA)
    with pytest.raises(HTTPException, match='artifact_not_ready'):
        store.publish('team', uuid.uuid4().hex+'.upload', len(DATA), SHA)
    with pytest.raises(HTTPException, match='artifact_too_large'):
        store.put(store.key('team', SHA), b'x'*(MAX_BYTES+1))
    store.client.put_object(Bucket=store.bucket, Key=store.key('team', SHA), Body=b'x'*(MAX_BYTES+1))
    with pytest.raises(HTTPException, match='artifact_too_large'):
        store.download('team', len(DATA), SHA)


def test_storage_failure_is_sanitized(store, monkeypatch):
    def fail(**kwargs):
        raise ClientError({'Error': {'Code':'AccessDenied', 'Message':'secret credential text'}}, 'GetObject')
    monkeypatch.setattr(store.client, 'get_object', fail)
    with pytest.raises(HTTPException) as error:
        store.read(store.key('team', SHA))
    assert error.value.status_code == 503
    assert error.value.detail == 'artifact_storage_unavailable'


def test_api_cross_instance_publish_privacy_and_revocation(store, tmp_path, monkeypatch):
    database = 'sqlite:///' + str(tmp_path/'shared.db')
    fixture = {'users': [{'id':'alice', 'token':'alice-token'}, {'id':'bob', 'token':'bob-token'}],
               'workspaces':[{'id':'team', 'members':{'alice':'admin', 'bob':'editor'}}]}
    first = create_app(database, fixture, tmp_path/'first', allow_dev_tokens=True, artifact_store=store)
    second = create_app(database, None, tmp_path/'second', allow_dev_tokens=True, artifact_store=store)
    alice = {'Authorization':'Bearer alice-token'}
    bob = {'Authorization':'Bearer bob-token'}
    root = '/v1/workspaces/team'
    with TestClient(first) as one, TestClient(second) as two:
        request = {'mutation_id':uuid.uuid4().hex, 'artifact_id':'private-result',
                   'sha256':SHA, 'size':len(DATA), 'visibility':'private'}
        response = one.post(root+'/artifacts/uploads', headers=alice, json=request)
        assert response.status_code == 200, response.text
        endpoint = root+'/artifacts/uploads/'+response.json()['upload_id']
        assert one.put(endpoint, headers=alice, content=DATA).status_code == 200
        assert list((tmp_path/'first'/'staging').iterdir()) == []
        finalize = {'mutation_id':uuid.uuid4().hex}
        original_put = store.client.put_object
        def fail(**kwargs):
            raise ClientError({'Error': {'Code':'AccessDenied', 'Message':'private storage details'}}, 'PutObject')
        monkeypatch.setattr(store.client, 'put_object', fail)
        failure = two.post(endpoint+'/finalize', headers=alice, json=finalize)
        assert failure.status_code == 503
        assert 'private storage details' not in failure.text
        assert one.get(root+'/snapshot', headers=alice).json()['records'] == []
        monkeypatch.setattr(store.client, 'put_object', original_put)
        response = two.post(endpoint+'/finalize', headers=alice, json=finalize)
        assert response.status_code == 200, response.text
        assert one.post(endpoint+'/finalize', headers=alice, json=finalize).json() == response.json()
        download = root+'/artifacts/private-result/content'
        assert two.get(download, headers=bob).status_code == 404
        assert two.get(download, headers=alice).content == DATA
        assert one.get(download, headers=alice).headers['cache-control'] == 'no-store'
        assert one.put(root+'/members/alice', headers=alice, json={'role':'revoked'}).status_code == 200
        assert two.get(download, headers=alice).status_code == 403
    first.state.engine.dispose()
    second.state.engine.dispose()
