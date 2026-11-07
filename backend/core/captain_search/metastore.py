"""The table browser: what's in each connected database, and how people use it (Querybook's table explorer).

- **Pull:** read schemas, tables, columns, types and comments from a connection through SQLAlchemy's inspector, which
  covers every connection kind (Snowflake, Databricks, BigQuery, Redshift, Postgres, MySQL, Trino, Athena,
  ClickHouse, SQL Server, DuckDB, SQLite). The result is cached in the workspace as `metastore/<connection>.json` with
  the time it was pulled, so browsing and search are instant and the app can say "last pulled 3 hours ago".
- **Samples:** a few rows of a table, through the same read-only path as every other query.
- **Insights:** every query run through Captain is logged (who, which connection, the SQL, rows, time). The tables
  each query touches are found by parsing it, which gives each table its frequent users, the tables queried with
  it, and example queries.

Nothing here writes to a database.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections import Counter

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

SKIP_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "sys", "mysql", "performance_schema", "system"}


def _system_schema(s: str) -> bool:
    """information_schema and friends, also as DuckDB's catalog.schema (system.main, temp.main)."""
    low = s.lower()
    last = low.rsplit(".", 1)[-1]
    return (last in SKIP_SCHEMAS or low.startswith(("pg_temp", "pg_toast")) or low.split(".", 1)[0] in ("system", "temp")
            and "." in low)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _key(conn: str) -> str:
    return f"metastore/{conn}.json"


def _split(schema: str) -> tuple[str | None, str]:
    """DuckDB and some others name schemas catalog.schema."""
    return tuple(schema.split(".", 1)) if "." in schema else (None, schema)  # type: ignore[return-value]


def _columns_from_information_schema(eng, schema: str, table: str) -> list[dict]:
    from sqlalchemy import text

    cat, sch = _split(schema)
    q = ("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
         "WHERE table_schema = :s AND table_name = :t" + (" AND table_catalog = :c" if cat else "")
         + " ORDER BY ordinal_position")
    with eng.connect() as c:
        rows = c.execute(text(q), {"s": sch, "t": table, **({"c": cat} if cat else {})}).fetchall()
    if not rows:
        raise LookupError("no columns in information_schema")
    return [{"name": r[0], "type": str(r[1]), "nullable": str(r[2]).upper() in ("YES", "TRUE"), "comment": None}
            for r in rows]


def _comment_from_information_schema(eng, schema: str, table: str) -> str | None:
    from sqlalchemy import text

    cat, sch = _split(schema)
    for q in ("SELECT comment FROM duckdb_tables() WHERE schema_name = :s AND table_name = :t" + (" AND database_name = :c" if cat else ""),
              "SELECT comment FROM information_schema.tables WHERE table_schema = :s AND table_name = :t"):
        try:
            with eng.connect() as c:
                r = c.execute(text(q), {"s": sch, "t": table, **({"c": cat} if cat else {})}).fetchone()
            if r and r[0]:
                return str(r[0])
        except Exception:  # noqa: BLE001
            continue
    return None


# ---- pull and read the cache --------------------------------------------------------------


def pull(ctx: Context, conn_name: str, schemas: list[str] | None = None, max_tables: int = 5000,
         progress=lambda f: None) -> dict:
    """Read the connection's metadata and cache it. Returns a summary."""
    from sqlalchemy import inspect

    from .connectors.sql import open_engine

    conn = ctx.load_connection(conn_name)
    t0 = time.time()
    out_schemas, n_tables, errors = [], 0, []
    with open_engine(conn) as eng:
        insp = inspect(eng)
        names = schemas or [s for s in insp.get_schema_names() if not _system_schema(s)]
        for i, sch in enumerate(names):
            progress(i / max(1, len(names)))
            entry = {"name": sch, "tables": []}
            try:
                tables = [(t, "table") for t in insp.get_table_names(schema=sch)]
                try:
                    tables += [(v, "view") for v in insp.get_view_names(schema=sch)]
                except NotImplementedError:
                    pass
            except Exception as e:  # noqa: BLE001  one unreadable schema shouldn't stop the rest
                errors.append({"schema": sch, "error": str(e).splitlines()[0][:200]})
                continue
            for tname, ttype in sorted(tables):
                if n_tables >= max_tables:
                    break
                n_tables += 1
                t = {"name": tname, "type": ttype, "columns": [], "comment": None}
                try:
                    for c in insp.get_columns(tname, schema=sch):
                        t["columns"].append({"name": c["name"], "type": str(c.get("type")), "nullable": c.get("nullable"),
                                             "comment": c.get("comment")})
                except Exception as e:  # noqa: BLE001  some drivers can't reflect: ask information_schema
                    try:
                        t["columns"] = _columns_from_information_schema(eng, sch, tname)
                    except Exception:  # noqa: BLE001
                        t["error"] = str(e).splitlines()[0][:200]
                try:
                    t["comment"] = (insp.get_table_comment(tname, schema=sch) or {}).get("text")
                except Exception:  # noqa: BLE001  some drivers can't: ask information_schema
                    t["comment"] = _comment_from_information_schema(eng, sch, tname)
                entry["tables"].append(t)
            out_schemas.append(entry)
    doc = {"connection": conn_name, "engine": conn.engine, "pulled_at": _now(), "pulled_by": whoami(),
           "seconds": round(time.time() - t0, 2), "schemas": out_schemas, "errors": errors,
           "truncated": n_tables >= max_tables}
    body = json.dumps(doc).encode()
    for _ in range(5):
        try:
            _, tag = ctx.store.get_with_tag(_key(conn_name))
        except NotFound:
            tag = None
        try:
            ctx.store.put_if(_key(conn_name), body, tag)
            break
        except Conflict:
            continue
    progress(1.0)
    return {"connection": conn_name, "schemas": len(out_schemas), "tables": n_tables, "pulled_at": doc["pulled_at"],
            "seconds": doc["seconds"], "errors": errors, "truncated": doc["truncated"]}


def cached(ctx: Context, conn_name: str) -> dict | None:
    try:
        return json.loads(ctx.store.get(_key(conn_name)))
    except NotFound:
        return None


def overview(ctx: Context) -> list[dict]:
    """Every connection with what's cached about it."""
    out = []
    for name in ctx.connections():
        try:
            c = ctx.load_connection(name)
        except NotFound:
            continue
        doc = cached(ctx, name)
        out.append({"connection": name, "engine": c.engine, "pulled_at": doc and doc["pulled_at"],
                    "schemas": len(doc["schemas"]) if doc else None,
                    "tables": sum(len(s["tables"]) for s in doc["schemas"]) if doc else None})
    return out


def schemas(ctx: Context, conn_name: str) -> dict:
    doc = cached(ctx, conn_name)
    if doc is None:
        raise NotFound(f"{conn_name}'s tables haven't been read yet. Pull them first.")
    return {"connection": conn_name, "pulled_at": doc["pulled_at"], "errors": doc.get("errors", []),
            "schemas": [{"name": s["name"], "tables": [{"name": t["name"], "type": t["type"], "columns": len(t["columns"]),
                                                       "comment": t.get("comment")} for t in s["tables"]]}
                        for s in doc["schemas"]]}


def table(ctx: Context, conn_name: str, schema: str, name: str) -> dict:
    doc = cached(ctx, conn_name)
    if doc is None:
        raise NotFound(f"{conn_name}'s tables haven't been read yet. Pull them first.")
    for s in doc["schemas"]:
        if s["name"] == schema:
            for t in s["tables"]:
                if t["name"] == name:
                    return {"connection": conn_name, "schema": schema, **t, "pulled_at": doc["pulled_at"],
                            "insights": insights(ctx, conn_name, schema, name)}
    raise NotFound(f"no table {schema}.{name} in {conn_name} as of {doc['pulled_at']}. Pull again if it's new.")


def search(ctx: Context, q: str, conn_name: str | None = None, limit: int = 50) -> list[dict]:
    """Tables whose name, comment or column names match every word of q. Name matches rank first."""
    words = [w for w in q.lower().split() if w]
    hits = []
    for name in ([conn_name] if conn_name else ctx.connections()):
        doc = cached(ctx, name)
        if not doc:
            continue
        for s in doc["schemas"]:
            for t in s["tables"]:
                cols = " ".join(c["name"] for c in t["columns"]).lower()
                hay = f"{s['name']}.{t['name']} {t.get('comment') or ''} {cols}".lower()
                if words and all(w in hay for w in words):
                    score = sum(3 if w in t["name"].lower() else 1 if w in (t.get("comment") or "").lower() else 0
                                for w in words)
                    hits.append({"connection": name, "schema": s["name"], "name": t["name"], "type": t["type"],
                                 "columns": len(t["columns"]), "comment": t.get("comment"), "score": score})
    hits.sort(key=lambda h: (-h["score"], h["schema"], h["name"]))
    return hits[:limit]


def sample(ctx: Context, conn_name: str, schema: str, name: str, n: int = 20, where: str | None = None,
           order: str | None = None, desc: bool = False) -> dict:
    """A few rows, through the read-only guard. `where` and `order` are optional SQL fragments."""
    from sqlalchemy.engine import make_url

    from .connectors.sql import run_sql
    from .workspace import render_secrets

    conn = ctx.load_connection(conn_name)
    prep = make_url(render_secrets(conn.url)).get_dialect()().identifier_preparer
    qual = ".".join(prep.quote(x) for x in [*(schema.split(".") if schema else []), name] if x)
    sql = f"SELECT * FROM {qual}"
    if where:
        sql += f" WHERE {where}"
    if order:
        sql += f" ORDER BY {prep.quote(order)}{' DESC' if desc else ''}"
    sql += f" LIMIT {max(1, min(int(n), 1000))}"
    r = run_sql(conn, sql, limit=n)
    from .server import _jsonable

    return {"sql": sql, "columns": [{"name": f.name, "type": str(f.type)} for f in r.table.schema],
            "rows": [list(map(_jsonable, row.values())) for row in r.table.to_pylist()], "ms": r.elapsed_ms}


# ---- query log and insights ---------------------------------------------------------------


def tables_in(sql: str, dialect: str | None = None) -> list[str]:
    """schema.table names a query reads, lower-cased. Unparseable SQL gives []."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = sqlglot.parse(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return []
    out = set()
    for tree in trees:
        if tree is None:
            continue
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if t.name and t.name.lower() not in ctes:
                out.add(".".join(x for x in (t.db, t.name) if x).lower())
    return sorted(out)


def record_query(ctx: Context, conn_name: str, sql: str, *, rows: int | None = None, ms: int | None = None,
                 source: str = "sql", ok: bool = True) -> None:
    """Log one query run through Captain. Append-only: one small file per query, so writers never collide."""
    from .connectors.guard import DIALECTS

    try:
        engine = ctx.load_connection(conn_name).engine
    except Exception:  # noqa: BLE001
        engine = None
    rec = {"id": uuid.uuid4().hex, "connection": conn_name, "user": whoami(), "at": _now(), "sql": sql[:20000],
           "tables": tables_in(sql, DIALECTS.get(engine or "")), "rows": rows, "ms": ms, "source": source, "ok": ok}
    try:
        ctx.store.put_new(f"metastore/queries/{rec['at'][:10]}/{rec['id']}.json", json.dumps(rec).encode())
    except Exception:  # noqa: BLE001  logging must never break the query it describes
        pass


def _queries(ctx: Context, conn_name: str, days: int = 90) -> list[dict]:
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).date().isoformat()
    out = []
    for k in ctx.store.list("metastore/queries/"):
        day = k.split("/")[2] if k.count("/") >= 3 else ""
        if day < cutoff or not k.endswith(".json"):
            continue
        try:
            r = json.loads(ctx.store.get(k))
        except Exception:  # noqa: BLE001
            continue
        if r.get("connection") == conn_name:
            out.append(r)
    return out


def insights(ctx: Context, conn_name: str, schema: str, name: str, examples: int = 10) -> dict:
    """Who queries this table, what's queried with it, and recent queries on it."""
    full, short = f"{schema}.{name}".lower(), name.lower()
    same = {full, short, ".".join(full.split(".")[-2:])}  # a query may name it with or without its catalog or schema
    mine = [q for q in _queries(ctx, conn_name) if same & set(q["tables"])]
    users = Counter(q["user"] for q in mine)
    together = Counter(t for q in mine for t in q["tables"] if t not in same)
    mine.sort(key=lambda q: q["at"], reverse=True)
    return {"queries": len(mine), "frequent_users": [{"user": u, "queries": n} for u, n in users.most_common(10)],
            "queried_with": [{"table": t, "queries": n} for t, n in together.most_common(10)],
            "examples": [{k: q.get(k) for k in ("at", "user", "sql", "rows", "ms", "source")} for q in mine[:examples]]}
