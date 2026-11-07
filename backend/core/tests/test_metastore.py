"""The table browser: pull a database's metadata, browse, search, sample, and learn usage from logged queries."""
import duckdb
import pytest
from fastapi.testclient import TestClient

from captain_search import metastore as MS
from captain_search.model.entities import Connection
from captain_search.workspace import init_workspace, whoami


@pytest.fixture
def ctx(tmp_path):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema shop")
    con.execute("create table shop.products as select i as id, 'item ' || i as title, i * 2.5 as price from range(30) t(i)")
    con.execute("create table shop.searches as select i as id, 'q' || i as query, i % 3 as hits from range(10) t(i)")
    con.execute("create view shop.cheap as select * from shop.products where price < 10")
    con.execute("comment on table shop.products is 'Every product we sell'")
    con.close()
    c = init_workspace("t", str(tmp_path / "bucket"))
    c.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="wh", engine="duckdb",
                                 url=f"duckdb:///{db}"))
    return c


def _shop(ctx):
    return next(s["name"] for s in MS.schemas(ctx, "wh")["schemas"] if s["name"].endswith("shop"))


def test_pull_browse_search_sample(ctx):
    r = MS.pull(ctx, "wh")
    assert r["tables"] >= 3 and not r["errors"]
    sch = {s["name"]: s for s in MS.schemas(ctx, "wh")["schemas"]}
    shop = next(k for k in sch if k.endswith("shop"))
    assert not any(k.startswith(("system.", "temp.")) or k.endswith("information_schema") for k in sch)
    names = {t["name"]: t for t in sch[shop]["tables"]}
    assert names["cheap"]["type"] == "view" and names["products"]["columns"] == 3
    t = MS.table(ctx, "wh", _shop(ctx), "products")
    assert [c["name"] for c in t["columns"]] == ["id", "title", "price"] and t["pulled_at"]
    assert MS.search(ctx, "price")[0]["name"] in ("products", "cheap")
    assert MS.search(ctx, "searches")[0]["name"] == "searches"
    s = MS.sample(ctx, "wh", _shop(ctx), "products", n=5, where="price > 20", order="price", desc=True)
    assert len(s["rows"]) == 5 and float(s["rows"][0][2]) == 72.5
    assert MS.overview(ctx)[0]["tables"] == r["tables"]


def test_sample_stays_read_only(ctx):
    MS.pull(ctx, "wh")
    with pytest.raises(Exception):
        MS.sample(ctx, "wh", _shop(ctx), "products", where="1=1; drop table shop.products")


def test_insights_from_logged_queries(ctx, monkeypatch):
    assert MS.tables_in("with x as (select * from shop.products) select * from x join shop.searches s on 1=1") == [
        "shop.products", "shop.searches"]
    MS.record_query(ctx, "wh", "select * from shop.products p join shop.searches s on p.id = s.id", rows=10)
    monkeypatch.setenv("CST_USER", "ana@example.com")
    MS.record_query(ctx, "wh", "select count(*) from shop.products", rows=1)
    MS.record_query(ctx, "wh", "select price from shop.products where id = 3", rows=1)
    i = MS.table(ctx, "wh", _shop(ctx), "products")["insights"] if MS.pull(ctx, "wh") else None
    assert i["queries"] == 3
    assert i["frequent_users"][0] == {"user": "ana@example.com", "queries": 2}
    assert i["queried_with"] == [{"table": "shop.searches", "queries": 1}]
    assert i["examples"][0]["sql"].startswith("select")


def test_routes(ctx, tmp_path, monkeypatch):
    from captain_search import server

    monkeypatch.setattr(server, "_ctx", None)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert c.get("/api/tables/wh", headers=h).status_code == 404
    MS.pull(ctx, "wh")
    assert c.get("/api/tables", headers=h).json()[0]["connection"] == "wh"
    assert c.get("/api/tables/wh/" + _shop(ctx) + "/searches", headers=h).json()["columns"][1]["name"] == "query"
    assert len(c.get("/api/tables/wh/" + _shop(ctx) + "/searches/sample?n=3", headers=h).json()["rows"]) == 3
    r = c.post("/api/sql", headers=h, json={"conn": "wh", "sql": "select * from shop.searches"})
    assert r.status_code == 200
    assert c.get("/api/tables/wh/" + _shop(ctx) + "/searches", headers=h).json()["insights"]["queries"] == 1


def test_a_catalog_with_a_dash_in_its_name(tmp_path):
    """DuckDB names the schema of a file called t-store.duckdb '"t-store".main'. Columns, search and samples must
    still work (they read nothing when the quotes were kept)."""
    db = tmp_path / "t-store.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table products as select i as id, 'item ' || i as title, i * 2.5 as price from range(5) t(i)")
    con.close()
    c = init_workspace("t", str(tmp_path / "bucket"))
    c.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="store", engine="duckdb",
                                 url=f"duckdb:///{db}"))
    assert MS._parts('"t-store".main') == ["t-store", "main"]
    MS.pull(c, "store")
    sch = next(s for s in MS.schemas(c, "store")["schemas"] if s["name"].endswith("main"))
    t = next(t for t in sch["tables"] if t["name"] == "products")
    assert [x["name"] for x in MS.table(c, "store", sch["name"], "products")["columns"]] == ["id", "title", "price"], t
    hits = MS.search(c, "price", "store")
    assert any("products" in (r.get("name"), r.get("table")) for r in hits), hits
    s = MS.sample(c, "store", sch["name"], "products", n=2, order="price", desc=True)
    assert len(s["rows"]) == 2 and float(s["rows"][0][2]) == 10.0


def test_a_broken_sample_is_a_sentence(ctx):
    MS.pull(ctx, "wh")
    with pytest.raises(ValueError) as e:
        MS.sample(ctx, "wh", _shop(ctx), "products", where="no_such_column > 1")
    assert str(e.value).startswith("Couldn't read sample rows of products:")
