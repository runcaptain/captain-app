import json

import boto3
import keyring
import pytest
import yaml
from keyring.backend import KeyringBackend
from moto import mock_aws

from captain_search import compute as C
from captain_search import demo, historic as H, models as MD, ops
from captain_search.workspace import init_workspace


class MemKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.d = {}

    def set_password(self, s, u, p):
        self.d[(s, u)] = p

    def get_password(self, s, u):
        return self.d.get((s, u))

    def delete_password(self, s, u):
        self.d.pop((s, u), None)


@pytest.fixture(autouse=True)
def memkeyring():
    old = keyring.get_keyring()
    keyring.set_keyring(MemKeyring())
    yield
    keyring.set_keyring(old)


@pytest.fixture
def ctx(tmp_path):
    c = init_workspace("t", str(tmp_path / "bucket"))
    demo.seed(c, progress=lambda s: None)
    return c


def test_historic_demo_is_simulated_and_flags_coverage(ctx):
    run = ctx.registry.load("backtest-run", "demo-historic-replay")
    assert run.mode == "historic"
    assert {a.evidence.value for a in run.arms} == {"SIMULATED"}  # synthetic logs never upgrade evidence
    assert all("ndcg@10_tw" in a.offline and "coverage" in a.offline for a in run.arms)
    assert any(c["metric"] == "ndcg@10_tw" and c["primary"] for c in run.comparisons)


def test_real_logs_give_logged_clicks_evidence(ctx, tmp_path):
    from captain_search.data.datasets import load_table

    rows = load_table(ctx, ctx.registry.load("dataset", "logs/demo-search-sessions")).to_pylist()
    f = tmp_path / "real.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows[:20000]))
    from captain_search.data.datasets import read_file, snapshot
    from captain_search.model.entities import DatasetKind, Provenance

    snapshot(ctx, "logs/real", read_file(str(f)), kind=DatasetKind.events, provenance=Provenance(source="file", file=str(f)))
    j = H.judgments_from_logs(ctx, "logs/real", "judgments/real")
    assert j.judgment_source == "click" and not j.tags.get("synthetic")
    bt = ops.backtest_plan(ctx, "real-replay", ["demo-bm25@v1", "demo-tuned@v1"], None, "judgments/real@v1", None, "h",
                           mode="historic", logs="logs/real")
    run = ops.backtest_run(ctx, bt.name)
    assert {a.evidence.value for a in run.arms} == {"LOGGED_CLICKS"}


def test_coec_grades_remove_position_bias():
    rows = []
    for i in range(200):  # p1 always shown at rank 1, clicked at the rank-1 average: grade 2
        rows.append({"query": "tent", "product_id": "p1", "position": 1, "clicked": i % 2 == 0, "purchased": False})
        # p9 shown at rank 5 and clicked far above that rank's rate: relevant despite the low rank
        rows.append({"query": "tent", "product_id": "p9", "position": 5, "clicked": i % 4 == 0, "purchased": False})
        rows.append({"query": "tent", "product_id": "p5", "position": 5, "clicked": False, "purchased": False})
    g = {r["product_id"]: r["grade"] for r in H.grades(rows)[0]}
    assert g["p9"] >= 2 and g["p5"] == 0


def test_dataset_folders_in_tree(ctx):
    from fastapi.testclient import TestClient

    from captain_search import server

    server._ctx = None
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    t = c.get("/api/datasets/tree", headers=h).json()
    logs = next(f for f in t["folders"] if f["name"] == "logs")
    assert logs["datasets"][0]["leaf"] == "demo-search-sessions"
    hist = next(f for f in t["folders"] if f["name"] == "demo-historic-replay")
    assert {d["leaf"] for d in hist["datasets"]} == {"queries", "judgments"}


def test_cloud_profile_secrets_never_in_config(ctx, tmp_path):
    from captain_search import cloud
    from captain_search.workspace import config_path

    cloud.add_profile("prod-aws", "aws", "keys", "us-west-1", {"access_key_id": "AKIAEXAMPLE"},
                      {"secret_access_key": "s3cr3t-value"})
    assert "s3cr3t-value" not in open(config_path()).read()
    assert cloud.get_profile("prod-aws")["secret_fields"] == ["secret_access_key"]
    with pytest.raises(ValueError, match="required"):
        cloud.add_profile("bad", "gcp", "service-account", None, {"project": "p"}, {})
    with pytest.raises(ValueError, match="secret field"):
        cloud.add_profile("bad", "aws", "keys", None, {"access_key_id": "a", "secret_access_key": "x"}, {})
    s = cloud.aws_session("prod-aws")
    assert s.get_credentials().secret_key == "s3cr3t-value"


def test_compute_plans(ctx):
    with pytest.raises(ValueError, match="needs a aws cloud profile"):
        C.add(ctx, "batch", "aws-batch", None, "us-west-1", {"job_queue": "q", "job_definition": "d"})
    t = C.add(ctx, "batch", "aws-batch", "prod-aws", "us-west-1", {"job_queue": "q", "job_definition": "d"})
    with pytest.raises(ValueError, match="cloud workspace"):
        C.plan_backtest(ctx, t, "demo-tuned-vs-bm25")  # local workspace
    ctx.workspace.storage_uri = "s3://bucket/ws"
    plan = C.plan_backtest(ctx, t, "demo-tuned-vs-bm25")
    a = plan[0]["args"]
    assert plan[0]["call"] == "submit_job" and a["jobQueue"] == "q"
    assert a["containerOverrides"]["command"][-3:] == ["backtest", "run", "demo-tuned-vs-bm25"]
    sm = C.add(ctx, "sm", "aws-sagemaker", "prod-aws", "us-west-1", {"image": "123.dkr.ecr/x:1", "execution_role_arn": "arn:role"})
    sp = C.plan_serve(ctx, sm, "demo-tuned@v1", "prod")
    assert [s["call"] for s in sp] == ["create_model", "create_endpoint_config", "update_endpoint_or_create"]
    assert sp[0]["args"]["PrimaryContainer"]["Environment"]["CST_PIPELINE"] == "demo-tuned@v1"
    with pytest.raises(ValueError, match="cannot serve"):
        ops.env_create(ctx, "bad", "live", {"type": "record"}, compute="batch")
    ops.env_create(ctx, "prod-sm", "demo", {"type": "record"}, compute="sm")
    d = ops.promote(ctx, "demo-tuned@v1", "prod-sm", dry_run=True)
    assert d.gate_results["target"]["executed"] is False and d.gate_results["target"]["type"] == "aws-sagemaker"


REG = {
    "arch-v1-a/brand1/v5log/MANIFEST.yaml": {"model": "b1", "status": "current: live", "parent": "brand1 v3 (x) and v2", "attached_to_demo": True, "repro_status": "not-tested"},
    "arch-v2-b/brand1/v6/MANIFEST.yaml": {"status": "candidate: passed", "parent": "brand1 v5log", "repro_status": "not-tested",
                                          "results": [{"label": "SIMULATED", "text": "conv +4%"}]},
    "arch-v2-b/brand2/v1/MANIFEST.yaml": {"status": "unattached: failed gate", "parent": "none, first model", "repro_status": "exact-verified"},
}


@mock_aws
def test_registry_import_and_model_lineage(ctx):
    s3 = boto3.client("s3", region_name="us-west-1")
    s3.create_bucket(Bucket="reg", CreateBucketConfiguration={"LocationConstraint": "us-west-1"})
    for k, v in REG.items():
        s3.put_object(Bucket="reg", Key=k, Body=yaml.safe_dump(v).encode())
        s3.put_object(Bucket="reg", Key=k.replace("MANIFEST.yaml", "weights/w.bin"), Body=b"x" * 10)
    snap = MD.import_registry(ctx, "s3://reg", s3=s3)
    assert len(snap.models) == 3
    m = {x["id"]: x for x in MD.listing(ctx)}
    assert m["brand1@v6"]["parent_id"] == "brand1@v5log" and m["brand1@v6"]["status_kind"] == "candidate"
    assert m["brand1@v5log"]["parent_ids"] == ["brand1@v3", "brand1@v2"]
    assert m["brand2@v1"]["parent_ids"] == []
    g = MD.lineage(ctx)
    kinds = {n["id"]: n["kind"] for n in g["nodes"]}
    assert kinds["model:brand1@v3"] == "model-unregistered" and kinds["demo:brand1"] == "demo"
    assert {"src": "model:brand1@v5log", "dst": "model:brand1@v6", "relation": "parent"} in g["edges"]
    assert MD.detail(ctx, "brand1@v6")["files"][0]["path"] == "MANIFEST.yaml"


def test_coding_agent_folder(ctx):
    from captain_search import codeagent

    info = codeagent.prepare(ctx)
    import os

    assert os.path.exists(os.path.join(info["cwd"], "CLAUDE.md"))
    assert os.path.exists(os.path.join(info["cwd"], ".claude", "skills", "backtest-readout", "SKILL.md"))
    assert [a["id"] for a in info["agents"]] == ["shell"] and info["shell"]["command"][0].endswith("sh")


def test_webhook_dry_run_calls_nothing(ctx, monkeypatch):
    import httpx

    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("called")))
    ops.env_create(ctx, "wh", "demo", {"type": "webhook", "url": "https://example.invalid/hook"})
    d = ops.promote(ctx, "demo-tuned@v1", "wh", dry_run=True)
    assert called == [] and d.gate_results["target"]["executed"] is False
    with pytest.raises(ValueError, match="failed"):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no route")))
        ops.promote(ctx, "demo-tuned@v1", "wh")
    with pytest.raises(ValueError, match="compute target"):
        ops.env_create(ctx, "sm", "demo", {"type": "sagemaker-endpoint"})


def test_extract_comparisons_from_readout_shapes():
    from captain_search.brands import extract_comparisons

    data = {"ab": {"all": {"conversion": {"baseline": 0.02, "model": 0.0216, "rel": 0.081, "rel_ci": [0.052, 0.115]},
                           "aov": {"rel": -0.004, "rel_ci": [-0.07, 0.06]}}},
            "arms": {"m": {"ctr": [0.31, 0.30, 0.32], "clicks_lift": [4.2, -1.0, 9.0]}},
            "sess": {"b_minus_a": 0.0119, "lo": 0.0065, "hi": 0.0174, "n": 900}}
    c = {x["metric"]: x for x in extract_comparisons(data)}
    assert c["ab.all.conversion.rel"]["verdict"] == "win"
    assert c["ab.all.aov.rel"]["verdict"] == "noise"
    assert c["arms.m.clicks_lift"]["verdict"] == "noise"
    assert c["sess.b_minus_a"]["verdict"] == "win"


def test_commands_tree_and_run(ctx):
    from captain_search import commands as CMD

    paths = {" ".join(c["path"]) for c in CMD.tree()}
    assert {"data list", "backtest plan", "workflow run", "deploy promote"} <= paths and "serve" not in paths
    r = CMD.run(["data", "list"], {"kind": "catalog"}, workspace="t")
    assert r["exit_code"] == 0 and r["output"][0]["name"] == "demo-catalog"
    r = CMD.run(["sql"], {"conn_name": "demo-store", "execute": "drop table products"}, workspace="t")
    assert r["exit_code"] == 1 and "not allowed" in r["error"]
    with pytest.raises(ValueError):
        CMD.run(["serve"], {}, workspace="t")


def test_workflow_template_runs_end_to_end(ctx):
    from captain_search import workflows as WF

    t = WF.TEMPLATES["simulated-backtest"]
    WF.save(ctx, "wf1", t["steps"], {**t["params"], "name": "wfx"})
    r = WF.run(ctx, "wf1")
    assert r.status == "done", r.steps
    assert ctx.registry.load("backtest-run", "wfx").arms[1].arm == "demo-tuned@v1"
    with pytest.raises(ValueError, match="cycle"):
        WF.save(ctx, "bad", [{"id": "a", "command": ["data", "list"], "needs": ["b"]}, {"id": "b", "command": ["data", "list"], "needs": ["a"]}])
    assert WF.due("*/15 6 * * *", __import__("datetime").datetime(2026, 11, 6, 6, 30))


def test_browse_pages_sorts_filters(ctx):
    from captain_search import browse

    m = ctx.registry.load("dataset", "demo-catalog")
    r = browse.rows(ctx, m, offset=470, limit=20)
    assert len(r["rows"]) == 10 and not r["has_more"] and r["total"] == 480
    r = browse.rows(ctx, m, limit=5, order="price", desc=True, where="price < 100", count=True)
    prices = [row[[c["name"] for c in r["columns"]].index("price")] for row in r["rows"]]
    assert prices == sorted(prices, reverse=True) and max(prices) < 100 and r["total"] > 0
    with pytest.raises(ValueError, match="not allowed"):
        browse.rows(ctx, m, where="read_text('/etc/passwd') is not null")
    with pytest.raises(ValueError, match="subqueries"):
        browse.rows(ctx, m, where="price in (select 1)")


def test_dataset_classes_are_team_defined(ctx):
    from captain_search import classes as CL

    names = {c["name"] for c in CL.listing(ctx)}
    assert {"catalog", "queries", "judgments", "events"} <= names
    CL.upsert(ctx, {"name": "vendor-feed", "description": "partner feeds", "role": "catalog", "required_columns": "id,title,price"}, True)
    CL.upsert(ctx, {"name": "pii-reviewed", "color": "#123456"}, True)
    r = CL.classify(ctx, "demo-catalog", ["vendor-feed", "pii-reviewed"])
    assert r["role"] == "catalog" and r["warnings"] == []
    assert CL.classes_of(ctx, "demo-catalog") == ["vendor-feed", "pii-reviewed"]
    with pytest.raises(ValueError, match="one role"):
        CL.classify(ctx, "demo-catalog", ["vendor-feed", "queries"])
    with pytest.raises(ValueError, match="Reclassify"):
        CL.delete(ctx, "vendor-feed")
    w = CL.classify(ctx, "demo-queries", ["vendor-feed"])["warnings"]
    assert w and "expects columns" in w[0]
    assert CL.classes_of(ctx, "demo-judgments") == ["judgments"]  # unclassified falls back to its role
