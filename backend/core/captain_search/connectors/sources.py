"""Product-analytics sources that aren't SQL databases: PostHog and Google Analytics 4.

They behave like any other connection: the same "Connect a database" form, `run_sql` (so previews, saving as a
dataset, query logging and the read-only rules all apply), and the table browser.
- **PostHog:** queries are HogQL (PostHog's SQL, ClickHouse-flavoured), sent to `/api/projects/<id>/query/` with a
  personal API key. Read-only by nature, and the SQL guard checks them too.
- **Google Analytics 4:** the Data API isn't SQL. A query is a small JSON report spec:
  `{"dimensions": ["searchTerm"], "metrics": ["eventCount"], "start": "28daysAgo", "end": "today",
    "filter": {"dimension": "eventName", "equals": "view_search_results"}, "limit": 1000}`.
  Teams that export GA4 to BigQuery should connect BigQuery instead: the suggestions know the export's tables.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, unquote

import pyarrow as pa

ENGINES = ("posthog", "ga4")


def _result(rows: list[dict], cols: list[str], t0: float, truncated: bool):
    from .sql import Result

    table = pa.Table.from_pylist(rows) if rows else pa.table({c: [] for c in cols})
    return Result(table=table, truncated=truncated, elapsed_ms=int((time.time() - t0) * 1000))


# ---- PostHog -------------------------------------------------------------------------------------


def _posthog(conn):
    from ..workspace import render_secrets

    u = urlparse(render_secrets(conn.url).replace("posthog+", "", 1))
    key = unquote(u.password or "")
    host = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    project = u.path.strip("/")
    if not key or not project:
        raise ValueError("this PostHog connection is missing its project id or API key")
    return host, project, key


def _posthog_call(conn, query: dict) -> dict:
    host, project, key = _posthog(conn)
    req = urllib.request.Request(f"{host}/api/projects/{project}/query/", data=json.dumps({"query": query}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        try:
            detail = json.loads(detail).get("detail") or detail
        except ValueError:
            pass
        if e.code in (401, 403):
            raise PermissionError(f"PostHog refused the API key ({e.code}). It needs the query:read scope.") from None
        raise RuntimeError(f"PostHog said: {detail}") from None


def posthog_run(conn, sql: str, limit: int | None):
    t0 = time.time()
    q = sql.strip().rstrip(";")
    if limit and " limit " not in q.lower():
        q = f"{q}\nLIMIT {int(limit)}"
    out = _posthog_call(conn, {"kind": "HogQLQuery", "query": q})
    cols = out.get("columns") or []
    rows = [dict(zip(cols, r)) for r in out.get("results") or []]
    return _result(rows, cols, t0, bool(limit) and len(rows) >= int(limit))


def posthog_describe(conn) -> list[dict]:
    out = _posthog_call(conn, {"kind": "DatabaseSchemaQuery"})
    tables = []
    for name, t in sorted((out.get("tables") or out).items()):
        if not isinstance(t, dict):
            continue
        fields = t.get("fields") or {}
        tables.append({"name": name, "type": t.get("type", "table"), "comment": None, "columns": [
            {"name": f, "type": str((v or {}).get("type", "")), "nullable": True, "comment": None}
            for f, v in fields.items()]})
    return [{"name": "posthog", "tables": tables}]


# ---- Google Analytics 4 ---------------------------------------------------------------------------


def _ga4(conn):
    u = urlparse(conn.url)
    prop = u.netloc or u.path.strip("/")
    creds = (parse_qs(u.query).get("credentials_path") or [None])[0]
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
    except ImportError:
        raise ModuleNotFoundError("google-analytics-data", name="google.analytics.data_v1beta") from None
    if creds:
        from google.oauth2 import service_account

        client = BetaAnalyticsDataClient(credentials=service_account.Credentials.from_service_account_file(creds))
    else:
        client = BetaAnalyticsDataClient()
    return client, f"properties/{prop}"


def ga4_spec(text: str) -> dict:
    try:
        spec = json.loads(text)
    except ValueError:
        raise ValueError('a Google Analytics query is a JSON report, like {"dimensions": ["searchTerm"], '
                         '"metrics": ["eventCount"], "start": "28daysAgo"}') from None
    if not spec.get("metrics") and not spec.get("dimensions"):
        raise ValueError("name at least one dimension or metric")
    return spec


def ga4_run(conn, text: str, limit: int | None):
    from google.analytics.data_v1beta.types import (DateRange, Dimension, Filter, FilterExpression, Metric,
                                                    RunReportRequest)

    t0 = time.time()
    spec = ga4_spec(text)
    client, prop = _ga4(conn)
    lim = int(min(limit or spec.get("limit") or 1000, spec.get("limit") or 10**6))
    req = RunReportRequest(property=prop, dimensions=[Dimension(name=d) for d in spec.get("dimensions", [])],
                           metrics=[Metric(name=m) for m in spec.get("metrics", [])],
                           date_ranges=[DateRange(start_date=spec.get("start", "28daysAgo"), end_date=spec.get("end", "today"))],
                           limit=lim)
    f = spec.get("filter")
    if f:
        req.dimension_filter = FilterExpression(filter=Filter(
            field_name=f["dimension"], string_filter=Filter.StringFilter(value=str(f["equals"]))))
    res = client.run_report(req)
    cols = [h.name for h in res.dimension_headers] + [h.name for h in res.metric_headers]
    rows = []
    for r in res.rows:
        vals = [v.value for v in r.dimension_values] + [_num(v.value) for v in r.metric_values]
        rows.append(dict(zip(cols, vals)))
    return _result(rows, cols, t0, res.row_count > len(rows))


def _num(v: str):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def ga4_describe(conn) -> list[dict]:
    client, prop = _ga4(conn)
    md = client.get_metadata(name=f"{prop}/metadata")
    cols = [{"name": d.api_name, "type": "dimension", "nullable": True, "comment": d.ui_name} for d in md.dimensions]
    cols += [{"name": m.api_name, "type": "metric", "nullable": True, "comment": m.ui_name} for m in md.metrics]
    return [{"name": "ga4", "tables": [{"name": "report", "type": "report", "comment": "Dimensions and metrics you can "
                                                                                    "ask for in a report", "columns": cols}]}]


def run(conn, sql: str, limit: int | None):
    return posthog_run(conn, sql, limit) if conn.engine == "posthog" else ga4_run(conn, sql, limit)


def describe(conn) -> list[dict]:
    return posthog_describe(conn) if conn.engine == "posthog" else ga4_describe(conn)
