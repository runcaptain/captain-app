"""The dataset read log: cheap appends where rows are read, once per reader per window, and Frequent readers."""
import pytest
from fastapi.testclient import TestClient

from captain_search import demo
from captain_search.data import insights as IN
from captain_search.data import reads as RD
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    monkeypatch.delenv("CST_AGENT", raising=False)
    RD._last.clear()
    c = init_workspace("r", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def test_once_per_reader_per_window(ctx, monkeypatch):
    assert RD.record(ctx, "demo-catalog", 1, "app", user="ana") is True
    assert RD.record(ctx, "demo-catalog", 1, "app", user="ana") is False, "paging the grid isn't a new read"
    assert RD.record(ctx, "demo-catalog", 1, "notebook", user="ana") is True, "another way of reading is"
    assert RD.record(ctx, "demo-catalog", 1, "app", user="bo") is True
    t = [100000.0]
    monkeypatch.setattr(RD.time, "time", lambda: t[0])
    RD._last.clear()
    assert RD.record(ctx, "demo-catalog", 1, "app", user="ana")
    t[0] += RD.WINDOW_S + 1
    assert RD.record(ctx, "demo-catalog", 1, "app", user="ana"), "after the window, a new read"
    r = RD.readers(ctx, "demo-catalog")
    assert r["reads"] == 5 and r["readers"][0]["user"] == "ana" and r["readers"][0]["reads"] == 4
    assert r["readers"][0]["via"] == {"app": 3, "notebook": 1} and r["by_via"]["app"] == 4


def test_names_are_kept_apart(ctx):
    RD.record(ctx, "catalogs/products", 1, "app", user="ana")
    RD.record(ctx, "catalogs/products-v2", 1, "app", user="ana")
    assert RD.readers(ctx, "catalogs/products")["reads"] == 1


def test_an_agent_at_the_cli_counts_as_an_agent(ctx, monkeypatch):
    monkeypatch.setenv("CST_AGENT", "claude")
    RD.record(ctx, "demo-catalog", 1, "cli", user="ana")
    e = RD.readers(ctx, "demo-catalog")["readers"][0]
    assert e["agent"] == "claude" and e["via"] == {"agent": 1}


def test_a_broken_store_never_breaks_a_read(ctx, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ctx.store, "put_new", boom)
    assert RD.record(ctx, "demo-catalog", 1, "app", user="ana") is False


def test_routes_and_insights_count_app_reads(ctx, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", ctx)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    for off in (0, 100, 200):
        assert c.get(f"/api/datasets/demo-catalog/rows?offset={off}", headers=h).status_code == 200
    assert c.get("/api/datasets/demo-queries/head", headers=h).status_code == 200
    ins = IN.dataset_insights(ctx, "demo-catalog")
    assert ins["readers"]["reads"] == 1, "three pages, one read"
    assert ins["readers"]["readers"][0]["via"] == {"app": 1} and ins["counts"]["reads"] == 1
    assert RD.readers(ctx, "demo-queries")["reads"] == 1


def test_notebook_dataset_helper_reads_and_logs(ctx, monkeypatch):
    from captain_search.notebook import helpers as H

    monkeypatch.setattr(H, "_ctx", ctx)
    df = H.dataset("demo-catalog")
    assert len(df) == 480
    assert len(H.dataset("demo-queries@v1")) > 0
    assert RD.readers(ctx, "demo-catalog")["readers"][0]["via"] == {"notebook": 1}
    assert RD.readers(ctx, "demo-queries")["readers"][0]["versions"] == [1]
