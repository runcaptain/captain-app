"""Say that something about a deployed model changed, on the app's change feed (`captain_search.changes`), so a
screen that is looking hears at once and never has to ask on a timer.

Topics:
  deployed                 what is deployed changed in any way: a model appeared, went, changed state or step
  deployment:<project>     that project's deployed model changed
  job:<id>, jobs           a job's status, step, progress line or end

This is the one place the serving code calls, so the topic names are spelled once.
"""
from __future__ import annotations

from .. import changes as CH

LINGER_S = None   # how long a listener that just left still counts; None is the feed's own (a long poll comes straight back)


def bump(*topics: str) -> None:
    """Something changed under each topic. Safe from any thread, cheap, and never raises: telling a screen must
    not break the work being told about."""
    try:
        CH.bump(*[t for t in topics if t])
    except Exception:  # noqa: BLE001
        pass


def listening(prefix: str) -> bool:
    """Is anyone listening to this topic, or to one that starts with it? The cloud watcher runs only while so."""
    try:
        return bool(CH.FEED.listening(prefix) if LINGER_S is None else CH.FEED.listening(prefix, linger=LINGER_S))
    except Exception:  # noqa: BLE001
        return False


def project(pid: str | None) -> str | None:
    return f"deployment:{pid}" if pid else None


def job(jid: str | None) -> str | None:
    return f"job:{jid}" if jid else None
