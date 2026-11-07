"""Data and model lineage, like Unity Catalog's: one graph across warehouse tables, datasets, pipelines, models, runs
and deployments, walked upstream and downstream from any node, with column-level lineage for datasets made by SQL.

Sources of edges:
- Captain's own records (ops.lineage): dataset versions, pipelines, backtest runs, environments.
- Model registries (models.lineage): models, their parents and architectures.
- SQL provenance: a dataset saved from SQL reads warehouse tables, found by parsing its SQL. Each becomes a
  `table:<connection>/<schema.table>` node, so lineage reaches back into the warehouse.
- The warehouse's own lineage, imported on request (`import_external`): Databricks Unity Catalog's
  `system.access.table_lineage` and `column_lineage` tables, and Snowflake's `ACCOUNT_USAGE.ACCESS_HISTORY`. Read
  with the connection's own read-only login and cached in the workspace (`lineage/external/<conn>.json`).

Node ids are stable strings: `dataset:<name>@v<n>`, `pipeline:<name>@v<n>`, `run:<name>@v<n>`, `model:<brand>@<v>`,
`table:<connection>/<schema.table>`, `environment:<name>`, `architecture:<name>`.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict, deque

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami


def _ext_key(conn: str) -> str:
    return f"lineage/external/{conn}.json"


def _table_id(conn: str, table: str) -> str:
    return f"table:{conn}/{table.lower()}"


def graph(ctx: Context) -> dict:
    from . import metastore as MS
    from . import models as MD
    from . import ops
    from .connectors.guard import DIALECTS

    g = ops.lineage(ctx)
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = list(g["edges"])
    try:
        mg = MD.lineage(ctx)
        for n in mg["nodes"]:
            nodes.setdefault(n["id"], n)
        edges += mg["edges"]
    except Exception:  # noqa: BLE001  no registry attached
        pass
    # SQL provenance reaches into the warehouse
    for name in ctx.registry.names("dataset"):
        for v in ctx.registry.versions("dataset", name):
            m = ctx.registry.load("dataset", name, v)
            p = m.provenance
            if p.source != "sql" or not p.sql or not p.connection:
                continue
            try:
                engine = ctx.load_connection(p.connection).engine
            except Exception:  # noqa: BLE001
                engine = None
            me = f"dataset:{name}@v{v}"
            for t in MS.tables_in(p.sql, DIALECTS.get(engine or "")):
                tid = _table_id(p.connection, t)
                nodes.setdefault(tid, {"id": tid, "kind": "table", "label": t, "connection": p.connection})
                edges.append({"src": tid, "dst": me, "relation": "read_by"})
    # imported warehouse lineage
    for conn in ctx.connections():
        ext = external(ctx, conn)
        if not ext:
            continue
        for e in ext["tables"]:
            s, d = _table_id(conn, e["source"]), _table_id(conn, e["target"])
            for tid, lab in ((s, e["source"]), (d, e["target"])):
                nodes.setdefault(tid, {"id": tid, "kind": "table", "label": lab, "connection": conn})
            edges.append({"src": s, "dst": d, "relation": "writes", "via": e.get("via"), "source_of_edge": ext["system"]})
    seen, uniq = set(), []
    for e in edges:
        k = (e["src"], e["dst"], e["relation"])
        if k not in seen and e["src"] in nodes and e["dst"] in nodes:
            seen.add(k)
            uniq.append(e)
    return {"nodes": list(nodes.values()), "edges": uniq}


def neighborhood(ctx: Context, node: str, up: int = 2, down: int = 2) -> dict:
    """The part of the graph within `up` steps upstream and `down` steps downstream of a node. A node given without
    a version (dataset:x) matches its newest version."""
    g = graph(ctx)
    ids = {n["id"] for n in g["nodes"]}
    if node not in ids:
        same = sorted((i for i in ids if i.startswith(node + "@v")), key=lambda i: int(i.rsplit("@v", 1)[1]))
        if not same:
            raise NotFound(f"{node} isn't in the lineage graph")
        node = same[-1]
    ins, outs = defaultdict(list), defaultdict(list)
    for e in g["edges"]:
        outs[e["src"]].append(e)
        ins[e["dst"]].append(e)
    keep, kept_edges = {node: 0}, []

    def walk(start, depth, nxt, key, sign):
        q = deque([(start, 0)])
        while q:
            cur, d = q.popleft()
            if d >= depth:
                continue
            for e in nxt[cur]:
                other = e[key]
                kept_edges.append(e)
                if other not in keep:
                    keep[other] = sign * (d + 1)
                    q.append((other, d + 1))

    walk(node, up, ins, "src", -1)
    walk(node, down, outs, "dst", 1)
    nodes = [{**n, "level": keep[n["id"]]} for n in g["nodes"] if n["id"] in keep]
    uniq = {(e["src"], e["dst"], e["relation"]): e for e in kept_edges}
    more_up = any(ins[n["id"]] and n["level"] == -up for n in nodes)
    more_down = any(outs[n["id"]] and n["level"] == down for n in nodes)
    return {"focus": node, "nodes": nodes, "edges": list(uniq.values()), "more_upstream": more_up,
            "more_downstream": more_down}


# ---- column lineage ---------------------------------------------------------------------------


def columns(ctx: Context, dataset: str, column: str) -> dict:
    """Where one column of a SQL-made dataset came from: warehouse table columns, found by tracing its SQL with
    SQLGlot against the connection's read table metadata (cst tables pull)."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.lineage import lineage as sg_lineage

    from . import metastore as MS
    from .connectors.guard import DIALECTS

    name, _, ver = dataset.partition("@v")
    m = ctx.registry.load("dataset", name, int(ver) if ver else None)
    p = m.provenance
    if p.source != "sql" or not p.sql:
        return {"dataset": f"{m.name}@v{m.version}", "column": column, "sources": [], "known": False,
                "reason": f"{m.name} wasn't made by SQL (it came from {p.source}), so its columns can't be traced."}
    engine = ctx.load_connection(p.connection).engine
    dialect = DIALECTS.get(engine)
    schema = {}
    doc = MS.cached(ctx, p.connection)
    if doc:
        for s in doc["schemas"]:
            for t in s["tables"]:
                cols = {c["name"]: str(c["type"]) for c in t["columns"]}
                schema.setdefault(s["name"].split(".")[-1], {})[t["name"]] = cols
    try:
        node = sg_lineage(column, sqlglot.parse_one(p.sql, read=dialect), schema=schema or None, dialect=dialect)
    except Exception as e:  # noqa: BLE001
        return {"dataset": f"{m.name}@v{m.version}", "column": column, "sources": [], "known": False,
                "reason": f"Couldn't trace {column}: {str(e).splitlines()[0][:200]}"}
    sources, steps = [], []
    for n in node.walk():
        steps.append(n.name)
        if isinstance(n.expression, exp.Table) or not n.downstream:
            tbl = n.expression if isinstance(n.expression, exp.Table) else n.expression.find(exp.Table)
            col = n.name.split(".")[-1]
            if tbl is not None:
                full = ".".join(x for x in (tbl.db, tbl.name) if x).lower()
                sources.append({"table": full, "column": col, "node": _table_id(p.connection, full)})
    uniq = list({(s["table"], s["column"]): s for s in sources}.values())
    return {"dataset": f"{m.name}@v{m.version}", "column": column, "sql": p.sql, "sources": uniq, "known": bool(uniq),
            "path": steps, "reason": None if uniq else "No source column found. The column may be computed from constants."}


# ---- the warehouse's own lineage ------------------------------------------------------------

DATABRICKS_SQL = """
SELECT source_table_full_name AS source, target_table_full_name AS target, entity_type AS via,
       max(event_time) AS last_seen
FROM system.access.table_lineage
WHERE source_table_full_name IS NOT NULL AND target_table_full_name IS NOT NULL
  AND event_date >= date_sub(current_date(), {days})
GROUP BY 1, 2, 3
"""

DATABRICKS_COLUMNS_SQL = """
SELECT source_table_full_name AS source, source_column_name AS source_column,
       target_table_full_name AS target, target_column_name AS target_column
FROM system.access.column_lineage
WHERE source_table_full_name IS NOT NULL AND target_table_full_name IS NOT NULL
  AND event_date >= date_sub(current_date(), {days})
GROUP BY 1, 2, 3, 4
"""

SNOWFLAKE_SQL = """
SELECT DISTINCT src.value:"objectName"::string AS source, tgt.value:"objectName"::string AS target,
       'QUERY' AS via, max(ah.query_start_time) AS last_seen
FROM snowflake.account_usage.access_history ah,
     lateral flatten(input => ah.base_objects_accessed) src,
     lateral flatten(input => ah.objects_modified) tgt
WHERE ah.query_start_time >= dateadd(day, -{days}, current_timestamp())
  AND src.value:"objectDomain"::string IN ('Table', 'View') AND tgt.value:"objectDomain"::string = 'Table'
GROUP BY 1, 2, 3
"""


def import_external(ctx: Context, conn_name: str, days: int = 30) -> dict:
    """Read the warehouse's own lineage with the connection's login. Databricks needs access to the system.access
    schema, and Snowflake needs ACCOUNT_USAGE (Enterprise edition). Nothing is written to the warehouse."""
    from .connectors.sql import run_sql

    conn = ctx.load_connection(conn_name)
    days = max(1, min(int(days), 365))
    if conn.engine == "databricks":
        system, sql, col_sql = "Databricks Unity Catalog", DATABRICKS_SQL, DATABRICKS_COLUMNS_SQL
    elif conn.engine == "snowflake":
        system, sql, col_sql = "Snowflake access history", SNOWFLAKE_SQL, None
    else:
        raise ValueError(f"{conn.engine} doesn't keep lineage Captain can read. Databricks and Snowflake do. "
                         "Lineage from Captain's own SQL imports works for every database.")
    rows = run_sql(conn, sql.format(days=days), limit=200_000).table.to_pylist()
    tables = [{"source": str(r["source"]).lower(), "target": str(r["target"]).lower(), "via": r.get("via"),
               "last_seen": str(r.get("last_seen") or "")} for r in rows if r.get("source") and r.get("target")]
    cols = []
    if col_sql:
        try:
            cols = [{k: str(v).lower() for k, v in r.items()}
                    for r in run_sql(conn, col_sql.format(days=days), limit=500_000).table.to_pylist()]
        except Exception:  # noqa: BLE001  column lineage is a bonus; tables alone still help
            cols = []
    doc = {"connection": conn_name, "system": system, "days": days, "read_at": dt.datetime.now(dt.UTC).isoformat(),
           "read_by": whoami(), "tables": tables, "columns": cols}
    body = json.dumps(doc).encode()
    for _ in range(5):
        try:
            _, tag = ctx.store.get_with_tag(_ext_key(conn_name))
        except NotFound:
            tag = None
        try:
            ctx.store.put_if(_ext_key(conn_name), body, tag)
            break
        except Conflict:
            continue
    return {"connection": conn_name, "system": system, "table_edges": len(tables), "column_edges": len(cols),
            "days": days}


def external(ctx: Context, conn_name: str) -> dict | None:
    try:
        return json.loads(ctx.store.get(_ext_key(conn_name)))
    except NotFound:
        return None


# ---- quality profile --------------------------------------------------------------------------


def profile_dataset(ctx: Context, ref: str) -> dict:
    """Per column: type, null share, distinct count, min and max. Plus rows and age. Computed with DuckDB on the
    dataset's own files."""
    import duckdb

    from .data import datasets as DS

    name, _, ver = ref.partition("@v")
    m = ctx.registry.load("dataset", name, int(ver) if ver else None)
    t = DS.load_table(ctx, m)
    con = duckdb.connect()
    con.register("t", t)
    out = []
    n = t.num_rows
    for f in t.schema:
        q = f'"{f.name}"'
        simple = str(f.type).split("<")[0] not in ("struct", "list", "large_list", "map")
        stats = con.execute(f"SELECT count({q}), count(DISTINCT {q}) FROM t").fetchone() if simple else (
            con.execute(f"SELECT count({q}) FROM t").fetchone() + (None,))
        lo = hi = None
        if simple and str(f.type) not in ("bool",):
            try:
                lo, hi = con.execute(f"SELECT min({q}), max({q}) FROM t").fetchone()
            except Exception:  # noqa: BLE001
                pass
        out.append({"name": f.name, "type": str(f.type), "nulls": (n - stats[0]) / n if n else None,
                    "distinct": stats[1], "min": None if lo is None else str(lo)[:80],
                    "max": None if hi is None else str(hi)[:80]})
    age_h = (dt.datetime.now(dt.UTC) - m.created_at).total_seconds() / 3600
    return {"ref": f"{m.name}@v{m.version}", "rows": n, "created": m.created_at.isoformat(), "age_hours": round(age_h, 1),
            "columns": out}
