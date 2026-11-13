"""Watch the clouds while someone is looking, so a screen knows within a second or two that a model appeared,
went or changed state, whoever did it and wherever: here, in a terminal, in the cloud's own console.

One watcher runs for each place to ask (a connected login and its region). Each asks that cloud's own listing,
which is free, once a second, and says "changed" only when the answer differs from the last one. It also looks at
Captain's own records each second, which costs nothing, so a model on this computer and a deploy's steps are told
the same way.

Rules it keeps:
  - Nobody listening, nothing asked. It starts with the first listener and stops with the last.
  - It never calls a model. The listing is the cloud's control plane.
  - It never reads the paid numbers (metrics). Those stay on their own ration.
  - An error slows it down: asked again after 2, 4, 8 and up to 60 seconds. A cloud that says "slow down" is asked
    no more than once every 15 seconds, rising to a minute, and the list says so in words.
  - It is never asking more than half the time: after an ask that took a while, it waits at least as long again.
    Asking Modal what is there starts a process (`modal app list`) that takes about a second and real CPU, so
    Modal is asked every two seconds, not every one.

What asking Modal costs, as an estimate (2026-11-12): starting the process and loading Modal's command took 0.19
seconds and 0.18 seconds of one processor, the median of five runs of a command of the same shape that touches no
network and no workspace (`python -m modal app list --help`) on a laptop. At one ask every two seconds that is
about 9% of one processor while a screen is open, before the network round trip, which was not measured: the only
Modal workspace on the machine it was timed on runs production, and nothing was run against it. Listing through
Modal's library inside the service, with no process to start, would remove that cost.
"""
from __future__ import annotations

import threading
import time

from ..workspace import Context
from . import announce as AN
from . import fleet as FL

EVERY_S = 1.0          # how often a place is asked while all is well
EVERY_BY_CLOUD = {"modal": 2.0}   # a cloud whose listing costs more than a network call
ERROR_MAX_S = 60.0     # the slowest it asks after plain errors: 2, 4, 8 and up to this
SLOW_MIN_S = 15.0      # the fastest it asks a cloud that said to slow down
SLOW_MAX_S = 60.0
FRESH_S = 2.5          # how long an answer may stand in for asking again
# Words a cloud uses when it wants fewer calls.
SLOW_WORDS = ("throttl", "rate exceeded", "too many requests", "toomanyrequests", "ratelimit", "rate limit", "quota", "resource_exhausted", " 429")

_lock = threading.Lock()
_places: dict[tuple, dict] = {}     # (workspace, cloud, login, region) -> {things, err, at, every, slowed, sig, thread}
_boss: dict[str, threading.Thread] = {}
_stop: dict[str, threading.Event] = {}


def _key(ctx: Context, src: dict) -> tuple:
    return (ctx.workspace.name, src["cloud"], src.get("login"), src.get("region"))


def _sig(things: list[dict], err: str | None) -> tuple:
    """Everything a place said about each thing it has, as something to compare with the last answer."""
    return (bool(err), tuple(sorted(tuple(sorted((str(k), str(v)) for k, v in t.items())) for t in things)))


def every(cloud: str) -> float:
    """How often this cloud is asked while all is well."""
    return max(EVERY_S, EVERY_BY_CLOUD.get(cloud, EVERY_S)) if EVERY_S >= 1.0 else EVERY_S * EVERY_BY_CLOUD.get(cloud, 1.0)


def _slow(err: str | None) -> bool:
    e = f" {err or ''}".lower()
    return any(w in e for w in SLOW_WORDS)


def recent(ctx: Context, src: dict) -> tuple[list[dict], str | None] | None:
    """The watcher's last answer for this place when it is fresh enough to stand in for asking again, or None.
    While someone is looking, the list is read from here and makes no call of its own."""
    with _lock:
        p = _places.get(_key(ctx, src))
        if p and p.get("at") and time.monotonic() - p["at"] < max(FRESH_S, p.get("wait", p.get("every", EVERY_S)) * 1.5):
            return list(p["things"]), p["err"]
    return None


def status(ctx: Context) -> list[dict]:
    """Each place being watched now: [{cloud, login, region, every_s, slowed, text}]. `text` is set when it has been
    slowed: "AWS asked Captain to slow down. Asking every 10 seconds." """
    out = []
    with _lock:
        for (ws, cloud, login, region), p in _places.items():
            if ws != ctx.workspace.name:
                continue
            label = FL.CLOUDS.get(cloud, cloud)
            every = p.get("every", EVERY_S)
            base = globals()["every"](cloud)
            text = (f"{label} asked Captain to slow down. Asking every {every:g} seconds." if p.get("slowed")
                    else f"{label} could not be asked. Trying again every {every:g} seconds." if p.get("err") and every > base else None)
            out.append({"cloud": cloud, "login": login, "region": region, "every_s": every, "slowed": bool(p.get("slowed")), "text": text,
                        "took_s": round(p.get("took") or 0.0, 3)})
    return out


def look(ctx: Context, src: dict) -> bool:
    """Ask one place once, remember the answer, and say so if it changed. Returns whether it changed."""
    began = time.monotonic()
    things, err = FL._ask(ctx, src)
    took = time.monotonic() - began
    k, base = _key(ctx, src), every(src["cloud"])
    sig = _sig(things, err)
    with _lock:
        p = _places.setdefault(k, {"every": base})
        first = "sig" not in p
        changed = (not first) and p["sig"] != sig
        if err:
            slow = _slow(err)
            p["slowed"] = slow
            p["every"] = min(SLOW_MAX_S, max(SLOW_MIN_S, p["every"] * 2)) if slow else min(ERROR_MAX_S, max(base * 2, p["every"] * 2))
        else:
            p["slowed"], p["every"] = False, base
        p["took"] = took
        p["wait"] = max(p["every"], took)  # never asking more than half the time
        # An answer that could not be had does not replace the last good one as "what is there".
        p.update(things=things if not err else p.get("things", []), err=err, at=time.monotonic(), sig=sig)
    if changed:
        FL.forget(ctx)
        _tell(ctx)
    return changed


def _tell(ctx: Context, projects: list[str] | None = None) -> None:
    if projects is None:
        try:
            projects = [pr["id"] for pr, _env, _rec in FL._records(ctx)]
        except Exception:  # noqa: BLE001
            projects = []
    AN.bump("deployed", *[AN.project(p) for p in projects])


def _records_mark(ctx: Context) -> dict:
    """Captain's own records and the deploys under way, by project: what a local model or a deploy's step shows as."""
    out = {}
    try:
        for pr, env, rec in FL._records(ctx):
            out[pr["id"]] = (str(rec.get("id")), str(rec.get("status")), str(rec.get("model_version")), str(rec.get("stopped_at")))
        for (pid, env), g in FL._deploying(ctx).items():
            out[pid] = (*out.get(pid, ()), "deploying", str(g["step"].get("id")), str(g.get("last_line")))
    except Exception:  # noqa: BLE001
        pass
    return out


def wanted() -> bool:
    return AN.listening("deployed") or AN.listening("deployment:")


def _watch_place(ctx: Context, src: dict, stop: threading.Event) -> None:
    k = _key(ctx, src)
    while not stop.is_set() and wanted():
        look(ctx, src)
        with _lock:
            wait = _places.get(k, {}).get("wait", every(src["cloud"]))
        stop.wait(wait)
    with _lock:
        _places.pop(k, None)   # nobody is looking: what was seen is no longer fresh, and is not kept


def run(ctx: Context, stop: threading.Event | None = None, idle_s: float = EVERY_S) -> None:
    """The watcher for one workspace: runs until `stop` is set. With nobody listening it asks nothing and only
    checks, once a second, whether someone has started to. Each place gets its own thread, so a cloud that is slow
    or slowed never holds up another."""
    stop = stop or threading.Event()
    threads: dict[tuple, threading.Thread] = {}
    mark = None
    while not stop.is_set():
        if not wanted():
            mark = None
            stop.wait(idle_s)
            continue
        try:
            for src in FL.sources(ctx):
                k = _key(ctx, src)
                if k not in threads or not threads[k].is_alive():
                    threads[k] = threading.Thread(target=_watch_place, args=(ctx, src, stop), daemon=True, name=f"watch-{src['cloud']}")
                    threads[k].start()
        except Exception:  # noqa: BLE001  the next second tries again
            pass
        now = _records_mark(ctx)
        if mark is not None and now != mark:
            changed = [p for p in set(now) | set(mark) if now.get(p) != mark.get(p)]
            FL.forget(ctx)
            _tell(ctx, changed)
        mark = now
        stop.wait(idle_s)


def start(ctx: Context) -> threading.Event:
    """Start this workspace's watcher in the background, once. Returns the event that stops it."""
    name = ctx.workspace.name
    with _lock:
        if name in _boss and _boss[name].is_alive():
            return _stop[name]
        _stop[name] = threading.Event()
        _boss[name] = threading.Thread(target=run, args=(ctx, _stop[name]), daemon=True, name="watch-clouds")
        _boss[name].start()
        return _stop[name]


def stop_all() -> None:
    with _lock:
        for e in _stop.values():
            e.set()
        _boss.clear()
        _stop.clear()
        _places.clear()
