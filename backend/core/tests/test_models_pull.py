"""Pulling a model version's files from the registry, checked against SHA256SUMS (S3 faked)."""
import hashlib

import boto3
import pytest
from moto import mock_aws

from captain_search import models as MD
from captain_search.workspace import init_workspace

BASE = "arch-v1/acme/"


def _put(s3, key, body: bytes):
    s3.put_object(Bucket="reg-test", Key=key, Body=body)


def _version(s3, label, status, files: dict, tamper: str | None = None):
    sums = "".join(f"{hashlib.sha256(b).hexdigest()}  {p}\n" for p, b in files.items())
    for p, b in files.items():
        _put(s3, f"{BASE}{label}/{p}", b"tampered" if p == tamper else b)
    _put(s3, f"{BASE}{label}/MANIFEST.yaml", f"status: {status}\n".encode())
    _put(s3, f"{BASE}{label}/SHA256SUMS", sums.encode())


@pytest.fixture
def setup(tmp_path):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-west-1")
        s3.create_bucket(Bucket="reg-test", CreateBucketConfiguration={"LocationConstraint": "us-west-1"})
        files = {"weights/model.bin": b"w" * 5000, "code/train.py": b"print(1)\n", "inputs/big.parquet": b"x" * 100,
                 "REPRODUCE.md": b"run it\n"}
        _version(s3, "v1", "rolled back", files)
        _version(s3, "v2", "current", files)
        _version(s3, "v10", "candidate", files, tamper="weights/model.bin")
        ctx = init_workspace("t", str(tmp_path / "store"))
        MD.import_registry(ctx, "s3://reg-test")
        yield ctx, tmp_path


def test_pull_checks_and_resumes(setup):
    ctx, tmp = setup
    r = MD.pull(ctx, "acme@v2", str(tmp / "out"))
    out = tmp / "out"
    assert (out / "weights/model.bin").read_bytes() == b"w" * 5000
    assert not (out / "inputs").exists() and r["parts"] == ["weights", "code", "env", "results"]
    assert r["mismatched"] == [] and r["unchecked"] == ["MANIFEST.yaml"] and r["downloaded"] == r["files"] == 5
    again = MD.pull(ctx, "acme@v2", str(tmp / "out"))
    assert again["downloaded"] == 2 and again["already_there"] == 3  # MANIFEST and SHA256SUMS have no sum, so they come again
    only = MD.pull(ctx, "acme@v2", str(tmp / "w"), ["weights"])
    assert not (tmp / "w/code").exists() and (tmp / "w/weights/model.bin").exists()


def test_bare_brand_picks_current_and_mismatch_fails(setup):
    ctx, tmp = setup
    assert MD.find(ctx, "acme")[0]["id"] == "acme@v2"  # current beats the newer candidate
    with pytest.raises(ValueError, match="don't match SHA256SUMS: weights/model.bin"):
        MD.pull(ctx, "acme@v10", str(tmp / "bad"))
    with pytest.raises(ValueError, match="unknown part"):
        MD.pull(ctx, "acme@v2", str(tmp / "x"), ["models"])
