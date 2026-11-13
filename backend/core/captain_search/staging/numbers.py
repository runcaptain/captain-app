"""Traffic and numbers for one deployment, from the cloud's own metrics, with the request log as the second source.

For each deployment: requests a minute, errors, response time at the endpoint (median and 95th percentile), CPU
and memory, the copies that were up, and what it has cost today and this month with the hourly rate.

Where each number comes from, by host:

  aws     CloudWatch, for everything. One read-only call per look. Cost is the hours each copy was up (CloudWatch
          hears from every copy once a minute) at the list price for its machine.
  modal   Modal gives no history of an app's requests to read, so requests, errors and response time come from
          Captain's request log, and the copies up now from Modal. CPU, memory and cost so far: not read yet.
  gcp     Not read yet. The request log, when there is one.
  azure   Not read yet. The request log, when there is one.
  local   The request log.

What is not read is said in a sentence (`not_read`), never shown as a zero.

A cloud's numbers arrive a minute or two late and are counted by the minute, so asking more often than that only
costs money: CloudWatch charges about a cent for a thousand metrics read. The answer for a deployment and range is
kept for a minute, and the month's cost for ten.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from ..workspace import Context
from . import ENV, state

RANGES = {"15m": (15 * 60, 60, "the last 15 minutes"), "1h": (3600, 60, "the last hour"),
          "24h": (86400, 300, "the last 24 hours"), "7d": (7 * 86400, 3600, "the last 7 days")}
KEEP_S = 60.0
KEEP_COST_S = 600.0
NOT_READ = {
    "modal": "Modal keeps no history of an app's requests that Captain can read. Requests, errors and response time come from the "
             "request log. CPU, memory and the cost so far are not read yet.",
    "gcp": "Captain does not read Google Cloud's own metrics yet. Requests, errors and response time come from the request log.",
    "azure": "Captain does not read Azure's own metrics yet. Requests, errors and response time come from the request log.",
}
_kept: dict = {}
_lock = threading.Lock()


def forget() -> None:
    with _lock:
        _kept.clear()


def _keep(key, seconds: float, make):
    with _lock:
        hit = _kept.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
    got = make()
    with _lock:
        _kept[key] = (time.monotonic(), got)
    return got


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _money(x: float) -> str:
    from ..cost_estimates import money

    return money(x) if x >= 0.01 else "under $0.01"


def _pct(values: list[float], p: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    return v[min(len(v) - 1, int(p / 100 * len(v)))] if v else None


def _where(ctx: Context, rec: dict):
    """(cloud, compute target, provider) of a record."""
    from .. import compute as C
    from .. import compute_providers as P
    from .fleet import _where_of

    cloud, _login, _region, target = _where_of(ctx, rec)
    if cloud == "local" or target is None:
        return cloud, target, None
    try:
        return cloud, target, P.get(target, ctx)
    except Exception:  # noqa: BLE001
        return cloud, C.get(ctx, rec["target"]), None


def _hourly(ctx: Context, cloud: str, target: dict | None, rec: dict, machine: str | None) -> float | None:
    """The list price of one copy for an hour."""
    from . import sizing as SZ

    if cloud == "aws":
        name = machine or ((target or {}).get("config") or {}).get("instance_type")
        hit = next((s for s in SZ.sizes("aws", (target or {}).get("region")) if s["name"] == name), None)
        if hit:
            return float(hit["hourly_usd"])
    c = rec.get("cost") or {}
    return float(c["hourly_usd"]) if c.get("hourly_usd") is not None else None


def _from_log(ctx: Context, pid: str, env: str, span: str) -> dict | None:
    """Requests, errors and response time from Captain's request log, in the request log's own buckets."""
    from . import observe as OB

    try:
        return OB.metrics(ctx, pid, env, span)
    except Exception:  # noqa: BLE001
        return None


def numbers(ctx: Context, pid: str, env: str = ENV, span: str = "1h") -> dict:
    """{range, source, source_text, buckets, summary, cost, not_read, text} for one deployment. Each bucket:
    {t, requests, errors, p50_ms, p95_ms, cpu, memory, copies}, with None where a number is not known."""
    if span not in RANGES:
        raise ValueError("the time range is 15m, 1h, 24h or 7d")
    rec = state(ctx, pid, env)
    seconds, period, words = RANGES[span]
    base = {"project": pid, "env": env, "range": span, "range_text": words, "ranges": list(RANGES), "bucket_seconds": period,
            "buckets": [], "summary": {}, "not_read": [], "cost": None}
    if not rec or not rec.get("id"):
        return {**base, "source": "none", "source_text": "Nothing is deployed.", "text": "Nothing is deployed, so there is nothing to count."}
    return _keep((ctx.workspace.name, pid, env, span, rec.get("id")), KEEP_S, lambda: _numbers(ctx, pid, env, span, rec, base))


def _numbers(ctx: Context, pid: str, env: str, span: str, rec: dict, base: dict) -> dict:
    seconds, period, words = RANGES[span]
    cloud, target, prov = _where(ctx, rec)
    name = (rec.get("endpoint") or {}).get("name")
    end = _now().replace(second=0, microsecond=0)
    start = end - dt.timedelta(seconds=seconds)
    out = dict(base)
    got = None
    if cloud == "aws" and prov is not None and name and callable(getattr(prov, "metrics", None)):
        try:
            got = prov.metrics(name, start, end, period)
        except Exception as e:  # noqa: BLE001  fall back to the log, and say why
            out["not_read"].append(f"Captain couldn't read AWS's own metrics just now ({str(e)[:160]}). These numbers are from the request log.")
    if got is not None:
        out.update(_from_cloud(got, start, end, period))
        out["source"], out["source_text"] = "cloud", "From AWS's own metrics (CloudWatch). They arrive a minute or two late."
    else:
        log = _from_log(ctx, pid, env, span)
        if cloud in NOT_READ:
            out["not_read"].append(NOT_READ[cloud])
        if log and log.get("buckets"):
            step = float(log.get("bucket_seconds") or seconds / max(1, len(log["buckets"])))
            out["bucket_seconds"] = step
            out["buckets"] = [{"t": b.get("t") or b.get("start"), "requests": b.get("requests"), "errors": b.get("errors"),
                               "p50_ms": b.get("p50"), "p95_ms": b.get("p95"), "cpu": None, "memory": None, "copies": b.get("copies")}
                              for b in log["buckets"]]
            s = log.get("summary") or {}
            total = int(s.get("requests") or 0)
            errs = int(s.get("errors") or 0)
            per_min = [(b.get("requests") or 0) * 60.0 / step for b in log["buckets"]]
            out["summary"] = {"requests": total, "errors": errs, "error_rate": (errs / total) if total else None,
                              "per_minute": round(total * 60.0 / seconds, 2), "per_minute_peak": round(max(per_min), 2) if per_min else None,
                              "p50_ms": s.get("p50"), "p95_ms": s.get("p95"), "cpu": None, "cpu_peak": None, "memory": None, "memory_peak": None,
                              "copies_now": None, "copies_peak": None}
            out["source"], out["source_text"] = "log", "From Captain's request log, which the model writes every few seconds."
        else:
            out["source"], out["source_text"] = "none", "No numbers yet: nothing has been read from the cloud or the request log."
    out["cost"] = cost(ctx, pid, env, rec)
    s = out["summary"]
    if s.get("requests"):
        out["text"] = (f"{s['requests']:,} request{'s' if s['requests'] != 1 else ''} in {words}"
                       + (f", {s['errors']:,} error{'s' if s['errors'] != 1 else ''}" if s.get("errors") else ", no errors")
                       + (f". Median {s['p50_ms']:.1f} ms, 95th percentile {s['p95_ms']:.1f} ms at the endpoint." if s.get("p50_ms") is not None and s.get("p95_ms") is not None else "."))
    else:
        out["text"] = f"No requests in {words}."
    return out


def _from_cloud(got: dict, start: dt.datetime, end: dt.datetime, period: int) -> dict:
    """CloudWatch's series, by time, as equal buckets and a summary."""
    times = []
    t = start
    while t < end:
        times.append(t.isoformat(timespec="seconds"))
        t += dt.timedelta(seconds=period)
    g = lambda k, ts: (got.get(k) or {}).get(ts)  # noqa: E731
    minutes = period / 60.0
    buckets = []
    for ts in times:
        req = g("requests", ts)
        e4, e5 = g("errors_4xx", ts), g("errors_5xx", ts)
        rep = g("reports", ts)
        buckets.append({"t": ts, "requests": int(req) if req is not None else 0,
                        "errors": int((e4 or 0) + (e5 or 0)),
                        "p50_ms": round(g("p50_us", ts) / 1000.0, 3) if g("p50_us", ts) is not None else None,
                        "p95_ms": round(g("p95_us", ts) / 1000.0, 3) if g("p95_us", ts) is not None else None,
                        "cpu": round(g("cpu", ts), 1) if g("cpu", ts) is not None else None,
                        "memory": round(g("memory", ts), 1) if g("memory", ts) is not None else None,
                        "copies": round(rep / minutes) if rep else None})
    total = sum(b["requests"] for b in buckets)
    errs = sum(b["errors"] for b in buckets)
    # A percentile of percentiles is not a percentile. The median over the range is the median bucket weighted by
    # its requests, which is the honest thing a minute-by-minute source allows. The 95th is the worst bucket's 95th
    # that carried at least a twentieth of the requests, so one slow minute with one request does not set it.
    weighted = sorted(((b["p50_ms"], b["requests"]) for b in buckets if b["p50_ms"] is not None and b["requests"]), key=lambda x: x[0])
    p50 = None
    if weighted:
        half, run = sum(w for _, w in weighted) / 2.0, 0.0
        for v, w in weighted:
            run += w
            if run >= half:
                p50 = v
                break
    heavy = [b["p95_ms"] for b in buckets if b["p95_ms"] is not None and b["requests"] >= max(1, total / 20.0 / max(1, len(buckets)))]
    cpus = [b["cpu"] for b in buckets if b["cpu"] is not None]
    mems = [b["memory"] for b in buckets if b["memory"] is not None]
    copies = [b["copies"] for b in buckets if b["copies"]]
    span_minutes = max(1.0, (end - start).total_seconds() / 60.0)
    return {"buckets": buckets,
            "summary": {"requests": total, "errors": errs, "error_rate": (errs / total) if total else None,
                        "per_minute": round(total / span_minutes, 2),
                        "per_minute_peak": round(max((b["requests"] / minutes for b in buckets), default=0.0), 2),
                        "p50_ms": p50, "p95_ms": max(heavy) if heavy else None,
                        "cpu": round(sum(cpus) / len(cpus), 1) if cpus else None, "cpu_peak": max((g("cpu_max", ts) or 0 for ts in times), default=None) if cpus else None,
                        "memory": round(sum(mems) / len(mems), 1) if mems else None,
                        "memory_peak": max((g("memory_max", ts) or 0 for ts in times), default=None) if mems else None,
                        "copies_now": copies[-1] if copies else None, "copies_peak": max(copies) if copies else None,
                        "cpu_text": "CPU is a share of one core: 100% is one core in full use.",
                        "p95_text": "The 95th percentile is the slowest minute's, among minutes that carried real traffic."}}


def cost(ctx: Context, pid: str, env: str = ENV, rec: dict | None = None) -> dict:
    """{hourly_usd, today_usd, month_usd, copy_hours_today, copy_hours_month, source, text}: the rate now, and what
    it has cost today and this month (UTC). On AWS from the hours each copy was up, as CloudWatch saw them, at the
    list price. Elsewhere from Captain's own record of when the deployment was up. Never the cloud's bill."""
    rec = rec or state(ctx, pid, env) or {}
    return _keep((ctx.workspace.name, pid, env, "cost", rec.get("id")), KEEP_COST_S, lambda: _cost(ctx, pid, env, rec))


def _cost(ctx: Context, pid: str, env: str, rec: dict) -> dict:
    from . import observe as OB

    cloud, target, prov = _where(ctx, rec)
    name = (rec.get("endpoint") or {}).get("name")
    now = _now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    if cloud == "aws" and prov is not None and name and callable(getattr(prov, "metrics", None)):
        try:
            got = prov.metrics(name, month, now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1), 3600, only=("reports",))
            reports = got.get("reports") or {}
            # One report a minute from each copy: 60 reports are one copy up for one hour.
            hours_month = sum(reports.values()) / 60.0
            hours_today = sum(v for t, v in reports.items() if t >= day.isoformat(timespec="seconds")) / 60.0
            machine = ((target or {}).get("config") or {}).get("instance_type")
            hourly = _hourly(ctx, "aws", target, rec, machine)
            if hourly is not None:
                up = rec.get("status") == "running"
                return {"hourly_usd": round(hourly, 4) if up else 0.0, "rate_usd": round(hourly, 4), "today_usd": round(hours_today * hourly, 4),
                        "month_usd": round(hours_month * hourly, 4), "copy_hours_today": round(hours_today, 2), "copy_hours_month": round(hours_month, 2),
                        "machine": machine, "source": "cloud",
                        "text": (f"About ${hourly:.2f} an hour for each copy while it is up. " if up else "Stopped, so it costs nothing now. ")
                                + f"Today: {_money(hours_today * hourly)}. This month: {_money(hours_month * hourly)}. "
                                  f"Worked out from the {hours_month:.1f} hours AWS saw a copy up this month, at the list price for {machine}. "
                                  "Your AWS bill is the final word."}
        except Exception:  # noqa: BLE001  fall through to Captain's own record
            pass
    try:
        c = OB.cost(ctx, pid, env)
    except Exception:  # noqa: BLE001
        c = {}
    hourly = c.get("hourly_usd") if c.get("hourly_usd") is not None else (rec.get("cost") or {}).get("hourly_usd")
    return {"hourly_usd": hourly, "rate_usd": hourly, "today_usd": c.get("day_usd", c.get("last_24h_usd")), "month_usd": c.get("month_usd"),
            "copy_hours_today": None, "copy_hours_month": None, "machine": None, "source": "record",
            "text": (c.get("text") or "Captain has no cost for this deployment yet.")
                    + ("" if cloud in ("local", "aws") else " Worked out from Captain's own record of when it was up, not from the cloud.")}
