"""Import from real sources: suggested sample queries, previews, PostHog (HogQL over HTTP) and Google Analytics 4."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import duckdb
import pyarrow as pa
import pytest

from captain_search import metastore as MS
from captain_search import suggest as SG
from captain_search.connectors import kinds as K
from captain_search.connectors.guard import WriteRefused
from captain_search.connectors.sql import run_sql
from captain_search.model.entities import Connection
from captain_search.workspace import init_workspace, whoami


def _conn(c, name, kind, fields, secret=None, monkeypatch=None):
    b = K.build(kind, name, fields)
    for s in b["secrets"]:
        monkeypatch.setenv(s, secret)
    c.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name=name, engine=b["engine"], url=b["url"]))
    return c.load_connection(name)


@pytest.fixture
def ctx(tmp_path):
    return init_workspace("t", str(tmp_path / "bucket"))


def test_sql_database_suggestions_and_preview(ctx, tmp_path, monkeypatch):
    db = tmp_path / "logs.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table searches as select 'q' || (i % 7) as query, now() - (i * interval 1 hour) as ts, "
                "case when i % 5 = 0 then 0 else 10 end as hits from range(500) t(i)")
    con.execute("create table products as select i as id, 'item ' || i as title, 'cat' || (i % 3) as category from range(50) t(i)")
    con.close()
    _conn(ctx, "logs", "duckdb", {"path": str(db)}, monkeypatch=monkeypatch)
    assert SG.suggestions(ctx, "logs")["pulled"] is False
    MS.pull(ctx, "logs")
    s = SG.suggestions(ctx, "logs")["suggestions"]
    titles = [x["title"] for x in s]
    assert any(t.startswith("Top searches, last 7 days") for t in titles)
    assert any(t.startswith("Searches that returned nothing") for t in titles)
    assert any(t.startswith("1,000 products") for t in titles) and any(t.startswith("Products per category") for t in titles)
    for x in s:  # every suggestion runs and is bounded
        p = SG.preview(ctx, "logs", x["query"], 1000)
        assert p["row_count"] <= 1000, x["title"]
    top = SG.preview(ctx, "logs", next(x for x in s if x["title"].startswith("Top searches"))["query"])
    assert top["columns"][0]["name"] == "query" and top["row_count"] == 7
    zero = SG.preview(ctx, "logs", next(x for x in s if x["title"].startswith("Searches that returned"))["query"])
    assert all(r[1] > 0 for r in zero["rows"]) and zero["row_count"] >= 1


class _FakePostHog(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakePostHog.seen.append((self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != "Bearer phx_test":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"detail": "Invalid key"}')
            return
        q = body["query"]
        if q["kind"] == "DatabaseSchemaQuery":
            out = {"tables": {"events": {"type": "posthog", "fields": {"event": {"type": "string"},
                                                                         "timestamp": {"type": "datetime"}}}}}
        else:
            out = {"columns": ["event", "n"], "results": [["$pageview", 120], ["search_performed", 30]]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_posthog_end_to_end(ctx, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _FakePostHog)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host = f"http://127.0.0.1:{srv.server_port}"
        conn = _conn(ctx, "ph", "posthog", {"host": host, "project_id": "42"}, "phx_test", monkeypatch)
        assert "phx_test" not in conn.url  # the key is only a reference
        s = SG.suggestions(ctx, "ph")["suggestions"]
        assert s[0]["title"] == "Top events, last 7 days"
        r = run_sql(conn, s[0]["query"], limit=100)
        assert r.table.column_names == ["event", "n"] and r.table.num_rows == 2
        path, auth, body = _FakePostHog.seen[-1]
        assert path == "/api/projects/42/query/" and body["query"]["kind"] == "HogQLQuery"
        with pytest.raises(WriteRefused):
            run_sql(conn, "insert into events values (1)")
        MS.pull(ctx, "ph")
        assert MS.schemas(ctx, "ph")["schemas"][0]["tables"][0]["name"] == "events"
        monkeypatch.setenv("PH_API_KEY", "wrong")
        with pytest.raises(PermissionError, match="refused the API key"):
            run_sql(ctx.load_connection("ph"), "select 1", limit=1)
    finally:
        srv.shutdown()


def test_ga4_reports(ctx, monkeypatch):
    from captain_search.connectors import sources as SRC

    class FakeClient:
        def run_report(self, req):
            assert req.property == "properties/123" and [d.name for d in req.dimensions] == ["searchTerm"]
            assert req.dimension_filter.filter.field_name == "eventName"

            class H:
                def __init__(self, n):
                    self.name = n

            class V:
                def __init__(self, v):
                    self.value = v

            class Row:
                def __init__(self, d, m):
                    self.dimension_values, self.metric_values = [V(d)], [V(m)]

            class R:
                dimension_headers, metric_headers = [H("searchTerm")], [H("eventCount")]
                rows = [Row("tent", "40"), Row("boots", "12")]
                row_count = 2

            return R()

    monkeypatch.setattr(SRC, "_ga4", lambda conn: (FakeClient(), "properties/123"))
    conn = _conn(ctx, "ga", "ga4", {"property_id": "123"}, monkeypatch=monkeypatch)
    s = SG.suggestions(ctx, "ga")["suggestions"]
    assert s[0]["title"].startswith("Site search terms")
    r = run_sql(conn, s[0]["query"], limit=1000)
    assert r.table.to_pylist() == [{"searchTerm": "tent", "eventCount": 40}, {"searchTerm": "boots", "eventCount": 12}]
    with pytest.raises(ValueError, match="JSON report"):
        run_sql(conn, "select * from events", limit=5)
