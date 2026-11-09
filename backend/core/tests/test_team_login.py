"""Synthetic device provider responses, never evidence of live company login."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from captain_search.collaboration import login


@pytest.fixture
def flow(monkeypatch, tmp_path):
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'cache'))
    login.logout()
    monkeypatch.delenv('CST_SYNC_TOKEN', raising=False)
    for key, value in {'SERVER': 'https://team.example', 'WORKSPACE': 'team',
                       'OIDC_DEVICE_URL': 'https://identity.example/device',
                       'OIDC_TOKEN_URL': 'https://identity.example/token',
                       'OIDC_CLIENT_ID': 'public-client', 'OIDC_AUDIENCE': 'team-api'}.items():
        monkeypatch.setenv('CST_SYNC_' + key, value)
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'cache'))
    clock = [100.0]
    monkeypatch.setattr(login.time, 'monotonic', lambda: clock[0])
    calls = []
    replies = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/device':
            return httpx.Response(200, json={'device_code': 'secret-device', 'user_code': 'ABCD',
                'verification_uri': 'https://identity.example/activate', 'expires_in': 600, 'interval': 5})
        if request.url.path == '/token':
            status, body = replies.pop(0)
            return httpx.Response(status, json=body)
        if request.url.path.endswith('/identity'):
            if replies:
                status, body = replies.pop(0)
                return httpx.Response(status, json=body)
            return httpx.Response(200, json={'workspace': 'team', 'user_id': 'alice', 'epoch': 1, 'role': 'editor'})
        return httpx.Response(200, json={'records': [], 'cursor': 0, 'epoch': 1})
    real = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(login.httpx, 'Client', lambda **kwargs: real(transport=transport, **kwargs))
    yield clock, calls, replies
    login.logout()


def token():
    return {'access_token': 'secret-access', 'token_type': 'Bearer', 'expires_in': 300,
            'refresh_token': 'discarded-refresh'}


def test_device_login_validates_access_and_never_returns_secrets(flow):
    clock, calls, replies = flow
    assert not login.status()['authenticated']
    start = login.start()
    assert start['user_code'] == 'ABCD'
    assert 'secret' not in json.dumps(start)
    assert login.poll()['state'] == 'pending'
    assert len(calls) == 1
    clock[0] += 5
    replies.append((200, token()))
    assert login.poll() == {'state': 'authenticated'}
    assert calls[-2].url.path == '/v1/workspaces/team/identity'
    assert calls[-1].url.path == '/v1/workspaces/team/snapshot'
    assert calls[-1].headers['Authorization'] == 'Bearer secret-access'
    assert login.status()['auth_source'] == 'device'
    assert 'secret' not in json.dumps(login.status())
    with login.credential() as credential:
        assert credential == 'secret-access'
    clock[0] += 301
    assert not login.status()['authenticated']


def test_slow_down_pending_and_expiry(flow):
    clock, calls, replies = flow
    login.start()
    clock[0] += 5
    replies.append((400, {'error': 'slow_down'}))
    assert login.poll()['interval'] == 10
    clock[0] += 5
    assert login.poll()['state'] == 'pending'
    assert len(calls) == 2
    clock[0] += 5
    replies.append((400, {'error': 'authorization_pending'}))
    assert login.poll()['state'] == 'pending'
    clock[0] += 600
    with pytest.raises(ValueError):
        login.poll()
    assert not login.status()['authenticated']


@pytest.mark.parametrize('body', [
    {'error': 'access_denied'},
    {**token(), 'token_type': 'MAC'},
    {**token(), 'expires_in': -1},
    {**token(), 'expires_in': float('inf')},
])
def test_reject_invalid_token_response(flow, body):
    clock, _, replies = flow
    login.start()
    clock[0] += 5
    replies.append((200, body))
    with pytest.raises(ValueError):
        login.poll()
    assert not login.status()['authenticated']


def test_configuration_change_cannot_redirect_token(flow, monkeypatch):
    clock, calls, _ = flow
    login.start()
    monkeypatch.setenv('CST_SYNC_SERVER', 'https://other.example')
    clock[0] += 5
    with pytest.raises(ValueError):
        login.poll()
    assert len(calls) == 1


def test_unconfigured_and_insecure_login_fail_closed(flow, monkeypatch):
    monkeypatch.setenv('CST_SYNC_OIDC_TOKEN_URL', 'http://identity.example/token')
    assert not login.status()['login_available']
    with pytest.raises(ValueError):
        login.start()


def test_local_routes_require_daemon_auth_and_hide_provider_errors(flow):
    from captain_search import server
    clock, _, replies = flow
    with TestClient(server.app) as client:
        for method, path in [('get', 'connection'), ('post', 'login/start'), ('post', 'login/poll'), ('post', 'logout')]:
            assert getattr(client, method)('/api/team/' + path).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + server.TOKEN
        assert client.get('/api/team/connection').status_code == 200
        assert client.post('/api/team/login/start').status_code == 200
        clock[0] += 5
        replies.append((400, {'error': 'access_denied', 'error_description': 'secret-provider-details'}))
        response = client.post('/api/team/login/poll')
        assert response.status_code == 502
        assert 'secret' not in response.text
        assert client.post('/api/team/logout').json() == {'state': 'signed_out'}


def test_logout_cancels_pending_and_environment_remains_explicit(flow, monkeypatch):
    login.start()
    assert login.logout() == {'state': 'signed_out'}
    with pytest.raises(ValueError):
        login.poll()
    monkeypatch.setenv('CST_SYNC_TOKEN', 'operator-token')
    assert login.logout() == {'state': 'environment_configured'}
    assert login.status()['auth_source'] == 'environment'


def test_resign_in_retains_drafts_for_verified_user(flow):
    from captain_search.collaboration.session import connection
    clock, _, replies = flow
    login.start()
    clock[0] += 5
    replies.append((200, token()))
    login.poll()
    with connection() as client:
        client.queue_edit('draft', 'investigation', {'title': 'Unsynced'})
    login.logout()
    login.start()
    clock[0] += 5
    replies.append((200, {**token(), 'access_token': 'rotated-access'}))
    login.poll()
    with connection() as client:
        assert client.drafts()[0]['mutation']['body'] == {'title': 'Unsynced'}


def test_identity_endpoint_checks_token_and_membership(tmp_path):
    from captain_search.collaboration.server import create_app
    fixture = {'users': [{'id': 'alice', 'token': 'alice-test'}, {'id': 'bob', 'token': 'bob-test'}],
               'workspaces': [{'id': 'team', 'members': {'alice': 'admin'}}]}
    with TestClient(create_app('sqlite:///' + str(tmp_path / 'db'), fixture, allow_dev_tokens=True)) as client:
        endpoint = '/v1/workspaces/team/identity'
        assert client.get(endpoint).status_code == 401
        assert client.get(endpoint, headers={'Authorization': 'Bearer bob-test'}).status_code == 403
        response = client.get(endpoint, headers={'Authorization': 'Bearer alice-test'})
        assert response.status_code == 200
        assert response.json()['user_id'] == 'alice'
        assert response.json()['role'] == 'admin'


def test_rejected_membership_does_not_accept_provider_token(flow):
    clock, _, replies = flow
    login.start()
    clock[0] += 5
    replies.extend([(200, token()), (403, {'detail': 'workspace_access_denied'})])
    with pytest.raises(httpx.HTTPStatusError):
        login.poll()
    assert not login.status()['authenticated']


def test_relogin_reconciles_cached_records_before_exposing_them(flow):
    from captain_search.collaboration.session import connection
    clock, _, replies = flow
    login.start()
    clock[0] += 5
    replies.append((200, token()))
    login.poll()
    with connection() as client:
        with client.db:
            client._store({'id': 'formerly-visible', 'kind': 'config', 'body': {}, 'revision': 1})
    login.logout()
    login.start()
    clock[0] += 5
    replies.append((200, {**token(), 'access_token': 'rotated-access'}))
    login.poll()
    with connection() as client:
        assert client.get('formerly-visible') is None


def test_unauthorized_hides_drafts_until_verified_sign_in(flow):
    from captain_search.collaboration.session import connection
    clock, _, replies = flow
    login.start()
    clock[0] += 5
    replies.append((200, token()))
    login.poll()
    with connection() as client:
        client.queue_edit('offline', 'investigation', {'title': 'Keep this proposal'})
        with pytest.raises(httpx.HTTPStatusError):
            client._check(httpx.Response(401, request=httpx.Request('GET', 'https://team.example/snapshot')))
        assert client.drafts() == []
        assert client.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 1
    login.invalidate()
    assert not login.status()['authenticated']
    login.start()
    clock[0] += 5
    replies.append((200, {**token(), 'access_token': 'fresh-access'}))
    login.poll()
    with connection() as client:
        assert client.drafts()[0]['mutation']['body']['title'] == 'Keep this proposal'


def test_natural_expiry_prompts_ui_reauthentication(flow):
    from captain_search import server
    clock, calls, replies = flow
    login.start()
    clock[0] += 5
    replies.append((200, token()))
    login.poll()
    with TestClient(server.app, headers={'Authorization': 'Bearer ' + server.TOKEN}) as client:
        assert client.put('/api/team/records/expiry-draft/draft', json={'body': {'title': 'Keep'}}).status_code == 200
        clock[0] += 301
        before = len(calls)
        response = client.get('/api/team/records')
        assert response.status_code == 401
        assert 'preserved' in response.text
        assert len(calls) == before
        assert not login.status()['authenticated']
