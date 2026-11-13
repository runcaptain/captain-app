"""What a deployed model needs before a real site sends it traffic: a key that changes with no downtime, a request
size and rate limit with a clear error, and a time budget with a fallback answer. Against the runtime in this
process with stand-ins that are slow and that fail, and against a real endpoint on this computer."""
from __future__ import annotations

import hashlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import project_state as PS
from captain_search import server
from captain_search import staging as SG
from captain_search.cli.main import app
from captain_search.staging import health as HL
from captain_search.staging import protect as PT
from captain_search.staging import runtime as RT

from test_staging import _runtime, keychain, ws  # noqa: F401  (fixtures)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ask(url, key, **body):
    return httpx.post(url + "/rank", json={"query": "jacket", "k": 3, **body}, headers={"Authorization": f"Bearer {key}"}, timeout=20)


# ---- the key ------------------------------------------------------------------------------------------

def test_a_changed_key_works_at_once_and_the_old_one_until_its_grace_ends(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    url = SG.state(ws, "shop")["endpoint"]["url"]
    old = SG._key(SG.state(ws, "shop"))
    st = PT.key_state(ws, "shop")
    assert st["can_rotate"] and st["text"] == "The key has not been changed since it was made." and not st["previous_valid"]
    assert _ask(url, old).status_code == 200 and _ask(url, "wrong").status_code == 401

    out = PT.rotate_key(ws, "shop", grace_minutes=30)
    new = SG._key(SG.state(ws, "shop"))
    assert out["rotated"] and new != old and out["previous_valid"] and "The old key works until" in out["text"]
    assert "key" not in out and new not in str(out) and old not in str(out), "the answer never holds a key"
    # no downtime: the new key and the old key both work, straight away, with no restart
    assert _ask(url, new).status_code == 200 and _ask(url, old).status_code == 200
    assert _ask(url, "wrong").status_code == 401
    assert SG.query(ws, "shop", "jacket", k=3)["staged"]["results"], "Captain's own requests use the new key"

    # end the grace now: change it again with none. The key before that one is already gone.
    PT.rotate_key(ws, "shop", grace_minutes=0)
    newest = SG._key(SG.state(ws, "shop"))
    time.sleep(1.1)  # a copy re-reads its control document at most once a second for a key it doesn't know
    assert _ask(url, newest).status_code == 200
    assert _ask(url, new).status_code == 401 and _ask(url, old).status_code == 401, "an ended key is refused"
    assert "The old key no longer works." in PT.key_state(ws, "shop")["text"]
    assert any("changed the deployed model's key" in x["what"] for x in PS.get(ws, "shop")["history"])


def test_the_old_key_stops_when_its_grace_runs_out(ws, monkeypatch):  # noqa: F811
    rt = _runtime(ws, key_sha256=_sha("first"))
    assert rt.authorized("first") and not rt.authorized("second") and not rt.authorized(None)
    SG.set_control(ws, "shop", "staging", stage=rt.stage, keys={"current": _sha("second"), "previous": _sha("first"),
                                                                "previous_until": "2026-01-01T01:00:00+00:00"})
    rt._key_reload_at = -10.0
    monkeypatch.setattr(RT, "_now", lambda: "2026-01-01T00:30:00.000Z")
    assert rt.authorized("second"), "a key it doesn't know makes it read the control document, with no restart"
    assert rt.authorized("first"), "inside the grace period"
    monkeypatch.setattr(RT, "_now", lambda: "2026-01-01T01:00:01.000Z")
    assert rt.authorized("second") and not rt.authorized("first"), "after it"


def test_a_key_set_in_the_environment_is_not_changed_here_and_a_fresh_deploy_starts_clean(ws, monkeypatch):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    PT.rotate_key(ws, "shop")
    assert SG.control(ws, "shop").get("keys", {}).get("current")
    SG.unstage(ws, "shop")
    # a fresh deploy makes a fresh key: hashes left by the earlier change must not refuse it
    SG.stage(ws, "shop", target="local")
    rec = SG.state(ws, "shop")
    assert not SG.control(ws, "shop").get("keys")
    assert _ask(rec["endpoint"]["url"], SG._key(rec)).status_code == 200
    monkeypatch.setenv(rec["key_secret"], "set-by-hand")
    st = PT.key_state(ws, "shop")
    assert not st["can_rotate"] and "set in the environment" in st["why_not"]
    with pytest.raises(ValueError, match="set in the environment"):
        PT.rotate_key(ws, "shop")


# ---- limits ---------------------------------------------------------------------------------------------

def test_a_request_too_big_or_one_too_many_is_refused_with_a_sentence(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    rec = SG.state(ws, "shop")
    url, key = rec["endpoint"]["url"], SG._key(rec)
    d = PT.get(ws, "shop")
    assert d["limits"] == {"max_request_kb": 256, "rate_per_s": None} and d["rate_text"] == "No limit on requests a second."
    with pytest.raises(ValueError, match="between 1 and 10240"):
        PT.change(ws, "shop", max_request_kb=0)
    out = PT.change(ws, "shop", max_request_kb=2, rate_per_s=2)
    assert out["size_text"].startswith("Requests up to 2 KB.") and out["rate_text"].startswith("Up to 2 requests a second on each copy.")
    time.sleep(0.2)

    big = _ask(url, key, note="x" * 5000)
    assert big.status_code == 413
    assert "This request is 5 KB. This deployment takes requests up to 2 KB." in big.json()["detail"] and "--max-request-kb" in big.json()["detail"]

    codes = [_ask(url, key) for _ in range(8)]
    over = [r for r in codes if r.status_code == 429]
    assert [r.status_code for r in codes][:2] == [200, 200] and over, [r.status_code for r in codes]
    assert over[0].headers["retry-after"] == "1" and "Too many requests. This deployment takes 2 a second on each copy." in over[0].json()["detail"]
    time.sleep(1.1)
    assert _ask(url, key).status_code == 200, "a second later it is taken again"
    st = SG.call(ws, rec, "status", {})
    assert st["refused"] >= 1 + len(over) and st["errors"] == 0, "refused requests are counted apart from failures"
    # off again
    assert PT.change(ws, "shop", rate_per_s=0)["limits"]["rate_per_s"] is None


# ---- the time budget and the fallback answer -----------------------------------------------------------

def _stand_in(rt, monkeypatch, how):
    """Make the new model slow, or make it fail."""
    cand = rt.arms["candidate"]
    real = cand.rank

    def slow(query, k):
        time.sleep(0.4)
        return real(query, k)

    def broken(query, k):
        raise RuntimeError("the model could not score this")

    monkeypatch.setattr(cand, "rank", {"slow": slow, "broken": broken}[how])


def test_a_slow_model_is_answered_by_the_compared_model_and_counted(ws, monkeypatch):  # noqa: F811
    rt = _runtime(ws)
    SG.set_control(ws, "shop", "staging", stage=rt.stage, budget={"ms": 100, "fallback": True})
    rt.control(force=True)
    fast = rt.rank({"query": "jacket", "k": 3})
    assert "fallback" not in fast and fast["model_version"] == ws.candidate, "inside the budget it answers itself"
    _stand_in(rt, monkeypatch, "slow")
    t0 = time.perf_counter()
    r = rt.rank({"query": "jacket", "k": 3, "traffic": "real"})
    took = time.perf_counter() - t0
    assert r["ids"] and r["fallback"] == {"why": "slow", "answered_by": ws.baseline} and r["model_version"] == ws.baseline
    assert r["arm"] == "candidate", "it stays the request of the model the shopper was given"
    assert took < 0.35, f"the site did not wait for the slow model: {took:.2f} s"
    assert rt.fallbacks == 1 and rt.errors == 0 and rt.status()["fallbacks"] == 1
    logged = rt._req[-1]
    assert logged["fallback"]["why"] == "slow" and logged["fallback"]["from"] == ws.candidate and logged["fallback"]["budget_ms"] == 100
    # a request that names one model is never answered by the other: it takes its time and answers itself
    forced = rt.rank({"query": "jacket", "k": 3}, "candidate")
    assert "fallback" not in forced and forced["model_version"] == ws.candidate


def test_a_failing_model_is_answered_by_the_compared_model_even_with_no_budget(ws, monkeypatch):  # noqa: F811
    rt = _runtime(ws)
    _stand_in(rt, monkeypatch, "broken")
    r = rt.rank({"query": "jacket", "k": 3})
    assert r["ids"] and r["fallback"] == {"why": "failed", "answered_by": ws.baseline}
    assert rt.fallbacks == 1 and rt.errors == 0
    assert "could not score" in rt._req[-1]["fallback"]["error"]
    # turned off: the failure is the answer, as before
    SG.set_control(ws, "shop", "staging", stage=rt.stage, budget={"ms": None, "fallback": False})
    rt.control(force=True)
    with pytest.raises(RuntimeError, match="could not score"):
        rt.rank({"query": "jacket", "k": 3})
    assert rt.errors == 1


def test_with_no_compared_model_a_slow_request_gets_a_clear_504(ws, monkeypatch):  # noqa: F811
    rt = _runtime(ws)
    SG.set_control(ws, "shop", "staging", stage=rt.stage, budget={"ms": 100, "fallback": True})
    rt.control(force=True)
    _stand_in(rt, monkeypatch, "slow")
    del rt.arms["baseline"]
    c = TestClient(RT.make_app(rt))
    r = c.post("/rank", json={"query": "jacket", "k": 3})
    assert r.status_code == 504
    assert r.json()["detail"] == "This request took longer than the 100 ms this deployment allows, and there is no other model loaded to answer in its place."
    assert rt.errors == 1 and rt.fallbacks == 0


def test_fallbacks_count_against_the_models_health(ws, monkeypatch):  # noqa: F811
    m = {"answering": True, "requests": 40, "errors": 0, "fallbacks": 10, "error_rate": 0.25, "p95_ms": 3.0, "stopped": False, "asleep": False}
    why = HL.judge(m, {"p95_ms": None, "error_rate": 0.05, "set": True})
    assert why == ["10 of its last 40 requests failed or were answered by the compared model because this one was slow or failed (25.0%), over the 5% target."]
    # and through a real log: a model that always falls back is unhealthy, though every request got an answer
    SG.stage(ws, "shop", target="local")
    rec = SG.state(ws, "shop")
    PT.change(ws, "shop", budget_ms=10)
    url, key = rec["endpoint"]["url"], SG._key(rec)
    for _ in range(25):
        assert _ask(url, key).status_code == 200
    got = HL.measure(ws, "shop")
    assert got["requests"] >= 25 and got["fallbacks"] <= got["requests"]
    assert got["error_rate"] == round(got["fallbacks"] / got["requests"], 4), "the rate counts fallbacks"


# ---- the command and the addresses -----------------------------------------------------------------------

def test_the_commands_and_the_addresses(ws, monkeypatch):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    run = lambda *a: CliRunner().invoke(app, list(a))  # noqa: E731
    r = run("deployments", "protect", "shop")
    assert r.exit_code == 0 and "Requests up to 256 KB." in r.output and "No limit on requests a second." in r.output, r.output
    r = run("deployments", "protect", "shop", "--budget-ms", "200", "--rate", "50")
    assert "If this model takes longer than 200 ms, or fails, the request is answered with the compared model's results." in r.output
    assert run("deployments", "protect", "shop", "--budget-ms", "5").exit_code != 0
    before = SG._key(SG.state(ws, "shop"))
    r = run("deployments", "key", "shop", "--rotate", "--grace-minutes", "15")
    after = SG._key(SG.state(ws, "shop"))
    assert r.exit_code == 0 and "The key was changed." in r.output and after != before and after not in r.output and before not in r.output
    assert run("deployments", "key", "shop", "--show").output.strip() == after, "the key is printed only when asked for"

    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    d = c.get("/api/projects/shop/staging/protect", headers=hd).json()
    assert d["budget"] == {"ms": 200.0, "fallback": True} and d["limits"]["rate_per_s"] == 50.0 and d["has_fallback"] and d["key"]["previous_valid"]
    assert c.put("/api/projects/shop/staging/protect", headers=hd, json={"rate_per_s": 0}).json()["limits"]["rate_per_s"] is None
    assert c.put("/api/projects/shop/staging/protect", headers=hd, json={"budget_ms": "soon"}).status_code == 400
    out = c.post("/api/projects/shop/staging/key/rotate", headers=hd, json={"grace_minutes": 5})
    newest = SG._key(SG.state(ws, "shop"))
    assert out.status_code == 200 and out.json()["rotated"] and newest not in out.text and after not in out.text
    assert c.post("/api/projects/shop/staging/key/rotate", headers=hd, json={"grace_minutes": 99999999}).status_code == 400
