"""Suggested sample queries for a connection, before anyone writes SQL.

After a connection's tables are read (metastore.pull), each table is recognized by its columns:
- **search log:** a query column and a time column. Suggests top searches over the last 7 days, searches that returned
  nothing (when there's a hits or results column), and searches per day.
- **catalog:** an id column and a title or name column. Suggests a sample of products and a count by category.
- **event stream:** a time column and an event or type column. Suggests events per day and a recent sample.
- **Google Analytics 4's BigQuery export** (`events_*` tables with event_name and event_params): site search terms.
- anything else: a small sample.

PostHog and Google Analytics 4 get their own: top events and search-like events (HogQL), and site-search and page
reports (GA4).

Every suggestion is bounded: a LIMIT, a recent time window, or both. That makes a preview a portion of the database,
never all of it. Each has a title, why it's useful, the query, and the kind of dataset it would make.
"""
from __future__ import annotations

import json

from .model.entities import DatasetKind

QUERY = ("query", "query_text", "search_term", "searchterm", "search_query", "q", "keyword", "term")
TIME = ("timestamp", "ts", "event_time", "created_at", "time", "searched_at", "event_timestamp", "occurred_at", "date")
HITS = ("hits", "num_results", "result_count", "results", "nb_hits", "total_hits", "n_results")
ID = ("id", "product_id", "sku", "item_id", "objectid", "doc_id")
TITLE = ("title", "name", "product_name")
EVENT = ("event", "event_name", "event_type", "type", "action")
CATEGORY = ("category", "category_name", "department", "product_type")


def _pick(cols: dict[str, str], names) -> str | None:
    low = {c.lower(): c for c in cols}
    return next((low[n] for n in names if n in low), None)


def _q(dialect_prep, *parts) -> str:
    return ".".join(dialect_prep(p) for p in parts if p)


def _since(engine: str, col: str, days: int) -> str:
    return {
        "bigquery": f"{col} >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)",
        "snowflake": f"{col} >= DATEADD(day, -{days}, CURRENT_TIMESTAMP())",
        "databricks": f"{col} >= current_timestamp() - INTERVAL {days} DAYS",
        "mysql": f"{col} >= NOW() - INTERVAL {days} DAY",
        "clickhouse": f"{col} >= now() - INTERVAL {days} DAY",
        "sqlserver": f"{col} >= DATEADD(day, -{days}, SYSDATETIME())",
        "trino": f"{col} >= current_timestamp - INTERVAL '{days}' DAY",
        "athena": f"{col} >= current_timestamp - INTERVAL '{days}' DAY",
        "sqlite": f"{col} >= datetime('now', '-{days} days')",
    }.get(engine, f"{col} >= CURRENT_TIMESTAMP - INTERVAL '{days} days'")


def _day(engine: str, col: str) -> str:
    return {"bigquery": f"DATE({col})", "clickhouse": f"toDate({col})", "mysql": f"DATE({col})",
            "sqlserver": f"CAST({col} AS date)", "sqlite": f"date({col})"}.get(engine, f"CAST({col} AS DATE)")


def _limit(engine: str, sql: str, n: int) -> str:
    if engine == "sqlserver":
        return sql.replace("SELECT ", f"SELECT TOP {n} ", 1)
    return f"{sql}\nLIMIT {n}"


def suggestions(ctx, conn_name: str, per_table: int = 3, max_total: int = 24) -> dict:
    from . import metastore as MS

    conn = ctx.load_connection(conn_name)
    if conn.engine == "posthog":
        return {"connection": conn_name, "pulled": True, "suggestions": _posthog()}
    if conn.engine == "ga4":
        return {"connection": conn_name, "pulled": True, "suggestions": _ga4()}
    doc = MS.cached(ctx, conn_name)
    if doc is None:
        return {"connection": conn_name, "pulled": False, "suggestions": [],
                "reason": "Captain hasn't read this database's tables yet. Read them, then the suggestions appear."}
    from sqlalchemy.engine import make_url

    from .workspace import render_secrets

    try:
        prep = make_url(render_secrets(conn.url)).get_dialect()().identifier_preparer.quote
    except Exception:  # noqa: BLE001
        prep = lambda x: f'"{x}"'
    eng = conn.engine
    out = []
    for s in doc["schemas"]:
        for t in s["tables"]:
            cols = {c["name"]: c["type"] for c in t["columns"]}
            if not cols:
                continue
            parts = [*MS._parts(s["name"]), t["name"]] if hasattr(MS, "_parts") else [*s["name"].split("."), t["name"]]
            tbl = _q(prep, *parts)
            label = f"{s['name'].split('.')[-1]}.{t['name']}"
            out += _for_table(eng, tbl, label, cols, prep)[:per_table]
    out.sort(key=lambda x: x["rank"])
    for i, x in enumerate(out):
        x.pop("rank")
        x["id"] = f"s{i + 1}"
    return {"connection": conn_name, "pulled": True, "pulled_at": doc["pulled_at"], "suggestions": out[:max_total]}


def _for_table(eng, tbl, label, cols, prep) -> list[dict]:
    qc, tc, hc = _pick(cols, QUERY), _pick(cols, TIME), _pick(cols, HITS)
    idc, title, ev, cat = _pick(cols, ID), _pick(cols, TITLE), _pick(cols, EVENT), _pick(cols, CATEGORY)
    s = []
    lowcols = {c.lower() for c in cols}
    if label.split(".")[-1].startswith("events_") and {"event_name", "event_params"} <= lowcols:
        s.append({"rank": 0, "title": f"Site search terms from Google Analytics ({label})",
                  "why": "GA4's BigQuery export: what people typed into your site search, last 7 days.",
                  "kind": DatasetKind.queries.value, "query": _limit(eng, (
                      f"SELECT (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'search_term') AS query,\n"
                      f"       COUNT(*) AS searches\nFROM {tbl}\nWHERE event_name = 'view_search_results'\n"
                      f"  AND _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY))\n"
                      f"GROUP BY 1\nORDER BY searches DESC"), 1000)})
    if qc and tc:
        q, t = prep(qc), prep(tc)
        s.append({"rank": 1, "title": f"Top searches, last 7 days ({label})",
                  "why": "The queries that matter most by traffic: where to start judging relevance.",
                  "kind": DatasetKind.queries.value, "query": _limit(eng, (
                      f"SELECT {q} AS query, COUNT(*) AS searches\nFROM {tbl}\nWHERE {_since(eng, t, 7)}\n"
                      f"GROUP BY {q}\nORDER BY searches DESC"), 1000)})
        if hc:
            s.append({"rank": 1, "title": f"Searches that returned nothing, last 7 days ({label})",
                      "why": "Zero-result queries are the clearest failures to fix first.",
                      "kind": DatasetKind.queries.value, "query": _limit(eng, (
                          f"SELECT {q} AS query, COUNT(*) AS searches\nFROM {tbl}\n"
                          f"WHERE {prep(hc)} = 0 AND {_since(eng, t, 7)}\nGROUP BY {q}\nORDER BY searches DESC"), 500)})
        s.append({"rank": 3, "title": f"Searches per day, last 30 days ({label})",
                  "why": "Traffic shape, and whether logging has gaps.", "kind": DatasetKind.generic.value,
                  "query": (f"SELECT {_day(eng, t)} AS day, COUNT(*) AS searches\nFROM {tbl}\n"
                            f"WHERE {_since(eng, t, 30)}\nGROUP BY 1\nORDER BY 1")})
    elif idc and title:
        s.append({"rank": 2, "title": f"1,000 products ({label})",
                  "why": "A sample of the catalog to check titles, attributes and coverage.",
                  "kind": DatasetKind.catalog.value, "query": _limit(eng, f"SELECT *\nFROM {tbl}", 1000)})
        if cat:
            s.append({"rank": 3, "title": f"Products per {cat} ({label})", "why": "How the catalog is spread.",
                      "kind": DatasetKind.generic.value, "query": _limit(eng, (
                          f"SELECT {prep(cat)} AS category, COUNT(*) AS products\nFROM {tbl}\n"
                          f"GROUP BY 1\nORDER BY products DESC"), 200)})
    elif tc and ev:
        t, e = prep(tc), prep(ev)
        s.append({"rank": 2, "title": f"Events per day by type, last 14 days ({label})",
                  "why": "Which interactions are logged and how often.", "kind": DatasetKind.generic.value,
                  "query": (f"SELECT {_day(eng, t)} AS day, {e} AS event, COUNT(*) AS n\nFROM {tbl}\n"
                            f"WHERE {_since(eng, t, 14)}\nGROUP BY 1, 2\nORDER BY 1, n DESC")})
        s.append({"rank": 3, "title": f"1,000 recent events ({label})", "why": "What one event row looks like.",
                  "kind": DatasetKind.events.value, "query": _limit(eng, (
                      f"SELECT *\nFROM {tbl}\nWHERE {_since(eng, t, 1)}\nORDER BY {t} DESC"), 1000)})
    if not s:
        s.append({"rank": 5, "title": f"100 rows of {label}", "why": "A first look at the table.",
                  "kind": DatasetKind.generic.value, "query": _limit(eng, f"SELECT *\nFROM {tbl}", 100)})
    return s


def _posthog() -> list[dict]:
    rows = [
        ("Top events, last 7 days", "What PostHog records for your product, by volume.", "generic",
         "SELECT event, count() AS n\nFROM events\nWHERE timestamp > now() - INTERVAL 7 DAY\nGROUP BY event\nORDER BY n DESC\nLIMIT 100"),
        ("Search-like events, last 7 days", "Events whose name mentions search: a place to find search logging.", "events",
         "SELECT event, count() AS n\nFROM events\nWHERE timestamp > now() - INTERVAL 7 DAY AND event ILIKE '%search%'\n"
         "GROUP BY event\nORDER BY n DESC\nLIMIT 50"),
        ("Search terms from page URLs, last 7 days", "Many sites put the query in the URL, as ?q= or ?query=.", "queries",
         "SELECT extract(properties.$current_url, '[?&](?:q|query|search|s)=([^&#]+)') AS query, count() AS searches\n"
         "FROM events\nWHERE event = '$pageview' AND timestamp > now() - INTERVAL 7 DAY\n"
         "  AND match(properties.$current_url, '[?&](q|query|search|s)=')\nGROUP BY query\nORDER BY searches DESC\nLIMIT 1000"),
        ("1,000 recent events", "What one event looks like, with its properties.", "events",
         "SELECT timestamp, event, distinct_id, properties\nFROM events\nWHERE timestamp > now() - INTERVAL 1 DAY\n"
         "ORDER BY timestamp DESC\nLIMIT 1000"),
    ]
    return [{"id": f"s{i + 1}", "title": t, "why": w, "kind": k, "query": q} for i, (t, w, k, q) in enumerate(rows)]


def _ga4() -> list[dict]:
    rows = [
        ("Site search terms, last 28 days", "What people typed into your site search (needs enhanced measurement).",
         "queries", {"dimensions": ["searchTerm"], "metrics": ["eventCount"], "start": "28daysAgo", "end": "today",
                     "filter": {"dimension": "eventName", "equals": "view_search_results"}, "limit": 1000}),
        ("Searches per day, last 28 days", "Search traffic over time.", "generic",
         {"dimensions": ["date"], "metrics": ["eventCount"], "start": "28daysAgo", "end": "today",
          "filter": {"dimension": "eventName", "equals": "view_search_results"}, "limit": 100}),
        ("Top pages, last 7 days", "Where visitors go, with views and users.", "generic",
         {"dimensions": ["pagePath"], "metrics": ["screenPageViews", "totalUsers"], "start": "7daysAgo", "end": "today",
          "limit": 500}),
        ("Items viewed and bought, last 28 days", "Ecommerce items with views and purchases (needs ecommerce events).",
         "generic", {"dimensions": ["itemName"], "metrics": ["itemsViewed", "itemsPurchased"], "start": "28daysAgo",
                     "end": "today", "limit": 1000}),
    ]
    return [{"id": f"s{i + 1}", "title": t, "why": w, "kind": k, "query": json.dumps(q, indent=2)}
            for i, (t, w, k, q) in enumerate(rows)]


def preview(ctx, conn_name: str, query: str, limit: int = 1000) -> dict:
    """Run a query with a hard cap (at most 1,000 rows) and return them without saving anything."""
    from . import metastore as MS
    from .connectors.sql import run_sql
    from .server import _jsonable

    conn = ctx.load_connection(conn_name)
    n = max(1, min(int(limit), 1000))
    r = run_sql(conn, query, limit=n)
    MS.record_query(ctx, conn_name, query, rows=r.table.num_rows, ms=r.elapsed_ms, source="preview")
    return {"columns": [{"name": f.name, "type": str(f.type)} for f in r.table.schema],
            "rows": [list(map(_jsonable, row.values())) for row in r.table.to_pylist()],
            "row_count": r.table.num_rows, "limited": r.truncated or r.table.num_rows >= n, "ms": r.elapsed_ms}

