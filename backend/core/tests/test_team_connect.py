"""Token connection tests use a fake in-memory keychain, not OS acceptance."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from captain_search.collaboration import credentials, login
from captain_search.collaboration.session import connection

_native_keyring = credentials._keyring


class MemoryKeychain:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[service, account] = password

    def delete_password(self, service, account):
        del self.values[service, account]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'cache'))
    for name in ('CST_SYNC_TOKEN', 'CST_SYNC_SERVER', 'CST_SYNC_WORKSPACE'):
        monkeypatch.delenv(name, raising=False)
    login._credential = None
    login._pending = None
    keychain = MemoryKeychain()
    monkeypatch.setattr(credentials, '_keyring', lambda: keychain)
    state = {'offline': False, 'denied': None, 'calls': 0, 'user_id': 'alice'}
    def handle(request):
        state['calls'] += 1
        if state['offline']:
            raise httpx.ConnectError('secret-network-detail', request=request)
        if state['denied']:
            return httpx.Response(state['denied'], json={'detail': 'secret-rejection-detail'})
        if request.url.path.endswith('/identity'):
            return httpx.Response(200, json={'workspace': 'team', 'user_id': state['user_id'], 'epoch': 1, 'role': 'editor'})
        return httpx.Response(200, json={'records': [], 'cursor': 0, 'epoch': 1})
    real = httpx.Client
    monkeypatch.setattr(login.httpx, 'Client', lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs))
    yield tmp_path / 'cache', keychain, state
    login.logout()


def test_persist_restore_and_logout_keep_token_out_of_files(setup):
    root, keychain, state = setup
    result = login.connect('https://team.example', 'team', 'secret-token')
    assert result['auth_source'] == 'keychain' and result['authenticated']
    assert [json.loads(v)['token'] for v in keychain.values.values()] == ['secret-token']
    assert 'secret-token' not in (root / 'connection.json').read_text()
    assert (root / 'connection.json').stat().st_mode & 0o077 == 0
    with connection() as client:
        client.queue_edit('draft', 'investigation', {'title': 'Retain me'})
    login._credential = None  # daemon restart simulation
    count = state['calls']
    assert login.status()['authenticated']
    assert state['calls'] == count + 2  # identity and permission snapshot
    with connection() as client:
        assert client.drafts()[0]['mutation']['body']['title'] == 'Retain me'
    assert login.logout()['state'] == 'signed_out'
    assert not keychain.values
    assert not (root / 'connection.json').exists()
    assert not login.status()['authenticated']


def test_rotation_uses_verified_principal_cache(setup):
    _, keychain, _ = setup
    login.connect('https://team.example', 'team', 'first-token')
    with connection() as client:
        client.queue_edit('draft', 'investigation', {'title': 'Retain me'})
    login.connect('https://team.example', 'team', 'rotated-token')
    assert [json.loads(v)['token'] for v in keychain.values.values()] == ['rotated-token']
    with connection() as client:
        assert len(client.drafts()) == 1


def test_different_principal_cannot_read_previous_drafts(setup):
    _, _, state = setup
    login.connect('https://team.example', 'team', 'alice-token')
    with connection() as client:
        client.queue_edit('private', 'investigation', {'title': 'Alice'})
    state['user_id'] = 'bob'
    login.connect('https://team.example', 'team', 'bob-token')
    with connection() as client:
        assert client.drafts() == []


def test_offline_current_process_allowed_restart_requires_verification(setup):
    _, _, state = setup
    login.connect('https://team.example', 'team', 'secret-token')
    state['offline'] = True
    with connection() as client:
        client.queue_edit('draft', 'investigation', {'title': 'Offline'})
        assert len(client.drafts()) == 1
    login._credential = None
    assert not login.status()['authenticated']
    with pytest.raises(httpx.ConnectError):
        with connection():
            pass
    state['offline'] = False
    with connection() as client:
        assert len(client.drafts()) == 1


def test_rejected_token_is_not_persisted(setup):
    root, keychain, state = setup
    state['denied'] = 401
    with pytest.raises(httpx.HTTPStatusError):
        login.connect('https://team.example', 'team', 'secret-token')
    assert not keychain.values
    assert not (root / 'connection.json').exists()


def test_plaintext_backend_rejected_before_network(setup, monkeypatch):
    _, _, state = setup
    import keyring
    # Test the production backend allowlist directly, without any real write.
    monkeypatch.setattr(keyring, 'get_keyring', lambda: MemoryKeychain())
    with pytest.raises(credentials.KeychainUnavailable):
        _native_keyring()


def test_insecure_origin_rejected_before_network(setup):
    _, _, state = setup
    with pytest.raises(ValueError):
        login.connect('http://remote.example', 'team', 'secret-token')
    assert state['calls'] == 0


def test_connect_route_validation_and_failures_never_echo_token(setup, monkeypatch):
    from captain_search import server
    _, keychain, state = setup
    with TestClient(server.app) as client:
        assert client.post('/api/team/connect', json={}).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + server.TOKEN
        for body in ({'token': 'secret-token'}, {'server': 'https://team.example', 'workspace': 'team', 'token': {'secret-token': 1}},
                     {'server': 'https://team.example', 'workspace': 'team', 'token': 'secret-token' * 4000}):
            response = client.post('/api/team/connect', json=body)
            assert response.status_code == 400
            assert 'secret-token' not in response.text
        body = {'server': 'https://team.example', 'workspace': 'team', 'token': 'secret-token'}
        state['denied'] = 401
        response = client.post('/api/team/connect', json=body)
        assert response.status_code == 401 and 'secret' not in response.text
        state['denied'] = None
        def broken(*args):
            raise RuntimeError('secret-keychain-error')
        monkeypatch.setattr(keychain, 'set_password', broken)
        response = client.post('/api/team/connect', json=body)
        assert response.status_code == 503 and 'secret' not in response.text


def test_keychain_missing_does_not_fall_back_to_environment(setup, monkeypatch):
    _, keychain, _ = setup
    login.connect('https://team.example', 'team', 'secret-token')
    keychain.values.clear()
    login._credential = None
    monkeypatch.setenv('CST_SYNC_TOKEN', 'other-user')
    assert not login.status()['authenticated']


def test_edited_saved_origin_cannot_forward_keychain_token(setup):
    root, _, state = setup
    login.connect('https://team.example', 'team', 'secret-token')
    login._credential = None
    config = json.loads((root / 'connection.json').read_text())
    config['server'] = 'https://untrusted.example'
    (root / 'connection.json').write_text(json.dumps(config))
    calls = state['calls']
    with pytest.raises(credentials.KeychainUnavailable):
        login.status()
    assert state['calls'] == calls


def test_directory_fsync_failure_keeps_published_credential(setup, monkeypatch):
    root, keychain, _ = setup
    real = credentials.os.fsync
    count = [0]
    def fsync(fd):
        count[0] += 1
        if count[0] == 2:
            raise OSError('directory sync failed')
        return real(fd)
    monkeypatch.setattr(credentials.os, 'fsync', fsync)
    with pytest.raises(OSError):
        login.connect('https://team.example', 'team', 'secret-token')
    config = credentials.read()
    assert credentials.token(config)['token'] == 'secret-token'
    assert login.status()['authenticated']


@pytest.mark.parametrize('denial, retained', [(401, True), (403, False)])
def test_restart_denial_hides_or_purges_drafts(setup, denial, retained):
    _, _, state = setup
    login.connect('https://team.example', 'team', 'secret-token')
    with connection() as client:
        client.queue_edit('draft', 'investigation', {'title': 'Local'})
    login._credential = None
    state['denied'] = denial
    assert not login.status()['authenticated']
    state['denied'] = None
    login.connect('https://team.example', 'team', 'replacement-token')
    with connection() as client:
        assert bool(client.drafts()) is retained
