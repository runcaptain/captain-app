"""Local SQLite index of the bucket, rebuilt by `cst sync`. The bucket is the source of truth;
this cache only makes listing fast and lets the app work offline."""
from __future__ import annotations

import json
import os
import sqlite3

from .model.entities import KIND_DIRS
from .workspace import Context, home


def db_path(workspace: str) -> str:
    d = os.path.join(home(), "index")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{workspace.replace('/', '_')}.sqlite")


def connect(workspace: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path(workspace))
    db.execute(
        """create table if not exists versions (
             kind text, name text, version integer, created_by text, created_at text,
             description text, manifest text, primary key (kind, name, version))"""
    )
    return db


def sync(ctx: Context) -> dict[str, int]:
    db = connect(ctx.workspace.name)
    counts: dict[str, int] = {}
    with db:
        db.execute("delete from versions")
        for kind in KIND_DIRS:
            n = 0
            for name in ctx.registry.names(kind):
                for v in ctx.registry.versions(kind, name):
                    m = ctx.registry.load(kind, name, v)
                    db.execute(
                        "insert into versions values (?,?,?,?,?,?,?)",
                        (kind, name, v, m.created_by, m.created_at.isoformat(), m.description,
                         json.dumps(m.model_dump(mode="json"))),
                    )
                    n += 1
            counts[kind] = n
    return counts


def rows(workspace: str, kind: str | None = None) -> list[dict]:
    db = connect(workspace)
    q = "select kind, name, version, created_by, created_at, description from versions"
    args: tuple = ()
    if kind:
        q += " where kind = ?"
        args = (kind,)
    q += " order by kind, name, version"
    cols = ["kind", "name", "version", "created_by", "created_at", "description"]
    return [dict(zip(cols, r)) for r in db.execute(q, args)]


def index_md(ctx: Context) -> str:
    """INDEX.md is generated on read, never written to the bucket by several people at once."""
    out = [f"# {ctx.workspace.name}", "", f"Storage: `{ctx.workspace.storage_uri}`", ""]
    for kind in KIND_DIRS:
        names = ctx.registry.names(kind)
        if not names:
            continue
        out += [f"## {KIND_DIRS[kind]}", "", "| name | latest | by | created |", "|---|---|---|---|"]
        for name in names:
            m = ctx.registry.load(kind, name)
            out.append(f"| {name} | v{m.version} | {m.created_by} | {m.created_at:%Y-%m-%d} |")
        out.append("")
    return "\n".join(out)
