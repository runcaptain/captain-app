"""Break a backtest down by segment, and score extra surfaces with guardrails.

A top-line nDCG can hide a segment that moves the other way. Two kinds of segment:
  class    the query's `class` column (failure class for generated queries, `logged` for log replays)
  traffic  head, torso and tail: the most-searched queries covering the first 50% of traffic, the next
           30%, and the last 20%. Needs per-query weights (logged query sets have them).
Each segment gets each arm's mean nDCG@10 and judged@10, and a paired comparison against the baseline
arm. Segments with fewer than MIN_N queries report numbers but no verdict.

Surfaces: one pipeline often serves several places (search, related items, notifications). A backtest can
name extra surfaces, each with its own queries and judgments, and guardrails that must all hold:
  {metric: ndcg@10, min: 0.40}        the arm scores at least 0.40 on this surface
  {metric: ndcg@10, max_drop: 0.01}   the arm falls at most 0.01 below the baseline arm on this surface
"""
from __future__ import annotations

from . import extensions as X
from .eval import metrics as M
from .eval.stats import compare

MIN_N = 5
BANDS = (("head", 0.5), ("torso", 0.8), ("tail", 1.0))


def traffic_bands(qs: list[dict]) -> dict[str, str]:
    w = {q["query_id"]: float(q["weight"]) for q in qs if q.get("weight") is not None}
    if not w:
        return {}
    total = sum(w.values()) or 1.0
    out, cum = {}, 0.0
    for qid, x in sorted(w.items(), key=lambda kv: (-kv[1], kv[0])):
        start = cum / total  # where this query's traffic begins: the top query is always head
        cum += x
        out[qid] = next(b for b, edge in BANDS if start < edge)
    return out


def _seg_rows(kind: str, groups: dict[str, list[str]], perq: dict[str, dict], arms: list[str], mde: float) -> list[dict]:
    base = arms[0]
    rows = []
    order = {b: i for i, (b, _) in enumerate(BANDS)}
    for seg, qids in sorted(groups.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0])):
        for a in arms:
            vals = [perq[a][q] for q in qids if q in perq[a]]
            n = len(vals)
            row = {"kind": kind, "segment": seg, "arm": a, "n": n,
                   "ndcg@10": round(sum(v["ndcg10"] for v in vals) / n, 4) if n else None,
                   "judged@10": round(sum(v["judged10"] or 0 for v in vals) / n, 4) if n else None,
                   "delta": None, "ci_low": None, "ci_high": None, "verdict": None}
            if a != base and n:
                c = compare({q: perq[base][q]["ndcg10"] for q in qids if q in perq[base]},
                            {q: perq[a][q]["ndcg10"] for q in qids if q in perq[a]}, "ndcg@10", mde, arm_a=base, arm_b=a)
                row.update(delta=c["delta"], ci_low=c["ci_low"], ci_high=c["ci_high"],
                           verdict=c["verdict"] if n >= MIN_N else "too few")
            rows.append(row)
    return rows


def segments(qs: list[dict], perq: dict[str, dict], arms: list[str], mde: float) -> list[dict]:
    """perq: arm -> query_id -> per-query metrics (from metrics.per_query)."""
    if not perq or not any(perq.values()):
        return []
    out = []
    classes: dict[str, list[str]] = {}
    for q in qs:
        if q.get("class"):
            classes.setdefault(str(q["class"]), []).append(q["query_id"])
    if len(classes) > 1:
        out += _seg_rows("class", classes, perq, arms, mde)
    bands = traffic_bands(qs)
    if bands:
        groups: dict[str, list[str]] = {}
        for qid, b in bands.items():
            groups.setdefault(b, []).append(qid)
        out += _seg_rows("traffic", groups, perq, arms, mde)
    return out


def check_guardrails(g_list, arm: str, base: str, metrics: dict[str, dict]) -> list[dict]:
    out = []
    for g in g_list:
        v = metrics.get(arm, {}).get(g.metric)
        if v is None:
            out.append({"arm": arm, "metric": g.metric, "rule": "unknown metric", "value": None, "ok": False})
            continue
        if g.min is not None:
            out.append({"arm": arm, "metric": g.metric, "rule": f"at least {g.min}", "value": v, "ok": v >= g.min})
        if g.max_drop is not None:
            b = metrics.get(base, {}).get(g.metric, v)
            out.append({"arm": arm, "metric": g.metric, "rule": f"drops at most {g.max_drop} vs {base}",
                        "value": round(v - b, 5), "ok": (b - v) <= g.max_drop + 1e-12})
    return out


def score_surface(ctx, surface, arms: list[str], mde: float, rankings, depths: dict | None = None) -> dict:
    """rankings(arm, queries) -> (ranks, ms), the same engine call the main run uses. `depths`: how deep each
    measure looks in this run (eval/metrics.py), the usual depths when not given."""
    from .ops import judgments_map, rows_of

    _, qs, _ = rows_of(ctx, surface.queries)
    _, jmap = judgments_map(ctx, surface.judgments)
    perq, metrics, arm_rows = {}, {}, []
    for a in arms:
        ranks, _ = rankings(a, qs)
        perq[a] = M.per_query(ranks, jmap, X.current(ctx).custom("metric"), depths)
        metrics[a] = M.summarize(perq[a])
        arm_rows.append({"arm": a, "metrics": metrics[a], "judged@10": metrics[a].get("judged@10")})
    base = arms[0]
    comps = []
    for a in arms[1:]:
        for metric in ("ndcg@10", "r@50"):
            key = M.NAMES[metric]
            comps.append(compare({q: r[key] for q, r in perq[base].items()}, {q: r[key] for q, r in perq[a].items()},
                                 metric, mde if metric == "ndcg@10" else 0.0, arm_a=base, arm_b=a))
    guards = [r for a in arms[1:] for r in check_guardrails(surface.guardrails, a, base, metrics)]
    return {"surface": surface.name, "queries": surface.queries, "judgments": surface.judgments, "arms": arm_rows,
            "comparisons": comps, "guardrails": guards}
