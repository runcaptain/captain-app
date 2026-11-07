"""Scope model (Brand > Site > Engine by default), engines as persistent identities, and the Explorer tree."""
import pytest

from captain_search import projects as PR
from captain_search import scopes as SC
from captain_search.workspace import init_workspace


def test_explorer_follows_the_spec_tree(tmp_path):
    c = init_workspace("acme", str(tmp_path / "b"))
    brand = SC.add_scope(c, "brand", "Acme Outdoors")
    site = SC.add_scope(c, "site", "US storefront", parent=brand["id"])
    search = SC.add_engine(c, "Product search", "search", [site["id"]], ["demo-bm25"])
    SC.add_engine(c, "Recommendations", "recommendation", [site["id"]])
    SC.add_engine(c, "Loose engine")
    PR.create(c, "Session-aware ranking")
    t = SC.explorer(c)["tree"]
    labels = [n.get("label") for n in t]
    assert labels[:3] == ["All engines", "All experiments", "Brands"] and labels[-2:] == ["Projects", "Scope model"]
    acme = t[2]["children"][0]
    us = acme["children"][0]["children"][0]
    assert acme["name"] == "Acme Outdoors" and acme["children"][0]["label"] == "Sites" and us["name"] == "US storefront"
    assert [e["name"] for e in us["children"][0]["children"]] == ["Product search", "Recommendations"]
    assert any(n.get("label") == "Engines" for n in t)  # an engine with no scope sits at the root
    r = SC.Resolve(c)
    got = r.targets("pipeline", "demo-bm25@v2")
    assert got["engine"] == search["id"] and got["scopes"] == [site["id"], brand["id"]]
    assert r.within("pipeline", "demo-bm25", scope=brand["id"])


def test_hierarchy_rules(tmp_path):
    c = init_workspace("acme", str(tmp_path / "b"))
    b = SC.add_scope(c, "brand", "A")
    s = SC.add_scope(c, "site", "S", parent=b["id"])
    with pytest.raises(ValueError, match="can't sit inside"):
        SC.add_scope(c, "brand", "B", parent=s["id"])
    m = SC.model(c)
    loop = {**m, "types": [{**t, "parents": ["site"]} if t["key"] == "brand" else t for t in m["types"]]}
    with pytest.raises(ValueError, match="loop"):
        SC.set_model(c, loop)
    no_site = {**m, "types": [t for t in m["types"] if t["key"] == "brand"], "explorer": {"order": ["brand"], "hidden": []}}
    with pytest.raises(ValueError, match="removes"):
        SC.set_model(c, no_site)
    renamed = {**m, "types": [{**t, "singular": "Storefront", "plural": "Storefronts"} if t["key"] == "site" else t
                              for t in m["types"]]}
    new = SC.set_model(c, renamed, expect_revision=1)
    assert new["revision"] == 2 and SC.explorer(c)["tree"][2]["children"][0]["children"][0]["label"] == "Storefronts"


def test_registry_brand_maps_to_a_scope(tmp_path):
    c = init_workspace("acme", str(tmp_path / "b"))
    b = SC.add_scope(c, "brand", "Globex", fields={"registry_brands": ["globex-eu"]})
    r = SC.Resolve(c)
    assert r.targets("model", "globex-eu@v3", brand="globex-eu")["scopes"] == [b["id"]]
    assert r.targets("model", "globex@v1", brand="globex")["scopes"] == [b["id"]]


def test_scope_header_filters_lists(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import demo, server

    c = init_workspace("acme", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    brand = SC.add_scope(c, "brand", "Acme Outdoors")
    eng = SC.add_engine(c, "Product search", "search", [brand["id"]], ["demo-bm25"])
    SC.link(c, [{"kind": "dataset", "name": "demo-catalog"}], scope=brand["id"])
    monkeypatch.setattr(server, "_ctx", None)
    cl = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    by = lambda hdr, path: [x["name"] for x in cl.get(path, headers={**h, "X-CST-Scope": hdr}).json()]
    assert by(f"scope:{brand['id']}", "/api/datasets") == ["demo-catalog"]
    assert by(f"scope:{brand['id']}", "/api/pipelines") == ["demo-bm25"]
    assert by(f"engine:{eng['id']}", "/api/pipelines") == ["demo-bm25"]
    assert len(by("view:all-engines", "/api/pipelines")) > 1
    assert cl.get("/api/datasets", headers={**h, "X-CST-Scope": "bogus:x"}).status_code == 400


def test_link_refuses_unknown_targets_and_class_counts_follow_the_pick(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import demo, server

    c = init_workspace("acme", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    b = SC.add_scope(c, "brand", "Acme Shop")
    with pytest.raises(ValueError, match="no scope 'acme-shop'"):
        SC.link(c, [{"kind": "dataset", "name": "demo-catalog"}], scope="acme-shop")
    with pytest.raises(ValueError, match="no engine"):
        SC.link(c, [{"kind": "dataset", "name": "demo-catalog"}], engine="nope")
    SC.link(c, [{"kind": "dataset", "name": "demo-catalog"}], scope=b["id"])
    monkeypatch.setattr(server, "_ctx", None)
    cl = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert cl.post("/api/scopes/link", headers=h, json={"items": [{"kind": "dataset", "name": "x"}], "scope": "acme-shop"}).status_code == 400
    everything = {x["name"]: x["datasets"] for x in cl.get("/api/dataset-classes", headers=h).json()["classes"]}
    scoped = {x["name"]: x["datasets"] for x in cl.get("/api/dataset-classes", headers={**h, "X-CST-Scope": f"scope:{b['id']}"}).json()["classes"]}
    assert sum(scoped.values()) <= 1 < sum(everything.values())


def test_runs_grading_and_signals_follow_the_pick(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import demo, investigations as INV, server

    c = init_workspace("acme", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    b = SC.add_scope(c, "brand", "Acme")
    eng = SC.add_engine(c, "Search", "search", [b["id"]], ["demo-bm25"])
    inv = INV.create(c, "Do returns help?", "")
    sig = INV.signal_create(c, "returns", "orders", "count(returns)", "why", investigation=inv["id"])
    SC.link(c, [{"kind": "investigation", "name": inv["id"]}], scope=b["id"])
    monkeypatch.setattr(server, "_ctx", None)
    cl = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    g = lambda path, pick=None: cl.get(path, headers={**h, **({"X-CST-Scope": pick} if pick else {})}).json()
    allruns = g("/api/runs")
    assert allruns and allruns[0]["created"] >= allruns[-1]["created"]
    mine = g("/api/runs", f"engine:{eng['id']}")
    assert mine and all("demo-bm25" in " ".join(r["arms"]) for r in mine)
    assert [q["name"] for q in g("/api/grading", f"scope:{b['id']}")] == ["demo-grading"]
    other = SC.add_scope(c, "brand", "Other")
    assert g("/api/grading", f"scope:{other['id']}") == [] and g("/api/runs", f"scope:{other['id']}") == []
    assert [s["id"] for s in g("/api/signals", f"scope:{b['id']}")] == [sig["id"]]
    assert g("/api/signals", f"scope:{other['id']}") == []
