"""Page through any dataset, however big, without downloading it.

DuckDB reads the files where they live (local parquet blobs, or S3 objects through httpfs with the
workspace's cloud credentials). A page asks for `limit` rows at `offset`, so a 400 MB JSONL catalog
returns rows in seconds and parquet reads only the row groups it needs. Sorting, text search and a
filter expression run in DuckDB too. Counting a filtered set scans the data, so it is a separate
opt-in call.

Speed (Edgar, 2026-11-07: the first page of a 400 MB catalog on S3 took 12 s to show):
- one DuckDB database per cloud login for the whole process, with httpfs loaded and the credentials set once; each
  thread gets its own cursor on it (a cursor shares the database's extensions and secrets);
- a file's schema is guessed once from a small sample and remembered, and the page query then names its columns,
  so DuckDB doesn't guess again from tens of MB of JSON on every page. A row that doesn't fit the guessed types makes
  the page fall back to a full guess;
- the first pages of a JSONL or CSV file on S3, unsorted and unfiltered, come from a ranged read of the file's start
  (a megabyte or so, growing until it holds the rows asked for), read by DuckDB on this computer: DuckDB reading the
  same file over httpfs fetches a 16 MB or larger buffer first;
- a count is remembered per file and filter, and the app asks for it after the rows are on screen, never with them.
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
_dbs: dict = {}  # login -> the process's DuckDB database for it
_lock = threading.Lock()
_schemas: dict = {}  # (uri, bytes) -> [(name, type)]
_counts: dict = {}  # (uri, bytes, where) -> rows
SAMPLE_ROWS = 2048  # rows read to guess a JSON or CSV file's columns


def _con(profile: str | None = None):
    """This thread's cursor on the login's shared database."""
    key = profile or "_default"
    cache = getattr(_local, "cons", None)
    if cache is None:
        cache = _local.cons = {}
    if key not in cache:
        with _lock:
            if key not in _dbs:
                _dbs[key] = _open_db(profile)
        cache[key] = _dbs[key].cursor()
    return cache[key]


def _open_db(profile: str | None):
    import duckdb

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


def _reader(uri: str, fmt: str, columns: list | None = None, sample: int | None = None) -> str:
    """A DuckDB table function for the file. With `columns` ([(name, type)]) JSON isn't guessed again; `sample` caps
    the rows read to guess."""
    q = "'" + uri.replace("'", "''") + "'"
    if fmt == "parquet":
        return f"read_parquet({q})"
    if fmt in ("csv", "tsv"):
        return f"read_csv_auto({q}" + (f", sample_size={int(sample)}" if sample else "") + ")"
    if columns:
        lit = lambda x: "'" + str(x).replace("'", "''") + "'"  # noqa: E731
        cols = "{" + ", ".join(f"{lit(n)}: {lit(t)}" for n, t in columns) + "}"
        fmt_ = "newline_delimited" if fmt in ("jsonl", "ndjson") else "auto"
        return f"read_json({q}, format='{fmt_}', columns={cols}, maximum_object_size=67108864)"
    return f"read_json_auto({q}, maximum_object_size=16777216" + (f", sample_size={int(sample)}" if sample else "") + ")"


HEAD_START, HEAD_MAX = 1 << 20, 32 << 20  # the ranged read's first size and its ceiling


def _head_file(t: dict, rows_needed: int, profile: str | None):
    """The start of an S3 JSONL/CSV file, cut at a line end, saved to a local temp file holding at least
    `rows_needed` complete lines (plus the header for CSV) when the file has that many. (path, whole_file) or None
    when the rows asked for lie beyond HEAD_MAX."""
    import tempfile

    from .cloud import aws_session

    u = urlparse(t["uri"])
    key = (t["uri"], t.get("bytes"), "head")
    hit = _schemas.get(key)
    if hit and hit[1] >= rows_needed and os.path.exists(hit[0]):
        return hit[0], hit[2]
    s3 = aws_session(profile, "us-west-1").client("s3")
    size, n = t.get("bytes"), HEAD_START
    extra = 1 if t["format"] in ("csv", "tsv") else 0
    while True:
        r = s3.get_object(Bucket=u.netloc, Key=u.path.lstrip("/"), Range=f"bytes=0-{n - 1}")
        body = r["Body"].read()
        total = size or int(str(r.get("ContentRange", "")).rpartition("/")[2] or 0) or None
        whole = total is not None and len(body) >= total
        cut = body if whole else body[: body.rfind(b"\n") + 1]
        lines = cut.count(b"\n") + (1 if whole and cut and not cut.endswith(b"\n") else 0)
        if lines >= rows_needed + extra or whole:
            break
        if n >= HEAD_MAX:
            return None
        n *= 4
    if hit:
        try:
            os.remove(hit[0])  # a smaller piece read before: this one replaces it
        except OSError:
            pass
    fd, path = tempfile.mkstemp(suffix="." + t["format"], prefix="cst-head-")
    with os.fdopen(fd, "wb") as f:
        f.write(cut)
    _schemas[key] = (path, lines - extra, whole)
    return path, whole


def _schema(con, t: dict, full: bool = False) -> list:
    """[(name, type)] of a table file, guessed once from a small sample (or from all of it with full) and remembered
    for files that can't change under the same size."""
    key = (t["uri"], t.get("bytes"))
    if not full and key in _schemas:
        return _schemas[key]
    cols = [(c[0], str(c[1])) for c in con.sql(f"describe select * from {_reader(t['uri'], t['format'], sample=None if full else SAMPLE_ROWS)}").fetchall()]
    if t.get("bytes") is not None:
        _schemas[key] = cols
    return cols


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
    t_start = time.time()
    head = None
    if not where and not search and not order and t["format"] in ("jsonl", "ndjson", "csv", "tsv") and t["uri"].startswith("s3://"):
        try:
            head = _head_file(t, int(offset) + limit + 1, profile)
        except Exception:  # noqa: BLE001  the ranged read failed: read the file through DuckDB as usual
            head = None
    if head and (t["uri"], t.get("bytes")) not in _schemas:
        # The columns from the piece read: no second read of the file just to guess them.
        cols = _schema(con, {**t, "uri": head[0], "bytes": None}, full=True)
        if t.get("bytes") is not None:
            _schemas[(t["uri"], t.get("bytes"))] = cols
    else:
        cols = _schema(con, t)
    src = _reader(t["uri"], t["format"], cols if t["format"] not in ("parquet", "csv", "tsv") else None)
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
    page = f"{wsql}{osql} limit {limit + 1} offset {int(offset)}"
    if head:
        path, whole = head
        local = {**t, "uri": path, "bytes": None}
        hcols = cols if t["format"] not in ("csv", "tsv") else None
        try:
            res = con.sql(f"select * from {_reader(path, t['format'], hcols)}{page}").fetchall()
        except Exception:  # noqa: BLE001  the remembered types don't fit these rows: guess from them
            hcols = _schema(con, local, full=True)
            res = con.sql(f"select * from {_reader(path, t['format'])}{page}").fetchall()
            cols = hcols
        if len(res) > limit or whole:  # else fewer rows than asked in the piece: read the file itself
            has_more = len(res) > limit
            res = res[:limit]
            return _page(t, ts, cols, res, offset, limit, has_more, t_start, t0, conds, wsql, src, con, count, m)
    try:
        res = con.sql(f"select * from {src}{page}").fetchall()
    except Exception as e:  # noqa: BLE001  a row the small sample's types don't fit: guess from the whole file
        if "Conversion" not in type(e).__name__ and "conver" not in str(e).lower() and "cast" not in str(e).lower():
            raise
        cols = _schema(con, t, full=True)
        src = _reader(t["uri"], t["format"])
        res = con.sql(f"select * from {src}{page}").fetchall()
    has_more = len(res) > limit
    res = res[:limit]
    return _page(t, ts, cols, res, offset, limit, has_more, t_start, t0, conds, wsql, src, con, count, m)


def _page(t, ts, cols, res, offset, limit, has_more, t_start, t0, conds, wsql, src, con, count, m) -> dict:
    out = {"file": t["file"], "files": [{k: x[k] for k in ("file", "bytes", "format")} for x in ts],
           "columns": [{"name": c[0], "type": str(c[1]), "kind": kind_of(str(c[1]))} for c in cols],
           "rows": [[_cell(v) for v in r] for r in res], "offset": int(offset), "limit": limit, "has_more": has_more,
           "ms": int((time.time() - t_start) * 1000), "query_ms": int((time.time() - t0) * 1000), "filtered": bool(conds)}
    ckey = (t["uri"], t.get("bytes"), wsql)
    if ckey in _counts:
        out["total"] = _counts[ckey]
    elif count:
        out["total"] = con.sql(f"select count(*) from {src}{wsql}").fetchall()[0][0]
        if t.get("bytes") is not None:
            _counts[ckey] = out["total"]
    elif not conds and m.row_count and len(ts) == 1:
        out["total"] = m.row_count
    return out


from .data.cells import cell as _cell, kind_of  # noqa: E402  one cell format for every route (data/cells.py)
