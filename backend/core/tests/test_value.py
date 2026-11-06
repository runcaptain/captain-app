"""Cost against value, live monitoring with retrain-and-review, and segments and surfaces."""
import random

import pyarrow as pa
import pytest

from captain_search import costs as COST
from captain_search import demo, ops
from captain_search import monitor as MON
from captain_search import retrain as RT
from captain_search import segments as SEG
from captain_search import workflows as WF
from captain_search.data.datasets import snapshot, to_parquet
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    import os

    home = str(tmp_path_factory.mktemp("home"))
    old = os.environ.get("CST_HOME")
    os.environ["CST_HOME"] = home
    c = init_workspace("t", str(tmp_path_factory.mktemp("bucket")))
    demo.seed(c, progress=lambda s: None)
    if old is not None:
        os.environ["CST_HOME"] = old
    return c, home


@pytest.fixture
def ctx(seeded, monkeypatch):
    c, home = seeded
    monkeypatch.setenv("CST_HOME", home)  # the CLI steps a workflow runs look the workspace up here
    return c


def test_cost_history_and_roi_flags(ctx):
    COST.set_cost(ctx, "demo-bm25@v1", {"serve_usd_per_1k": 0.002, "train_usd": 0})
    COST.set_cost(ctx, "demo-tuned@v1", {"serve_usd_per_1k": 0.003, "train_usd": 40})
    c = COST.set_cost(ctx, "demo-tuned@v1", {"train_usd": 36})
    assert c["train_usd"] == 36 and len(c["history"]) == 2 and c["history"][-1]["from"] == {"train_usd": 40.0}
    with pytest.raises(ValueError):
        COST.set_cost(ctx, "demo-tuned@v1", {"train_usd": -1})
    with pytest.raises(ValueError):
        COST.set_cost(ctx, "demo-tuned@v1", {"bogus": 1})
    a, b = {"serve_usd_per_1k": 0.002, "train_usd": 0}, {"serve_usd_per_1k": 0.003, "train_usd": 36}
    assert COST.roi(a, b, 0.02, "win", 100000)["flag"] == "worth-it"
    assert COST.roi(a, b, 0.0, "noise", 100000)["flag"] == "retire"
    assert COST.roi(b, a, 0.02, "win", 100000)["flag"] == "cheaper-and-better"
    r = COST.roi(a, b, 0.02, "win", 100000)
    assert r["extra_monthly_usd"] == round(0.001 * 100000 * 30 / 1000 + 36 / 12, 2)
    assert COST.roi(a, b, 0.02, "win", None)["extra_monthly_usd"] is None  # needs traffic to price serving
    assert COST.roi({}, {}, 0.02, "win", 1000)["flag"] is None


def test_backtest_run_carries_cost_and_segments(ctx):
    COST.set_settings(ctx, 200000)
    run = ops.backtest_run(ctx, "demo-tuned-vs-bm25")
    assert all(a.cost.get("p99_ms") is not None for a in run.arms) and run.wall_s is not None
    prim = next(c for c in run.comparisons if c["primary"])
    assert prim["cost"]["flag"] in ("worth-it", "retire") and prim["cost"]["extra_monthly_usd"] is not None
    kinds = {s["kind"] for s in run.segments}
    assert "class" in kinds
    assert all(s["verdict"] == "too few" for s in run.segments if s["arm"] != run.arms[0].arm and s["n"] < SEG.MIN_N)
    text = ops.backtest_report(run, ctx.registry.load("backtest", "demo-tuned-vs-bm25"))
    assert "## Cost" in text and "## Segments" in text
    ledger = {r["pipeline"]: r for r in COST.ledger(ctx)}
    assert ledger["demo-tuned@v1"]["roi"] is not None


def test_traffic_bands_follow_weight():
    qs = [{"query_id": f"q{i}", "weight": w} for i, w in enumerate([50, 20, 10, 10, 5, 5])]
    b = SEG.traffic_bands(qs)
    assert b["q0"] == "head" and b["q1"] == "torso" and b["q5"] == "tail"
    assert SEG.traffic_bands([{"query_id": "x"}]) == {}


def test_surface_guardrails_gate_live_promote(ctx):
    ops.backtest_plan(ctx, "surf", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", "demo-judgments@v1", None,
                      "tuned holds up on related", surfaces=[{"name": "related",
                      "queries": "demo-historic-replay/queries@v2:holdout", "judgments": "demo-historic-replay/judgments@v1",
                      "guardrails": [{"metric": "ndcg@10", "min": 0.99}]}])
    run = ops.backtest_run(ctx, "surf")
    sf = run.surfaces[0]
    assert sf["surface"] == "related" and sf["guardrails"] and sf["guardrails"][0]["ok"] is False
    env = ops.env_load(ctx, "prod")
    gates = {g["gate"]: g for g in ops.gates_for(ctx, env, "demo-tuned@v1")}
    assert gates["surface_guardrails"]["ok"] is False
    with pytest.raises(ValueError):
        ops.backtest_plan(ctx, "bad", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", "demo-judgments@v1", None, "h",
                          surfaces=[{"name": "x", "queries": "demo-queries@v2", "judgments": "demo-judgments@v1",
                                     "guardrails": [{"metric": "ctr", "min": 0.1}]}])


def _degraded_logs(ctx):
    _, rows, _ = ops.rows_of(ctx, "logs/demo-search-sessions@v1")
    rng = random.Random(1)
    for r in rows:
        if r["clicked"] and rng.random() < 0.4:
            r["clicked"] = r["purchased"] = False
    t = pa.Table.from_pylist(rows)
    m = snapshot(ctx, "logs/degraded", t, kind=DatasetKind.events, dry_run=True,
                 provenance=Provenance(source="generated", params={}), description="40% of clicks removed")
    e = ctx.registry.put_blob(to_parquet(t), "data/part-0.parquet")
    return ctx.registry.publish(m.model_copy(update={"files": [e]}))


def test_monitor_detects_a_drop(ctx):
    a = MON.record(ctx, "staging", "logs/demo-search-sessions@v1")
    assert a["pipeline"] == "demo-tuned@v1" and 0 < a["metrics"]["ctr"] <= 1
    assert MON.check(ctx, "staging")["degraded"] is False  # one snapshot cannot be compared
    _degraded_logs(ctx)
    MON.record(ctx, "staging", "logs/degraded@v1")
    r = MON.check(ctx, "staging", "ctr", 0.05)
    assert r["degraded"] is True and r["drop"] >= 0.05
    with pytest.raises(ValueError):
        MON.record(ctx, "staging", "demo-catalog@v1")  # not an events table


def test_retrain_needs_consent_and_records_cost(ctx):
    with pytest.raises(ValueError, match="declares no training command"):
        RT.retrain(ctx, "demo-bm25", local_exec=True)
    pm = ctx.registry.load("pipeline", "demo-tuned")
    draft = {"name": "demo-tuned", "engine": pm.engine, "catalog": pm.catalog, "spec": {**pm.spec, "train": {
        "command": "printf 'cost:\\n  train_usd: 3.5\\ntraining_commit: abc\\n' > \"$CST_TRAIN_OUT\""}}}
    v2 = ops.pipeline_publish(ctx, draft)
    with pytest.raises(ValueError, match="--local-exec"):
        RT.retrain(ctx, "demo-tuned")
    r = RT.retrain(ctx, "demo-tuned", local_exec=True)
    new = ctx.registry.load("pipeline", "demo-tuned", r["version"])
    assert new.parent == v2.version and new.training_commit == "abc"
    assert COST.get(ctx, r["pipeline"])["train_usd"] == 3.5
    bad = {**draft, "spec": {**pm.spec, "train": {"command": "exit 7"}}}
    ops.pipeline_publish(ctx, bad)
    with pytest.raises(ValueError, match="exit code 7"):
        RT.retrain(ctx, "demo-tuned", local_exec=True)


def test_workflow_when_skips_dependents(ctx):
    steps = [{"id": "a", "command": ["deploy", "status"], "args": {"env": "staging"}},
             {"id": "b", "command": ["deploy", "status"], "args": {"env": "staging"}, "needs": ["a"], "when": "${params.go}"},
             {"id": "c", "command": ["deploy", "status"], "args": {"env": "staging"}, "needs": ["b"]}]
    WF.save(ctx, "cond", steps, {"go": "false"})
    r = WF.run(ctx, "cond")
    st = {s["id"]: s for s in r.steps}
    assert r.status == "done" and st["a"]["status"] == "done"
    assert st["b"]["status"] == "skipped" and st["b"]["reason"] == "condition false"
    assert st["c"]["status"] == "skipped" and "needs skipped step b" in st["c"]["reason"]
    r2 = WF.run(ctx, "cond", params={"go": "yes"})
    assert all(s["status"] == "done" for s in r2.steps)
    assert "retrain-and-review" in WF.TEMPLATES and not any(s["command"][:2] == ["deploy", "promote"] for t in WF.TEMPLATES.values() for s in t["steps"])
