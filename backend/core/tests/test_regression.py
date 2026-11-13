"""Regression testing: every evaluation run as one row, with the model judged, the one it is compared with and the
change on the primary metric, and the two facts that change what a result means."""
import pytest
from fastapi.testclient import TestClient

from captain_search import compare as CP
from captain_search import demo
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("shop", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def test_one_row_per_run_with_the_change_against_the_other_model(ctx):
    got = CP.regression(ctx)
    assert got["rows"], "the demo has evaluation runs"
    r = next(x for x in got["rows"] if x["name"] == "demo-tuned-vs-bm25")
    run = ctx.registry.load("backtest-run", "demo-tuned-vs-bm25", r["version"])
    p = next(c for c in run.comparisons if c.get("primary"))
    assert (r["model"], r["against"], r["metric"]) == (p["b"], p["a"], p["metric"])
    assert r["delta"] == p["delta"] and r["comparison"] == p["verdict"] and r["evidence"] == p["evidence"]
    assert r["comparison"] in ("win", "noise", "loss")
    # The two arms' own scores on that metric, so the table can show from what to what.
    assert r["value"] is not None and r["against_value"] is not None
    assert abs((r["value"] - r["against_value"]) - r["delta"]) < 1e-3
    # What changes the meaning of the result comes with every row.
    assert r["too_few_judged"] == (not run.complete) and r["own_labeler"] is False and r["independent"] == run.independent
    assert [x["created"] for x in got["rows"]] == sorted((x["created"] for x in got["rows"]), reverse=True), "newest first"
    assert "demo-tuned" in got["models"]


def test_one_models_runs_and_its_own_labeler(ctx):
    one = CP.regression(ctx, model="demo-tuned@v1")
    assert one["rows"] and all(x["model"].split("@")[0] == "demo-tuned" for x in one["rows"])
    assert CP.regression(ctx, model="nobody")["rows"] == []
    assert len(CP.regression(ctx, limit=1)["rows"]) == 1
    # An evaluation whose trained arm learned from the labeler that grades the test set says so on its rows.
    bt = ctx.registry.load("backtest", "demo-tuned-vs-bm25")
    assert all(not x["own_labeler"] for x in one["rows"])
    marked = bt.model_copy(update={"tags": {**(bt.tags or {}), "label_independence": "shared"}})
    orig = ctx.registry.load
    ctx.registry.load = lambda kind, name, v=None: marked if (kind, name) == ("backtest", "demo-tuned-vs-bm25") else orig(kind, name, v)
    try:
        rows = [x for x in CP.regression(ctx)["rows"] if x["name"] == "demo-tuned-vs-bm25"]
    finally:
        ctx.registry.load = orig
    assert rows and all(x["own_labeler"] for x in rows)


def test_the_route_filters_by_model(ctx, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", None)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    got = c.get("/api/regression", headers=h).json()
    assert got["rows"] and got["models"]
    one = c.get("/api/regression?model=demo-tuned&limit=5", headers=h).json()
    assert one["rows"] and all(x["model"].startswith("demo-tuned") for x in one["rows"]) and len(one["rows"]) <= 5
