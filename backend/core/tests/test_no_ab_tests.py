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
