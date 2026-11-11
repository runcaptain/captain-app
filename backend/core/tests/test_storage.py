import threading

import boto3
import pytest
from moto import mock_aws

from captain_search.storage.backend import AlreadyExists, Conflict, LocalStore, NotFound, S3Store


def _contract(store):
    store.put_new("a/b.txt", b"one")
    with pytest.raises(AlreadyExists):
        store.put_new("a/b.txt", b"two")
    assert store.get("a/b.txt") == b"one"
    with pytest.raises(NotFound):
        store.get("missing")
    t1 = store.put_if("ptr.yaml", b"v1", None)
    with pytest.raises(Conflict):
        store.put_if("ptr.yaml", b"v2", None)  # must not exist
    t2 = store.put_if("ptr.yaml", b"v2", t1)
    with pytest.raises(Conflict):
        store.put_if("ptr.yaml", b"v3", t1)  # stale tag
    assert store.get_with_tag("ptr.yaml") == (b"v2", t2)
    assert store.list("a/") == ["a/b.txt"]


def test_local_contract(tmp_path):
    _contract(LocalStore(str(tmp_path / "s")))


def test_local_rejects_escape(tmp_path):
    from captain_search.storage.backend import StorageError

    with pytest.raises(StorageError):
        LocalStore(str(tmp_path / "s")).put_new("../x", b"")


def test_local_concurrent_create_only_one_wins(tmp_path):
    store = LocalStore(str(tmp_path / "s"))
    wins = []

    def go(i):
        try:
            store.put_new("k", str(i).encode())
            wins.append(i)
        except AlreadyExists:
            pass

    ts = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1


@mock_aws
def test_s3_contract():
    c = boto3.client("s3", region_name="us-east-1")
    c.create_bucket(Bucket="cst-test")
    _contract(S3Store("cst-test", "ws1", client=c))


# ---- Azure Blob: a fake with the SDK's shapes (azure-storage-blob 12) ---------------------
# Run against a real account on 2026-11-10: see docs/compute-live-testing.md.


def _fake_azure(monkeypatch):
    import sys
    import types

    blobs: dict[str, tuple[bytes, str]] = {}
    n = [0]

    class ResourceExistsError(Exception):
        pass

    class ResourceModifiedError(Exception):
        pass

    class ResourceNotFoundError(Exception):
        pass

    class MatchConditions:
        IfNotModified = "IfNotModified"

    class BlobClient:
        def __init__(self, name):
            self.name = name

        def upload_blob(self, data, overwrite=False, etag=None, match_condition=None):
            if isinstance(data, (bytes, bytearray)):
                data = bytes(data)
            else:
                data = b"".join(data)
            cur = blobs.get(self.name)
            if not overwrite and cur is not None:
                raise ResourceExistsError(self.name)
            if match_condition == MatchConditions.IfNotModified and (cur is None or cur[1] != etag):
                raise ResourceModifiedError(self.name)
            n[0] += 1
            blobs[self.name] = (data, f'"0x{n[0]:X}"')
            return {"etag": blobs[self.name][1], "last_modified": None}  # the response headers

    class Download:
        def __init__(self, data, etag):
            self._d, self.properties = data, types.SimpleNamespace(etag=etag)

        def readall(self):
            return self._d

    class ContainerClient:
        def __init__(self, url, container, credential=None):
            self.url = url

        def get_blob_client(self, name):
            return BlobClient(name)

        def upload_blob(self, name, data, overwrite=False, **kw):
            # the real one uploads and returns the blob's client, not the response headers
            BlobClient(name).upload_blob(data, overwrite=overwrite, **kw)
            return BlobClient(name)

        def download_blob(self, name):
            if name not in blobs:
                raise ResourceNotFoundError(name)
            return Download(*blobs[name])

        def list_blobs(self, name_starts_with=None):
            return [types.SimpleNamespace(name=k) for k in blobs if k.startswith(name_starts_with or "")]

    mods = {"azure": types.ModuleType("azure"), "azure.core": types.ModuleType("azure.core"),
            "azure.core.exceptions": types.ModuleType("azure.core.exceptions"),
            "azure.storage": types.ModuleType("azure.storage"), "azure.storage.blob": types.ModuleType("azure.storage.blob")}
    mods["azure.core"].MatchConditions = MatchConditions
    ex = mods["azure.core.exceptions"]
    ex.ResourceExistsError, ex.ResourceModifiedError, ex.ResourceNotFoundError = (
        ResourceExistsError, ResourceModifiedError, ResourceNotFoundError)
    mods["azure.storage.blob"].ContainerClient = ContainerClient
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return blobs


def test_azure_contract(monkeypatch):
    """put_if once read the new ETag off ContainerClient.upload_blob's answer, which is a BlobClient: the pointer
    was written and the caller got "TypeError: 'BlobClient' object is not subscriptable"."""
    from captain_search.storage.backend import AzureStore

    blobs = _fake_azure(monkeypatch)
    store = AzureStore("acct", "box", "ws1", credential=object())
    _contract(store)
    assert store.uri == "az://acct/box/ws1" and "ws1/a/b.txt" in blobs
    with pytest.raises(Conflict):
        store.put_if("never-written", b"x", '"0x1"')  # a tag for a key that isn't there
    assert store.list() == ["a/b.txt", "ptr.yaml"]


def test_azure_extra_installs_the_sign_in_library():
    """The runner image installs the `azure` extra only. Without azure-identity a job on Azure died at its first
    read of the workspace: "ModuleNotFoundError: No module named 'azure.identity'"."""
    import tomllib
    from pathlib import Path

    extras = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert any(d.startswith("azure-identity") for d in extras["azure"])

