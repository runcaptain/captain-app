"""Failure injection with a fake keychain; no real OS credentials are touched."""
import json

import pytest

from captain_search.collaboration import credentials


class Keychain:
    def __init__(self):
        self.values = {}
        self.fail = set()

    def set_password(self, service, account, value):
        self.values[account] = value

    def get_password(self, service, account):
        return self.values.get(account)

    def delete_password(self, service, account):
        if account in self.fail:
            raise RuntimeError('private backend exception')
        self.values.pop(account, None)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'cache'))
    backend = Keychain()
    monkeypatch.setattr(credentials, '_keyring', lambda: backend)
    return backend


def save(token='private-token'):
    return credentials.save('https://team.example', 'team', token, 'verified-user')


def test_logout_retry_after_restart_removes_failed_credential(store, monkeypatch):
    config = save()
    store.fail.add(config['account'])
    with pytest.raises(credentials.KeychainUnavailable, match='retry sign out'):
        credentials.delete()
    assert credentials.read() is None
    pending = credentials.home() / 'credential-cleanup.json'
    assert json.loads(pending.read_text()) == [config['account']]
    assert pending.stat().st_mode & 0o077 == 0
    assert 'private-token' not in pending.read_text()
    assert 'verified-user' not in pending.read_text()
    # A fresh backend instance has only persisted OS entries, no in-memory queue.
    fresh = Keychain()
    fresh.values = store.values
    monkeypatch.setattr(credentials, '_keyring', lambda: fresh)
    credentials.delete()
    assert fresh.values == {}
    assert not pending.exists()
    credentials.delete()  # repeated sign-out is harmless


def test_replacement_cleanup_failure_retains_old_account_for_logout_retry(store):
    old = save('old-private-token')
    store.fail.add(old['account'])
    with pytest.raises(credentials.KeychainUnavailable, match='previous keychain'):
        save('new-private-token')
    current = credentials.read()
    assert current['account'] != old['account']
    assert credentials.token(current)['token'] == 'new-private-token'
    assert credentials._pending_accounts() == {old['account']}
    with pytest.raises(credentials.KeychainUnavailable):
        credentials.delete()
    assert credentials.read() is None
    assert current['account'] not in store.values
    assert old['account'] in store.values
    store.fail.clear()
    credentials.delete()
    assert not store.values
    assert not credentials._pending_accounts()


def test_failed_replacement_publication_keeps_active_credential(store, monkeypatch):
    old = save()
    real = credentials.os.replace
    def replace(source, destination):
        if destination.name == 'connection.json':
            raise OSError('synthetic replacement failure')
        return real(source, destination)
    monkeypatch.setattr(credentials.os, 'replace', replace)
    with pytest.raises(OSError):
        save('new-private-token')
    assert credentials.read() == old
    assert set(store.values) == {old['account']}
    assert not credentials._pending_accounts()


def test_failed_publication_and_failed_cleanup_can_be_retried(store, monkeypatch):
    old = save()
    real = credentials.os.replace
    def replace(source, destination):
        if destination.name == 'connection.json':
            store.fail.update(set(store.values) - {old['account']})
            raise OSError('synthetic replacement failure')
        return real(source, destination)
    monkeypatch.setattr(credentials.os, 'replace', replace)
    with pytest.raises(credentials.KeychainUnavailable, match='cleanup needs retry'):
        save('new-private-token')
    assert credentials.read() == old
    orphan = set(store.values) - {old['account']}
    assert len(orphan) == 1 and credentials._pending_accounts() == orphan
    store.fail.clear()
    credentials.delete()
    assert not store.values
    assert credentials.read() is None
    assert not credentials._pending_accounts()


def test_keychain_unavailable_during_logout_keeps_retry_record(store, monkeypatch):
    config = save()
    real = credentials._keyring
    def unavailable():
        raise RuntimeError('private initialization detail')
    monkeypatch.setattr(credentials, '_keyring', unavailable)
    with pytest.raises(credentials.KeychainUnavailable) as error:
        credentials.delete()
    assert 'private initialization' not in str(error.value)
    assert credentials.read() is None
    assert credentials._pending_accounts() == {config['account']}
    monkeypatch.setattr(credentials, '_keyring', real)
    credentials.delete()
    assert not store.values
