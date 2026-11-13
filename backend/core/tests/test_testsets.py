"""A person's own test set (questions and the products they want back), and what the New evaluation form offers."""
import pytest
from fastapi.testclient import TestClient

from captain_search import demo, ops
from captain_search import testsets as TS
from captain_search.eval import evalform as EF
from captain_search.eval import metrics as M
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("shop", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


@pytest.fixture
def pairs(ctx, tmp_path):
    """A CSV of the person's own: a question, the product they want, sometimes a grade, and a group."""
    ids = [str(r["id"]) for r in ops.products_of(ctx, "demo-catalog")[:6]]
    f = tmp_path / "my-questions.csv"
    f.write_text("question,wanted,relevance,kind\n"
                 f"warm tent for two,{ids[0]},3,camping\n"
                 f"warm tent for two,{ids[1]},2,camping\n"
                 f"trail shoes,{ids[2]},,footwear\n"
                 f"trail shoes,{ids[2]},1,footwear\n"   # the same pair again: the higher grade stands
                 f",{ids[3]},3,footwear\n"              # no question
                 f"rain jacket,,3,clothing\n"           # no product
                 f"rain jacket,{ids[4]},9,clothing\n")  # a grade that isn't 0 to 3
    return str(f), ids


def test_the_mapping_is_guessed_and_the_preview_writes_nothing(ctx, pairs):
    path, ids = pairs
    before = set(ctx.registry.names("dataset"))
    p = TS.preview(ctx, {"file": path})
    assert p["columns"] == ["question", "wanted", "relevance", "kind"] and p["total"] == 7 and len(p["rows"]) == 7
    assert p["mapping"] == {"query": "question", "product": "wanted", "grade": "relevance", "slice": "kind"}
    assert p["ready"] and p["questions"] == 2 and p["answers"] == 3
    assert p["skipped"] == {"no question": 1, "no product": 1, "a grade that isn't 0 to 3": 1}, "every skipped row has its reason"
    assert {"query": "trail shoes", "product_id": ids[2], "grade": 3, "slice": "footwear"} in p["sample"], "an empty grade is the default, and the higher of two stands"
    assert set(ctx.registry.names("dataset")) == before, "a preview makes nothing"
    # Without a column for the question it says what is missing instead of guessing.
    q = TS.preview(ctx, {"file": path}, {"product": "wanted"})
    assert not q["ready"] and "which column holds the question" in q["problem"]
    with pytest.raises(ValueError, match="there is no file"):
        TS.preview(ctx, {"file": "/nowhere/at-all.csv"})
    with pytest.raises(ValueError, match="pick a table from Data"):
        TS.preview(ctx, {})


def test_it_makes_a_query_set_and_human_judgments_an_evaluation_can_run_on(ctx, pairs):
    path, ids = pairs
    made = TS.from_pairs(ctx, "my-test", {"file": path}, {"query": "question", "product": "wanted", "grade": "relevance", "slice": "kind"})
    assert made["questions"] == 2 and made["answers"] == 3 and made["queries"] == "my-test/questions@v1" and made["judgments"] == "my-test/answers@v1"
    qm, qs, _ = ops.rows_of(ctx, made["queries"])
    jm, js, _ = ops.rows_of(ctx, made["judgments"])
    assert qm.dataset_kind.value == "queries" and {q["class"] for q in qs} == {"camping", "footwear"}
    assert jm.dataset_kind.value == "judgments" and jm.judgment_source == "human", "the person's own answers are human judgments"
    assert {(j["query"], j["product_id"], j["grade"]) for j in js} == {("warm tent for two", ids[0], 3), ("warm tent for two", ids[1], 2), ("trail shoes", ids[2], 3)}
    assert jm.provenance.params["skipped"] == {"no question": 1, "no product": 1, "a grade that isn't 0 to 3": 1}
    # An offline evaluation plans and runs on them, and its evidence is human graded.
    bt = ops.backtest_plan(ctx, "own-test", ["demo-bm25", "demo-tuned"], made["queries"], made["judgments"], None, "tuned finds my answers", "ndcg@10", 0.003)
    assert bt.queries == made["queries"] and bt.judgments == made["judgments"]
    run = ops.backtest_run(ctx, "own-test")
    assert run.arms[0].evidence.value == "HUMAN_GRADED" and "ndcg@10" in run.arms[0].offline
    # The same name again is a new version of each: the test set is versioned like any table.
    again = TS.from_pairs(ctx, "my-test", {"file": path}, {"query": "question", "product": "wanted"}, default_grade=2)
    assert again["queries"] == "my-test/questions@v2" and again["answers"] == 4, "with no grade column every row with a product counts, at the default grade"


def test_a_table_already_in_data_works_the_same(ctx):
    p = TS.preview(ctx, {"dataset": "demo-judgments"})
    assert p["source"].startswith("demo-judgments@v") and p["mapping"].get("product") == "product_id"
    # Whatever the guess missed, the person maps by hand: any column can be the question.
    cols = p["columns"]
    q = "query" if "query" in cols else "query_id"
    mapped = TS.preview(ctx, {"dataset": "demo-judgments"}, {"query": q, "product": "product_id", "grade": "grade"})
    assert mapped["ready"] and mapped["questions"] > 0 and mapped["answers"] > 0
    # A column the table doesn't have is dropped from the mapping, not sent to the reader.
    assert "slice" not in TS.preview(ctx, {"dataset": "demo-judgments"}, {"query": q, "product": "product_id", "slice": "nope"})["mapping"]


def test_the_form_offers_only_what_a_run_can_do(ctx):
    o = EF.options()
    ids = [m["id"] for m in o["measures"]]
    assert set(M.NAMES) <= set(ids), "every offline measure the runner computes is offered"
    for m in o["measures"]:
        assert m["line"] and m["needs"] in ("answers", "logs", "shoppers"), m
        # Each one the form can make the deciding measure is one the planner accepts.
        if m["needs"] == "answers":
            ops.backtest_plan(ctx, "opt-check", ["demo-bm25", "demo-tuned"], "demo-queries", "demo-judgments", None, "x", m["id"], 0.003, dry_run=True)
    assert {x["id"] for x in o["limits"]} == {"mde", "population_size"} and all("default" in x for x in o["limits"])
    # What was asked for and a run can't do is named, with the reason, instead of being a control that does nothing.
    assert {x["what"] for x in o["not_yet"]} == {"Which slices", "Which measures to compute"}
    # Depth is real now: a field per measure that has one, starting at the usual depth.
    assert {d["id"]: d["default"] for d in o["depths"]} == M.DEFAULT_DEPTHS and all(d["max"] == M.MAX_DEPTH for d in o["depths"])


def test_routes(ctx, pairs, monkeypatch):
    from captain_search import server

    path, _ = pairs
    monkeypatch.setattr(server, "_ctx", None)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    o = c.get("/api/evaluations/options", headers=h).json()
    assert o["default_measure"] == "ndcg@10" and o["grades"]["3"] == "Exact"
    p = c.post("/api/testsets/preview", headers=h, json={"source": {"file": path}}).json()
    assert p["ready"] and p["questions"] == 2
    made = c.post("/api/testsets", headers=h, json={"name": "routed", "source": {"file": path}, "mapping": p["mapping"]}).json()
    assert made["queries"] == "routed/questions@v1"
    bad = c.post("/api/testsets", headers=h, json={"name": "routed", "source": {"file": path}, "mapping": {}})
    assert bad.status_code == 400 and "which column holds the question" in bad.json()["error"]
    # population_size reaches the plan: a replay of logged searches makes that many shoppers.
    r = c.post("/api/backtests", headers=h, json={"name": "replay-small", "mode": "historic", "arms": ["demo-bm25", "demo-tuned"],
                                                    "logs": "logs/demo-search-sessions", "hypothesis": "x", "primary": "ndcg@10", "population_size": 300})
    assert r.status_code == 200, r.text
    pop = ctx.registry.load("population", "replay-small/shoppers")
    assert pop.size == 300
