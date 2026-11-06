"""Labels defined as versioned code, computed where the logs live.

Changing a training label often means editing a batch job and waiting weeks. Moving label derivation next to the
data loader makes a new label a code change. Here a label
is a small, versioned definition:
  sql    a DuckDB aggregate over the rows of one group, e.g.
         max(case when clicked and dwell_s >= 5 then 1 else 0 end)
  grain  the group: search (session_id + query), session, user (user_id), or impression (one row each)
  where  an optional row filter
DuckDB runs it over an events dataset in place (local parquet or S3), so large logs are not downloaded.
`preview` shows a page of labelled groups and the label's mean. `compute` writes the labels as a dataset
whose provenance pins the label version and the logs version, so a model can say which label it learned.
"""
from __future__ import annotations

import pyarrow as pa

from .model.entities import LabelDef
from .storage.registry import check_name
from .workspace import Context, whoami

GRAIN_KEYS = {"search": ["session_id", "query"], "session": ["session_id"], "user": ["user_id"], "impression": None}
BLOCKED = ("read_", "glob", "copy", "attach", "install", "load", "getenv", "query(", "pragma", ";")


def _check_sql(sql: str) -> str:
    import sqlglot

    low = sql.lower()
    if any(b in low for b in BLOCKED):
        raise ValueError("a label is one aggregate expression over the group's rows. File access, statements and ; are not allowed.")
    try:
        sqlglot.parse_one(f"select {sql} from t", read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"the label is not valid SQL: {e}") from None
    return sql


def define(ctx: Context, name: str, sql: str, grain: str = "search", where: str | None = None, dtype: str = "binary",
           description: str = "", dry_run: bool = False) -> LabelDef:
    from . import browse as B

    if grain not in GRAIN_KEYS:
        raise ValueError(f"grain is one of {', '.join(GRAIN_KEYS)}")
    _check_sql(sql)
    if where:
        B._check_where(where)
    m = LabelDef(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=check_name(name), version=0,
                 sql=sql.strip(), grain=grain, where=where, dtype=dtype, description=description)
    return m if dry_run else ctx.registry.publish(m)


def _query(ctx: Context, label: str, logs: str):
    from . import browse as B

    lab, _ = ctx.registry.resolve("label", label)
    lm, _ = ctx.registry.resolve("dataset", logs)
    ts = B.tables(ctx, lm)
    if not ts:
        raise ValueError(f"{logs} has no file that reads as a table")
    con = B._con(None)
    src = B._reader(ts[0]["uri"], ts[0]["format"])
    cols = {c[0] for c in con.sql(f"describe select * from {src}").fetchall()}
    keys = GRAIN_KEYS[lab.grain]
    if keys is not None:
        missing = [k for k in keys if k not in cols]
        if missing:
            raise ValueError(f"grain {lab.grain} groups by {keys}, and {logs} has no {missing[0]} column")
    wsql = f" where ({B._check_where(lab.where)})" if lab.where else ""
    if keys is None:
        sel = f"select row_number() over () as row_id, ({_row_expr(lab.sql)}) as label from {src}{wsql}"
    else:
        kq = ", ".join(f'"{k}"' for k in keys)
        sel = f"select {kq}, ({lab.sql}) as label from {src}{wsql} group by {kq}"
    return con, sel, lab, lm


def _row_expr(sql: str) -> str:
    """At impression grain each row is its own group: strip one outer aggregate so max(x) reads x."""
    s = sql.strip()
    low = s.lower()
    for agg in ("max(", "min(", "sum(", "avg(", "any_value("):
        if low.startswith(agg) and s.endswith(")"):
            return s[len(agg):-1]
    return s


def preview(ctx: Context, label: str, logs: str, limit: int = 20) -> dict:
    con, sel, lab, lm = _query(ctx, label, logs)
    try:
        rows = con.sql(f"{sel} limit {max(1, min(int(limit), 500))}").fetchall()
        stats = con.sql(f"select count(*), avg(cast(label as double)), count(label) from ({sel})").fetchall()[0]
        cols = [c[0] for c in con.sql(f"describe {sel}").fetchall()]
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"the label failed on {logs}: {str(e).splitlines()[0][:300]}") from None
    return {"label": f"{lab.name}@v{lab.version}", "logs": f"{lm.name}@v{lm.version}", "grain": lab.grain, "groups": stats[0],
            "mean": None if stats[1] is None else round(float(stats[1]), 5), "non_null": stats[2], "columns": cols,
            "rows": [[str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v for v in r] for r in rows]}


def compute(ctx: Context, label: str, logs: str, into: str, dry_run: bool = False):
    from .data.datasets import snapshot, to_parquet
    from .model.entities import DatasetKind, Provenance

    con, sel, lab, lm = _query(ctx, label, logs)
    try:
        t = con.sql(sel).to_arrow_table()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"the label failed on {logs}: {str(e).splitlines()[0][:300]}") from None
    if not isinstance(t, pa.Table):
        t = t.read_all()
    m = snapshot(ctx, check_name(into), t, kind=DatasetKind.generic, dry_run=True,
                 provenance=Provenance(source="generated", params={"label": f"{lab.name}@v{lab.version}", "logs": f"{lm.name}@v{lm.version}",
                                                                     "grain": lab.grain, "sql": lab.sql, "where": lab.where}),
                 description=f"label {lab.name}@v{lab.version} over {lm.name}@v{lm.version}, one row per {lab.grain}")
    m = m.model_copy(update={"tags": {**(lm.tags or {}), "label": f"{lab.name}@v{lab.version}"}})
    if dry_run:
        return m
    e = ctx.registry.put_blob(to_parquet(t), "data/part-0.parquet")
    return ctx.registry.publish(m.model_copy(update={"files": [e]}))
