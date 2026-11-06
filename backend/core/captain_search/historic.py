"""Historic replay: turn real search logs into grades, a query mix and a click-by-rank curve.

Logs are an `events` dataset with one row per impression: query, product_id, position (1-based),
clicked, purchased, and optionally session_id, ts, price.

Grades use clicks over expected clicks (COEC): a product shown at rank r is expected to get
ctr(r) clicks per impression, where ctr(r) is the whole log's click rate at that rank. A product that
beats its expected clicks is relevant, whatever rank it was shown at, which removes most of the
position bias. Grades only exist for products the old ranking showed, so a new arm that surfaces
unseen products has lower coverage; reports show coverage next to nDCG.
"""
from __future__ import annotations

from collections import defaultdict

import pyarrow as pa

from .data.datasets import snapshot, to_parquet
from .eval import queries as Q
from .model.entities import DatasetKind, Provenance

REQUIRED = {"query", "product_id", "position", "clicked"}


def norm(q: str) -> str:
    return " ".join(str(q or "").lower().split())


def _rows(ctx, logs_ref: str):
    from .ops import rows_of

    m, rows, _ = rows_of(ctx, logs_ref)
    if rows and not REQUIRED <= set(rows[0]):
        raise ValueError(f"{logs_ref} needs columns {sorted(REQUIRED)} (plus optional purchased, session_id, price); it has {sorted(rows[0])}")
    return m, rows


def ctr_by_rank(rows, depth: int = 20) -> list[float]:
    shown, clicks = [0] * depth, [0] * depth
    for r in rows:
        k = int(r["position"]) - 1
        if 0 <= k < depth:
            shown[k] += 1
            clicks[k] += 1 if r["clicked"] else 0
    out = [(clicks[k] / shown[k]) if shown[k] else 0.0 for k in range(depth)]
    # fill empty ranks with a decaying tail so the curve is defined to `depth`
    last = next((x for x in reversed(out) if x > 0), 0.01)
    return [x if x > 0 else last * 0.8 for x in out]


def grades(rows, min_impressions: int = 3) -> tuple[list[dict], dict]:
    ctr = ctr_by_rank(rows)
    agg = defaultdict(lambda: [0, 0.0, 0, 0])  # impressions, expected clicks, clicks, purchases
    for r in rows:
        k = int(r["position"]) - 1
        a = agg[(norm(r["query"]), str(r["product_id"]))]
        a[0] += 1
        a[1] += ctr[k] if 0 <= k < len(ctr) else ctr[-1]
        a[2] += 1 if r["clicked"] else 0
        a[3] += 1 if r.get("purchased") else 0
    out = []
    for (q, pid), (n, exp, c, b) in agg.items():
        if n < min_impressions:
            continue
        coec = c / exp if exp else 0.0
        g = 3 if (b >= 1 and coec >= 1.0) or b / n >= 0.05 else 2 if coec >= 1.0 else 1 if coec >= 0.4 else 0
        out.append({"query_id": Q.qid("log", q), "query": q, "product_id": pid, "grade": g, "impressions": n,
                    "clicks": c, "purchases": b, "coec": round(coec, 4), "source": "click", "model": "coec-v1",
                    "prompt_version": ""})
    meta = {"pairs": len(out), "impressions": len(rows), "ctr_by_rank": [round(x, 4) for x in ctr[:10]]}
    return out, meta


def query_mix(rows) -> list[dict]:
    """One row per distinct query, weighted by how many searches it got (sessions if present)."""
    seen = defaultdict(set)
    for r in rows:
        seen[norm(r["query"])].add(r.get("session_id") or (r.get("ts"), r["query"]))
    return [{"query_id": Q.qid("log", q), "query": q, "class": "logged", "weight": len(s)} for q, s in sorted(seen.items())]


def synthetic(m) -> bool:
    return str((m.tags or {}).get("synthetic", "")).lower() == "true"


def judgments_from_logs(ctx, logs_ref: str, into: str, min_impressions: int = 3, dry_run=False):
    from .ops import ref

    lm, rows = _rows(ctx, logs_ref)
    out, meta = grades(rows, min_impressions)
    if not out:
        raise ValueError(f"no (query, product) pair in {logs_ref} has {min_impressions}+ impressions")
    m = snapshot(ctx, into, pa.Table.from_pylist(out), kind=DatasetKind.judgments, dry_run=True,
                 provenance=Provenance(source="generated", params={"logs": ref(lm), "min_impressions": min_impressions,
                                                                     "method": "COEC grades from logged clicks", **meta}),
                 description=f"{len(out)} grades from logged clicks in {ref(lm)}")
    m = m.model_copy(update={"judgment_source": "click", "judge": {"model": "coec-v1", "prompt_version": ""},
                             "tags": {"synthetic": "true"} if synthetic(lm) else {}})
    if dry_run:
        return m
    e = ctx.registry.put_blob(to_parquet(pa.Table.from_pylist(out)), "data/part-0.parquet")
    return ctx.registry.publish(m.model_copy(update={"files": [e]}))


def queries_from_logs(ctx, logs_ref: str, into: str, holdout: float = 0.5):
    from .ops import queries_seal, ref

    lm, rows = _rows(ctx, logs_ref)
    mix = query_mix(rows)
    t = pa.Table.from_pylist(mix)
    m = snapshot(ctx, into, t, kind=DatasetKind.queries, dry_run=True,
                 provenance=Provenance(source="generated", params={"logs": ref(lm), "method": "distinct logged queries, weight = searches"}),
                 description=f"{len(mix)} logged queries from {ref(lm)}")
    e = ctx.registry.put_blob(to_parquet(t), "data/part-0.parquet")
    m = ctx.registry.publish(m.model_copy(update={"files": [e], "tags": {"synthetic": "true"} if synthetic(lm) else {}}))
    return queries_seal(ctx, ref(m), holdout)


def make_synthetic_logs(products: list[dict], rank, queries: list[str], sessions: int, seed: int) -> list[dict]:
    """Synthetic impression logs for the demo: the old ranker (`rank`) shows the top 10, a hidden
    relevance (heuristic grade) and position bias decide clicks and purchases. Tagged synthetic."""
    import random

    from .eval.judge import heuristic_grade

    rng = random.Random(seed)
    pmap = {p["id"]: p for p in products}
    look = [0.95, 0.75, 0.6, 0.48, 0.4, 0.33, 0.28, 0.24, 0.21, 0.18]
    attract = {0: 0.03, 1: 0.12, 2: 0.4, 3: 0.65}
    buy = {0: 0.0, 1: 0.01, 2: 0.06, 3: 0.18}
    out = []
    for s in range(sessions):
        q = queries[min(len(queries) - 1, int(len(queries) * rng.random() ** 2.2))]
        ids = rank(q)[:10]
        for k, pid in enumerate(ids):
            g = heuristic_grade(q, pmap[pid])
            clicked = rng.random() < look[k] and rng.random() < attract[g]
            bought = clicked and rng.random() < buy[g] * 4
            out.append({"session_id": f"s{s:06d}", "ts": f"2026-10-{1 + s % 30:02d}", "query": q, "product_id": pid,
                        "position": k + 1, "clicked": clicked, "purchased": bought,
                        "price": float(pmap[pid].get("price") or 0)})
    return out
