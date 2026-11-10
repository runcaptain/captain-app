"""cst app context and cst app shot: an agent in a Captain terminal sees what the person is looking at.

The window answers one JSON line each way on <app home>/captain-app.sock (src/control.rs). Here a small fake window
plays that part, so the tests cover what cst does with the answer: the readable text, the JSON, what the services add,
what is hidden, and the plain sentence when Captain isn't running."""
import json
import os
import shutil
import socket
import tempfile
import threading
import time

import pytest
from typer.testing import CliRunner

from captain_search import codeagent
from captain_search import investigations as INV
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search import workspace as wsp
from captain_search.cli import appctl
from captain_search.cli.main import app

# A made-up token in the shape of a real one. It must never come out of `cst app context`.
TOKEN = "ghp_FAKEfakeFAKEfakeFAKEfake0123456789"
PASSWORD = "hunter2secret"


class Window:
    """A stand-in for Captain's window: answers {"view": true} with `view`, and {"shot": path} by writing a PNG."""

    def __init__(self, home: str, view: dict):
        self.view, self.asked = view, []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(os.path.join(home, appctl.SOCKET))
        self.sock.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile().readline())
                self.asked.append(req)
                if "view" in req:
                    out = self.view
                elif "shot" in req:
                    with open(req["shot"], "wb") as f:
                        f.write(b"\x89PNG\r\n\x1a\n")
                    out = {"path": req["shot"]}
                else:
                    out = {"error": "expected { do }, { ls }, { commands } or { status }"}
                conn.sendall((json.dumps(out) + "\n").encode())

    def close(self):
        self.sock.close()


@pytest.fixture
def app_home(monkeypatch):
    # A short folder: a Unix socket's path has to fit in about 100 characters.
    d = tempfile.mkdtemp(prefix="cap", dir="/tmp")
    monkeypatch.setenv("CAPTAIN_APP_HOME", d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ws(tmp_path):
    wsp.init_workspace("shop", str(tmp_path / "store"))
    c = wsp.open_context("shop")
    from captain_search import demo

    demo.seed(c)
    p = PR.create(c, "Outdoor")
    PS.pin(c, p["id"], {"catalog": "demo-catalog"})
    return c


def view(**over):
    base = {"at": int(time.time() * 1000), "workspace": "shop", "scope": {"kind": "project", "id": "outdoor", "label": "Outdoor"},
            "section": "project", "overlays": ["toast"], "agent": "t3", "terminals_shown": True, "layout": "captain",
            "views": {"project": {"project": {"id": "outdoor", "name": "Outdoor", "kind": "search-judgments"}, "candidate": None,
                                  "next": {"text": "Choose queries: the project can't train without it.", "label": "Choose queries",
                                           "action": {"action": "set-input", "slot": "queries"}},
                                  "workspace": "shop", "at": 1}}}
    base.update(over)
    return base


def test_it_says_plainly_when_captain_is_not_running(app_home):
    r = CliRunner()
    for cmd in (["app", "context"], ["app", "shot"]):
        out = r.invoke(app, cmd)
        assert out.exit_code == 1, out.output
        assert "Captain isn't running" in out.output and "Open Captain and try again" in out.output
        assert "Traceback" not in out.output
    out = r.invoke(app, ["--json", "app", "context"])
    assert out.exit_code == 1 and "Captain isn't running" in json.loads(out.output)["error"]


def test_the_project_page_with_what_the_services_know(app_home, ws):
    w = Window(app_home, view())
    r = CliRunner()
    out = r.invoke(app, ["app", "context"])
    assert out.exit_code == 0, out.output
    t = out.output
    assert "What the person is looking at in Captain" in t
    assert "Workspace" in t and "shop" in t
    assert "Outdoor (id outdoor), search-judgments" in t
    assert "Home  [section id: project]" in t
    assert "Choose queries: the project can't train without it.  (button: Choose queries)" in t
    assert "Agent tab" in t and "t3" in t
    # What the services know: the same stages the page draws (PS.stages), each input and whether it is chosen.
    assert "What Captain's services know about it:" in t
    assert "demo-catalog@v" in t, t
    assert "cst app shot" in t
    assert w.asked == [{"view": True}]

    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    assert d["workspace"] == "shop" and d["screen"] == "Home" and d["section"] == "project"
    assert d["project"] == {"id": "outdoor", "name": "Outdoor", "kind": "search-judgments"}
    assert d["next"]["action"] == {"action": "set-input", "slot": "queries"}
    assert d["dialog"] is None and d["open_over_the_page"] == []
    stages = PS.stages(ws, "outdoor")
    assert [i["slot"] for i in d["known"]["project"]["inputs"]] == [i["slot"] for i in stages["inputs"]]
    pinned = {i["slot"]: i.get("pinned") for i in d["known"]["project"]["inputs"]}
    assert pinned["catalog"].startswith("demo-catalog@v")
    assert d["known"]["project"]["next"] == stages.get("next")
    # --bare: only what the window says.
    assert "known" not in json.loads(r.invoke(app, ["--json", "app", "context", "--bare"]).output)
    w.close()


def test_other_screens_a_dataset_an_evaluation_an_experiment(app_home, ws):
    inv = INV.create(ws, "Why do numeric queries regress?")
    v = view(section="data", scope=None, views={
        "data": {"tab": "datasets", "dataset": "demo-catalog", "version": 1, "dataset_tab": "sample", "workspace": "shop", "at": 2},
        "runs": {"tab": "backtests", "selected": "demo-tuned-vs-bm25", "backtest": "demo-tuned-vs-bm25", "workspace": "shop", "at": 2},
        "investigations": {"experiment": inv["id"], "title": inv["title"], "tab": "overview", "workspace": "shop", "at": 2},
        # Published while another workspace was shown: not this window's now.
        "handoff": {"launch_review": "old-one", "workspace": "elsewhere", "at": 2}})
    w = Window(app_home, v)
    r = CliRunner()
    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    assert d["screen"] == "Data and signals" and d["tab"] == "datasets" and d["project"] is None
    assert d["selected"] == {"dataset": "demo-catalog", "version": 1, "dataset_tab": "sample"}
    ds = d["known"]["dataset"]
    assert ds["name"] == "demo-catalog" and ds["version"] == 1 and ds["rows"] > 0 and ds["columns"], ds
    t = r.invoke(app, ["app", "context"]).output
    assert "Data and signals, tab datasets" in t and "Dataset" in t and "demo-catalog" in t

    w.view = {**v, "section": "runs"}
    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    assert d["screen"] == "Runs" and d["selected"]["backtest"] == "demo-tuned-vs-bm25"
    ev = d["known"]["offline_evaluation"]
    assert ev["name"] == "demo-tuned-vs-bm25" and ev["status"] in ("finished", "planned, not run yet") and ev["arms"]

    w.view = {**v, "section": "investigations"}
    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    assert d["screen"] == "Experiments" and d["known"]["experiment"]["title"] == "Why do numeric queries regress?"

    # A screen whose part belongs to another workspace reads as nothing selected, and nothing is looked up for it.
    w.view = {**v, "section": "handoff"}
    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    assert d["screen"] == "Launch review" and d["selected"] == {} and "launch_review" not in d["known"]
    # A selection the services don't have: said, not a crash.
    w.view = {**v, "views": {"data": {"tab": "datasets", "dataset": "gone", "workspace": "shop"}}}
    out = r.invoke(app, ["--json", "app", "context"])
    assert out.exit_code == 0 and "problem" in json.loads(out.output)["known"]["dataset"]
    w.close()


def test_a_form_with_a_token_in_it_never_leaks(app_home, ws):
    """The screens already hide secrets (kit/view.js). This is the second check: even a window that sent them raw
    gets them hidden before anything is printed."""
    dialog = {"mod": "captain-forms", "form": "database", "title": "Connect a database", "workspace": "shop", "at": 3,
              "fields": {"name": "warehouse", "kind": "postgres", "token": TOKEN, "af:password": PASSWORD, "api_key": "k-123",
                         "host": "db.example.test", "note": TOKEN,
                         "url": f"postgresql://app:{PASSWORD}@db.example.test:5432/shop?sslmode=require&password={PASSWORD}"}}
    v = view(overlays=["captain-forms", "toast"])
    v["views"]["dialog"] = dialog
    w = Window(app_home, v)
    r = CliRunner()
    for args in (["app", "context"], ["--json", "app", "context"], ["app", "context", "--bare"]):
        out = r.invoke(app, args)
        assert out.exit_code == 0, out.output
        for leak in (TOKEN, PASSWORD, "k-123"):
            assert leak not in out.output, f"{leak} leaked from {args}"
    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    f = d["dialog"]["fields"]
    assert d["dialog"]["form"] == "database" and d["dialog"]["title"] == "Connect a database"
    assert f["name"] == "warehouse" and f["kind"] == "postgres" and f["host"] == "db.example.test"  # the harmless ones stay
    assert f["token"] == f["af:password"] == f["api_key"] == f["note"] == "(hidden)"
    assert f["url"] == "postgresql://(hidden)@db.example.test:5432/shop?sslmode=require&password=(hidden)"
    t = r.invoke(app, ["app", "context"]).output
    assert "Connect a database  [form id: database]" in t and "warehouse" in t and "(hidden)" in t

    # The form is closed (its mod is no longer over the page): what it last published is not on screen any more.
    w.view = {**v, "overlays": ["toast"]}
    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    assert d["dialog"] is None
    # Another dialog over the page is named.
    w.view = {**v, "overlays": ["captain-cloud"]}
    d = json.loads(r.invoke(app, ["--json", "app", "context"]).output)
    assert d["dialog"] is None and d["open_over_the_page"] == ["captain-cloud"]
    w.close()


def test_scrub():
    assert appctl.scrub("demo-catalog@v2") == "demo-catalog@v2"
    assert appctl.scrub("patagonia-catalog-2026") == "patagonia-catalog-2026"
    assert appctl.scrub("AKIA" + "ABCDEFGHIJKLMNOP") == "(hidden)"
    assert appctl.scrub({"secret_access_key": "x", "region": "us-west-2", "nested": {"client_secret": "y", "ok": [1, "a"]}}) == {
        "secret_access_key": "(hidden)", "region": "us-west-2", "nested": {"client_secret": "(hidden)", "ok": [1, "a"]}}
    assert appctl.scrub("s3://bucket/path/file.parquet") == "s3://bucket/path/file.parquet"
    assert appctl.scrub("https://u:p@host.example.test/x?sig=abc&v=1") == "https://(hidden)@host.example.test/x?sig=(hidden)&v=1"


def test_shot_saves_under_the_app_home_and_prints_the_path(app_home, tmp_path, monkeypatch):
    w = Window(app_home, view())
    r = CliRunner()
    monkeypatch.chdir(tmp_path)  # a repo, say: nothing may land here
    out = r.invoke(app, ["app", "shot"])
    assert out.exit_code == 0, out.output
    p = out.output.strip()
    assert p.startswith(os.path.join(app_home, "cache", "shots") + os.sep) and p.endswith(".png") and os.path.isfile(p)
    assert w.asked[-1] == {"shot": p}
    assert os.listdir(tmp_path) == [] or all(not f.endswith(".png") for f in os.listdir(tmp_path))
    # A place of the person's choosing.
    mine = str(tmp_path / "look.png")
    assert json.loads(r.invoke(app, ["--json", "app", "shot", mine]).output) == {"path": mine}
    assert os.path.isfile(mine)
    out = r.invoke(app, ["app", "shot", str(tmp_path / "look.jpg")])
    assert out.exit_code == 1 and ".png" in out.output
    # The folder stays small.
    folder = os.path.join(app_home, "cache", "shots")
    for i in range(30):
        open(os.path.join(folder, f"captain-20200101-0000{i:02d}.png"), "wb").close()
    r.invoke(app, ["app", "shot"])
    assert len(os.listdir(folder)) <= 20
    w.close()


def test_an_older_captain_is_named(app_home):
    w = Window(app_home, view())
    w.view = {"error": "expected { do }, { ls }, { commands } or { status }"}
    out = CliRunner().invoke(app, ["app", "context"])
    assert out.exit_code == 1 and "older than this cst" in out.output
    w.close()


def test_agents_are_told_how_to_look(ws):
    line = "To see what the person is looking at in Captain right now, run `cst app context` (and `cst app shot` for a picture)."
    info = codeagent.prepare(ws)
    for f in ("AGENTS.md", "CLAUDE.md"):
        guide = open(os.path.join(info["cwd"], f)).read()
        assert line in guide and '"this", "here", "what I\'m looking at", or name nothing' in guide
        assert guide.index(line) < guide.index("## Rules"), "near the top"
    # Every prompt the services build starts with the cst line, and that carries it.
    assert line in codeagent.cst_line()
    d = INV.create(ws, "Why do numeric queries regress?")
    p = INV.prompt(ws, d["id"])
    assert p.splitlines()[0].startswith("Use this cst: ") and line in p.split("\n\n")[0]
