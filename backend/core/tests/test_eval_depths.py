"""How many results deep each measure looks: a setting of the evaluation, passed through the plan and the run and
recorded with the run, so two runs at different depths are never read as the same measurement."""
import pytest
from fastapi.testclient import TestClient

from captain_search import compare as CP
from captain_search import demo, ops
from captain_search.eval import metrics as M
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("shop", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def test_the_measures_look_as_deep_as_they_are_told():
    ranked = [f"p{i}" for i in range(60)]
    rel = {"p0": 3, "p15": 3, "p55": 2}
    usual = M.metrics(ranked, rel)
    assert usual == M.metrics(ranked, rel, M.DEFAULT_DEPTHS), "no depths given is the usual depths"
    assert usual["p10"] == 0.1 and round(usual["r50"], 4) == round(2 / 3, 4)
    deep = M.metrics(ranked, rel, M.check_depths({"ndcg@10": 20, "p@10": 20, "r@50": 60, "mrr": 60}))
    assert deep["p10"] == 2 / 20, "precision over the top 20 counts the hit at rank 16"
    assert deep["r50"] == 1.0, "recall at 60 reaches the hit at rank 56"
    assert deep["ndcg10"] > usual["ndcg10"], "nDCG at 20 gets credit for the hit at rank 16"
    shallow = M.metrics(ranked[1:], rel, M.check_depths({"mrr": 5}))
    assert shallow["mrr"] == 0.0, "the first good answer is past rank 5"
    # The share of judged results is taken at nDCG's depth.
    pq = M.per_query({"q": ranked}, {"q": rel}, depths=M.check_depths({"ndcg@10": 20}))
    assert pq["q"]["judged10"] == 2 / 20


def test_a_bad_depth_is_refused_with_a_sentence():
    assert M.check_depths(None) == M.DEFAULT_DEPTHS and M.check_depths({"r@50": "100"})["r@50"] == 100
    for bad, words in (({"ndcg@10": 0}, "from 1 to 200"), ({"p@10": 201}, "from 1 to 200"), ({"p@10": 2.5}, "whole number"),
                       ({"mrr": "deep"}, "whole number"), ({"zero": 5}, "has no depth"), ({"ctr": 5}, "has no depth")):
        with pytest.raises(ValueError, match=words):
            M.check_depths(bad)
    assert M.non_default(None) == [] and M.non_default(M.DEFAULT_DEPTHS) == []
    assert M.non_default({"ndcg@10": 20, "r@50": 100}) == ["nDCG looks 20 results deep, not the usual 10.", "Recall looks 100 results deep, not the usual 50."]
    assert M.label("ndcg@10", {"ndcg@10": 20}) == "ndcg@20" and M.label("ndcg@10_tw", {"ndcg@10": 20}) == "ndcg@20_tw"
    assert M.label("ndcg@10", None) == "ndcg@10" and M.label("mrr", {"mrr": 5}) == "mrr" and M.label("ctr", {"ndcg@10": 20}) == "ctr"


def test_depth_goes_from_the_plan_to_the_run_and_is_recorded(ctx):
    usual = ops.backtest_run(ctx, "demo-tuned-vs-bm25")
    assert usual.depths == M.DEFAULT_DEPTHS, "a run always records the depths it looked, the usual ones too"
    with pytest.raises(ValueError, match="from 1 to 200"):
        ops.backtest_plan(ctx, "deep", ["demo-bm25", "demo-tuned"], "demo-queries", "demo-judgments", None, "x", "ndcg@10", 0.003, depths={"ndcg@10": 999})
    bt = ops.backtest_plan(ctx, "deep", ["demo-bm25", "demo-tuned"], "demo-queries", "demo-judgments", None, "x", "ndcg@10", 0.003,
                           depths={"ndcg@10": 3, "p@10": 3, "r@50": 50})
    assert bt.depths == {"ndcg@10": 3, "p@10": 3}, "only what differs from the usual is kept on the plan"
    run = ops.backtest_run(ctx, "deep")
    assert run.depths == {"ndcg@10": 3, "p@10": 3, "r@50": 50, "mrr": 50}
    # The numbers really are at depth 3: the same queries and judgments at the usual depth give another score.
    same = ops.backtest_plan(ctx, "usual", ["demo-bm25", "demo-tuned"], "demo-queries", "demo-judgments", None, "x", "ndcg@10", 0.003)
    assert same.depths == {}
    base = ops.backtest_run(ctx, "usual")
    assert run.arms[1].offline["p@10"] != base.arms[1].offline["p@10"]
    assert run.arms[1].offline["r@50"] == base.arms[1].offline["r@50"], "recall was left at its usual depth, so it is unchanged"
    # A query's own numbers are recomputed at the run's depth, so they add up to the run's.
    page = CP.queries(ctx, "deep", limit=500)
    mean_b = sum(r["ndcg_b"] for r in page["rows"]) / len(page["rows"])
    assert abs(mean_b - run.arms[1].offline["ndcg@10"]) < 1e-3
    # The report says so before any number.
    rep = ops.backtest_report(run, bt)
    assert "NOT THE USUAL DEPTHS: nDCG looks 3 results deep, not the usual 10. Precision looks 3 results deep, not the usual 10." in rep
    assert "NOT THE USUAL DEPTHS" not in ops.backtest_report(base, same)


def test_two_runs_at_different_depths_are_never_compared_as_equal(ctx):
    ops.backtest_plan(ctx, "deep", ["demo-bm25", "demo-tuned"], "demo-queries", "demo-judgments", None, "x", "ndcg@10", 0.003, depths={"ndcg@10": 3})
    ops.backtest_run(ctx, "deep")
    ops.backtest_plan(ctx, "usual", ["demo-bm25", "demo-tuned"], "demo-queries", "demo-judgments", None, "x", "ndcg@10", 0.003)
    ops.backtest_run(ctx, "usual")
    x = CP.compare_runs(ctx, "deep", "usual")
    assert not x["comparable"]
    assert [i["code"] for i in x["issues"]] == ["different-depth"], x["issues"]
    assert "nDCG looks a different number of results deep (3 vs 10)" in x["issues"][0]["text"]
    assert CP.compare_runs(ctx, "usual", "usual")["comparable"]
    # The run's own warnings say it, and so does every regression row, so the verdict sentence can.
    w = CP.compatibility(ctx, "deep")["warnings"]
    assert any(i["code"] == "depth" and "nDCG looks 3 results deep, not the usual 10." in i["text"] for i in w)
    assert not any(i["code"] == "depth" for i in CP.compatibility(ctx, "usual")["warnings"])
    rows = {r["name"]: r for r in CP.regression(ctx)["rows"]}
    assert rows["deep"]["not_usual"] == ["nDCG looks 3 results deep, not the usual 10."] and rows["deep"]["depths"]["ndcg@10"] == 3
    assert rows["usual"]["not_usual"] == [] and rows["usual"]["depths"] == M.DEFAULT_DEPTHS


def test_a_run_from_before_depths_reads_as_the_usual_depths(ctx):
    from captain_search.model.entities import BacktestRun

    run = ctx.registry.load("backtest-run", "demo-tuned-vs-bm25")
    # A record written before depths existed has no such field: it loads, and reads as the usual depths.
    old = run.model_dump(mode="json")
    old.pop("depths")
    before = BacktestRun.model_validate(old)
    assert before.depths == {}
    assert M.check_depths(before.depths) == M.DEFAULT_DEPTHS and M.non_default(before.depths) == []
    assert "NOT THE USUAL DEPTHS" not in ops.backtest_report(before, ctx.registry.load("backtest", "demo-tuned-vs-bm25"))
    assert CP.compare_runs(ctx, "demo-tuned-vs-bm25", "demo-tuned-vs-bm25")["comparable"]


def test_routes(ctx, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", None)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    body = {"name": "by-route", "arms": ["demo-bm25", "demo-tuned"], "queries": "demo-queries", "judgments": "demo-judgments", "hypothesis": "x"}
    bad = c.post("/api/backtests", headers=h, json={**body, "depths": {"ndcg@10": 0}})
    assert bad.status_code == 400 and "from 1 to 200" in bad.json()["error"]
    ok = c.post("/api/backtests", headers=h, json={**body, "depths": {"ndcg@10": 20, "p@10": 10}})
    assert ok.status_code == 200 and ok.json()["depths"] == {"ndcg@10": 20}
    ops.backtest_run(ctx, "by-route")
    r = c.get("/api/runs/by-route", headers=h).json()
    assert r["depths"]["ndcg@10"] == 20 and r["not_usual"] == ["nDCG looks 20 results deep, not the usual 10."]
    old = c.get("/api/runs/demo-tuned-vs-bm25", headers=h).json()
    assert old["depths"] == M.DEFAULT_DEPTHS and old["not_usual"] == []
