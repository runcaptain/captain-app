"""Native OS credential storage. No plaintext or fallback keyring backends."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import sqlite3
import threading
from contextlib import contextmanager

_local = threading.local()


class KeychainUnavailable(ValueError):
    pass


def home():
    return Path(os.environ.get('CST_SYNC_HOME', str(Path.home() / '.cache/captain/sync')))


def _keyring():
    import keyring
    try:
        backend = keyring.get_keyring()
    except Exception:
        raise KeychainUnavailable("Unable to initialize the native OS keychain") from None
    name = type(backend).__module__ + '.' + type(backend).__name__
    if name not in ('keyring.backends.macOS.Keyring', 'keyring.backends.Windows.WinVaultKeyring',
                    'keyring.backends.SecretService.Keyring'):
        raise KeychainUnavailable('A supported native OS keychain is required')
    return backend


def _service():
    return 'captain-team-sync-' + hashlib.sha256(str(home().resolve()).encode()).hexdigest()[:24]


def read():
    path = home() / 'connection.json'
    try:
        with path.open() as f:
            value = json.load(f)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or set(value) != {'server', 'workspace', 'account'} or not all(isinstance(v, str) and v for v in value.values()):
        raise ValueError('Invalid saved team connection')
    # The account is an opaque keychain locator, never a caller-selected user ID.
    uuid.UUID(value['account'])
    return value


def token(config):
    try:
        raw = _keyring().get_password(_service(), config['account'])
        if raw is None:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'token', 'server', 'workspace', 'identity'}
                or not all(isinstance(v, str) and v for v in value.values())
                or value['server'] != config['server'] or value['workspace'] != config['workspace']):
            raise ValueError('Credential binding mismatch')
        return value
    except Exception:
        raise KeychainUnavailable('Unable to read the native OS keychain') from None


def save(server, workspace, token, identity):
    backend = _keyring()
    root = home()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = read()
    config = {'server': server, 'workspace': workspace, 'account': str(uuid.uuid4())}
    try:
        backend.set_password(_service(), config['account'], json.dumps(
            {'token': token, 'server': server, 'workspace': workspace, 'identity': identity}))
    except Exception:
        raise KeychainUnavailable('Unable to save to the native OS keychain') from None
    name = None
    replaced = False
    try:
        fd, name = tempfile.mkstemp(prefix='.connection-', dir=root)
        with os.fdopen(fd, 'w') as f:
            json.dump(config, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, root / 'connection.json')
        replaced = True
        if os.name != 'nt':
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except Exception:
        # Once published, the locator must retain its keychain entry even if
        # directory fsync fails. A later status can safely restore it.
        if not replaced:
            try:
                backend.delete_password(_service(), config['account'])
            except Exception:
                pass
        raise
    finally:
        if name:
            Path(name).unlink(missing_ok=True)
    if old:
        try:
            backend.delete_password(_service(), old['account'])
        except Exception:
            # New connection is durable. Old locator is no longer reachable by
            # the app, but caller must see that keychain cleanup needs attention.
            raise KeychainUnavailable('Connected, but previous keychain credential cleanup failed') from None
    return config


def delete():
    config = read()
    # Removing the locator first hides the cache even if OS deletion is denied.
    (home() / 'connection.json').unlink(missing_ok=True)
    if config:
        try:
            backend = _keyring()
            if backend.get_password(_service(), config['account']) is not None:
                backend.delete_password(_service(), config['account'])
        except Exception:
            raise KeychainUnavailable('Signed out locally, but OS keychain removal failed') from None


@contextmanager
def lock():
    """Serialize saved credential changes with active cache use across processes."""
    if getattr(_local, 'active', False):
        yield
        return
    root = home()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / 'connection.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=35)
    try:
        db.execute('BEGIN IMMEDIATE')
        _local.active = True
        yield
    finally:
        _local.active = False
        db.close()
