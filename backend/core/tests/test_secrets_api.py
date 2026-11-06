"""POST /api/secrets keeps the value in the keychain and never returns it."""
import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend


class Mem(KeyringBackend):
    priority = 1

    def __init__(self):
        self.d = {}

    def set_password(self, s, u, p):
        self.d[(s, u)] = p

    def get_password(self, s, u):
        return self.d.get((s, u))

    def delete_password(self, s, u):
        self.d.pop((s, u), None)


def test_store_list_delete_never_echo(tmp_path, monkeypatch):
    from captain_search import server
    from captain_search.workspace import get_secret, init_workspace

    old = keyring.get_keyring()
    mem = Mem()
    keyring.set_keyring(mem)
    try:
        monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
        monkeypatch.setattr(server, "_ctx", None)
        init_workspace("t", str(tmp_path / "b"))
        c = TestClient(server.app)
        h = {"Authorization": f"Bearer {server.TOKEN}"}
        r = c.post("/api/secrets", headers=h, json={"name": "DB_PASSWORD", "value": "hunter2"})
        assert r.status_code == 200 and "hunter2" not in r.text
        assert get_secret("DB_PASSWORD") == "hunter2"
        lst = c.get("/api/secrets", headers=h)
        assert lst.json()["names"] == ["DB_PASSWORD"] and "hunter2" not in lst.text
        assert c.post("/api/secrets", headers=h, json={"name": "bad name", "value": "x"}).status_code == 400
        assert c.post("/api/secrets", headers=h, json={"name": "EMPTY", "value": ""}).status_code == 400
        assert c.post("/api/secrets", json={"name": "X", "value": "y"}).status_code == 401
        assert c.delete("/api/secrets/DB_PASSWORD", headers=h).json()["deleted"] is True
        with pytest.raises(KeyError):
            get_secret("DB_PASSWORD")
    finally:
        keyring.set_keyring(old)


def test_existing_name_is_409_with_a_sentence(tmp_path, monkeypatch):
    from captain_search import server
    from captain_search.workspace import init_workspace

    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(server, "_ctx", None)
    init_workspace("t", str(tmp_path / "b"))
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    body = {"name": "dup", "kind": "local", "config": {}}
    assert c.post("/api/compute", headers=h, json=body).status_code == 200
    r = c.post("/api/compute", headers=h, json=body)
    assert r.status_code == 409 and r.json()["error"] == "dup already exists in compute. Pick another name."
