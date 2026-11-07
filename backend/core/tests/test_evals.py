"""Living evaluation sets: health (traffic, staleness, coverage, reliability) and refresh as new versions."""
import pyarrow as pa
import pytest

from captain_search import demo, evals as EV, ops
from captain_search.data import datasets as DS
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    import os

    os.environ["CST_HOME"] = str(tmp_path_factory.mktemp("home"))
    c = init_workspace("t", str(tmp_path_factory.mktemp("b")))
    demo.seed(c, progress=lambda s: None)
    # the catalog moves on after grading: one product gone, one price changed, one relevant product out of stock
    jmap = ops.judgments_map(c, "demo-judgments")[1]
    relevant = sorted({p for d in jmap.values() for p, g in d.items() if g >= 2})
    graded = sorted({p for d in jmap.values() for p in d})
    gone, priced, oos = graded[0], relevant[1], relevant[2]
    cat = DS.load_table(c, c.registry.load("dataset", "demo-catalog")).to_pylist()
    new = []
    for p in cat:
        if p["id"] == gone:
            continue
        if p["id"] == priced:
            p = {**p, "price": float(p["price"]) + 10}
        if p["id"] == oos:
            p = {**p, "in_stock": False}
        new.append(p)
    DS.snapshot(c, "demo-catalog", pa.Table.from_pylist(new), kind=DatasetKind.catalog,
                provenance=Provenance(source="generated"))
    c._test = {"gone": gone, "priced": priced, "oos": oos}
    return c


def test_health_names_what_went_stale(ctx):
    h = EV.health(ctx, "demo-queries@v2", "demo-judgments", logs="logs/demo-search-sessions", catalog="demo-catalog")
    s = h["staleness"]
    assert s["catalog_now"] == "demo-catalog@v2" and s["catalog_when_graded"] == "demo-catalog@v1"
    assert ctx._test["gone"] in {r["product_id"] for r in s["retired"]}
    assert any(r["product_id"] == ctx._test["priced"] and r["fields"] == ["price"] for r in s["changed"])
    assert ctx._test["oos"] in {r["product_id"] for r in s["out_of_stock_relevant"]}
    r = h["representativeness"]
    assert 0 <= r["traffic_covered"] <= 1 and set(r["by_band"]) == {"head", "torso", "tail"}
    assert h["reliability"]["exact_agreement"] is None and "can't be measured" in h["reliability"]["note"]
    assert h["coverage"] and all(0 <= x["judged_at_10"] <= 1 for x in h["coverage"])
    assert h["status"] == "needs refresh" and any("no longer in the catalog" in f for f in h["findings"])


def test_refresh_publishes_new_versions_and_keeps_the_seal(ctx):
    plan = EV.refresh(ctx, "demo-queries@v2", "demo-judgments", "logs/demo-search-sessions", "demo-catalog",
                      add=20, dry_run=True)
    assert plan["added_queries"] and "queries" not in plan
    out = EV.refresh(ctx, "demo-queries@v2", "demo-judgments", "logs/demo-search-sessions", "demo-catalog", add=20)
    assert out["queries"] == "demo-queries@v3" and out["judgments"] == "demo-judgments@v2"
    q3 = ctx.registry.load("dataset", "demo-queries", 3)
    q2 = ctx.registry.load("dataset", "demo-queries", 2)
    assert q3.splits["holdout"] == q2.splits["holdout"] and q3.holdout_seal == q2.holdout_seal
    assert len(q3.splits["added"]) == len(out["added_queries"]) and q3.row_count == q2.row_count + len(out["added_queries"])
    j2 = ops.judgments_map(ctx, "demo-judgments@v2")[1]
    assert all(ctx._test["gone"] not in d for d in j2.values())
    assert "added" in out["changelog"][0] and "seed 7" in out["changelog"][0]
    again = EV.refresh(ctx, "demo-queries@v2", "demo-judgments@v1", "logs/demo-search-sessions", add=20, dry_run=True)
    assert [a["query"] for a in again["added_queries"]] == [a["query"] for a in out["added_queries"]]  # same seed, same sample


def test_rubrics_reach_the_judge_and_never_mix(tmp_path, monkeypatch):
    import json

    from captain_search import extensions as X
    from captain_search import grading as GR
    from captain_search import llm as LLM
    from captain_search import ops

    proj = X.scaffold(str(tmp_path / "repo"), "acme")
    (proj / "rubrics" / "outdoor.md").write_text("A tent is relevant to 'shelter' queries. Grade sleeping bags 1.")
    monkeypatch.setenv("CST_PROJECT", str(proj))
    import os

    os.environ["CST_HOME"] = str(tmp_path / "h2")
    c = init_workspace("r", str(tmp_path / "b2"))
    demo.seed(c, progress=lambda s: None)
    seen = []

    class Fake:
        backend, model = "fake", "m"

        def complete(self, system, msgs, max_tokens=0):
            seen.append(system)
            import re

            ids = re.findall(r"\[(p\d+)\]", msgs[0]["content"])
            return json.dumps({"grades": {i: 2 for i in ids}})

    monkeypatch.setattr(LLM, "get_llm", lambda: Fake())
    m = ops.judge_run(c, "demo-queries@v2:holdout", ["demo-bm25"], "llm", "outdoor-grades", depth=2, rubric="outdoor")
    assert seen and "Grade sleeping bags 1." in seen[0]
    assert m.provenance.params["rubric"]["name"] == "outdoor" and "+outdoor:" in m.provenance.params["prompt_version"]
    (proj / "rubrics" / "outdoor.md").write_text("Changed rubric.")
    with pytest.raises(ValueError, match="another rubric"):
        ops.judge_run(c, "demo-queries@v2:holdout", ["demo-bm25"], "llm", "outdoor-grades", depth=2, rubric="outdoor")
    q = GR.create(c, "outdoor-queue", "demo-queries@v2:holdout", ["demo-bm25"], depth=2, sample=3, rubric="outdoor")
    assert q["rubric"]["text"] == "Changed rubric."
    with pytest.raises(ValueError, match="no rubric 'nope'"):
        GR.create(c, "q2", "demo-queries@v2:holdout", ["demo-bm25"], depth=2, rubric="nope")
