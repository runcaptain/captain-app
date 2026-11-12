"""Inspect model differences (spec story S5): per-query deltas, one query side by side, and whether a comparison
can be trusted.

Built for native screens, which must not hold or sort a whole result set (gpui-kit stops a script after 50 ms in
render). So every list here is sorted, filtered and paged on the server, and charts get pre-totalled series.

  queries(run)          one row per query: both arms' nDCG@10, the delta, judged@10, class and weight. Paged.
  query(run, qid)       both arms' ranked lists with titles, grades (null = unjudged) and how each item moved
  compatibility(run)    reasons a winner should not be shown without qualification
  compare_runs(a, b)    the same check across two runs, for comparing results from different backtests
  histogram(run)        the delta distribution, pre-binned, for a chart
"""
from __future__ import annotations

import json

from . import extensions as X
from .eval import metrics as M
from .model.entities import BacktestRun, Evidence
from .workspace import Context


def _run(ctx: Context, ref: str) -> BacktestRun:
    name, _, v = ref.partition("@v")
    return ctx.registry.load("backtest-run", name, int(v) if v else None)


def _arms(run: BacktestRun, a: str | None, b: str | None) -> tuple[str, str]:
    names = [x.arm for x in run.arms]
    a = a or names[0]
    b = b or (names[1] if len(names) > 1 else names[0])
    for x in (a, b):
        if x not in names:
            raise ValueError(f"{x} is not an arm of this run: {', '.join(names)}")
    return a, b


def _rankings(ctx: Context, run: BacktestRun, arm: str) -> dict[str, list[str]]:
    ar = next(x for x in run.arms if x.arm == arm)
    entry = next((f for f in ar.logs if f.path.startswith("rankings/")), None)
    if entry is None:
        raise ValueError(f"{arm} has no stored rankings in this run (imported runs keep only their readout)")
    out = {}
    for line in ctx.registry.get_blob(entry).decode().splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["query_id"]] = r["ids"]
    return out


def _context(ctx: Context, run: BacktestRun):
    from .ops import judgments_map, rows_of

    bt = ctx.registry.load("backtest", run.name)
    _, qs, _ = rows_of(ctx, bt.queries)
    jmap = judgments_map(ctx, bt.judgments)[1] if bt.judgments else {}
    return bt, {q["query_id"]: q for q in qs}, jmap


def queries(ctx: Context, run_ref: str, a: str | None = None, b: str | None = None, sort: str = "delta",
            desc: bool = False, offset: int = 0, limit: int = 100, cls: str | None = None, search: str | None = None,
            only: str | None = None) -> dict:
    run = _run(ctx, run_ref)
    a, b = _arms(run, a, b)
    bt, qs, jmap = _context(ctx, run)
    ra, rb = _rankings(ctx, run, a), _rankings(ctx, run, b)
    ex = X.current(ctx).custom("metric")
    pa, pb = M.per_query(ra, jmap, ex), M.per_query(rb, jmap, ex)
    rows = []
    for qid in sorted(set(pa) & set(pb)):
        q = qs.get(qid, {})
        d = round(pb[qid]["ndcg10"] - pa[qid]["ndcg10"], 5)
        rows.append({"query_id": qid, "query": q.get("query"), "class": q.get("class"), "weight": q.get("weight"),
                     "ndcg_a": round(pa[qid]["ndcg10"], 5), "ndcg_b": round(pb[qid]["ndcg10"], 5), "delta": d,
                     "judged_a": pa[qid]["judged10"], "judged_b": pb[qid]["judged10"],
                     "overlap10": len(set(ra[qid][:10]) & set(rb[qid][:10]))})
    total_all = len(rows)
    if cls:
        rows = [r for r in rows if r["class"] == cls]
    if search:
        s = search.lower()
        rows = [r for r in rows if s in str(r["query"] or "").lower()]
    if only == "regressions":
        rows = [r for r in rows if r["delta"] < 0]
    elif only == "improvements":
        rows = [r for r in rows if r["delta"] > 0]
    elif only == "unchanged":
        rows = [r for r in rows if r["delta"] == 0]
    keys = {"delta": lambda r: r["delta"], "query": lambda r: str(r["query"] or ""), "ndcg_a": lambda r: r["ndcg_a"],
            "ndcg_b": lambda r: r["ndcg_b"], "weight": lambda r: r["weight"] or 0, "overlap": lambda r: r["overlap10"]}
    if sort not in keys:
        raise ValueError(f"sort is one of {', '.join(keys)}")
    rows.sort(key=keys[sort], reverse=desc)
    limit = max(1, min(int(limit), 500))
    return {"run": f"{run.name}@v{run.version}", "a": a, "b": b, "total": len(rows), "total_unfiltered": total_all,
            "counts": {"regressions": sum(1 for r in rows if r["delta"] < 0), "improvements": sum(1 for r in rows if r["delta"] > 0),
                       "unchanged": sum(1 for r in rows if r["delta"] == 0)},
            "offset": int(offset), "limit": limit, "rows": rows[int(offset): int(offset) + limit],
            "metric": "ndcg@10", "metric_definition": M.__doc__.strip().splitlines()[0] if M.__doc__ else "nDCG@10"}


def query(ctx: Context, run_ref: str, qid: str, a: str | None = None, b: str | None = None, k: int = 20) -> dict:
    from .ops import products_of

    run = _run(ctx, run_ref)
    a, b = _arms(run, a, b)
    bt, qs, jmap = _context(ctx, run)
    ra, rb = _rankings(ctx, run, a), _rankings(ctx, run, b)
    if qid not in ra or qid not in rb:
        raise ValueError(f"query {qid} is not in both arms' rankings")
    prods = {}
    for arm in (b, a):  # titles from the first arm that's a Captain pipeline with a catalog
        try:
            pm, _ = ctx.registry.resolve("pipeline", arm)
        except Exception:  # noqa: BLE001  an imported arm (a team's own system) has no pipeline or catalog here
            continue
        if pm.catalog:
            prods = {p["id"]: p for p in products_of(ctx, pm.catalog)}
            break
    if not prods and (bt.tags or {}).get("catalog"):  # imported rankings can name a catalog for titles
        prods = {str(p["id"]): p for p in products_of(ctx, bt.tags["catalog"])}
    grades = jmap.get(qid, {})
    k = max(1, min(int(k), 100))
    la, lb = ra[qid][:k], rb[qid][:k]
    pos_a = {pid: i + 1 for i, pid in enumerate(ra[qid])}
    pos_b = {pid: i + 1 for i, pid in enumerate(rb[qid])}

    def item(pid, rank, other):
        p = prods.get(pid, {})
        o = other.get(pid)
        return {"rank": rank, "product_id": pid, "title": p.get("title"), "brand": p.get("brand"), "price": p.get("price"),
                "grade": grades.get(pid), "other_rank": o, "moved": None if o is None else o - rank}

    ex = X.current(ctx).custom("metric")
    pa, pb = M.per_query({qid: ra[qid]}, jmap, ex)[qid], M.per_query({qid: rb[qid]}, jmap, ex)[qid]
    return {"run": f"{run.name}@v{run.version}", "query_id": qid, "query": qs.get(qid, {}).get("query"),
            "class": qs.get(qid, {}).get("class"), "a": {"arm": a, "ndcg10": pa["ndcg10"], "judged10": pa["judged10"],
                                                          "items": [item(pid, i + 1, pos_b) for i, pid in enumerate(la)]},
            "b": {"arm": b, "ndcg10": pb["ndcg10"], "judged10": pb["judged10"], "items": [item(pid, i + 1, pos_a) for i, pid in enumerate(lb)]},
            "dropped": [pid for pid in la if pid not in rb[qid][:k]], "new": [pid for pid in lb if pid not in ra[qid][:k]],
            "grade_scale": {"0": "irrelevant", "1": "partial", "2": "relevant", "3": "exact", "null": "not judged"},
            "notes": ["A product without a grade was never judged for this query, so nDCG counts it as not relevant."]}


def compatibility(ctx: Context, run_ref: str) -> dict:
    run = _run(ctx, run_ref)
    bt = ctx.registry.load("backtest", run.name)
    warnings = []
    if not run.independent:
        warnings.append({"code": "not-independent", "text": "the simulated shoppers clicked by the same judgments that score nDCG, so the two are one signal counted twice"})
    if (bt.tags or {}).get("label_independence") == "shared":
        from .project_state import OWN_LABELER

        warnings.append({"code": "own-labeler", "text": f"the trained arm learned from the same labeler that grades the test set "
                         f"({bt.tags.get('train_labels') or 'its training labels'}), so a higher score {OWN_LABELER} rather than beating the other arm"})
    if not run.complete:
        warnings.append({"code": "incomplete", "text": "an arm has judged@10 under 90%, so its nDCG is understated"})
    jud = [x.judged_at_10 for x in run.arms if x.judged_at_10 is not None]
    if len(jud) > 1 and max(jud) - min(jud) > 0.1:
        warnings.append({"code": "uneven-coverage", "text": f"judged@10 differs between arms ({min(jud):.0%} to {max(jud):.0%}), so part of the delta can come from coverage, not quality"})
    if run.mode == "historic":
        warnings.append({"code": "exposure-bias", "text": "grades come from logged clicks on what the old ranking showed: products it never showed can't be judged, which favours the old ranking"})
    ev = sorted({x.evidence for x in run.arms}, key=lambda e: e.rank)
    if ev and ev[-1].rank < Evidence.REAL_TRAFFIC.rank:
        warnings.append({"code": "offline", "text": f"offline evidence ({ev[-1].value}): not proof of online or business impact"})
    if bt.queries and not bt.queries.endswith(":holdout"):
        warnings.append({"code": "not-holdout", "text": "scored on a query set that is not a sealed test set, so tuning on it can overfit"})
    small = [s for s in run.segments if s.get("verdict") == "too few"]
    if small:
        warnings.append({"code": "small-slices", "text": (f"{len(small)} slice results rest on fewer than 5 queries each" if len(small) != 1 else "1 slice result rests on fewer than 5 queries")})
    return {"run": f"{run.name}@v{run.version}", "trustworthy": not any(w["code"] in ("not-independent", "incomplete", "own-labeler") for w in warnings),
            "warnings": warnings, "queries": bt.queries, "judgments": bt.judgments, "mode": run.mode}


def compare_runs(ctx: Context, a_ref: str, b_ref: str) -> dict:
    """Two runs from different backtests: list everything that makes their numbers not comparable."""
    ra, rb = _run(ctx, a_ref), _run(ctx, b_ref)
    ba, bb = ctx.registry.load("backtest", ra.name), ctx.registry.load("backtest", rb.name)
    issues = []
    if ba.queries.split(":")[0] != bb.queries.split(":")[0] or ba.queries.split(":")[1:] != bb.queries.split(":")[1:]:
        issues.append({"code": "different-queries", "text": f"different query sets ({ba.queries} vs {bb.queries})"})
    if ba.judgments != bb.judgments:
        issues.append({"code": "different-judgments", "text": f"different judgments ({ba.judgments} vs {bb.judgments}), so nDCG is not on the same scale"})
    if ba.preregistration.primary_metric != bb.preregistration.primary_metric:
        issues.append({"code": "different-metric", "text": f"different primary metrics ({ba.preregistration.primary_metric} vs {bb.preregistration.primary_metric})"})
    if ra.mode != rb.mode:
        issues.append({"code": "different-mode", "text": f"different modes ({ra.mode} vs {rb.mode})"})
    if ba.population != bb.population:
        issues.append({"code": "different-shoppers", "text": "different shopper populations, so simulated CTR and conversion are not comparable"})
    return {"a": f"{ra.name}@v{ra.version}", "b": f"{rb.name}@v{rb.version}", "comparable": not issues, "issues": issues}


def regression(ctx: Context, member=None, model: str | None = None, limit: int = 50) -> dict:
    """Regression testing: every evaluation run, newest first, one row each with the model judged, the model it
    is compared with and the change on the primary metric. {rows, models}.

    A row: {name, version, created, mode, model, against, metric, delta, ci_low, ci_high, comparison (win | noise |
    loss, as the run recorded it), evidence, value, against_value, too_few_judged, own_labeler, independent}.
    The model judged is the primary comparison's B arm; `value` and `against_value` are the two arms' scores on
    that metric. `too_few_judged` (an arm has under 90% of its top 10 judged) and `own_labeler` (the model learned
    from the labeler that grades the test set) change what the result means, so they come with every row.
    `model` keeps one model's rows (its name without the version). `models` lists every model that has a row.
    `member` is the list routes' name predicate for evaluations (a project or the Explorer's pick)."""
    bare = lambda ref: str(ref or "").split("@")[0]
    rows: list[dict] = []
    for n in ctx.registry.names("backtest-run"):
        if member is not None and not member(n):
            continue
        try:
            bt = ctx.registry.load("backtest", n)
        except Exception:  # noqa: BLE001  a run whose evaluation is gone still lists, without what the plan said
            bt = None
        own = bool(bt and (bt.tags or {}).get("label_independence") == "shared")
        for v in ctx.registry.versions("backtest-run", n):
            r = ctx.registry.load("backtest-run", n, v)
            p = next((c for c in r.comparisons if c.get("primary")), r.comparisons[0] if r.comparisons else None)
            if p is None:
                continue
            metric = p.get("metric")
            score = {a.arm: (a.offline or {}).get(metric) for a in r.arms}
            rows.append({"name": n, "version": v, "created": r.created_at.isoformat(), "mode": r.mode,
                         "model": p.get("b"), "against": p.get("a"), "metric": metric, "delta": p.get("delta"),
                         "ci_low": p.get("ci_low"), "ci_high": p.get("ci_high"), "comparison": p.get("verdict"),
                         "evidence": p.get("evidence"), "value": score.get(p.get("b")), "against_value": score.get(p.get("a")),
                         "too_few_judged": not r.complete, "own_labeler": own, "independent": r.independent})
    models = sorted({bare(r["model"]) for r in rows if r["model"]})
    if model:
        rows = [r for r in rows if bare(r["model"]) == bare(model)]
    rows.sort(key=lambda r: r["created"], reverse=True)
    return {"rows": rows[: max(1, min(int(limit), 500))], "models": models}


def histogram(ctx: Context, run_ref: str, a: str | None = None, b: str | None = None, bins: int = 21) -> dict:
    """The per-query nDCG@10 delta distribution, binned on [-1, 1], for a chart that never sees raw rows."""
    first = queries(ctx, run_ref, a, b, limit=500)
    allrows, off = list(first["rows"]), 500
    while off < first["total"]:
        allrows += queries(ctx, run_ref, a, b, limit=500, offset=off)["rows"]
        off += 500
    bins = max(3, min(int(bins), 101))
    counts = [0] * bins
    for r in allrows:
        counts[min(bins - 1, max(0, int((r["delta"] + 1) / 2 * bins)))] += 1
    return {"run": first["run"], "a": first["a"], "b": first["b"], "n": first["total"],
            "edges": [round(-1 + 2 * i / bins, 4) for i in range(bins + 1)], "counts": counts}
