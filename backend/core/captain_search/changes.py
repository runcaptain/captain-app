"""The change feed the app's one live channel reads (defaults/captain/kit/live.js).

A STAND-IN, so the screens can be converted now. It knows about changes that come through the server's own routes
and its own jobs. It does not see a change made by another process (a `cst` command in a terminal, a job running
elsewhere): the real feed, which watches the store, replaces this file and keeps the same wire shape.

  GET /api/changes?since=<seq>&topics=a,b&wait=<seconds>
  -> {"seq": 1042, "changed": [{"topic": "runs", "seq": 1040}], "reset": false}

One counter for the whole service, rising by one per change. `changed` lists the asked-for topics that changed
after `since`. No `since`: the current seq, at once. `reset` is true when the service can't say what was missed
(`since` is ahead of the counter: the service restarted).

Topics: projects, project:<id>, data, table:<ref>, runs, run:<id>, jobs, deployed, deployment:<project>, notebooks,
logins, compute, settings. A change to one thing also bumps its list.
"""
from __future__ import annotations

import re
import threading
import time

LISTS = ("projects", "data", "runs", "jobs", "deployed", "notebooks", "logins", "compute", "settings")
PARENT = {"project": "projects", "table": "data", "run": "runs", "deployment": "deployed"}
MAX_WAIT = 25.0

_lock = threading.Lock()
_seq = 0
_at: dict[str, int] = {}  # topic -> the seq of its latest change


def bump(*topics: str) -> int:
    """Say that these topics changed. A thing's own topic ("run:abc") bumps its list too. Returns the new seq."""
    global _seq
    names = set()
    for t in topics:
        if not t:
            continue
        names.add(t)
        head = t.split(":", 1)[0]
        if ":" in t and head in PARENT:
            names.add(PARENT[head])
    if not names:
        return _seq
    with _lock:
        for t in sorted(names):
            _seq += 1
            _at[t] = _seq
        return _seq


def seq() -> int:
    return _seq


def covers(asked: str, changed: str) -> bool:
    """An asked-for topic covers a changed one: the same, or "run:" for every "run:<id>"."""
    return asked == changed or (asked.endswith(":") and changed.startswith(asked))


def since(n: int | None, topics: list[str]) -> dict:
    """What changed after seq `n` among `topics`."""
    with _lock:
        now = _seq
        if n is None:
            return {"seq": now, "changed": [], "reset": False}
        if n > now:
            return {"seq": now, "changed": [], "reset": True}
        hit = [{"topic": t, "seq": s} for t, s in sorted(_at.items(), key=lambda kv: kv[1])
               if s > n and any(covers(a, t) for a in topics)]
        return {"seq": now, "changed": hit, "reset": False}


# ---- things that change without a write -----------------------------------------------------------
# A job's progress, and the state a cloud reports for a deployed model, change with no request to this service.
# Each such source gives a digest of what it sees. While someone is asking about its topics the digest is taken
# again every `every` seconds, and a different digest bumps the topics. Nobody asking: nothing is read.
_sources: list[dict] = []
_asked: dict[str, float] = {}  # topic -> when it was last asked about
_thread: threading.Thread | None = None
ASKED_FOR_S = 40.0  # a topic counts as watched this long after the last request that named it


def source(topics: list[str], every: float, digest) -> None:
    """Register something that changes by itself: `digest()` returns a value that differs when it has changed."""
    _sources.append({"topics": list(topics), "every": float(every), "digest": digest, "seen": None, "at": 0.0, "primed": False})


def _wanted(src: dict, now: float) -> bool:
    for t in src["topics"]:
        child = PARENT_OF.get(t)
        for asked, when in list(_asked.items()):
            if now - when > ASKED_FOR_S:
                continue
            if asked == t or (child and asked.startswith(child + ":")):
                return True
    return False


PARENT_OF = {v: k for k, v in PARENT.items()}  # "deployed" -> "deployment"


def check_sources(now: float | None = None) -> None:
    """One pass over the sources (the thread calls it a few times a second; a test calls it directly)."""
    now = time.monotonic() if now is None else now
    for src in _sources:
        if now - src["at"] < src["every"] or not _wanted(src, now):
            continue
        src["at"] = now
        try:
            seen = src["digest"]()
        except Exception:  # noqa: BLE001  a source that can't be read says nothing; the next pass asks again
            continue
        if src["primed"] and seen != src["seen"]:
            bump(*src["topics"])
        src["seen"], src["primed"] = seen, True


def _loop() -> None:
    while True:
        time.sleep(0.25)
        check_sources()


def _note_asked(topics: list[str]) -> None:
    global _thread
    now = time.monotonic()
    for t in topics:
        _asked[t] = now
    if _sources and _thread is None:
        _thread = threading.Thread(target=_loop, daemon=True, name="changes-sources")
        _thread.start()


def wait_for(n: int | None, topics: list[str], wait: float) -> dict:
    """since(), held until something asked for changes or `wait` seconds pass."""
    _note_asked(topics)
    end = time.monotonic() + max(0.0, min(float(wait or 0), MAX_WAIT))
    while True:
        out = since(n, topics)
        if out["changed"] or out["reset"] or n is None or time.monotonic() >= end:
            return out
        time.sleep(0.05)


_PID = r"/api/projects/([^/]+)"


def topics_for(method: str, path: str) -> list[str]:
    """The topics a successful write to this route changes. A write to a route this table does not know bumps every
    list: a screen that re-reads once too often is right, one that misses a change is not."""
    if method.upper() in ("GET", "HEAD", "OPTIONS") or not path.startswith("/api/") or path.startswith("/api/changes"):
        return []
    out: list[str] = []
    m = re.match(_PID, path)
    if m:
        out += ["projects", f"project:{m.group(1)}"]
        if re.search(r"/(deploy|staging|traffic|rollback|protect|serving)", path):
            out += ["deployed", f"deployment:{m.group(1)}"]
        if re.search(r"/(experiments|backtests|runs|evaluate|train)", path):
            out += ["runs"]
        return out
    if path.startswith("/api/projects"):
        return ["projects"]
    table = (
        (r"^/api/(datasets|data|library|tables|connections|testsets|dataset-classes|labels|scopes|mlflow|registries)", ["data"]),
        (r"^/api/(backtests|runs|workflows|grading|replays|live|evaluations|compare-runs|regression|investigations)", ["runs"]),
        (r"^/api/jobs", ["jobs"]),
        (r"^/api/(deployed|serving|deployments)", ["deployed"]),
        (r"^/api/(notebooks|kernels)", ["notebooks"]),
        (r"^/api/(cloud|profiles|logins|secrets)", ["logins"]),
        (r"^/api/compute", ["compute"]),
        (r"^/api/(settings|workspace|workspaces|members|team|onboarding)", ["settings"]),
    )
    for pattern, topics in table:
        if re.match(pattern, path):
            out += topics
    # Starting something that runs as a job changes the jobs list too.
    if out and re.search(r"/(run|import|deploy|pull|snapshot)$", path):
        out.append("jobs")
    return out or list(LISTS)
