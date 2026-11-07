"""The profiler (plan 5): where a pipeline's time and memory go, stage by stage, over many queries.

It replays queries offline through the query debugger's traces (explain.py), which time every stage, and measures
the Python memory each stage allocates (tracemalloc peak, which isn't process RSS and is labeled so). It aggregates per
stage: p50, p95, p99, mean, and its share of total time. It lists the slowest queries with the stage that dominates
each, and checks the pipeline's latency budget, declared in its spec:

  budget: {p99_ms: 120, stages: {retrieve: 30, rules: 5}}     stage keys are stage kinds or stage names

Every result carries `measured_on` (machine, Python, concurrency 1, offline replay), because a laptop replay isn't
production latency. Engines that can't break their time down show one "search" stage with the whole time.
"""
from __future__ import annotations

import platform
import time
import tracemalloc
from collections import defaultdict


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 3)


def measured_on() -> dict:
    return {"machine": f"{platform.system()} {platform.machine()}", "python": platform.python_version(),
            "concurrency": 1, "where": "offline replay on this computer, not production"}


def profile_pipeline(ctx, pipeline: str, queries: list[dict], *, memory: bool = True, k: int = 50,
                     progress=lambda f: None) -> dict:
    """queries: [{query_id?, query}]. Returns per-stage stats, the slowest queries and the budget check."""
    from . import ops
    from .engines import make_engine
    from .explain import explain_engine

    pm = ctx.registry.resolve("pipeline", pipeline)[0]
    prods = ops.products_of(ctx, pm.catalog) if pm.catalog else None
    eng = make_engine(pm.engine, pm.spec, prods, workspace=ctx.workspace.name)
    stage_ms, stage_mem, order, per_query, kinds = defaultdict(list), defaultdict(list), [], [], {}
    explained = True
    for i, q in enumerate(queries):
        progress(i / max(1, len(queries)))
        if memory:
            tracemalloc.start()
        t0 = time.perf_counter()
        stages = explain_engine(eng, q["query"], k)
        total = (time.perf_counter() - t0) * 1000
        peak = tracemalloc.get_traced_memory()[1] if memory else None
        if memory:
            tracemalloc.stop()
        times = {}
        for s in stages:
            name = s["stage"]
            kinds[name] = s.get("kind")
            if name not in order:
                order.append(name)
            ms = s.get("timing_ms")
            if ms is None and len(stages) == 1:
                ms = total
            if ms is not None:
                stage_ms[name].append(float(ms))
                times[name] = float(ms)
            explained = explained and s.get("explained", True)
        if peak is not None:
            stage_mem["query"].append(peak)
        dom = max(times.items(), key=lambda kv: kv[1])[0] if times else None
        per_query.append({"query_id": q.get("query_id"), "query": q["query"], "total_ms": round(total, 3),
                          "dominant_stage": dom, "stages_ms": {k_: round(v, 3) for k_, v in times.items()},
                          "candidates": next((s.get("candidates") for s in stages if s.get("candidates") is not None), None)})
    totals = [x["total_ms"] for x in per_query]
    sum_total = sum(totals) or 1.0
    stages = [{"stage": n, "kind": kinds.get(n),
               "p50_ms": _pct(stage_ms[n], 50), "p95_ms": _pct(stage_ms[n], 95), "p99_ms": _pct(stage_ms[n], 99),
               "mean_ms": round(sum(stage_ms[n]) / len(stage_ms[n]), 3) if stage_ms[n] else None,
               "share": round(sum(stage_ms[n]) / sum_total, 3) if stage_ms[n] else None} for n in order]
    slow = sorted(per_query, key=lambda x: -x["total_ms"])[:10]
    out = {"pipeline": ops.ref(pm), "engine": pm.engine, "queries": len(queries), "explained": explained,
           "total": {"p50_ms": _pct(totals, 50), "p95_ms": _pct(totals, 95), "p99_ms": _pct(totals, 99),
                     "mean_ms": round(sum(totals) / len(totals), 3) if totals else None},
           "memory": {"peak_python_bytes_p50": _pct(stage_mem["query"], 50), "peak_python_bytes_max":
                      max(stage_mem["query"]) if stage_mem["query"] else None,
                      "what": "Python allocations at their peak during one query (tracemalloc), not process RSS"}
           if memory else None,
           "stages": stages, "slowest": slow, "measured_on": measured_on()}
    out["budget"] = check_budget((pm.spec or {}).get("budget"), out)
    progress(1.0)
    return out


def check_budget(budget: dict | None, prof: dict) -> dict | None:
    """{ok, breaches: [sentence], budget}. None when the pipeline declares no budget."""
    if not budget:
        return None
    breaches = []
    p99 = prof["total"]["p99_ms"]
    if budget.get("p99_ms") is not None and p99 is not None and p99 > float(budget["p99_ms"]):
        breaches.append(f"p99 is {p99:.1f} ms, over the {float(budget['p99_ms']):g} ms budget")
    for key, limit in (budget.get("stages") or {}).items():
        st = next((s for s in prof["stages"] if s["stage"] == key or s["kind"] == key), None)
        if st is None:
            breaches.append(f"the budget names a stage {key!r} this pipeline doesn't have")
        elif st["p99_ms"] is not None and st["p99_ms"] > float(limit):
            breaches.append(f"{st['stage']} p99 is {st['p99_ms']:.1f} ms, over its {float(limit):g} ms budget")
    return {"ok": not breaches, "breaches": breaches, "budget": budget}


def profile_run(ctx, run_ref: str, max_queries: int = 200, memory: bool = True, progress=lambda f: None) -> dict:
    """Each replayable arm of a comparison run over the run's queries, and the latency change against the baseline."""
    from . import compare as CP

    run = CP._run(ctx, run_ref)
    arms, skipped = [], []
    replayable = []
    imported = getattr(run, "mode", None) == "imported"  # its rankings came from outside, whatever its arms are named
    for a in (r.arm for r in run.arms):
        try:
            if imported:
                raise ValueError("imported")
            ctx.registry.resolve("pipeline", a)
            replayable.append(a)
        except Exception:  # noqa: BLE001
            skipped.append({"arm": a, "reason": "from imported rankings, so it can't be replayed to time it"})
    if not replayable:
        # Nothing to replay (an imported run): say why for each arm, before looking for a query set it may not have.
        progress(1.0)
        return {"run": f"{run.name}@v{run.version}", "queries": 0, "arms": [], "skipped": skipped, "summary": [],
                "measured_on": measured_on()}
    bt, qs, _ = CP._context(ctx, run)
    queries = list(qs.values())[: max(1, int(max_queries))]
    for i, a in enumerate(replayable):
        arms.append(profile_pipeline(ctx, a, queries, memory=memory,
                                     progress=lambda f, i=i: progress((i + f) / max(1, len(replayable)))))
    lines = []
    if len(arms) >= 2:
        base = arms[0]
        for other in arms[1:]:
            d = (other["total"]["p99_ms"] or 0) - (base["total"]["p99_ms"] or 0)
            by_stage = {s["stage"]: s["p99_ms"] or 0 for s in base["stages"]}
            moves = sorted(((s["stage"], (s["p99_ms"] or 0) - by_stage.get(s["stage"], 0)) for s in other["stages"]),
                           key=lambda kv: -abs(kv[1]))
            top = moves[0] if moves else None
            lines.append(f"{other['pipeline']} {'adds' if d >= 0 else 'saves'} {abs(d):.1f} ms at p99 against "
                         f"{base['pipeline']}" + (f", most of it in {top[0]} ({top[1]:+.1f} ms)" if top and abs(top[1]) > 0.05 else ""))
    return {"run": f"{run.name}@v{run.version}", "queries": len(queries), "arms": arms, "skipped": skipped,
            "summary": lines, "measured_on": measured_on()}
