import json

import pytest

from captain_search import demo, ops
from captain_search.eval import metrics as M
from captain_search.eval.stats import compare, quadratic_kappa
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    c = init_workspace("t", str(tmp_path / "bucket"))
    demo.seed(c, progress=lambda s: None)
    return c


def test_metrics_match_reference_definitions():
    rel = {"a": 3, "b": 2, "c": 0, "d": 1}
    m = M.metrics(["c", "a", "x", "b"], rel)
    # hand-computed: dcg = 7/log2(3) + 3/log2(5); ideal = 7 + 3/log2(3) + 1/2
    import math
    dcg = 7 / math.log2(3) + 3 / math.log2(5)
    ideal = 7 + 3 / math.log2(3) + 1 / 2
    assert abs(m["ndcg10"] - dcg / ideal) < 1e-12
    assert m["p10"] == 0.2 and m["mrr"] == 0.5 and m["r50"] == 1.0
    assert M.judged_at(["c", "a", "x", "b"], rel) == 0.75


def test_compare_verdicts():
    a = {str(i): 0.5 for i in range(200)}
    b = {str(i): 0.6 for i in range(200)}
    assert compare(a, b, "ndcg@10", 0.003)["verdict"] == "win"
    assert compare(a, {k: v + 0.001 for k, v in a.items()}, "ndcg@10", 0.003)["verdict"] == "noise"
    assert quadratic_kappa([0, 1, 2, 3], [0, 1, 2, 3]) == 1.0


def test_demo_backtest_labels_and_logs(ctx):
    run = ctx.registry.load("backtest-run", "demo-tuned-vs-bm25")
    assert run.independent and run.complete
    assert {a.evidence.value for a in run.arms} == {"SIMULATED"}
    assert all(any(e.path.startswith("logs/") for e in a.logs) for a in run.arms)
    assert ctx.registry.verify("backtest-run", run.name, run.version) == []
    primary = next(c for c in run.comparisons if c["primary"])
    assert primary["metric"] == "ndcg@10" and primary["evidence"] == "SIMULATED"


def test_verify_reports_share_that_differs(ctx):
    r = ops.backtest_verify(ctx, "demo-tuned-vs-bm25")
    assert r["checked"] and all("share_differ" in a for a in r["arms"])


def test_seal_detects_tampering(ctx):
    assert ops.check_seal(ctx, "demo-queries@v2")["ok"]
    m = ctx.registry.load("dataset", "demo-queries", 2)
    # forge a version whose holdout list changed but kept the old seal
    forged = m.model_copy(update={"splits": {"train": m.splits["train"], "holdout": m.splits["holdout"][1:]}, "parent": 2})
    f = ctx.registry.publish(forged)
    assert not ops.check_seal(ctx, f"demo-queries@v{f.version}")["ok"]


def test_click_model_on_scoring_judgments_is_not_independent(ctx):
    pop = ops.population_create(ctx, "circular", "cascade-v1", 300, 3, click_judgments="demo-judgments@v1")
    ops.backtest_plan(ctx, "circ", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", "demo-judgments@v1",
                      ops.ref(pop), "circular check")
    assert ops.backtest_run(ctx, "circ").independent is False


def test_ndcg_needs_judgments_and_ctr_needs_population(ctx):
    with pytest.raises(ValueError, match="judgments"):
        ops.backtest_plan(ctx, "x", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", None, None, "h")
    with pytest.raises(ValueError, match="population"):
        ops.backtest_plan(ctx, "x", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", None, None, "h", primary="ctr")


def test_index_drift_blocks_backtest(ctx):
    m = ctx.registry.load("pipeline", "demo-tuned")
    fp = m.index_fingerprint.model_copy(update={"doc_count": 1})
    ctx.registry.publish(m.model_copy(update={"index_fingerprint": fp, "parent": m.version}))
    ops.backtest_plan(ctx, "drift", ["demo-bm25@v1", "demo-tuned@v2"], "demo-queries@v2:holdout", "demo-judgments@v1", None, "h")
    with pytest.raises(ValueError, match="changed since it was published"):
        ops.backtest_run(ctx, "drift")


def test_judge_reuses_previous_grades(ctx):
    j = ops.judge_run(ctx, "demo-queries@v2:holdout", ["demo-bm25@v1", "demo-tuned@v1"], "heuristic", "demo-judgments")
    assert j.version == 2 and j.provenance.params["new_pairs"] == 0


def test_demo_tier_has_no_gates_live_tier_does(ctx):
    assert ops.current(ctx, "staging")["pipeline"] == "demo-tuned@v1"
    with pytest.raises(ops.GateFailed, match="confirm"):
        ops.promote(ctx, "demo-tuned@v1", "prod")
    with pytest.raises(ops.GateFailed) as e:
        ops.promote(ctx, "demo-tuned@v1", "prod", confirm=True)
    assert any(r["gate"] == "evidence" and not r["ok"] for r in e.value.results)
    d = ops.promote(ctx, "demo-tuned@v1", "prod", override_reason="demo for Edgar", confirm=True)
    assert d.override_reason == "demo for Edgar" and "evidence" in d.gate_results["overridden"]


def test_rollback(ctx):
    d = ops.rollback(ctx, "staging")
    assert d.pipeline == "demo-bm25@v1" and ops.current(ctx, "staging")["pipeline"] == "demo-bm25@v1"


def test_human_labels_make_evidence_human(ctx, tmp_path):
    from captain_search.data.datasets import load_table

    rows = load_table(ctx, ctx.registry.load("dataset", "demo-judgments")).to_pylist()
    f = tmp_path / "h.jsonl"
    f.write_text("\n".join(json.dumps({"query_id": r["query_id"], "product_id": r["product_id"], "grade": r["grade"]}) for r in rows))
    ops.judge_import(ctx, str(f), "human-labels")
    assert ops.judge_calibrate(ctx, "demo-judgments@v1", "human-labels@v1")["kappa"] == 1.0
    ops.backtest_plan(ctx, "hum", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", "human-labels@v1", None, "h")
    run = ops.backtest_run(ctx, "hum")
    assert {a.evidence.value for a in run.arms} == {"HUMAN_GRADED"}


def test_lineage_has_edges(ctx):
    g = ops.lineage(ctx)
    rel = {e["relation"] for e in g["edges"]}
    assert {"produced_by", "derived_from", "evaluated_on", "deployed_from"} <= rel
