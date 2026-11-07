"""Real networked databases in throwaway Docker containers: connect the way the app's form does (connection kinds +
a secret), query, get refused on writes, read the tables, sample rows.

Skipped when Docker isn't running. Run only these with `pytest -m docker`. Each container gets a random host port and
is removed afterwards, whatever happens.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import uuid

import pytest

from captain_search import metastore as MS
from captain_search.connectors import kinds as K
from captain_search.connectors.guard import WriteRefused
from captain_search.connectors.sql import run_sql
from captain_search.model.entities import Connection
from captain_search.workspace import init_workspace, whoami


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker isn't running")]


def _run(image: str, port: int, env: dict | None = None, args: list[str] | None = None) -> tuple[str, int]:
    name = f"cst-test-{uuid.uuid4().hex[:8]}"
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1::{port}"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    subprocess.run(cmd + [image, *(args or [])], check=True, capture_output=True)
    out = subprocess.run(["docker", "port", name, str(port)], check=True, capture_output=True, text=True).stdout
    return name, int(out.strip().splitlines()[0].rsplit(":", 1)[1])


def _stop(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _wait(fn, seconds: int = 120):
    deadline, last = time.time() + seconds, None
    while time.time() < deadline:
        try:
            return fn()
        except (ImportError, ModuleNotFoundError):
            raise  # a missing driver won't fix itself by waiting
        except Exception as e:  # noqa: BLE001
            if "Can't load plugin" in str(e):
                raise
            last = e
            time.sleep(2)
    raise TimeoutError(f"not ready after {seconds}s: {last}")


def _connect(tmp_path, monkeypatch, kind: str, fields: dict, password: str | None):
    ctx = init_workspace("t", str(tmp_path / "bucket"))
    built = K.build(kind, "wh", {**fields, **({"password_set": True} if password else {})})
    for s in built["secrets"]:
        monkeypatch.setenv(s, password)
    conn = Connection(org="captain", workspace="t", created_by=whoami(), name="wh", engine=built["engine"], url=built["url"])
    ctx.save_connection(conn)
    return ctx, conn


def _exercise(ctx, conn, admin_sql: list[str], schema_hint: str):
    """The checks every networked database must pass."""
    # the admin seeds data through a writable twin of the same connection
    admin = conn.model_copy(update={"writable": True})
    for s in admin_sql:
        run_sql(admin, s)
    r = run_sql(conn, "select count(*) as n from products")
    assert int(r.table.column(0)[0].as_py()) == 3
    with pytest.raises(WriteRefused):
        run_sql(conn, "delete from products")
    pulled = MS.pull(ctx, "wh")
    assert pulled["tables"] >= 1, pulled
    hit = MS.search(ctx, "products")[0]
    assert hit["name"] == "products" and schema_hint in hit["schema"]
    t = MS.table(ctx, "wh", hit["schema"], "products")
    assert [c["name"] for c in t["columns"]][:3] == ["id", "title", "price"]
    s = MS.sample(ctx, "wh", hit["schema"], "products", n=2, order="price", desc=True)
    assert len(s["rows"]) == 2 and float(s["rows"][0][2]) == 30.0


def test_postgres(tmp_path, monkeypatch):
    name, port = _run("postgres:16-alpine", 5432, {"POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "shop"})
    try:
        ctx, conn = _connect(tmp_path, monkeypatch, "postgres",
                             {"host": "127.0.0.1", "port": port, "database": "shop", "user": "postgres", "sslmode": "disable"}, "pw")
        _wait(lambda: run_sql(conn, "select 1"))
        _exercise(ctx, conn, ["create table products (id int, title text, price numeric)",
                              "insert into products values (1,'a',10),(2,'b',20),(3,'c',30)"], "public")
    finally:
        _stop(name)


def test_mysql(tmp_path, monkeypatch):
    name, port = _run("mysql:8.0", 3306, {"MYSQL_ROOT_PASSWORD": "pw", "MYSQL_DATABASE": "shop"})
    try:
        ctx, conn = _connect(tmp_path, monkeypatch, "mysql",
                             {"host": "127.0.0.1", "port": port, "database": "shop", "user": "root"}, "pw")
        _wait(lambda: run_sql(conn, "select 1"), 180)
        _exercise(ctx, conn, ["create table products (id int, title text, price decimal(10,2))",
                              "insert into products values (1,'a',10),(2,'b',20),(3,'c',30)"], "shop")
    finally:
        _stop(name)


def test_clickhouse(tmp_path, monkeypatch):
    name, port = _run("clickhouse/clickhouse-server:24.8", 8123,
                      {"CLICKHOUSE_USER": "u", "CLICKHOUSE_PASSWORD": "pw", "CLICKHOUSE_DB": "shop"})
    try:
        ctx, conn = _connect(tmp_path, monkeypatch, "clickhouse",
                             {"host": "127.0.0.1", "port": port, "database": "shop", "user": "u", "https": "no"}, "pw")
        _wait(lambda: run_sql(conn, "select 1"))
        _exercise(ctx, conn, ["create table products (id Int32, title String, price Float64) engine = MergeTree order by id",
                              "insert into products values (1,'a',10),(2,'b',20),(3,'c',30)"], "shop")
    finally:
        _stop(name)


def test_trino(tmp_path, monkeypatch):
    name, port = _run("trinodb/trino:470", 8080)
    try:
        ctx, conn = _connect(tmp_path, monkeypatch, "trino",
                             {"host": "127.0.0.1", "port": port, "user": "captain", "catalog": "memory", "schema": "default"}, None)
        _wait(lambda: run_sql(conn, "select 1"), 240)
        _exercise(ctx, conn, ["create table products (id integer, title varchar, price double)",
                              "insert into products values (1,'a',10),(2,'b',20),(3,'c',30)"], "default")
    finally:
        _stop(name)
