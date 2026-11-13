"""A deployed model's health, as a fact Captain keeps.

A check asks four things and writes the answer down:

  is it answering       one status call to the endpoint, with a short timeout
  its error rate        failed requests over all requests in the last minutes, from the request log
  its response time     the 95th percentile at the endpoint, against a target the person sets
  when it last worked   the last request, and the time of this check

The result goes on the deployment's record as `health` (the newest one) and as one write-once file per check under
`staging/<project>/<env>/health/`, so the history is kept. A check is run by the service while the app is open
(every minute), by `cst deployments watch` for an agent or a cron, and on demand.

Unhealthy takes two bad checks in a row, so one slow minute is not an alert. Recovering takes one good one. When
the status changes either way a message goes out: to the webhook a person set (Slack, PagerDuty and the like take
a JSON body with `text`), and to the caller, which the app shows as a system notification.

Three things a check never does. It never wakes a host that sleeps when idle: with no copy up the status is
`asleep`, and nothing is sent. It never alerts while a deploy is running on that project: a switch-over is not an
outage. And it never guesses: with too few requests to judge a rate it says so, and judges only whether the model
answers.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.request

from ..model.entities import new_id
from ..workspace import Context
from . import ENV, _save, ask_status, call, state
from . import logs as L

WINDOW_MINUTES = 15
MIN_REQUESTS = 20            # fewer than this in the window: the rates are not judged
DEFAULT_ERROR_RATE = 0.05    # until the person sets a target
BAD_IN_A_ROW = 2
PROBE_TIMEOUT_S = 5.0


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _t(iso) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")) if iso else None
    except ValueError:
        return None


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, -(-len(s) * p // 100) - 1))]


def _ms(x: float) -> str:
    return f"{x:.0f} ms" if x >= 10 else f"{x:.1f} ms"


def prefix(pid: str, env: str) -> str:
    return f"staging/{pid}/{env}/health/"


# ---- the target a person sets -------------------------------------------------------------------------

def target(rec: dict | None) -> dict:
    """{p95_ms, error_rate, set}: what "healthy" means for this deployment. Until one is set, an error rate over 5%
    is unhealthy and response time is not judged."""
    t = (rec or {}).get("health_target") or {}
    return {"p95_ms": t.get("p95_ms"), "error_rate": t.get("error_rate") if t.get("error_rate") is not None else DEFAULT_ERROR_RATE,
            "set": bool(t)}


def set_target(ctx: Context, pid: str, p95_ms=None, error_rate=None, env: str = ENV) -> dict:
    """Set what healthy means: the response time 95 in 100 requests must stay under, in ms at the endpoint, and the
    share of requests that may fail (0.01 is 1%). Pass null to stop judging one of them."""
    rec = state(ctx, pid, env)
    if not rec:
        raise ValueError("Nothing is deployed, so there is no target to set.")
    out = {}
    if p95_ms is not None:
        try:
            v = float(p95_ms)
        except (TypeError, ValueError):
            raise ValueError("the response time target is a number of milliseconds, like 200") from None
        if not 1 <= v <= 60_000:
            raise ValueError("the response time target is between 1 ms and 60 seconds")
        out["p95_ms"] = v
    if error_rate is not None:
        try:
            v = float(error_rate)
        except (TypeError, ValueError):
            raise ValueError("the error rate target is a share between 0 and 1, like 0.01 for 1%") from None
        if not 0 <= v <= 1:
            raise ValueError("the error rate target is a share between 0 and 1, like 0.01 for 1%")
        out["error_rate"] = v
    _save(ctx, pid, env, health_target=out, health_target_by=_who(), health_target_at=_iso(_now()))
    return get(ctx, pid, env)


def _who() -> str:
    from ..workspace import whoami

    return whoami()


# ---- one check ----------------------------------------------------------------------------------------

def _deploying(ctx: Context, pid: str) -> bool:
    try:
        from . import deploy as DP

        return (DP.status(ctx, pid) or {}).get("status") == "running"
    except Exception:  # noqa: BLE001
        return False


def _asleep(ctx: Context, rec: dict) -> bool:
    if not rec.get("scale_to_zero"):
        return False
    from . import _awake

    return not _awake(ctx, rec)


def measure(ctx: Context, pid: str, env: str = ENV, window: int = WINDOW_MINUTES, now: dt.datetime | None = None,
            asked: bool = False) -> dict:
    """What can be said right now, with no judgement: is it answering, and the last minutes' requests, errors and
    response time. Never wakes a sleeping host."""
    now = now or _now()
    rec = state(ctx, pid, env)
    out = {"project": pid, "env": env, "checked_at": _iso(now), "window_minutes": window, "answering": None, "probe_error": None,
           "requests": 0, "errors": 0, "error_rate": None, "p50_ms": None, "p95_ms": None, "last_request_at": None,
           "fallbacks": 0, "asleep": False, "deploying": False}
    if not rec or rec.get("status") != "running":
        return {**out, "stopped": True}
    out["stopped"] = False
    out["deploying"] = _deploying(ctx, pid)
    if _asleep(ctx, rec):
        return {**out, "asleep": True}
    cloud = rec.get("kind") != "local"
    out["cloud"] = cloud
    if rec.get("how") != "image" and (not cloud or asked):
        try:
            # On this computer the model is asked on every check: nothing counts it. A model in a cloud is never
            # called by a check on a schedule: there every call counts as traffic, toward the customer's own
            # alarms and toward adding a copy. The cloud's own state says whether it is up, and the request log
            # says whether it answers well. Only "Check now", which a person presses, asks the model itself.
            st = (call(ctx, rec, "status", {}, timeout=PROBE_TIMEOUT_S) if not cloud
                  else ask_status(ctx, rec, max_age_s=0, timeout=PROBE_TIMEOUT_S))
            out["answering"] = bool(st.get("ok", True)) and not st.get("stopped")
            out["fallbacks"] = int(st.get("fallbacks") or 0)
            if not cloud:
                try:
                    call(ctx, rec, "flush", {}, timeout=PROBE_TIMEOUT_S)  # it is awake and answering: read a current log
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            out["answering"], out["probe_error"] = False, str(e)[:200]
    since = now - dt.timedelta(minutes=window)
    got = L.read(ctx, pid, env, rec["id"], extra=True)
    reqs = [r for r in got["requests"] if (_t(r.get("ts")) or since) >= since]
    errs = [x for x in got.get("lines") or [] if x.get("level") == "error" and (_t(x.get("ts")) or since) >= since]
    ms = [float(r["ms"]) for r in reqs if r.get("ms") is not None]
    total = len(reqs) + len(errs)  # a request that failed has a log line and no request record
    fell = sum(1 for r in reqs if r.get("fallback"))
    out["fallbacks"] = fell
    # A request the compared model had to answer is this model failing, though the site got an answer.
    out.update(requests=total, errors=len(errs), error_rate=round((len(errs) + fell) / total, 4) if total else None,
               p50_ms=_pct(ms, 50), p95_ms=_pct(ms, 95),
               last_request_at=max((str(r.get("ts")) for r in got["requests"]), default=None))
    return out


def judge(m: dict, tgt: dict) -> list[str]:
    """Why this measurement is not healthy, as sentences. Empty when it is."""
    if m.get("stopped") or m.get("asleep"):
        return []
    why = []
    if m.get("answering") is False:
        why.append("It is not answering." + (f" {m['probe_error']}" if m.get("probe_error") else ""))
    if m["requests"] >= MIN_REQUESTS:
        rate, limit = m.get("error_rate") or 0.0, tgt["error_rate"]
        if limit is not None and rate > limit:
            fell = int(m.get("fallbacks") or 0)
            why.append((f"{m['errors'] + fell:,} of its last {m['requests']:,} requests failed or were answered by the compared model "
                        f"because this one was slow or failed ({rate:.1%}), over the {limit:.0%} " if fell else
                        f"{m['errors']:,} of its last {m['requests']:,} requests failed ({rate:.1%}), over the {limit:.0%} ")
                       + f"{'target' if tgt['set'] else 'Captain allows until you set a target'}.")
        if tgt.get("p95_ms") is not None and m.get("p95_ms") is not None and m["p95_ms"] > tgt["p95_ms"]:
            why.append(f"95 in 100 requests took up to {_ms(m['p95_ms'])} at the endpoint, over the {_ms(tgt['p95_ms'])} target.")
    return why


def check(ctx: Context, pid: str, env: str = ENV, window: int = WINDOW_MINUTES, notify: bool = True, now: dt.datetime | None = None,
          asked: bool = False) -> dict:
    """Run one check, record it, and send a message if the status changed. Returns the health, with `changed` and
    `alert` (the message that went out, or None). `asked` is a person pressing "Check now": only then is a model
    in a cloud itself called. A check on a schedule reads the cloud's state and the request log."""
    now = now or _now()
    rec = state(ctx, pid, env)
    if not rec:
        return {"project": pid, "status": "none", "text": "Nothing is deployed.", "changed": False, "alert": None}
    before = rec.get("health") or {}
    m = measure(ctx, pid, env, window, now, asked)
    tgt = target(rec)
    reasons = judge(m, tgt)
    bad = (int(before.get("bad_in_a_row") or 0) + 1) if reasons else 0
    was = before.get("status")
    if m.get("stopped"):
        status = "stopped"
    elif m.get("asleep"):
        status = "asleep"
    elif m.get("deploying") and was != "unhealthy":
        status, bad = (was if was in ("healthy", "unknown") else "unknown"), 0  # a switch-over is not an outage
    elif reasons:
        status = "unhealthy" if bad >= BAD_IN_A_ROW or was == "unhealthy" else (was if was in ("healthy", "unknown") else "unknown")
    elif m.get("cloud") and m.get("answering") is None:
        # Up, by the cloud's own state. Healthy only when there are requests to judge by, and they are fine.
        status = "healthy" if m["requests"] >= MIN_REQUESTS else "unknown"
    else:
        status = "healthy" if m.get("answering") else "unknown"
    changed = status != was
    h = {"status": status, "since": _iso(now) if changed or not before.get("since") else before["since"], "checked_at": _iso(now),
         "answering": m.get("answering"), "last_request_at": m.get("last_request_at"), "window_minutes": window,
         "requests": m["requests"], "errors": m["errors"], "error_rate": m["error_rate"], "p50_ms": m["p50_ms"], "p95_ms": m["p95_ms"],
         "target": {"p95_ms": tgt["p95_ms"], "error_rate": tgt["error_rate"], "set": tgt["set"]}, "reasons": reasons,
         "bad_in_a_row": bad, "fallbacks": m.get("fallbacks", 0), "judged": m["requests"] >= MIN_REQUESTS,
         # A model in a cloud that the schedule did not call: it is running by the cloud's own state.
         "running_by_cloud": bool(m.get("cloud")) and m.get("answering") is None and status in ("healthy", "unknown") and not m.get("deploying")}
    h["sources"] = sources(rec, m, window)
    h["text"] = words(rec, h)
    _save(ctx, pid, env, health=h)
    try:
        L.shared(ctx, pid, env).put_new(f"{prefix(pid, env)}{now.strftime('%Y%m%dT%H%M%S%f')}-{new_id()[-4:].lower()}.json",
                                        json.dumps({**h, "stage": rec.get("id"), "model_version": rec.get("model_version")}).encode())
    except Exception:  # noqa: BLE001  the record holds the newest result even when the history can't be written
        pass
    alert = None
    # A message goes out when it turns unhealthy, and when it comes back from that. Not for asleep or stopped.
    if changed and (status == "unhealthy" or (was == "unhealthy" and status == "healthy")):
        alert = message(pid, rec, h)
        if status == "unhealthy":
            from . import rollback as RB

            back = RB.maybe(ctx, pid, h, env)
            if back:
                alert["rolled_back"] = back
                alert["text"] = f"{alert['text']} {back['text']}"
        if notify:
            alert["webhook"] = send_webhook(ctx, alert)
    return {**get(ctx, pid, env), "changed": changed, "was": was, "alert": alert}


def sources(rec: dict, m: dict, window: int) -> list[dict]:
    """Where each fact in a health came from, for the health panel: [{fact, from}]. A person should be able to
    tell what Captain looked at, and that it did not call a model in a cloud unless they asked."""
    cloud = rec.get("kind") != "local"
    place = {"aws": "AWS", "modal": "Modal", "gcp": "Google Cloud", "azure": "Azure"}.get(rec.get("host") or "", "the cloud")
    if m.get("stopped") or m.get("asleep"):
        return [{"fact": "Whether it is up", "from": f"{place}'s own state. The model was not called." if cloud else "This computer: is its process running."}]
    asked = m.get("answering") is not None
    up = ("Captain asked the model itself, just now." if not cloud else
          "Captain asked the model itself, because you pressed Check now." if asked else
          f"{place}'s own state. The model was not called.")
    return [{"fact": "Whether it is up", "from": up},
            {"fact": "How many requests failed, and how fast they were",
             "from": f"The request log: the requests it answered in the last {window} minutes."}]


def words(rec: dict, h: dict) -> str:
    """The health in one or two sentences, as the deployed screen and the project page say it."""
    s = h["status"]
    if s == "stopped":
        return "Stopped."
    if s == "asleep":
        return "Asleep. It wakes on the next request."
    if s == "unhealthy":
        return "Unhealthy. " + " ".join(h["reasons"])
    if s == "unknown" and h.get("running_by_cloud") and not h["reasons"]:
        n = h.get("requests") or 0
        return ("Running. No requests to judge by yet." if not n else
                f"Running. Only {n:,} request{'s' if n != 1 else ''} in the last {h['window_minutes']} minutes, too few to judge by yet.")
    if s == "unknown":
        return ("Not confirmed healthy yet. " + " ".join(h["reasons"])) if h["reasons"] else "Not checked yet."
    if not h.get("judged"):
        return (f"Healthy: it is answering. Only {h['requests']:,} request{'s' if h['requests'] != 1 else ''} in the last "
                f"{h['window_minutes']} minutes, too few to judge its error rate or response time.")
    t = h["target"]
    fast = f" 95 in 100 took under {_ms(h['p95_ms'])} at the endpoint" + (f" (target {_ms(t['p95_ms'])})." if t.get("p95_ms") is not None else ".") if h.get("p95_ms") is not None else ""
    return (f"Healthy. {h['errors']:,} of its last {h['requests']:,} requests failed"
            + (f" ({h['error_rate']:.1%})" if h["errors"] else "") + f", in the last {h['window_minutes']} minutes.{fast}")


def message(pid: str, rec: dict, h: dict) -> dict:
    """What an alert says: one line a person can read on a lock screen, and the facts for a machine."""
    name = str(rec.get("model_version") or pid).split("/")[-1]
    text = (f"{name} is unhealthy. " + " ".join(h["reasons"])) if h["status"] == "unhealthy" else f"{name} is healthy again."
    return {"project": pid, "model_version": rec.get("model_version"), "status": h["status"], "text": text, "reasons": h["reasons"],
            "at": h["checked_at"], "title": "Captain: a deployed model is unhealthy" if h["status"] == "unhealthy" else "Captain: a deployed model recovered"}


def send_webhook(ctx: Context, alert: dict) -> dict:
    """POST the alert as JSON to the workspace's webhook, when one is set. Never raises, and never logs the URL."""
    from .. import settings as ST

    url = ((ST.get(ctx).get("alerts") or {}).get("webhook_url") or "").strip()
    if not url:
        return {"sent": False, "why": "No webhook is set."}
    try:
        body = json.dumps({"text": alert["text"], **{k: alert[k] for k in ("project", "model_version", "status", "reasons", "at")}}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "captain-search"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310  http(s) only: checked when it was set
            return {"sent": 200 <= r.status < 300, "status": r.status}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "why": f"The webhook didn't accept it: {type(e).__name__}."}


def system_notification(alert: dict) -> dict:
    """Post the alert as a notification of the computer the service runs on (macOS: Notification Centre, with a
    sound). The words are handed over as arguments, never pasted into a script. Never raises."""
    import subprocess
    import sys

    if sys.platform != "darwin":
        return {"sent": False, "why": "System notifications are sent on macOS only."}
    script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv) sound name "Ping"\nend run'
    try:
        r = subprocess.run(["osascript", "-e", script, str(alert.get("title") or "Captain"), str(alert.get("text") or "")[:300]],
                           capture_output=True, timeout=10)
        return {"sent": r.returncode == 0}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "why": type(e).__name__}


# ---- reading it ---------------------------------------------------------------------------------------

def get(ctx: Context, pid: str, env: str = ENV) -> dict:
    """The newest health on the record, with the target and the sentence. Never runs a check."""
    rec = state(ctx, pid, env)
    if not rec:
        return {"project": pid, "status": "none", "text": "Nothing is deployed.", "target": target(None)}
    h = dict(rec.get("health") or {})
    if rec.get("status") != "running":
        h.update(status="stopped", text="Stopped.")
    elif not h:
        h = {"status": "unknown", "text": "Not checked yet.", "reasons": []}
    return {"project": pid, **h, "target": target(rec)}


def history(ctx: Context, pid: str, env: str = ENV, limit: int = 50) -> dict:
    """The recorded checks, newest first."""
    store = L.shared(ctx, pid, env)
    keys = sorted(store.list(prefix(pid, env)), reverse=True)[:max(1, min(500, int(limit)))]
    rows = []
    for k in keys:
        try:
            rows.append(json.loads(store.get(k)))
        except Exception:  # noqa: BLE001
            continue
    return {"project": pid, "rows": rows, "text": f"{len(rows)} recorded check{'s' if len(rows) != 1 else ''}." if rows else "No checks recorded yet."}


def watch_once(ctx: Context, notify: bool = True, system: bool = False) -> list[dict]:
    """Check every running deployment in the workspace once. What `cst deployments watch` and the service repeat.
    With `system`, a status change is also posted as a notification of this computer."""
    from .. import project_state as PS
    from .. import projects as PR

    out = []
    for pr in PR.rows(ctx):
        try:
            envs = (PS.read(ctx, pr["id"])[0].get("staging") or {})
        except Exception:  # noqa: BLE001
            continue
        for env, rec in envs.items():
            if rec and rec.get("status") == "running":
                try:
                    if rec.get("kind") != "local":
                        # With no page open, the record may be hours old: a model that stopped by itself would
                        # read as running. The cloud is asked for its state first, which is free and is not a
                        # call to the model. If it can't be asked the record stands as it is.
                        try:
                            from . import fleet as FL

                            FL.one(ctx, pr["id"], env)
                        except Exception:  # noqa: BLE001
                            pass
                    r = check(ctx, pr["id"], env, notify=notify)
                    if system and r.get("alert"):
                        r["alert"]["system"] = system_notification(r["alert"])
                    out.append(r)
                except Exception as e:  # noqa: BLE001  one deployment that can't be checked never stops the others
                    out.append({"project": pr["id"], "status": "unknown", "text": f"The check itself failed: {str(e)[:160]}", "changed": False, "alert": None})
    return out
