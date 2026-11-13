"""Captain runs no A/B tests. Nothing about one can stop or delay a deploy, a record of one left by an older
version is ignored and shown nowhere, the commands say where A/B testing lives now, and the endpoint takes nothing
about what a shopper did."""
from __future__ import annotations

import json
import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import project_state as PS
from captain_search import server
from captain_search import staging as SG
from captain_search.cli.main import app
from captain_search.staging import deploy as DP
from captain_search.staging import logs as L
from captain_search.staging import rollback as RB
from captain_search.staging import runtime as RT
from captain_search.staging import traffic as TF

from test_staging import _runtime, keychain, ws  # noqa: F401  (fixtures)

LINE = "Captain no longer runs A/B tests. Give your own testing tool each model's address."

# What an older version of Captain left in a project's state: a test still marked running at 5%.
OLD_TEST = {"id": "ab-0123456789", "status": "running", "mode": "captain", "env": "staging", "split": 0.05, "primary": "clicks",
            "guardrails": ["purchases"], "practice": True, "arms": {"baseline": "shop/keyword-baseline@v1", "candidate": "shop/starter-ranker@v2"},
            "started_at": "2026-11-11T18:00:00+00:00", "last_readout": {"text": "The candidate gets 5% of traffic.", "enough": False}}


@pytest.fixture(autouse=True)
def only_the_five_steps(monkeypatch):
    monkeypatch.setattr(DP, "AFTER", [])


def test_a_state_file_with_a_running_test_deploys_redeploys_rolls_back_and_stops(ws):  # noqa: F811
    PS._update(ws, "shop", lambda d: d.__setitem__("ab", dict(OLD_TEST)))
    assert DP.run(ws, "shop", "local", baseline=True)["status"] == "done"
    # the endpoint was also left an old split in its control document: it is ignored, every request gets the model deployed
    SG.set_control(ws, "shop", "staging", split=0.5, experiment="ab-0123456789")
    assert DP.run(ws, "shop", "local")["status"] == "done", "a second version deploys over the first"
    rec = SG.state(ws, "shop")
    arms = {SG.call(ws, rec, "rank", {"query": "jacket", "k": 3, "session_id": f"s{i}"})["arm"] for i in range(40)}
    assert arms == {"candidate"}, "no shopper is assigned to anything"
    assert RB.go(ws, "shop", None, "It is too slow")["rolled_back_to"] == ws.baseline
    assert SG.unstage(ws, "shop")["status"] == "stopped"
    assert PS.get(ws, "shop")["ab"] == OLD_TEST, "the old record is left as it was: ignored, not rewritten"


def test_the_old_record_is_shown_nowhere(ws, monkeypatch):  # noqa: F811
    PS._update(ws, "shop", lambda d: d.__setitem__("ab", dict(OLD_TEST)))
    assert DP.run(ws, "shop", "local")["status"] == "done"
    doc = PS.get(ws, "shop")
    sh = SG.ship_state(ws, doc)
    said = json.dumps([sh, SG.agent_summary(ws, "shop"), PS.page(ws, "shop") if hasattr(PS, "page") else {}, TF.connect(ws, "shop")], default=str)
    for word in ("ab-0123456789", "5% of traffic", "A/B test is", "split traffic between the baseline", "ramp", "Ramp"):
        assert word not in said, word
    assert [s["id"] for s in sh["steps"]] == ["stage", "try", "connect"]
    assert SG.agent_summary(ws, "shop")["ab_tests"].startswith("Captain runs no A/B tests.")
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    for path in ("/ab", "/ab/options"):
        assert c.get(f"/api/projects/shop{path}", headers=hd).status_code == 404
    for path in ("/ab/start", "/ab/ramp", "/ab/rollback", "/ab/stop", "/ab/decide", "/ab/simulate", "/ab/outcomes", "/ab/result"):
        assert c.post(f"/api/projects/shop{path}", headers=hd, json={}).status_code in (404, 405), path
    assert not [r.path for r in server.app.routes if "/ab" in getattr(r, "path", "").replace("/absent", "")]


def test_every_old_command_prints_one_line_and_exits_2(ws):  # noqa: F811
    run = CliRunner()
    for cmd in ("ab", "ab-start", "ab-readout", "ab-ramp", "ab-rollback", "ab-stop", "ab-simulate", "ab-outcomes", "ab-result", "ab-decide"):
        r = run.invoke(app, ["projects", cmd, "shop", "--split", "5%"])
        assert r.exit_code == 2 and r.output.strip() == LINE, (cmd, r.exit_code, r.output)
    listed = run.invoke(app, ["projects", "--help"]).output
    assert "ab-start" not in listed and "ab-readout" not in listed, "they are hidden"


def test_the_endpoint_takes_a_timing_and_nothing_about_what_a_shopper_did(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    rec = SG.state(ws, "shop")
    url, hd = rec["endpoint"]["url"], {"Authorization": f"Bearer {SG._key(rec)}"}
    a = httpx.post(url + "/rank", json={"query": "jacket", "k": 3, "traffic": "real"}, headers=hd, timeout=20).json()
    r = httpx.post(url + "/outcome", json={"request_id": a["request_id"], "event": "purchase", "product_id": a["ids"][0], "value": 59.0}, headers=hd, timeout=20)
    assert r.status_code == 410 and r.json()["detail"] == RT.OUTCOMES_GONE
    assert "no longer collects what shoppers clicked or bought" in RT.OUTCOMES_GONE and "POST /timing" in RT.OUTCOMES_GONE
    # how long it took at the site is still welcome, at its own address and from code pasted before
    assert httpx.post(url + "/timing", json={"request_id": a["request_id"], "ms": 120}, headers=hd, timeout=20).json() == {"ok": True, "request_id": a["request_id"]}
    assert httpx.post(url + "/outcome", json={"request_id": a["request_id"], "event": "timing", "ms": 130}, headers=hd, timeout=20).status_code == 200
    assert httpx.post(url + "/timing", json={"request_id": a["request_id"], "ms": 1}, timeout=20).status_code == 401, "it needs the key"
    SG.call(ws, rec, "flush", {})
    got = L.read(ws, "shop", "staging", rec["id"], extra=True)
    assert "outcomes" not in got and [t["ms"] for t in got["timings"]] == [120.0, 130.0]
    files = [k.rsplit("/", 1)[-1] for k in L.shared(ws, "shop", "staging").list(L.prefix("shop", "staging", rec["id"]))]
    assert not [f for f in files if f.startswith("outcomes-")], "nothing a shopper did is stored"
    st = SG.call(ws, rec, "status", {})
    assert "outcomes" not in st and "split" not in st and "experiment" not in st


def test_outcome_files_written_by_an_older_version_are_never_read(ws):  # noqa: F811
    rt = _runtime(ws)
    rt.rank({"query": "jacket", "k": 3, "request_id": "r-1"})
    rt.flush()
    store = L.shared(ws, "shop", "staging")
    store.put_new(L.prefix("shop", "staging", rt.stage) + "outcomes-old.jsonl",
                  json.dumps({"request_id": "r-1", "event": "purchase", "product_id": "p1", "value": 59.0, "ts": "2026-11-10T00:00:00Z"}).encode())
    got = L.read(ws, "shop", "staging", rt.stage)
    assert "outcomes" not in got and len(got["requests"]) == 1
    rows = L.impressions(got["requests"])
    assert rows and not {"clicked", "purchased", "added_to_cart", "value"} & set(rows[0])
    assert all("events" not in x for x in TF.request(ws, "shop", "r-1")["results"]) if SG.state(ws, "shop") else True


def test_use_it_from_your_site_gives_the_code_an_address_a_model_and_the_one_paragraph(ws):  # noqa: F811
    SG.stage(ws, "shop", target="local")
    k = TF.connect(ws, "shop")
    assert k["own_tests"] == "To compare models on real shoppers, split traffic in your own testing tool and send each group to its model's address."
    url = SG.state(ws, "shop")["endpoint"]["url"]
    assert [(a["role"], a["address"], a["model_version"]) for a in k["addresses"]] == [
        ("The deployed model", f"{url}/rank", ws.candidate), ("The compared model", f"{url}/rank/baseline", ws.baseline)]
    for code in k["snippets"].values():
        assert "/outcome" not in code and "purchase" not in code and "click" not in code and "add-to-cart" not in code
    # each address answers with its own model
    hd = {"Authorization": f"Bearer {SG._key(SG.state(ws, 'shop'))}"}
    for a in k["addresses"]:
        got = httpx.post(a["address"], json={"query": "jacket", "k": 3}, headers=hd, timeout=20).json()
        assert got["model_version"] == a["model_version"]


def test_nothing_in_the_deployment_code_names_a_split_or_a_ramp():
    src = pathlib.Path(SG.__file__).parent
    assert not (src / "ab.py").exists()
    for f in src.glob("*.py"):
        text = f.read_text()
        for word in ("An A/B test is running", "staging another version", "ramp(", "ab-start", "import ab"):
            assert word not in text, (f.name, word)


def test_a_model_that_ran_in_a_cloud_is_never_started_on_this_computer_because_no_place_was_named(ws):  # noqa: F811
    """After a model was removed from AWS, a start with no place named ran it on this computer and the record
    said "local", with a 127.0.0.1 address. It asks now."""
    SG._save(ws, "shop", "staging", {"env": "staging", "status": "stopped", "kind": "aws-sagemaker", "host": "aws", "target": "aws-serving",
                                      "model_version": ws.candidate, "resources": []})
    with pytest.raises(ValueError, match="last ran on aws, and no place was named"):
        SG.stage(ws, "shop")                       # no place named
    with pytest.raises(ValueError, match="not set up for deploying any more"):
        SG.stage(ws, "shop", target="")            # a place was meant, and its name is missing
    rec = SG.state(ws, "shop")
    assert rec["kind"] == "aws-sagemaker" and rec["status"] == "stopped" and not rec.get("endpoint"), "nothing was started, and the record is as it was"
    # choosing this computer, in so many words, still works
    assert SG.stage(ws, "shop", target="local")["kind"] == "local"
    SG.unstage(ws, "shop")
    # and a project that never ran in a cloud still deploys here when no place is named, as before
    assert SG.stage(ws, "shop")["kind"] == "local"


def test_a_model_in_a_cloud_is_asked_how_it_is_at_most_once_a_minute_however_often_a_page_reads(ws, monkeypatch):  # noqa: F811
    """On SageMaker every call to the endpoint counts as traffic. A project page left open read the status every
    five seconds, and 24 test searches showed as 160 requests in an hour."""
    calls = []
    monkeypatch.setattr(SG, "call", lambda ctx, rec, op, body, *a, **k: (calls.append(op), {"ok": True, "requests": 24, "traffic": {"real": 0}})[1])
    SG.forget_asked()
    cloud = {"id": "stg-cloud-1", "kind": "aws-sagemaker", "status": "running", "how": "captain"}
    for _ in range(170):                                   # fourteen minutes of a page reading every five seconds
        assert SG.ask_status(ws, cloud)["requests"] == 24
        SG.read_flush(ws, cloud)
    assert calls == ["status", "flush"], f"one question and one flush, not {len(calls)} calls"
    assert SG.ask_status(ws, cloud)["asked_ago_s"] >= 0, "the answer says how old it is"
    # a person asking for the requests list still gets a fresh log
    SG.read_flush(ws, cloud, asked_for=True)
    assert calls == ["status", "flush", "flush"]
    # a minute later it is asked again
    SG._ASKED["stg-cloud-1"] = (SG._ASKED["stg-cloud-1"][0] - 61, SG._ASKED["stg-cloud-1"][1])
    SG.ask_status(ws, cloud)
    assert calls.count("status") == 2
    # a model that is down is not called every few seconds either
    calls.clear()
    SG.forget_asked()
    monkeypatch.setattr(SG, "call", lambda ctx, rec, op, body, *a, **k: (calls.append(op), (_ for _ in ()).throw(ValueError("no answer")))[1])
    for _ in range(20):
        with pytest.raises(ValueError, match="no answer"):
            SG.ask_status(ws, cloud)
    assert calls == ["status"]
    # on this computer nothing counts it, so it is asked every time
    calls.clear()
    monkeypatch.setattr(SG, "call", lambda ctx, rec, op, body, *a, **k: (calls.append(op), {"ok": True})[1])
    here = {"id": "stg-local-1", "kind": "local", "status": "running", "how": "captain"}
    for _ in range(5):
        SG.ask_status(ws, here)
        SG.read_flush(ws, here)
    assert calls == ["status", "flush"] * 5
    SG.forget_asked()
