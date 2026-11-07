"""The what-if sandbox: sweep knobs on a sample then the full set, read the ripple, promote a setting."""
import pytest

from captain_search import demo
from captain_search import sandbox as SB
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    import os

    os.environ["CST_HOME"] = str(tmp_path_factory.mktemp("home"))
    c = init_workspace("t", str(tmp_path_factory.mktemp("b")))
    demo.seed(c, progress=lambda s: None)
    return c


Q, J = "demo-queries@v2:holdout", "demo-judgments"


def test_sweep_two_passes_with_current_as_baseline(ctx):
    r = SB.sweep(ctx, "demo-tuned", Q, J, {"boosts.0.weight": {"values": [1.0, 1.15, 1.5]}, "k1": {"min": 0.8, "max": 1.6, "steps": 3}},
                 sample=40, top=2)
    assert r["current"] == {"boosts.0.weight": 1.15, "k1": 1.2} and r["knobs"]["k1"] == [0.8, 1.2, 1.6]
    assert len(r["sample"]["cells"]) == 9 and r["sample"]["n"] <= 40
    cur = next(c for c in r["sample"]["cells"] if c["is_current"])
    assert cur["delta"] == 0
    assert len(r["full"]["cells"]) == 2 and r["full"]["n"] > r["sample"]["n"]
    assert all(c["verdict"] in ("win", "loss", "noise") for c in r["full"]["cells"])
    assert r["evidence"] == "SIMULATED" and "doesn't replace" in r["note"]


def test_sweep_refuses_unreadable_or_unknown_knobs(ctx):
    with pytest.raises(ValueError, match="at most three"):
        SB.sweep(ctx, "demo-tuned", Q, J, {"a": {"values": [1]}, "b": {"values": [1]}, "k1": {"values": [1]}, "x": {"values": [1]}})
    with pytest.raises(ValueError, match="no rerank.depth"):
        SB.sweep(ctx, "demo-bm25", Q, J, {"rerank.depth": {"values": [10, 20]}})
    with pytest.raises(ValueError, match="name the knobs"):
        SB.sweep(ctx, "demo-bm25", Q, J)


def test_ripple_and_promote(ctx):
    rp = SB.ripple(ctx, "demo-tuned", Q, J, {"boosts.0.weight": 3.0}, must_not_break=["voltwave backpack"])
    assert rp["moved_most"] and rp["moved_most"][0]["top10_changed"] >= rp["moved_most"][-1]["top10_changed"]
    assert rp["must_not_break"] and rp["must_not_break"][0]["query"] == "voltwave backpack"
    assert "queries change their top 10" in rp["summary"]
    p = SB.promote(ctx, "demo-tuned", {"boosts.0.weight": 1.3})
    assert p["pipeline"] == "demo-tuned@v2" and p["from"] == "demo-tuned@v1" and "Pre-register" in p["next"]
    v2 = ctx.registry.load("pipeline", "demo-tuned", 2)
    assert v2.spec["boosts"][0]["weight"] == 1.3 and v2.spec["synonyms"]
