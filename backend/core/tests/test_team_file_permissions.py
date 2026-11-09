"""Capability simulation on the host; this is not Windows runtime acceptance."""
import os

import pytest

from captain_search.collaboration import credentials, login
from captain_search.collaboration.session import connection


def test_missing_fchmod_allows_status_cache_and_logout(tmp_path, monkeypatch):
    monkeypatch.delattr(credentials.os, 'fchmod', raising=False)
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'cache'))
    monkeypatch.setenv('CST_SYNC_SERVER', 'https://team.example')
    monkeypatch.setenv('CST_SYNC_WORKSPACE', 'team')
    monkeypatch.setenv('CST_SYNC_TOKEN', 'synthetic-test-token')
    monkeypatch.setattr(login, '_credential', None)
    real_chmod = os.chmod
    touched = []
    def chmod(path, mode):
        touched.append((path.name, mode))
        return real_chmod(path, mode)
    monkeypatch.setattr(credentials.os, 'chmod', chmod)
    assert login.status()['authenticated']
    with connection() as client:
        client.queue_edit('offline', 'config', {'synthetic': True})
        assert client.drafts()[0]['mutation']['record_id'] == 'offline'
    assert login.logout()['state'] == 'environment_configured'
    assert any(name == 'connection.lock' for name, _ in touched)
    assert any(name.endswith('.lock.sqlite') for name, _ in touched)
    assert any(name.endswith('.sqlite') and not name.endswith('.lock.sqlite') for name, _ in touched)
    assert all(mode == 0o600 for _, mode in touched)


@pytest.mark.skipif(os.name == 'nt', reason='Windows privacy uses inherited ACLs, not POSIX mode bits')
@pytest.mark.parametrize('fallback', [False, True])
def test_unix_existing_file_permissions_remain_private(tmp_path, monkeypatch, fallback):
    if fallback:
        monkeypatch.delattr(credentials.os, 'fchmod', raising=False)
    path = tmp_path / 'cache.sqlite'
    path.write_text('synthetic')
    path.chmod(0o666)
    credentials.prepare_private_file(path)
    assert path.read_text() == 'synthetic'
    assert path.stat().st_mode & 0o777 == 0o600


def test_permission_failure_closes_descriptor(tmp_path, monkeypatch):
    monkeypatch.delattr(credentials.os, 'fchmod', raising=False)
    real_open, real_close = os.open, os.close
    opened, closed = [], []
    def open_file(*args):
        fd = real_open(*args)
        opened.append(fd)
        return fd
    def close_file(fd):
        closed.append(fd)
        return real_close(fd)
    def fail(*args):
        raise PermissionError('synthetic permission failure')
    monkeypatch.setattr(credentials.os, 'open', open_file)
    monkeypatch.setattr(credentials.os, 'close', close_file)
    monkeypatch.setattr(credentials.os, 'chmod', fail)
    with pytest.raises(PermissionError):
        credentials.prepare_private_file(tmp_path / 'cache.sqlite')
    assert opened == closed
