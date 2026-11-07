"""cst tables: browse what's in each connected database (pull, list, show, search, sample)."""
from __future__ import annotations

from typing import Optional

import typer

from .. import metastore as MS
from .main import app, ctx, emit

tables_app = typer.Typer(no_args_is_help=True, help="What's in each connected database: schemas, tables, columns, usage")
app.add_typer(tables_app, name="tables")


@tables_app.command("pull")
def tables_pull(conn: str, schema: Optional[list[str]] = typer.Option(None, "--schema", help="only these schemas")):
    """Read a connection's schemas, tables and columns into the workspace (read-only on the database)."""
    r = MS.pull(ctx(), conn, schema or None)
    emit(r, f"{conn}: {r['schemas']} schemas, {r['tables']} tables in {r['seconds']} s"
            + (f", {len(r['errors'])} schema(s) unreadable" if r["errors"] else ""))


@tables_app.command("list")
def tables_list(conn: Optional[str] = typer.Argument(None)):
    c = ctx()
    if not conn:
        rows = MS.overview(c)
        return emit(rows, "\n".join(f"{r['connection']:<20} {r['engine']:<12} "
                                    + (f"{r['tables']} tables, read {r['pulled_at']}" if r["pulled_at"] else "not read yet")
                                    for r in rows))
    d = MS.schemas(c, conn)
    emit(d, "\n".join(f"{s['name']}.{t['name']}  ({t['type']}, {t['columns']} columns)" for s in d["schemas"] for t in s["tables"]))


@tables_app.command("show")
def tables_show(conn: str, table: str = typer.Argument(..., help="schema.table")):
    sch, _, name = table.rpartition(".")
    t = MS.table(ctx(), conn, sch, name)
    lines = [f"{t['schema']}.{t['name']} ({t['type']}), read {t['pulled_at']}"] + [
        f"  {col['name']:<30} {col['type']}" for col in t["columns"]]
    i = t["insights"]
    if i["queries"]:
        lines.append(f"{i['queries']} queries through Captain. Most by: "
                     + ", ".join(f"{u['user']} ({u['queries']})" for u in i["frequent_users"][:3]))
    emit(t, "\n".join(lines))


@tables_app.command("search")
def tables_search(q: str, conn: Optional[str] = typer.Option(None, "--conn")):
    hits = MS.search(ctx(), q, conn)
    emit(hits, "\n".join(f"{h['connection']}  {h['schema']}.{h['name']}  ({h['columns']} columns)" for h in hits) or "no match")


@tables_app.command("sample")
def tables_sample(conn: str, table: str, n: int = typer.Option(20, "-n")):
    sch, _, name = table.rpartition(".")
    r = MS.sample(ctx(), conn, sch, name, n)
    emit(r, "\n".join(["\t".join(c["name"] for c in r["columns"])] + ["\t".join(map(str, row)) for row in r["rows"]]))
