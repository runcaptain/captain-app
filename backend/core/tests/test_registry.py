import threading

import pytest

from captain_search.model.entities import DatasetVersion, Provenance
from captain_search.storage.backend import Conflict, LocalStore
from captain_search.storage.registry import Registry


def _m(name="cat"):
    return DatasetVersion(org="o", workspace="w", created_by="t", name=name, version=0,
                          provenance=Provenance(source="generated"))


def test_publish_increments_and_links_parent(tmp_path):
    r = Registry(LocalStore(str(tmp_path)))
    a = r.publish(_m())
    b = r.publish(_m())
    assert (a.version, b.version, b.parent) == (1, 2, 1)
    assert r.load("dataset", "cat").version == 2
    assert r.resolve("dataset", "cat@v1")[0].version == 1


def test_concurrent_publish_gets_distinct_versions(tmp_path):
    r = Registry(LocalStore(str(tmp_path)))
    got = []
    lock = threading.Lock()

    def go():
        m = r.publish(_m())
        with lock:
            got.append(m.version)

    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == list(range(1, 13))


def test_verify_catches_tampered_blob(tmp_path):
    s = LocalStore(str(tmp_path))
    r = Registry(s)
    e = r.put_blob(b"hello", "data/x.bin")
    m = r.publish(_m().model_copy(update={"files": [e]}))
    assert r.verify("dataset", "cat", m.version) == []
    with open(s._p(r.blob_key(e.sha256)), "wb") as fh:
        fh.write(b"evil")
    assert any("checksum" in p for p in r.verify("dataset", "cat", m.version))


def test_alias_cas(tmp_path):
    r = Registry(LocalStore(str(tmp_path)))
    r.publish(_m())
    r.publish(_m())
    t = r.set_alias("dataset", "cat", "prod", 1, None, "a")
    with pytest.raises(Conflict):
        r.set_alias("dataset", "cat", "prod", 2, None, "b")
    r.set_alias("dataset", "cat", "prod", 2, t, "b")
    assert r.resolve("dataset", "cat@prod")[0].version == 2


def test_bad_names_rejected(tmp_path):
    r = Registry(LocalStore(str(tmp_path)))
    for bad in ["Cat", "../x", "a/b/c/d/e/f/g/h/i", "a//b", "cat/v2", ""]:
        with pytest.raises(ValueError):
            r.publish(_m(bad))


def test_nested_names(tmp_path):
    r = Registry(LocalStore(str(tmp_path)))
    r.publish(_m("catalogs/acme/products"))
    r.publish(_m("catalogs/acme/products"))
    r.publish(_m("catalogs/zeta"))
    assert r.names("dataset") == ["catalogs/acme/products", "catalogs/zeta"]
    assert r.versions("dataset", "catalogs/acme/products") == [1, 2]


def test_newer_manifest_fields_are_ignored(tmp_path):
    import yaml
    s = LocalStore(str(tmp_path))
    r = Registry(s)
    m = r.publish(_m())
    raw = yaml.safe_load(s.get("datasets/cat/v1/MANIFEST.yaml"))
    raw["field_from_the_future"] = 1
    raw["provenance"]["also_new"] = True
    s.put_new("datasets/cat/v2/MANIFEST.yaml", yaml.safe_dump(raw).encode())
    assert r.load("dataset", "cat", 2).name == "cat"
