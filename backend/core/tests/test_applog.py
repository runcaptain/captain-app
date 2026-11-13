"""The services' log (applog.py), and `cst app logs`.

What must hold: a failed request is logged with a request id and its traceback, and nothing of its body. A secret
inside an exception's message is taken out. A slow request is logged. Requests that go well are counted, never
listed. `cst app logs` prints the logs and `--bundle` writes one zip that is safe to attach to a bug report."""
from __future__ import annotations

import io
import json
import os
import pathlib
import re
import time
import zipfile

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import applog

ROOT = pathlib.Path(__file__).resolve().parents[3]
LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z (INFO |WARN |ERROR) [a-z-]+  \S")
# Made up, and built here so no scanner reads it as a real key.
SECRET = "tok" + "en=" + "ab" * 10
BODY_WORD = "wool-socks-the-person-typed"


@pytest.fixture
def logs(tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    monkeypatch.setenv(applog.DIR_ENV, str(folder))
    applog.COUNT.__init__()
    applog.start()
    yield folder
    applog.stop()


def _text(folder) -> str:
    p = folder / "services.log"
    return p.read_text() if p.exists() else ""


def _app() -> FastAPI:
    app = FastAPI()
    applog.install(app)

    @app.exception_handler(ConnectionError)
    async def refused(request: Request, exc: ConnectionError):
        applog.note_failure(request, exc)
        return JSONResponse({"error": "could not reach it"}, status_code=502)

    @app.post("/api/search")
    async def search(request: Request):
        await request.body()
        raise RuntimeError(f"upstream said no: {SECRET} for /Users/sam/data/file.csv")

    @app.get("/api/warehouse")
    def warehouse():
        raise ConnectionError("warehouse did not answer")

    @app.get("/api/ok")
    def ok():
        return {"ok": True}

    @app.get("/api/missing")
    def missing():
        return JSONResponse({"error": "no such thing"}, status_code=404)

    @app.get("/api/slow")
    def slow():
        time.sleep(0.05)
        return {"ok": True}

    return app


def test_the_two_redactions_agree_on_every_case():
    cases = json.loads((ROOT / "tests" / "fixtures" / "redaction-cases.json").read_text())["cases"]
    assert len(cases) >= 20
    for case in cases:
        assert applog.redact(case["in"]) == case["out"], case["in"]
        assert applog.redact(case["out"]) == case["out"], "twice: " + case["in"]
    assert applog.redact("cannot read /Volumes/work/sam/notes.txt", "/Volumes/work/sam/") == "cannot read ~/notes.txt"


def test_a_failed_request_is_logged_with_an_id_and_no_body(logs):
    c = TestClient(_app(), raise_server_exceptions=False)
    r = c.post("/api/search?q=" + BODY_WORD, json={"query": BODY_WORD}, headers={"Authorization": "Bearer " + "cd" * 12})
    assert r.status_code == 500
    text = _text(logs)
    first = text.splitlines()[0]
    assert LINE.match(first), first
    m = re.search(r"ERROR requests  POST /api/search 500 id=([0-9a-f]{12}) after \d+\.\d\d s$", first)
    assert m, first
    # The traceback is under it, indented, and ends with the exception's type and message.
    assert re.search(r'\n      File "[^"]+test_applog\.py", line \d+, in search\n', text), text
    assert "RuntimeError: upstream said no: token=[redacted] for ~/data/file.csv" in text
    # Nothing of the body, the query string or the Authorization header.
    assert BODY_WORD not in text and "cdcdcd" not in text and "abababab" not in text and "/Users/sam" not in text
    assert all(line.startswith("    ") or LINE.match(line) for line in text.splitlines()), text


def test_a_5xx_an_exception_handler_answered_is_logged_with_its_exception(logs):
    c = TestClient(_app())
    r = c.get("/api/warehouse")
    assert r.status_code == 502 and re.fullmatch(r"[0-9a-f]{12}", r.headers["x-request-id"])
    text = _text(logs)
    assert f"ERROR requests  GET /api/warehouse 502 id={r.headers['x-request-id']}" in text
    assert "ConnectionError: warehouse did not answer" in text


def test_requests_that_go_well_are_counted_not_listed(logs):
    c = TestClient(_app())
    for _ in range(5):
        assert c.get("/api/ok").status_code == 200
    assert c.get("/api/missing").status_code == 404
    assert _text(logs) == "", "a request that went well, or that was refused, wrote a line of its own"
    # A minute on, the next request writes the count.
    applog.COUNT.add(200, now=applog.COUNT.since + 61)
    text = _text(logs)
    assert re.search(r"INFO  requests  5 requests answered and 1 refused \(4xx\) since \d{4}-", text), text
    assert "/api/ok" not in text


def test_a_slow_request_is_logged(logs, monkeypatch):
    monkeypatch.setattr(applog, "SLOW_SECONDS", 0.01)
    c = TestClient(_app())
    assert c.get("/api/slow").status_code == 200
    assert re.search(r"WARN  requests  slow: GET /api/slow 200 id=[0-9a-f]{12} took 0\.\d\d s", _text(logs)), _text(logs)
    # A request that waits on purpose (the change feed) is never called slow.
    monkeypatch.setattr(applog, "WAITS", ("/api/slow",))
    before = _text(logs)
    c.get("/api/slow")
    assert _text(logs) == before


def test_a_database_error_does_not_bring_its_statement_into_the_log():
    e = ValueError("(duckdb.Error) no such column [SQL: SELECT * FROM searches WHERE q = 'wool socks'] [parameters: ('wool socks',)] (Background on this error at: x)")
    said = applog.describe(e)
    assert "wool socks" not in said and "[SQL: left out]" in said and "[parameters: left out]" in said
    assert len(applog.describe(RuntimeError("x" * 2000))) < 540


def test_an_uncaught_exception_in_a_thread_is_logged(logs):
    import threading

    before = threading.excepthook, __import__("sys").excepthook
    try:
        threading.excepthook = lambda args: None  # keep the test's own output quiet
        applog.watch_threads()
        t = threading.Thread(target=lambda: 1 / 0, name="cst-cloud-watch")
        t.start()
        t.join()
    finally:
        threading.excepthook, __import__("sys").excepthook = before
    text = _text(logs)
    assert "ERROR threads  uncaught exception in thread cst-cloud-watch" in text and "ZeroDivisionError: division by zero" in text


def test_the_real_service_logs_jobs_and_adds_request_ids(logs, tmp_path, monkeypatch):
    from captain_search import server
    from captain_search.workspace import init_workspace

    ctx = init_workspace("acme", str(tmp_path / "store"))
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ctx)
    c = TestClient(server.app)
    r = c.get("/api/health", headers={"Authorization": f"Bearer {server.TOKEN}"})
    assert r.status_code == 200 and re.fullmatch(r"[0-9a-f]{12}", r.headers["x-request-id"])
    assert server.TOKEN not in _text(logs)

    def boom(progress):
        raise RuntimeError("the trainer stopped: api_key=" + "zz" * 8)

    done = server._job(lambda p: "ok", "comparing shop with the baseline")
    failed = server._job(boom, "training a starter ranker for shop")
    for _ in range(100):
        if server.JOBS[done]["status"] == "done" and server.JOBS[failed]["status"] == "failed":
            break
        time.sleep(0.02)
    text = _text(logs)
    assert f"INFO  jobs  job {done} started: comparing shop with the baseline" in text
    assert re.search(rf"INFO  jobs  job {done} done after \d+\.\d s", text), text
    assert re.search(rf"ERROR jobs  job {failed} failed after \d+\.\d s: training a starter ranker for shop", text), text
    assert "RuntimeError: the trainer stopped: api_key=[redacted]" in text and "zzzzzz" not in text


def test_the_log_starts_a_new_file_when_full(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "MAX_BYTES", 400)
    monkeypatch.setattr(applog, "KEEP", 2)
    applog.start(str(tmp_path))
    try:
        for n in range(40):
            applog.info("services", f"line {n} " + "x" * 60)
    finally:
        applog.stop()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["services.log", "services.log.1", "services.log.2"], names
    assert "line 39" in (tmp_path / "services.log").read_text()


def _fill(folder: pathlib.Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    old, new = applog.stamp(now - 7200), applog.stamp(now - 60)
    (folder / "app.log").write_text(
        f"{old} INFO  app  started: Captain 0.1.13 (commit 31aec7ff), macos aarch64, launched from a terminal\n"
        f"{old} INFO  window  opened the main window\n"
        f"{new} ERROR mods  mod=captain-data error in click handler: boom\n    at save (~/.config/captain-app/mods/captain-data/main.js:3)\n")
    (folder / "services.log").write_text(
        f"{old} INFO  services  started: cst 0.1.0, pid 41\n"
        f"{applog.stamp(now - 90)} ERROR requests  GET /api/x 500 id=0123456789ab after 0.10 s\n")
    (folder / "crash-20261113T102233Z.log").write_text("Captain crash record\nkind: panic\nmessage: boom\n")
    (folder / "last-run.json").write_text(json.dumps({"ending": "crashed", "detail": "crashed, record crash-20261113T102233Z.log", "record": "crash-20261113T102233Z.log"}))
    # Written by an install script, not by us: whatever it holds is redacted on the way into a bundle.
    (folder / "installs.log").write_text("installed to /Users/sam/Applications with " + SECRET + "\n")


def test_cst_app_logs_prints_them(tmp_path, monkeypatch):
    from captain_search.cli.main import app

    folder = tmp_path / "logs"
    _fill(folder)
    monkeypatch.setenv(applog.DIR_ENV, str(folder))
    run = CliRunner()
    everything = run.invoke(app, ["app", "logs"]).output
    lines = everything.splitlines()
    # App and services lines are merged by time, a stack stays under its event, crash records come last.
    assert "started: Captain 0.1.13" in lines[0] or "started: cst" in lines[0]
    assert lines.index(next(x for x in lines if "GET /api/x 500" in x)) < lines.index(next(x for x in lines if "error in click handler" in x))
    assert lines[lines.index(next(x for x in lines if "error in click handler" in x)) + 1].startswith("    at save")
    assert "==== crash-20261113T102233Z.log ====" in everything and everything.rstrip().endswith("message: boom")

    recent = run.invoke(app, ["app", "logs", "--since", "1h", "--which", "app"]).output
    assert "error in click handler" in recent and "    at save" in recent and "opened the main window" not in recent and "GET /api/x" not in recent
    assert "GET /api/x 500" in run.invoke(app, ["app", "logs", "--which", "services"]).output
    assert run.invoke(app, ["app", "logs", "--which", "crash"]).output.startswith("==== crash-")
    assert run.invoke(app, ["app", "logs", "--last"]).output.startswith("Captain crash record")
    bad = run.invoke(app, ["app", "logs", "--since", "soon"])
    assert bad.exit_code == 1
    assert run.invoke(app, ["app", "logs", "--which", "everything"]).exit_code == 1


def test_follow_prints_new_whole_lines_and_survives_a_new_file(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    log = folder / "app.log"
    log.write_text("old line\n")
    out = io.StringIO()
    applog.follow("app", str(folder), out, poll=0, rounds=1)
    assert out.getvalue() == "", "what was there before is not printed again"
    with log.open("a") as f:
        f.write("one\nhalf a li")
    applog.follow("app", str(folder), out, poll=0, rounds=1)
    assert out.getvalue() == ""  # a new follow starts at the end
    out = io.StringIO()
    log.write_text("")  # the log started a new file
    import threading

    def write():  # half a line is held back until its end arrives
        time.sleep(0.05)
        with log.open("a") as f:
            f.write("first of the new file\nsecond")
        time.sleep(0.1)
        with log.open("a") as f:
            f.write(" done\n")

    t = threading.Thread(target=write)
    t.start()
    applog.follow("app", str(folder), out, poll=0.02, rounds=20)
    t.join()
    assert out.getvalue() == "first of the new file\nsecond done\n"


def test_the_bundle_is_one_zip_safe_to_attach(tmp_path, monkeypatch):
    from captain_search.cli.main import app

    folder = tmp_path / "logs"
    _fill(folder)
    monkeypatch.setenv(applog.DIR_ENV, str(folder))
    monkeypatch.setenv("CAPTAIN_BUNDLE_DIR", str(tmp_path / "Desktop"))
    out = CliRunner().invoke(app, ["app", "logs", "--bundle"]).output.strip()
    assert out.startswith(str(tmp_path / "Desktop")) and out.endswith(".zip") and os.path.isfile(out), out
    with zipfile.ZipFile(out) as z:
        names = sorted(n.split("/", 1)[1] for n in z.namelist())
        assert names == ["app.log", "crash-20261113T102233Z.log", "facts.txt", "installs.log", "last-run.json", "services.log"], names
        read = {n.split("/", 1)[1]: z.read(n).decode() for n in z.namelist()}
    facts = read["facts.txt"]
    assert "app: Captain 0.1.13 (commit 31aec7ff)" in facts and "services (cst): " in facts and "system: " in facts
    assert "last run: crashed, record crash-20261113T102233Z.log" in facts
    assert "token=[redacted]" in read["installs.log"] and "abababab" not in read["installs.log"] and "/Users/sam" not in read["installs.log"]
    # A second one in the same second does not overwrite the first.
    again = CliRunner().invoke(app, ["app", "logs", "--bundle", "--to", str(tmp_path / "Desktop")]).output.strip()
    assert again != out and os.path.isfile(again)


def test_processor_and_memory_use_is_logged_once_a_minute(logs):
    line, busy = applog.usage_line(41.0, 60.0, 412.4, 9)
    assert line == "used 41.0 s of processor time in the last 60 s (68% of one processor), memory 412 MB, 9 threads" and busy
    assert applog.usage_line(0.4, 60.0, None, 3) == ("used 0.4 s of processor time in the last 60 s (1% of one processor), memory unknown, 3 threads", False)
    # The real loop, with a short minute: two lines, each with this process's own numbers.
    applog.watch_usage(every=0.05, rounds=2).join(5)
    lines = [ln for ln in _text(logs).splitlines() if " usage  used " in ln]
    assert len(lines) == 2 and all(re.search(r"used \d+\.\d s of processor time in the last 0 s \(\d+% of one processor\), memory \d+ MB, \d+ threads$", ln) for ln in lines), lines


# ---- what every thread is doing, on request -----------------------------------------------------------------------

def test_cst_app_stacks_prints_where_every_thread_of_the_running_services_is(tmp_path):
    """The services sat at half a processor and nothing said why. On SIGUSR1 they write every thread's stack into
    services.log, and `cst app stacks` asks and prints it."""
    import signal
    import subprocess
    import sys

    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("no SIGUSR1 on this system")
    logs_dir, home = tmp_path / "logs", tmp_path / "home"
    home.mkdir()
    code = (
        "import json, os, sys, threading, time\n"
        "from captain_search import applog as AL\n"
        "AL.start(sys.argv[1]); AL.watch_stacks()\n"
        "def listing_folders_in_a_loop():\n"
        "    while True: os.listdir('.')\n"
        "threading.Thread(target=listing_folders_in_a_loop, daemon=True, name='the-busy-one').start()\n"
        "AL.info('services', 'started: a stand-in for the services')\n"
        "json.dump({'pid': os.getpid(), 'port': 1, 'token': 'x', 'stacks': True}, open(os.path.join(sys.argv[2], 'serve.json'), 'w'))\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(logs_dir), str(home)])
    try:
        for _ in range(100):
            if (home / "serve.json").exists():
                break
            time.sleep(0.05)
        time.sleep(0.2)
        text = applog.ask_stacks(proc.pid, str(logs_dir / "services.log"))
        assert "listing_folders_in_a_loop" in text and "Thread 0x" in text, text[:600]
        assert text.count("most recent call first") >= 2, "every thread, not only the one that took the signal"
        # the log's own lines are still whole, and a second ask adds a second dump
        again = applog.ask_stacks(proc.pid, str(logs_dir / "services.log"))
        assert "listing_folders_in_a_loop" in again
        whole = (logs_dir / "services.log").read_text()
        assert "started: a stand-in for the services" in whole and whole.count("listing_folders_in_a_loop") >= 2
        # the command: finds the services by their record in the home folder, asks, prints
        env = {**os.environ, "CST_HOME": str(home), "CAPTAIN_LOG_DIR": str(logs_dir)}
        cst = os.path.join(os.path.dirname(sys.executable), "cst")
        out = subprocess.run([cst, "app", "stacks"], env=env, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[-400:]
        assert "listing_folders_in_a_loop" in out.stdout and f"process {proc.pid}" in out.stdout and "innermost call first" in out.stdout
        short = subprocess.run([cst, "--json", "app", "stacks", "--top", "2"], env=env, capture_output=True, text=True)
        got = json.loads(short.stdout)
        assert got["pid"] == proc.pid and got["threads"] >= 2 and all(len(t.split("\n")) <= 3 for t in got["stacks"])
    finally:
        proc.kill()
        proc.wait()
    # Services from before this existed would be ended by the signal: they are never sent it.
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        (home / "serve.json").write_text(json.dumps({"pid": old.pid, "port": 1, "token": "x"}))
        env = {**os.environ, "CST_HOME": str(home), "CAPTAIN_LOG_DIR": str(logs_dir)}
        out = subprocess.run([os.path.join(os.path.dirname(sys.executable), "cst"), "app", "stacks"], env=env, capture_output=True, text=True)
        assert out.returncode != 0 and "older version" in (out.stderr + out.stdout)
        time.sleep(0.3)
        assert old.poll() is None, "the older services were not sent the signal: they are still running"
    finally:
        old.kill()
        old.wait()
    # nothing running: said plainly
    env = {**os.environ, "CST_HOME": str(home), "CAPTAIN_LOG_DIR": str(logs_dir)}
    gone = subprocess.run([os.path.join(os.path.dirname(sys.executable), "cst"), "app", "stacks"], env=env, capture_output=True, text=True)
    assert gone.returncode != 0 and "aren't running here" in (gone.stderr + gone.stdout)
