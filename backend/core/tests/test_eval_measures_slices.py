"""Which measures a run computes, and what it is sliced by: both settings of the evaluation, checked when it is
planned, used by the run and recorded with it."""
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from captain_search import compare as CP
from captain_search import demo, ops
from captain_search import segments as SEG
from captain_search import testsets as TS
from captain_search.eval import metrics as M
from captain_search.workspace import init_workspace

ARMS = ["demo-bm25", "demo-tuned"]


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("shop", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def plan(ctx, name, **kw):
    return ops.backtest_plan(ctx, name, ARMS, "demo-queries", "demo-judgments", None, "x", kw.pop("primary", "ndcg@10"), 0.003, **kw)


def test_the_choice_of_measures_is_checked():
    assert M.check_measures(None, "ndcg@10") == [] and M.check_measures([], "mrr") == []
    assert M.check_measures(["mrr", "ndcg@10"], "ndcg@10") == ["ndcg@10", "mrr"], "kept in the usual order"
    with pytest.raises(ValueError, match="ndcg@10 decides the verdict, so it must be one of the measures computed"):
        M.check_measures(["mrr"], "ndcg@10")
    with pytest.raises(ValueError, match="speed is not a measure"):
        M.check_measures(["ndcg@10", "speed"], "ndcg@10")
    assert M.measures_words([]) == [] and M.measures_words(list(M.ALL_MEASURES)) == []
    assert M.measures_words(["ndcg@10"]) == ["Only nDCG was computed."]
    assert M.measures_words(["mrr", "ndcg@10", "r@50"]) == ["Only nDCG, Recall and MRR were computed."]


def test_a_run_computes_only_the_measures_asked_for(ctx):
    with pytest.raises(ValueError, match="ctr comes from simulated shoppers, and this evaluation has none"):
        plan(ctx, "bad", measures=["ndcg@10", "ctr"])
    with pytest.raises(ValueError, match="must be one of the measures computed"):
        plan(ctx, "bad", measures=["mrr"])
    bt = plan(ctx, "few", measures=["mrr", "ndcg@10"])
    assert bt.measures == ["ndcg@10", "mrr"]
    run = ops.backtest_run(ctx, "few")
    assert run.measures == ["ndcg@10", "mrr"]
    assert [c["metric"] for c in run.comparisons] == ["ndcg@10", "mrr"]
    assert [c["metric"] for c in run.comparisons if c["primary"]] == ["ndcg@10"]
    for r in run.arms:
        assert {"ndcg@10", "mrr", "judged@10"} <= set(r.offline) and not {"p@10", "r@50", "zero"} & set(r.offline)
    # The numbers it did compute are the same as a run that computed everything.
    plan(ctx, "all")
    full = ops.backtest_run(ctx, "all")
    assert full.measures == [] and {"ndcg@10", "p@10", "r@50", "mrr"} <= {c["metric"] for c in full.comparisons}
    assert run.arms[1].offline["ndcg@10"] == full.arms[1].offline["ndcg@10"]
    # It is said: in the report (with a dash, never a zero, for what was not computed), the warnings and the row.
    rep = ops.backtest_report(run, bt)
    assert "Only nDCG and MRR were computed." in rep and "Only" not in ops.backtest_report(full, ctx.registry.load("backtest", "all"))
    line = next(x for x in rep.splitlines() if x.startswith(run.arms[1].arm))
    assert line.split()[2] == "-" and line.split()[3] == "-", line
    assert any(w["code"] == "measures" for w in CP.compatibility(ctx, "few")["warnings"])
    rows = {r["name"]: r for r in CP.regression(ctx)["rows"]}
    assert rows["few"]["not_usual"] == ["Only nDCG and MRR were computed."] and rows["few"]["measures"] == ["ndcg@10", "mrr"]
    assert rows["all"]["not_usual"] == [] and rows["all"]["measures"] == []


def test_slices_are_columns_of_the_questions(ctx):
    cols = ["query_id", "query", "class", "weight", "device"]
    assert SEG.check_slices(None, cols) == [] and SEG.check_slices(["device", "traffic", "device"], cols) == ["device", "traffic"]
    with pytest.raises(ValueError, match="no column named season"):
        SEG.check_slices(["season"], cols)
    with pytest.raises(ValueError, match="query is not a group of questions"):
        SEG.check_slices(["query"], cols)
    with pytest.raises(ValueError, match="needs a weight on each question"):
        SEG.check_slices(["traffic"], ["query_id", "query", "class"])
    assert SEG.slices_words([]) == [] and SEG.slices_words(["class", "traffic"]) == []
    assert SEG.slices_words(["device"]) == ["Sliced by device."] and SEG.slices_words(["class", "device"]) == ["Sliced by group and device."]
    # The slicing itself: the usual when nothing is asked for, else each column asked for, in order.
    qs = [{"query_id": f"q{i}", "query": "x", "class": "a" if i < 6 else "b", "device": ("phone", "desk", "tv")[i % 3], "same": "one"} for i in range(12)]
    perq = {arm: {q["query_id"]: {"ndcg10": 0.5 + (0.1 if arm == "new" else 0), "judged10": 1.0} for q in qs} for arm in ("old", "new")}
    usual = SEG.segments(qs, perq, ["old", "new"], 0.0)
    assert {r["kind"] for r in usual} == {"class"}
    by = SEG.segments(qs, perq, ["old", "new"], 0.0, ["device", "same"])
    assert {r["kind"] for r in by} == {"device"}, "a column where every question is the same makes no slice"
    assert sorted({r["segment"] for r in by}) == ["desk", "phone", "tv"] and all(r["n"] == 4 for r in by)
    ch = SEG.choices(list(qs[0]), qs)
    assert [(c["id"], c["label"], c["groups"]) for c in ch["columns"]] == [("class", "Group", 2), ("device", "device", 3)]
    assert ch["traffic"] is False and ch["usual"] == ["class"]


def test_a_test_set_keeps_the_columns_to_slice_by_and_a_run_slices_by_them(ctx, tmp_path):
    cat = ops.products_of(ctx, "demo-catalog")
    rows = [{"question": f"{p['title']}", "sku": p["id"], "device": ("phone", "desk")[i % 2], "season": ("winter", "summer", "fall")[i % 3]}
            for i, p in enumerate(cat[:24])]
    import pyarrow.parquet as pq

    f = tmp_path / "pairs.parquet"
    pq.write_table(pa.Table.from_pylist(rows), f)
    mapping = {"query": "question", "product": "sku", "also": ["device", "season"]}
    with pytest.raises(ValueError, match="no column named colour"):
        TS.from_pairs(ctx, "bad", {"file": str(f)}, {**mapping, "also": ["colour"]})
    made = TS.from_pairs(ctx, "mine", {"file": str(f)}, mapping)
    _, qs, _ = ops.rows_of(ctx, made["queries"])
    assert {"device", "season"} <= set(qs[0]) and {q["device"] for q in qs} == {"phone", "desk"}
    assert TS.preview(ctx, {"file": str(f)}, mapping)["mapping"]["also"] == ["device", "season"]
    with pytest.raises(ValueError, match="no column named colour"):
        ops.backtest_plan(ctx, "bad", ARMS, made["queries"], made["judgments"], None, "x", "ndcg@10", 0.003, slices=["colour"])
    bt = ops.backtest_plan(ctx, "sliced", ARMS, made["queries"], made["judgments"], None, "x", "ndcg@10", 0.003, slices=["device", "season"])
    assert bt.slices == ["device", "season"]
    run = ops.backtest_run(ctx, "sliced")
    assert run.slices == ["device", "season"]
    assert {(r["kind"], r["segment"]) for r in run.segments} == {("device", "phone"), ("device", "desk"), ("season", "winter"), ("season", "summer"), ("season", "fall")}
    assert "Sliced by device and season." in ops.backtest_report(run, bt)
    assert {r["name"]: r for r in CP.regression(ctx)["rows"]}["sliced"]["not_usual"] == ["Sliced by device and season."]
    # Without a choice, the same questions slice the usual way: by group only, and here there is one group.
    ops.backtest_plan(ctx, "usual", ARMS, made["queries"], made["judgments"], None, "x", "ndcg@10", 0.003)
    assert ops.backtest_run(ctx, "usual").segments == []


def test_routes(ctx, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", None)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    body = {"name": "by-route", "arms": ARMS, "queries": "demo-queries", "judgments": "demo-judgments", "hypothesis": "x"}
    bad = c.post("/api/backtests", headers=h, json={**body, "measures": ["mrr"]})
    assert bad.status_code == 400 and "must be one of the measures computed" in bad.json()["error"]
    bad = c.post("/api/backtests", headers=h, json={**body, "slices": ["nope"]})
    assert bad.status_code == 400 and "no column named nope" in bad.json()["error"]
    ch = c.get("/api/evaluations/slices?queries=demo-queries", headers=h).json()
    assert "class" in [x["id"] for x in ch["columns"]] and "class" in ch["usual"]
    assert c.get("/api/evaluations/slices?queries=no-such-set", headers=h).status_code == 404
    ok = c.post("/api/backtests", headers=h, json={**body, "measures": ["ndcg@10", "p@10"], "slices": ["class"]})
    assert ok.status_code == 200 and ok.json()["measures"] == ["ndcg@10", "p@10"] and ok.json()["slices"] == ["class"]
    ops.backtest_run(ctx, "by-route")
    r = c.get("/api/runs/by-route", headers=h).json()
    assert r["measures"] == ["ndcg@10", "p@10"] and r["not_usual"] == ["Only nDCG and Precision were computed."]
