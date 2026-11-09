"""Publishing pipeline versions to a model registry (write-once, manifest last) and importing outside model versions."""
import sys
import threading

import boto3
import pyarrow as pa
import pytest
import yaml
from moto import mock_aws

from captain_search import model_versions as MV
from captain_search import models as MD
from captain_search import ops
from captain_search import retrain as RT
from captain_search.data.datasets import snapshot
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.storage.backend import AlreadyExists, S3Store, open_store
from captain_search.workspace import init_workspace

TRAINER = r'''
import os, pathlib
out = pathlib.Path(os.environ["CST_OUT"])
(out / "m").mkdir()
(out / "m" / "weights.bin").write_bytes(b"w" * 3000)
(out / "result.yaml").write_text("metrics:\n  ndcg@10: 0.5\nartifact_dir: m\n")
'''


@pytest.fixture
def trained(tmp_path):
    ctx = init_workspace("t", str(tmp_path / "store"))
    snapshot(ctx, "shop-catalog", pa.table({"id": ["a"], "title": ["red hat"]}), kind=DatasetKind.catalog,
             provenance=Provenance(source="generated"))
    (tmp_path / "train.py").write_text(TRAINER)
    ops.pipeline_publish(ctx, {"name": "ltr", "engine": "local-bm25", "catalog": "shop-catalog", "spec": {"train": {
        "command": f"{sys.executable} {tmp_path / 'train.py'}", "inputs": ["shop-catalog"]}}})
    RT.retrain(ctx, "ltr", local_exec=True)
    return ctx, tmp_path


def test_publish_writes_the_layout_and_pull_verifies_it(trained):
    ctx, tmp = trained
    reg = str(tmp / "registry")
    r = MV.publish(ctx, "ltr@v2", "arch-v1", "shop", reg)
    assert r["model"] == "shop@v1" and r["pipeline"] == "ltr@v2" and r["repro_status"] == "not-tested"
    st = open_store(reg)
    keys = set(st.list("arch-v1/shop/v1/"))
    for k in ("MANIFEST.yaml", "SHA256SUMS", "REPRODUCE.md", "reproduce.sh", ".claim", "weights/weights.bin",
              "code/pipeline.yaml", "env/env.yaml", "inputs/inputs.yaml", "results/metrics.yaml"):
        assert f"arch-v1/shop/v1/{k}" in keys, k
    man = yaml.safe_load(st.get("arch-v1/shop/v1/MANIFEST.yaml"))
    assert man["repro_status"] == "not-tested" and man["pipeline"] == "ltr@v2"
    assert man["results"] == [{"metric": "ndcg@10", "value": 0.5, "kind": "offline, reported by the trainer"}]
    inputs = yaml.safe_load(st.get("arch-v1/shop/v1/inputs/inputs.yaml"))
    assert inputs["train_inputs"][0]["ref"] == "shop-catalog@v1"  # a ref, not the data
    assert not any(k.endswith(".parquet") for k in keys)
    assert "cst pipeline retrain ltr@v1 --local-exec --input catalog=shop-catalog@v1" in st.get("arch-v1/shop/v1/reproduce.sh").decode()
    assert "shop" in st.get("INDEX.md").decode()
    assert MD.find(ctx, "shop@v1")[0]["status_kind"] == "candidate"  # re-imported, so it shows
    p = MD.pull(ctx, "shop@v1", str(tmp / "out"), ["weights", "code", "env", "results", "inputs"])
    assert p["mismatched"] == [] and p["unchecked"] == ["MANIFEST.yaml"] and p["checked"] == p["files"] - 2
    assert (tmp / "out/weights/weights.bin").read_bytes() == b"w" * 3000
    # A second publish of a later version names its parent from the registry.
    RT.retrain(ctx, "ltr", local_exec=True)
    r2 = MV.publish(ctx, "ltr@v3", "arch-v1", "shop", reg)
    assert r2["model"] == "shop@v2"
    assert yaml.safe_load(st.get("arch-v1/shop/v2/MANIFEST.yaml"))["parent"].startswith("v1")
    assert MD.find(ctx, "shop@v2")[0]["parents"] == ["v1"]


def test_publish_is_write_once(trained, monkeypatch):
    ctx, tmp = trained
    reg = str(tmp / "registry")
    MV.publish(ctx, "ltr@v2", "arch-v1", "shop", reg)
    monkeypatch.setattr(MV, "_claim", lambda st, base, body: 1)  # pretend the claim lost the race and took v1 anyway
    with pytest.raises(AlreadyExists):
        MV.publish(ctx, "ltr@v2", "arch-v1", "shop", reg)
    st = open_store(reg)
    assert yaml.safe_load(st.get("arch-v1/shop/v1/MANIFEST.yaml"))["pipeline"] == "ltr@v2"
    with pytest.raises(AlreadyExists):
        st.put_new("arch-v1/shop/v1/.claim", b"again")


def test_concurrent_publishes_get_distinct_versions(trained):
    ctx, tmp = trained
    reg = str(tmp / "registry")
    out, errs = [], []

    def go():
        try:
            out.append(MV.publish(ctx, "ltr@v2", "arch-v1", "shop", reg)["version"])
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and sorted(out, key=lambda v: int(v[1:])) == [f"v{i}" for i in range(1, 7)]


def test_a_crash_before_the_manifest_leaves_the_version_invisible(trained, monkeypatch):
    ctx, tmp = trained
    reg = str(tmp / "registry")
    real = MV._put

    def dies(st, key, data):
        if key.endswith("SHA256SUMS"):
            raise RuntimeError("lost the connection")
        real(st, key, data)

    monkeypatch.setattr(MV, "_put", dies)
    with pytest.raises(RuntimeError):
        MV.publish(ctx, "ltr@v2", "arch-v1", "shop", reg)
    assert MD.scan(reg)["models"] == []  # parts are there, but nobody lists them
    assert open_store(reg).exists("arch-v1/shop/v1/weights/weights.bin")
    monkeypatch.setattr(MV, "_put", real)
    assert MV.publish(ctx, "ltr@v2", "arch-v1", "shop", reg)["model"] == "shop@v2"  # v1 stays used
    assert [m["id"] for m in MD.scan(reg)["models"]] == ["shop@v2"]


def test_bad_names_are_refused(trained):
    ctx, tmp = trained
    for arch, brand in (("Arch", "shop"), ("arch-v1", "v3"), ("arch-v1", "../x")):
        with pytest.raises(ValueError, match="bad"):
            MV.publish(ctx, "ltr@v2", arch, brand, str(tmp / "registry"))


@pytest.fixture
def aws(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"), ("AWS_DEFAULT_REGION", "us-west-1")):
        monkeypatch.setenv(k, v)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-west-1")
        s3.create_bucket(Bucket="reg-pub", CreateBucketConfiguration={"LocationConstraint": "us-west-1"})
        yield s3


def test_publish_to_s3_uses_a_create_only_multipart_upload_for_large_files(trained, aws, monkeypatch):
    ctx, tmp = trained
    monkeypatch.setattr(MV, "LARGE", 2048)  # the 3000-byte weights count as large here
    monkeypatch.setattr(MV, "CHUNK", 1000)
    calls = []
    real = S3Store.put_new_chunks

    def spy(self, key, chunks):
        calls.append(key)
        return real(self, key, chunks)

    monkeypatch.setattr(S3Store, "put_new_chunks", spy)
    r = MV.publish(ctx, "ltr@v2", "arch-v1", "shop", "s3://reg-pub/models")
    assert r["path"] == "s3://reg-pub/models/arch-v1/shop/v1/"
    assert calls == ["arch-v1/shop/v1/weights/weights.bin"]
    st = open_store("s3://reg-pub/models")
    with pytest.raises(AlreadyExists):  # the multipart path is create-only too
        st.put_new_chunks("arch-v1/shop/v1/weights/weights.bin", [b"x" * 10])
    assert st.get("arch-v1/shop/v1/weights/weights.bin") == b"w" * 3000
    p = MD.pull(ctx, "shop@v1", str(tmp / "s3out"))
    assert p["mismatched"] == [] and (tmp / "s3out/weights/weights.bin").stat().st_size == 3000


def test_import_version_from_the_registry_round_trips(trained):
    ctx, tmp = trained
    reg = str(tmp / "registry")
    MV.publish(ctx, "ltr@v2", "arch-v1", "shop", reg)
    v = MV.import_version(ctx, "ltr", registry_path="arch-v1/shop/v1")
    assert v.version == 3 and v.parent == 2 and v.train_metrics == {"ndcg@10": 0.5}
    assert v.source["kind"] == "registry" and v.source["brand"] == "shop" and v.parents == ["registry:shop@v1"]
    assert v.registry_ref.endswith("arch-v1/shop/v1/") and v.repro_status.value == "not-tested"
    assert not any(f.path.startswith("artifact/") for f in v.files)  # the weights are the registry's, not copied
    other = MV.import_version(ctx, "ltr-copy", registry_path="arch-v1/shop/v1", base="ltr@v1")
    assert other.version == 1 and "ltr@v1" in other.parents and other.engine == "local-bm25"
    with pytest.raises(ValueError, match="no pipeline"):
        MV.import_version(ctx, "fresh", registry_path="arch-v1/shop/v1")
    with pytest.raises(ValueError, match="one source"):
        MV.import_version(ctx, "ltr")


def test_import_version_from_mlflow(trained, monkeypatch):
    ctx, _ = trained
    from captain_search import mlflow_bridge as MB

    MB.configure(ctx, uri="https://mlflow.example.test")
    answers = {
        "model-versions/get": {"model_version": {"name": "ranker", "version": "4", "run_id": "r123",
                                                 "source": "s3://ml-artifacts/1/r123/artifacts/model"}},
        "runs/get": {"run": {"info": {"run_id": "r123", "artifact_uri": "s3://ml-artifacts/1/r123/artifacts"},
                             "data": {"metrics": [{"key": "ndcg_at_10", "value": 0.47}],
                                      "params": [{"key": "trees", "value": "400"}],
                                      "tags": [{"key": "mlflow.source.git.commit", "value": "abc123"}]}}},
    }
    seen = []
    monkeypatch.setattr(MB, "_call", lambda c, method, path, body=None: (seen.append((path, body)), answers[path])[1])
    v = MV.import_version(ctx, "ltr", mlflow_model="ranker@4")
    assert v.train_metrics == {"ndcg_at_10": 0.47} and v.train_params == {"trees": "400"}
    assert v.training_commit == "abc123" and v.registry_ref == "s3://ml-artifacts/1/r123/artifacts/model"
    assert v.parents == ["mlflow-model:ranker@4", "mlflow-run:r123"]
    assert v.source == {"kind": "mlflow", "tracking": "https://mlflow.example.test", "model": "ranker", "version": "4",
                        "run_id": "r123", "artifacts": "s3://ml-artifacts/1/r123/artifacts/model"}
    assert ("model-versions/get", {"name": "ranker", "version": "4"}) in seen
    run_only = MV.import_version(ctx, "ltr", mlflow_run="r123")
    assert run_only.parents == ["mlflow-run:r123"] and run_only.train_metrics == {"ndcg_at_10": 0.47}


def test_publish_and_import_routes_run_as_jobs(trained, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from captain_search import server

    ctx, tmp = trained
    monkeypatch.setattr(server, "ctx", lambda: ctx)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}

    def finish(jid):
        for _ in range(200):
            if server.JOBS[jid]["status"] in ("done", "failed"):
                return server.JOBS[jid]
            time.sleep(0.02)
        raise AssertionError("job didn't finish")

    j = c.post("/api/models/publish", headers=h, json={"pipeline": "ltr", "arch": "arch-v1", "brand": "shop",
                                                        "registry": str(tmp / "registry")}).json()
    done = finish(j["job"])
    assert done["status"] == "done" and done["result"]["model"] == "shop@v1" and done["result"]["pipeline"] == "ltr@v2"
    assert c.post("/api/models/publish", headers=h, json={"pipeline": "ltr", "brand": "shop"}).status_code >= 400
    j = c.post("/api/models/import-version", headers=h, json={"pipeline": "ltr", "registry_path": "arch-v1/shop/v1"}).json()
    done = finish(j["job"])
    assert done["status"] == "done" and done["result"]["pipeline"] == "ltr@v3"


def test_cli_publish_and_import_version(trained):
    import json

    from typer.testing import CliRunner

    from captain_search.cli.main import app

    ctx, tmp = trained
    run = CliRunner()
    r = run.invoke(app, ["--json", "-w", "t", "models", "publish", "ltr@v2", "--arch", "arch-v1", "--brand", "shop",
                         "--registry", str(tmp / "registry")])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["model"] == "shop@v1"
    r = run.invoke(app, ["--json", "-w", "t", "models", "import-version", "ltr", "--registry-path", "arch-v1/shop/v1"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["pipeline"] == "ltr@v3"
    r = run.invoke(app, ["--json", "-w", "t", "pipeline", "retrain", "ltr@v1", "--local-exec", "--param", "trees=5"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["params"] == {"trees": 5}
