"""Unity-Catalog-style lineage: warehouse tables to datasets to pipelines and runs, up and down, plus columns."""
import duckdb
import pytest

from captain_search import lineage as LN
from captain_search import metastore as MS
from captain_search.connectors.sql import run_sql
from captain_search.data import datasets as DS
from captain_search.model.entities import Connection, DatasetKind, Provenance
from captain_search.workspace import init_workspace, whoami


@pytest.fixture
def ctx(tmp_path):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema shop")
    con.execute("create table shop.products as select i as id, 'item ' || i as title, i * 2.5 as price from range(30) t(i)")
    con.execute("create table shop.searches as select i as id, 'q' || i as query, i % 3 as hits from range(10) t(i)")
    con.close()
    c = init_workspace("t", str(tmp_path / "bucket"))
    c.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="wh", engine="duckdb",
                                 url=f"duckdb:///{db}"))
    return c


def _save(c, name, sql):
    r = run_sql(c.load_connection("wh"), sql)
    return DS.snapshot(c, name, r.table, kind=DatasetKind.catalog,
                       provenance=Provenance(source="sql", connection="wh", sql=sql))


def test_warehouse_tables_flow_into_datasets_and_back(ctx):
    _save(ctx, "cheap", "select p.id, p.title as name, p.price * 2 as double_price from shop.products p where price < 10")
    _save(ctx, "joined", "select p.id, s.query from shop.products p join shop.searches s on p.id = s.id")
    g = LN.graph(ctx)
    ids = {n["id"] for n in g["nodes"]}
    assert {"table:wh/shop.products", "table:wh/shop.searches", "dataset:cheap@v1", "dataset:joined@v1"} <= ids
    down = LN.neighborhood(ctx, "table:wh/shop.products", up=0, down=1)
    assert {n["id"] for n in down["nodes"]} == {"table:wh/shop.products", "dataset:cheap@v1", "dataset:joined@v1"}
    up = LN.neighborhood(ctx, "dataset:joined", up=1, down=0)  # no version: the newest
    assert up["focus"] == "dataset:joined@v1"
    assert {n["id"]: n["level"] for n in up["nodes"]} == {
        "dataset:joined@v1": 0, "table:wh/shop.products": -1, "table:wh/shop.searches": -1, "connection:wh": -1}


def test_column_lineage_traces_through_sql(ctx):
    MS.pull(ctx, "wh")
    _save(ctx, "cheap", "select p.id, p.title as name, p.price * 2 as double_price from shop.products p")
    got = LN.columns(ctx, "cheap", "double_price")
    assert got["known"] and got["sources"][0]["table"] == "shop.products" and got["sources"][0]["column"] == "price"
    assert LN.columns(ctx, "cheap", "name")["sources"][0]["column"] == "title"


def test_import_databricks_lineage(ctx, monkeypatch):
    import pyarrow as pa

    from captain_search.connectors import sql as SQL

    ctx.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="lake", engine="databricks",
                                   url="databricks://token:x@h?http_path=/p"))

    def fake(conn, sql, **kw):
        if "column_lineage" in sql:
            rows = [{"source": "main.raw.events", "source_column": "q", "target": "main.gold.searches", "target_column": "query"}]
        else:
            rows = [{"source": "main.raw.events", "target": "main.gold.searches", "via": "JOB", "last_seen": "2026-11-05"}]
        return SQL.Result(table=pa.Table.from_pylist(rows), truncated=False, elapsed_ms=5)

    monkeypatch.setattr(SQL, "run_sql", fake)
    r = LN.import_external(ctx, "lake", days=7)
    assert r["table_edges"] == 1 and r["column_edges"] == 1 and r["system"] == "Databricks Unity Catalog"
    nb = LN.neighborhood(ctx, "table:lake/main.gold.searches", up=1, down=0)
    assert any(n["id"] == "table:lake/main.raw.events" for n in nb["nodes"])
    with pytest.raises(ValueError, match="doesn't keep lineage"):
        LN.import_external(ctx, "wh")


def test_quality_profile(ctx):
    _save(ctx, "cheap", "select id, case when id % 2 = 0 then null else title end as title, price from shop.products")
    p = LN.profile_dataset(ctx, "cheap")
    cols = {c["name"]: c for c in p["columns"]}
    assert p["rows"] == 30 and cols["title"]["nulls"] == 0.5 and cols["id"]["distinct"] == 30
    assert cols["price"]["min"] == "0.0" or float(cols["price"]["min"]) == 0.0
