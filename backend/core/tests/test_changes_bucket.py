"""A workspace in a bucket, written by another machine (changes.BucketWatch). The bucket's own listing carries
every object's tag, so what was added, replaced or removed is seen without reading any object. The answer names
the topics that are only partly watched. Against moto: nothing here calls a real cloud."""
from __future__ import annotations

import time

import boto3
import pytest
from moto import mock_aws

from captain_search import changes as CH
from captain_search.storage.backend import LocalStore, S3Store


@pytest.fixture
def bucket(monkeypatch):
    with mock_aws():
        CH.reset_for_tests()
        monkeypatch.setattr(CH.BucketWatch, "EVERY_S", 0.5)   # the rhythm is 2 s: shorter here so the test is quick
        monkeypatch.setattr(CH, "LINGER_S", 0.5)
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="cst-test")
        store = S3Store("cst-test", "ws1", client=c)
        for key in ("projects.yaml", "projects/shop/state.yaml", "datasets/demo-catalog/v1/manifest.yaml", "jobs/j1.yaml",
                    "notebooks/zero-results/v1.yaml", "staging/shop/staging/deploy.json", "staging/shop/staging/logs/a.jsonl",
                    "objects/sha256/ab/cd"):
            c.put_object(Bucket="cst-test", Key="ws1/" + key, Body=b"one")
        CH.watch(store)

        class B:
            pass

        b = B()
        b.store, b.w = store, CH._WATCHERS[store.uri]
        b.other = lambda key, body=b"two": c.put_object(Bucket="cst-test", Key="ws1/" + key, Body=body)  # another machine
        b.gone = lambda key: c.delete_object(Bucket="cst-test", Key="ws1/" + key)
        yield b
        CH.reset_for_tests()
        time.sleep(0.8)   # the watcher ends by itself once nobody listens


def _told(b, write, topics, within=3.0):
    """Listen, let the watcher take its first look, make the write, and return (the topics told, how long it took)."""
    seq = CH.FEED.seq
    CH.FEED.listen(set(topics))
    try:
        for _ in range(60):
            if b.w.primed:
                break
            time.sleep(0.05)
        assert b.w.primed, "the watcher never took its first look"
        assert CH.FEED.answer(seq, set(topics))["changed"] == [], "the first look tells nothing"
        t0 = time.perf_counter()
        write()
        got = CH.FEED.wait(seq, set(topics), timeout=within)
        return [c["topic"] for c in got["changed"]], time.perf_counter() - t0
    finally:
        CH.FEED.unlisten(set(topics))


def test_the_listing_carries_tags_one_level_or_all(bucket):
    top = bucket.store.list_tags("", shallow=True)
    assert top["projects.yaml"] and {"projects/", "datasets/", "staging/", "objects/"} <= set(top) and top["projects/"] == ""
    deep = bucket.store.list_tags("datasets/")
    assert list(deep) == ["datasets/demo-catalog/v1/manifest.yaml"] and deep["datasets/demo-catalog/v1/manifest.yaml"]
    # a store with no tags of its own still lists one level, so the watcher works on it (it sees keys come and go)
    import tempfile

    local = LocalStore(tempfile.mkdtemp())
    local.put_new("projects/shop/state.yaml", b"x")
    local.put_new("projects.yaml", b"x")
    assert local.list_tags("", shallow=True) == {"projects.yaml": "", "projects/": ""}


@pytest.mark.parametrize("key,topic", [
    ("datasets/demo-sessions/v1/manifest.yaml", "table:demo-sessions"),   # a dataset written from another machine
    ("backtest-runs/tuned-vs-current/v1.yaml", "run:tuned-vs-current"),   # a run, in a folder that was not there before
    ("jobs/j2.yaml", "job:j2"),
    ("notebooks/zero-results/v2.yaml", "notebooks"),
    ("projects.yaml", "projects"),                                        # replaced in place: same key, new tag
    ("projects/shop/state.yaml", "project:shop"),
    ("jobs/j1.yaml", "job:j1"),                                           # a job's record replaced in place
    ("staging/shop/staging/deploy.json", "deployment:shop"),
])
def test_what_another_machine_writes_is_told(bucket, key, topic):
    told, took = _told(bucket, lambda: bucket.other(key), [topic])
    assert told == [topic], f"{key} written by another machine was not told"
    assert took < 2.0, f"told {took:.2f} s after the write, at a look every half second"


def test_a_removed_object_is_told_and_this_services_own_write_is_not_told_twice(bucket):
    told, _ = _told(bucket, lambda: bucket.gone("notebooks/zero-results/v1.yaml"), ["notebooks"])
    assert told == ["notebooks"]
    seq = CH.FEED.seq
    CH.FEED.listen({"jobs"})
    bucket.store.put_new("jobs/j9.yaml", b"mine")   # through this service: told at once
    first = CH.FEED.answer(seq, {"jobs", "job:j9"})
    assert [c["topic"] for c in first["changed"]] == ["job:j9", "jobs"]
    time.sleep(1.6)                                  # three more looks
    again = CH.FEED.answer(first["seq"], {"jobs", "job:j9"})
    CH.FEED.unlisten({"jobs"})
    assert again["changed"] == [], f"the bucket watch told this service's own write a second time: {again}"


def test_the_answer_names_what_is_only_partly_watched(bucket, tmp_path):
    # what a deployed model writes about itself is many small objects, and is not listed
    told, _ = _told(bucket, lambda: bucket.other("staging/shop/staging/logs/b.jsonl"), ["deployment:shop", "deployed"], within=1.5)
    assert told == [], "a model's own log is not watched in a bucket"
    assert CH.partial(bucket.store, ["projects", "deployed", "deployment:shop", "data"]) == ["deployed", "deployment:shop"]
    assert CH.partial(bucket.store) == ["deployed", "deployment:*"]
    # a folder whose listing is slow or large is listed every 30 seconds, and its topics are named
    bucket.w.SLOW_KEYS = 0
    CH.FEED.listen({"data"})
    time.sleep(1.2)
    CH.FEED.unlisten({"data"})
    assert "datasets/" in bucket.w.slow
    assert CH.partial(bucket.store, ["data", "table:demo-catalog", "settings"]) == ["data", "table:demo-catalog"]
    # a workspace on this computer is watched whole
    local = LocalStore(str(tmp_path))
    CH.watch(local)
    assert CH.partial(local, ["deployed", "data"]) == [] and CH.partial(local) == []


def test_with_no_listeners_the_bucket_is_not_listed(bucket):
    time.sleep(1.2)
    assert bucket.w.looks == 0 and bucket.w.thread is None, "nobody listens: nothing is asked of the bucket"
    CH.FEED.listen({"projects"})
    time.sleep(0.8)
    CH.FEED.unlisten({"projects"})
    n = bucket.w.looks
    assert n > 0
    # each look lists the top level, each folder but objects/ and staging/, and each deployed model's own level
    assert not any(p.startswith("objects") for p, _ in bucket.w.tags) and ("staging/shop/staging/", True) in bucket.w.tags
    time.sleep(1.6)   # the last listener left: half a second later it stops
    m = bucket.w.looks
    time.sleep(1.0)
    assert bucket.w.looks == m, "it went on listing with nobody listening"
