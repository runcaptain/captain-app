"""The investigation-to-decision workflow from the 2026-11-06 spec (S1, S3, S6, S7)."""
import os

import pytest

from captain_search import demo
from captain_search import investigations as INV
from captain_search.storage.backend import Conflict
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
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
    monkeypatch.setenv("CST_HOME", home)
    monkeypatch.delenv("CST_AGENT", raising=False)
    return c


def test_anchor_story_from_question_to_decision(ctx, monkeypatch):
    # an agent opens the question with no plan, and missing information is explicit
    monkeypatch.setenv("CST_AGENT", "claude")
    d = INV.create(ctx, "Do returns and recent browsing improve ranking?")
    assert d["state"] == "draft" and d["initiator"]["agent"] == "claude" and "baseline" in " ".join(d["missing"])
    with pytest.raises(ValueError, match="Missing"):
        INV.run(ctx, d["id"])
    s = INV.signal_create(ctx, "recent-browse-overlap", "logs/demo-search-sessions", "deep-search@v1",
                          "shoppers who browse a category click its products", "at query time", investigation=d["id"])
    assert s["availability"]["status"] == "unchecked" and s["leakage"]["status"] == "unchecked"
    d = INV.revise(ctx, d["id"], "The tuned pipeline beats BM25 on nDCG@10.", "first plan", baseline="demo-bm25@v1",
                   candidates=["demo-tuned@v1"], queries="demo-queries@v2:holdout", judgments="demo-judgments@v1")
    assert d["plan"]["rev"] == 2 and not d["missing"] and s["id"] in d["signals"]
    d = INV.run(ctx, d["id"], idempotency_key="k1")
    assert d["state"] == "ready_for_review" and d["runs"][-1]["status"] == "done" and d["runs"][-1]["rev"] == 2
    again = INV.run(ctx, d["id"], idempotency_key="k1")
    assert again["reused"]["run"] == d["runs"][-1]["run"] and len(again["runs"]) == 1  # a retry doesn't run twice
    # revising later never rewrites what the run tested
    INV.revise(ctx, d["id"], "Same, with a tighter MDE.", mde=0.01)
    assert INV.get(ctx, d["id"])["runs"][0]["rev"] == 2
    # the agent can't decide
    with pytest.raises(ValueError, match="person decides"):
        INV.decide(ctx, d["id"], "online-test", "x", [], "y")
    monkeypatch.delenv("CST_AGENT")
    with pytest.raises(ValueError, match="REAL_TRAFFIC"):
        INV.decide(ctx, d["id"], "online-test", "tuned wins offline", [], "A/B test it", claim="online-lift")
    dec = INV.decide(ctx, d["id"], "online-test", "tuned wins offline on the holdout", ["synthetic store"], "A/B test it",
                     claim="offline-improvement")
    assert dec["evidence_label"] == "SIMULATED" and any("not proof of online" in x for x in dec["limitations"])
    d = INV.get(ctx, d["id"])
    assert d["state"] == "decided" and d["decision"]["outcome"] == "online-test"
    with pytest.raises(ValueError, match="decided"):
        INV.revise(ctx, d["id"], "more")
    assert [r["id"] for r in INV.listing(ctx, q="returns browsing")] == [d["id"]]
    ex = INV.export(ctx, d["id"])
    assert ex["format"] == "captain.investigation/v1" and ex["decision"]["id"] == dec["id"] and ex["runs"] and ex["signals"]


def test_signal_checks_need_a_method(ctx):
    s = INV.signal_create(ctx, "returns-rate", "warehouse.returns", "select ...", "high returns mean poor fit")
    with pytest.raises(ValueError, match="how it was checked"):
        INV.signal_check(ctx, s["id"], "leakage", "ok", "")
    s = INV.signal_check(ctx, s["id"], "leakage", "ok", "returns joined only from orders before the query timestamp")
    assert s["leakage"]["status"] == "ok" and s["history"][-1]["from"] == "unchecked"


def test_policy_limits_agents_and_only_people_change_it(ctx, monkeypatch):
    d = INV.create(ctx, "q", "h", baseline="demo-bm25@v1", candidates=["demo-tuned@v1"], queries="demo-queries@v2:holdout",
                   judgments="demo-judgments@v1")
    INV.set_policy(ctx, {"agent_runs": False})
    monkeypatch.setenv("CST_AGENT", "claude")
    with pytest.raises(ValueError, match="does not let agents"):
        INV.run(ctx, d["id"])
    with pytest.raises(ValueError, match="cannot change the execution policy"):
        INV.set_policy(ctx, {"agent_runs": True})
    monkeypatch.delenv("CST_AGENT")
    INV.set_policy(ctx, {"agent_runs": True})


def test_concurrent_revisions_conflict_instead_of_overwriting(ctx):
    d = INV.create(ctx, "q2", "h")
    INV.revise(ctx, d["id"], "h2", expected_revision=1)
    with pytest.raises(Conflict):
        INV.revise(ctx, d["id"], "h3 from a stale copy", expected_revision=1)


def test_prompt_marks_unknowns_and_never_holds_secrets(ctx):
    d = INV.create(ctx, "Why do numeric queries regress?")
    t = INV.prompt(ctx, d["id"])
    assert "UNKNOWN" in t and "Missing before this can run" in t and d["id"] in t
    assert "token" not in t.lower().replace("token from `cst` config", "")
    with pytest.raises(ValueError):
        INV.prompt(ctx, d["id"], "launch-rockets")


def test_onboarding_reads_real_progress(ctx):
    from captain_search import onboarding as OB

    s = OB.status(ctx)
    done = {x["id"]: x["done"] for x in s["steps"]}
    assert done["workspace"] and done["data"] and done["pipeline"] and done["run"]
    # Experiments left the screens: the tour no longer asks for one or for a recorded decision.
    assert "investigation" not in done and "decision" not in done
    assert s["counts"]["datasets"] > 0 and s["workspace"]["name"] == "t"


def test_side_by_side_queries_and_compatibility(ctx):
    from captain_search import compare as CP

    page = CP.queries(ctx, "demo-tuned-vs-bm25", sort="delta", limit=5)
    assert page["total"] > 5 and len(page["rows"]) == 5
    assert [r["delta"] for r in page["rows"]] == sorted(r["delta"] for r in page["rows"])
    worst = page["rows"][0]
    reg = CP.queries(ctx, "demo-tuned-vs-bm25", only="regressions", limit=500)
    assert all(r["delta"] < 0 for r in reg["rows"])
    sbs = CP.query(ctx, "demo-tuned-vs-bm25", worst["query_id"], k=10)
    assert len(sbs["a"]["items"]) <= 10 and sbs["a"]["items"][0]["rank"] == 1
    assert abs((sbs["b"]["ndcg10"] - sbs["a"]["ndcg10"]) - worst["delta"]) < 1e-4
    assert all(i["moved"] is None or i["moved"] == i["other_rank"] - i["rank"] for i in sbs["b"]["items"])
    comp = CP.compatibility(ctx, "demo-tuned-vs-bm25")
    assert any(w["code"] == "offline" for w in comp["warnings"])
    h = CP.histogram(ctx, "demo-tuned-vs-bm25")
    assert sum(h["counts"]) == h["n"] and len(h["edges"]) == len(h["counts"]) + 1
    x = CP.compare_runs(ctx, "demo-tuned-vs-bm25", "demo-historic-replay")
    assert not x["comparable"] and {i["code"] for i in x["issues"]} >= {"different-queries", "different-mode"}


def test_one_daemon_serves_each_space_workspace(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import server
    from captain_search.workspace import init_workspace

    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(server, "_ctx", None)
    monkeypatch.setattr(server, "_ctxs", {})
    init_workspace("alpha", str(tmp_path / "a"))
    b = init_workspace("beta", str(tmp_path / "b"))
    INV.create(b, "only in beta")
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert c.get("/api/investigations", headers=h).json() == []
    assert [i["title"] for i in c.get("/api/investigations", headers={**h, "X-CST-Workspace": "beta"}).json()] == ["only in beta"]
    assert [i["title"] for i in c.get("/api/investigations?workspace=beta", headers=h).json()] == ["only in beta"]
    r = c.get("/api/investigations", headers={**h, "X-CST-Workspace": "nope"})
    assert r.status_code == 400 and "unknown workspace" in r.json()["error"]
