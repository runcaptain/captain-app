"""Human grading: build a queue of (query, product) pairs, let people grade them, publish the grades.

Building the evaluation set is often most of the work, and a live promote here needs
HUMAN_GRADED evidence. This turns that into a queue.

A queue holds the pairs the given pipelines put in their top `depth` for each query, deduplicated, in a
fixed shuffled order, optionally with an overlap share that every grader sees (to measure agreement).
Each grader's grades live in their own file, so two people never write the same object:
  grading/<queue>/queue.yaml           pairs, products shown, settings
  grading/<queue>/grader-<who>.yaml    {grades: {pair_id: {grade, note, at}}}
Grades are 0 (irrelevant), 1 (partial), 2 (relevant), 3 (exact). Publishing writes a judgments dataset with
judgment_source human, so backtests that score with it are HUMAN_GRADED. When several people graded the
same pair, the published grade is their median (rounded down), and agreement is reported as weighted kappa.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random
import re

import pyarrow as pa
import yaml

from .storage.backend import AlreadyExists, Conflict, NotFound
from .storage.registry import check_name, dump_yaml
from .workspace import Context, whoami

GRADES = {0: "irrelevant", 1: "partial", 2: "relevant", 3: "exact"}
SHOW = ("title", "brand", "category", "price", "description", "image_url", "url")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _qkey(name: str) -> str:
    return f"grading/{name}/queue.yaml"


def _gkey(name: str, who: str) -> str:
    return f"grading/{name}/grader-{re.sub(r'[^A-Za-z0-9._-]', '_', who)}.yaml"


def _pid(qid: str, product: str) -> str:
    return hashlib.sha1(f"{qid}\x00{product}".encode()).hexdigest()[:12]


def _rubric(ctx, rubric: str | None):
    """The rubric graders see on every pair, stored with the queue so it can't drift under them."""
    if not rubric:
        return None
    from .rubrics import resolve

    r = resolve(rubric, ctx.workspace.name)
    return {"name": r["name"], "sha": r["sha"], "text": r["text"]}


def create(ctx: Context, name: str, queries: str, pipelines: list[str], depth: int = 10, sample: int | None = None,
           overlap: float = 0.1, seed: int = 7, dry_run: bool = False, rubric: str | None = None) -> dict:
    from .ops import _rankings, ref, rows_of

    check_name(name)
    if not 1 <= depth <= 50:
        raise ValueError("depth is between 1 and 50")
    if not 0 <= overlap <= 1:
        raise ValueError("overlap is a share between 0 and 1")
    qm, qs, _ = rows_of(ctx, queries)
    rng = random.Random(seed)
    if sample and sample < len(qs):
        qs = rng.sample(qs, sample)
    pairs, seen, products = [], set(), {}
    for p in pipelines:
        pm, _, prods, ranks, _ = _rankings(ctx, p, qs, k=depth)
        pmap = {x["id"]: x for x in (prods or [])}
        qtext = {q["query_id"]: q["query"] for q in qs}
        for qid, ids in ranks.items():
            for rank, pid in enumerate(ids[:depth], 1):
                k = _pid(qid, pid)
                if k in seen:
                    continue
                seen.add(k)
                pairs.append({"id": k, "query_id": qid, "query": qtext[qid], "product_id": pid, "first_seen": f"{ref(pm)} #{rank}"})
                if pid in pmap and pid not in products:
                    products[pid] = {f: pmap[pid].get(f) for f in SHOW if pmap[pid].get(f) is not None}
    if not pairs:
        raise ValueError("the pipelines returned nothing to grade for these queries")
    rng.shuffle(pairs)
    n_over = int(round(len(pairs) * overlap))
    for i, p in enumerate(pairs):
        p["overlap"] = i < n_over
    doc = {"name": name, "queries": queries, "pipelines": pipelines, "depth": depth, "overlap": overlap, "seed": seed, "rubric": _rubric(ctx, rubric),
           "created_by": whoami(), "created_at": _now(), "pairs": pairs, "products": products, "published": []}
    if dry_run:
        return {k: v for k, v in doc.items() if k not in ("pairs", "products")} | {"pairs": len(pairs)}
    try:
        ctx.store.put_new(_qkey(name), dump_yaml(doc))
    except AlreadyExists:
        raise ValueError(f"a grading queue named {name} already exists") from None
    return status(ctx, name)


def _queue(ctx: Context, name: str):
    try:
        data, tag = ctx.store.get_with_tag(_qkey(name))
    except NotFound:
        raise NotFound(f"no grading queue {name}") from None
    return yaml.safe_load(data), tag


def _grader_files(ctx: Context, name: str) -> dict[str, dict]:
    out = {}
    for k in ctx.store.list(f"grading/{name}/"):
        if k.rsplit("/", 1)[-1].startswith("grader-"):
            d = yaml.safe_load(ctx.store.get(k)) or {}
            out[d.get("grader") or k] = d.get("grades", {})
    return out


def listing(ctx: Context) -> list[dict]:
    names = sorted({k.split("/")[1] for k in ctx.store.list("grading/") if k.endswith("/queue.yaml")})
    return [status(ctx, n) for n in names]


def next_pair(ctx: Context, name: str, grader: str | None = None) -> dict:
    """The next pair this grader has not graded: overlap pairs first, then pairs nobody has graded."""
    who = grader or whoami()
    q, _ = _queue(ctx, name)
    files = _grader_files(ctx, name)
    mine = files.get(who, {})
    anyone = {pid for g in files.values() for pid in g}
    todo = [p for p in q["pairs"] if p["id"] not in mine]
    pick = next((p for p in todo if p["overlap"]), None) or next((p for p in todo if p["id"] not in anyone), None)
    left = sum(1 for p in todo if p["overlap"] or p["id"] not in anyone)
    if pick is None:
        return {"queue": name, "grader": who, "done": True, "remaining": 0}
    return {"queue": name, "grader": who, "done": False, "remaining": left, "pair": pick,
            "product": q["products"].get(pick["product_id"], {}), "scale": GRADES}


def grade(ctx: Context, name: str, pair_id: str, value: int, note: str = "", grader: str | None = None) -> dict:
    who = grader or whoami()
    if int(value) not in GRADES:
        raise ValueError("a grade is 0 (irrelevant), 1 (partial), 2 (relevant) or 3 (exact)")
    q, _ = _queue(ctx, name)
    if pair_id not in {p["id"] for p in q["pairs"]}:
        raise ValueError(f"pair {pair_id} is not in queue {name}")
    key = _gkey(name, who)
    try:
        data, tag = ctx.store.get_with_tag(key)
        doc = yaml.safe_load(data) or {}
    except NotFound:
        doc, tag = {"grader": who, "grades": {}}, None
    doc["grades"][pair_id] = {"grade": int(value), "note": note, "at": _now()}
    try:
        ctx.store.put_if(key, dump_yaml(doc), tag)
    except Conflict:
        raise Conflict("your grades changed in another window at the same moment. Reload and try again.") from None
    return {"queue": name, "pair": pair_id, "grade": int(value), "grader": who}


def status(ctx: Context, name: str) -> dict:
    from .eval.stats import quadratic_kappa

    q, _ = _queue(ctx, name)
    files = _grader_files(ctx, name)
    graded = {pid for g in files.values() for pid in g}
    # agreement on pairs at least two people graded
    xs, ys = [], []
    people = list(files)
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            for pid in set(files[a]) & set(files[b]):
                xs.append(files[a][pid]["grade"])
                ys.append(files[b][pid]["grade"])
    return {"name": name, "queries": q["queries"], "pipelines": q["pipelines"], "depth": q["depth"], "pairs": len(q["pairs"]), "rubric": q.get("rubric"),
            "graded": len(graded), "share": round(len(graded) / len(q["pairs"]), 4),
            "graders": {who: len(g) for who, g in files.items()}, "agreement_pairs": len(xs),
            "kappa": round(quadratic_kappa(xs, ys), 4) if len(xs) >= 10 else None,
            "created_by": q["created_by"], "created_at": q["created_at"], "published": q.get("published", [])}


def publish(ctx: Context, name: str, into: str, dry_run: bool = False):
    from .data.datasets import snapshot, to_parquet
    from .model.entities import DatasetKind, Provenance

    q, tag = _queue(ctx, name)
    files = _grader_files(ctx, name)
    by_pair: dict[str, list[int]] = {}
    for g in files.values():
        for pid, v in g.items():
            by_pair.setdefault(pid, []).append(v["grade"])
    pmap = {p["id"]: p for p in q["pairs"]}
    rows = []
    for pid, vs in by_pair.items():
        vs = sorted(vs)
        p = pmap[pid]
        rows.append({"query_id": p["query_id"], "query": p["query"], "product_id": p["product_id"],
                     "grade": vs[(len(vs) - 1) // 2], "graders": len(vs), "source": "human"})
    if not rows:
        raise ValueError(f"nobody has graded anything in {name} yet")
    st = status(ctx, name)
    t = pa.Table.from_pylist(rows)
    m = snapshot(ctx, check_name(into), t, kind=DatasetKind.judgments, dry_run=True,
                 provenance=Provenance(source="generated", params={"grading_queue": name, "queries": q["queries"], "pipelines": list(q.get("pipelines") or []),
                                                                     "graders": sorted(files),
                                                                     "kappa": st["kappa"], "coverage": st["share"]}),
                 description=f"{len(rows)} human grades from queue {name} by {len(files)} grader(s)")
    m = m.model_copy(update={"judgment_source": "human", "judge": {"model": "human", "prompt_version": f"queue {name}"}})
    if dry_run:
        return m
    e = ctx.registry.put_blob(to_parquet(t), "data/part-0.parquet")
    m = ctx.registry.publish(m.model_copy(update={"files": [e]}))
    q["published"] = q.get("published", []) + [{"at": _now(), "by": whoami(), "dataset": f"{m.name}@v{m.version}", "rows": len(rows)}]
    try:
        ctx.store.put_if(_qkey(name), dump_yaml(q), tag)
    except Conflict:
        pass  # the dataset is published either way; the queue's note of it is best effort
    return m
