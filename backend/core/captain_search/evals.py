"""Living evaluation sets (plan 4): notice when a query set and its grades stop telling the truth, and refresh them as
new versions.

health() reports, in plain sentences with the numbers behind them:
- **Representativeness:** how much of recent search traffic (an events dataset with a query column) the set's queries
  cover, overall and by traffic band (head: the queries making the first 50% of searches, torso: the next 30%, tail:
  the rest), and the most-searched queries the set has never seen.
- **Staleness:** grades on products that left the catalog, products whose title, price or stock changed since they
  were graded, relevant grades on products now out of stock, and grades older than the set's lifetime.
- **Coverage:** for each run scored with these grades, the share of each arm's top 10 that has a grade.
- **Reliability:** agreement between graders when the same pair was graded more than once. With one grader per pair,
  it says agreement can't be measured, instead of inventing a number.

refresh() never edits a version. It publishes a new query set version (the old queries plus new ones sampled from
traffic, stratified by band with a recorded seed, in a new split `added`, so a sealed holdout stays sealed) and a
new grades version without the retired products, and it returns the pairs that need grading, with a changelog.
"""
from __future__ import annotations

import random
import re
from collections import Counter, defaultdict

LIFETIME_DAYS = 180


def _norm(q) -> str:
    return re.sub(r"\s+", " ", str(q or "").lower().strip())


def _bands(counts: Counter) -> dict[str, set]:
    total = sum(counts.values()) or 1
    out, run = {"head": set(), "torso": set(), "tail": set()}, 0
    for q, n in counts.most_common():
        band = "head" if run < 0.5 * total else "torso" if run < 0.8 * total else "tail"
        out[band].add(q)
        run += n
    return out


def _log_queries(ctx, logs: str) -> Counter:
    from .ops import rows_of

    _, rows, _ = rows_of(ctx, logs)
    col = next((c for c in ("query", "search_term", "query_text", "q") if rows and c in rows[0]), None)
    if col is None:
        raise ValueError(f"{logs} has no query column, so traffic can't be compared")
    return Counter(_norm(r[col]) for r in rows if r.get(col))


def _catalog_at(ctx, catalog: str, when):
    """The newest catalog version created at or before `when` (None if every version is newer)."""
    name = catalog.split("@")[0]
    best = None
    for v in ctx.registry.versions("dataset", name):
        m = ctx.registry.load("dataset", name, v)
        if m.created_at <= when:
            best = m
    return best


def health(ctx, queries: str, judgments: str, logs: str | None = None, catalog: str | None = None,
           lifetime_days: int = LIFETIME_DAYS) -> dict:
    import datetime as dt

    from . import ops
    from .data import datasets as DS

    qm, qs, _ = ops.rows_of(ctx, queries)
    jm, jmap = ops.judgments_map(ctx, judgments)
    set_q = {_norm(q["query"]) for q in qs}
    findings, out = [], {"queries": f"{qm.name}@v{qm.version}", "judgments": f"{jm.name}@v{jm.version}",
                        "n_queries": len(qs), "n_grades": sum(len(v) for v in jmap.values())}
    # representativeness
    if logs:
        counts = _log_queries(ctx, logs)
        total = sum(counts.values()) or 1
        bands = _bands(counts)
        cov = {b: round(sum(counts[q] for q in qs_ if q in set_q) / max(1, sum(counts[q] for q in qs_)), 3)
               for b, qs_ in bands.items()}
        seen = sum(n for q, n in counts.items() if q in set_q) / total
        unseen = [{"query": q, "searches": n} for q, n in counts.most_common() if q not in set_q][:20]
        stale_q = sorted(q for q in set_q if q not in counts)
        out["representativeness"] = {"traffic_covered": round(seen, 3), "by_band": cov, "top_unseen": unseen,
                                     "set_queries_not_searched": len(stale_q), "logs": logs,
                                     "distinct_searched": len(counts)}
        if seen < 0.3:
            findings.append(f"The set's queries cover {seen:.0%} of recent searches. Add queries from traffic.")
        if cov["head"] < 0.5:
            findings.append(f"Only {cov['head']:.0%} of head traffic (the most-searched queries) is in the set.")
        if len(stale_q) > 0.5 * len(set_q):
            findings.append(f"{len(stale_q)} of the set's {len(set_q)} queries weren't searched in these logs.")
    # staleness
    graded = {(q, p): g for q, d in jmap.items() for p, g in d.items()}
    stale = {"retired": [], "changed": [], "out_of_stock_relevant": [], "age_days": None}
    age = (dt.datetime.now(dt.UTC) - jm.created_at).days
    stale["age_days"] = age
    if age > lifetime_days:
        findings.append(f"The grades are {age} days old, past the set's {lifetime_days}-day lifetime.")
    if catalog:
        now_m = ctx.registry.resolve("dataset", catalog)[0]
        now = {str(p["id"]): p for p in DS.load_table(ctx, now_m).to_pylist()}
        then_m = _catalog_at(ctx, catalog, jm.created_at)
        then = {str(p["id"]): p for p in DS.load_table(ctx, then_m).to_pylist()} if then_m and then_m.version != now_m.version else None
        for (q, p), g in graded.items():
            if p not in now:
                stale["retired"].append({"query_id": q, "product_id": p, "grade": g})
                continue
            if then and p in then and any(str(then[p].get(f)) != str(now[p].get(f)) for f in ("title", "price", "in_stock")):
                stale["changed"].append({"query_id": q, "product_id": p, "grade": g,
                                         "fields": [f for f in ("title", "price", "in_stock")
                                                    if str(then[p].get(f)) != str(now[p].get(f))]})
            if g >= 2 and str(now[p].get("in_stock", "true")).lower() in ("false", "0", "no"):
                stale["out_of_stock_relevant"].append({"query_id": q, "product_id": p, "grade": g})
        stale["catalog_now"] = f"{now_m.name}@v{now_m.version}"
        stale["catalog_when_graded"] = f"{then_m.name}@v{then_m.version}" if then_m else None
        for key, what in (("retired", "products no longer in the catalog"),
                          ("changed", "products whose title, price or stock changed since rating"),
                          ("out_of_stock_relevant", "relevant products now out of stock")):
            if stale[key]:
                findings.append(f"{len(stale[key])} grades are on {what}.")
        for key in ("retired", "changed", "out_of_stock_relevant"):
            stale[f"{key}_count"] = len(stale[key])
            stale[key] = stale[key][:50]
    out["staleness"] = stale
    # coverage, from runs scored with these grades
    cov_rows = []
    for name in ctx.registry.names("backtest-run"):
        try:
            bt = ctx.registry.load("backtest", name)
        except Exception:  # noqa: BLE001
            continue
        if not bt.judgments or bt.judgments.split("@")[0] != jm.name:
            continue
        run = ctx.registry.load("backtest-run", name)
        for a in run.arms:
            if a.judged_at_10 is not None:
                cov_rows.append({"run": f"{run.name}@v{run.version}", "arm": a.arm, "judged_at_10": a.judged_at_10})
                if a.judged_at_10 < 0.9:
                    findings.append(f"In {run.name}, {a.arm} has grades for only {a.judged_at_10:.0%} of its top 10.")
    out["coverage"] = cov_rows
    # reliability: the same pair graded more than once
    _, rows, _ = ops.rows_of(ctx, f"{jm.name}@v{jm.version}")
    by_pair = defaultdict(list)
    who = next((c for c in ("grader", "created_by", "annotator") if rows and c in rows[0]), None)
    for r in rows:
        by_pair[(str(r["query_id"]), str(r["product_id"]))].append((r.get(who) if who else None, int(r["grade"])))
    multi = [v for v in by_pair.values() if len({w for w, _ in v}) > 1]
    if multi:
        agree = sum(1 for v in multi if len({g for _, g in v}) == 1) / len(multi)
        out["reliability"] = {"pairs_graded_twice": len(multi), "exact_agreement": round(agree, 3)}
        if agree < 0.7:
            findings.append(f"Raters agree on only {agree:.0%} of the pairs rated twice.")
    else:
        out["reliability"] = {"pairs_graded_twice": 0, "exact_agreement": None,
                              "note": "Each pair has one grade, so agreement can't be measured. Grade a sample twice "
                                      "(cst grade) to know how far to trust these grades."}
    out["source"] = jm.judgment_source
    out["findings"] = findings
    out["status"] = "needs refresh" if findings else "healthy"
    return out


def refresh(ctx, queries: str, judgments: str, logs: str, catalog: str | None = None, add: int = 50,
            seed: int = 7, dry_run: bool = False) -> dict:
    import pyarrow as pa

    from . import ops
    from .data import datasets as DS
    from .model.entities import DatasetKind, Provenance

    h = health(ctx, queries, judgments, logs, catalog)
    qm, qs, _ = ops.rows_of(ctx, queries.split(":")[0])
    jm, _ = ops.judgments_map(ctx, judgments)
    counts = _log_queries(ctx, logs)
    have = {_norm(q["query"]) for q in qs}
    bands = _bands(counts)
    rng = random.Random(seed)
    share = {"head": 0.5, "torso": 0.3, "tail": 0.2}
    picked = []
    for band, frac in share.items():
        pool = sorted(q for q in bands[band] if q not in have)
        rng.shuffle(pool)
        picked += [(band, q) for q in pool[: max(0, round(add * frac))]]
    new_rows = [{**{k: q.get(k) for k in qs[0]}} for q in qs] if qs else []
    import hashlib

    added = []
    for band, q in picked:
        qid = "q-" + hashlib.sha1(q.encode()).hexdigest()[:12]
        row = {k: None for k in (qs[0] if qs else {"query_id": 1, "query": 1})}
        row.update({"query_id": qid, "query": q})
        if "class" in row:
            row["class"] = f"traffic-{band}"
        new_rows.append(row)
        added.append({"query_id": qid, "query": q, "band": band, "searches": counts[q]})
    retired = {(r["query_id"], r["product_id"]) for r in h["staleness"].get("retired", [])}
    regrade = h["staleness"].get("changed", []) + h["staleness"].get("out_of_stock_relevant", [])
    _, jrows, _ = ops.rows_of(ctx, f"{jm.name}@v{jm.version}")
    keep_j = [r for r in jrows if (str(r["query_id"]), str(r["product_id"])) not in retired]
    changelog = [f"added {len(added)} queries from {logs} (seed {seed}): "
                 + ", ".join(f"{b} {sum(1 for x in added if x['band'] == b)}" for b in share),
                 f"removed {len(jrows) - len(keep_j)} grades on retired products",
                 f"{len(regrade)} grades to redo (changed or out-of-stock products)",
                 f"{len(added)} new queries to grade"]
    plan = {"health": h, "added_queries": added, "regrade": regrade[:200], "changelog": changelog}
    if dry_run:
        return plan
    splits = dict(qm.splits or {})
    splits["added"] = [a["query_id"] for a in added]
    nq = DS.snapshot(ctx, qm.name, pa.Table.from_pylist(new_rows), kind=DatasetKind.queries, dry_run=True,
                     provenance=Provenance(source="generated", params={"refresh_of": f"{qm.name}@v{qm.version}",
                                                                       "logs": logs, "seed": seed, "changelog": changelog}),
                     description=f"Refreshed from traffic: {'; '.join(changelog)}")
    entry = ctx.registry.put_blob(DS.to_parquet(pa.Table.from_pylist(new_rows)), "data/part-0.parquet")
    nq = ctx.registry.publish(nq.model_copy(update={"files": [entry], "splits": splits,
                                                    "holdout_seal": qm.holdout_seal}))
    nj = DS.snapshot(ctx, jm.name, pa.Table.from_pylist(keep_j), kind=DatasetKind.judgments, dry_run=True,
                     provenance=Provenance(source="generated", params={"refresh_of": f"{jm.name}@v{jm.version}",
                                                                       "changelog": changelog}),
                     description=f"Refreshed: {'; '.join(changelog[1:3])}")
    jentry = ctx.registry.put_blob(DS.to_parquet(pa.Table.from_pylist(keep_j)), "data/part-0.parquet")
    nj = ctx.registry.publish(nj.model_copy(update={"files": [jentry], "judgment_source": jm.judgment_source}))
    return {**plan, "queries": f"{nq.name}@v{nq.version}", "judgments": f"{nj.name}@v{nj.version}"}
