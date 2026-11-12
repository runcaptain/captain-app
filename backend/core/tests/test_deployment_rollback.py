"""Roll back that is real: an earlier version goes back through the same one-job deploy, what runs keeps answering
during the switch, who did it and why is kept, and Captain can do it by itself when a new version turns unhealthy
in its first minutes. On this computer's host, and on a stand-in for a cloud."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import project_state as PS
from captain_search import server
from captain_search import staging as SG
from captain_search.cli.main import app
from captain_search.staging import deploy as DP
from captain_search.staging import health as HL
from captain_search.staging import rollback as RB

from test_deploy_one_call import logins, modal  # noqa: F401  (fixtures)
from test_staging import keychain, ws  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def only_the_five_steps(monkeypatch):
    monkeypatch.setattr(DP, "AFTER", [])


def _answers(ws):
    """True when the deployed model answers a request with products."""
    r = SG.query(ws, "shop", "jacket", k=3)
    return bool(((r.get("staged") or {}).get("results")))


def _two_versions(ws, host="local", login=None):
    """The compared model first, then this project's model: two versions deployed to one place."""
    assert DP.run(ws, "shop", host, login, baseline=True, confirm=True)["status"] == "done"
    first = SG.state(ws, "shop")["model_version"]
    assert DP.run(ws, "shop", host, login, confirm=True)["status"] == "done"
    second = SG.state(ws, "shop")["model_version"]
    assert first != second
    return first, second


def test_roll_back_on_this_computer_goes_through_the_deploy_and_records_who_and_why(ws):  # noqa: F811
    first, second = _two_versions(ws)
    assert [r["model_version"] for r in RB.earlier(ws, "shop")] == [first]
    with pytest.raises(ValueError, match="Say why"):
        RB.go(ws, "shop")
    with pytest.raises(ValueError, match="no earlier version 99"):
        RB.go(ws, "shop", 99, "a test")
    seen = []
    out = RB.go(ws, "shop", None, "It gives  worse results", on_event=lambda kind, **d: seen.append((kind, d)))
    assert out["rolled_back_to"] == first and out["from"] == second and out["reason"] == "It gives worse results" and not out["auto"]
    # the same job every deploy is: the same named steps, and it ends answering
    assert [d["title"] for k, d in seen if k == "step"] == ["Checking your account", "Starting the model", "Checking it answers"]
    assert out["deploy"]["status"] == "done" and DP.status(ws, "shop")["status"] == "done"
    rec = SG.state(ws, "shop")
    assert rec["status"] == "running" and rec["model_version"] == first
    assert _answers(ws), "the earlier version answers"
    h = RB.history(ws, "shop")
    assert h["text"] == "1 roll back so far." and len(h["rows"]) == 1
    row = h["rows"][0]
    assert (row["from"], row["to"], row["reason"], row["auto"]) == (second, first, "It gives worse results", False) and row["by"] and row["at"]
    assert any("rolled the deployment back" in x["what"] and "It gives worse results" in x["what"] for x in PS.get(ws, "shop")["history"])
    # and forward again: the version it left is now the earlier one, and the record of the first roll back stays
    again = RB.go(ws, "shop", None, "The fix is in")
    assert again["rolled_back_to"] == second and [r["reason"] for r in RB.history(ws, "shop")["rows"]] == ["The fix is in", "It gives worse results"]


def test_requests_keep_being_answered_while_it_switches(ws):  # noqa: F811
    _two_versions(ws)
    done, failures, answered = threading.Event(), [], [0]

    def shopper():
        while not done.is_set():
            try:
                if _answers(ws):
                    answered[0] += 1
            except Exception as e:  # noqa: BLE001
                failures.append(str(e)[:160])
            time.sleep(0.02)

    t = threading.Thread(target=shopper, daemon=True)
    t.start()
    time.sleep(0.3)
    before = answered[0]
    RB.go(ws, "shop", None, "It is too slow")
    time.sleep(0.3)
    done.set()
    t.join(5)
    assert before > 0 and answered[0] > before, "requests were answered before, during and after"
    assert failures == [], f"no request failed during the switch: {failures[:3]}"


def test_an_earlier_version_that_fails_to_start_leaves_what_was_running(ws, monkeypatch):  # noqa: F811
    first, second = _two_versions(ws)
    real = SG._start_local

    def broken(*a, **k):
        raise RuntimeError("the earlier version would not start")

    monkeypatch.setattr(SG, "_start_local", broken)
    with pytest.raises(ValueError, match="is still running"):
        RB.go(ws, "shop", None, "It is failing requests")
    monkeypatch.setattr(SG, "_start_local", real)
    rec = SG.state(ws, "shop")
    assert rec["status"] == "running" and rec["model_version"] == second
    assert _answers(ws), "what was running still answers"
    assert RB.history(ws, "shop")["rows"] == [], "a roll back that did not happen is not recorded as one"


def test_roll_back_on_a_cloud_stand_in_deploys_to_the_same_place_under_the_same_login(ws, logins, modal):  # noqa: F811
    logins.append({"name": "modal-test", "provider": "modal", "auth": "token", "fields": {}, "last_test": {"ok": True}})
    first, second = _two_versions(ws, "modal", "modal-test")
    target = SG.state(ws, "shop")["target"]
    out = RB.go(ws, "shop", None, "Deployed by mistake")
    rec = SG.state(ws, "shop")
    assert out["rolled_back_to"] == first and rec["model_version"] == first and rec["status"] == "running"
    assert (rec["host"], rec["login"], rec["target"]) == ("modal", "modal-test", target), "the same place, the same login"
    assert [s["id"] for s in out["deploy"]["steps"]] == list(DP.STEPS)


def test_what_a_person_set_outlives_a_redeploy(ws):  # noqa: F811
    assert DP.run(ws, "shop", "local", baseline=True)["status"] == "done"
    HL.set_target(ws, "shop", p95_ms=200, error_rate=0.01)
    assert RB.set_auto(ws, "shop", 30)["on"]
    assert DP.run(ws, "shop", "local")["status"] == "done"
    rec = SG.state(ws, "shop")
    assert rec["health_target"]["p95_ms"] == 200 and rec["auto_rollback"]["minutes"] == 30


def _unhealthy_twice(ws, monkeypatch):
    """Two checks in a row that find the model not answering: what turns a deployment unhealthy."""
    with monkeypatch.context() as m:
        m.setattr(HL, "measure", lambda *a, **k: {"answering": False, "requests": 0, "errors": 0, "error_rate": None, "p50_ms": None,
                                                  "p95_ms": None, "last_request_at": None, "fallbacks": 0, "state": "up", "window_minutes": 15})
        HL.check(ws, "shop", notify=False)
        return HL.check(ws, "shop", notify=False)


def test_automatic_roll_back_puts_the_previous_version_back_and_says_so(ws, monkeypatch):  # noqa: F811
    first, second = _two_versions(ws)
    assert not RB.auto(ws, "shop")["on"]
    with pytest.raises(ValueError, match="between 5 minutes and a day"):
        RB.set_auto(ws, "shop", 2)
    assert RB.set_auto(ws, "shop", 30)["text"].startswith("On: if a new version is unhealthy in its first 30 minutes")
    r = _unhealthy_twice(ws, monkeypatch)
    back = r["alert"]["rolled_back"]
    assert back["auto"] and back["by"] == "Captain" and back["rolled_back_to"] == first and back["from"] == second
    assert "Captain put" in r["alert"]["text"] and "It turned unhealthy" in back["reason"] and "not answering" in back["reason"].lower()
    rec = SG.state(ws, "shop")
    assert rec["model_version"] == first and rec["status"] == "running"
    row = RB.history(ws, "shop")["rows"][0]
    assert row["auto"] and row["by"] == "Captain"
    # The version it put back is not rolled back again by itself: there is nowhere further to go.
    r2 = _unhealthy_twice(ws, monkeypatch)
    assert SG.state(ws, "shop")["model_version"] == first and len(RB.history(ws, "shop")["rows"]) == 1
    assert not (r2.get("alert") or {}).get("rolled_back")


def test_automatic_roll_back_stays_out_when_off_when_too_late_and_with_nothing_earlier(ws, monkeypatch):  # noqa: F811
    # nothing earlier: the first version deployed here
    assert DP.run(ws, "shop", "local", baseline=True)["status"] == "done"
    RB.set_auto(ws, "shop", 30)
    r = _unhealthy_twice(ws, monkeypatch)
    assert r["status"] == "unhealthy" and "rolled_back" not in r["alert"]
    # off: unhealthy, and it stays up
    assert DP.run(ws, "shop", "local")["status"] == "done"
    second = SG.state(ws, "shop")["model_version"]
    RB.set_auto(ws, "shop", 0)
    r = _unhealthy_twice(ws, monkeypatch)
    assert r["status"] == "unhealthy" and "rolled_back" not in r["alert"] and SG.state(ws, "shop")["model_version"] == second
    # on, but the version has been up longer than the minutes asked for
    RB.set_auto(ws, "shop", 5)
    SG._save(ws, "shop", "staging", started_at="2020-01-01T00:00:00+00:00", health=None)
    r = _unhealthy_twice(ws, monkeypatch)
    assert r["status"] == "unhealthy" and "rolled_back" not in r["alert"] and SG.state(ws, "shop")["model_version"] == second


def test_the_command_and_the_addresses(ws, monkeypatch):  # noqa: F811
    first, second = _two_versions(ws)
    run = CliRunner()
    r = run.invoke(app, ["deployments", "rollback", "shop"])
    assert r.exit_code != 0 and "Say why" in r.output
    r = run.invoke(app, ["deployments", "rollback", "shop", "--reason", "It is too slow"])
    assert r.exit_code == 0 and "Rolled back to version" in r.output and "It is too slow" in r.output, r.output
    assert SG.state(ws, "shop")["model_version"] == first
    r = run.invoke(app, ["deployments", "rollback", "shop", "--history"])
    assert "1 roll back so far." in r.output and "It is too slow" in r.output and "Off:" in r.output
    r = run.invoke(app, ["deployments", "rollback", "shop", "--auto", "30"])
    assert r.exit_code == 0 and "On: if a new version is unhealthy in its first 30 minutes" in r.output

    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    d = c.get("/api/projects/shop/staging/rollbacks", headers=hd).json()
    assert d["rows"][0]["reason"] == "It is too slow" and d["auto"] == {"minutes": 30.0, "on": True, "text": d["auto"]["text"]}
    n = RB.earlier(ws, "shop")[0]["n"]
    assert c.post(f"/api/projects/shop/staging/versions/{n}/rollback", headers=hd, json={}).status_code == 400, "no reason, no roll back"
    assert c.post("/api/projects/shop/staging/versions/999/rollback", headers=hd, json={"reason": "x"}).status_code == 400
    assert c.put("/api/projects/shop/staging/rollbacks/auto", headers=hd, json={"minutes": 0}).json()["on"] is False
    assert c.put("/api/projects/shop/staging/rollbacks/auto", headers=hd, json={"minutes": 3}).status_code == 400


def test_the_key_a_site_holds_survives_a_redeploy_and_a_roll_back(ws):  # noqa: F811
    assert DP.run(ws, "shop", "local", baseline=True)["status"] == "done"
    key = SG._key(SG.state(ws, "shop"))
    assert DP.run(ws, "shop", "local")["status"] == "done"
    assert SG._key(SG.state(ws, "shop")) == key, "a new version keeps the key"
    RB.go(ws, "shop", None, "It is too slow")
    assert SG._key(SG.state(ws, "shop")) == key, "and so does a roll back"
    assert "switching_from" not in SG.state(ws, "shop") and "switching_from" not in SG.public(SG.state(ws, "shop"))
