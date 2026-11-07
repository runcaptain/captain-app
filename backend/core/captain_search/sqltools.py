"""The query editor's tools (Querybook's cell menu, snippets and query history): format SQL with keywords in upper or
lower case, transpile it between engines, explain it on its connection, keep snippets with {{placeholders}}, and list
the queries run through Captain."""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

import sqlglot
import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

SNIPPETS_KEY = "snippets/snippets.yaml"
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _dialect(ctx: Context, conn: str | None, dialect: str | None) -> str | None:
    if dialect:
        return dialect
    if conn:
        from .connectors.guard import DIALECTS

        return DIALECTS.get(ctx.load_connection(conn).engine)
    return None


def format_sql(sql: str, case: str = "upper", dialect: str | None = None) -> str:
    """Pretty-printed, keywords upper or lower case. Identifiers and literals are left as written."""
    if case not in ("upper", "lower"):
        raise ValueError("case is upper or lower")
    try:
        out = ";\n\n".join(e.sql(dialect=dialect, pretty=True) for e in sqlglot.parse(sql, read=dialect) if e is not None)
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"This SQL doesn't parse, so it can't be formatted: {str(e).splitlines()[0]}") from None
    if case == "lower":
        from sqlglot.tokens import Tokenizer, TokenType

        keep = {TokenType.VAR, TokenType.STRING, TokenType.NUMBER, TokenType.IDENTIFIER, TokenType.NATIONAL_STRING}
        chars = list(out)
        for t in Tokenizer().tokenize(out):
            if t.token_type in keep or not t.text or not t.text.isupper():
                continue
            if t.text.upper() in Tokenizer.KEYWORDS or t.token_type.name == t.text.upper():
                chars[t.start:t.end + 1] = list(t.text.lower())
        out = "".join(chars)
    return out


def transpile_sql(sql: str, read: str | None, write: str) -> dict:
    """The same query in another engine's dialect, with what didn't carry over."""
    if not write:
        raise ValueError("name the engine to transpile to, like snowflake or bigquery")
    warnings: list[str] = []
    try:
        out = sqlglot.transpile(sql, read=read, write=write, pretty=True, unsupported_level=sqlglot.ErrorLevel.WARN)
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"This SQL doesn't parse as {read or 'standard SQL'}: {str(e).splitlines()[0]}") from None
    except sqlglot.errors.UnsupportedError as e:
        warnings.append(str(e).splitlines()[0])
        out = sqlglot.transpile(sql, read=read, write=write, pretty=True, unsupported_level=sqlglot.ErrorLevel.IGNORE)
    return {"sql": ";\n\n".join(out), "from": read, "to": write, "warnings": warnings}


def explain(ctx: Context, conn_name: str, sql: str) -> dict:
    """The engine's plan for a read-only query, through the read-only guard (EXPLAIN of a read is a read)."""
    from .connectors.sql import run_sql

    conn = ctx.load_connection(conn_name)
    q = sql.strip().rstrip(";")
    if re.match(r"(?is)^\s*explain\b", q):
        q = re.sub(r"(?is)^\s*explain\s+", "", q)
    r = run_sql(conn, f"EXPLAIN {q}", limit=500)
    lines = [" ".join("" if v is None else str(v) for v in row.values()) for row in r.table.to_pylist()]
    return {"connection": conn_name, "plan": "\n".join(lines), "ms": r.elapsed_ms}


# ---- snippets ------------------------------------------------------------------------------


def _read(ctx: Context) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(SNIPPETS_KEY)
        return (yaml.safe_load(body) or {"snippets": []}), tag
    except NotFound:
        return {"snippets": []}, None


def _write(ctx: Context, change):
    for _ in range(10):
        doc, tag = _read(ctx)
        out = change(doc)
        try:
            ctx.store.put_if(SNIPPETS_KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return out
        except Conflict:
            continue
    raise Conflict("The snippets kept changing while saving. Try again.")


def placeholders(sql: str) -> list[str]:
    """The {{names}} a snippet asks for when it's inserted, in order, once each."""
    seen: list[str] = []
    for m in VAR_RE.finditer(sql or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def snippets(ctx: Context, engine: str | None = None, q: str | None = None) -> list[dict]:
    doc, _ = _read(ctx)
    out = [s for s in doc["snippets"] if not engine or not s.get("engine") or s.get("engine") == engine]
    if q:
        words = q.lower().split()
        out = [s for s in out if all(w in f"{s['title']} {s.get('description', '')} {s['sql']}".lower() for w in words)]
    return sorted(out, key=lambda s: (not s.get("golden"), s["title"].lower()))


def save_snippet(ctx: Context, body: dict, sid: str | None = None) -> dict:
    title = str(body.get("title", "")).strip()
    sql = str(body.get("sql", ""))
    if sid is None and (not title or not sql.strip()):
        raise ValueError("a snippet needs a title and its SQL")

    def change(doc):
        if sid is None:
            s = {"id": uuid.uuid4().hex[:10], "title": title, "description": body.get("description", ""), "sql": sql,
                 "engine": body.get("engine") or None, "golden": bool(body.get("golden")), "created_by": whoami(),
                 "created_at": dt.datetime.now(dt.UTC).isoformat()}
            doc["snippets"].append(s)
        else:
            s = next((x for x in doc["snippets"] if x["id"] == sid), None)
            if s is None:
                raise NotFound(f"no snippet {sid}")
            for k in ("title", "description", "sql", "engine", "golden"):
                if k in body:
                    s[k] = bool(body[k]) if k == "golden" else body[k]
            s["updated_by"], s["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat()
        s["vars"] = placeholders(s["sql"])
        return dict(s)

    return _write(ctx, change)


def delete_snippet(ctx: Context, sid: str) -> dict:
    def change(doc):
        if not any(x["id"] == sid for x in doc["snippets"]):
            raise NotFound(f"no snippet {sid}")
        doc["snippets"] = [x for x in doc["snippets"] if x["id"] != sid]
        return {"deleted": sid}

    return _write(ctx, change)


def fill(sql: str, values: dict) -> str:
    """A snippet's SQL with its {{names}} filled in. A name without a value stays as written."""
    return VAR_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values and values[m.group(1)] != "" else m.group(0), sql)


# ---- query history ---------------------------------------------------------------------------


def history(ctx: Context, conn: str | None = None, status: str | None = None, source: str | None = None,
            q: str | None = None, days: int = 30, limit: int = 200) -> list[dict]:
    """Queries run through Captain (the app, notebooks, previews and cst sql), newest first."""
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).date().isoformat()
    keys = [k for k in ctx.store.list("metastore/queries/") if k.endswith(".json") and k.split("/")[2] >= cutoff]
    out = []
    for k in sorted(keys, reverse=True):
        try:
            r = json.loads(ctx.store.get(k))
        except Exception:  # noqa: BLE001
            continue
        if conn and r.get("connection") != conn:
            continue
        if status == "ok" and not r.get("ok", True) or status == "failed" and r.get("ok", True):
            continue
        if source and r.get("source") != source:
            continue
        if q and q.lower() not in r.get("sql", "").lower():
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("at", ""), reverse=True)
    return out[:limit]
