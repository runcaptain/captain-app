"""Live runs: what a backtest, a training session or a team's own tool is doing right now, as a stream of events.

Runs (the app) draws every live run the same way: its stages, a progress bar, metrics as numbers and lines, simulated
shoppers side by side, and the log. Captain's backtests and retrains write here, and so can anything else:

- from Python: `r = live.start("training", "ranker v7"); r.metric("loss", 0.31, step=200); r.end()`;
- from a shell: `cst live start`, `cst live emit`, `cst live end`;
- from any program: `cst live pipe -- <command>` streams its output. A line that is a JSON object with a "type" is an
  event, every other line is a log line. So an internal simulator or trainer needs no Captain code: it prints.

Events (one JSON object per line, each with "type", "at" in seconds since the run started, and "n" its position):
- stage     {name, index, of}                       a step of the run (Rank queries, Simulate shoppers, Compare)
- progress  {fraction, done?, total?, unit?}        how far along; the header keeps the latest
- metric    {name, value, arm?, step?}              a number; repeated values of one name and arm draw a line
- shopper   {arm, shopper, query, results, steps, clicked, bought, revenue}  one simulated session; steps are
            the session's events (search, reformulate, click {rank}, purchase {price})
- log       {text}
- result    {ref?, summary?}                        what the run made

A run lives on this computer, at $CST_HOME/live/<id>.json (its header) and <id>.jsonl (its events), since appending
to a bucket on every event would be slow. A run whose process is gone without ending reads as "stopped".
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time

from ulid import ULID

from .workspace import home, whoami

KINDS = ("backtest", "training", "sweep", "custom")
_lock = threading.Lock()


def _dir() -> str:
    d = os.path.join(home(), "live")
    os.makedirs(d, exist_ok=True)
    return d


def _paths(rid: str) -> tuple[str, str]:
    if not rid or "/" in rid or ".." in rid:
        raise ValueError(f"{rid!r} isn't a live run id")
    return os.path.join(_dir(), f"{rid}.json"), os.path.join(_dir(), f"{rid}.jsonl")


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_header(rid: str, head: dict) -> None:
    hp, _ = _paths(rid)
    tmp = f"{hp}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(head, f)
    os.replace(tmp, hp)


def header(rid: str) -> dict:
    hp, _ = _paths(rid)
    try:
        with open(hp) as f:
            h = json.load(f)
    except FileNotFoundError:
        from .storage.backend import NotFound

        raise NotFound(f"no live run {rid}") from None
    if h.get("status") == "running" and not _alive(h.get("pid")):
        h["status"] = "stopped"
        h["note"] = "The process running it ended without saying it finished."
    return h


class Run:
    """A live run being written. Every method is safe to call from the thread doing the work."""

    def __init__(self, rid: str, head: dict):
        self.id, self.head = rid, head
        self.t0 = time.time()
        self._n = head.get("events", 0)
        self._last_head = 0.0

    def emit(self, type_: str, **data) -> None:
        _, ep = _paths(self.id)
        with _lock:
            ev = {"type": type_, "at": round(time.time() - self.t0, 3), "n": self._n, **data}
            self._n += 1
            with open(ep, "a") as f:
                f.write(json.dumps(ev, default=str) + "\n")
            self.head["events"] = self._n
            if type_ == "stage":
                self.head["stage"] = data.get("name")
                self.head.setdefault("stages", [])
                if data.get("name") not in self.head["stages"]:
                    self.head["stages"].append(data.get("name"))
            if type_ == "progress":
                self.head["progress"] = data.get("fraction")
            now = time.time()
            if type_ in ("stage", "result") or now - self._last_head > 0.5:
                self.head["updated"] = _now()
                _write_header(self.id, self.head)
                self._last_head = now

    def stage(self, name: str, index: int | None = None, of: int | None = None) -> None:
        self.emit("stage", name=name, index=index, of=of)

    def progress(self, fraction: float, done: int | None = None, total: int | None = None, unit: str | None = None) -> None:
        self.emit("progress", fraction=round(max(0.0, min(1.0, float(fraction))), 4), done=done, total=total, unit=unit)

    def metric(self, name: str, value: float, arm: str | None = None, step: float | None = None) -> None:
        self.emit("metric", name=name, value=value, arm=arm, step=step)

    def log(self, text: str) -> None:
        self.emit("log", text=str(text)[:2000])

    def end(self, status: str = "done", ref: str | None = None, summary: str | None = None, error: str | None = None) -> None:
        if ref or summary:
            self.emit("result", ref=ref, summary=summary)
        if error:
            self.emit("log", text=error[:2000], level="error")
        with _lock:
            self.head.update(status=status, ended=_now(), updated=_now(), error=error, ref=ref or self.head.get("ref"))
            if status == "done":
                self.head["progress"] = 1.0
            _write_header(self.id, self.head)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def start(kind: str, title: str, *, of: str | None = None, arms: list[str] | None = None, source: str = "captain",
          pid: int | None = None) -> Run:
    """Begin a live run. `of` is what it's a run of (a backtest's name, a pipeline), `arms` the versions compared."""
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    rid = str(ULID()).lower()
    head = {"id": rid, "kind": kind, "title": title, "of": of, "arms": arms or [], "source": source, "by": whoami(),
            "started": _now(), "updated": _now(), "status": "running", "progress": None, "stage": None, "stages": [],
            "events": 0, "pid": pid or os.getpid()}
    _write_header(rid, head)
    open(_paths(rid)[1], "a").close()
    return Run(rid, head)


def attach(rid: str) -> Run:
    """Keep writing to a run another process started (`cst live emit`)."""
    h = header(rid)
    r = Run(rid, h)
    try:
        r.t0 = dt.datetime.fromisoformat(h["started"]).timestamp()
    except (KeyError, ValueError):
        pass
    return r


def read(rid: str, since: int = 0, limit: int = 2000) -> dict:
    """The run's header and its events from position `since`: the app asks again with `next`."""
    h = header(rid)
    _, ep = _paths(rid)
    out = []
    with open(ep) as f:
        for i, line in enumerate(f):
            if i < since:
                continue
            if len(out) >= limit:
                break
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return {"run": h, "events": out, "next": since + len(out)}


def listing(limit: int = 30, active: bool = False) -> list[dict]:
    rows = []
    for name in os.listdir(_dir()):
        if not name.endswith(".json"):
            continue
        try:
            rows.append(header(name[:-5]))
        except Exception:  # noqa: BLE001  a half-written header is skipped, not fatal
            continue
    if active:
        rows = [r for r in rows if r.get("status") == "running"]
    return sorted(rows, key=lambda r: r.get("started", ""), reverse=True)[:limit]


def pipe(argv: list[str], kind: str, title: str, of: str | None = None) -> dict:
    """Run a command and stream it: JSON lines with a "type" are events, other lines are log lines."""
    import subprocess

    r = start(kind, title, of=of, source=os.path.basename(argv[0]) if argv else "command")
    r.log("$ " + " ".join(argv))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        ev = None
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except ValueError:
                ev = None
        if isinstance(ev, dict) and isinstance(ev.get("type"), str):
            t = ev.pop("type")
            ev.pop("at", None), ev.pop("n", None)
            r.emit(t, **ev)
        else:
            r.log(line)
    code = proc.wait()
    r.end("done" if code == 0 else "failed", error=None if code == 0 else f"the command ended with exit code {code}")
    return {"id": r.id, "exit_code": code}
