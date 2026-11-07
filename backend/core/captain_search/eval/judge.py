"""Relevance judges. Grades 0-3 on (query, product) pairs.

- `llm`: blind grading. One call holds one query and up to 20 shuffled products labelled p1..pN; the
  judge never sees engine names, ranks or scores. Prompt version s1. Evidence: LLM_GRADED.
- `heuristic`: token overlap plus a price check. Free and instant, for smoke tests and for the
  shopper simulator's own click model. Evidence: SIMULATED.
- human labels come in through `import_labels`. Evidence: HUMAN_GRADED.

Judgments are cached: a run reuses every pair already graded by the same judge in the previous
version of the target judgment dataset, and only pays for new pairs.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor

from ..model.entities import Evidence
from ..engines import tokens

PROMPT_VERSION = "s1"
PER_CALL = 20
SYSTEM = """You grade product search results for an online store. For each product, decide how well it answers the
shopper's query, using only the product text shown. Grades:
3 = exactly what the shopper asked for, every stated constraint met (type, numbers and units, attributes, compatibility).
2 = the right kind of product, but one stated constraint is missed or cannot be verified from the text.
1 = related but not what was asked (an accessory or part of the requested product, wrong size, wrong model).
0 = unrelated.
Judge numbers and units carefully: "under 42" is not met by 45.
Answer with JSON only: {"grades": {"p1": <0-3>, "p2": <0-3>, ...}} with one entry per product shown."""

EVIDENCE = {"llm": Evidence.LLM_GRADED, "heuristic": Evidence.SIMULATED, "human": Evidence.HUMAN_GRADED,
            "click": Evidence.LOGGED_CLICKS}


def product_text(p: dict) -> str:
    parts = [str(p.get("title") or "")]
    for f in ("brand", "category", "price"):
        if p.get(f) not in (None, ""):
            parts.append(f"{f.capitalize()}: {p[f]}")
    if p.get("description"):
        parts.append("Description: " + re.sub(r"\s+", " ", str(p["description"]))[:900])
    return "\n".join(parts)


def heuristic_grade(query: str, p: dict) -> int:
    q = [t for t in tokens(query) if t not in ("under", "below", "less", "than")]
    m = re.search(r"\b(?:under|below|less than)\s+\$?(\d+(?:\.\d+)?)", query.lower())
    if m:
        q = [t for t in q if t != m.group(1).split(".")[0]]
    text = set(tokens(" ".join(str(p.get(f) or "") for f in ("title", "brand", "category", "description"))))
    if not q:
        return 0
    frac = sum(1 for t in q if t in text) / len(q)
    g = 3 if frac >= 0.99 else 2 if frac >= 0.66 else 1 if frac >= 0.34 else 0
    if m and g > 1:
        try:
            if float(p.get("price")) > float(m.group(1)):
                g = 1
        except (TypeError, ValueError):
            g = min(g, 2)
    return g


def _calls(items: list[tuple[str, str, list[str]]], products: dict[str, dict]) -> list[dict]:
    calls = []
    for qid, q, ids in items:
        ids = sorted(set(ids))
        random.Random(int(hashlib.md5(qid.encode()).hexdigest(), 16)).shuffle(ids)
        for k in range(0, len(ids), PER_CALL):
            chunk = ids[k : k + PER_CALL]
            cmap = {f"p{j + 1}": pid for j, pid in enumerate(chunk)}
            docs = "\n\n".join(f"[{c}]\n{product_text(products[pid])}" for c, pid in cmap.items())
            calls.append({"query_id": qid, "cmap": cmap, "user": f"Query: {q}\n\nProducts:\n\n{docs}"})
    return calls


def judge_pairs(pairs: dict[str, tuple[str, list[str]]], products: dict[str, dict], judge: str,
                workers: int = 4, progress=None, workspace: str | None = None,
                rubric: dict | None = None) -> tuple[list[dict], dict]:
    """pairs: query_id -> (query, [product ids to grade]). Returns (rows, meta)."""
    rows: list[dict] = []
    from .. import extensions as X

    ext = X.load(workspace).get("judge", judge)
    if ext is not None and ext.source != "builtin":
        for qid, (q, ids) in pairs.items():
            for pid in ids:
                g = ext.obj(q, products[pid])
                g, why = (g if isinstance(g, tuple) else (g, None))
                rows.append({"query_id": qid, "product_id": pid, "grade": int(max(0, min(3, int(g)))),
                             "source": "custom", "model": f"{ext.name}@{ext.version}", "prompt_version": ext.source,
                             **({"rationale": str(why)[:500]} if why else {})})
        return rows, {"model": f"{ext.name}@{ext.version}", "prompt_version": ext.source, "calls": 0,
                      "extension": ext.public()}
    if judge == "heuristic":
        for qid, (q, ids) in pairs.items():
            for pid in ids:
                rows.append({"query_id": qid, "product_id": pid, "grade": heuristic_grade(q, products[pid]),
                             "source": "heuristic", "model": "token-overlap", "prompt_version": "h1"})
        return rows, {"model": "token-overlap", "prompt_version": "h1", "calls": 0}
    if not judge.startswith("llm"):
        raise ValueError(f"unknown judge {judge!r}: use llm, llm:<model>, heuristic, or a judge from your captain/ folder "
                         "(cst ext list)")
    import os

    from ..llm import get_llm, parse_json

    if ":" in judge:
        os.environ["CST_MODEL"] = judge.split(":", 1)[1]
    llm = get_llm()
    system = SYSTEM + (f"\n\nThis team's rubric for its domain. Where it is more specific than the grades above, "
                       f"follow it:\n{rubric['text']}" if rubric else "")
    pv = PROMPT_VERSION + (f"+{rubric['name']}:{rubric['sha'][:8]}" if rubric else "")
    calls = _calls([(qid, q, ids) for qid, (q, ids) in pairs.items()], products)
    done = 0

    def one(c):
        nonlocal done
        for _ in range(3):
            try:
                out = parse_json(llm.complete(system, [{"role": "user", "content": c["user"]}], max_tokens=1500))
                grades = out.get("grades", {})
                r = [{"query_id": c["query_id"], "product_id": pid, "grade": int(max(0, min(3, int(grades[cid])))),
                      "source": "llm", "model": f"{llm.backend}:{llm.model}", "prompt_version": pv}
                     for cid, pid in c["cmap"].items() if cid in grades]
                break
            except Exception:
                r = []
        done += 1
        if progress:
            progress(done / max(1, len(calls)))
        return r

    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(one, calls):
            rows.extend(r)
    return rows, {"model": f"{llm.backend}:{llm.model}", "prompt_version": pv, "calls": len(calls),
                  **({"rubric": {"name": rubric["name"], "sha": rubric["sha"]}} if rubric else {})}


def to_map(rows: list[dict]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(str(r["query_id"]), {})[str(r["product_id"])] = int(r["grade"])
    return out


def calibrate(llm_rows: list[dict], human_rows: list[dict]) -> dict:
    from .stats import quadratic_kappa

    h = {(str(r["query_id"]), str(r["product_id"])): int(r["grade"]) for r in human_rows}
    pairs = [(int(r["grade"]), h[(str(r["query_id"]), str(r["product_id"]))]) for r in llm_rows
             if (str(r["query_id"]), str(r["product_id"])) in h]
    if not pairs:
        return {"n": 0, "kappa": None, "mean_grade_bias": None}
    x, y = zip(*pairs)
    return {"n": len(pairs), "kappa": round(quadratic_kappa(list(x), list(y)), 4),
            "mean_grade_bias": round(sum(a - b for a, b in pairs) / len(pairs), 4),
            "exact_agreement": round(sum(a == b for a, b in pairs) / len(pairs), 4)}


def dumps(rows) -> str:
    return json.dumps(rows)
