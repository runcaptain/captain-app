"""Putting an earlier version back, for real.

A roll back is a deploy of the earlier version to the same place, through the same one-job deploy every deploy
uses (deploy.run). So it works on every host that deploy works on, runs the same steps (the model answers, its
numbers match training), and keeps serving during the switch the same way: the version that is up stays up until
the earlier one answers, and only then is it taken down. If the earlier version fails to start, what was running
keeps running.

Who did it and why is kept: each roll back is written to the deployment's record (`rollbacks`, newest first) and
to the project's history, with the version it left, the one it put back, the person and the reason. A reason is
required from a person. Captain's own roll backs carry theirs.

Automatic roll back is an option a person turns on per deployment: for the first N minutes after a version is
deployed, if a health check finds it unhealthy (two bad checks in a row, against the person's target), Captain
puts the previous version back and says so in the alert. It never fires without an earlier version to go to,
never twice for one deploy, and never while a deploy is already running.
"""
from __future__ import annotations

import datetime as dt

from .. import project_state as PS
from ..workspace import Context, whoami
from . import ENV, _ago, _note, _save, state

KEEP = 50


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def earlier(ctx: Context, pid: str, env: str = ENV) -> list[dict]:
    """The versions this deployment can go back to, newest first: every earlier deploy that is not the one running."""
    from .traffic import versions

    rec = state(ctx, pid, env) or {}
    seen, out = set(), []
    for r in versions(ctx, pid, env)["rows"]:
        if r["live"] or r["model_version"] == rec.get("model_version") or r["model_version"] in seen:
            continue
        seen.add(r["model_version"])
        out.append(r)
    return out


def go(ctx: Context, pid: str, to: int | None = None, reason: str | None = None, env: str = ENV, auto: bool = False,
       on_event=None) -> dict:
    """Put an earlier version back on this deployment. `to` is its number in Earlier versions, default the one
    before the version running now. Returns {rolled_back_to, from, version, reason, by, auto, deploy}."""
    from . import deploy as DP

    rec = state(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Nothing is deployed, so there is nothing to roll back.")
    why = " ".join(str(reason or "").split())
    if not why:
        raise ValueError("Say why: a roll back is recorded with its reason. Pass --reason \"...\".")
    choices = earlier(ctx, pid, env)
    if not choices:
        raise ValueError("There is no earlier version to go back to. This is the first one deployed here.")
    row = choices[0] if to is None else next((r for r in choices if r["n"] == int(to)), None)
    if row is None:
        raise ValueError(f"There is no earlier version {to} to go back to. See cst deployments history {pid}.")
    doc = PS.get(ctx, pid)
    left = rec.get("model_version")
    is_base = row["model_version"] == doc.get("champion")
    # The same one-job deploy, to the same place. What is running stays up until the earlier version answers.
    try:
        out = DP.run(ctx, pid, rec.get("host") or "local", rec.get("login"), candidate=None if is_base else row["model_version"],
                     baseline=is_base, env=env, confirm=True, on_event=on_event, force=True)
        failed = None if out.get("status") == "done" else str(out.get("error") or "")
    except Exception as e:  # noqa: BLE001  whatever stopped it, the question is what is running now
        failed = str(e)
    if failed is not None:
        now = state(ctx, pid, env) or {}
        still = now.get("status") == "running" and now.get("model_version") == left
        raise ValueError((f"The earlier version didn't start, so {str(left).split('/')[-1]} is still running. " if still
                          else "The earlier version didn't start, and nothing is running now. ") + failed[:400])
    entry = {"at": _now(), "by": "Captain" if auto else whoami(), "from": left, "to": row["model_version"], "version": row["n"],
             "reason": why, "auto": bool(auto)}
    now = state(ctx, pid, env) or {}
    _save(ctx, pid, env, rollbacks=[entry, *(now.get("rollbacks") or [])][:KEEP], rolled_back=entry)
    _note(ctx, pid, f"{'Captain rolled' if auto else 'rolled'} the deployment back from {left} to {row['model_version']}: {why}")
    return {"rolled_back_to": row["model_version"], "from": left, "version": row["n"], "reason": why, "by": entry["by"], "auto": bool(auto),
            "deploy": {k: out.get(k) for k in ("status", "address", "steps", "soft_failed")},
            "text": f"{'Captain put' if auto else 'Put'} {row['model_version'].split('/')[-1]} back in place of {str(left).split('/')[-1]}. {why}"}


def history(ctx: Context, pid: str, env: str = ENV) -> dict:
    """Every roll back of this deployment, newest first: when, who, from which version to which, and why."""
    rows = list((state(ctx, pid, env) or {}).get("rollbacks") or [])
    return {"project": pid, "rows": rows, "auto": auto(ctx, pid, env),
            "text": f"{len(rows)} roll back{'s' if len(rows) != 1 else ''} so far." if rows else "It has never been rolled back."}


# ---- automatic roll back ----------------------------------------------------------------------------

def auto(ctx: Context, pid: str, env: str = ENV) -> dict:
    """{minutes, on, text}: for how long after a deploy an unhealthy version is put back by itself."""
    m = ((state(ctx, pid, env) or {}).get("auto_rollback") or {}).get("minutes")
    return {"minutes": m, "on": bool(m),
            "text": (f"On: if a new version is unhealthy in its first {m:g} minutes, Captain puts the previous one back and tells you."
                     if m else "Off: an unhealthy version stays up until you roll it back.")}


def set_auto(ctx: Context, pid: str, minutes, env: str = ENV) -> dict:
    """Turn automatic roll back on for the first `minutes` after each deploy (5 to 1440), or off with None or 0."""
    if not state(ctx, pid, env):
        raise ValueError("Nothing is deployed, so there is nothing to protect yet.")
    if minutes in (None, "", 0, "0"):
        _save(ctx, pid, env, auto_rollback=None)
        return auto(ctx, pid, env)
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        raise ValueError("the time is a number of minutes, like 30") from None
    if not 5 <= m <= 1440:
        raise ValueError("the time is between 5 minutes and a day")
    _save(ctx, pid, env, auto_rollback={"minutes": m, "by": whoami(), "at": _now()})
    return auto(ctx, pid, env)


def maybe(ctx: Context, pid: str, health: dict, env: str = ENV) -> dict | None:
    """Called when a check has just found the deployment unhealthy. Rolls back if the person asked for that, the
    version is still inside its first minutes, there is an earlier one, and this deploy was not rolled back
    already. Returns what it did, or None. Never raises: a roll back that can't happen leaves the alert as it is."""
    rec = state(ctx, pid, env) or {}
    m = (rec.get("auto_rollback") or {}).get("minutes")
    if not m or rec.get("status") != "running":
        return None
    age = _ago(rec.get("started_at"))
    if age is None or age > float(m) * 60:
        return None
    if (rec.get("rolled_back") or {}).get("to") == rec.get("model_version"):
        return None  # this is the version a roll back put here: there is nowhere further to go by itself
    try:
        if not earlier(ctx, pid, env):
            return None
        why = (f"It turned unhealthy {max(1, round(age / 60))} minute{'s' if round(age / 60) != 1 else ''} after it was deployed. "
               + " ".join(health.get("reasons") or []))
        return go(ctx, pid, None, why, env, auto=True)
    except Exception as e:  # noqa: BLE001
        return {"failed": True, "text": f"Captain tried to put the previous version back and couldn't: {str(e)[:200]}"}
