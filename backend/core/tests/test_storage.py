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
