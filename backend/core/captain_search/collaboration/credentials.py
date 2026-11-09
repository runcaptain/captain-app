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


def _sync_directory():
    if os.name != 'nt':
        directory = os.open(home(), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _pending_accounts():
    try:
        value = json.loads((home() / 'credential-cleanup.json').read_text())
    except FileNotFoundError:
        return set()
    if not isinstance(value, list) or not all(isinstance(account, str) for account in value):
        raise ValueError('Invalid pending credential cleanup')
    for account in value:
        uuid.UUID(account)
    return set(value)


def _write_pending(accounts):
    """Only opaque account locators are stored, never credentials or identity."""
    path = home() / 'credential-cleanup.json'
    if not accounts:
        path.unlink(missing_ok=True)
        _sync_directory()
        return
    fd, name = tempfile.mkstemp(prefix='.credential-cleanup-', dir=home())
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(sorted(accounts), out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        _sync_directory()
    finally:
        Path(name).unlink(missing_ok=True)


def _record_pending(account):
    accounts = _pending_accounts()
    accounts.add(account)
    _write_pending(accounts)


def _cleanup_pending(backend, message):
    accounts = _pending_accounts()
    if not accounts:
        return
    active = read()
    remaining = set()
    for account in accounts:
        # A failed replacement may leave the old connection active. Its locator
        # is still durable, so it is neither orphaned nor eligible for deletion.
        if active and account == active['account']:
            continue
        try:
            if backend.get_password(_service(), account) is not None:
                backend.delete_password(_service(), account)
        except Exception:
            remaining.add(account)
    _write_pending(remaining)
    if remaining:
        raise KeychainUnavailable(message) from None


def save(server, workspace, token, identity):
    with lock():
        return _save(server, workspace, token, identity)


def _save(server, workspace, token, identity):
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
        if old:
            # Journal the old locator before publishing its replacement.
            _record_pending(old['account'])
        os.replace(name, root / 'connection.json')
        replaced = True
        _sync_directory()
    except Exception:
        # Once published, the locator must retain its keychain entry even if
        # directory fsync fails. A later status can safely restore it.
        if not replaced:
            _record_pending(config['account'])
            _cleanup_pending(backend, 'Connection save failed; OS keychain cleanup needs retry')
        raise
    finally:
        if name:
            Path(name).unlink(missing_ok=True)
    _cleanup_pending(backend, 'Connected, but previous keychain credential cleanup failed; retry sign out')
    return config


def delete():
    with lock():
        config = read()
        if config:
            # Persist the locator before hiding the connection. A failed OS
            # deletion remains retryable after sign-out and process restart.
            _record_pending(config['account'])
            (home() / 'connection.json').unlink(missing_ok=True)
            _sync_directory()
        if _pending_accounts():
            try:
                backend = _keyring()
            except Exception:
                raise KeychainUnavailable('Signed out locally, but OS keychain removal failed; retry sign out') from None
            _cleanup_pending(backend, 'Signed out locally, but OS keychain removal failed; retry sign out')


def prepare_private_file(path):
    """Use POSIX mode 0600 where supported; Windows inherits directory ACLs."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)
        else:
            # Windows has no fchmod. chmod keeps the file writable there;
            # confidentiality comes from the containing user's directory ACL.
            os.chmod(path, 0o600)
    finally:
        os.close(fd)


@contextmanager
def lock():
    """Serialize saved credential changes with active cache use across processes."""
    if getattr(_local, 'active', False):
        yield
        return
    root = home()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / 'connection.lock'
    prepare_private_file(path)
    db = sqlite3.connect(path, timeout=35)
    try:
        db.execute('BEGIN IMMEDIATE')
        _local.active = True
        yield
    finally:
        _local.active = False
        db.close()
