"""Every table of the workspace in one list: the datasets Captain keeps and the tables of each connected database.

A dataset is a table Captain keeps versions of. A database table is one Captain reads where it lives. The Data
screen showed them on two tabs; they are the same thing to a person looking for their data, so this lists them
together, one row each, saying where the table lives, how many rows it has, whether Captain keeps versions of it,
and when it was last read.

Cheap by design: the datasets come from the registry as they do for `GET /api/datasets`, the database tables from
what `cst tables pull` already cached (no database is asked anything), and "last read" from one listing of the
read log's keys (the day is in the key, so no read record is opened).
"""
from __future__ import annotations

from urllib.parse import unquote

from ..storage.backend import NotFound
from ..workspace import Context

READS = "metastore/reads/"


def last_read_days(ctx: Context) -> dict[str, str]:
    """dataset name -> the last day (YYYY-MM-DD) anything read it, from the read log's keys alone."""
    out: dict[str, str] = {}
    try:
        keys = ctx.store.list(READS)
    except Exception:  # noqa: BLE001  a store that can't list the log: the column is empty, the list still shows
        return out
    for k in keys:
        parts = k[len(READS):].split("/")
        if len(parts) < 3:
            continue
        name, day = unquote(parts[0]), parts[1]
        if day > out.get(name, ""):
            out[name] = day
    return out


def _dataset_where(folder: str, prov: dict) -> str:
    """Where a dataset lives, in words: Captain keeps it, in which folder, and what it was made from."""
    made = ""
    if prov.get("source") == "sql" and prov.get("connection"):
        made = f", a saved query on {prov['connection']}"
    elif prov.get("connection"):
        made = f", from {prov['connection']}"
    return f"Captain{f', folder {folder}' if folder else ''}{made}"


def tables(ctx: Context, member=None, q: str | None = None, kind: str | None = None, scoped: bool = False) -> dict:
    """{rows: [...], connections: [...]}. Each row:
      {kind: "dataset" | "table", name, where, rows, versions, version, last_read, columns, ...}
    - a dataset: versions is how many Captain keeps (its latest version number), last_read the last day it was read;
    - a database table: versions is None (Captain keeps none: it is read where it lives), last_read when Captain
      last read the database's tables, and `connection`, `schema`, `engine` say where to open it.
    `member` is the list routes' name predicate (a project or the Explorer's pick): it filters datasets. Database
    tables belong to no project, so with something picked (`scoped`) they are left out.
    q: every word must be in the name, the place it lives, its description or a column's name.
    kind: "dataset" or "table" lists one of the two."""
    from .. import classes as CL
    from .. import library as LIB
    from .. import metastore as MS

    words = [w for w in (q or "").lower().split() if w]
    out: list[dict] = []
    if kind in (None, "dataset"):
        try:
            placed, _ = LIB._read(ctx)
        except Exception:  # noqa: BLE001  no library file yet: every dataset sits where its name says
            placed = {"placed": {}}
        reads = last_read_days(ctx)
        for n in ctx.registry.names("dataset"):
            if member is not None and not member(n):
                continue
            m = ctx.registry.load("dataset", n)
            cols = [f.get("name") for f in (m.schema_fields or []) if isinstance(f, dict) and f.get("name")]
            prov = m.provenance.model_dump(mode="json") if m.provenance else {}
            folder = LIB.folder_of(placed, n)
            row = {"kind": "dataset", "name": n, "where": _dataset_where(folder, prov), "folder": folder,
                   "rows": m.row_count if (m.row_count or 0) > 0 else None, "versions": m.version, "version": m.version,
                   "last_read": reads.get(n), "columns": len(cols), "column_names": cols,
                   "classes": CL.classes_of(ctx, n), "dataset_kind": m.dataset_kind.value,
                   "description": m.description, "by_reference": bool(m.external)}
            out.append(row)
    connections = []
    if kind in (None, "table"):
        for conn in ctx.connections():
            try:
                c = ctx.load_connection(conn)
            except NotFound:
                continue
            doc = MS.cached(ctx, conn)
            connections.append({"connection": conn, "engine": c.engine, "pulled_at": doc and doc["pulled_at"],
                                "tables": sum(len(s["tables"]) for s in doc["schemas"]) if doc else None})
            if doc is None or scoped:
                continue
            for s in doc["schemas"]:
                schema = str(s["name"]).replace('"', "")
                for t in s["tables"]:
                    cols = [x.get("name") for x in t.get("columns") or [] if x.get("name")]
                    out.append({"kind": "table", "name": t["name"], "where": f"{conn}, {schema}", "connection": conn,
                                "schema": s["name"], "engine": c.engine, "rows": t.get("rows"), "versions": None,
                                "version": None, "last_read": doc["pulled_at"], "columns": len(cols),
                                "column_names": cols, "type": t.get("type"), "description": t.get("comment")})
    if words:
        def hit(r: dict) -> bool:
            hay = " ".join([r["name"], r["where"], r.get("description") or "", *r.get("column_names", [])]).lower()
            return all(w in hay for w in words)
        out = [r for r in out if hit(r)]
    for r in out:
        # Matched only through a column: say which, then drop the names (a wide table has hundreds).
        if words and not all(w in f"{r['name']} {r['where']} {r.get('description') or ''}".lower() for w in words):
            r["matched_column"] = next((c for c in r.get("column_names", []) if any(w in c.lower() for w in words)), None)
        r.pop("column_names", None)
    out.sort(key=lambda r: (r["name"].lower(), r["where"]))
    return {"rows": out, "connections": connections}
