"""cst app talks to the running desktop app through app.json and a token."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from typer.testing import CliRunner

from captain_search.cli.main import app

SEEN = []


class Fake(BaseHTTPRequestHandler):
    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer tok":
            return self._reply(401, {"error": "bad token"})
        if self.path == "/commands":
            return self._reply(200, [{"name": "layout.switch", "help": "show a layout"}])
        return self._reply(200, {"layout": "main", "errors": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, body))
        if self.path == "/check":
            return self._reply(200, {"ok": False, "error": "main.js:3 render threw"})
        if body.get("command") == "nope":
            return self._reply(400, {"error": "unknown command nope"})
        return self._reply(200, {"ok": True})


def test_app_do_commands_status_and_check(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("CST_HOME", str(tmp_path))
    r = CliRunner()
    out = r.invoke(app, ["app", "status"])
    assert out.exit_code == 1 and "not running" in out.output
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "app.json").write_text(json.dumps({"port": srv.server_port, "token": "tok", "pid": os.getpid()}))
    out = r.invoke(app, ["--json", "app", "do", "layout.switch", "-a", "name=focus", "-a", "px=240"])
    assert out.exit_code == 0, out.output
    assert SEEN[-1] == ("/do", {"command": "layout.switch", "args": {"name": "focus", "px": 240}})
    assert r.invoke(app, ["app", "do", "nope"]).exit_code == 1
    assert "layout.switch" in r.invoke(app, ["app", "commands"]).output
    assert json.loads(r.invoke(app, ["--json", "app", "status"]).output)["layout"] == "main"
    mod = tmp_path / "m"
    mod.mkdir()
    (mod / "main.js").write_text("export default () => null")
    out = r.invoke(app, ["app", "check", str(mod)])
    assert out.exit_code == 1 and "render threw" in out.output
    srv.shutdown()
