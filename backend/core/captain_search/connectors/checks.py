"""A connection test that proves more than `select 1`: the login works and the place it points at exists.

`select 1` succeeds on Databricks even when the connection's catalog doesn't exist, so a connection could test fine
and then fail on every real query ("Catalog 'main' was not found"). verify() also opens the configured catalog and
schema, and when one is missing it says which ones the login can see.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from ..model.entities import Connection


def _plain(e: Exception) -> str:
    s = re.sub(r"dapi[0-9a-f]{8,}", "dapi***", str(e))  # never echo a Databricks token
    m = re.search(r"\[(NO_SUCH_CATALOG_EXCEPTION|SCHEMA_NOT_FOUND|TABLE_OR_VIEW_NOT_FOUND)\]\s*([^\n]+?)(?: SQLSTATE|$)", s)
    if m:
        return m.group(2).strip()
    if "Invalid access token" in s or "403" in s and "databricks" in s.lower():
        return "Databricks refused the access token. Check it hasn't expired and has the sql scope."
    return s.splitlines()[0][:300] if s else type(e).__name__


def verify(conn: Connection) -> dict:
    from .sql import run_sql

    try:
        r = run_sql(conn, "select 1 as ok", limit=1)
    except ModuleNotFoundError as e:
        return {"ok": False, "error": f"this database's driver isn't installed ({e.name})"}
    except KeyError as e:
        return {"ok": False, "error": str(e).strip("'\"")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _plain(e)}
    out = {"ok": True, "ms": r.elapsed_ms}
    if conn.engine == "databricks":
        q = parse_qs(urlparse(conn.url).query)
        catalog, schema = (q.get("catalog") or [None])[0], (q.get("schema") or [None])[0]
        try:
            cats = [x["catalog"] for x in run_sql(conn, "SHOW CATALOGS", limit=1000).table.to_pylist()]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _plain(e)}
        out["catalogs"] = cats
        if catalog and catalog not in cats:
            return {"ok": False, "catalogs": cats,
                    "error": f"The catalog {catalog!r} doesn't exist for this login. It can see: {', '.join(cats)}. "
                             "Change the catalog, or leave it empty to browse them all."}
        if catalog and schema:
            try:
                schemas = [x.get("databaseName") or x.get("namespace") or next(iter(x.values()))
                           for x in run_sql(conn, f"SHOW SCHEMAS IN `{catalog}`", limit=10000).table.to_pylist()]
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": _plain(e)}
            if schema not in schemas:
                return {"ok": False, "error": f"The schema {schema!r} isn't in {catalog}. It has: "
                                              f"{', '.join(schemas[:20])}{' and more' if len(schemas) > 20 else ''}."}
    return out
