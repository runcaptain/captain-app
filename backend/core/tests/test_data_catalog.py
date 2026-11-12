"""Data's one list of tables: datasets Captain keeps versions of and the tables of each connected database, together."""
import duckdb
import pytest
from fastapi.testclient import TestClient

from captain_search import demo
from captain_search import library as LIB
from captain_search import metastore as MS
from captain_search.data import catalog as CAT
from captain_search.data import reads as RD
from captain_search.model.entities import Connection
from captain_search.workspace import init_workspace, whoami


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    monkeypatch.delenv("CST_AGENT", raising=False)
    RD._last.clear()
    c = init_workspace("shop", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema shop")
    con.execute("create table shop.orders as select i as id, i * 2.5 as total, 'c' || i as customer_ref from range(30) t(i)")
    con.close()
    c.save_connection(Connection(org="captain", workspace="shop", created_by=whoami(), name="wh", engine="duckdb",
                                 url=f"duckdb:///{db}"))
    return c


def test_datasets_and_database_tables_are_one_list(ctx):
    MS.pull(ctx, "wh")
    got = CAT.tables(ctx)
    rows = {(r["kind"], r["name"]): r for r in got["rows"]}
    ds = rows[("dataset", "demo-catalog")]
    # A dataset is a table Captain keeps versions of: it says so with a count, and where it lives in words.
    assert ds["versions"] >= 1 and ds["version"] == ds["versions"] and ds["where"].startswith("Captain")
    assert ds["rows"] and ds["columns"] > 0 and "column_names" not in ds
    tb = rows[("table", "orders")]
    # A database table is read where it lives: Captain keeps no versions of it.
    assert tb["versions"] is None and tb["connection"] == "wh" and tb["where"].startswith("wh, ") and "shop" in tb["where"]
    assert tb["columns"] == 3 and tb["last_read"], "when Captain last read the database's tables"
    assert [r["name"].lower() for r in got["rows"]] == sorted(r["name"].lower() for r in got["rows"])
    assert {"connection": "wh", "engine": "duckdb"}.items() <= next(k for k in got["connections"] if k["connection"] == "wh").items()


def test_a_database_not_read_yet_lists_no_tables_but_is_named(ctx):
    got = CAT.tables(ctx)
    assert not any(r["kind"] == "table" and r.get("connection") == "wh" for r in got["rows"])
    wh = next(k for k in got["connections"] if k["connection"] == "wh")
    assert wh["pulled_at"] is None and wh["tables"] is None


def test_last_read_comes_from_the_read_log_without_opening_it(ctx, monkeypatch):
    assert next(r for r in CAT.tables(ctx)["rows"] if r["name"] == "demo-catalog")["last_read"] is None
    assert RD.record(ctx, "demo-catalog", 1, "app", user="ana")
    opened = []
    real = ctx.store.get
    monkeypatch.setattr(ctx.store, "get", lambda k: (opened.append(k), real(k))[1])
    day = CAT.last_read_days(ctx)["demo-catalog"]
    assert len(day) == 10 and day[4] == "-"
    assert not any(k.startswith(CAT.READS) for k in opened), "no read record is opened for the list"
    assert next(r for r in CAT.tables(ctx)["rows"] if r["name"] == "demo-catalog")["last_read"] == day


def test_search_looks_at_names_places_and_columns(ctx):
    MS.pull(ctx, "wh")
    by_col = CAT.tables(ctx, q="customer_ref")["rows"]
    assert [r["name"] for r in by_col] == ["orders"] and by_col[0]["matched_column"] == "customer_ref"
    by_place = CAT.tables(ctx, q="wh shop")["rows"]
    assert by_place and all(r["kind"] == "table" for r in by_place) and "matched_column" not in by_place[0]
    assert CAT.tables(ctx, q="no such thing anywhere")["rows"] == []
    assert all(r["kind"] == "dataset" for r in CAT.tables(ctx, kind="dataset")["rows"])
    assert all(r["kind"] == "table" for r in CAT.tables(ctx, kind="table")["rows"])


def test_a_folder_and_a_saved_query_show_in_where_it_lives(ctx):
    LIB.make_folder(ctx, "gifts")
    LIB.move(ctx, ["demo-catalog"], "gifts")
    r = next(r for r in CAT.tables(ctx)["rows"] if r["name"] == "demo-catalog")
    assert r["folder"] == "gifts" and r["where"].startswith("Captain, folder gifts")
    assert CAT._dataset_where("", {"source": "sql", "connection": "wh"}) == "Captain, a saved query on wh"
    assert CAT._dataset_where("a/b", {}) == "Captain, folder a/b"


def test_the_route_follows_the_explorers_pick(ctx, monkeypatch):
    from captain_search import server

    MS.pull(ctx, "wh")
    monkeypatch.setattr(server, "_ctx", None)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    got = c.get("/api/data/tables", headers=h).json()
    kinds = {r["kind"] for r in got["rows"]}
    assert kinds == {"dataset", "table"}
    assert [r["name"] for r in c.get("/api/data/tables?q=customer_ref", headers=h).json()["rows"]] == ["orders"]
    assert {r["kind"] for r in c.get("/api/data/tables?kind=dataset", headers=h).json()["rows"]} == {"dataset"}
    # With a project picked, only its datasets show: a database's tables belong to no project.
    from captain_search import projects as PR

    p = PR.create(ctx, "Outdoor")
    PR.assign(ctx, p["id"], [{"kind": "dataset", "name": "demo-catalog"}])
    scoped = c.get("/api/data/tables", headers={**h, "X-CST-Scope": f"project:{p['id']}"}).json()
    assert [r["name"] for r in scoped["rows"]] == ["demo-catalog"]
    assert any(k["connection"] == "wh" for k in scoped["connections"]), "the databases are still named"
