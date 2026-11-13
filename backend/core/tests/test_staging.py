"""Staging: a model version as a live endpoint a person can try. Everything here runs on the local target, with no
cloud: the same runtime and the same bundle the cloud serves."""
import json
import time

import httpx
import keyring
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend
from typer.testing import CliRunner

from captain_search import demo as D
from captain_search import ops
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search import staging as SG
from captain_search import starter as ST
from captain_search import trainers as TR
from captain_search.cli.main import app
from captain_search.data.datasets import snapshot
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.staging import logs as L
from captain_search.staging import runtime as RT
from captain_search.workspace import init_workspace

from test_starter_ranker import FAKE_TRAINER, _logs


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.d = {}

    def get_password(self, service, name):
        return self.d.get((service, name))

    def set_password(self, service, name, value):
        self.d[(service, name)] = value

    def delete_password(self, service, name):
        self.d.pop((service, name), None)


@pytest.fixture(autouse=True)
def keychain():
    old = keyring.get_keyring()
    mem = MemoryKeyring()
    keyring.set_keyring(mem)
    yield mem
    keyring.set_keyring(old)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A project with a keyword baseline and one trained candidate (a fake trainer: cheaper products first)."""
    ctx = init_workspace("t", str(tmp_path / "store"))
    cat = snapshot(ctx, "shop-catalog", pa.Table.from_pylist(D.catalog()), kind=DatasetKind.catalog,
                   provenance=Provenance(source="generated"))
    logs, _ = _logs(ctx, cat, sessions=1500)
    PR.create(ctx, "shop")
    PS.pin(ctx, "shop", {"catalog": ops.ref(cat), "events": ops.ref(logs)})
    (tmp_path / "fake_trainer.py").write_text(FAKE_TRAINER)
    monkeypatch.setattr(TR, "command", lambda name="lightgbm-ltr": f'"$CST_PYTHON" {tmp_path / "fake_trainer.py"}')
    r = ST.train(ctx, "shop")
    ctx.candidate, ctx.baseline, ctx.exp = r["pipeline"], r["baseline"], r["experiment"]
    yield ctx
    rec = SG.state(ctx, "shop")
    if rec and rec.get("status") == "running":
        SG.unstage(ctx, "shop")


def _runtime(ctx, serve="candidate", **kw):
    """The runtime in this process, over a real bundle: for tests that need its clock or its insides."""
    arms = {"candidate": ctx.candidate, "baseline": ctx.baseline}
    key, _ = SG.build_bundle(ctx, "shop", "staging", arms, serve)
    return RT.Runtime(ctx, key, **kw)


# ---- stage, try, stop ------------------------------------------------------------------------------


def test_stage_on_this_computer_then_rank_then_stop(ws):
    r = SG.stage(ws, "shop", target="local")
    assert r["status"] == "running" and r["kind"] == "local" and r["model_version"] == ws.candidate
    assert r["arms"] == {"candidate": ws.candidate, "baseline": ws.baseline}
    assert r["endpoint"]["url"].startswith("http://127.0.0.1:")  # loopback only
    assert "key_secret" not in r and r["cost"]["hourly_usd"] == 0.0 and r["cost"]["estimate"]["free"]
    rec = SG.state(ws, "shop")
    url, key = rec["endpoint"]["url"], SG._key(rec)

    assert httpx.post(url + "/rank", json={"query": "jacket"}).status_code == 401  # the endpoint needs its key
    assert httpx.post(url + "/rank", json={"query": "jacket"}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    j = httpx.post(url + "/rank", json={"query": "jacket", "k": 5, "session_id": "s1", "request_id": "r-1"},
                   headers={"Authorization": f"Bearer {key}"}).json()
    assert j["request_id"] == "r-1" and j["arm"] == "candidate" and j["model_version"] == ws.candidate
    assert len(j["ids"]) == 5 and len(j["scores"]) == 5 and j["ms"] >= 0

    s = SG.status(ws, "shop")
    assert s["status"] == "running" and s["health"]["requests"] == 1 and s["stops_at"]
    assert "Stops by itself" in s["text"]

    out = SG.unstage(ws, "shop")
    assert out["status"] == "stopped" and not out["resources"]
    with pytest.raises(httpx.HTTPError):
        httpx.get(url + "/ping", timeout=1)
    assert SG.unstage(ws, "shop")["status"] == "stopped"  # twice is fine
    # the project's history says what happened
    whats = [h["what"] for h in PS.get(ws, "shop")["history"]]
    assert any(w.startswith("deployed ") for w in whats) and any(w.startswith("stopped the deployed model") for w in whats)


def test_the_runtime_ranks_exactly_as_the_offline_engine_does(ws):
    """Serving uses the feature module and the model files training used: same ids, same order, same scores."""
    from captain_search.engines import artifact_reader, make_engine

    rt = _runtime(ws)
    pm, _ = ws.registry.resolve("pipeline", ws.candidate)
    eng = make_engine(pm.engine, pm.spec, ops.products_of(ws, pm.catalog), artifact=artifact_reader(ws, pm))
    for q in ("jacket", "boots under 50", "waterproof tent"):
        got = rt.rank({"query": q, "k": 20})
        want = eng.search_scored(q, 20)
        assert got["ids"] == [p for p, _ in want]
        assert got["scores"] == [round(s, 6) for _, s in want]


def test_console_query_joins_titles_and_shows_the_baseline_beside(ws):
    SG.stage(ws, "shop", target="local")
    r = SG.query(ws, "shop", "jacket", k=5)
    assert r["traffic"] == "test"
    assert r["staged"]["arm"] == "candidate" and r["baseline"]["arm"] == "baseline"
    assert r["baseline"]["model_version"] == ws.baseline
    titles = {p["id"]: p["title"] for p in D.catalog()}
    for side in (r["staged"], r["baseline"]):
        assert len(side["results"]) == 5 and side["ms"] >= 0
        assert all(x["title"] == titles[x["id"]] for x in side["results"])
    assert isinstance(r["same_order"], bool)
    with pytest.raises(ValueError, match="Type a query"):
        SG.query(ws, "shop", "  ")


def test_staging_the_baseline_alone_and_restaging(ws):
    a = SG.stage(ws, "shop", baseline=True, target="local")
    assert a["arms"] == {"baseline": ws.baseline} and a["serve"] == "baseline"
    first = SG.state(ws, "shop")["endpoint"]
    r = SG.query(ws, "shop", "jacket")
    assert r["staged"]["arm"] == "baseline" and r["baseline"] is None
    b = SG.stage(ws, "shop", candidate=ws.exp, target="local")  # restage: the old process goes away
    assert b["model_version"] == ws.candidate and b["previous"] == ws.baseline
    assert not SG._alive(first["pid"])
    # the deploy history the promote path keeps knows what is on the endpoint and what was there before
    assert ops.current(ws, "shop-staging")["pipeline"] == ws.candidate
    assert ops.history(ws, "shop-staging")[-1].rollback_to == ws.baseline


def test_a_version_that_cannot_be_served_is_refused_in_a_sentence(ws):
    draft = ops.pipeline_publish(ws, {"name": "shop/outside", "engine": "http", "spec": {"url": "https://set-me/v1/rank"}}, dry_run=True)
    for f in draft.files:
        ws.registry.put_blob(b"x", f.path)
    pm = ws.registry.publish(draft.model_copy(update={"engine": "custom-torch", "files": []}))
    linked = ws.registry.publish(draft.model_copy(update={"name": "shop/linked", "files": []}))
    with pytest.raises(SG.NotServable, match="no serving entry point"):
        SG.stage(ws, "shop", candidate=ops.ref(linked), target="local")
    with pytest.raises(SG.NotServable, match="no serving entry point"):
        SG.stage(ws, "shop", candidate=ops.ref(pm), target="local")
    assert SG.state(ws, "shop") is None  # nothing was started or recorded


def test_use_an_endpoint_i_already_run(ws, tmp_path):
    """A team's own service is wrapped: Captain's runtime sits in front of it, logs and can split."""
    SG.stage(ws, "shop", baseline=True, target="local")
    theirs = SG.state(ws, "shop")
    # their service: any HTTP endpoint that answers POST {query, k} with {ids}. Here, another local runtime's
    # SageMaker-shaped route would need a key, so stand one up with plain FastAPI.
    import threading

    import uvicorn
    from fastapi import FastAPI

    api = FastAPI()

    @api.post("/search")
    def search(body: dict):
        return {"ids": ["p-own-1", "p-own-2"][: body.get("k", 10)]}

    port = SG._free_port()
    srv = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.post(f"http://127.0.0.1:{port}/search", json={"query": "x"}, timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    try:
        r = SG.stage(ws, "shop", endpoint=f"http://127.0.0.1:{port}/search")
        assert r["how"] == "endpoint" and r["kind"] == "local" and r["arms"]["baseline"] == ws.baseline
        q = SG.query(ws, "shop", "jacket", k=2)
        assert [x["id"] for x in q["staged"]["results"]] == ["p-own-1", "p-own-2"]
        assert q["baseline"]["model_version"] == ws.baseline
    finally:
        srv.should_exit = True
    assert not SG._alive(theirs["endpoint"]["pid"])


def test_cli_and_api(ws, monkeypatch):
    from captain_search import server

    run = CliRunner()
    dry = run.invoke(app, ["--json", "--dry-run", "projects", "stage", "shop", "--target", "local"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["dry_run"] and SG.state(ws, "shop") is None
    out = run.invoke(app, ["--json", "projects", "stage", "shop", "--target", "local"])
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["status"] == "running"
    q = run.invoke(app, ["projects", "staging-query", "shop", "jacket", "-k", "3"])
    assert q.exit_code == 0 and "Candidate" in q.output and "Baseline" in q.output
    st = run.invoke(app, ["projects", "staging", "shop"])
    assert st.exit_code == 0 and "is running on this computer" in st.output

    monkeypatch.setattr(server, "ctx", lambda: ws)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    s = c.get("/api/projects/shop/staging", headers=h).json()
    assert s["status"] == "running" and "key_secret" not in json.dumps(s)
    r = c.post("/api/projects/shop/staging/query", json={"query": "boots", "k": 3}, headers=h).json()
    assert len(r["staged"]["results"]) == 3 and r["baseline"]
    assert c.post("/api/projects/shop/staging/query", json={"query": ""}, headers=h).status_code == 400
    job = c.post("/api/projects/shop/unstage", json={}, headers=h).json()["job"]
    for _ in range(100):
        if server.JOBS[job]["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert server.JOBS[job]["status"] == "done"
    assert c.get("/api/projects/shop/staging", headers=h).json()["status"] == "stopped"
    job = c.post("/api/projects/shop/stage", json={"baseline": True, "target": "local"}, headers=h).json()["job"]
    for _ in range(400):
        if server.JOBS[job]["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert server.JOBS[job]["status"] == "done", server.JOBS[job]
    assert run.invoke(app, ["projects", "unstage", "shop"]).exit_code == 0


def test_idle_endpoint_stops_by_itself(ws):
    now = [1000.0]
    stopped = []
    rt = _runtime(ws, idle_minutes=240, on_stop=stopped.append, clock=lambda: now[0])
    rt.rank({"query": "jacket", "session_id": "s"})
    now[0] += 239 * 60
    rt.tick()
    assert not rt.stopped and rt.status()["stops_in_s"] == 60
    rt.rank({"query": "jacket", "session_id": "s"})  # use resets the countdown
    now[0] += 239 * 60
    rt.tick()
    assert not rt.stopped
    now[0] += 2 * 60
    rt.tick()
    assert rt.stopped and stopped == ["no requests for 240 minutes"]
    assert json.loads(ws.store.get(RT.stopped_key("shop", "staging", rt.stage)))["reason"] == stopped[0]
    assert L.read(ws, "shop", "staging", rt.stage)["requests"]  # the log was written before it stopped


def test_captain_notices_an_endpoint_that_stopped_by_itself(ws):
    SG.stage(ws, "shop", target="local", idle_minutes=0.02)  # about a second
    for _ in range(100):
        s = SG.status(ws, "shop")
        if s["status"] == "stopped":
            break
        time.sleep(0.2)
    assert s["status"] == "stopped" and "stopped by itself" in s["staged"]["stop_reason"]


# ---- serve-time logging and the consistency check ---------------------------------------------------


def test_every_request_is_logged_with_what_it_served(ws):
    rt = _runtime(ws)
    a = rt.rank({"query": "jacket", "k": 5, "session_id": "s1", "user_id": "u1", "request_id": "r-1", "traffic": "test"})
    rt.rank({"query": "boots", "k": 3, "session_id": "s2"})
    assert not hasattr(rt, "outcome"), "the endpoint takes nothing about what a shopper did"
    assert rt.flush(publish=True)["published"] == "shop/staging-logs@v1"
    got = L.read(ws, "shop", "staging", rt.stage)
    r1 = next(r for r in got["requests"] if r["request_id"] == "r-1")
    assert r1["query"] == "jacket" and r1["ids"] == a["ids"] and r1["model_version"] == ws.candidate
    assert r1["arm"] == "candidate" and r1["traffic"] == "test" and r1["ms"] >= 0 and r1["ts"].endswith("Z")
    names = got["names"]["candidate"]
    assert set(r1["features"]) == set(a["ids"]) and all(len(v) == len(names) for v in r1["features"].values())
    assert got["requests"][1]["traffic"] == "unlabelled" and got["requests"][1]["request_id"]  # never real by default. An id is made when none is sent
    assert "outcomes" not in got
    # the published version is a log of what was served, with no shopper outcomes, and it is never mistaken for shoppers
    m, rows, _ = ops.rows_of(ws, "shop/staging-logs@v1")
    assert m.dataset_kind == DatasetKind.events and m.tags["synthetic"] == "true" and m.tags["traffic"] == "test,unlabelled"
    top = next(r for r in rows if r["request_id"] == "r-1" and r["position"] == 1)
    assert top["arm"] == "candidate" and not {"clicked", "added_to_cart", "purchased", "value"} & set(top)
    assert {"query", "product_id", "position", "session_id", "ts", "request_id", "model_version"} <= set(top)
    assert rt.flush(publish=True) == {"requests": 0, "published": None}  # nothing new, no new version


def test_the_log_is_written_on_a_timer(ws):
    now = [0.0]
    rt = _runtime(ws, flush_s=15, publish_minutes=5, clock=lambda: now[0])
    rt.rank({"query": "jacket"})
    rt.tick()
    assert not L.read(ws, "shop", "staging", rt.stage)["requests"]  # still buffered
    now[0] = 16
    rt.tick()
    assert len(L.read(ws, "shop", "staging", rt.stage)["requests"]) == 1
    assert ws.registry.versions("dataset", "shop/staging-logs") == []
    now[0] = 301
    rt.tick()
    assert ws.registry.versions("dataset", "shop/staging-logs") == [1]  # published after five minutes


def test_staging_check_passes_when_serving_computes_what_training_computes(ws):
    from captain_search.staging import consistency as SC

    SG.stage(ws, "shop", target="local")
    first = SC.check(ws, "shop")
    assert first["status"] == "nothing-to-check" and "Try a query first" in first["text"]
    for q in ("jacket", "boots under 50", "tent", "waterproof jacket"):
        SG.query(ws, "shop", q, k=10)
    r = SC.check(ws, "shop")
    assert r["status"] == "pass" and r["checked"] == 40 and r["match_rate"] == 1.0
    assert r["mismatch_rate"] == 0 and r["missing_rate"] == 0 and r["extra_rate"] == 0 and r["without_request_id"] == 0
    assert r["text"] == "Served features match training: 100% of 40 checked"
    assert SG.state(ws, "shop")["check"]["status"] == "pass"
    assert SG.status(ws, "shop")["staged"]["check"]["text"] == r["text"]
    out = CliRunner().invoke(app, ["projects", "staging-check", "shop"])
    assert out.exit_code == 0 and "Served features match training" in out.output


def test_staging_check_fails_on_a_planted_feature_difference(ws):
    """A stale lookup table on the server: the click features it serves are not the ones training computed."""
    from captain_search.staging import consistency as SC

    rt = _runtime(ws)
    feat = rt.arms["candidate"].engine.feat
    assert "pop_coec" in feat.names
    for q in ("jacket", "boots", "tent"):
        rt.rank({"query": q, "k": 10})
    feat.tables = {"window": feat.tables["window"], "pair": {},
                   "product": {k: [v[0] + 500, v[1], v[2], v[3]] for k, v in feat.tables["product"].items()}}
    for q in ("jacket", "boots", "tent"):
        rt.rank({"query": q, "k": 10})
    rt.flush()
    m = SC.measure(ws, L.read(ws, "shop", "staging", rt.stage))
    assert m["checked"] == 60 and m["matched"] <= 30 and m["missing_rate"] == 0 and m["extra_rate"] == 0
    bad = {f["feature"] for f in m["features"]}
    assert "pop_impressions_log" in bad and not any(n.startswith(("bm25_", "title_", "price_")) for n in bad)
    assert m["relative_error"]["pop_impressions_log"] > 0 and m["examples"][0]["feature"] in bad
    assert 0 < m["mismatch_rate"] < 0.5

    # end to end: the same thing through a staged endpoint blocks launch review until someone overrides with a reason
    SG.stage(ws, "shop", target="local")
    rec = SG.state(ws, "shop")
    SG.query(ws, "shop", "jacket", k=10)
    SG.call(ws, rec, "flush", {})
    good = L.read(ws, "shop", "staging", rec["id"])["requests"][0]
    planted = {**good, "request_id": "planted", "features": {p: [x + 1 for x in row] for p, row in good["features"].items()}}
    ws.store.put_new(L.prefix("shop", "staging", rec["id"]) + "requests-zz-planted.jsonl", json.dumps(planted).encode())
    r = SC.check(ws, "shop")
    assert r["status"] == "fail" and r["match_rate"] == 0.5 and "It needs 99.9%" in r["text"]
    out = CliRunner().invoke(app, ["projects", "staging-check", "shop"])
    assert out.exit_code == 1 and "mismatch rate" in out.output
    gates = ops.gates_for(ws, ops.env_create(ws, "prod", "live", {"type": "record"}), ws.candidate)
    if len(gates) > 1:  # the candidate has an offline evaluation: the served-features criterion sits beside the others
        g = next(x for x in gates if x["gate"] == "served_features")
        assert not g["ok"] and "Served features match training: 50%" in g["detail"]
    assert SC.latest_for(ws, ws.candidate)["status"] == "fail"


def test_a_failed_check_blocks_launch_review_until_overridden(ws):
    from captain_search.staging import consistency as SC

    PS.experiment_backtest(ws, "shop", ws.exp)
    env = ops.env_create(ws, "prod", "live", {"type": "record"})
    assert not any(g["gate"] == "served_features" for g in ops.gates_for(ws, env, ws.candidate))  # never staged: no such criterion
    ws.store.put_new(SC.check_key(ws.candidate), json.dumps({"status": "fail", "text": "Served features match training: 50% of 20 checked. It needs 99.9%."}).encode())
    g = next(x for x in ops.gates_for(ws, env, ws.candidate) if x["gate"] == "served_features")
    assert not g["ok"]
    with pytest.raises(ops.GateFailed, match="served_features"):
        ops.promote(ws, ws.candidate, "prod", confirm=True)
    d = ops.promote(ws, ws.candidate, "prod", confirm=True, override_reason="the stale table is fixed in the next build")
    assert d.action == "handoff" and "served_features" in d.gate_results["overridden"] and d.override_reason


# ---- the A/B test ----------------------------------------------------------------------------------


def test_a_failed_restage_keeps_the_endpoint_that_was_running(ws, monkeypatch):
    SG.stage(ws, "shop", baseline=True, target="local")
    before = SG.state(ws, "shop")

    def boom(*a, **kw):
        raise ValueError("The model did not start on this computer.")

    monkeypatch.setattr(SG, "_start_local", boom)
    with pytest.raises(ValueError, match="did not start"):
        SG.stage(ws, "shop", candidate=ws.exp, target="local")
    after = SG.state(ws, "shop")
    assert after["status"] == "running" and after["id"] == before["id"] and after["model_version"] == ws.baseline
    assert "is still running" in after["error"] and SG._alive(after["endpoint"]["pid"])
    monkeypatch.undo()
    # and it still answers, with the key it had
    assert SG.query(ws, "shop", "jacket")["staged"]["model_version"] == ws.baseline

