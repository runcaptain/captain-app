"""Finding registries among a login's buckets, the infrastructure code for a new one, and making one (AWS faked)."""
import boto3
import pytest
import yaml
from moto import mock_aws

from captain_search import attach as AT
from captain_search import registry_setup as RS
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    return init_workspace("t", str(tmp_path / "store"))


def _bucket(s3, name):
    s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": "us-west-1"})


@mock_aws
def test_discover_marks_registries_first(ctx):
    s3 = boto3.client("s3", region_name="us-west-1")
    for b in ("aaa-other", "bbb-empty", "zzz-root-reg", "mmm-nested", "ccc-workspace"):
        _bucket(s3, b)
    s3.put_object(Bucket="ccc-workspace", Key="ws/workspace.yaml", Body=b"name: ws\n")
    s3.put_object(Bucket="ccc-workspace", Key="ws/datasets/hello/v1/MANIFEST.yaml", Body=b"{}")
    s3.put_object(Bucket="ccc-workspace", Key="ws/datasets/logs/a/b/v1/MANIFEST.yaml", Body=b"{}")
    s3.put_object(Bucket="aaa-other", Key="logs/2026/x.json", Body=b"{}")
    s3.put_object(Bucket="zzz-root-reg", Key="arch-v1/brand/v1/MANIFEST.yaml", Body=yaml.safe_dump({"status": "live"}).encode())
    s3.put_object(Bucket="zzz-root-reg", Key="arch-v1/brand/v1/weights/w.bin", Body=b"x")
    s3.put_object(Bucket="mmm-nested", Key="models/arch-v2/brand/v3/MANIFEST.yaml", Body=b"status: candidate\n")
    r = RS.discover(ctx, None)
    by = {b["bucket"]: b for b in r["buckets"]}
    assert by["zzz-root-reg"]["kind"] == "registry" and by["zzz-root-reg"]["uri"] == "s3://zzz-root-reg"
    assert by["mmm-nested"]["kind"] == "registry" and by["mmm-nested"]["uri"] == "s3://mmm-nested/models"
    assert by["bbb-empty"]["kind"] == "empty" and by["aaa-other"]["kind"] == "other"
    assert [b["kind"] for b in r["buckets"]][:2] == ["registry", "registry"] and r["registries"] == 2
    assert by["zzz-root-reg"]["region"] == "us-west-1"
    assert by["ccc-workspace"]["kind"] == "workspace"


def test_templates_and_names():
    tf = RS.template("acme-models", "us-east-2", "terraform")
    assert 'default = "acme-models"' in tf["text"] and "prevent_destroy = true" in tf["text"] and 'status = "Enabled"' in tf["text"]
    cf = RS.template("acme-models", fmt="cloudformation")
    assert "DeletionPolicy: Retain" in cf["text"] and "aws:SecureTransport" in cf["text"]
    assert "captain-model-registry-acme-models" in cf["how"]
    assert RS.check_name("Acme-Models") == "acme-models"
    for bad in ("a", "has_underscore", "-dash"):
        with pytest.raises(ValueError):
            RS.check_name(bad)


@mock_aws
def test_create_makes_an_attached_empty_registry(ctx):
    r = RS.create(ctx, "acme-models-test", "us-west-1", None)
    s3 = boto3.client("s3", region_name="us-west-1")
    # moto's CloudFormation makes the bucket but skips its versioning and policy: the live test checks those.
    assert b"Model registry" in s3.get_object(Bucket="acme-models-test", Key="INDEX.md")["Body"].read()
    assert r["stack"] == "captain-model-registry-acme-models-test" and r["models"] == 0
    assert [x["uri"] for x in AT.registries(ctx)] == ["s3://acme-models-test"]
    found = {b["bucket"]: b for b in RS.discover(ctx, None)["buckets"]}["acme-models-test"]
    assert found["kind"] == "registry" and found["attached"] and found["detail"] == "made by Captain"
    with pytest.raises(ValueError, match="already exists"):
        RS.create(ctx, "acme-models-test", "us-west-1", None)
