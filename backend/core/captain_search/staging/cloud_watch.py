"""What only a cloud knows, told the moment it changes.

A model that stops by itself, is deleted in the cloud's console, or scales to another copy writes nothing in the
workspace. A Deployments row said "Stopping" for two hours after its model was gone, because nothing asked.

While any client listens to `deployed` or a `deployment:<project>` on the change feed (changes.py), one watcher
for each connected login asks that cloud what is there, once a second, and bumps the topic only when the answer
changes. Then the screen reads again, and the read sets Captain's own records right (fleet.py).

What it asks is each cloud's own listing of what is deployed: a describe, free, and never a call to a model, so a
model that sleeps stays asleep and nothing is counted as traffic. The paid metric reads (numbers.py) are not
asked here and stay on their own ration.

It is silent when nobody listens: the threads end, and nothing is asked until someone listens again. A cloud that
answers with an error is asked less often (2, 4, 8 ... up to 60 seconds), and one that says it is being asked too
often waits at least 15. A listing that is slow (Modal's starts a process) is never asked more than half the
time: the wait is at least as long as the last ask took.
"""
from __future__ import annotations

import threading
import time

from .. import changes as CH

EVERY_S = 1.0
MAX_WAIT_S = 60.0
LIMITED_WAIT_S = 15.0
LIMITED = ("throttl", "rate exceeded", "too many requests", "429", "ratelimit", "rate limit", "quota")
TOPICS = ("deployed", "deployment:")

_lock = threading.Lock()
_threads: dict[tuple, threading.Thread] = {}
_ctx_of = None            # how to get the workspace the service is serving (server.py sets it)
asks: list = []           # (when, cloud, login): every ask made, for the test that it is silent with no listeners
MAX_ASKS_KEPT = 2000


def listening() -> bool:
    return any(CH.FEED.listening(t) for t in TOPICS)


def _sig(things: list[dict]) -> dict:
    """What a screen would show differently, for each thing found: its state, its copies, which version it is."""
    return {str(t.get("name")): (t.get("state"), t.get("copies_now"), t.get("copies_min"), t.get("copies_max"), t.get("stage"),
                                 t.get("machine"), t.get("reason"), t.get("project"), t.get("env")) for t in things}


def _wait_after(took: float, err: str | None, failures: int) -> float:
    """How long to wait before asking this cloud again."""
    if err:
        limited = any(w in err.lower() for w in LIMITED)
        back = min(MAX_WAIT_S, 2.0 ** min(failures, 6))
        return max(back, LIMITED_WAIT_S) if limited else back
    return max(EVERY_S - took, took, 0.05)   # never asking more than half the time


def _watch(ctx, src: dict, key: tuple) -> None:
    from . import fleet as FL

    last, primed, failures, looked = None, False, 0, time.monotonic()
    try:
        while listening():
            if time.monotonic() - looked > 10.0:
                looked = time.monotonic()
                ensure(ctx)   # a login connected while this runs gets a watcher of its own
            t0 = time.monotonic()
            things, err = FL._ask(ctx, src)
            took = time.monotonic() - t0
            with _lock:
                asks.append((time.time(), src["cloud"], src.get("login")))
                del asks[:-MAX_ASKS_KEPT]
            if err:
                failures += 1
                if failures == 1 and primed:
                    CH.bump("deployed")   # it could be asked and now can't: the screens say so
            else:
                now = _sig(things)
                if primed and (now != last or failures):
                    # Something in this cloud changed. The kept answer is dropped, so the read the screens make
                    # next asks again and sets the records right. Told for each project it touches.
                    FL.forget(ctx)
                    moved = {n for n in set(now) | set(last or {}) if (last or {}).get(n) != now.get(n)}
                    projects = {(now.get(n) or (last or {}).get(n) or (None,) * 9)[7] for n in moved}
                    CH.bump("deployed", *[f"deployment:{p}" for p in projects if p])
                last, primed, failures = now, True, 0
            # Sleep in short steps, so the thread ends soon after the last listener leaves.
            end = time.monotonic() + _wait_after(took, err, failures)
            while time.monotonic() < end and listening():
                time.sleep(min(0.2, max(0.0, end - time.monotonic())))
    finally:
        with _lock:
            if _threads.get(key) is threading.current_thread():
                del _threads[key]


def ensure(ctx=None) -> int:
    """Start a watcher for each connected login that has none, if anyone is listening. Returns how many run."""
    if not listening():
        return 0
    try:
        ctx = ctx if ctx is not None else (_ctx_of() if _ctx_of else None)
        if ctx is None:
            return 0
        from . import fleet as FL

        srcs = FL.sources(ctx)
    except Exception:  # noqa: BLE001
        return 0
    with _lock:
        for s in srcs:
            key = (ctx.workspace.name, s["cloud"], s.get("login"), s.get("region"))
            t = _threads.get(key)
            if t is not None and t.is_alive():
                continue
            t = threading.Thread(target=_watch, args=(ctx, s, key), daemon=True, name=f"cloud-watch-{s['cloud']}")
            _threads[key] = t
            t.start()
        return sum(1 for t in _threads.values() if t.is_alive())


def attach(ctx_of) -> None:
    """Called once by the service: when someone starts listening to what is deployed, the watchers start. They
    are looked at again every few seconds while it lasts, so a login connected meanwhile gets one too."""
    global _ctx_of
    _ctx_of = ctx_of
    CH.on_listen(lambda: ensure())


def running() -> int:
    with _lock:
        return sum(1 for t in _threads.values() if t.is_alive())


def reset_for_tests() -> None:
    with _lock:
        _threads.clear()
        del asks[:]
