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


# How many results deep each measure looks unless an evaluation says otherwise. The names keep their usual depth
# in them ("ndcg@10") because they are identifiers used everywhere; an evaluation's `depths` says how deep it
# really looked, and a run records it, so two runs at different depths are never read as the same measurement.
DEFAULT_DEPTHS = {"ndcg@10": 10, "p@10": 10, "r@50": 50, "mrr": 50}
MAX_DEPTH = 200
DEPTH_WORDS = {"ndcg@10": "nDCG", "p@10": "Precision", "r@50": "Recall", "mrr": "MRR"}


def check_depths(depths: dict | None) -> dict[str, int]:
    """The depth of every measure that has one: the defaults, with what was asked for on top. Refuses a measure
    that has no depth, and a depth that isn't a whole number from 1 to MAX_DEPTH."""
    out = dict(DEFAULT_DEPTHS)
    for k, v in (depths or {}).items():
        if k not in DEFAULT_DEPTHS:
            raise ValueError(f"{k} has no depth to set. Depth applies to {', '.join(DEFAULT_DEPTHS)}.")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"the depth of {k} must be a whole number, not {v!r}") from None
        if n != float(v) or not 1 <= n <= MAX_DEPTH:
            raise ValueError(f"the depth of {k} must be a whole number from 1 to {MAX_DEPTH}, not {v!r}")
        out[k] = n
    return out


def non_default(depths: dict | None) -> list[str]:
    """What differs from the usual depths, as sentences: ["nDCG looks 20 results deep, not the usual 10."]."""
    d = check_depths(depths) if depths else DEFAULT_DEPTHS
    return [f"{DEPTH_WORDS[k]} looks {d[k]} results deep, not the usual {DEFAULT_DEPTHS[k]}." for k in DEFAULT_DEPTHS if d[k] != DEFAULT_DEPTHS[k]]


def label(name: str, depths: dict | None) -> str:
    """A measure's name with the depth it really looked: "ndcg@10" at depth 20 reads "ndcg@20"."""
    d = check_depths(depths) if depths else DEFAULT_DEPTHS
    base = name[:-3] if name.endswith("_tw") else name
    if base in d and "@" in base and d[base] != DEFAULT_DEPTHS[base]:
        return f"{base.split('@')[0]}@{d[base]}" + ("_tw" if name.endswith("_tw") else "")
    return name


def metrics(ranked, rel, depths: dict | None = None):
    d = depths or DEFAULT_DEPTHS
    kn, kp, kr, km = d["ndcg@10"], d["p@10"], d["r@50"], d["mrr"]
    g = lambda i: rel.get(str(i), 0)
    dcg = sum((2 ** g(i) - 1) / math.log2(k + 2) for k, i in enumerate(ranked[:kn]))
    ideal = sum((2 ** x - 1) / math.log2(k + 2) for k, x in enumerate(sorted(rel.values(), reverse=True)[:kn]))
    nrel = sum(1 for x in rel.values() if x >= 2)
    first = next((k for k, i in enumerate(ranked[:km]) if g(i) >= 2), -1)
    return {
        "ndcg10": dcg / ideal if ideal else 0.0,
        "p10": sum(1 for i in ranked[:kp] if g(i) >= 2) / kp,
        "r50": (sum(1 for i in ranked[:kr] if g(i) >= 2) / nrel) if nrel else 0.0,
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


def per_query(results: dict[str, list[str]], judgments: dict[str, dict[str, int]], extra=(), depths: dict | None = None) -> dict[str, dict]:
    """results: query_id -> ranked product ids. judgments: query_id -> {product_id: grade}.
    extra: custom metric extensions (extensions.Ext), computed under their own names.
    depths: how deep each measure looks (check_depths), the usual depths when not given. The share of judged
    results is taken at nDCG's depth, since that is the measure it protects."""
    out = {}
    d = depths or DEFAULT_DEPTHS
    for qid, ranked in results.items():
        rel = judgments.get(qid, {})
        m = metrics(ranked, rel, d)
        m["judged10"] = judged_at(ranked, rel, d["ndcg@10"])
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
