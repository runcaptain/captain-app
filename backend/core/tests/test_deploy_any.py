"""Deploying a model that is not a ranker: a weights model version through the same one-call deploy.

The general request shape (POST /predict: named inputs in, named outputs out, described by the manifest), a deploy on
this computer with the same record, log, stop and roll back, and each cloud host against its stand-in. Nothing here
calls a real cloud."""
import pytest
from fastapi.testclient import TestClient

from captain_search import projects as PR
from captain_search import staging as SG
from captain_search.staging import deploy as DP
from captain_search.staging import logs as L
from captain_search.staging import predict as PD
from captain_search.staging import runtime as RT
from captain_search.staging import traffic as TF
from captain_search.weights import session as WSN
from captain_search.weights import store as WS
from captain_search.workspace import init_workspace

from test_staging import keychain  # noqa: F401  (fixture)

KEY = "k"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def net(tmp_path):
    """A workspace with one project and the two starter networks. No ranker anywhere."""
    ctx = init_workspace("t", str(tmp_path / "store"))
    PR.create(ctx, "shop")
    WS.ensure_starters(ctx)
    yield ctx
    rec = SG.state(ctx, "shop")
    if rec and rec.get("status") == "running":
        SG.unstage(ctx, "shop")


def edited(ctx, model="tiny", value=-9.0):
    """Save an edited version of a starter: one bias set, with a reason. Returns the save's answer."""
    s = WSN.open_session(ctx, model, by="agent", name="tester")
    WSN.edit(ctx, s["id"], {"op": "set", "layer": 2 if model == "tiny" else 3, "neuron": 0, "from": None, "value": value, "why": "to see the answer move"}, by="agent")
    return WSN.save(ctx, s["id"], note="one bias moved")


def runtime(ctx, ref):
    import hashlib

    key, _ = SG.build_bundle(ctx, "shop", "staging", {"candidate": ref}, "candidate")
    rt = RT.Runtime(ctx, key, key_sha256=hashlib.sha256(KEY.encode()).hexdigest())
    return rt, TestClient(RT.make_app(rt))


# ---- the request shape -----------------------------------------------------------------------------


def test_the_shape_comes_from_the_manifest(net):
    d = PD.shape(WS.manifest(net, "digits"))
    assert d["inputs"][0]["count"] == 784 and d["inputs"][0]["shape"] == [28, 28] and d["inputs"][0]["name"] == "input"
    assert [o["name"] for o in d["outputs"]] == ["scores", "named", "chosen", "chosen_index"] and d["outputs"][0]["names"] == [str(i) for i in range(10)]
    assert d["text"] == "Takes a grid of 28 by 28 numbers. Gives back 10 scores and the one chosen."
    t = PD.shape(WS.manifest(net, "tiny"))
    assert t["inputs"][0]["names"] == ["x1", "x2"] and [o["name"] for o in t["outputs"]] == ["scores", "named", "yes"]
    assert t["text"] == "Takes 2 numbers (x1, x2). Gives back 1 score."


def test_digits_takes_784_numbers_or_a_grid_and_gives_ten_scores_and_the_digit(net):
    rt, c = runtime(net, "weights:digits")
    x, y = WS.sets(net, WS.manifest(net, "digits"), "practice")
    flat = [float(v) for v in x[0]]
    a = c.post("/predict", json={"inputs": {"input": flat}, "traffic": "test"}, headers=AUTH).json()
    b = c.post("/predict", json={"inputs": {"input": [flat[r * 28:(r + 1) * 28] for r in range(28)]}}, headers=AUTH).json()
    s = c.post("/predict", json={"inputs": flat}, headers=AUTH).json()
    assert a["outputs"] == b["outputs"] == s["outputs"] and a["model_version"] == "weights:digits@v1" and a["arm"] == "candidate" and a["ms"] >= 0
    out = a["outputs"]
    assert len(out["scores"]) == 10 and list(out["named"]) == [str(i) for i in range(10)]
    assert out["chosen"] == str(out["chosen_index"]) and out["scores"][out["chosen_index"]] == max(out["scores"])
    assert out["chosen_index"] == int(y[0])  # the starter names its own first practice picture
    assert rt.status()["serves"] == "predict" and rt.status()["traffic"] == {"real": 0, "unlabelled": 2, "simulated": 0, "test": 1}


def test_inputs_by_name_and_a_single_yes_or_no_output(net):
    _, c = runtime(net, "weights:tiny@v1")
    hi = c.post("/predict", json={"inputs": {"x1": 0.9, "x2": 0.1}}, headers=AUTH).json()["outputs"]
    lo = c.post("/predict", json={"inputs": {"input": [0.9, 0.9]}}, headers=AUTH).json()["outputs"]
    assert hi["yes"] is True and lo["yes"] is False and list(hi["named"]) == ["exactly one is high"] and "chosen" not in hi


@pytest.mark.parametrize("body, says", [
    ({}, 'A predict request needs "inputs". This model takes a list of 2 numbers, or one number for each name: x1, x2'),
    ({"inputs": {"x1": 0.5}}, "The request is missing x2. This model needs all 2: x1, x2."),
    ({"inputs": {"x1": 0.5, "x2": 0.1, "x3": 1}}, "This model has no input named 'x3'. Its inputs are: x1, x2."),
    ({"inputs": [0.1, 0.2, 0.3]}, "This model takes 2 numbers. The request sent 3."),
    ({"inputs": [0.1, "high"]}, 'The inputs must be numbers. Number 2 is "high".'),
    ({"inputs": [0.1, True]}, "The inputs must be numbers. Number 2 is true."),
    ({"inputs": "0.1,0.2"}, "The input must be a list of numbers."),
    ({"inputs": [[0.1], [0.2]]}, "This model takes one list of 2 numbers, not rows of them."),
    ({"inputs": {"pixels": [0.1, 0.2]}}, "The request names 'pixels'. This model takes"),
])
def test_a_request_that_does_not_fit_is_refused_in_a_sentence(net, body, says):
    rt, c = runtime(net, "weights:tiny")
    r = c.post("/predict", json=body, headers=AUTH)
    assert r.status_code == 400 and says in r.json()["detail"], r.json()
    assert "Traceback" not in r.text and "Error" not in r.json()["detail"]
    assert rt.refused == 1 and rt.errors == 0 and rt.requests == 0


def test_a_grid_of_the_wrong_size_and_a_missing_key_are_refused(net):
    _, c = runtime(net, "weights:digits")
    r = c.post("/predict", json={"inputs": {"input": [[0.0] * 28] * 27}}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"] == "This model takes a grid of 28 by 28 numbers. The request sent 27 rows of 28."
    assert c.post("/predict", json={"inputs": [0.0] * 784}).status_code == 401


def test_a_network_refuses_rank_and_says_where_to_send_it(net):
    rt, c = runtime(net, "weights:tiny")
    r = c.post("/rank", json={"query": "boots"}, headers=AUTH)
    assert r.status_code == 400 and "not a search ranker. Send it POST /predict. Takes 2 numbers (x1, x2)." in r.json()["detail"]
    assert rt.errors == 0  # refused for what it sent, not counted as a failure of the model
    d = c.post("/describe", json={}, headers=AUTH).json()
    assert d["serves"] == "predict" and d["route"] == "/predict" and d["example"] == {"inputs": {"x1": 0.5, "x2": 0.5}}


def test_the_one_route_of_sagemaker_takes_predict_too(net):
    _, c = runtime(net, "weights:tiny")
    r = c.post("/invocations", json={"op": "predict", "key": KEY, "inputs": {"x1": 0.9, "x2": 0.1}, "traffic": "test"})
    assert r.status_code == 200 and r.json()["outputs"]["yes"] is True
    assert c.post("/invocations", json={"op": "describe", "key": KEY}).json()["serves"] == "predict"
    assert c.post("/invocations", json={"op": "predict", "key": "wrong", "inputs": [0.9, 0.1]}).status_code == 401


def test_the_runtime_serves_the_base_plus_the_saved_edits(net):
    saved = edited(net, "tiny")  # the output's bias pushed far down: every answer turns to no
    assert saved["ref"] == "tiny@v2" and saved["base"] == "tiny@v1"
    _, v1 = runtime(net, "weights:tiny@v1")
    rt, v2 = runtime(net, "weights:tiny@v2")
    body = {"inputs": {"x1": 0.9, "x2": 0.1}}
    assert v1.post("/predict", json=body, headers=AUTH).json()["outputs"]["yes"] is True
    after = v2.post("/predict", json=body, headers=AUTH).json()
    assert after["outputs"]["yes"] is False and after["model_version"] == "weights:tiny@v2"
    # The bundle holds the version it started from and the edits, not a second copy of the numbers.
    files = sorted(k.rsplit("/weights/", 1)[1] for k in net.store.list(rt.bundle_key + "/candidate/weights/"))
    assert files == ["line.json", "manifest.json", "tensors.safetensors"]
    # An edit of the edit is followed back to the start.
    s = WSN.open_session(net, "tiny", by="agent", name="tester")
    assert s["version"] == 2
    WSN.edit(net, s["id"], {"op": "set", "layer": 2, "neuron": 0, "from": None, "value": 9.0, "why": "and back up"}, by="agent")
    assert WSN.save(net, s["id"])["ref"] == "tiny@v3"
    _, v3 = runtime(net, "weights:tiny")
    import numpy as np

    from captain_search.weights import files as F
    from captain_search.weights import net as N

    m, tensors = WS.load(net, "tiny", 3)
    want = float(N.predict(F.network_from(m, tensors), np.asarray([[0.9, 0.9]]))[0][0])
    got = v3.post("/predict", json={"inputs": [0.9, 0.9]}, headers=AUTH).json()
    assert got["model_version"] == "weights:tiny@v3" and abs(got["outputs"]["scores"][0] - want) < 1e-6
    assert got["outputs"]["scores"][0] > v2.post("/predict", json={"inputs": [0.9, 0.9]}, headers=AUTH).json()["outputs"]["scores"][0]
    assert PD.carries(net, "weights:tiny@v3")["line"] == ["tiny@v2", "tiny@v3"]


def test_a_bundle_whose_numbers_were_changed_is_not_loaded(net):
    import json

    edited(net, "tiny")
    key, _ = SG.build_bundle(net, "shop", "staging", {"candidate": "weights:tiny@v2"}, "candidate")
    path = f"{key}/candidate/weights/line.json"
    line = json.loads(net.store.get(path))
    line["steps"][0]["patches"][0]["new"] = [1.0]

    class Tampered:
        def __getattr__(self, name):
            return getattr(net.store, name)

        def get(self, k):
            return json.dumps(line).encode() if k == path else net.store.get(k)

    with pytest.raises(ValueError, match="does not match the numbers recorded when it was saved"):
        PD.load(Tampered(), key, "candidate", json.loads(net.store.get(f"{key}/bundle.json"))["arms"]["candidate"])


def test_the_log_line_of_a_predict_request_is_short_and_readable(net):
    rt, c = runtime(net, "weights:digits")
    x, _ = WS.sets(net, WS.manifest(net, "digits"), "practice")
    rid = c.post("/predict", json={"inputs": [float(v) for v in x[0]], "traffic": "test", "session_id": "s1"}, headers=AUTH).json()["request_id"]
    rt.flush()
    got = L.read(net, "shop", "staging", rt.stage, extra=True)
    r = got["requests"][0]
    assert (r["request_id"], r["kind"], r["traffic"], r["session_id"], r["arm"], r["model_version"]) == (rid, "predict", "test", "s1", "candidate", "weights:digits@v1")
    assert r["ids"] == [] and r["ms"] >= 0 and r["copy"] == rt.copy
    inp, out = r["predict"]["inputs"]["input"], r["predict"]["outputs"]
    assert inp["count"] == 784 and inp["shape"] == [28, 28] and len(inp["first"]) == 8 and "min" in inp  # not 784 numbers a line
    assert len(out["scores"]) == 10 and out["chosen"] == r["predict"]["text"].split("chose ")[1].split(",")[0]
    assert r["predict"]["text"].startswith("In: a 28 by 28 grid, ") and ". Out: chose " in r["predict"]["text"]
    assert any(ln["text"].startswith("Answered with the candidate in ") and ln.get("request_id") == rid for ln in got["lines"])
    assert L.impressions(got["requests"]) == []  # nothing here is a product placed for a shopper


# ---- on this computer: the same deploy, record, log, stop, start and roll back ----------------------------


def test_one_call_deploys_a_weights_version_on_this_computer(net):
    assert DP.offers(net, "shop")["hosts"][0]["can_deploy"] is False  # a project with no ranker and nothing named
    o = DP.offers(net, "shop", candidate="weights:digits")
    assert o["model_version"] == "weights:digits@v1" and o["beside"] is None and o["hosts"][0]["can_deploy"]
    p = DP.plan(net, "shop", "local", candidate="weights:digits")
    assert [s["id"] for s in p["steps"]] == ["check", "access", "image", "start", "ready", "match"] and not p["needs_confirm"]
    seen = []
    out = DP.run(net, "shop", "local", candidate="weights:digits", on_event=lambda kind, **d: seen.append((kind, d)))
    assert out["status"] == "done" and out["address"].startswith("http://127.0.0.1:") and out["soft_failed"] == []
    assert [(s["id"], s["status"]) for s in out["steps"]] == [("check", "done"), ("access", "skipped"), ("image", "skipped"), ("start", "done"),
                                                              ("ready", "done"), ("match", "done")]
    lines = [d["text"] for k, d in seen if k == "line"]
    assert "Sent 5 test requests through it. They are marked as test traffic, never as real requests." in lines
    assert "The deployed model gives the same answers as digits v1 on 5 practice inputs." in lines
    rec = SG.state(net, "shop")
    assert (rec["status"], rec["kind"], rec["host"], rec["serves"], rec["model_version"], rec["arms"]) == ("running", "local", "local", "predict", "weights:digits@v1", {"candidate": "weights:digits@v1"})
    assert rec["carries"]["started_from"] == "digits@v1" and rec["carries"]["changed"] == 0 and "carries no edits" in rec["carries"]["text"]
    # Health and status, as for any deployment.
    st = SG.status(net, "shop")
    assert st["status"] == "running" and st["health"]["serves"] == "predict" and st["health"]["requests"] == 10
    assert "weights:digits@v1 is running on this computer" in st["text"]
    # A request through the same door, and the request list.
    x, _ = WS.sets(net, WS.manifest(net, "digits"), "practice")
    r = SG.call(net, rec, "predict", {"inputs": [float(v) for v in x[3]], "traffic": "test"})
    assert len(r["outputs"]["scores"]) == 10
    rows = TF.requests(net, "shop", refresh=True)
    assert rows["total"] == 11 and rows["rows"][0]["request_id"] == r["request_id"] and rows["rows"][0]["traffic"] == "test"
    assert rows["rows"][0]["kind"] == "predict" and rows["rows"][0]["summary"].startswith("In: a 28 by 28 grid") and rows["rows"][0]["query"] is None
    one = TF.request(net, "shop", r["request_id"])
    assert one["kind"] == "predict" and one["outputs"]["chosen"] == r["outputs"]["chosen"] and one["inputs"]["input"]["count"] == 784 and one["results"] == []
    # Stop, then start again: the same deploy.
    assert SG.unstage(net, "shop")["status"] == "stopped"
    again = DP.run(net, "shop", "local", candidate="weights:digits@v1")
    assert again["status"] == "done" and SG.state(net, "shop")["status"] == "running"
    assert DP.remove(net, "shop")["status"] == "removed" and SG.state(net, "shop") is None


def test_an_edited_version_deploys_over_the_first_and_rolls_back(net):
    from captain_search.staging import rollback as RB

    assert DP.run(net, "shop", "local", candidate="weights:tiny")["status"] == "done"
    body = {"inputs": {"x1": 0.9, "x2": 0.1}, "traffic": "test"}
    assert SG.call(net, SG.state(net, "shop"), "predict", body)["outputs"]["yes"] is True
    edited(net, "tiny")
    out = DP.run(net, "shop", "local", candidate="weights:tiny@v2")
    assert out["status"] == "done" and out["model_version"] == "weights:tiny@v2"
    rec = SG.state(net, "shop")
    assert rec["previous"] == "weights:tiny@v1" and rec["carries"]["base"] == "tiny@v1" and rec["carries"]["changed"] == 1
    assert rec["carries"]["edits"][0]["why"] == "to see the answer move" and rec["carries"]["text"] == "tiny v2 is tiny v1 plus 1 changed number in 1 edit."
    assert SG.call(net, rec, "predict", body)["outputs"]["yes"] is False
    assert [r["model_version"] for r in TF.versions(net, "shop")["rows"]] == ["weights:tiny@v2", "weights:tiny@v1"]
    back = RB.go(net, "shop", reason="the edit made every answer no")
    assert back["rolled_back_to"] == "weights:tiny@v1" and back["from"] == "weights:tiny@v2"
    rec = SG.state(net, "shop")
    assert rec["model_version"] == "weights:tiny@v1" and rec["serves"] == "predict" and rec["rollbacks"][0]["reason"] == "the edit made every answer no"
    assert SG.call(net, rec, "predict", body)["outputs"]["yes"] is True


def test_a_version_that_is_not_there_is_refused_before_anything_starts(net):
    with pytest.raises(ValueError, match="There is no version v9 of the weights model tiny"):
        DP.run(net, "shop", "local", candidate="weights:tiny@v9")
    with pytest.raises(ValueError, match="There is no weights model named nothing-here"):
        DP.plan(net, "shop", "local", candidate="weights:nothing-here")
    assert SG.state(net, "shop") is None
