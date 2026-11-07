"""The query debugger: per-stage traces, exact BM25 contributions, two-arm divergence, engines that can't explain."""
import pytest

from captain_search import demo
from captain_search import explain as EX
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    import os

    os.environ["CST_HOME"] = str(tmp_path_factory.mktemp("home"))
    c = init_workspace("t", str(tmp_path_factory.mktemp("b")))
    demo.seed(c, progress=lambda s: None)
    return c


def test_bm25_trace_is_exact_and_names_every_stage(ctx):
    t = EX.trace(ctx, "demo-tuned", "earbuds under 50")
    kinds = [s["kind"] for s in t["stages"]]
    assert kinds == ["understand", "retrieve", "rules", "filter", "final"]
    u = t["stages"][0]
    assert "earphones" in u["expanded_with"] and u["synonyms"] == {"earbuds": ["earphones"]}
    assert any("price limit: 50" in n for n in u["notes"])
    for it in t["stages"][1]["items"]:  # contributions add up to the score, exactly
        assert abs(sum(it["score_parts"].values()) - it["score"]) < 1e-4
        assert all(k.split(":")[0] in ("title", "brand", "category", "description") for k in it["score_parts"])
    rules = t["stages"][2]
    assert any(it["fired"] == ["in_stock = True: x1.15"] for it in rules["items"])
    boosted = next(it for it in rules["items"] if it["fired"])
    assert abs(boosted["score_parts"]["after"] - boosted["score_parts"]["before"] * 1.15) < 1e-4
    final = t["stages"][-1]
    assert [it["rank"] for it in final["items"]] == list(range(1, len(final["items"]) + 1))


def test_unknown_terms_are_called_out(ctx):
    u = EX.trace(ctx, "demo-bm25", "zzqx tent")["stages"][0]
    assert u["unknown_terms"] == ["zzqx"] and "no product contains: zzqx" in u["notes"]


def test_two_arms_from_a_run_name_the_first_divergence(ctx):
    from captain_search import compare as CP

    rows = CP.queries(ctx, "demo-tuned-vs-bm25", sort="delta", limit=200)["rows"]
    worse = [r for r in rows if r.get("ndcg_b") is not None and r["ndcg_b"] < r["ndcg_a"]]
    better = [r for r in rows if r.get("ndcg_b") is not None and r["ndcg_b"] > r["ndcg_a"]]
    pick = (better or worse)[0]
    d = EX.from_run(ctx, "demo-tuned-vs-bm25", pick["query_id"])
    assert d["graded"] and d["a"]["pipeline"].startswith("demo-bm25") and d["b"]["pipeline"].startswith("demo-tuned")
    assert any(it.get("grade") is not None for it in d["a"]["stages"][-1]["items"])
    fd = d["first_divergence"]
    if fd:
        assert fd["kind"] in ("retrieve", "rules", "filter", "final") and (fd["lost"] or fd["ranked_lower"])


def test_first_divergence_logic():
    a = {"stages": [{"stage": "r", "kind": "retrieve", "items": [{"id": "x", "rank": 1}, {"id": "y", "rank": 2}]},
                    {"stage": "f", "kind": "final", "items": [{"id": "x", "rank": 1}, {"id": "y", "rank": 2}]}]}
    b = {"stages": [{"stage": "r", "kind": "retrieve", "items": [{"id": "x", "rank": 1}, {"id": "y", "rank": 2}]},
                    {"stage": "f", "kind": "final", "items": [{"id": "x", "rank": 1}]}]}
    fd = EX.first_divergence(a, b, {"x": 3, "y": 2})
    assert fd["kind"] == "final" and fd["lost"] == [{"id": "y", "rank_a": 2}]
    assert EX.first_divergence(a, b, {"z": 0}) is None


def test_engines_without_explain_say_so(monkeypatch):
    from captain_search.engines import HTTPEngine

    eng = HTTPEngine({"url": "http://x"})
    monkeypatch.setattr(eng, "search", lambda q, k=50: ["a", "b"])
    s = EX.explain_engine(eng, "q")
    assert len(s) == 1 and s[0]["explained"] is False and "no explanation provided" in s[0]["notes"][0]


def test_elasticsearch_explain_is_read(monkeypatch):
    import httpx

    from captain_search.engines import ElasticEngine

    resp = {"hits": {"hits": [{"_id": "p1", "_score": 4.2, "_source": {"title": "Red tent"}, "_explanation": {
        "value": 4.2, "description": "sum of:", "details": [
            {"value": 3.0, "description": "weight(title:tent in 0)", "details": []},
            {"value": 1.2, "description": "weight(description:red in 0)", "details": []}]}}]}}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return resp

    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None: seen.update(body=json) or R())
    s = EX.explain_engine(ElasticEngine({"url": "http://es:9200", "index": "p"}), "red tent")
    assert seen["body"]["explain"] is True
    it = s[0]["items"][0]
    assert it["id"] == "p1" and it["score_parts"] == {"weight(title:tent in 0)": 3.0, "weight(description:red in 0)": 1.2}
