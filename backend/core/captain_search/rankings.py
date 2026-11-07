"""Bring your own rankings (spec S2): import what a team's own search returned, and compare it like any backtest.

The contract is one long table, csv, tsv, parquet, json or jsonl, with one row per (query, arm, item):

  query_id   required  the query's id, the same across arms
  arm        required  which system or version returned it (e.g. "prod", "candidate-v2")
  item_id    required  the result's id (product, document)
  rank       one of    1-based position. Or give `score`, and rank is derived (highest first)
  score      one of
  query      optional  the query text, for reading results side by side
  label      optional  a relevance grade 0 to 3 for (query, item). Needed for nDCG, P@10, R@50 and MRR, unless a
                       judgments dataset is given instead
  segment    optional  a slice to break results down by (device, market, query class)
  weight     optional  per-query traffic weight, for traffic-weighted nDCG

Column names can be mapped (`mapping={"query_id": "qid", ...}`). validate() reports problems in plain words and
never guesses. import_rankings() versions the raw file, builds query and label datasets, and publishes a
BacktestRun with every arm's rankings stored, so comparisons, slices and side by side all work. Analysis the data can't
support (no labels, so no nDCG) is marked unavailable, not filled in.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

import pyarrow as pa

REQUIRED = ("query_id", "arm", "item_id")
OPTIONAL = ("rank", "score", "query", "label", "segment", "weight")
LABEL_SOURCES = {"human": "human", "llm": "llm", "clicks": "click", "click": "click", "heuristic": "heuristic"}


def _rename(t: pa.Table, mapping: dict | None) -> pa.Table:
    mapping = {k: v for k, v in (mapping or {}).items() if v}
    back = {v: k for k, v in mapping.items()}
    return t.rename_columns([back.get(c, c) for c in t.column_names])


def validate(t: pa.Table, mapping: dict | None = None, judgments: bool = False) -> dict:
    """{ok, errors, warnings, stats, available}. Errors stop an import; warnings don't."""
    t = _rename(t, mapping)
    cols = set(t.column_names)
    errors, warnings = [], []
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        errors.append(f"Missing column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
                      f"The file has: {', '.join(t.column_names)}. Map yours with mapping.")
    if "rank" not in cols and "score" not in cols:
        errors.append("Give a rank (1 is the top) or a score (higher is better) for each row.")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "stats": {}, "available": {}}
    rows = t.to_pylist()
    nulls = Counter(c for r in rows for c in REQUIRED if r.get(c) in (None, ""))
    for c, n in nulls.items():
        errors.append(f"{n} row(s) have no {c}.")
    seen, dupes = set(), 0
    for r in rows:
        k = (str(r["query_id"]), str(r["arm"]), str(r["item_id"]))
        dupes += k in seen
        seen.add(k)
    if dupes:
        errors.append(f"{dupes} row(s) repeat the same item for the same query and arm.")
    if "rank" in cols:
        bad = [r["rank"] for r in rows if r.get("rank") is not None and not _is_pos_int(r["rank"])]
        if bad:
            errors.append(f"{len(bad)} rank(s) aren't whole numbers from 1 (e.g. {bad[0]!r}).")
        ranks = defaultdict(list)
        for r in rows:
            if _is_pos_int(r.get("rank")):
                ranks[(str(r["query_id"]), str(r["arm"]))].append(int(r["rank"]))
        tied = sum(1 for v in ranks.values() if len(v) != len(set(v)))
        if tied:
            errors.append(f"{tied} (query, arm) pair(s) give two items the same rank.")
    elif any(not isinstance(r.get("score"), (int, float)) for r in rows):
        errors.append("Every score must be a number.")
    if "label" in cols:
        bad = [r["label"] for r in rows if r.get("label") is not None and r["label"] not in (0, 1, 2, 3)]
        if bad:
            errors.append(f"Labels must be grades 0 to 3. {len(bad)} aren't (e.g. {bad[0]!r}).")
        conflict = defaultdict(set)
        for r in rows:
            if r.get("label") is not None:
                conflict[(str(r["query_id"]), str(r["item_id"]))].add(r["label"])
        n = sum(1 for v in conflict.values() if len(v) > 1)
        if n:
            errors.append(f"{n} (query, item) pair(s) carry different labels in different arms.")
    arms = sorted({str(r["arm"]) for r in rows if r.get("arm") not in (None, "")})
    by_arm = defaultdict(set)
    for r in rows:
        by_arm[str(r["arm"])].add(str(r["query_id"]))
    allq = set().union(*by_arm.values()) if by_arm else set()
    if len(arms) < 2:
        warnings.append(f"Only one arm ({arms[0] if arms else 'none'}): there's nothing to compare it with.")
    for a in arms:
        miss = len(allq - by_arm[a])
        if miss:
            warnings.append(f"{a} has no results for {miss} of {len(allq)} queries. They count as empty results.")
    has_labels = "label" in cols and any(r.get("label") is not None for r in rows)
    available = {
        "relevance metrics": has_labels or judgments,
        "per-query side by side": True,
        "slices": "segment" in cols,
        "traffic-weighted nDCG": "weight" in cols and (has_labels or judgments),
    }
    if not available["relevance metrics"]:
        warnings.append("No labels and no judgments dataset: nDCG, P@10, R@50 and MRR will be unavailable. Rankings "
                        "can still be compared side by side, and graded later.")
    if has_labels:
        labelled = {(str(r["query_id"]), str(r["item_id"])) for r in rows if r.get("label") is not None}
        for a in arms:
            top = [r for r in rows if str(r["arm"]) == a and _top10(r)]
            cov = sum((str(r["query_id"]), str(r["item_id"])) in labelled for r in top) / max(1, len(top))
            if cov < 0.9:
                warnings.append(f"Only {cov:.0%} of {a}'s top 10 results have a label. Its nDCG will read low until "
                                "the rest are graded.")
    stats = {"rows": len(rows), "queries": len(allq), "arms": arms,
             "results_per_query": round(len(rows) / max(1, len(allq) * max(1, len(arms))), 1)}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats, "available": available}


def _is_pos_int(v) -> bool:
    try:
        return int(v) == float(v) and int(v) >= 1
    except (TypeError, ValueError):
        return False


def _top10(r) -> bool:
    return _is_pos_int(r.get("rank")) and int(r["rank"]) <= 10 if "rank" in r else True


def _ranked(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """arm -> query_id -> item ids in rank order."""
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = int(r["rank"]) if r.get("rank") is not None else -float(r["score"])
        by[str(r["arm"])][str(r["query_id"])].append((key, str(r["item_id"])))
    return {a: {q: [i for _, i in sorted(v)] for q, v in qs.items()} for a, qs in by.items()}


def import_rankings(ctx, name: str, source: str, *, mapping: dict | None = None, baseline: str | None = None,
                    labels_from: str | None = None, judgments: str | None = None, primary_metric: str = "ndcg@10",
                    mde: float = 0.003, hypothesis: str = "", project: str | None = None,
                    catalog: str | None = None) -> dict:
    """source: a file path or a dataset ref. Returns {run, backtest, datasets, validation}."""
    from . import ops
    from .data import datasets as DS
    from .eval import metrics as M
    from .eval import stats as ST
    from .model.entities import (ArmResult, Backtest, BacktestRun, DatasetKind, Evidence, Preregistration,
                                 Provenance)
    from .segments import segments as seg_rows
    from .storage.registry import check_name
    from .workspace import whoami

    name = check_name(name)
    if os.path.exists(os.path.expanduser(source)):
        raw = DS.read_file(os.path.expanduser(source))
        prov = Provenance(source="file", file=os.path.abspath(os.path.expanduser(source)))
    else:
        m = ctx.registry.resolve("dataset", source)[0]
        raw = DS.load_table(ctx, m)
        prov = Provenance(source="import", params={"dataset": ops.ref(m)})
    rep = validate(raw, mapping, judgments=bool(judgments))
    if not rep["ok"]:
        raise ValueError("The rankings can't be imported: " + " ".join(rep["errors"]))
    t = _rename(raw, mapping)
    rows = t.to_pylist()
    has_labels = "label" in t.column_names and any(r.get("label") is not None for r in rows)
    if has_labels and labels_from not in LABEL_SOURCES:
        raise ValueError("Say where the labels come from (labels_from: human, llm, clicks or heuristic). Captain labels "
                         "every result with its evidence, so it won't guess.")
    arms = rep["stats"]["arms"]
    base = baseline or arms[0]
    if base not in arms:
        raise ValueError(f"{base} isn't an arm in the file. Arms: {', '.join(arms)}")
    order = [base] + [a for a in arms if a != base]
    made = {}
    # the raw file, versioned
    made["rankings"] = ops.ref(DS.snapshot(ctx, f"rankings/{name}", raw, kind=DatasetKind.generic, provenance=prov,
                                           description=f"Ranked results for {len(arms)} arms, imported"))
    # queries
    qmeta = {}
    for r in rows:
        q = qmeta.setdefault(str(r["query_id"]), {"query_id": str(r["query_id"]), "query": None, "class": None,
                                                  "weight": None})
        q["query"] = q["query"] or (str(r["query"]) if r.get("query") not in (None, "") else None)
        q["class"] = q["class"] or (str(r["segment"]) if r.get("segment") not in (None, "") else None)
        q["weight"] = q["weight"] if q["weight"] is not None else (float(r["weight"]) if r.get("weight") is not None else None)
    qs = sorted(qmeta.values(), key=lambda q: q["query_id"])
    for q in qs:
        q["query"] = q["query"] or q["query_id"]
    qm = DS.snapshot(ctx, f"rankings/{name}-queries", pa.Table.from_pylist(qs), kind=DatasetKind.queries,
                     provenance=Provenance(source="import", params={"from": made["rankings"]}),
                     description="Queries from the imported rankings")
    made["queries"] = ops.ref(qm)
    # labels -> a judgments dataset
    jref = judgments
    if has_labels:
        seen, jrows = set(), []
        for r in rows:
            k = (str(r["query_id"]), str(r["item_id"]))
            if r.get("label") is not None and k not in seen:
                seen.add(k)
                jrows.append({"query_id": k[0], "product_id": k[1], "grade": int(r["label"])})
        jm = DS.snapshot(ctx, f"rankings/{name}-labels", pa.Table.from_pylist(jrows), kind=DatasetKind.judgments,
                         provenance=Provenance(source="import", params={"from": made["rankings"]}), dry_run=True,
                         description=f"Labels from the imported rankings ({labels_from})")
        jm = jm.model_copy(update={"judgment_source": LABEL_SOURCES[labels_from]})
        entry = ctx.registry.put_blob(DS.to_parquet(pa.Table.from_pylist(jrows)), "data/part-0.parquet")
        jm = ctx.registry.publish(jm.model_copy(update={"files": [entry]}))
        jref = made["labels"] = ops.ref(jm)
    jmeta, jmap = ops.judgments_map(ctx, jref) if jref else (None, {})
    from .eval.judge import EVIDENCE

    ev = EVIDENCE.get(jmeta.judgment_source or "heuristic", Evidence.SIMULATED) if jmeta else Evidence.SIMULATED
    ranked = _ranked(rows)
    allq = [q["query_id"] for q in qs]
    weights = {q["query_id"]: q["weight"] for q in qs if q["weight"] is not None} or None
    arm_results, perq = [], {}
    for a in order:
        ranks = {q: ranked.get(a, {}).get(q, []) for q in allq}
        pq = M.per_query(ranks, jmap) if jref else {}
        offline = M.summarize(pq) if jref else {}
        if jref and weights:
            tot = sum(weights.get(q, 0.0) for q in pq) or 1.0
            offline["ndcg@10_tw"] = round(sum(r["ndcg10"] * weights.get(q, 0.0) for q, r in pq.items()) / tot, 4)
        blob = ("\n".join(json.dumps({"query_id": q, "ids": v}) for q, v in sorted(ranks.items())) + "\n").encode()
        entry = ctx.registry.put_blob(blob, f"rankings/{a}.jsonl")
        perq[a] = pq
        arm_results.append(ArmResult(arm=a, offline=offline, judged_at_10=offline.get("judged@10") if jref else None,
                                     evidence=ev, logs=[entry]))
    comps = []
    if jref:
        for a in order[1:]:
            for metric in ("ndcg@10", "p@10", "r@50", "mrr"):
                key = M.NAMES[metric]
                c = ST.compare({q: r[key] for q, r in perq[base].items()}, {q: r[key] for q, r in perq[a].items()},
                               metric, mde if metric == primary_metric else 0.0, arm_a=base, arm_b=a)
                comps.append(c | {"evidence": ev.value, "primary": metric == primary_metric})
    pre = Preregistration(hypothesis=hypothesis or f"Imported rankings: {', '.join(order[1:])} against {base}.",
                          primary_metric=primary_metric if jref else "n/a", mde=mde)
    bt = ctx.registry.publish(Backtest(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(),
                                       name=name, version=0, mode="rankings", arms=order, queries=made["queries"],
                                       judgments=jref, preregistration=pre, metrics=["ndcg@10", "p@10", "r@50", "mrr"],
                                       description=f"Rankings imported from {made['rankings']}",
                                       tags={"source": made["rankings"],
                                             **({"catalog": ops.ref(ctx.registry.resolve("dataset", catalog)[0])}
                                                if catalog else {})}))
    files = [e for r in arm_results for e in r.logs]
    complete = all((r.judged_at_10 or 0) >= 0.9 for r in arm_results) if jref else False
    run = ctx.registry.publish(BacktestRun(
        org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0,
        backtest=ops.ref(bt), mode="rankings", source=made["rankings"], arms=arm_results, comparisons=comps,
        segments=seg_rows(qs, perq, order, mde) if jref else [], complete=complete, independent=True, files=files,
        description=f"{len(allq)} queries, {len(order)} arms, imported rankings"
                    + ("" if jref else ". No labels yet, so relevance metrics are unavailable")))
    if project:
        from . import projects as PR

        PR.assign(ctx, project, [{"kind": "backtest", "name": name}] +
                  [{"kind": "dataset", "name": v.split("@")[0]} for v in made.values()])
    return {"run": ops.ref(run), "backtest": ops.ref(bt), "datasets": made, "validation": rep,
            "evidence": ev.value if jref else None}
