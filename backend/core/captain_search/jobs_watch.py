"""Compute jobs, told the moment their state changes.

A job that runs on a compute host (a cloud batch job, a Modal function) writes nothing in the workspace while it
runs: its state is the host's to say. The Runs screen used to ask for the whole list, "and ask each host first",
every 4 seconds, and for an open job's new log lines every 2. Every open window did, each on its own timer.

This asks once, here, for all of them. While any client listens to `jobs` or a `job:<id>` on the change feed
(changes.py), every 4 seconds each unfinished job's host is asked for its state. A state that changed is saved,
and the save tells the feed like any other write (`jobs`, `job:<id>`). A job that is still running is also told
on each look, because its log lines live on the host and may have grown: the screen showing that job reads on
from where it was.

With nobody listening nothing is asked. A finished job is never asked again. A look never takes more than half
the time: the wait is at least as long as the look took.
"""
from __future__ import annotations

import threading
import time

import yaml

from . import changes as CH
from . import compute_providers as P
from . import jobs as J

EVERY_S = 4.0
IDLE_S = 1.0     # how often it checks whether someone has started listening

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_stops: dict[str, threading.Event] = {}
looks = 0        # looks made, for the test that it is silent with no listeners


def wanted() -> bool:
    return CH.listening("jobs") or CH.listening("job:")


def look(ctx, done: set[str]) -> list[str]:
    """One look: ask each unfinished job's host for its state. Returns the ids of jobs still going. `done` holds
    the ids already known to be finished, so their records are not read again."""
    global looks
    looks += 1
    going = []
    for k in ctx.store.list("jobs/"):
        if not k.endswith(".yaml") or k.endswith(".result.yaml"):
            continue
        jid = k[len("jobs/"):-len(".yaml")]
        if jid in done:
            continue
        try:
            rec = yaml.safe_load(ctx.store.get(k))
            if rec["state"] not in P.TERMINAL and rec.get("handle"):
                rec = J.refresh(ctx, rec)   # saves what changed, and the save tells the feed
        except Exception:  # noqa: BLE001  one job that can't be read or asked never hides the others
            continue
        if rec.get("state") in P.TERMINAL:
            done.add(jid)
        elif rec.get("handle"):
            going.append(jid)
    return going


def run(ctx, stop: threading.Event) -> None:
    done: set[str] = set()
    while not stop.is_set():
        if not wanted():
            stop.wait(IDLE_S)
            continue
        t0 = time.monotonic()
        try:
            going = look(ctx, done)
            if going:
                CH.bump(*[f"job:{j}" for j in going])   # still running: its log on the host may have grown
        except Exception:  # noqa: BLE001  the next look tries again
            pass
        took = time.monotonic() - t0
        stop.wait(max(EVERY_S - took, took, 0.05))


def start(ctx) -> threading.Event:
    """Start this workspace's jobs watcher in the background, once. Returns the event that stops it."""
    name = ctx.workspace.name
    with _lock:
        if name in _threads and _threads[name].is_alive():
            return _stops[name]
        _stops[name] = threading.Event()
        _threads[name] = threading.Thread(target=run, args=(ctx, _stops[name]), daemon=True, name="watch-jobs")
        _threads[name].start()
        return _stops[name]


def stop_all() -> None:
    with _lock:
        for e in _stops.values():
            e.set()
        _threads.clear()
        _stops.clear()
