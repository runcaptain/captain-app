import json

import duckdb
from typer.testing import CliRunner

from captain_search.cli.main import app

R = CliRunner()


def cst(*args, ok=True):
    r = R.invoke(app, ["--json", *args])
    if ok:
        assert r.exit_code == 0, r.output
    return r


def _setup(tmp_path, rows=500):
    db = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(db))
    con.execute(f"create table products as select i as id, 'item ' || i as title, (i % 50) * 1.5 as price from range({rows}) t(i)")
    con.close()
    cst("init", "acme", "--storage", str(tmp_path / "bucket"))
    cst("conn", "add", "shop", "--engine", "duckdb", "--url", f"duckdb:///{db}", "--row-limit", "1000")
    return db


def test_sql_save_list_head_verify(tmp_path):
    _setup(tmp_path)
    out = json.loads(cst("sql", "shop", "-e", "select * from products", "--save", "acme-catalog", "--kind", "catalog").output)
    assert out["rows"] == 500 and out["dataset"] == "acme-catalog@v1"
    cst("sql", "shop", "-e", "select * from products where price > 10", "--save", "acme-catalog", "--kind", "catalog")
    lst = json.loads(cst("data", "list").output)
    assert lst[0]["latest"] == 2
    show = json.loads(cst("data", "show", "acme-catalog@v1").output)
    assert show["provenance"]["sql"] == "select * from products"
    assert show["provenance"]["connection"] == "shop"
    head = json.loads(cst("data", "head", "acme-catalog@1", "-n", "3").output)
    assert [r["id"] for r in head] == [0, 1, 2]
    assert json.loads(cst("data", "verify", "acme-catalog@v2").output)["ok"]
    cst("data", "alias", "acme-catalog@v1", "good")
    assert json.loads(cst("data", "show", "acme-catalog@good").output)["version"] == 1
    counts = json.loads(cst("sync").output)
    assert counts["dataset"] == 2


def test_write_sql_refused(tmp_path):
    _setup(tmp_path)
    r = cst("sql", "shop", "-e", "delete from products", ok=False)
    assert r.exit_code == 1 and "not allowed" in r.output


def test_row_limit_refused_then_local_override(tmp_path):
    _setup(tmp_path, rows=2500)
    r = cst("sql", "shop", "-e", "select * from products", ok=False)
    assert "too much for a laptop" in r.output
    out = json.loads(cst("sql", "shop", "-e", "select * from products", "--local").output)
    assert out["rows"] == 2500
    out = json.loads(cst("sql", "shop", "-e", "select * from products", "--limit", "10").output)
    assert out["rows"] == 10 and out["truncated"]


def test_dry_run_writes_nothing(tmp_path):
    _setup(tmp_path)
    cst("--dry-run", "sql", "shop", "-e", "select 1 as a", "--save", "tiny")
    assert json.loads(cst("data", "list").output) == []


def test_inline_password_refused(tmp_path):
    _setup(tmp_path)
    r = cst("conn", "add", "pg", "--engine", "postgres", "--url", "postgresql://u:hunter2@db/x", ok=False)
    assert "inline password" in r.output
    cst("conn", "add", "pg", "--engine", "postgres", "--url", "postgresql://u:{secret:PG_PW}@db/x")


def test_secret_placeholder_is_url_quoted(monkeypatch):
    from captain_search.workspace import render_secrets

    monkeypatch.setenv("PG_PW", "p@ss/w:rd")
    assert render_secrets("postgresql://u:{secret:PG_PW}@db/x") == "postgresql://u:p%40ss%2Fw%3Ard@db/x"
    assert render_secrets("{secret_raw:PG_PW}") == "p@ss/w:rd"


def test_snapshot_file(tmp_path):
    _setup(tmp_path)
    f = tmp_path / "q.csv"
    f.write_text("query,count\nred shoes,10\nusb c cable,4\n")
    out = json.loads(cst("data", "snapshot", str(f), "--name", "acme-queries", "--kind", "queries").output)
    assert out["row_count"] == 2 and out["provenance"]["source"] == "file"
