"""Proposals with human approval, human grading, label definitions, replay and scale benchmarks."""
import os

import pytest

from captain_search import bench as BE
from captain_search import demo, ops
from captain_search import grading as GR
from captain_search import labels as LB
from captain_search import proposals as PR
from captain_search import replay as RP
from captain_search.model.entities import Evidence
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    home = str(tmp_path_factory.mktemp("home"))
    old = os.environ.get("CST_HOME")
    os.environ["CST_HOME"] = home
    c = init_workspace("t", str(tmp_path_factory.mktemp("bucket")))
    out = demo.seed(c, progress=lambda s: None)
    if old is not None:
        os.environ["CST_HOME"] = old
    return c, home, out


@pytest.fixture
def ctx(seeded, monkeypatch):
    c, home, _ = seeded
    monkeypatch.setenv("CST_HOME", home)
    monkeypatch.delenv("CST_AGENT", raising=False)
    return c


def test_agents_propose_but_only_people_approve(ctx, seeded, monkeypatch):
    pid = seeded[2]["proposal"]
    d = PR.get(ctx, pid)
    assert d["agent"] == "demo-agent" and d["status"] == "proposed"
    with pytest.raises(ValueError, match="Only an approved proposal runs"):
        PR.run(ctx, pid)
    monkeypatch.setenv("CST_AGENT", "claude")
    with pytest.raises(ValueError, match="agent session cannot approve"):
        PR.review(ctx, pid, "approved")
    mine = PR.create(ctx, "t", "h", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", "demo-judgments@v1")
    assert mine["agent"] == "claude"
    monkeypatch.delenv("CST_AGENT")
    # a live promote of an agent-proposed pipeline waits for a person's approval
    gates = {g["gate"]: g for g in ops.gates_for(ctx, ops.env_load(ctx, "prod"), "demo-tuned@v1")}
    assert gates["human_approval"]["ok"] is False
    PR.review(ctx, pid, "approved", "looks sound")
    PR.review(ctx, mine["id"], "approved")
    gates = {g["gate"]: g for g in ops.gates_for(ctx, ops.env_load(ctx, "prod"), "demo-tuned@v1")}
    assert gates["human_approval"]["ok"] is True
    r = PR.run(ctx, pid)
    assert r["status"] == "ran" and r["run"] and r["result"]["metric"] == "ndcg@10"
    with pytest.raises(ValueError):
        PR.review(ctx, pid, "rejected")  # already ran


def test_human_grading_publishes_human_evidence(ctx, monkeypatch):
    st = GR.status(ctx, "demo-grading")
    assert st["pairs"] > 0 and st["graded"] == 0
    for who in ("ana@x", "bo@x"):
        for _ in range(15):
            n = GR.next_pair(ctx, "demo-grading", grader=who)
            if n["done"]:
                break
            GR.grade(ctx, "demo-grading", n["pair"]["id"], 2 if "shoe" in n["pair"]["query"] else 1, grader=who)
    st = GR.status(ctx, "demo-grading")
    assert set(st["graders"]) == {"ana@x", "bo@x"} and st["agreement_pairs"] >= 1
    with pytest.raises(ValueError):
        GR.grade(ctx, "demo-grading", "nope", 2)
    with pytest.raises(ValueError):
        GR.grade(ctx, "demo-grading", n["pair"]["id"], 5)
    m = GR.publish(ctx, "demo-grading", "demo-human")
    assert m.judgment_source == "human" and m.row_count == st["graded"]
    assert "demo-human@v1" in [p["dataset"] for p in GR.status(ctx, "demo-grading")["published"]]
    bt = ops.backtest_plan(ctx, "human-scored", ["demo-bm25@v1", "demo-tuned@v1"], "demo-queries@v2:holdout", "demo-human@v1",
                           None, "scored by people")
    run = ops.backtest_run(ctx, bt.name)
    assert run.arms[0].evidence == Evidence.HUMAN_GRADED


def test_labels_are_versioned_and_computed_in_place(ctx):
    v2 = LB.define(ctx, "deep-search", "max(case when clicked and position <= 1 then 1 else 0 end)", "search")
    assert v2.version == 2
    p1 = LB.preview(ctx, "deep-search@v1", "logs/demo-search-sessions@v1")
    p2 = LB.preview(ctx, "deep-search@v2", "logs/demo-search-sessions@v1")
    assert p1["groups"] == p2["groups"] and p2["mean"] <= p1["mean"]
    m = LB.compute(ctx, "deep-search@v2", "logs/demo-search-sessions@v1", "labels/deep-search")
    assert m.row_count == p2["groups"] and m.provenance.params["label"] == "deep-search@v2"
    with pytest.raises(ValueError):
        LB.define(ctx, "evil", "max(1); copy t to 'x'")
    with pytest.raises(ValueError, match="no user_id"):
        LB.define(ctx, "per-user", "count(*)", "user")
        LB.preview(ctx, "per-user", "logs/demo-search-sessions@v1")
    imp = LB.define(ctx, "clicked", "max(case when clicked then 1 else 0 end)", "impression")
    assert LB.preview(ctx, f"clicked@v{imp.version}", "logs/demo-search-sessions@v1")["groups"] == 60000


def test_replay_finds_the_best_threshold(ctx):
    r = ctx.registry.load("replay", "shopping-trigger")
    assert r.evidence == Evidence.SIMULATED  # synthetic logs stay simulated
    rates = [p["trigger_rate"] for p in r.curve]
    assert rates == sorted(rates, reverse=True) and rates[0] == 1.0
    kinds = {o["kind"] for p in r.curve for o in p["outcomes"].values()}
    assert kinds == {"direct", "directional"}
    best = max(r.curve, key=lambda p: p["outcomes"]["engaged"]["delta_vs_never"] or -1)
    assert 0 < best["trigger_rate"] < 1  # treating everyone is not best when low-uplift requests lose
    with pytest.raises(ValueError, match="no column"):
        RP.sweep(ctx, "bad", "logs/shopping-trigger-holdout@v1", "nope", "treated", ["engaged"])


def test_bench_rows_records_a_measured_run(ctx):
    r = BE.rows(ctx, "demo-catalog@v1", page=50)
    assert r["rows"] == 480 and len(r["timings"]) == 6 and r["where"] == "local disk"
    assert BE.listing(ctx)[0]["id"] == r["id"]


def test_live_promote_is_a_handoff_that_deploys_nothing(ctx, monkeypatch):
    import httpx

    from captain_search import handoff as HO

    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Captain must not call a live target")))
    before = ops.current(ctx, "prod")
    d = ops.promote(ctx, "demo-tuned@v1", "prod", override_reason="readiness review", confirm=True)
    assert d.action == "handoff" and d.handoff and d.gate_results["target"]["executed"] is False
    assert ops.current(ctx, "prod") == before  # nothing is live until the customer's system says so
    b = HO.get(ctx, d.handoff)
    assert any("offline evidence only" in x for x in b["limitations"]) and b["readiness"]["override_reason"] == "readiness review"
    HO.link(ctx, d.handoff, "argo", "rollout-42")
    assert ops.current(ctx, "prod")["pipeline"] == "demo-tuned@v1"
    b = HO.outcome(ctx, d.handoff, "conversion", 0.012, "optimizely report 77", ci_low=0.004, ci_high=0.02)
    assert b["links"][-1]["evidence"] == "REAL_TRAFFIC"
    with pytest.raises(ValueError, match="Captain stays outside production"):
        ops.rollback(ctx, "prod", confirm=True)


def test_grading_queue_over_http(ctx):
    from fastapi.testclient import TestClient

    from captain_search import server

    server._ctx = ctx
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    r = c.post("/api/grading", headers=h, json={"name": "http-q", "queries": "demo-queries@v2:holdout",
                                                 "pipelines": "demo-bm25@v1,demo-tuned@v1", "depth": 3, "sample": 5})
    assert r.status_code == 200 and r.json()["pairs"] > 0
    assert c.post("/api/grading", headers=h, json={"name": "http-q", "queries": "demo-queries@v2", "pipelines": ["demo-bm25@v1"]}).status_code == 400
    assert c.post("/api/grading", headers=h, json={"name": "x", "queries": "demo-queries@v2", "pipelines": []}).status_code == 400
    server._ctx = None
