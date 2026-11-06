"""Page through any dataset, however big, without downloading it.

DuckDB reads the files where they live (local parquet blobs, or S3 objects through httpfs with the
workspace's cloud credentials). A page asks for `limit` rows at `offset`, so a 400 MB JSONL catalog
returns rows in seconds and parquet reads only the row groups it needs. Sorting, text search and a
filter expression run in DuckDB too. Counting a filtered set scans the data, so it is a separate
opt-in call.
"""
from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import sqlglot
from sqlglot import exp

from .model.entities import DatasetVersion
from .workspace import Context

TABLE_EXT = (".parquet", ".jsonl", ".ndjson", ".csv", ".tsv", ".json")
PREFERRED = ("items", "products", "catalog", "rows", "data", "part-0")
_local = threading.local()


def _con(profile: str | None = None):
    import duckdb

    key = profile or "_default"
    cache = getattr(_local, "cons", None)
    if cache is None:
        cache = _local.cons = {}
    if key in cache:
        return cache[key]
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    try:
        con.execute("LOAD httpfs")
    except Exception:  # noqa: BLE001
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
    try:
        from .cloud import aws_session

        c = aws_session(profile, "us-west-1").get_credentials()
        if c is not None:
            c = c.get_frozen_credentials()
            tok = f", SESSION_TOKEN '{c.token}'" if c.token else ""
            con.execute(f"CREATE OR REPLACE SECRET cst_s3 (TYPE s3, KEY_ID '{c.access_key}', SECRET '{c.secret_key}', REGION 'us-west-1'{tok})")
    except Exception:  # noqa: BLE001
        pass  # local-only use still works
    cache[key] = con
    return con


def tables(ctx: Context, m: DatasetVersion) -> list[dict]:
    """The files of a dataset that can be read as a table, best first."""
    out = []
    for f in m.files:
        if f.path.endswith(".parquet"):
            out.append({"file": f.path, "uri": _blob_uri(ctx, f.sha256), "bytes": f.bytes, "format": "parquet"})
    for f in m.external:
        name = f.uri.rsplit("/", 1)[-1].lower()
        if f.uri.startswith(("s3://", "gs://", "/")) and name.endswith(TABLE_EXT):
            out.append({"file": f.uri, "uri": f.uri, "bytes": f.bytes, "format": name.rsplit(".", 1)[-1]})

    def rank(t):
        base = t["file"].rsplit("/", 1)[-1].lower()
        pref = next((i for i, p in enumerate(PREFERRED) if base.startswith(p)), len(PREFERRED))
        return (pref, -(t["bytes"] or 0))

    return sorted(out, key=rank)


def _blob_uri(ctx: Context, digest: str) -> str:
    key = ctx.registry.blob_key(digest)
    st = ctx.store
    if hasattr(st, "root"):
        return os.path.join(st.root, key)
    if hasattr(st, "bucket"):
        return f"s3://{st.bucket}/{st.prefix + '/' if st.prefix else ''}{key}"
    raise ValueError("browsing blobs in GCS or Azure workspaces is not supported yet")


def _reader(uri: str, fmt: str) -> str:
    q = "'" + uri.replace("'", "''") + "'"
    if fmt == "parquet":
        return f"read_parquet({q})"
    if fmt in ("csv", "tsv"):
        return f"read_csv_auto({q})"
    return f"read_json_auto({q}, maximum_object_size=67108864)"


BLOCKED_FUNCS = ("read_", "glob", "scan", "query", "getenv", "copy", "install", "load", "attach")


def _check_where(where: str) -> str:
    try:
        e = sqlglot.parse_one(f"select 1 where {where}", read="duckdb")
    except sqlglot.errors.ParseError as err:
        raise ValueError(f"the filter is not valid SQL: {err}") from None
    cond = e.args.get("where")
    if cond is None:
        raise ValueError("the filter must be a condition, like price < 50 and brand = 'Acme'")
    for node in cond.walk():
        if isinstance(node, (exp.Select, exp.Subquery, exp.Command)):
            raise ValueError("subqueries are not allowed in a filter")
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if name.startswith(BLOCKED_FUNCS):
                raise ValueError(f"{name}() is not allowed in a filter")
    return cond.this.sql(dialect="duckdb")


def rows(ctx: Context, m: DatasetVersion, file: str | None = None, offset: int = 0, limit: int = 100,
         order: str | None = None, desc: bool = False, search: str | None = None, where: str | None = None,
         count: bool = False, profile: str | None = None) -> dict:
    ts = tables(ctx, m)
    if not ts:
        kinds = sorted({f.uri.rsplit(".", 1)[-1] for f in m.external if "." in f.uri.rsplit("/", 1)[-1]})
        raise ValueError("this dataset has no file that reads as a table" + (f" (it holds {', '.join(kinds)})" if kinds else ""))
    t = next((x for x in ts if x["file"] == file), None) if file else ts[0]
    if t is None:
        raise ValueError(f"{file} is not a table file of this dataset")
    limit = max(1, min(int(limit), 5000))
    con = _con(profile)
    src = _reader(t["uri"], t["format"])
    cols = con.sql(f"describe select * from {src}").fetchall()
    names = [c[0] for c in cols]
    conds = []
    if where:
        conds.append(f"({_check_where(where)})")
    if search:
        s = search.replace("'", "''")
        text_cols = [c[0] for c in cols if "VARCHAR" in str(c[1]).upper()] or names
        conds.append("(" + " or ".join(f'cast("{c}" as varchar) ilike \'%{s}%\'' for c in text_cols) + ")")
    wsql = (" where " + " and ".join(conds)) if conds else ""
    osql = ""
    if order:
        if order not in names:
            raise ValueError(f"unknown column {order}")
        osql = f' order by "{order}" {"desc" if desc else "asc"} nulls last'
    t0 = time.time()
    res = con.sql(f"select * from {src}{wsql}{osql} limit {limit + 1} offset {int(offset)}").fetchall()
    has_more = len(res) > limit
    res = res[:limit]
    out = {"file": t["file"], "files": [{k: x[k] for k in ("file", "bytes", "format")} for x in ts],
           "columns": [{"name": c[0], "type": str(c[1])} for c in cols],
           "rows": [[_cell(v) for v in r] for r in res], "offset": int(offset), "limit": limit, "has_more": has_more,
           "ms": int((time.time() - t0) * 1000), "filtered": bool(conds)}
    if count:
        out["total"] = con.sql(f"select count(*) from {src}{wsql}").fetchall()[0][0]
    elif not conds and m.row_count and len(ts) == 1:
        out["total"] = m.row_count
    return out


def _cell(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, dict)):
        import json

        return json.dumps(v, default=str)[:2000]
    return str(v)
