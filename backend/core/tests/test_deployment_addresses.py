"""The deployment page, tab by tab, through the addresses the page itself calls.

The Copies tab once broke while every test passed: two tests each called their own function, and none called the
address the page uses, which another route had taken. Each test here goes through the app, on the address written
in the page's code, and checks the fields that tab reads."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from captain_search import server
from captain_search import staging as SG

from test_staging import keychain, ws  # noqa: F401  (fixtures)

# Whether every address the page's code names is served, and whether any is registered twice, is checked for all
# screens at once in test_screen_addresses.py.
P = "/api/projects/shop"


@pytest.fixture
def page(ws, monkeypatch):  # noqa: F811
    """A model staged on this computer with a few requests, and a client on the app as the page has one."""
    monkeypatch.setattr(server, "_ctx", None)
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    SG.stage(ws, "shop", target="local", allow_large=True)
    rec = SG.state(ws, "shop")
    got = [SG.call(ws, rec, "rank", {"query": q, "k": 5, "session_id": f"s{i}", "traffic": "real"}) for i, q in enumerate(["jacket", "tent", "boots"])]
    SG.call(ws, rec, "outcome", {"request_id": got[0]["request_id"], "event": "click", "product_id": got[0]["ids"][0], "client_ms": 111})
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}

    def get(path, **params):
        r = c.get(path, headers=h, params=params)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
        return r.json()

    get.put = lambda path, body: c.put(path, headers=h, json=body)
    get.first = got[0]["request_id"]
    return get


def has(d: dict, *keys):
    missing = [k for k in keys if k not in d]
    assert not missing, f"missing {missing} in {sorted(d)}"


def test_header_status_live_card_and_chart(page):
    s = page(f"{P}/staging")
    has(s, "status", "staged", "health", "text")
    has(s["staged"], "id", "kind", "model_version", "endpoint", "arms", "serve", "idle_minutes")
    has(s["health"], "uptime_s", "stops_in_s", "running", "idle_s")
    lv = page(f"{P}/staging/live")
    has(lv, "state", "label", "line", "copies", "running")
    assert lv["state"] in ("handling", "idle") and lv["copies"] == 1
    m = page(f"{P}/staging/metrics", range="1h")
    has(m, "buckets", "markers", "summary", "text", "start", "range_text")
    assert len(m["buckets"]) == 30 and sum(b["requests"] for b in m["buckets"]) == 3
    has(m["buckets"][0], "requests", "errors", "p50", "p95", "site_p50", "site_p95")
    has(m["markers"][0], "slot", "text", "at")


def test_requests_tab_and_one_request_beside_it(page):
    d = page(f"{P}/staging/requests", limit=50)
    has(d, "rows", "total", "text")
    assert d["total"] == 3
    has(d["rows"][0], "request_id", "ts", "query", "arm", "ms", "traffic")
    one = page(f"{P}/staging/requests/{page.first}")
    has(one, "request_id", "query", "arm", "model_version", "ms", "traffic", "results", "shadow", "lines", "time_text", "site_ms")
    has(one["results"][0], "position", "title", "signals", "events")
    assert one["site_ms"] == 111 and [x["text"].split(" ")[0] for x in one["lines"]] == ["Ranked", "Outcome:"]


def test_try_it_and_traffic_tabs(page):
    k = page(f"{P}/staging/connect")
    has(k, "address", "text", "live", "real", "unlabelled", "test", "simulated", "running", "snippets", "connected", "evidence_label", "test_request")
    assert k["real"] == 3 and k["connected"]
    has(page(f"{P}/ab"), "status", "can_measure")
    has(page(f"{P}/ab/options"), "ready", "shares", "text")
    has(page(f"{P}/shadow"), "status")
    has(page(f"{P}/staging/differ"), "queries")


def test_copies_tab(page):
    d = page(f"{P}/staging/copies")
    has(d, "rows", "text")
    assert len(d["rows"]) == 1, d  # the copy itself, not another route's answer on the same address
    has(d["rows"][0], "copy", "status", "state", "text", "where", "machine", "model_version", "created_at", "started_at",
        "ready_s", "requests", "errors", "memory_mb", "can_stop")
    assert d["rows"][0]["requests"] == 3


def test_metrics_tab(page):
    m = page(f"{P}/staging/metrics", range="15m")
    has(m["summary"], "requests", "errors", "error_rate", "p50", "p95", "site_p50", "site_p95", "site_reported", "copies",
        "copies_min", "copies_max", "memory_mb", "waiting", "endpoint_text", "site_text")
    assert m["summary"]["site_reported"] == 1 and m["summary"]["site_p50"] == 111


def test_versions_tab(page):
    d = page(f"{P}/staging/versions")
    has(d, "rows", "text")
    has(d["rows"][0], "n", "model_version", "live", "at", "by", "can_roll_back")
    assert d["rows"][0]["live"]


def test_logs_tab_and_its_filter(page):
    d = page(f"{P}/staging/logs", limit=300)
    has(d, "rows", "total", "streaming", "text", "note")
    has(d["rows"][0], "ts", "text", "level")
    assert d["streaming"] and d["rows"][0]["text"].startswith("Ready in ")
    only = page(f"{P}/staging/logs", limit=300, q="tent")
    assert only["matching"] == 1 and "tent" in only["rows"][0]["text"]


def test_settings_tab_and_changing_the_idle_time(page):
    d = page(f"{P}/staging/settings")
    has(d, "groups", "text", "idle_minutes", "idle_choices", "can_edit")
    has(d["groups"][0], "title", "rows")
    assert d["can_edit"] and any(g.get("edit") == "idle" for g in d["groups"])
    r = page.put(f"{P}/staging/settings", {"idle_minutes": 60})
    assert r.status_code == 200 and r.json()["idle_minutes"] == 60 and r.json()["saved"].startswith("It now stops after 1 h")
    assert page.put(f"{P}/staging/settings", {"idle_minutes": 1}).status_code == 400


def test_cost_tab(page):
    d = page(f"{P}/staging/cost")
    has(d, "free", "text", "rows")
    assert d["free"] and d["text"] == "Runs on this computer. No cloud cost."


def test_the_list_of_staged_models_and_the_deploy_history_each_answer_on_their_own_address(page):
    d = page("/api/staging/deployments", which="live")
    has(d, "rows", "text", "which")
    has(d["rows"][0], "project", "name", "status", "state", "model_version", "where", "since_text", "series", "errors", "line", "requests_24h")
    assert page("/api/staging/deployments", which="stopped")["rows"] == []
    hist = page("/api/deployments")
    assert isinstance(hist, list) and hist[0]["pipeline"] == SG.state(server.ctx(), "shop")["model_version"]  # the deploy history
