"""Off-policy replay over logs with a randomized holdout: sweep a policy threshold, forecast each outcome.

A standard way to decide when to call an expensive retrieval source: requests
are randomly assigned to treat or not, a model scored each request, and an offline replay swept the score
threshold to forecast every operating point before any online test.

Logs: one row per request with a model score, the logged action (1 treated, 0 not), the probability the
logging policy treated it (the propensity, 0.5 for a 50/50 holdout when there is no column), and outcomes.
For the policy "treat when score >= t", each row whose logged action matches the policy counts, weighted
by 1 / P(logged action). Per threshold:
  trigger_rate   share of requests the policy would treat
  ips            inverse-propensity estimate of the mean outcome, with a standard error
  snips          self-normalised version (lower variance, small bias)
  delta_vs_never snips minus the never-treat policy
Everything runs as one DuckDB query over the logs where they live.

Replay is accurate for outcomes the policy controls directly (trigger rate, impressions of
the treated source) and only directional for downstream engagement. Outcomes named in `direct` are marked
direct. All others are marked directional, and a launch decision still needs an online A/B test.
"""
from __future__ import annotations

from .model.entities import Evidence, ReplayRun
from .storage.registry import check_name
from .workspace import Context, whoami


def _ident(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def sweep(ctx: Context, name: str, logs: str, score_col: str, action_col: str, outcomes: list[str],
          propensity_col: str | None = None, direct: list[str] | None = None, points: int = 20,
          dry_run: bool = False) -> ReplayRun:
    from . import browse as B

    if not outcomes:
        raise ValueError("name at least one outcome column")
    if not 2 <= points <= 200:
        raise ValueError("points is between 2 and 200")
    lm, _ = ctx.registry.resolve("dataset", logs)
    ts = B.tables(ctx, lm)
    if not ts:
        raise ValueError(f"{logs} has no file that reads as a table")
    con = B._con(None)
    src = B._reader(ts[0]["uri"], ts[0]["format"])
    cols = {c[0] for c in con.sql(f"describe select * from {src}").fetchall()}
    for c in [score_col, action_col, *outcomes] + ([propensity_col] if propensity_col else []):
        if c not in cols:
            raise ValueError(f"{logs} has no column {c}")
    direct = [d for d in (direct or []) if d in outcomes]
    s, a = _ident(score_col), f"(cast({_ident(action_col)} as integer) > 0)"
    p = f"cast({_ident(propensity_col)} as double)" if propensity_col else "0.5"
    bad = con.sql(f"select count(*) from {src} where {p} <= 0 or {p} >= 1").fetchall()[0][0]
    if bad:
        raise ValueError(f"{bad} rows have a propensity of 0 or 1, so the replay cannot reweight them. "
                         "Every request needs a real chance of either action.")
    qs = ", ".join(str(i / (points - 1)) for i in range(points))
    thresholds = [r[0] for r in con.sql(f"select unnest(quantile_cont({s}, [{qs}])) from {src}").fetchall()]
    thresholds = sorted(set(round(float(t), 6) for t in thresholds))
    tl = ", ".join(f"({t})" for t in thresholds)
    w = f"(case when ({s} >= th.t) = {a} then 1.0 / (case when {a} then {p} else 1 - {p} end) else 0 end)"
    w_never = f"(case when not {a} then 1.0 / (1 - {p}) else 0 end)"
    parts = [f"avg(case when {s} >= th.t then 1.0 else 0 end) as trig", f"count(*) as n",
             f"sum(case when {w} > 0 then 1 else 0 end) as matched", f"sum({w}) as sw"]
    for i, y in enumerate(outcomes):
        yv = f"cast({_ident(y)} as double)"
        parts += [f"avg({w} * {yv}) as ips{i}", f"stddev_samp({w} * {yv}) as sd{i}", f"sum({w} * {yv}) as swy{i}"]
    sql = f"select th.t, {', '.join(parts)} from {src} cross join (values {tl}) as th(t) group by th.t order by th.t"
    never = con.sql("select " + ", ".join(
        f"sum({w_never} * cast({_ident(y)} as double)) / nullif(sum({w_never}), 0)" for y in outcomes) + f" from {src}").fetchall()[0]
    curve = []
    for row in con.sql(sql).fetchall():
        t, trig, n, matched, sw = row[:5]
        out = {}
        for i, y in enumerate(outcomes):
            ips, sd, swy = row[5 + 3 * i: 8 + 3 * i]
            snips = (swy / sw) if sw else None
            out[y] = {"ips": round(float(ips), 6), "se": round(float(sd) / (n ** 0.5), 6) if sd is not None else None,
                      "snips": None if snips is None else round(float(snips), 6),
                      "delta_vs_never": None if snips is None or never[i] is None else round(float(snips - never[i]), 6),
                      "kind": "direct" if y in direct else "directional"}
        curve.append({"threshold": float(t), "trigger_rate": round(float(trig), 5), "n": int(n), "matched": int(matched),
                      "outcomes": out})
    note = ("Replay over a randomized holdout. Outcomes marked directional show which way they move, not by how much. "
            "Confirm the chosen threshold with an online A/B test.")
    m = ReplayRun(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=check_name(name), version=0,
                  logs=f"{lm.name}@v{lm.version}", score_col=score_col, action_col=action_col, propensity_col=propensity_col,
                  outcomes=outcomes, direct=direct, curve=curve, evidence=Evidence.LOGGED_CLICKS, note=note,
                  tags={"synthetic": "true"} if str((lm.tags or {}).get("synthetic", "")).lower() == "true" else {},
                  description=f"{len(curve)} operating points over {curve[0]['n'] if curve else 0:,} requests")
    if m.tags.get("synthetic") == "true":
        m = m.model_copy(update={"evidence": Evidence.SIMULATED})
    return m if dry_run else ctx.registry.publish(m)


def make_demo_logs(n: int = 20000, seed: int = 3) -> list[dict]:
    """Synthetic randomized-holdout logs for the demo: a 50/50 trigger, a score that predicts uplift."""
    import random

    rng = random.Random(seed)
    out = []
    for i in range(n):
        uplift = rng.betavariate(2, 5)  # hidden: how much this request gains from the expensive source
        score = min(1.0, max(0.0, uplift + rng.gauss(0, 0.08)))
        treated = rng.random() < 0.5
        base = 0.30
        engaged = rng.random() < base + (uplift - 0.22 if treated else 0)
        out.append({"request_id": f"r{i:06d}", "score": round(score, 4), "treated": int(treated), "propensity": 0.5,
                    "engaged": int(engaged), "shopping_impressions": (rng.randint(3, 8) if treated else 0)})
    return out
