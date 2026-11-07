"""The query editor's tools: format, transpile, explain (through the read-only guard), snippets and query history."""
import duckdb
import pytest
from fastapi.testclient import TestClient

from captain_search import sqltools as ST
from captain_search.connectors.guard import WriteRefused, check_read_only
from captain_search.metastore import record_query
from captain_search.model.entities import Connection
from captain_search.workspace import init_workspace, whoami


@pytest.fixture
def ctx(tmp_path):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table products as select i as id, 'item ' || i as title, i * 2.5 as price from range(30) t(i)")
    con.close()
    c = init_workspace("t", str(tmp_path / "bucket"))
    c.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="wh", engine="duckdb",
                                 url=f"duckdb:///{db}"))
    return c


def test_format_upper_and_lower():
    up = ST.format_sql("select id, 'Select me' as t from Products where price > 3")
    assert up.startswith("SELECT") and "FROM Products" in up and "'Select me'" in up
    low = ST.format_sql("SELECT id FROM Products WHERE price > 3", "lower")
    assert low.startswith("select") and "from Products" in low and "where" in low
    with pytest.raises(ValueError, match="doesn't parse"):
        ST.format_sql("select from where (")


def test_transpile():
    out = ST.transpile_sql("select ifnull(a, 0) from t limit 5", "duckdb", "tsql")
    assert "TOP 5" in out["sql"] and out["to"] == "tsql"
    with pytest.raises(ValueError, match="name the engine"):
        ST.transpile_sql("select 1", None, "")


def test_explain_of_a_read_is_a_read(ctx):
    check_read_only("EXPLAIN select * from products", "duckdb")
    with pytest.raises(WriteRefused):
        check_read_only("EXPLAIN delete from products", "duckdb")
    plan = ST.explain(ctx, "wh", "select * from products where price > 10;")
    assert plan["plan"] and plan["connection"] == "wh"


def test_snippets(ctx):
    s = ST.save_snippet(ctx, {"title": "Top products", "sql": "select * from {{table}} limit {{n}} -- {{n}}", "golden": True})
    assert s["vars"] == ["table", "n"]
    ST.save_snippet(ctx, {"title": "Another", "sql": "select 1", "engine": "snowflake"})
    assert [x["title"] for x in ST.snippets(ctx)] == ["Top products", "Another"]  # golden first
    assert [x["title"] for x in ST.snippets(ctx, engine="duckdb")] == ["Top products"]
    assert [x["title"] for x in ST.snippets(ctx, q="top")] == ["Top products"]
    assert ST.fill(s["sql"], {"table": "products", "n": 5}) == "select * from products limit 5 -- 5"
    assert ST.fill(s["sql"], {"table": "products"}).endswith("{{n}} -- {{n}}")
    changed = ST.save_snippet(ctx, {"sql": "select {{col}} from products"}, s["id"])
    assert changed["vars"] == ["col"] and changed["title"] == "Top products"
    ST.delete_snippet(ctx, s["id"])
    assert len(ST.snippets(ctx)) == 1
    with pytest.raises(ValueError):
        ST.save_snippet(ctx, {"title": "", "sql": "select 1"})


def test_history(ctx):
    record_query(ctx, "wh", "select * from products", rows=30, ms=4, source="app")
    record_query(ctx, "wh", "select price from products", rows=30, ms=2, source="notebook", ok=False)
    assert len(ST.history(ctx)) == 2
    assert [r["source"] for r in ST.history(ctx, status="failed")] == ["notebook"]
    assert [r["sql"] for r in ST.history(ctx, q="PRICE")] == ["select price from products"]
    assert ST.history(ctx, conn="other") == []


def test_routes(ctx, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", None)
    c = TestClient(server.app, raise_server_exceptions=False)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert c.post("/api/sql/format", headers=h, json={"sql": "select 1 as a", "conn": "wh"}).json()["sql"] == "SELECT\n  1 AS a"
    assert "TOP" in c.post("/api/sql/transpile", headers=h, json={"sql": "select a from t limit 1", "to": "tsql"}).json()["sql"]
    assert c.post("/api/sql/explain", headers=h, json={"conn": "wh", "sql": "select * from products"}).json()["plan"]
    r = c.post("/api/sql/explain", headers=h, json={"conn": "wh", "sql": "drop table products"})
    assert r.status_code >= 400
    s = c.post("/api/snippets", headers=h, json={"title": "All", "sql": "select * from {{t}}"}).json()
    assert c.get("/api/snippets", headers=h).json()[0]["vars"] == ["t"]
    assert c.put(f"/api/snippets/{s['id']}", headers=h, json={"title": "Everything"}).json()["title"] == "Everything"
    assert c.delete(f"/api/snippets/{s['id']}", headers=h).json() == {"deleted": s["id"]}
    assert c.delete(f"/api/snippets/{s['id']}", headers=h).status_code == 404
    c.post("/api/sql", headers=h, json={"conn": "wh", "sql": "select count(*) from products"})
    hist = c.get("/api/sql/history?conn=wh", headers=h).json()
    assert hist[0]["sql"] == "select count(*) from products" and hist[0]["source"] == "app"
    c.post("/api/sql", headers=h, json={"conn": "wh", "sql": "select nope from products"})
    failed = c.get("/api/sql/history?status=failed", headers=h).json()
    assert [r["sql"] for r in failed] == ["select nope from products"]
