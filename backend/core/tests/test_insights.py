"""A dataset's Insights: what uses it, the notebooks that name it, who works with it, and recent activity."""
import pytest
from fastapi.testclient import TestClient

from captain_search import demo
from captain_search import notebook as NB
from captain_search.data import insights as IN
from captain_search.storage.backend import NotFound
from captain_search.workspace import init_workspace


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    import os

    os.environ["CST_HOME"] = str(tmp_path_factory.mktemp("home"))
    c = init_workspace("t", str(tmp_path_factory.mktemp("b")))
    demo.seed(c, progress=lambda s: None)
    return c


def _nb(cells):
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"id": f"c{i}", "cell_type": "code", "metadata": {}, "source": s, "outputs": [], "execution_count": None} for i, s in enumerate(cells)]}


def test_what_uses_a_catalog(ctx):
    NB.save_revision(ctx, "reads-catalog", _nb(["df = cst.dataset('demo-catalog')", "print(1)", "x = 'demo-catalog'"]), "t")
    NB.save_revision(ctx, "other", _nb(["cst.dataset('demo-catalog-v2')"]), "t")  # a longer name isn't this one
    r = IN.dataset_insights(ctx, "demo-catalog")
    pipes = [e["label"] for e in r["used_by"]["pipeline"]]
    assert "demo-bm25@v1" in pipes and "demo-tuned@v1" in pipes
    assert {e["label"] for e in r["used_by"]["run"]} >= {"demo-tuned-vs-bm25@v1"}
    # A run opens in Runs: the Compare screen is leaving the app.
    assert all(e["section"] == "runs" for e in r["used_by"]["run"])
    assert not any(e["name"] == "demo-catalog" for es in r["used_by"].values() for e in es), "not itself"
    assert r["notebooks"] == [{"name": "reads-catalog", "cells": 2}]
    assert r["counts"]["notebooks"] == 1 and r["counts"]["pipeline"] == len(pipes)
    assert r["people"] and r["people"][0]["versions"] >= 1
    assert r["versions"][-1]["version"] == r["versions_total"]
    ats = [a["at"] for a in r["activity"]]
    assert ats == sorted(ats, reverse=True), "newest first"


def test_unknown_dataset_is_said_in_words(ctx):
    with pytest.raises(NotFound, match="no dataset named nope"):
        IN.dataset_insights(ctx, "nope")


def test_route(ctx, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", ctx)
    c = TestClient(server.app, raise_server_exceptions=False)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    r = c.get("/api/datasets/demo-judgments/insights", headers=h)
    assert r.status_code == 200 and r.json()["used_by"]["run"][0]["label"] == "demo-tuned-vs-bm25@v1"
    assert c.get("/api/datasets/nope/insights", headers=h).status_code == 404


def test_datasets_list_carries_columns_and_searches_them(ctx, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", ctx)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    rows = c.get("/api/datasets", headers=h).json()
    cat = next(r for r in rows if r["name"] == "demo-catalog")
    assert "price" in cat["columns"] and "title" in cat["columns"]
    hits = [r["name"] for r in c.get("/api/datasets?q=price", headers=h).json()]
    assert "demo-catalog" in hits and "demo-queries" not in hits
    assert [r["name"] for r in c.get("/api/datasets?q=catalog%20price", headers=h).json()] == ["demo-catalog"]
