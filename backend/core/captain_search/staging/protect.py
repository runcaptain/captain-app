"""What a deployed model needs before a real site sends it traffic.

Three things, all kept in the deployment's control document, which every copy on every host re-reads every few
seconds, so none of them needs a restart:

**A key that can be changed with no downtime.** `rotate_key` makes a new key and keeps the old one valid for a
grace period (an hour unless told otherwise), so a site can switch at its own pace. The copy Captain reaches is
told at once. Every other copy reads the change within a few seconds, and a copy that sees a key it does not
know reads the control document again before refusing (at most once a second).
The key itself lives in this computer's keychain. Only hashes are written anywhere else.

**Limits with a clear error.** A request bigger than the size limit is refused with 413 and a sentence. More
requests a second than the rate limit are refused with 429, a sentence and Retry-After. The rate is counted on
each copy: with three copies up the deployment takes three times as many.

**A time budget with a fallback answer.** When this model takes longer than the budget, or fails, the request
is answered with the compared model's results instead, and counted as a fallback. The site always gets an
answer. A fallback counts against the model's health like a failed request, so a model that is always slow
cannot look healthy. With no compared model loaded there is nothing to fall back to: a slow request gets 504.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets as _secrets

from ..workspace import Context, set_secret, whoami
from . import ENV, _key, _note, _save, call, control, set_control, state

MAX_REQUEST_KB = 256  # what a deployment takes until a person says otherwise
GRACE_MINUTES = 60.0
MAX_GRACE_MINUTES = 7 * 24 * 60.0


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _rec(ctx: Context, pid: str, env: str) -> dict:
    rec = state(ctx, pid, env)
    if not rec:
        raise ValueError("Nothing is deployed yet. Deploy the model first.")
    return rec


def _tell(ctx: Context, rec: dict) -> bool:
    """Ask the endpoint to read its control document now. Best effort: every copy reads it within seconds anyway,
    and a sleeping host is never woken for this."""
    if rec.get("status") != "running" or rec.get("how") == "image":
        return False
    try:
        from . import _awake

        if rec.get("scale_to_zero") and not _awake(ctx, rec):
            return False
        call(ctx, rec, "reload", {}, timeout=5.0)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- limits and the time budget ------------------------------------------------------------------------

def _words(lim: dict, bud: dict, has_other: bool) -> dict:
    kb, rate, ms = lim.get("max_request_kb"), lim.get("rate_per_s"), bud.get("ms")
    return {
        "size_text": f"Requests up to {kb:g} KB. A bigger one is refused with a sentence saying so.",
        "rate_text": (f"Up to {rate:g} requests a second on each copy. More than that are refused, and told to try again in a second."
                      if rate else "No limit on requests a second."),
        "budget_text": ((f"If this model takes longer than {ms:g} ms, or fails, the request is answered with the compared model's results."
                         if bud.get("fallback") and has_other else
                         f"A request that takes longer than {ms:g} ms is refused. There is no compared model loaded to answer in its place."
                         if not has_other else f"A request that takes longer than {ms:g} ms is refused.")
                        if ms else
                        ("No time budget. If this model fails, the request is answered with the compared model's results."
                         if bud.get("fallback") and has_other else "No time budget.")),
    }


def get(ctx: Context, pid: str, env: str = ENV) -> dict:
    """{limits: {max_request_kb, rate_per_s}, budget: {ms, fallback}, has_fallback, key: {...}, *_text}."""
    rec = _rec(ctx, pid, env)
    ctl = control(ctx, pid, env)
    lim = {"max_request_kb": MAX_REQUEST_KB, "rate_per_s": None, **(ctl.get("limits") or {})}
    bud = {"ms": None, "fallback": True, **(ctl.get("budget") or {})}
    has_other = {"candidate", "baseline"} <= set(rec.get("arms") or {}) and rec.get("serve") != "baseline"
    return {"project": pid, "limits": lim, "budget": bud, "has_fallback": has_other, "key": key_state(ctx, pid, env),
            **_words(lim, bud, has_other)}


def change(ctx: Context, pid: str, env: str = ENV, **given) -> dict:
    """Change any of max_request_kb (1 to 10240), rate_per_s (a number, or 0 for no limit), budget_ms (10 to 60000,
    or 0 for none) and fallback (true or false). What is not given stays as it is."""
    rec = _rec(ctx, pid, env)
    ctl = control(ctx, pid, env)
    lim = {"max_request_kb": MAX_REQUEST_KB, "rate_per_s": None, **(ctl.get("limits") or {})}
    bud = {"ms": None, "fallback": True, **(ctl.get("budget") or {})}

    def number(name, what, lo, hi, zero_is_none=True):
        v = given.get(name)
        if v is None:
            return "keep"
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{what} is a number") from None
        if zero_is_none and x == 0:
            return None
        if not lo <= x <= hi:
            raise ValueError(f"{what} is between {lo:g} and {hi:g}" + (", or 0 for none" if zero_is_none else ""))
        return x

    kb = number("max_request_kb", "the request size limit, in KB,", 1, 10240, zero_is_none=False)
    rate = number("rate_per_s", "the requests a second", 0.1, 100000)
    ms = number("budget_ms", "the time budget, in ms,", 10, 60000)
    if kb != "keep":
        lim["max_request_kb"] = kb
    if rate != "keep":
        lim["rate_per_s"] = rate
    if ms != "keep":
        bud["ms"] = ms
    if given.get("fallback") is not None:
        bud["fallback"] = bool(given["fallback"])
    set_control(ctx, pid, env, limits=lim, budget=bud)
    _tell(ctx, rec)
    out = get(ctx, pid, env)
    _note(ctx, pid, "changed what the deployed model accepts: " + " ".join((out["size_text"], out["rate_text"], out["budget_text"])))
    return out


# ---- the key -------------------------------------------------------------------------------------------

def key_state(ctx: Context, pid: str, env: str = ENV) -> dict:
    """{rotated_at, rotated_by, previous_until, previous_valid, can_rotate, why_not, text}. Never the key."""
    rec = _rec(ctx, pid, env)
    keys = control(ctx, pid, env).get("keys") or {}
    until = keys.get("previous_until")
    valid = bool(keys.get("previous")) and bool(until) and until > _iso(_now())
    why = None
    if not rec.get("key_secret"):
        why = "This deployment has no key of its own: it calls an endpoint you run."
    elif os.environ.get(rec["key_secret"], "").strip():
        why = (f"The key is set in the environment as {rec['key_secret']}. Change it there and deploy again. "
               "Captain can only change a key it keeps in the keychain.")
    text = ("The key has not been changed since it was made." if not keys.get("rotated_at") else
            f"Changed {keys['rotated_at'][:16].replace('T', ' ')} UTC by {keys.get('rotated_by') or 'someone'}. "
            + (f"The old key works until {until[:16].replace('T', ' ')} UTC." if valid else "The old key no longer works."))
    return {"rotated_at": keys.get("rotated_at"), "rotated_by": keys.get("rotated_by"), "previous_until": until if valid else None,
            "previous_valid": valid, "can_rotate": why is None, "why_not": why, "text": text}


def rotate_key(ctx: Context, pid: str, grace_minutes: float | None = None, env: str = ENV) -> dict:
    """Make a new key. The old one keeps working for `grace_minutes` (60 unless given, 0 to end it now, at most a
    week). Returns key_state plus `rotated: True`. The key is read the way it always is: copied from the app, or
    `cst deployments key <project> --show`."""
    rec = _rec(ctx, pid, env)
    st = key_state(ctx, pid, env)
    if not st["can_rotate"]:
        raise ValueError(st["why_not"])
    try:
        grace = GRACE_MINUTES if grace_minutes is None else float(grace_minutes)
    except (TypeError, ValueError):
        raise ValueError("the grace period is a number of minutes") from None
    if not 0 <= grace <= MAX_GRACE_MINUTES:
        raise ValueError("the grace period is between 0 minutes and a week")
    old = _key(rec)
    new = _secrets.token_urlsafe(32)
    now = _now()
    before = control(ctx, pid, env).get("keys")
    keys = {"current": hashlib.sha256(new.encode()).hexdigest(),
            "previous": hashlib.sha256(old.encode()).hexdigest() if old and grace > 0 else None,
            "previous_until": _iso(now + dt.timedelta(minutes=grace)) if old and grace > 0 else None,
            "rotated_at": _iso(now), "rotated_by": whoami()}
    # The endpoint learns both keys first, and is told to read them while this computer still holds the old one,
    # so nothing sent next is refused. Then the keychain.
    set_control(ctx, pid, env, keys=keys)
    _tell(ctx, rec)
    try:
        set_secret(rec["key_secret"], new)
    except Exception as e:  # noqa: BLE001
        set_control(ctx, pid, env, keys=before)  # the keychain still holds the old key: keep the endpoint on it
        _tell(ctx, rec)
        raise ValueError(f"This computer's keychain would not take the new key ({type(e).__name__}), so the key was not changed.") from None
    _save(ctx, pid, env, key_rotated_at=keys["rotated_at"])
    _note(ctx, pid, "changed the deployed model's key. " + (f"The old key works for {grace:g} more minutes." if keys["previous"] else "The old key stopped working at once."))
    return {**key_state(ctx, pid, env), "rotated": True}
