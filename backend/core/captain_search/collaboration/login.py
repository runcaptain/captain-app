"""Operator-configured OAuth device sign-in. Secrets live only in this process.

RFC 8628 polling, with the company API as the authority for token and membership
validation. No provider discovery, refresh token persistence, or browser callback.
"""
import hashlib
import math
import os
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote, urlparse

import httpx
from . import credentials

_lock = threading.RLock()
_pending = None
_credential = None


def validate_origin(server):
    server = server.rstrip('/')
    parsed = urlparse(server)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path):
        raise ValueError('Configure a team server origin')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Team server requires HTTPS or a loopback tunnel')
    return server


def origin():
    saved = credentials.read()
    return validate_origin(saved['server'] if saved else os.environ.get('CST_SYNC_SERVER', ''))


def _https(value):
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Login endpoints require HTTPS without credentials or fragments')
    return value


def _config():
    values = {key: os.environ.get('CST_SYNC_OIDC_' + key, '') for key in
              ('DEVICE_URL', 'TOKEN_URL', 'CLIENT_ID', 'AUDIENCE')}
    if not all(values.values()):
        raise ValueError('Company login is not configured by the operator')
    _https(values['DEVICE_URL'])
    _https(values['TOKEN_URL'])
    return values


def _workspace():
    saved = credentials.read()
    value = saved['workspace'] if saved else os.environ.get('CST_SYNC_WORKSPACE', '')
    if not value:
        raise ValueError('Configure a team workspace')
    return value


def _verify(server, workspace, token, expected_identity=None):
    with httpx.Client(timeout=15, follow_redirects=False) as http:
        response = http.get(server + '/v1/workspaces/' + quote(workspace, safe='') + '/identity',
                            headers={'Authorization': 'Bearer ' + token})
        response.raise_for_status()
        value = response.json()
        if (not isinstance(value, dict) or value.get('workspace') != workspace
                or not isinstance(value.get('user_id'), str) or not value['user_id']):
            raise ValueError('Invalid team authorization response')
    identity = hashlib.sha256(('device:' + value['user_id']).encode()).hexdigest()
    if expected_identity is not None and identity != expected_identity:
        raise ValueError('Saved credential identity changed')
    from .session import _connection
    with _connection(workspace, token, identity, server=server) as client:
        client.bootstrap()
    return identity


def connect(server, workspace, token):
    global _credential, _pending
    with _lock, credentials.lock():
        server = validate_origin(server)
        if not workspace or len(workspace) > 200 or not token or len(token) > 16384 or any(c.isspace() for c in token):
            raise ValueError('Invalid team connection')
        # Check platform support before any remote operation or cache creation.
        credentials._keyring()
        identity = _verify(server, workspace, token)
        saved = credentials.save(server, workspace, token, identity)
        _pending = None
        _credential = {'token': token, 'identity': identity, 'scope': (server, workspace),
                       'expires': math.inf, 'account': saved['account'], 'source': 'keychain'}
        return status()


def _token():
    global _credential
    saved = credentials.read()
    if _credential and (_credential['expires'] <= time.monotonic()
                        or _credential['scope'] != (origin(), _workspace())
                        or (_credential.get('source') == 'keychain' and
                            (not saved or _credential['account'] != saved['account']))):
        _credential = None
    if not _credential and saved:
        stored = credentials.token(saved)
        if not stored:
            return ''
        token = stored['token']
        # A restart requires online verification. A trusted active process can
        # continue using its cache offline until denial or explicit sign-out.
        try:
            identity = _verify(validate_origin(saved['server']), saved['workspace'], token, stored['identity'])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                from .session import _connection
                with _connection(saved['workspace'], token, stored['identity'], server=saved['server']) as client:
                    client._check(exc.response)
            raise
        _credential = {'token': token, 'identity': identity, 'scope': (saved['server'], saved['workspace']),
                       'expires': math.inf, 'account': saved['account'], 'source': 'keychain'}
    return (_credential['token'] if _credential else os.environ.get('CST_SYNC_TOKEN', ''))


@contextmanager
def credential():
    # Logout cannot race a cache operation and let its response repopulate data.
    with _lock, credentials.lock():
        yield _token()


def cache_identity():
    with _lock, credentials.lock():
        return _credential["identity"] if _credential else None


def status():
    with _lock, credentials.lock():
        try:
            server, workspace = origin(), _workspace()
        except ValueError:
            return {'server': '', 'workspace': '', 'authenticated': False,
                    'login_available': False, 'auth_source': None}
        try:
            _config()
            available = True
        except ValueError:
            available = False
        try:
            token = _token()
        except (httpx.HTTPStatusError, httpx.RequestError):
            return {'server': server, 'workspace': workspace, 'authenticated': False,
                    'login_available': available, 'auth_source': None, 'restore_pending': bool(credentials.read())}
        return {'server': server, 'workspace': workspace, 'authenticated': bool(token),
                'login_available': available,
                'auth_source': (_credential.get('source', 'device') if _credential else 'environment') if token else None}


def _json(response):
    if len(response.content) > 65536:
        raise ValueError('Invalid identity provider response')
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError('Invalid identity provider response')
    return value


def _seconds(value, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value > maximum:
        raise ValueError('Invalid identity provider expiry or interval')
    return value


def start():
    global _pending
    with _lock, credentials.lock():
        config = _config()
        server, workspace = origin(), _workspace()
        _pending = None
        with httpx.Client(timeout=15, follow_redirects=False) as http:
            response = http.post(config['DEVICE_URL'], data={
                'client_id': config['CLIENT_ID'], 'audience': config['AUDIENCE'],
                'scope': os.environ.get('CST_SYNC_OIDC_SCOPE', 'openid')})
            response.raise_for_status()
            body = _json(response)
        for key in ('device_code', 'user_code', 'verification_uri'):
            if not isinstance(body.get(key), str) or not body[key] or len(body[key]) > 4096:
                raise ValueError('Invalid identity provider response')
        uri = _https(body['verification_uri'])
        lifetime = _seconds(body.get('expires_in'), 3600)
        interval = _seconds(body.get('interval', 5), 300)
        _pending = dict(config=config, scope=(server, workspace), code=body['device_code'],
                        expires=time.monotonic() + lifetime, next_poll=time.monotonic() + interval,
                        interval=interval)
        return {'state': 'pending', 'verification_uri': uri, 'user_code': body['user_code'],
                'expires_in': lifetime, 'interval': interval}


def poll():
    global _pending, _credential
    with _lock, credentials.lock():
        if not _pending:
            raise ValueError('Start company sign-in first')
        pending = _pending
        if pending['expires'] <= time.monotonic() or pending['scope'] != (origin(), _workspace()) or pending['config'] != _config():
            _pending = None
            raise ValueError('Company sign-in expired; start again')
        if time.monotonic() < pending['next_poll']:
            return {'state': 'pending', 'interval': math.ceil(pending['next_poll'] - time.monotonic())}
        pending['next_poll'] = time.monotonic() + pending['interval']
        config = pending['config']
        with httpx.Client(timeout=15, follow_redirects=False) as http:
            try:
                response = http.post(config['TOKEN_URL'], data={
                    'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                    'device_code': pending['code'], 'client_id': config['CLIENT_ID']})
            except httpx.TimeoutException:
                pending['interval'] = min(300, pending['interval'] * 2)
                pending['next_poll'] = time.monotonic() + pending['interval']
                return {'state': 'pending', 'interval': pending['interval']}
            body = _json(response)
            error = body.get('error')
            if error in ('authorization_pending', 'slow_down') and response.status_code == 400:
                if error == 'slow_down':
                    pending['interval'] += 5
                    pending['next_poll'] = time.monotonic() + pending['interval']
                return {'state': 'pending', 'interval': pending['interval']}
            # Any other error ends this attempt. Never forward provider error text.
            _pending = None
            response.raise_for_status()
            token = body.get('access_token')
            token_type = body.get('token_type')
            if error or not isinstance(token_type, str) or token_type.lower() != 'bearer' or not isinstance(token, str) or not token or len(token) > 16384:
                raise ValueError('Identity provider did not return a bearer access token')
            lifetime = _seconds(body.get('expires_in'), 86400)
            expires = time.monotonic() + lifetime
            server, workspace = pending['scope']
            authorized = http.get(server + '/v1/workspaces/' + quote(workspace, safe='') + '/identity',
                                  headers={'Authorization': 'Bearer ' + token})
            authorized.raise_for_status()
            identity_response = authorized.json()
            if (not isinstance(identity_response, dict) or identity_response.get('workspace') != workspace
                    or not isinstance(identity_response.get('user_id'), str) or not identity_response['user_id']):
                raise ValueError('Invalid team authorization response')
            identity = hashlib.sha256(('device:' + identity_response['user_id']).encode()).hexdigest()
        # Reconcile current permissions before exposing any cached records. Stable,
        # server-verified user IDs preserve offline drafts across token renewal.
        from .session import _connection
        with _connection(workspace, token, identity) as client:
            client.bootstrap()
        if expires <= time.monotonic():
            raise ValueError('Company sign-in expired; start again')
        _credential = {'token': token, 'identity': identity, 'scope': pending['scope'],
                       'expires': expires}
        return {'state': 'authenticated'}


def logout():
    global _pending, _credential
    with _lock, credentials.lock():
        _pending = None
        _credential = None
        credentials.delete()
        # Operator-provided env credentials cannot be removed by a UI action.
        return {'state': 'environment_configured' if os.environ.get('CST_SYNC_TOKEN') else 'signed_out'}


def invalidate():
    global _credential
    with _lock, credentials.lock():
        _credential = None
