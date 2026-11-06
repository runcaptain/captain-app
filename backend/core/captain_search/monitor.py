"""Watch a deployed pipeline's live metrics, and say when they have dropped.

`record` reads an events dataset (one row per impression: query, product_id, position, clicked, and
optionally purchased, session_id, ts) where it lives, aggregates it in DuckDB, and appends a snapshot for
the environment's current pipeline. Big logs are never downloaded: DuckDB pages them from the bucket.

A search is one (session_id, query) pair, or (ts, query) when there is no session. Metrics:
  searches, impressions
  ctr          share of searches with at least one click
  conversion   share of searches with a purchase (when the logs have `purchased`)
  mrr_click    mean of 1 / position of the first click (0 when nothing was clicked)
  clicks_at_3  share of searches with a click in the top 3

`check` compares the latest snapshot with a baseline for the same pipeline (its first snapshot, its
best, or the previous one) and reports degraded when the metric fell by at least `drop` (relative).
Snapshots live in monitor/<env>.yaml, updated by compare-and-swap. These are LOGGED_CLICKS numbers.
"""
from __future__ import annotations

import datetime as dt

import yaml

from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami

METRICS = ("ctr", "conversion", "mrr_click", "clicks_at_3", "searches", "impressions")
REQUIRED = {"query", "product_id", "position", "clicked"}


def _key(env: str) -> str:
    return f"monitor/{env}.yaml"


def _read(ctx: Context, env: str):
    try:
        data, tag = ctx.store.get_with_tag(_key(env))
        return yaml.safe_load(data) or {"snapshots": []}, tag
    except NotFound:
        return {"snapshots": []}, None


def measure(ctx: Context, logs: str, since: str | None = None, where: str | None = None, profile: str | None = None) -> dict:
    from . import browse as B

    m, _ = ctx.registry.resolve("dataset", logs)
    ts = B.tables(ctx, m)
    if not ts:
        raise ValueError(f"{logs} has no file that reads as a table")
    t = ts[0]
    con = B._con(profile)
    src = B._reader(t["uri"], t["format"])
    cols = {c[0] for c in con.sql(f"describe select * from {src}").fetchall()}
    missing = REQUIRED - cols
    if missing:
        raise ValueError(f"{logs} needs columns {sorted(REQUIRED)} (plus optional purchased, session_id, ts); it lacks {sorted(missing)}")
    conds = []
    if since:
        if "ts" not in cols:
            raise ValueError(f"--since needs a ts column, and {logs} has none")
        conds.append("cast(ts as varchar) >= '" + since.replace("'", "''") + "'")
    if where:
        conds.append(f"({B._check_where(where)})")
    wsql = (" where " + " and ".join(conds)) if conds else ""
    key = ("coalesce(cast(session_id as varchar), '')" if "session_id" in cols else "coalesce(cast(ts as varchar), '')") \
        + " || '|' || lower(trim(cast(query as varchar)))"
    clicked = "(cast(clicked as integer) > 0)"
    bought = "(cast(purchased as integer) > 0)" if "purchased" in cols else "false"
    sql = f"""
      with s as (
        select {key} k, count(*) imp,
               max(case when {clicked} then 1 else 0 end) c,
               max(case when {bought} then 1 else 0 end) b,
               min(case when {clicked} then cast(position as integer) end) fc,
               max(case when {clicked} and cast(position as integer) <= 3 then 1 else 0 end) c3
        from {src}{wsql} group by 1)
      select count(*), sum(imp), avg(c), avg(b), avg(case when fc is null or fc < 1 then 0 else 1.0 / fc end), avg(c3) from s"""
    n, imp, ctr, conv, mrr, c3 = con.sql(sql).fetchall()[0]
    if not n:
        raise ValueError(f"no searches in {logs}" + (f" since {since}" if since else ""))
    r = lambda x: round(float(x or 0), 5)
    return {"logs": f"{m.name}@v{m.version}", "metrics": {"searches": int(n), "impressions": int(imp or 0), "ctr": r(ctr),
            "conversion": r(conv) if "purchased" in cols else None, "mrr_click": r(mrr), "clicks_at_3": r(c3)}}


def record(ctx: Context, env: str, logs: str, since: str | None = None, where: str | None = None,
           pipeline: str | None = None, dry_run: bool = False) -> dict:
    from .ops import current, env_load

    env_load(ctx, env)  # must exist
    cur = current(ctx, env)
    served = pipeline or (cur["pipeline"] if cur else None)
    if not served:
        raise ValueError(f"nothing is deployed to {env}. Promote a pipeline first, or name the one these logs came from with --pipeline.")
    meas = measure(ctx, logs, since, where)
    snap = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "by": whoami(), "pipeline": served,
            "logs": meas["logs"], "since": since, "where": where, "metrics": meas["metrics"]}
    if dry_run:
        return {"env": env, "would_record": snap}
    doc, tag = _read(ctx, env)
    doc.setdefault("snapshots", []).append(snap)
    try:
        ctx.store.put_if(_key(env), dump_yaml(doc), tag)
    except Conflict:
        raise Conflict("someone recorded a snapshot for this environment at the same moment. Try again.") from None
    return {"env": env, **snap}


def show(ctx: Context, env: str) -> dict:
    doc, _ = _read(ctx, env)
    return {"env": env, "snapshots": doc.get("snapshots", [])}


def check(ctx: Context, env: str, metric: str = "ctr", drop: float = 0.05, baseline: str = "first") -> dict:
    if metric not in METRICS[:4]:
        raise ValueError(f"metric is one of {', '.join(METRICS[:4])}")
    if baseline not in ("first", "best", "previous"):
        raise ValueError("baseline is first, best or previous")
    if not 0 < drop < 1:
        raise ValueError("drop is a share between 0 and 1, for example 0.05 for a 5% fall")
    snaps = show(ctx, env)["snapshots"]
    out = {"env": env, "metric": metric, "drop_threshold": drop, "baseline_kind": baseline, "degraded": False,
           "pipeline": None, "current": None, "baseline": None, "drop": None, "snapshots": 0}
    if not snaps:
        return {**out, "reason": f"no snapshots for {env} yet. Run `cst monitor record` first."}
    last = snaps[-1]
    mine = [s for s in snaps if s["pipeline"] == last["pipeline"] and s["metrics"].get(metric) is not None]
    out.update(pipeline=last["pipeline"], snapshots=len(mine), current=last["metrics"].get(metric))
    if len(mine) < 2 or out["current"] is None:
        return {**out, "reason": f"{last['pipeline']} has {len(mine)} snapshot(s) with {metric}. It needs 2 to compare."}
    prior = mine[:-1]
    base = {"first": prior[0], "previous": prior[-1], "best": max(prior, key=lambda s: s["metrics"][metric])}[baseline]
    b = base["metrics"][metric]
    fall = (b - out["current"]) / b if b else 0.0
    out.update(baseline=b, baseline_at=base["at"], drop=round(fall, 5), degraded=fall >= drop)
    out["reason"] = (f"{metric} fell {fall:.1%} from {b:.4f} to {out['current']:.4f}, at or past the {drop:.0%} threshold"
                     if out["degraded"] else f"{metric} moved {-fall:+.1%} from {b:.4f} to {out['current']:.4f}, inside the {drop:.0%} threshold")
    return out
