"""The what-if sandbox (plan 1): turn a pipeline's knobs and see the ripple before any A/B test.

A knob is a path into the pipeline's spec ("k1", "fields.title", "boosts.0.weight") with the values to try, given as
{"values": [...]} or {"min": a, "max": b, "steps": n}. A pipeline can declare its own under spec.tunables.

sweep() runs the grid in two passes:
1. **Sample:** every combination on a stratified sample of queries (by class, seeded), which takes seconds;
2. **Full:** the most promising few combinations on every query.
Each cell reports nDCG@10 from grades, which is exact for graded results and carries the grades' evidence label, plus
its paired difference against the pipeline's current values, with a confidence interval and a verdict. It also reports
the slices that flip. ripple() shows what one setting would change: the queries whose top 10 moved most, the
regressions, and the team's "must not break" queries.

It never claims online impact: offline grades narrow what's worth an A/B test, and the test stays the decision.
"""
from __future__ import annotations

import copy
import itertools
import random
from collections import defaultdict

MAX_CELLS = 64


def _get(spec: dict, path: str):
    cur = spec
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def _set(spec: dict, path: str, value) -> dict:
    out = copy.deepcopy(spec)
    parts, cur = path.split("."), out
    for p in parts[:-1]:
        nxt = cur[int(p)] if isinstance(cur, list) else cur.setdefault(p, {})
        cur = nxt
    if isinstance(cur, list):
        cur[int(parts[-1])] = value
    else:
        cur[parts[-1]] = value
    return out


def _values(k: dict) -> list:
    if k.get("values") is not None:
        return list(k["values"])
    lo, hi, n = float(k["min"]), float(k["max"]), max(2, int(k.get("steps", 3)))
    return [round(lo + (hi - lo) * i / (n - 1), 6) for i in range(n)]


def _stratified(qs: list[dict], n: int, seed: int) -> list[dict]:
    if n >= len(qs):
        return qs
    rng = random.Random(seed)
    by = defaultdict(list)
    for q in qs:
        by[q.get("class") or "-"].append(q)
    out = []
    for cls, items in sorted(by.items()):
        take = max(1, round(n * len(items) / len(qs)))
        out += rng.sample(items, min(take, len(items)))
    return out[:n] if len(out) > n else out


def _score(ctx, pm, spec: dict, prods, qs, jmap, workspace) -> tuple[dict, dict]:
    from .engines import make_engine
    from .eval import metrics as M

    eng = make_engine(pm.engine, spec, prods, workspace=workspace)
    ranks = {q["query_id"]: eng.search(q["query"], 50) for q in qs}
    return M.per_query(ranks, jmap), ranks


def sweep(ctx, pipeline: str, queries: str, judgments: str, knobs: dict | None = None, *, sample: int = 60,
          top: int = 3, seed: int = 7, metric: str = "ndcg@10", progress=lambda f: None) -> dict:
    from . import ops
    from .eval import metrics as M
    from .eval import stats as ST
    from .eval.judge import EVIDENCE
    from .model.entities import Evidence

    pm = ctx.registry.resolve("pipeline", pipeline)[0]
    knobs = knobs or (pm.spec or {}).get("tunables") or {}
    if not knobs:
        raise ValueError("name the knobs to turn (e.g. {\"k1\": {\"min\": 0.8, \"max\": 2.0, \"steps\": 4}}), or "
                         "declare spec.tunables on the pipeline")
    if len(knobs) > 3:
        raise ValueError("turn at most three knobs at once, so the result can be read")
    grid_values = {p: _values(k) for p, k in knobs.items()}
    cells = [dict(zip(grid_values, combo)) for combo in itertools.product(*grid_values.values())]
    if len(cells) > MAX_CELLS:
        raise ValueError(f"{len(cells)} combinations is too many to read. Use fewer values (at most {MAX_CELLS}).")
    current = {p: _get(pm.spec or {}, p) for p in knobs}
    missing = [p for p, v in current.items() if v is None]
    if missing:
        raise ValueError(f"the pipeline's spec has no {', '.join(missing)}. Knob paths look like k1, fields.title, "
                         "boosts.0.weight")
    _, qs, _ = ops.rows_of(ctx, queries)
    jm, jmap = ops.judgments_map(ctx, judgments)
    ev = EVIDENCE.get(jm.judgment_source or "heuristic", Evidence.SIMULATED).value
    prods = ops.products_of(ctx, pm.catalog) if pm.catalog else None
    key = M.NAMES.get(metric, "ndcg10")
    sq = _stratified(qs, sample, seed)

    def run(values: dict, qset):
        spec = pm.spec or {}
        for p, v in values.items():
            spec = _set(spec, p, v)
        pq, ranks = _score(ctx, pm, spec, prods, qset, jmap, ctx.workspace.name)
        return pq, ranks

    base_sample, _ = run(current, sq)
    sample_cells = []
    for i, values in enumerate(cells):
        progress(0.6 * i / len(cells))
        pq, _ = run(values, sq)
        c = ST.compare({q: r[key] for q, r in base_sample.items()}, {q: r[key] for q, r in pq.items()}, metric, 0.0,
                       arm_a="current", arm_b="cell")
        sample_cells.append({"values": values, "is_current": values == current,
                             "mean": round(sum(r[key] for r in pq.values()) / max(1, len(pq)), 4),
                             "delta": c["delta"], "ci": [c["ci_low"], c["ci_high"]], "verdict": c["verdict"], "n": len(pq)})
    ranked = sorted((c for c in sample_cells if not c["is_current"]), key=lambda c: -c["delta"])[:top]
    base_full, base_ranks = run(current, qs)
    full_cells = []
    for i, c in enumerate(ranked):
        progress(0.6 + 0.4 * i / max(1, len(ranked)))
        pq, ranks = run(c["values"], qs)
        cmp_ = ST.compare({q: r[key] for q, r in base_full.items()}, {q: r[key] for q, r in pq.items()}, metric, 0.0,
                          arm_a="current", arm_b="cell")
        flips = _slice_flips(qs, base_full, pq, key)
        full_cells.append({"values": c["values"], "mean": round(sum(r[key] for r in pq.values()) / max(1, len(pq)), 4),
                           "delta": cmp_["delta"], "ci": [cmp_["ci_low"], cmp_["ci_high"]], "verdict": cmp_["verdict"], "n": len(pq),
                           "slice_flips": flips})
    progress(1.0)
    best = max(full_cells, key=lambda c: c["delta"]) if full_cells else None
    return {"pipeline": ops.ref(pm), "queries": queries, "judgments": f"{jm.name}@v{jm.version}", "metric": metric,
            "evidence": ev, "knobs": {p: grid_values[p] for p in knobs}, "current": current, "seed": seed,
            "sample": {"n": len(sq), "cells": sample_cells}, "full": {"n": len(qs), "cells": full_cells},
            "best": best, "baseline_mean": round(sum(r[key] for r in base_full.values()) / max(1, len(base_full)), 4),
            "note": "Offline, from grades: it narrows what's worth an A/B test, it doesn't replace one."}


def _slice_flips(qs, base: dict, new: dict, key: str) -> list[dict]:
    by = defaultdict(list)
    for q in qs:
        by[q.get("class") or "-"].append(q["query_id"])
    out = []
    for cls, ids in sorted(by.items()):
        ids = [i for i in ids if i in base and i in new]
        if len(ids) < 5:
            continue
        d = sum(new[i][key] - base[i][key] for i in ids) / len(ids)
        if abs(d) >= 0.01:
            out.append({"slice": cls, "n": len(ids), "delta": round(d, 4), "direction": "up" if d > 0 else "down"})
    return sorted(out, key=lambda x: x["delta"])


def ripple(ctx, pipeline: str, queries: str, judgments: str, values: dict, must_not_break: list[str] | None = None,
           limit: int = 20) -> dict:
    """What one setting changes against the pipeline's current values, query by query."""
    from . import ops
    from .eval import metrics as M

    pm = ctx.registry.resolve("pipeline", pipeline)[0]
    _, qs, _ = ops.rows_of(ctx, queries)
    _, jmap = ops.judgments_map(ctx, judgments)
    prods = ops.products_of(ctx, pm.catalog) if pm.catalog else None
    spec = pm.spec or {}
    for p, v in values.items():
        spec = _set(spec, p, v)
    must = [q.strip().lower() for q in (must_not_break or []) if q.strip()]
    extra = [{"query_id": f"must-{i}", "query": q} for i, q in enumerate(must) if q not in {x["query"].lower() for x in qs}]
    allq = qs + extra
    bpq, br = _score(ctx, pm, pm.spec or {}, prods, allq, jmap, ctx.workspace.name)
    npq, nr = _score(ctx, pm, spec, prods, allq, jmap, ctx.workspace.name)
    rows = []
    for q in allq:
        a, b = br[q["query_id"]][:10], nr[q["query_id"]][:10]
        changed = 1 - len(set(a) & set(b)) / max(1, len(set(a) | set(b)))
        rows.append({"query_id": q["query_id"], "query": q["query"], "top10_changed": round(changed, 3),
                     "ndcg_before": bpq[q["query_id"]]["ndcg10"] if jmap.get(q["query_id"]) else None,
                     "ndcg_after": npq[q["query_id"]]["ndcg10"] if jmap.get(q["query_id"]) else None,
                     "new_top": b[:3], "old_top": a[:3]})
    moved = sorted(rows, key=lambda r: -r["top10_changed"])[:limit]
    worse = sorted((r for r in rows if r["ndcg_before"] is not None and r["ndcg_after"] < r["ndcg_before"]),
                   key=lambda r: r["ndcg_after"] - r["ndcg_before"])[:limit]
    guard = [r for r in rows if r["query"].lower() in must]
    broke = [r for r in guard if r["top10_changed"] > 0.5 or (r["ndcg_before"] is not None and r["ndcg_after"] < r["ndcg_before"])]
    return {"pipeline": ops.ref(pm), "values": values, "moved_most": moved, "regressions": worse,
            "must_not_break": guard, "broken": [r["query"] for r in broke],
            "summary": f"{sum(1 for r in rows if r['top10_changed'] > 0)} of {len(rows)} queries change their top 10; "
                       f"{len(worse)} get worse" + (f"; {len(broke)} must-not-break queries moved" if must else "")}


def promote(ctx, pipeline: str, values: dict, note: str = "") -> dict:
    """Publish the setting as the pipeline's next version, ready for a pre-registered backtest. Nothing goes live."""
    from . import ops

    pm = ctx.registry.resolve("pipeline", pipeline)[0]
    spec = pm.spec or {}
    for p, v in values.items():
        spec = _set(spec, p, v)
    nv = ops.pipeline_publish(ctx, {"name": pm.name, "engine": pm.engine, "spec": spec, "catalog": pm.catalog,
                                    "description": note or f"From a sweep: {values}"})
    return {"pipeline": ops.ref(nv), "from": ops.ref(pm), "values": values,
            "next": f"Pre-register a backtest of {ops.ref(nv)} against {ops.ref(pm)} before trusting the sweep's number."}
