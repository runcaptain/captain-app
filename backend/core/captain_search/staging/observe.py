"""What a staged model is doing: its copies, its log, its settings, what it costs, and how it is performing.

These back the Copies, Logs, Settings, Cost and Metrics tabs of the deployment page, the chart in its header, the
Deployments list, and `cst deployments`. Everything here is read from what the endpoint wrote down (logs.py) and
from what it says about itself when asked, so a local endpoint and one in a cloud read the same way.

Three states are different things, and each is said plainly: stopped, up and idle, and handling requests. An
endpoint that is up with nothing to do is not a problem.

Response time is shown twice when it can be. "At the endpoint" is the time the ranking took inside it, which
Captain measures on every request. "As your site sees it" includes the network, and only the site can know it: the
site reports it (`client_ms` with an outcome, or the event `timing`). Measured on 2026-11-11 from a laptop to a
SageMaker endpoint in us-west-1: a median of 0.2 ms at the endpoint and 111 ms as the caller saw it.
"""
from __future__ import annotations

import datetime as dt

from ..workspace import Context
from . import ENV, _ago, _awake, _span, call, set_control, state, status
from . import logs as L

RANGES = {"15m": (15 * 60, "the last 15 minutes"), "1h": (3600, "the last hour"), "24h": (86400, "the last 24 hours"),
          "7d": (7 * 86400, "the last 7 days")}
BUCKETS = 30
IDLE_CHOICES = (15, 60, 240, 720, 1440)  # minutes an idle endpoint stays up before it stops by itself
HANDLING_S = 60  # an endpoint that answered within this many seconds is handling requests, not idle


def _t(iso: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")) if iso else None
    except ValueError:
        return None


def _pct(xs: list[float], p: float) -> float | None:
    """The p-th percentile by the nearest-rank rule: the smallest value with at least p% of the values at or under it."""
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, -(-len(s) * p // 100) - 1))]


def _ms(x: float | None) -> str:
    return "" if x is None else f"{x:.0f} ms" if x >= 10 else f"{x:.1f} ms"


def _flush(ctx: Context, rec: dict) -> None:
    if rec.get("status") == "running" and rec.get("how") != "image":
        if rec.get("scale_to_zero") and not _awake(ctx, rec):
            return  # asleep: nothing is buffered, and asking would wake it
        try:
            call(ctx, rec, "flush", {})
        except Exception:  # noqa: BLE001  a log a few seconds old is still a log
            pass


def _read(ctx: Context, pid: str, env: str, rec: dict, every_stage: bool = False) -> dict:
    _flush(ctx, rec)
    return L.read(ctx, pid, env, None if every_stage else rec["id"], extra=True)


def activity(rec: dict | None, health: dict | None, asleep: bool | None = None) -> dict:
    """Which state the endpoint is in: stopped, idle, or handling requests. On a host that sleeps when idle there
    is a fourth: asleep, with no copy up and nothing billing, until the next request wakes it."""
    if not rec or rec.get("status") != "running":
        return {"state": "stopped", "label": "Stopped", "text": "Stopped. Nothing is running and nothing is billing."}
    if asleep:
        return {"state": "asleep", "label": "Asleep", "text": "Asleep: no copy is up and nothing is billing. It wakes on the next request."}
    if not health and rec.get("scale_to_zero"):
        return {"state": "up", "label": "Up", "text": "Up. It sleeps when nobody calls it, and Captain didn't ask it how it is, so as not to wake it."}
    if not health:
        return {"state": "up", "label": "Up", "text": "Up."}
    if health.get("running", 0) > 0 or health.get("idle_s", 1e9) < HANDLING_S:
        n = health.get("running", 0)
        return {"state": "handling", "label": "Handling requests",
                "text": f"Handling requests: {n} right now." if n else "Handling requests."}
    return {"state": "idle", "label": "Idle",
            "text": f"Idle and ready: nothing has called it for {_span(health['idle_s'])}. It answers the moment a request "
                    + (f"arrives, and stops by itself in {_span(health['stops_in_s'])} if none does." if health.get("stops_in_s") is not None
                       else "arrives. If none does, its host puts it to sleep, and the next request wakes it.")}


def live(ctx: Context, pid: str, env: str = ENV) -> dict:
    """The small card that follows every tab: the state, the copies, and what is being answered right now."""
    s = status(ctx, pid, env, sweep=False)
    rec, h = state(ctx, pid, env), s.get("health")
    a = activity(rec if s.get("status") == "running" else None, h, asleep=s.get("asleep"))
    up = s.get("status") == "running"
    n = 0 if not up or s.get("asleep") else int(s.get("copies") or 1)
    return {"project": pid, **a, "copies": n, "running": (h or {}).get("running", 0) if up else 0,
            "requests": (h or {}).get("requests"), "errors": (h or {}).get("errors"),
            "line": f"Copies: {n} live. Requests: {(h or {}).get('running', 0) if up else 0} running."}


def copies(ctx: Context, pid: str, env: str = ENV) -> dict:
    """Each running copy of the endpoint: where, which version, when it started, how long it took to be ready, how
    many requests it has handled, and its state. Staging runs one copy."""
    from .traffic import where

    rec = state(ctx, pid, env)
    if not rec:
        return {"project": pid, "rows": [], "text": "Nothing is staged, so no copy is running."}
    s = status(ctx, pid, env, sweep=False)
    h = s.get("health")
    if s.get("status") != "running":
        return {"project": pid, "rows": [], "text": "No copy is running. " + (s.get("text") or "")}
    sleeps = bool(rec.get("scale_to_zero"))
    if sleeps and s.get("asleep"):
        return {"project": pid, "rows": [], "minimum": 0, "maximum": _scale(ctx, rec).get("max_copies") or 1,
                "text": "No copy is up: the endpoint is asleep, and nothing is billing. The next request wakes one."}
    a = activity(rec, h)
    row = {"copy": (h or {}).get("copy") or rec["id"][-8:], "where": where(rec)["text"].split(". ")[0] + ".",
           "machine": (rec.get("cost") or {}).get("instance_type") or ("this computer" if rec.get("kind") == "local" else None),
           "model_version": rec.get("model_version"), "created_at": rec.get("started_at"),
           "started_at": (h or {}).get("started_at"), "ready_s": (h or {}).get("ready_s"),
           "requests": (h or {}).get("requests"), "running": (h or {}).get("running"), "errors": (h or {}).get("errors"),
           "memory_mb": (h or {}).get("memory_mb"), "state": a["state"], "status": a["label"], "text": a["text"],
           "can_stop": True}
    if h and h.get("ready_s") is not None:
        row["ready_text"] = f"Ready to answer {h['ready_s']:g} s after it started."
    if sleeps:
        sc = _scale(ctx, rec)
        return {"project": pid, "rows": [row], "minimum": sc.get("min_copies") or 0, "maximum": sc.get("max_copies") or 1,
                "text": f"{s.get('copies') or 1} up now. " + (sc.get("text") or "") + " Stopping the endpoint removes it.",
                **({"warning": s["warning"]} if s.get("warning") else {})}
    return {"project": pid, "rows": [row], "minimum": 0, "maximum": 1,
            "text": "One copy is running. Staging runs one copy, and none once it has been idle for "
                    f"{_span(float(rec.get('idle_minutes') or 240) * 60)}. Stopping the copy stops the endpoint.",
            **({"warning": s["warning"]} if s.get("warning") else {})}


def _scale(ctx: Context, rec: dict) -> dict:
    """The fewest and most copies and how long an idle one stays, as the host has them. Read from the target and
    the record, with no call to the cloud."""
    from .. import compute as C

    try:
        return C.staged_scale(ctx, C.get(ctx, rec["target"]), rec)
    except Exception:  # noqa: BLE001
        return {}


def logs(ctx: Context, pid: str, env: str = ENV, q: str | None = None, limit: int = 300, request_id: str | None = None,
         since: str | None = None) -> dict:
    """The endpoint's log lines, oldest first, with times to the millisecond. `q` keeps lines containing it,
    `request_id` the lines of one request, `since` the lines after a time (for following the log as it grows)."""
    rec = state(ctx, pid, env)
    if not rec:
        return {"project": pid, "rows": [], "total": 0, "streaming": False, "text": "Nothing is staged, so there is no log."}
    if rec.get("how") == "image":
        return {"project": pid, "rows": [], "total": 0, "streaming": False,
                "text": "This endpoint runs your own container, so its log is where your container writes it."}
    lines = sorted(_read(ctx, pid, env, rec)["lines"], key=lambda x: str(x.get("ts")))
    total = len(lines)
    if request_id:
        lines = [x for x in lines if str(x.get("request_id")) == str(request_id)]
    if q and q.strip():
        needle = q.strip().lower()
        lines = [x for x in lines if needle in str(x.get("text", "")).lower() or needle in str(x.get("request_id", "")).lower()]
    if since:
        lines = [x for x in lines if str(x.get("ts")) > since]
    shown = lines[-max(1, min(2000, int(limit))):]
    streaming = rec.get("status") == "running"
    return {"project": pid, "stage": rec["id"], "rows": shown, "total": total, "matching": len(lines), "streaming": streaming,
            "last": shown[-1]["ts"] if shown else since,
            "note": "Lines from requests handled at the same time can interleave. Times are UTC, to the millisecond.",
            "text": (f"{len(lines):,} of {total:,} lines match." if (q and q.strip()) or request_id else
                     f"{total:,} line{'s' if total != 1 else ''} since this version was staged."
                     + (" New lines appear within about 15 seconds." if streaming else " The endpoint is stopped, so the log is complete."))
            if total else ("No lines yet." if streaming else "This version wrote no log lines.")}


def settings(ctx: Context, pid: str, env: str = ENV) -> dict:
    """How the endpoint is set up, in groups: where it runs, the machine, copies, stopping when idle, the time a
    site should wait, and the served-features check. Only the idle time can be changed while it runs."""
    from .traffic import TIMEOUT_MS, where

    rec = state(ctx, pid, env)
    if not rec:
        return {"project": pid, "groups": [], "text": "Nothing is staged."}
    c = rec.get("cost") or {}
    idle = float(rec.get("idle_minutes") or 240)
    local = rec.get("kind") == "local"
    chk = rec.get("check") or {}
    groups = [
        {"title": "Where it runs", "rows": [
            ["Runs", where(rec)["text"]],
            ["Compute", "This computer" if local else str(rec.get("target"))],
            ["Machine", "This computer's own processor and memory" if local else str(c.get("instance_type") or "Set by the compute target")],
            ["Serves", f"{rec.get('model_version')}" + (f", beside the baseline {rec['arms']['baseline']}" if (rec.get("arms") or {}).get("baseline") and rec.get("serve") != "baseline" else "")]]},
        {"title": "Copies", "rows": [
            ["Minimum", "None. It stops by itself when idle."], ["Maximum", "1"], ["Spare", "None"],
            ["Why", "Staging is for trying a model on a share of traffic, so it runs one copy. Production scaling is your own deployment's job."]]},
        {"title": "Stopping when idle", "edit": "idle", "rows": [
            ["Stops after", f"{_span(idle * 60)} with no requests"],
            ["Then", "The process ends." if local else "The endpoint deletes itself, so it stops billing."]]},
        {"title": "Time limits", "rows": [
            ["Your site should wait at most", f"{TIMEOUT_MS} ms, then show its own results"],
            ["Why", "A slow ranking must never hold up the page. The Connect your site code already does this."]]},
        {"title": "Served-features check", "rows": [
            ["Last result", chk.get("text") or "Not run yet. Run it from the Requests tab, or with cst projects staging-check."],
            ["Passes at", "99.9% of checked feature values equal to what training saw"]]},
    ]
    if rec.get("scale_to_zero"):  # the host decides when a copy sleeps, not an idle timer in the endpoint
        sc = _scale(ctx, rec)
        secs = sc.get("idle_seconds")
        groups[1] = {"title": "Copies", "rows": [
            ["Fewest", "None: it sleeps when nobody calls it." if not sc.get("min_copies") else f"{sc['min_copies']} kept up at all times"],
            ["Most", str(sc.get("max_copies") or 1)],
            ["Why", "Staging is for trying a model on a share of traffic. Production scaling is your own deployment's job."]]}
        groups[2] = {"title": "Sleeping when idle", "rows": [
            ["An idle copy stays up", f"{_span(secs)}" if secs else "As long as the host decides"],
            ["Then", "The host takes the copy away, and nothing bills. The next request starts one, which takes a few seconds."]]}
        return {"project": pid, "groups": groups, "idle_minutes": None, "idle_choices": [], "can_edit": False, "scale": sc,
                "text": "This host sleeps when idle, so there is no idle time to set here. To change the machine, the copies or where it runs, stage it again."}
    return {"project": pid, "groups": groups, "idle_minutes": idle, "idle_choices": list(IDLE_CHOICES),
            "can_edit": rec.get("status") == "running" and rec.get("how") != "image",
            "text": "Only the idle time can be changed while it runs. To change the machine or where it runs, stage it again."}


def set_idle(ctx: Context, pid: str, minutes: float, env: str = ENV) -> dict:
    """Change how long an idle endpoint stays up. Takes effect within a few seconds, with no restart."""
    from . import _save

    try:
        m = float(minutes)
    except (TypeError, ValueError):
        raise ValueError("the idle time is a number of minutes") from None
    if not 5 <= m <= 7 * 1440:
        raise ValueError("the idle time is between 5 minutes and 7 days")
    rec = state(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Nothing is staged, so there is nothing to change. The idle time is chosen when you stage.")
    if rec.get("scale_to_zero"):
        raise ValueError("This host puts the endpoint to sleep by itself when nobody calls it, so there is no idle time to set.")
    set_control(ctx, pid, env, idle_minutes=m)
    _save(ctx, pid, env, idle_minutes=m)
    try:
        call(ctx, rec, "reload", {})
    except Exception:  # noqa: BLE001  it re-reads its control every few seconds anyway
        pass
    return {**settings(ctx, pid, env), "saved": f"It now stops after {_span(m * 60)} with no requests."}


def cost(ctx: Context, pid: str, env: str = ENV) -> dict:
    """What this deployment has cost: so far this month, the last 24 hours, and the rate while it is up, by machine.
    Computed from how long each endpoint was up at the list price. The cloud's bill is the final word."""
    from .. import compute as C
    from .. import cost_estimates as CE

    rec = state(ctx, pid, env)
    if not rec:
        return {"project": pid, "free": True, "rows": [], "text": "Nothing is staged, so nothing is costing anything."}
    now = dt.datetime.now(dt.timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now - dt.timedelta(hours=24)
    spans = []  # (start, end, usd, machine, where)
    for d in CE.ledger(ctx, pid):
        a = d.get("actual") or {}
        if d.get("kind") != "staging" or a.get("usd") is None:
            continue
        end = _t(a.get("at"))
        if end:
            spans.append((end - dt.timedelta(seconds=float(a.get("seconds") or 0)), end, float(a["usd"]), a.get("machine"), d.get("where")))
    running = None
    if rec.get("status") == "running":
        up = _ago(rec.get("started_at")) or 0.0
        try:
            running = CE.cost_of(ctx, {"kind": "local"} if rec.get("kind") == "local" else C.get(ctx, rec["target"]), up, staging=True)
        except Exception:  # noqa: BLE001
            running = None
        if running and running.get("usd") is not None:
            spans.append((now - dt.timedelta(seconds=up), now, float(running["usd"]), running.get("machine"), running.get("where")))

    def within(start: dt.datetime) -> float:
        total = 0.0
        for a, b, usd, _m, _w in spans:
            length = (b - a).total_seconds()
            if b <= start:
                continue
            total += usd if a >= start or length <= 0 else usd * (b - start).total_seconds() / length
        return round(total, 4)

    by: dict[str, dict] = {}
    for a, b, usd, m, w in spans:
        if b > month_start:
            d = by.setdefault(m or w or "This computer", {"machine": m or w or "This computer", "usd": 0.0, "hours": 0.0})
            d["usd"] += usd
            d["hours"] += (b - max(a, month_start)).total_seconds() / 3600
    rows = [{"machine": d["machine"], "usd": round(d["usd"], 4), "hours": round(d["hours"], 2),
             "text": f"{d['machine']}: {CE.money(d['usd']) if d['usd'] >= 0.01 else 'under $0.01'} for {d['hours']:.1f} hours"} for d in by.values()]
    local_only = rec.get("kind") == "local" and not any(sp[2] for sp in spans)
    c = rec.get("cost") or {}
    hourly = (running or {}).get("hourly_usd") or c.get("hourly_usd")

    def say(x: float) -> str:
        return "$0.00" if x == 0 else "under $0.01" if x < 0.01 else CE.money(x)

    month, day = within(month_start), within(day_start)
    out = {"project": pid, "free": bool(local_only), "month_usd": month, "day_usd": day, "hourly_usd": None if local_only else hourly,
           "month_text": say(month), "day_text": say(day), "rows": rows, "running": rec.get("status") == "running",
           "rate_text": ("Runs on this computer. No cloud cost." if rec.get("kind") == "local" else
                         f"About ${hourly:.2f} an hour while a copy is awake, and nothing while it sleeps. What is shown is the most it can have cost." if hourly and rec.get("scale_to_zero") else
                         f"About ${hourly:.2f} an hour while it is up, about ${hourly * 24:.2f} a day." if hourly else
                         "Captain can't price this target.") if rec.get("status") == "running" else
                        "Stopped. It costs nothing now.",
           "estimate": (c.get("estimate") or {}).get("text") or c.get("text"),
           "note": "Worked out from how long each endpoint was up, at the list price. Your cloud's bill is the final word."}
    out["text"] = ("Runs on this computer. No cloud cost." if local_only else
                   f"{out['month_text']} so far this month, {out['day_text']} in the last 24 hours. {out['rate_text']}")
    return out


def metrics(ctx: Context, pid: str, env: str = ENV, span: str = "1h") -> dict:
    """Requests, response time (median and 95th percentile), errors and copies over one shared time range, in equal
    buckets, with a mark where each version was staged."""
    from .. import ops

    if span not in RANGES:
        raise ValueError("the time range is 15m, 1h, 24h or 7d")
    seconds, words = RANGES[span]
    rec = state(ctx, pid, env)
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(seconds=seconds)
    step = seconds / BUCKETS
    empty = {"project": pid, "range": span, "range_text": words, "ranges": list(RANGES), "buckets": [], "markers": [],
             "summary": {}, "start": start.isoformat(timespec="seconds"), "end": now.isoformat(timespec="seconds")}
    if not rec:
        return {**empty, "text": "Nothing is staged, so there is nothing to chart."}
    got = _read(ctx, pid, env, rec, every_stage=True)
    seen_by = {str(t["request_id"]): float(t["ms"]) for t in got["timings"]}
    b = [{"at": (start + dt.timedelta(seconds=step * i)).isoformat(timespec="seconds"), "requests": 0, "errors": 0,
          "_ms": [], "_site": []} for i in range(BUCKETS)]

    def slot(iso) -> int | None:
        t = _t(iso)
        if t is None or t < start or t > now:
            return None
        return min(BUCKETS - 1, int((t - start).total_seconds() // step))

    ms_all, site_all, kinds = [], [], {}
    for r in got["requests"]:
        i = slot(r.get("ts"))
        if i is None:
            continue
        b[i]["requests"] += 1
        kinds[r.get("traffic") or "unlabelled"] = kinds.get(r.get("traffic") or "unlabelled", 0) + 1
        if r.get("ms") is not None:
            b[i]["_ms"].append(float(r["ms"]))
            ms_all.append(float(r["ms"]))
        s = seen_by.get(str(r["request_id"]))
        if s is not None:
            b[i]["_site"].append(s)
            site_all.append(s)
    errors = 0
    for ln in got["lines"]:
        if ln.get("level") == "error":
            i = slot(ln.get("ts"))
            if i is not None:
                b[i]["errors"] += 1
                errors += 1
    for x in b:
        ms, site = x.pop("_ms"), x.pop("_site")
        x.update(p50=_pct(ms, 50), p95=_pct(ms, 95), site_p50=_pct(site, 50), site_p95=_pct(site, 95))
    markers = []
    try:
        for i, d in enumerate(sorted(ops.history(ctx, f"{pid}-{env}"), key=lambda d: d.created_at)):
            if d.created_at >= start:
                markers.append({"at": d.created_at.isoformat(timespec="seconds"), "n": i + 1, "model_version": d.pipeline,
                                "slot": slot(d.created_at.isoformat()), "text": f"Version {i + 1} staged: {d.pipeline}"})
    except Exception:  # noqa: BLE001
        pass
    total = sum(x["requests"] for x in b)
    h = status(ctx, pid, env, sweep=False).get("health") if rec.get("status") == "running" else None
    summary = {"requests": total, "errors": errors, "error_rate": round(errors / total, 4) if total else None,
               "p50": _pct(ms_all, 50), "p95": _pct(ms_all, 95), "site_p50": _pct(site_all, 50), "site_p95": _pct(site_all, 95),
               "site_reported": len(site_all), "traffic": kinds, "copies": 1 if rec.get("status") == "running" else 0,
               "copies_min": 0, "copies_max": 1, "memory_mb": (h or {}).get("memory_mb"), "waiting": (h or {}).get("running", 0)}
    summary["endpoint_text"] = (f"At the endpoint: median {_ms(summary['p50'])}, 95th percentile {_ms(summary['p95'])}."
                                if ms_all else "At the endpoint: no requests in this range.")
    summary["site_text"] = (f"As your site sees it: median {_ms(summary['site_p50'])}, 95th percentile {_ms(summary['site_p95'])}, "
                            f"from {len(site_all):,} request{'s' if len(site_all) != 1 else ''} your site timed."
                            if site_all else
                            "As your site sees it: not reported yet. The network adds to the time at the endpoint, and only "
                            "your site can measure it. Send client_ms with each outcome, as the Connect your site code does.")
    return {**empty, "buckets": b, "markers": markers, "summary": summary, "bucket_seconds": round(step, 1),
            "text": (f"{total:,} request{'s' if total != 1 else ''} in {words}"
                     + (f", {errors:,} error{'s' if errors != 1 else ''}" if errors else ", no errors") + ". " + summary["endpoint_text"])
            if total else f"No requests in {words}."}


def deployments(ctx: Context, which: str = "live") -> dict:
    """Every staged model across projects: who staged it and when, where it runs, its state, and its traffic over
    the last 24 hours. `which` is live or stopped. Stopped ones stay findable."""
    from .. import project_state as PS
    from .. import projects as PR
    from .traffic import where

    if which not in ("live", "stopped", "all"):
        raise ValueError("which is live, stopped or all")
    rows = []
    for pr in PR.listing(ctx):
        try:
            doc = PS.read(ctx, pr["id"])[0]
        except Exception:  # noqa: BLE001
            continue
        for env, rec in (doc.get("staging") or {}).items():
            if not rec or not rec.get("id"):
                continue
            s = status(ctx, pr["id"], env, sweep=False)
            rec = state(ctx, pr["id"], env) or rec  # looking may have found that it stopped by itself
            up = s.get("status") in ("running", "starting")
            if which != "all" and up != (which == "live"):
                continue
            h = s.get("health")
            a = activity(rec if up else None, h)
            try:
                m = metrics(ctx, pr["id"], env, "24h")
                series, errs, total = [x["requests"] for x in m["buckets"]], [x["errors"] for x in m["buckets"]], m["summary"].get("requests", 0)
            except Exception:  # noqa: BLE001
                series, errs, total = [], [], None
            when = rec.get("started_at") if up else rec.get("stopped_at") or rec.get("started_at")
            ago = _ago(when)
            rows.append({"project": pr["id"], "name": pr.get("name") or pr["id"], "env": env, "model_version": rec.get("model_version"),
                         "state": a["state"], "status": a["label"], "where": where(rec)["text"].split(". ")[0] + "." if up else
                         ("On this computer." if rec.get("kind") == "local" else f"On {rec.get('target')}."),
                         "kind": rec.get("kind"), "by": rec.get("started_by"), "since": when,
                         "since_text": (f"Staged by {rec.get('started_by') or 'someone'} {_span(ago)} ago" if up else
                                        f"Stopped {_span(ago)} ago" + (f": {rec['stop_reason']}" if rec.get("stop_reason") else ""))
                         if ago is not None else "",
                         "copies": 1 if up else 0, "running": (h or {}).get("running", 0) if up else 0,
                         "requests_24h": total, "series": series, "errors": errs,
                         "line": f"Copies: {1 if up else 0} live. Requests: {(h or {}).get('running', 0) if up else 0} running."})
    rows.sort(key=lambda r: str(r.get("since") or ""), reverse=True)
    n_live = sum(1 for r in rows if r["state"] != "stopped")
    return {"which": which, "rows": rows,
            "text": ("Nothing is staged in any project. Stage a model from a project's page to see it here." if not rows and which != "stopped"
                     else "Nothing has been stopped yet." if not rows else
                     f"{len(rows)} deployment{'s' if len(rows) != 1 else ''}" + (f", {n_live} live." if which == "all" else "."))}
