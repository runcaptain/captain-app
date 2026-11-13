"""Say that something changed, so a screen that is looking hears at once and never has to ask on a timer.

Topics:
  deployed                 what is deployed changed in any way: a model appeared, went, changed state or step
  deployment:<project>     that project's deployed model changed
  job:<id>                 a job's status, step, progress line or end

The feed itself (who is waiting, and waking them) is `captain_search.changes`. This module is the one place the
serving code calls, so the feed's own names can change without touching the callers. Where there is no feed (a
terminal command, a test), a small stand-in keeps the same counts in this process, so a bump is never an error.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_seq: dict[str, int] = {}      # the stand-in's counts, by topic
_wanted: set[str] = set()      # the stand-in's listeners: topics someone said they are waiting on


def _feed():
    try:
        from .. import changes as CH  # the service's change feed

        return CH if callable(getattr(CH, "bump", None)) else None
    except Exception:  # noqa: BLE001  not there: the stand-in below
        return None


def bump(*topics: str) -> None:
    """Something changed under each topic. Safe from any thread, cheap, and never raises."""
    feed = _feed()
    for t in dict.fromkeys(x for x in topics if x):
        try:
            if feed is not None:
                feed.bump(t)
            else:
                with _lock:
                    _seq[t] = _seq.get(t, 0) + 1
        except Exception:  # noqa: BLE001  telling a screen never breaks the work being told about
            pass


def listening(prefix: str) -> bool:
    """Is anyone waiting on this topic, or on one that starts with it? The cloud watcher runs only while this is true."""
    feed = _feed()
    if feed is not None and callable(getattr(feed, "listening", None)):
        try:
            return bool(feed.listening(prefix))
        except Exception:  # noqa: BLE001
            return False
    with _lock:
        return any(t == prefix or t.startswith(prefix) for t in _wanted)


def project(pid: str | None) -> str | None:
    return f"deployment:{pid}" if pid else None


def job(jid: str | None) -> str | None:
    return f"job:{jid}" if jid else None


# ---- the stand-in, for tests and for a process with no feed ---------------------------------------


def seq(topic: str) -> int:
    with _lock:
        return _seq.get(topic, 0)


def listen(*topics: str) -> None:
    with _lock:
        _wanted.update(topics)


def unlisten(*topics: str) -> None:
    with _lock:
        for t in topics or list(_wanted):
            _wanted.discard(t)


def reset() -> None:
    with _lock:
        _seq.clear()
        _wanted.clear()
