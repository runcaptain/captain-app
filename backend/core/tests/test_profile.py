"""The profiler: per-stage latency and memory over many queries, budgets, and the p99 change between arms."""
import pytest

from captain_search import demo
from captain_search import ops
from captain_search import profile as PF
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    import os

    os.environ["CST_HOME"] = str(tmp_path_factory.mktemp("home"))
    c = init_workspace("t", str(tmp_path_factory.mktemp("b")))
    demo.seed(c, progress=lambda s: None)
    return c


def test_pipeline_profile_breaks_time_down_by_stage(ctx):
    qs = ops.rows_of(ctx, "demo-queries@v2:holdout")[1][:30]
    p = PF.profile_pipeline(ctx, "demo-tuned", qs)
    names = [s["stage"] for s in p["stages"]]
    assert names == ["query understanding", "lexical retrieval (BM25)", "boosts", "price filter", "final ranking"]
    assert all(s["p99_ms"] is not None and s["p99_ms"] >= s["p50_ms"] >= 0 for s in p["stages"])
    assert abs(sum(s["share"] for s in p["stages"]) - 1.0) < 0.05
    assert p["total"]["p99_ms"] >= p["total"]["p50_ms"] > 0 and p["queries"] == 30
    assert p["slowest"][0]["dominant_stage"] in names and p["slowest"][0]["total_ms"] >= p["slowest"][-1]["total_ms"]
    assert p["memory"]["peak_python_bytes_max"] > 0 and "not process RSS" in p["memory"]["what"]
    assert "not production" in p["measured_on"]["where"] and p["budget"] is None


def test_budgets_flag_breaches_in_words():
    prof = {"total": {"p99_ms": 150.0}, "stages": [{"stage": "lexical retrieval (BM25)", "kind": "retrieve", "p99_ms": 40.0},
                                                    {"stage": "boosts", "kind": "rules", "p99_ms": 1.0}]}
    b = PF.check_budget({"p99_ms": 120, "stages": {"retrieve": 30, "rules": 5, "rerank": 10}}, prof)
    assert not b["ok"] and b["breaches"] == [
        "p99 is 150.0 ms, over the 120 ms budget",
        "lexical retrieval (BM25) p99 is 40.0 ms, over its 30 ms budget",
        "the budget names a stage 'rerank' this pipeline doesn't have"]
    assert PF.check_budget({"p99_ms": 200}, prof)["ok"]


def test_run_profile_compares_arms(ctx):
    r = PF.profile_run(ctx, "demo-tuned-vs-bm25", max_queries=20, memory=False)
    assert [a["pipeline"].split("@")[0] for a in r["arms"]] == ["demo-bm25", "demo-tuned"]
    assert r["summary"] and "at p99 against demo-bm25" in r["summary"][0]
    assert r["arms"][0]["memory"] is None


def test_an_imported_run_skips_every_arm_with_its_reason(ctx, monkeypatch):
    import contextlib
    import io
    import pathlib
    import runpy

    seed = pathlib.Path(__file__).resolve().parents[3] / "tests" / "mod-render" / "seed-imported.py"
    import captain_search.workspace as W

    monkeypatch.setattr(W, "open_context", lambda: ctx)  # the seed script's workspace is this one
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        runpy.run_path(str(seed), run_name="__main__")
    name = out.getvalue().strip().splitlines()[-1]
    r = PF.profile_run(ctx, f"{name}@v1")
    assert r["arms"] == [] and r["queries"] == 0 and r["summary"] == []
    assert len(r["skipped"]) == 2 and all("imported rankings" in s["reason"] for s in r["skipped"])
