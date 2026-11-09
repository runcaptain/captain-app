import json
import socket
import threading
import time
import uuid

import pytest
import uvicorn
from typer.testing import CliRunner
from captain_search.cli.main import app
from pathlib import Path
from captain_search.collaboration.server import create_app


@pytest.fixture
def remote(tmp_path):
    fixture = {"users": [{"id": "alice", "token": "alice-cli-test"}, {"id": "bob", "token": "bob-cli-test"}],
               "workspaces": [{"id": "team", "members": {"alice": "admin", "bob": "editor"}}]}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app("sqlite:///" + str(tmp_path / "server.db"), fixture, allow_dev_tokens=True), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.02)
    assert server.started
    yield "http://127.0.0.1:" + str(sock.getsockname()[1])
    server.should_exit = True
    thread.join(5)
    sock.close()


def test_real_cli_two_users_conflict_and_private_cache(remote, tmp_path):
    runner = CliRunner()
    body = tmp_path / "edit.json"
    def call(user, *args):
        result = runner.invoke(app, ["--json", "team", *args], env={
            "CST_SYNC_SERVER": remote, "CST_SYNC_WORKSPACE": "team",
            "CST_SYNC_TOKEN": user + "-cli-test", "CST_SYNC_HOME": str(tmp_path / "cache")})
        assert result.exit_code == 0, result.output + str(result.exception)
        return json.loads(result.output)
    body.write_text('{"weight":1}')
    call("alice", "edit", "config", "--body", str(body), "--kind", "investigation", "--visibility", "workspace")
    call("alice", "push")
    call("bob", "pull")
    assert call("bob", "list")[0]["body"] == {"weight": 1}
    body.write_text('{"weight":2}')
    call("bob", "edit", "config", "--body", str(body))
    body.write_text('{"weight":3}')
    call("alice", "edit", "config", "--body", str(body))
    call("alice", "push")
    conflict = call("bob", "push")["drafts"][0]
    assert conflict["state"] == "conflict"
    body.write_text('{"weight":4}')
    call("bob", "resolve", conflict["mutation"]["mutation_id"], "--body", str(body))
    assert call("bob", "push")["drafts"] == []
    call("alice", "pull")
    assert call("alice", "list")[0]["body"] == {"weight": 4}
    call("alice", "edit", "secret", "--body", str(body))
    call("alice", "push")
    call("bob", "pull")
    assert [r["id"] for r in call("bob", "list")] == ["config"]
    assert len(list((tmp_path / "cache").glob("*.sqlite"))) == 4
    assert all(p.stat().st_mode & 0o077 == 0 for p in (tmp_path / "cache").glob("*.sqlite"))


def test_dry_run_does_not_create_cache(tmp_path):
    body = tmp_path / "body.json"
    body.write_text('{}')
    cache = tmp_path / "cache"
    r = CliRunner().invoke(app, ["--json", "--dry-run", "team", "edit", "x", "--body", str(body)],
                           env={"CST_SYNC_HOME": str(cache)})
    assert r.exit_code == 0
    assert not cache.exists()


def test_legacy_sync_still_rebuilds_index(monkeypatch):
    from captain_search.cli import main
    marker = object()
    monkeypatch.setattr(main, 'ctx', lambda: marker)
    calls = []
    monkeypatch.setattr(main.idx, 'sync', lambda context: calls.append(context) or {'records': 7})
    result = CliRunner().invoke(app, ['--json', 'sync'])
    assert result.exit_code == 0
    assert calls == [marker]
    assert json.loads(result.output) == {'records': 7}


@pytest.mark.parametrize('command', ['list', 'drafts', 'pull', 'push'])
def test_all_team_dry_runs_leave_no_cache(command, tmp_path):
    cache = tmp_path / 'cache'
    result = CliRunner().invoke(app, ['--json', '--dry-run', 'team', command],
                               env={'CST_SYNC_HOME': str(cache)})
    assert result.exit_code == 0
    assert not cache.exists()


def test_desktop_daemon_routes_require_auth_and_share_cli_cache(remote, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from captain_search import server as daemon
    monkeypatch.setenv('CST_SYNC_SERVER', remote)
    monkeypatch.setenv('CST_SYNC_TOKEN', 'alice-cli-test')
    monkeypatch.setenv('CST_SYNC_WORKSPACE', 'team')
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'desktop-cache'))
    with TestClient(daemon.app) as client:
        assert client.get('/api/team/records').status_code == 401
        client.headers['Authorization'] = 'Bearer ' + daemon.TOKEN
        assert client.post('/api/team/pull').status_code == 200
        assert client.put('/api/team/records/research/draft', json={
            'kind': 'investigation', 'visibility': 'workspace', 'body': {'title': 'Desktop draft'}}).status_code == 200
        assert client.post('/api/team/push').status_code == 200
        rows = client.get('/api/team/records').json()['records']
        assert rows[0]['body']['title'] == 'Desktop draft'
        result = CliRunner().invoke(app, ['--json', 'team', 'list'])
        assert result.exit_code == 0
        assert json.loads(result.output) == rows


def test_cache_operation_lock_serializes_separate_processes(tmp_path, monkeypatch):
    import subprocess
    import os
    import sys
    from captain_search.collaboration.session import connection
    monkeypatch.setenv('CST_SYNC_SERVER', 'http://127.0.0.1:1')
    monkeypatch.setenv('CST_SYNC_TOKEN', 'lock-test')
    monkeypatch.setenv('CST_SYNC_WORKSPACE', 'team')
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'locked-cache'))
    script = 'from captain_search.collaboration.session import connection\nwith connection() as c:\n print("entered", flush=True)\n'
    with connection():
        child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                child.communicate(timeout=.3)
        except BaseException:
            child.kill()
            child.communicate()
            raise
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, stderr
    assert stdout.strip() == 'entered'


def test_discard_local_draft_never_publishes(remote, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from captain_search import server as daemon
    monkeypatch.setenv('CST_SYNC_SERVER', remote)
    monkeypatch.setenv('CST_SYNC_TOKEN', 'alice-cli-test')
    monkeypatch.setenv('CST_SYNC_WORKSPACE', 'team')
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'discard-cache'))
    with TestClient(daemon.app) as client:
        assert client.delete('/api/team/drafts/nope').status_code == 401
        client.headers['Authorization'] = 'Bearer ' + daemon.TOKEN
        draft = client.put('/api/team/records/discarded/draft', json={'body': {'unwanted': True}}).json()
        assert client.delete('/api/team/drafts/' + draft['mutation_id']).status_code == 200
        assert client.get('/api/team/records').json()['drafts'] == []
        assert client.post('/api/team/push').status_code == 200
        assert client.get('/api/team/records').json()['records'] == []


def test_reader_push_retains_local_draft(remote, tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from captain_search import server as daemon
    with httpx.Client(base_url=remote, headers={'Authorization': 'Bearer alice-cli-test'}) as admin:
        assert admin.put('/v1/workspaces/team/members/bob', json={'role': 'reader'}).status_code == 200
    monkeypatch.setenv('CST_SYNC_SERVER', remote)
    monkeypatch.setenv('CST_SYNC_TOKEN', 'bob-cli-test')
    monkeypatch.setenv('CST_SYNC_WORKSPACE', 'team')
    monkeypatch.setenv('CST_SYNC_HOME', str(tmp_path / 'reader-cache'))
    with TestClient(daemon.app, headers={'Authorization': 'Bearer ' + daemon.TOKEN}) as client:
        assert client.put('/api/team/records/proposal/draft', json={'body': {'title': 'Keep me'}}).status_code == 200
        response = client.post('/api/team/push')
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'write_denied'
        drafts = client.get('/api/team/records').json()['drafts']
        assert len(drafts) == 1 and drafts[0]['mutation']['body'] == {'title': 'Keep me'}
