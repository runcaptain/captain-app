"""Typed connection kinds: forms build URLs that never contain a secret value."""
import pytest
from fastapi.testclient import TestClient

from captain_search.connectors import kinds as K
from captain_search.connectors.guard import check_read_only


def test_snowflake_and_databricks_urls_reference_secrets_by_name():
    s = K.build("snowflake", "warehouse", {"account": "xy12345.us-west-2", "user": "captain", "warehouse": "WH",
                                           "database": "SEARCH", "schema": "PUBLIC", "role": "READER"})
    assert s["url"] == "snowflake://captain:{secret:WAREHOUSE_PASSWORD}@xy12345.us-west-2/SEARCH/PUBLIC?warehouse=WH&role=READER"
    assert s["secrets"] == ["WAREHOUSE_PASSWORD"] and s["dialect"] == "snowflake"
    d = K.build("databricks", "lake", {"server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/ab"})
    assert d["url"].startswith("databricks://token:{secret:LAKE_TOKEN}@dbc-1.cloud.databricks.com?http_path=%2Fsql")
    assert "catalog=main" in d["url"] and d["secrets"] == ["LAKE_TOKEN"]


def test_validation_is_plain():
    with pytest.raises(ValueError, match="Account is needed"):
        K.build("snowflake", "x", {"user": "u"})
    with pytest.raises(ValueError, match="Port must be a number"):
        K.build("postgres", "x", {"host": "h", "port": "abc", "database": "d", "user": "u"})
    with pytest.raises(ValueError, match="unknown database kind"):
        K.build("oracle9", "x", {})


def test_every_kind_lists_fields_and_driver_state():
    ks = {k["kind"]: k for k in K.listing()}
    assert {"snowflake", "databricks", "bigquery", "redshift", "postgres", "trino", "athena", "duckdb"} <= set(ks)
    assert ks["sqlite"]["installed"] is True
    assert all(f["label"] for k in ks.values() for f in k["fields"])


def test_guard_knows_the_new_dialects():
    check_read_only("select 1", "databricks")
    check_read_only("select 1", "clickhouse")
    with pytest.raises(Exception):
        check_read_only("drop table t", "databricks")


def test_add_route_refuses_secrets_and_tests_sqlite(tmp_path, monkeypatch):
    from captain_search import server
    from captain_search.workspace import init_workspace

    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    monkeypatch.setattr(server, "_ctx", None)
    init_workspace("t", str(tmp_path / "b"))
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    r = c.post("/api/connections", headers=h, json={"name": "wh", "kind": "snowflake",
                                                     "fields": {"account": "a", "user": "u", "password": "nope"}})
    assert r.status_code == 400 and "store it with POST /api/secrets" in r.json()["error"]
    db = tmp_path / "x.sqlite"
    import sqlite3

    sqlite3.connect(db).close()
    r = c.post("/api/connections", headers=h, json={"name": "local", "kind": "sqlite", "fields": {"path": str(db)}})
    assert r.status_code == 200 and r.json()["secrets"] == []
    assert c.post("/api/connections/local/test", headers=h).json()["ok"] is True
    assert c.post("/api/connections", headers=h, json={"name": "local", "kind": "sqlite", "fields": {"path": str(db)}}).status_code == 400
    assert "snowflake" in {k["kind"] for k in c.get("/api/connections/kinds", headers=h).json()}
