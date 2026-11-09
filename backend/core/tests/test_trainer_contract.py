"""The trainer contract (CST_TRAIN_INPUTS, CST_PARAMS, CST_OUT/result.yaml) and the cloud retrain that waits for its job."""
import json
import sys
from types import SimpleNamespace as NS

import pyarrow as pa
import pytest
import yaml

from captain_search import compute as C
from captain_search import compute_providers as P
from captain_search import jobs as J
from captain_search import ops
from captain_search import retrain as RT
from captain_search import workflows as WF
from captain_search.data.datasets import snapshot
from captain_search.model.entities import DatasetKind, PipelineVersion, Provenance
from captain_search.workspace import init_workspace

# A bring-your-own trainer: writes what it was given into its artifact and reports metrics through result.yaml.
TRAINER = r'''
import json, os, pathlib
out = pathlib.Path(os.environ["CST_OUT"])
inputs, params = json.loads(os.environ["CST_TRAIN_INPUTS"]), json.loads(os.environ["CST_PARAMS"])
(out / "model").mkdir()
(out / "model" / "seen.json").write_text(json.dumps({"inputs": inputs, "params": params}))
print("ndcg@10=0.41", flush=True)
(out / "result.yaml").write_text("metrics:\n  ndcg@10: 0.41\nparams:\n  trees: 50\nartifact_dir: model\n")
'''


@pytest.fixture
def ws(tmp_path):
    ctx = init_workspace("t", str(tmp_path / "store"))
    prov = Provenance(source="generated")
    snapshot(ctx, "shop-catalog", pa.table({"id": ["a", "b"], "title": ["red hat", "blue hat"]}),
             kind=DatasetKind.catalog, provenance=prov)
    j = snapshot(ctx, "shop-judgments", pa.table({"query": ["hat"], "product_id": ["a"], "grade": [3]}),
                 kind=DatasetKind.judgments, provenance=prov)
    ctx.registry.publish(j.model_copy(update={"judgment_source": "llm", "version": 0, "parent": None}))  # v2, llm
    (tmp_path / "train.py").write_text(TRAINER)
    spec = {"train": {"command": f"{sys.executable} {tmp_path / 'train.py'}",
                      "inputs": ["shop-catalog", {"ref": "shop-judgments@v2", "role": "labels", "as_of": "2026-10-01"}],
                      "params": {"lr": 0.1}}}
    ops.pipeline_publish(ctx, {"name": "ltr", "engine": "local-bm25", "catalog": "shop-catalog", "spec": spec})
    return ctx


def test_byo_trainer_gets_exact_refs_and_as_of_and_metrics_are_stored(ws):
    r = RT.retrain(ws, "ltr", local_exec=True, params={"lr": 0.2})
    new = ws.registry.load("pipeline", "ltr", r["version"])
    seen = json.loads(ws.registry.get_blob(next(f for f in new.files if f.path == "artifact/seen.json")))
    by_role = {i["role"]: i for i in seen["inputs"]}
    assert by_role["catalog"]["ref"] == "shop-catalog@v1" and by_role["catalog"]["as_of"] is None
    assert by_role["labels"]["ref"] == "shop-judgments@v2" and by_role["labels"]["as_of"] == "2026-10-01"
    assert by_role["labels"]["label_source"] == "llm" and by_role["catalog"]["label_source"] is None
    assert by_role["labels"]["uri"].endswith("datasets/shop-judgments/v2") or "shop-judgments/v2" in by_role["labels"]["uri"]
    assert by_role["catalog"]["files"][0]["uri"].startswith(ws.store.uri)
    assert seen["params"] == {"lr": 0.2}  # the command line beats spec.train.params
    assert new.train_metrics == {"ndcg@10": 0.41} and new.train_params == {"lr": 0.2, "trees": 50}
    assert [i["ref"] for i in new.train_inputs] == ["shop-catalog@v1", "shop-judgments@v2"]
    assert "files" not in new.train_inputs[0]  # the version records refs, not every part
    assert new.parents == ["ltr@v1"] and new.source["kind"] == "retrain"
    assert {"spec.yaml", "REPRODUCE.md", "artifact/seen.json"} <= {f.path for f in new.files}


def test_explicit_inputs_replace_the_spec_and_as_of_fills_gaps(ws):
    r = RT.retrain(ws, "ltr", local_exec=True, inputs=["labels=shop-judgments@v1"], as_of="2026-09-30")
    new = ws.registry.load("pipeline", "ltr", r["version"])
    assert len(new.train_inputs) == 1
    got = new.train_inputs[0]
    assert {k: got[k] for k in ("ref", "kind", "role", "label_source", "as_of", "rows", "mode")} == {
        "ref": "shop-judgments@v1", "kind": "judgments", "role": "labels", "label_source": None, "as_of": "2026-09-30",
        "rows": 1, "mode": "snapshot"}
    assert got["fingerprint"].startswith("sha256:") and got["read_at"]  # what was read, and when
    with pytest.raises(Exception, match="no-such"):
        RT.retrain(ws, "ltr", local_exec=True, inputs=["no-such@v1"])


def test_old_versions_and_old_trainers_still_read(ws, tmp_path):
    old = ws.registry.load("pipeline", "ltr", 1).model_dump(mode="json")
    for k in ("train_inputs", "train_metrics", "train_params", "parents", "source"):
        old.pop(k)
    assert PipelineVersion.model_validate(old).train_metrics == {}  # a manifest from before the contract
    ops.pipeline_publish(ws, {"name": "legacy", "engine": "local-bm25", "catalog": "shop-catalog", "spec": {"train": {
        "command": "printf 'metrics:\\n  auc: 0.7\\ntraining_commit: abc\\n' > \"$CST_TRAIN_OUT\""}}})
    r = RT.retrain(ws, "legacy", local_exec=True)
    new = ws.registry.load("pipeline", "legacy", r["version"])
    assert new.training_commit == "abc" and new.train_metrics == {"auc": 0.7} and new.train_inputs == []


def test_unknown_result_keys_fail(ws):
    ops.pipeline_publish(ws, {"name": "bad", "engine": "local-bm25", "catalog": "shop-catalog", "spec": {"train": {
        "command": "printf 'weights: x\\n' > \"$CST_OUT/result.yaml\""}}})
    with pytest.raises(ValueError, match="unknown keys"):
        RT.retrain(ws, "bad", local_exec=True)


# ---- a cloud retrain, against a fake compute target ------------------------------------------------------------


class FakeBatch(P.ComputeProvider):
    """Runs nothing at submit. The first status call plays the job's container: it runs the submitted cst argv in
    this process with the job's environment, as the runner image would, then reports the outcome."""
    kind, provider, label = "fake-batch", "none", "Fake batch"
    jobs: dict = {}
    storage = ""

    def plan(self, job):
        return [{"service": "fake", "call": "run", "args": {"argv": job.argv}}]

    def submit(self, job):
        FakeBatch.jobs[job.id] = {"job": job, "state": None}
        return {"job_id": job.id}

    def status(self, h):
        j = FakeBatch.jobs[h["job_id"]]
        if j["state"] is None:
            from typer.testing import CliRunner

            from captain_search.cli.main import app

            # The plans name the workspace's cloud bucket. The "container" here reads the same store from disk.
            env = {**j["job"].env, "CST_STORAGE": FakeBatch.storage}
            res = CliRunner().invoke(app, ["--json", *j["job"].argv], env=env)
            j["state"] = "succeeded" if res.exit_code == 0 else "failed"
            j["out"] = res.output
        return P.status(j["state"], j.get("out", "")[-300:] if j["state"] == "failed" else "done")

    def logs(self, h, since=None):
        return {"lines": [], "next": None}

    def cancel(self, h):
        return P.status("stopped", "cancelled")

    def test(self):
        return {"ok": True, "detail": "fake"}


@pytest.fixture
def fake_target(ws, monkeypatch):
    eps = [NS(name="fake-batch", value="tests:FakeBatch", load=lambda: FakeBatch)]
    monkeypatch.setattr(P, "entry_points", lambda group: eps if group == "captain_search.compute" else [])
    monkeypatch.setattr(RT, "POLL_S", 0.01)
    FakeBatch.jobs.clear()
    FakeBatch.storage = ws.workspace.storage_uri
    ws.workspace.storage_uri = "s3://captain-test/cst"  # cloud compute needs a cloud workspace. The store stays on disk
    C.add(ws, "batch", "fake-batch", None, None, {})
    return ws


def test_cloud_retrain_is_pending_and_never_returns_the_old_version(fake_target):
    ws = fake_target
    r = RT.retrain(ws, "ltr", compute="batch")
    assert r["pending"] is True and "pipeline" not in r and r["job"] and r["live_id"]
    assert ws.registry.versions("pipeline", "ltr") == [1]  # nothing recorded before the job finishes
    argv = FakeBatch.jobs[r["job"]]["job"].argv
    assert "labels=shop-judgments@v2" in argv and "ltr@v1" in argv  # the job trains exactly what was resolved here
    done = RT.wait_job(ws, r["job"])
    assert done["pipeline"] == "ltr@v2" and done["parent"] == "ltr@v1"
    assert J.show(ws, r["job"])["result"]["pipeline"] == "ltr@v2"
    assert ws.registry.load("pipeline", "ltr", 2).train_metrics == {"ndcg@10": 0.41}


def test_cloud_retrain_wait_fails_when_the_job_fails(fake_target, tmp_path):
    ws = fake_target
    ops.pipeline_publish(ws, {"name": "boom", "engine": "local-bm25", "catalog": "shop-catalog", "spec": {"train": {"command": "exit 3"}}})
    with pytest.raises(ValueError, match="failed"):
        RT.retrain(ws, "boom", compute="batch", wait=True)
    assert ws.registry.versions("pipeline", "boom") == [1]


def test_retrain_workflow_waits_for_the_cloud_job(fake_target, monkeypatch):
    ws = fake_target
    # The workflow's steps load the workspace afresh from its local config, so skip the cloud-bucket check here.
    monkeypatch.setattr(C, "_check_cloud_workspace", lambda ctx, target: None)
    tpl = WF.TEMPLATES["retrain-and-review"]
    step = next(s for s in tpl["steps"] if s["id"] == "retrain")
    assert step["args"].get("wait") is True
    WF.save(ws, "rt", [{"id": "retrain", "command": ["pipeline", "retrain"],
                        "args": {**step["args"], "name": "ltr", "compute": "batch"}}])
    run = WF.run(ws, "rt")
    out = run.steps[0]
    assert run.status == "done", out.get("error") or out.get("output")
    assert out["output"]["pipeline"] == "ltr@v2" and out["output"]["pending"] is False
    assert yaml.safe_load(ws.store.get(f"jobs/{out['output']['job']}.result.yaml"))["pipeline"] == "ltr@v2"


def test_a_retrain_from_a_project_page_becomes_its_experiment(ws, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from captain_search import project_state as PS
    from captain_search import projects as PR
    from captain_search import server

    PR.create(ws, "Shop")
    monkeypatch.setattr(server, "_ctx", None)
    cl = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    r = cl.post("/api/pipelines/ltr/retrain", headers=h, json={"project": "shop", "inputs": ["labels=shop-judgments@v2"]}).json()
    for _ in range(200):
        job = cl.get(f"/api/jobs/{r['job']}", headers=h).json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    exp = PS.get(ws, "shop")["experiments"]
    assert [e["pipeline"] for e in exp] == ["ltr@v2"] and job["result"]["experiment"] == exp[0]["id"]
    # its training labels come from the version itself, so independence can be checked
    assert PS.training_labels(ws, exp[0]) == "shop-judgments@v2"
