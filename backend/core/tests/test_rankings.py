"""Bring your own rankings (spec S2): validate, import, compare, with labels or without."""
import csv
import random

import pyarrow as pa
import pytest

from captain_search import compare as CP
from captain_search import rankings as RK
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    return init_workspace("t", str(tmp_path / "b"))


def _file(tmp_path, rows, name="r.csv"):
    p = tmp_path / name
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return str(p)


def _rows(n_queries=40, seed=7, labels=True):
    rng = random.Random(seed)
    out = []
    for qn in range(n_queries):
        good = {f"p{qn}-{i}" for i in range(3)}
        items = [f"p{qn}-{i}" for i in range(12)]
        for arm in ("prod", "candidate"):
            order = items[:] if arm == "candidate" else items[3:] + items[:3]  # candidate puts the good ones first
            if arm == "prod":
                rng.shuffle(order)
            for rank, it in enumerate(order[:10], 1):
                r = {"qid": f"q{qn}", "query": f"query {qn}", "system": arm, "item_id": it, "rank": rank,
                     "segment": "mobile" if qn % 2 else "desktop"}
                if labels:
                    r["label"] = 3 if it in good else 0
                out.append(r)
    return out


MAP = {"query_id": "qid", "arm": "system"}


def test_validation_names_each_problem():
    ok = RK.validate(pa.Table.from_pylist(_rows(5)), MAP)
    assert ok["ok"] and ok["stats"]["arms"] == ["candidate", "prod"] and ok["available"]["slices"]
    no_cols = RK.validate(pa.Table.from_pylist([{"q": 1, "x": 2}]))
    assert not no_cols["ok"] and "Missing columns: query_id, arm, item_id" in no_cols["errors"][0]
    rows = _rows(2)
    rows.append(dict(rows[0]))  # duplicate item
    rows.append({**rows[1], "item_id": "zzz"})  # same rank twice
    rows.append({**rows[2], "item_id": "yyy", "rank": 0})  # bad rank
    rows.append({**rows[3], "item_id": "xxx", "rank": 11, "label": 7})  # bad label
    bad = RK.validate(pa.Table.from_pylist(rows), MAP)
    text = " ".join(bad["errors"])
    assert not bad["ok"] and "repeat the same item" in text and "same rank" in text
    assert "whole numbers from 1" in text and "grades 0 to 3" in text
    unlabeled = RK.validate(pa.Table.from_pylist(_rows(3, labels=False)), MAP)
    assert unlabeled["ok"] and not unlabeled["available"]["relevance metrics"]
    assert any("unavailable" in w for w in unlabeled["warnings"])


def test_import_compares_arms_and_works_side_by_side(ctx, tmp_path):
    path = _file(tmp_path, _rows())
    with pytest.raises(ValueError, match="where the labels come from"):
        RK.import_rankings(ctx, "search-ab", path, mapping=MAP, baseline="prod")
    out = RK.import_rankings(ctx, "search-ab", path, mapping=MAP, baseline="prod", labels_from="human")
    run = ctx.registry.load("backtest-run", "search-ab")
    assert out["evidence"] == "HUMAN_GRADED" and [a.arm for a in run.arms] == ["prod", "candidate"]
    prod, cand = (a.offline["ndcg@10"] for a in run.arms)
    assert cand > prod and cand == 1.0
    primary = next(c for c in run.comparisons if c["primary"])
    assert primary["metric"] == "ndcg@10" and primary["verdict"] == "win" and primary["evidence"] == "HUMAN_GRADED"
    assert {s.get("segment") or s.get("name") or s.get("value") for s in run.segments} & {"mobile", "desktop"} or run.segments
    q = CP.queries(ctx, "search-ab", limit=5)
    assert q["total"] == 40 and {"ndcg_a", "ndcg_b"} <= set(q["rows"][0])
    side = CP.query(ctx, "search-ab", "q0")
    assert side and CP.histogram(ctx, "search-ab") and CP.compatibility(ctx, "search-ab")
    assert set(out["datasets"]) == {"rankings", "queries", "labels"}


def test_without_labels_relevance_is_unavailable_not_invented(ctx, tmp_path):
    path = _file(tmp_path, _rows(5, labels=False))
    out = RK.import_rankings(ctx, "nolabels", path, mapping=MAP)
    run = ctx.registry.load("backtest-run", "nolabels")
    assert run.comparisons == [] and all(a.offline == {} for a in run.arms) and out["evidence"] is None
    assert "unavailable" in run.description and run.complete is False
