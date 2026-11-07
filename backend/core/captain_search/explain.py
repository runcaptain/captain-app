"""The query debugger (plan 2): why did a pipeline rank this, stage by stage?

A trace is a list of StageTraces, one per stage, in order:
  {stage, kind: understand | retrieve | rerank | rules | filter | final, explained: bool, notes: [..],
   items: [{id, rank, score, score_parts: {name: value}, fired: [rule], moved: int | None}], timing_ms}

- Local BM25 explains every stage exactly: query tokens, synonyms and prefix expansions, the price filter it read,
  per-field and per-term BM25 contributions for every candidate, the boosts that fired with the score before and
  after, the items the filter removed, and the final ranks.
- Elasticsearch explains through its own `explain: true`: the top contributions of each hit's explanation tree.
- Any other engine returns its ranked ids with explained=false and the note "no explanation provided". Captain never
  invents a reason.

diff() compares two traces, the baseline and the candidate, for one query, and first_divergence() names the first
stage where a relevant item (grade 2 or more) was lost or ranked lower.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict


def _item(pid, rank, score=None, parts=None, fired=None, moved=None, title=None):
    return {"id": str(pid), "rank": rank, "score": None if score is None else round(float(score), 6),
            "score_parts": {k: round(float(v), 6) for k, v in (parts or {}).items()}, "fired": fired or [],
            "moved": moved, "title": title}


def explain_bm25(eng, query: str, k: int = 50) -> list[dict]:
    from .engines import _price, tokens

    t0 = time.perf_counter()
    clock = [t0]

    def lap():
        now = time.perf_counter()
        ms = round((now - clock[0]) * 1000, 3)
        clock[0] = now
        return ms

    raw = tokens(query)
    q, limit, notes = list(raw), None, []
    if eng.spec.get("numeric_filter"):
        m = re.search(r"\b(?:under|below|less than)\s+\$?(\d+(?:\.\d+)?)", query.lower())
        if m:
            limit = float(m.group(1))
            q = [t for t in q if t not in ("under", "below", "less", "than", m.group(1).split(".")[0])]
            notes.append(f"read a price limit: {limit:g} or less")
    expanded = eng._expand(q)
    added = [t for t in expanded if t not in q]
    syn = {t: eng.syn[t] for t in q if t in eng.syn}
    unknown = [t for t in q if not any(t in eng.idf[f] for f in eng.fields)]
    understand = {"stage": "query understanding", "kind": "understand", "explained": True, "items": [],
                  "tokens": raw, "terms": q, "expanded_with": added, "synonyms": syn, "unknown_terms": unknown,
                  "notes": notes + ([f"no product contains: {', '.join(unknown)}"] if unknown else []),
                  "timing_ms": lap()}
    parts: dict[str, dict[str, float]] = defaultdict(dict)
    for f, w in eng.fields.items():
        for t in expanded:
            idf = eng.idf[f].get(t)
            if idf is None:
                continue
            for pid in eng.post[f][t]:
                tf = eng.tf[pid][f][t]
                norm = tf * (eng.k1 + 1) / (tf + eng.k1 * (1 - eng.b + eng.b * eng.len[f][pid] / eng.avg[f]))
                key = f"{f}:{t}"
                parts[pid][key] = parts[pid].get(key, 0.0) + w * idf * norm
    base = {pid: sum(p.values()) for pid, p in parts.items()}
    order = lambda sc: [p for p, _ in sorted(sc.items(), key=lambda kv: (-kv[1], kv[0]))]
    r0 = order(base)
    title = lambda pid: eng.products.get(pid, {}).get("title")
    retrieve = {"stage": "lexical retrieval (BM25)", "kind": "retrieve", "explained": True,
                "notes": [f"{len(r0)} products match a query term", f"fields and weights: {eng.fields}"],
                "items": [_item(p, i + 1, base[p], parts[p], title=title(p)) for i, p in enumerate(r0[:k])],
                "candidates": len(r0), "timing_ms": lap()}
    scored, fired = dict(base), defaultdict(list)
    for boost in eng.spec.get("boosts") or []:
        for pid in scored:
            if str(eng.products[pid].get(boost["field"])).lower() == str(boost.get("equals")).lower():
                scored[pid] *= float(boost.get("weight", 1.0))
                fired[pid].append(f"{boost['field']} = {boost.get('equals')}: x{boost.get('weight', 1.0)}")
    r1 = order(scored)
    pos0 = {p: i + 1 for i, p in enumerate(r0)}
    rules = {"stage": "boosts", "kind": "rules", "explained": True,
             "notes": [f"{sum(1 for v in fired.values() if v)} products boosted"] if eng.spec.get("boosts") else
                      ["no boosts in this pipeline"],
             "items": [_item(p, i + 1, scored[p], {"before": base[p], "after": scored[p]}, fired[p],
                             moved=pos0[p] - (i + 1), title=title(p)) for i, p in enumerate(r1[:k])],
             "timing_ms": lap()}
    removed = []
    if limit is not None:
        keep = {p: s for p, s in scored.items() if _price(eng.products[p]) is not None and _price(eng.products[p]) <= limit}
        removed = [p for p in r1 if p not in keep]
        scored = keep
    r2 = order(scored)
    pos1 = {p: i + 1 for i, p in enumerate(r1)}
    filt = {"stage": "price filter", "kind": "filter", "explained": True,
            "notes": [f"removed {len(removed)} over {limit:g}"] if limit is not None else ["no filter applied"],
            "removed": [{"id": p, "title": title(p), "price": eng.products[p].get("price")} for p in removed[:50]],
            "items": [], "timing_ms": lap()}
    final = {"stage": "final ranking", "kind": "final", "explained": True, "notes": [],
             "items": [_item(p, i + 1, scored[p], fired=fired[p], moved=pos1.get(p, i + 1) - (i + 1), title=title(p))
                       for i, p in enumerate(r2[:k])],
             "timing_ms": lap(), "total_ms": round((time.perf_counter() - t0) * 1000, 3)}
    return [understand, retrieve, rules, filt, final]


def _es_parts(expl: dict, depth: int = 0, out=None) -> dict:
    """The leaves of an Elasticsearch explanation, by description, summed. Two levels deep is enough to read."""
    out = {} if out is None else out
    kids = expl.get("details") or []
    if not kids or depth >= 3:
        d = re.sub(r"\s+", " ", str(expl.get("description", "")))[:80]
        out[d] = out.get(d, 0.0) + float(expl.get("value") or 0)
        return out
    for k in kids:
        _es_parts(k, depth + 1, out)
    return out


def explain_elastic(eng, query: str, k: int = 50) -> list[dict]:
    import httpx

    from .workspace import get_secret

    t0 = time.perf_counter()
    h = {"Authorization": f"ApiKey {get_secret(eng.spec['api_key_secret'])}"} if eng.spec.get("api_key_secret") else {}
    body = {"size": k, "explain": True, "_source": ["title"],
            "query": {"multi_match": {"query": query, "fields": eng.spec.get("fields", ["*"])}}}
    r = httpx.post(f"{eng.spec['url'].rstrip('/')}/{eng.spec['index']}/_search", json=body, headers=h, timeout=60)
    r.raise_for_status()
    hits = r.json()["hits"]["hits"]
    items = []
    for i, hit in enumerate(hits):
        parts = _es_parts(hit.get("_explanation") or {})
        top = dict(sorted(parts.items(), key=lambda kv: -abs(kv[1]))[:8])
        items.append(_item(hit["_id"], i + 1, hit.get("_score"), top, title=(hit.get("_source") or {}).get("title")))
    return [{"stage": "Elasticsearch query", "kind": "retrieve", "explained": True,
             "notes": ["contributions from Elasticsearch's own explain, top 8 per hit"], "items": items,
             "timing_ms": round((time.perf_counter() - t0) * 1000, 2)},
            {"stage": "final ranking", "kind": "final", "explained": True, "notes": [],
             "items": [_item(x["id"], x["rank"], x["score"], title=x["title"]) for x in items], "timing_ms": None}]


def explain_engine(eng, query: str, k: int = 50) -> list[dict]:
    from .engines import ElasticEngine, LocalBM25

    if isinstance(eng, LocalBM25):
        return explain_bm25(eng, query, k)
    if isinstance(eng, ElasticEngine):
        return explain_elastic(eng, query, k)
    if hasattr(eng, "explain") and callable(eng.explain):  # a team's own engine (plan 0) may explain itself
        return eng.explain(query, k)
    t0 = time.perf_counter()
    ids = eng.search(query, k)
    return [{"stage": "search", "kind": "final", "explained": False,
             "notes": ["no explanation provided: this engine returns results without reasons. Implement explain() on "
                       "it to see inside."],
             "items": [_item(p, i + 1) for i, p in enumerate(ids)],
             "timing_ms": round((time.perf_counter() - t0) * 1000, 2)}]


def trace(ctx, pipeline: str, query: str, query_id: str | None = None, judgments: str | None = None,
          k: int = 50) -> dict:
    """One pipeline's trace for one query, with grades when a judgments dataset and query id are given."""
    from . import ops
    from .engines import make_engine

    pm = ctx.registry.resolve("pipeline", pipeline)[0]
    prods = ops.products_of(ctx, pm.catalog) if pm.catalog else None
    eng = make_engine(pm.engine, pm.spec, prods, workspace=ctx.workspace.name)
    stages = explain_engine(eng, query, k)
    grades = {}
    if judgments and query_id:
        grades = ops.judgments_map(ctx, judgments)[1].get(str(query_id), {})
        for s in stages:
            for it in s["items"]:
                it["grade"] = grades.get(it["id"])
    return {"pipeline": ops.ref(pm), "engine": pm.engine, "query": query, "query_id": query_id, "stages": stages,
            "graded": bool(grades)}


def first_divergence(a: dict, b: dict, grades: dict[str, int]) -> dict | None:
    """The first stage, in a's order, where a relevant item (grade >= 2) that a ranked is missing from b or ranked
    lower in b. Stages pair up by kind, and the final ranking always pairs."""
    rel = {p for p, g in grades.items() if g >= 2}
    if not rel:
        return None
    by_kind = {s["kind"]: s for s in b["stages"]}
    for s in a["stages"]:
        other = by_kind.get(s["kind"])
        if other is None or not s["items"]:
            continue
        pa = {it["id"]: it["rank"] for it in s["items"]}
        pb = {it["id"]: it["rank"] for it in other["items"]}
        lost = sorted((p for p in rel if p in pa and p not in pb), key=lambda p: pa[p])
        lower = sorted((p for p in rel if p in pa and p in pb and pb[p] > pa[p]), key=lambda p: pa[p])
        if lost or lower:
            return {"stage": s["stage"], "kind": s["kind"],
                    "lost": [{"id": p, "rank_a": pa[p]} for p in lost[:10]],
                    "ranked_lower": [{"id": p, "rank_a": pa[p], "rank_b": pb[p]} for p in lower[:10]]}
    return None


def compare_traces(ctx, a: str, b: str, query: str, query_id: str | None = None, judgments: str | None = None,
                   k: int = 50) -> dict:
    ta = trace(ctx, a, query, query_id, judgments, k)
    tb = trace(ctx, b, query, query_id, judgments, k)
    grades = {}
    if judgments and query_id:
        from . import ops

        grades = ops.judgments_map(ctx, judgments)[1].get(str(query_id), {})
    return {"a": ta, "b": tb, "first_divergence": first_divergence(ta, tb, grades),
            "graded": bool(grades), "note": None if grades else
            "No grades for this query, so Captain can't say where a relevant result was lost. Pick a query with "
            "judgments, or grade it."}



def from_run(ctx, run_ref: str, query_id: str, a: str | None = None, b: str | None = None, k: int = 50) -> dict:
    """Explain one query of a comparison run: its two arms, with the run's judgments and the query's text."""
    from . import compare as CP

    run = CP._run(ctx, run_ref)
    a, b = CP._arms(run, a, b)
    bt, qs, _ = CP._context(ctx, run)
    q = qs.get(str(query_id))
    if q is None:
        raise ValueError(f"query {query_id} isn't in this run")
    for arm in (a, b):
        try:
            ctx.registry.resolve("pipeline", arm)
        except Exception:  # noqa: BLE001
            raise ValueError(f"{arm} isn't a Captain pipeline (it came from imported rankings), so Captain can't "
                             "replay it to explain it. Import its traces, or rebuild it as a pipeline.") from None
    return compare_traces(ctx, a, b, q["query"], str(query_id), bt.judgments, k)
