"""A person's own test set: their questions and the answers they want, turned into what an offline evaluation
scores against.

An offline evaluation needs two datasets: a query set (one row per question) and judgments (which product is a
good answer to which question, graded 0 to 3). A team that has "QA pairs" has one table: a question, a product
they want back, and sometimes a grade and a group. This reads that table from a dataset already in Captain or from
a file on this computer, lets the person say which column is which, shows what it would make, and makes the two
datasets:

    <name>/questions   kind queries: query_id, query, class (their slice column, else "own")
    <name>/answers     kind judgments: query_id, query, product_id, grade, source "human"

The answers are the person's own, so the judgments are marked human: a run scored with them reads HUMAN_GRADED.
Nothing here caps what is read: every row of the table is used, and the preview says how many were skipped and why.
"""
from __future__ import annotations

import os

import pyarrow as pa

from .data.datasets import load_table, read_file, snapshot, to_parquet
from .eval import queries as Q
from .model.entities import DatasetKind, Provenance
from .workspace import Context

GRADE_WORDS = {0: "Irrelevant", 1: "Partial", 2: "Relevant", 3: "Exact"}
# Column names people use, to guess the mapping. The person can change every one.
GUESS = {
    "query": ("query", "question", "search", "search_term", "keyword", "q", "text"),
    "product": ("product_id", "wanted", "expected", "answer", "product", "item_id", "sku", "doc_id", "id"),
    "grade": ("grade", "relevance", "label", "rating", "score"),
    "slice": ("class", "slice", "segment", "category", "group", "kind", "type"),
}


def _table(ctx: Context, source: dict) -> tuple[pa.Table, str]:
    """The table a source names, and what to call it. source: {dataset: "name[@vN]"} or {file: "/path"}."""
    if source.get("dataset"):
        m, _ = ctx.registry.resolve("dataset", str(source["dataset"]))
        return load_table(ctx, m), f"{m.name}@v{m.version}"
    path = os.path.expanduser(str(source.get("file") or "").strip())
    if not path:
        raise ValueError("pick a table from Data, or give a file: CSV, TSV, Parquet, JSON or JSON lines")
    if not os.path.isfile(path):
        raise ValueError(f"there is no file at {path}")
    return read_file(path), os.path.basename(path)


def guess(columns: list[str]) -> dict:
    """Which column is which, from its name. Unmatched ones are left out."""
    low = {c.lower(): c for c in columns}
    out = {}
    for role, names in GUESS.items():
        hit = next((low[n] for n in names if n in low and low[n] not in out.values()), None)
        if hit:
            out[role] = hit
    return out


def _grade(v, default: int) -> int | None:
    if v is None or v == "":
        return default
    try:
        g = int(float(v))
    except (TypeError, ValueError):
        return None
    return g if 0 <= g <= 3 else None


def _pairs(rows: list[dict], mapping: dict, default_grade: int) -> tuple[list[dict], list[dict], dict]:
    """(questions, answers, skipped {reason: count}) for those rows under that mapping."""
    qcol, pcol, gcol, scol = mapping.get("query"), mapping.get("product"), mapping.get("grade"), mapping.get("slice")
    if not qcol or not pcol:
        raise ValueError("say which column holds the question and which holds the product you want back")
    questions: dict[str, dict] = {}
    answers: dict[tuple, dict] = {}
    skipped = {"no question": 0, "no product": 0, "a grade that isn't 0 to 3": 0}
    for r in rows:
        q = str(r.get(qcol) or "").strip()
        p = r.get(pcol)
        if not q:
            skipped["no question"] += 1
            continue
        if p is None or str(p).strip() == "":
            skipped["no product"] += 1
            continue
        g = _grade(r.get(gcol) if gcol else None, default_grade)
        if g is None:
            skipped["a grade that isn't 0 to 3"] += 1
            continue
        qid = Q.qid("own", q.lower())
        questions.setdefault(qid, {"query_id": qid, "query": q, "class": str(r.get(scol) or "own") if scol else "own"})
        key = (qid, str(p).strip())
        # The same pair twice: the higher grade stands.
        if key not in answers or g > answers[key]["grade"]:
            answers[key] = {"query_id": qid, "query": q, "product_id": str(p).strip(), "grade": g, "source": "human",
                            "model": "own answers", "prompt_version": ""}
    return list(questions.values()), list(answers.values()), {k: v for k, v in skipped.items() if v}


def preview(ctx: Context, source: dict, mapping: dict | None = None, default_grade: int = 3, limit: int = 8) -> dict:
    """What the table holds and what the mapping would make of it, without writing anything.
    {source, columns, rows (the first `limit`, as lists), total, mapping (given, else guessed), ready,
     questions, answers, skipped: {reason: n}, sample: [{query, product_id, grade, slice}], problem?}."""
    t, label = _table(ctx, source)
    cols = list(t.column_names)
    rows = t.to_pylist()
    mapping = {k: v for k, v in (mapping or guess(cols)).items() if v in cols}
    out = {"source": label, "columns": cols, "rows": [[r.get(c) for c in cols] for r in rows[: max(1, limit)]],
           "total": len(rows), "mapping": mapping, "default_grade": default_grade, "ready": False,
           "questions": 0, "answers": 0, "skipped": {}, "sample": []}
    if not mapping.get("query") or not mapping.get("product"):
        out["problem"] = "Say which column holds the question and which holds the product you want back."
        return out
    qs, ans, skipped = _pairs(rows, mapping, default_grade)
    out.update(questions=len(qs), answers=len(ans), skipped=skipped, ready=bool(qs and ans),
               sample=[{"query": a["query"], "product_id": a["product_id"], "grade": a["grade"],
                        "slice": next((q["class"] for q in qs if q["query_id"] == a["query_id"]), None)} for a in ans[: max(1, limit)]])
    if not out["ready"]:
        out["problem"] = "No row has both a question and a product."
    return out


def from_pairs(ctx: Context, name: str, source: dict, mapping: dict, default_grade: int = 3) -> dict:
    """Make <name>/questions and <name>/answers from the table. Returns {queries, judgments, questions, answers,
    skipped}: the two refs to hand an offline evaluation. A second call with the same name is a new version of each."""
    from .storage.registry import check_name
    from .ops import ref

    check_name(name)
    t, label = _table(ctx, source)
    qs, ans, skipped = _pairs(t.to_pylist(), mapping, default_grade)
    if not qs or not ans:
        raise ValueError("no row has both a question and a product, so there is nothing to test on")
    params = {"from": label, "mapping": {k: v for k, v in mapping.items() if v}, "default_grade": default_grade,
              "skipped": skipped}
    made = []
    for suffix, rows, kind, words in (("questions", qs, DatasetKind.queries, f"{len(qs)} of your own questions from {label}"),
                                      ("answers", ans, DatasetKind.judgments, f"{len(ans)} answers you want, from {label}")):
        table = pa.Table.from_pylist(rows)
        m = snapshot(ctx, f"{name}/{suffix}", table, kind=kind, dry_run=True, description=words,
                     provenance=Provenance(source="import", params=params))
        if kind == DatasetKind.judgments:
            m = m.model_copy(update={"judgment_source": "human", "judge": {"model": "own answers", "prompt_version": label}})
        e = ctx.registry.put_blob(to_parquet(table), "data/part-0.parquet")
        made.append(ctx.registry.publish(m.model_copy(update={"files": [e]})))
    return {"queries": ref(made[0]), "judgments": ref(made[1]), "questions": len(qs), "answers": len(ans), "skipped": skipped}
