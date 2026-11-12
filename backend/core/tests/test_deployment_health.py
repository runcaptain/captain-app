"""A deployed model's health: what a check measures, when it turns unhealthy, what it records and who it tells."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import server
from captain_search import settings as ST
from captain_search import staging as SG
from captain_search.cli.main import app as cli
from captain_search.staging import health as HL
from captain_search.staging import logs as L

from test_staging import keychain, ws  # noqa: F401  (fixtures)


def _site(ws, n, start=0):  # noqa: F811
    rec = SG.state(ws, "shop")
    return [SG.call(ws, rec, "rank", {"query": ["jacket", "tent", "boots"][i % 3], "k": 5, "session_id": f"s{start + i}", "traffic": "real"}) for i in range(n)]


def _errors(ws, n):  # noqa: F811
    """Error lines in the endpoint's log, as a model that fails on some requests writes them."""
    rec = SG.state(ws, "shop")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    lines = [{"ts": now, "text": "Ranking 'tent' with the candidate failed: boom", "level": "error", "request_id": f"bad{i}"} for i in range(n)]
    L.shared(ws, "shop", "staging").put_new(f"{L.prefix('shop', 'staging', rec['id'])}lines-test-{n}-{len(lines)}.jsonl", "\n".join(json.dumps(x) for x in lines).encode())


@pytest.fixture
def hooks(monkeypatch):
    """A stand-in for the webhook: what would have been posted."""
    sent = []
    monkeypatch.setattr(HL, "send_webhook", lambda ctx, alert: (sent.append(alert), {"sent": True, "status": 200})[1])
    return sent


def test_a_model_that_answers_is_healthy_and_too_few_requests_is_said(ws, hooks):  # noqa: F811
    assert HL.get(ws, "shop")["status"] == "none"
    SG.stage(ws, "shop", target="local", allow_large=True)
    assert HL.get(ws, "shop")["status"] == "unknown" and HL.get(ws, "shop")["text"] == "Not checked yet."
    _site(ws, 3)
    h = HL.check(ws, "shop")
    assert h["status"] == "healthy" and h["answering"] is True and h["requests"] == 3 and h["errors"] == 0 and h["changed"] and not h["alert"]
    assert h["text"] == "Healthy: it is answering. Only 3 requests in the last 15 minutes, too few to judge its error rate or response time."
    _site(ws, 30, start=10)
    h = HL.check(ws, "shop")
    assert h["status"] == "healthy" and h["judged"] and h["p95_ms"] is not None and not h["changed"]
    assert h["text"].startswith("Healthy. 0 of its last 33 requests failed, in the last 15 minutes. 95 in 100 took under ")
    assert SG.state(ws, "shop")["health"]["status"] == "healthy"  # kept on the deployment's record
    assert len(HL.history(ws, "shop")["rows"]) == 2 and HL.history(ws, "shop")["rows"][0]["status"] == "healthy"  # each check is recorded
    assert hooks == []


def test_a_model_that_stops_answering_turns_unhealthy_on_the_second_check_and_says_so(ws, hooks, monkeypatch):  # noqa: F811
    SG.stage(ws, "shop", target="local", allow_large=True)
    assert HL.check(ws, "shop")["status"] == "healthy"
    real = SG.call

    def down(ctx, rec, op, body, *a, **k):  # the stand-in that fails: the endpoint is there but does not answer
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(HL, "call", down)
    one = HL.check(ws, "shop")
    assert one["status"] == "healthy" and one["bad_in_a_row"] == 1 and one["answering"] is False and not one["alert"], "one bad check is not an alert"
    two = HL.check(ws, "shop")
    assert two["status"] == "unhealthy" and two["changed"] and two["was"] == "healthy"
    assert two["text"] == "Unhealthy. It is not answering. Connection refused"
    assert two["alert"]["text"] == f"{ws.candidate.split('/')[-1]} is unhealthy. It is not answering. Connection refused"
    assert two["alert"]["title"] == "Captain: a deployed model is unhealthy" and hooks == [two["alert"]]
    three = HL.check(ws, "shop")
    assert three["status"] == "unhealthy" and not three["changed"] and not three["alert"] and len(hooks) == 1, "said once, not every minute"
    # It comes back: one good check is enough, and that is said too.
    monkeypatch.setattr(HL, "call", real)
    back = HL.check(ws, "shop")
    assert back["status"] == "healthy" and back["alert"]["text"].endswith("is healthy again.") and len(hooks) == 2
    assert [r["status"] for r in HL.history(ws, "shop")["rows"]][:3] == ["healthy", "unhealthy", "unhealthy"]


def test_error_rate_and_response_time_are_judged_against_the_target_the_person_sets(ws, hooks):  # noqa: F811
    SG.stage(ws, "shop", target="local", allow_large=True)
    _site(ws, 40)
    assert HL.get(ws, "shop")["target"] == {"p95_ms": None, "error_rate": 0.05, "set": False}
    _errors(ws, 1)  # 1 of 41: under Captain's default of 5%
    assert HL.check(ws, "shop")["status"] == "healthy"
    t = HL.set_target(ws, "shop", error_rate=0.01)["target"]
    assert t == {"p95_ms": None, "error_rate": 0.01, "set": True}
    HL.check(ws, "shop")
    h = HL.check(ws, "shop")
    assert h["status"] == "unhealthy" and h["reasons"] == ["1 of its last 41 requests failed (2.4%), over the 1% target."], h["reasons"]
    assert hooks[-1]["status"] == "unhealthy"
    # response time: a target no request can meet, then one every request meets
    HL.set_target(ws, "shop", p95_ms=1, error_rate=0.5)
    slow = HL.check(ws, "shop")
    if slow["p95_ms"] > 1:
        assert slow["status"] == "unhealthy" and "over the 1.0 ms target." in slow["reasons"][0] and slow["reasons"][0].startswith("95 in 100 requests took up to ")
    HL.set_target(ws, "shop", p95_ms=60_000, error_rate=0.5)
    assert HL.check(ws, "shop")["status"] == "healthy"
    for bad, why in ((dict(p95_ms=0), "between 1 ms and 60 seconds"), (dict(error_rate=2), "between 0 and 1"), (dict(p95_ms="fast"), "number of milliseconds")):
        with pytest.raises(ValueError, match=why):
            HL.set_target(ws, "shop", **bad)


def test_a_sleeping_host_a_running_deploy_and_a_stopped_model_never_alert(ws, hooks, monkeypatch):  # noqa: F811
    SG.stage(ws, "shop", target="local", allow_large=True)
    HL.check(ws, "shop")
    calls = []
    monkeypatch.setattr(HL, "call", lambda *a, **k: calls.append(a[2]) or {})
    # a host that sleeps, with no copy up: asleep, and nothing is sent to it
    SG._save(ws, "shop", "staging", scale_to_zero=True)
    monkeypatch.setattr(SG, "_awake", lambda ctx, rec: False, raising=False)
    h = HL.check(ws, "shop")
    assert h["status"] == "asleep" and h["text"] == "Asleep. It wakes on the next request." and calls == [] and not h["alert"]
    SG._save(ws, "shop", "staging", scale_to_zero=False)
    # while a deploy runs on the project, a model that does not answer is not an outage
    monkeypatch.setattr(HL, "_deploying", lambda ctx, pid: True)
    monkeypatch.setattr(HL, "call", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("switching")))
    for _ in range(3):
        h = HL.check(ws, "shop")
    assert h["status"] != "unhealthy" and not h["alert"] and hooks == []
    monkeypatch.setattr(HL, "_deploying", lambda ctx, pid: False)
    SG.unstage(ws, "shop")
    h = HL.check(ws, "shop")
    assert h["status"] == "stopped" and h["text"] == "Stopped." and hooks == []


def test_the_webhook_gets_the_message_and_its_address_never_leaves_the_service(ws, monkeypatch):  # noqa: F811
    posted = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        posted.append((req.full_url, json.loads(req.data), req.get_method()))
        return Reply()

    monkeypatch.setattr(HL.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError, match="starts with https://"):
        ST.set_alerts(ws, "ftp://nope")
    ST.set_alerts(ws, "https://hooks.example.invalid/services/T000/B000/secret-part")
    SG.stage(ws, "shop", target="local", allow_large=True)
    HL.check(ws, "shop")
    monkeypatch.setattr(HL, "call", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("Connection refused")))
    HL.check(ws, "shop")
    out = HL.check(ws, "shop")
    assert out["alert"]["webhook"] == {"sent": True, "status": 200}
    url, body, method = posted[0]
    assert method == "POST" and url.endswith("/secret-part") and body["status"] == "unhealthy" and body["project"] == "shop"
    assert body["text"].endswith("is unhealthy. It is not answering. Connection refused") and set(body) == {"text", "project", "model_version", "status", "reasons", "at"}
    # through the app: whether one is set, never the address
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    for path in ("/api/settings", "/api/alerts", "/api/projects/shop/staging/health"):
        r = c.get(path, headers=h)
        assert r.status_code == 200 and "secret-part" not in r.text and "hooks.example" not in r.text, path
    assert c.get("/api/settings", headers=h).json()["alerts"] == {"webhook_set": True}
    bad = c.get("/api/alerts", headers=h).json()
    assert bad["webhook_set"] is True and bad["unhealthy"][0]["project"] == "shop" and bad["unhealthy"][0]["text"].startswith("Unhealthy. It is not answering.")
    assert c.put("/api/settings/alerts", headers=h, json={"webhook_url": ""}).json() == {"webhook_set": False}
    # a webhook that refuses never breaks the check
    ST.set_alerts(ws, "https://hooks.example.invalid/x")
    monkeypatch.setattr(HL.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    assert HL.send_webhook(ws, out["alert"]) == {"sent": False, "why": "The webhook didn't accept it: OSError."}


def test_the_health_addresses_and_the_watch_command(ws, hooks, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    run = lambda *a: CliRunner().invoke(cli, list(a))  # noqa: E731
    assert "Nothing is deployed." in run("deployments", "watch", "--once").output
    SG.stage(ws, "shop", target="local", allow_large=True)
    _site(ws, 25)
    r = c.post("/api/projects/shop/staging/health/check", headers=h).json()
    assert r["status"] == "healthy" and r["changed"] is True
    assert c.put("/api/projects/shop/staging/health/target", headers=h, json={"p95_ms": 200, "error_rate": 0.01}).json()["target"] == {"p95_ms": 200.0, "error_rate": 0.01, "set": True}
    assert c.put("/api/projects/shop/staging/health/target", headers=h, json={"p95_ms": 0}).status_code == 400
    got = c.get("/api/projects/shop/staging/health", headers=h, params={"history": 5}).json()
    assert got["status"] == "healthy" and len(got["history"]) == 1
    out = run("deployments", "health", "shop")
    assert out.exit_code == 0 and "Healthy." in out.output and "Target: 95 in 100 requests under 200 ms, at most 1% failing" in out.output
    assert run("deployments", "watch", "--once").exit_code == 0
    # the stand-in that fails: the watch exits 1 once it is unhealthy, so a cron or an agent can act
    monkeypatch.setattr(HL, "call", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("Connection refused")))
    assert run("deployments", "watch", "--once").exit_code == 0  # the first bad check
    bad = run("deployments", "watch", "--once")
    assert bad.exit_code == 1 and "Unhealthy. It is not answering." in bad.output and "[sent: " in bad.output and len(hooks) == 1
    assert "No webhook is set." in run("deployments", "alerts").output
    on = run("deployments", "alerts", "--webhook", "https://hooks.example.invalid/secret-part")
    assert "A webhook is set." in on.output and "secret-part" not in on.output
