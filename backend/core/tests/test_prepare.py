"""Warehouse tables in as clean, training-ready data: role, column mapping, cleaning, preview, then import."""
import duckdb
import pytest

from captain_search import library as LIB
from captain_search.data import prepare as PP
from captain_search.model.entities import Connection
from captain_search.workspace import init_workspace, whoami


@pytest.fixture
def ctx(tmp_path):
    db = tmp_path / "lake.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema bakehouse")
    con.execute("""create table bakehouse.sales_suppliers as select * from (values
        (1, '  Golden Grain  ', 'flour', '12.50', 'US'),
        (2, 'Sweet Co', 'sugar', '8', 'US'),
        (2, 'Sweet Co (dup)', 'sugar', '8', 'US'),
        (3, NULL, 'butter', 'n/a', 'FR'),
        (4, '', 'eggs', '3.2', 'US'),
        (5, 'Cocoa Ltd', 'cocoa', '20', 'GH')) t(supplierID, name, ingredient, price, country)""")
    con.execute("""create table bakehouse.labels as select * from (values
        ('q1', 'p1', 3), ('q1', 'p2', 9), ('q2', 'p1', 1), ('q2', 'p1', 1), (NULL, 'p3', 2)) t(qid, item_id, relevance)""")
    con.close()
    c = init_workspace("t", str(tmp_path / "bucket"))
    c.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="lake", engine="duckdb",
                                 url=f"duckdb:///{db}"))
    return c


def _shop(ctx):
    from captain_search import metastore as MS

    MS.pull(ctx, "lake")
    return next(s["name"] for s in MS.schemas(ctx, "lake")["schemas"] if s["name"].endswith("bakehouse"))


def test_catalog_preview_maps_and_cleans(ctx):
    src = {"conn": "lake", "table": f"{_shop(ctx)}.sales_suppliers"}
    p = PP.preview(ctx, src)
    assert p["role"] == "catalog" and p["mapping"]["id"] == "supplierID" and p["mapping"]["title"] == "name"
    assert p["mapping"]["price"] == "price" and not p["missing"]
    r = p["report"]
    assert r["rows_in"] == 6 and r["rows_out"] == 3 and r["dropped"] == 3
    assert "supplierID becomes id" in r["steps"] and any("duplicate id" in s for s in r["steps"])
    cols = [c["name"] for c in p["page"]["columns"]]
    assert cols[:3] == ["id", "title", "price"] and "ingredient" in cols
    first = dict(zip(cols, p["page"]["rows"][0]))
    assert first["title"] == "Golden Grain" and first["price"] == 12.5
    assert p["suggested_name"] == "sales-suppliers" and p["folder"] == "catalogs"


def test_import_whole_table_into_its_folder_with_provenance(ctx):
    src = {"conn": "lake", "table": f"{_shop(ctx)}.sales_suppliers"}
    out = PP.import_prepared(ctx, src)
    assert out["ref"] == "catalogs/sales-suppliers@v1" and out["kind"] == "catalog" and out["rows"] == 3
    m = ctx.registry.load("dataset", "catalogs/sales-suppliers")
    assert m.provenance.params["role"] == "catalog" and m.provenance.params["mapping"]["id"] == "supplierID"
    assert m.provenance.params["cleaning"] and "SELECT" in m.provenance.sql
    doc, _ = LIB._read(ctx)
    assert LIB.folder_of(doc, "catalogs/sales-suppliers") == "catalogs"
    again = PP.import_prepared(ctx, src)
    assert again["version"] == 2 and again["new_version_of_existing"] is True


def test_labels_with_custom_mapping_drop_bad_grades(ctx):
    src = {"conn": "lake", "table": f"{_shop(ctx)}.labels"}
    p = PP.preview(ctx, src, role="judgments", mapping={"query_id": "qid", "product_id": "item_id", "grade": "relevance"})
    assert not p["missing"] and p["report"]["rows_out"] == 2  # grade 9, the duplicate and the missing query dropped
    out = PP.import_prepared(ctx, src, role="judgments",
                             mapping={"query_id": "qid", "product_id": "item_id", "grade": "relevance"}, name="eval-labels")
    assert out["ref"] == "judgments/eval-labels@v1"


def test_missing_required_columns_are_explained(ctx):
    src = {"conn": "lake", "table": f"{_shop(ctx)}.labels"}
    p = PP.preview(ctx, src, role="events")
    assert set(p["missing"]) == {"timestamp", "event"} and "needs timestamp, event" in p["report"]["problems"][0]
    with pytest.raises(ValueError, match="needs timestamp, event"):
        PP.import_prepared(ctx, src, role="events")


def test_sql_and_dataset_sources(ctx):
    p = PP.preview(ctx, {"conn": "lake", "sql": f"select supplierID as id, name as title from {_shop(ctx)}.sales_suppliers"})
    assert p["role"] == "catalog" and p["report"]["rows_out"] == 3
    out = PP.import_prepared(ctx, {"conn": "lake", "table": f"{_shop(ctx)}.sales_suppliers"}, role="generic", name="raw")
    p2 = PP.preview(ctx, {"dataset": out["name"]}, role="catalog", mapping={"id": "supplierID", "title": "name"})
    assert p2["report"]["rows_in"] == 6 and p2["report"]["rows_out"] == 3
