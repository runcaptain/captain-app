"""How long each step of a deploy takes, measured on a real account. One table.

The confirm ("About 4 minutes"), each step of the plan and the lines a run prints ("Starting the endpoint (about 2
minutes)") all read this, so they cannot disagree. Before this table the confirm said about four minutes while the
run's own lines said "about 3 minutes" for the image and "4 to 10 minutes" for the endpoint, in a deploy that took
3 minutes 50 seconds.

Seconds, by host, then by whether it is the first deploy there, then by the plan's step id. A step that is missing
has not been timed on a real account: nothing is said about it, and a host with no entry gets a cautious range.

  aws    2026-11-12, us-west-1, ml.t2.medium, the demo store's starter ranker beside its baseline, one command from
         an account with nothing of Captain's in it: 229.7 s in all. "again" is the same start without the first-time
         steps. Moving a running endpoint to a new version has not been timed yet.
  modal  2026-11-11, one copy of 1 CPU and 2 GB: 1 s to set up, 96 s to ready with the image build, 20 s without.
"""
from __future__ import annotations

STEPS = {
    "aws": {"on": "2026-11-12",
            "first": {"check": 1, "access": 2, "image": 92, "start": 134, "ready": 1},
            "again": {"check": 1, "start": 134, "ready": 1}},
    "modal": {"on": "2026-11-11",
              "first": {"access": 1, "start": 96},
              "again": {"start": 20}},
}
UNMEASURED_MINUTES = {True: (5, 12), False: (2, 6)}  # first time, and after: a cautious range


def measured_on(host: str) -> str | None:
    return (STEPS.get(host) or {}).get("on")


def seconds(host: str, step: str, first_time: bool = True) -> int | None:
    """What this step took on a real account, or None when it has not been timed."""
    return ((STEPS.get(host) or {}).get("first" if first_time else "again") or {}).get(step)


def total(host: str, first_time: bool = True) -> int | None:
    """The whole deploy: the sum of its steps. None for a host that has not run on a real account."""
    steps = (STEPS.get(host) or {}).get("first" if first_time else "again")
    return sum(steps.values()) if steps else None


def words(s: float | None) -> str | None:
    """A measured time as a person says it: "under a minute", "about a minute and a half", "about 2 minutes"."""
    if s is None:
        return None
    if s < 50:
        return "under a minute"
    if s < 75:
        return "about a minute"
    if s < 105:
        return "about a minute and a half"
    return f"about {int(s / 60 + 0.5)} minutes"


def step_words(host: str, step: str, first_time: bool = True) -> str | None:
    return words(seconds(host, step, first_time))


def aside(host: str, step: str, first_time: bool = True, unknown: str = "not timed on a real account yet") -> str:
    """The bracket after a slow step's line: " (about 2 minutes)", or that it has not been timed."""
    w = step_words(host, step, first_time)
    return f" ({w})" if w else f" ({unknown})"
