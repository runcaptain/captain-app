"""A weights model version through each cloud host's deploy path: Modal, Google Cloud Run, Azure Container Apps
and AWS SageMaker. Every cloud here is the stand-in the other hosting tests use, which starts the real runtime over
the bundle the deploy uploaded. No real cloud is called, no credentials are read and nothing leaves this computer."""
import pytest

from captain_search import compute as C
from captain_search import staging as SG
from captain_search.staging import deploy as DP
from captain_search.staging import logs as L
from captain_search.staging import traffic as TF
from captain_search.weights import session as WSN
from captain_search.weights import store as WS

from test_deploy_one_call import LOGINS, logins  # noqa: F401  (fixture)
from test_hosting_clouds import azure, google  # noqa: F401  (fixtures)
from test_hosting_modal import modal  # noqa: F401  (fixture)
from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import aws  # noqa: F401  (fixture)

YES = {"inputs": {"x1": 0.9, "x2": 0.1}, "traffic": "test"}


def _edit(ctx):
    s = WSN.open_session(ctx, "tiny", by="agent", name="tester")
    WSN.edit(ctx, s["id"], {"op": "set", "layer": 2, "neuron": 0, "from": None, "value": -9.0, "why": "to see the answer move"}, by="agent")
    return WSN.save(ctx, s["id"])["ref"]


def _served_alike(ctx, host, lines):
    """What every host must show after a deploy of tiny v1: the record, a request, its log line, and the match step."""
    rec = SG.state(ctx, "shop")
    assert (rec["status"], rec["host"], rec["serves"], rec["model_version"]) == ("running", host, "predict", "weights:tiny@v1")
    assert rec["arms"] == {"candidate": "weights:tiny@v1"} and rec["carries"]["started_from"] == "tiny@v1"
    assert "The deployed model gives the same answers as tiny v1 on 5 practice inputs." in lines
    r = SG.call(ctx, rec, "predict", YES)
    assert r["outputs"]["yes"] is True and r["model_version"] == "weights:tiny@v1"
    SG.call(ctx, rec, "flush", {})
    got = L.read(ctx, "shop", "staging", rec["id"])["requests"]
    assert len(got) == 11 and {x["kind"] for x in got} == {"predict"} and {x["traffic"] for x in got} == {"test"}
    mine = next(x for x in got if x["request_id"] == r["request_id"])
    assert mine["predict"]["text"] == "In: x1 0.9, x2 0.1. Out: exactly one is high: yes, score " + f"{r['outputs']['scores'][0]:.3g}."
    return rec


@pytest.mark.parametrize("host", ["modal", "gcp", "azure"])
def test_a_weights_version_deploys_on_each_cloud_with_an_address(ws, logins, request, host):  # noqa: F811
    request.getfixturevalue({"modal": "modal", "gcp": "google", "azure": "azure"}[host])  # one stand-in at a time: each takes over the network calls
    logins.append(LOGINS[host])
    p = DP.plan(ws, "shop", host, candidate="weights:tiny")
    assert p["model_version"] == "weights:tiny@v1" and p["beside"] is None and p["needs_confirm"] and p["first_time"]
    assert [s["id"] for s in p["steps"]] == ["check", "access", "image", "start", "ready", "match"]
    lines = []
    out = DP.run(ws, "shop", host, candidate="weights:tiny", confirm=True, on_event=lambda k, **d: lines.append(d.get("text")))
    assert out["status"] == "done" and out["address"].startswith("https://") and out["soft_failed"] == []
    assert [s["status"] for s in out["steps"]][3:] == ["done", "done", "done"]
    rec = _served_alike(ws, host, lines)
    # The ranker's routes are still there and still say what this deployment is.
    with pytest.raises(Exception, match="not a search ranker. Send it POST /predict"):
        SG.call(ws, rec, "rank", {"query": "boots"})
    # An edited version over the first, then back.
    assert _edit(ws) == "tiny@v2"
    out = DP.run(ws, "shop", host, candidate="weights:tiny@v2", confirm=True)
    assert out["status"] == "done" and not out["plan"]["first_time"]
    rec = SG.state(ws, "shop")
    assert rec["carries"]["text"] == "tiny v2 is tiny v1 plus 1 changed number in 1 edit." and SG.call(ws, rec, "predict", YES)["outputs"]["yes"] is False
    back = TF.rollback_to(ws, "shop", None, reason="the edit made every answer no")
    assert back["rolled_back_to"] == "weights:tiny@v1" and SG.call(ws, SG.state(ws, "shop"), "predict", YES)["outputs"]["yes"] is True
    assert DP.remove(ws, "shop")["status"] == "removed" and SG.state(ws, "shop") is None


def test_a_weights_version_on_sagemaker_is_called_through_its_one_route(ws, aws):  # noqa: F811
    WS.ensure_starters(ws)
    p = SG.plan(ws, "shop", candidate="weights:tiny", target="sm")
    assert p["arms"] == {"candidate": "weights:tiny@v1"} and p["needs_confirm"] and p["how"] == "captain"
    r = SG.stage(ws, "shop", candidate="weights:tiny", target="sm", confirm=True)
    assert r["status"] == "running" and r["serves"] == "predict" and r["kind"] == "aws-sagemaker" and r["endpoint"]["name"]
    rec = SG.state(ws, "shop")
    got = SG.call(ws, rec, "predict", YES)
    assert got["outputs"]["yes"] is True and got["model_version"] == "weights:tiny@v1"
    assert SG.call(ws, rec, "describe", {})["example"] == {"inputs": {"x1": 0.5, "x2": 0.5}}
    with pytest.raises(Exception, match="The request is missing x2"):
        SG.call(ws, rec, "predict", {"inputs": {"x1": 0.5}})
    with pytest.raises(Exception):
        C.invoke_staged(ws, C.get(ws, "sm"), rec, {"op": "predict", "key": "wrong", "inputs": [0.9, 0.1]})
    # The edited version moves onto the same endpoint.
    assert _edit(ws) == "tiny@v2"
    r2 = SG.stage(ws, "shop", candidate="weights:tiny@v2", target="sm", confirm=True)
    assert r2["endpoint"]["name"] == r["endpoint"]["name"] and r2["previous"] == "weights:tiny@v1" and r2["carries"]["changed"] == 1
    assert SG.call(ws, SG.state(ws, "shop"), "predict", YES)["outputs"]["yes"] is False
    assert SG.unstage(ws, "shop")["status"] == "stopped" and not aws.endpoints


def test_a_ranker_deploys_beside_all_this_exactly_as_before(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    out = DP.run(ws, "shop", "modal", confirm=True)
    rec = SG.state(ws, "shop")
    assert out["status"] == "done" and rec["model_version"] == ws.candidate and "serves" not in rec and "carries" not in rec
    r = SG.call(ws, rec, "rank", {"query": "jacket", "k": 5, "traffic": "test"})
    assert len(r["ids"]) == 5 and set(r) == {"request_id", "ids", "scores", "arm", "model_version", "ms"}
    assert "serves" not in SG.call(ws, rec, "status", {})
    with pytest.raises(Exception, match="a search ranker. Send it POST /rank with a query"):
        SG.call(ws, rec, "predict", {"inputs": [0.1, 0.2]})
