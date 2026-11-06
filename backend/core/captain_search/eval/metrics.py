"""Retrieval metrics, with the standard definitions:

  nDCG@10   gain 2^grade - 1, ideal DCG from ALL judgments of the query (sorted desc, top 10), unjudged = 0
  P@10      share of the top 10 with grade >= 2 (divides by 10 even when fewer hits come back)
  Recall@50 relevant (grade >= 2) in the top 50 / all relevant of the query
  MRR       1 / rank of the first grade >= 2 hit within the top 50 (0 if none)
  zero      1 when the engine returned nothing
"""
from __future__ import annotations

import math

# cst metric name -> key in metrics() output
NAMES = {"ndcg@10": "ndcg10", "p@10": "p10", "r@50": "r50", "mrr": "mrr", "zero": "zero"}
DEFINITIONS = {
    "ndcg@10": "Gain 2^grade - 1, discounted by log2(rank + 1), over the ideal ordering of all of the query's judgments",
    "p@10": "Share of the top 10 with grade 2 or more",
    "r@50": "Relevant results (grade 2 or more) in the top 50, over all relevant results for the query",
    "mrr": "1 / rank of the first result with grade 2 or more, within the top 50",
    "zero": "1 when the engine returned nothing",
}


def metrics(ranked, rel):
    g = lambda i: rel.get(str(i), 0)
    dcg = sum((2 ** g(i) - 1) / math.log2(k + 2) for k, i in enumerate(ranked[:10]))
    ideal = sum((2 ** x - 1) / math.log2(k + 2) for k, x in enumerate(sorted(rel.values(), reverse=True)[:10]))
    nrel = sum(1 for x in rel.values() if x >= 2)
    first = next((k for k, i in enumerate(ranked[:50]) if g(i) >= 2), -1)
    return {
        "ndcg10": dcg / ideal if ideal else 0.0,
        "p10": sum(1 for i in ranked[:10] if g(i) >= 2) / 10,
        "r50": (sum(1 for i in ranked[:50] if g(i) >= 2) / nrel) if nrel else 0.0,
        "mrr": 1 / (first + 1) if first >= 0 else 0.0,
        "zero": 1 if not ranked else 0,
    }


def judged_at(ranked, rel, k=10) -> float | None:
    """Share of the top k that has any judgment. Low coverage silently drags nDCG down."""
    top = [str(i) for i in ranked[:k]]
    if not top:
        return None
    return sum(1 for i in top if i in rel) / len(top)


def pct(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(p / 100 * len(s)))] if s else None


def per_query(results: dict[str, list[str]], judgments: dict[str, dict[str, int]], extra=()) -> dict[str, dict]:
    """results: query_id -> ranked product ids. judgments: query_id -> {product_id: grade}.
    extra: custom metric extensions (extensions.Ext), computed under their own names."""
    out = {}
    for qid, ranked in results.items():
        rel = judgments.get(qid, {})
        m = metrics(ranked, rel)
        m["judged10"] = judged_at(ranked, rel)
        for e in extra:
            try:
                m[e.name] = float(e.obj([str(x) for x in ranked], rel))
            except Exception:  # noqa: BLE001  one bad query shouldn't sink a run; it counts as missing
                m[e.name] = None
        out[qid] = m
    return out


def summarize(rows: dict[str, dict]) -> dict[str, float]:
    vals = list(rows.values())
    n = max(1, len(vals))
    out = {name: round(sum(r[key] for r in vals) / n, 4) for name, key in NAMES.items()}
    j = [r["judged10"] for r in vals if r.get("judged10") is not None]
    out["judged@10"] = round(sum(j) / len(j), 4) if j else 0.0
    builtin = set(NAMES.values()) | {"judged10"}
    for name in sorted({k for r in vals for k in r} - builtin):
        xs = [r[name] for r in vals if r.get(name) is not None]
        if xs:
            out[name] = round(sum(xs) / len(xs), 4)
    return out
