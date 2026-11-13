"""Names a person types and the ids Captain keeps: a name is never refused for its spelling."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from captain_search import cloud, naming as NM, server
from captain_search.workspace import init_workspace


@pytest.mark.parametrize("typed,made", [
    ("gpu cloud", "gpu-cloud"), ("GPU cloud", "gpu-cloud"), ("  GPU   Cloud  ", "gpu-cloud"), ("gpu_cloud", "gpu-cloud"),
    ("Work (us-west)", "work-us-west"), ("Outdoor store search!", "outdoor-store-search"), ("a/b\\c", "abc"), ("--x--", "x"),
    ("Größe 2", "gre-2"), ("x" * 200, "x" * 60),
])
def test_an_id_is_made_from_whatever_was_typed(typed, made):
    assert NM.make_id(typed) == made and NM.is_id(made)


def test_nothing_usable_gets_the_kinds_own_word_and_never_an_error():
    assert NM.make_id("!!!", "login") == "login" and NM.make_id("", "project") == "project" and NM.make_id(None) == "item"
    assert NM.propose("???", "cloud", [])["id"] == "login"


def test_a_taken_id_gets_the_next_free_one_and_says_so():
    assert NM.free("gpu-cloud", []) == "gpu-cloud"
    assert NM.free("gpu-cloud", ["gpu-cloud"]) == "gpu-cloud-2"
    assert NM.free("gpu-cloud", ["gpu-cloud", "gpu-cloud-2", "gpu-cloud-3"]) == "gpu-cloud-4"
    assert NM.free("gpu-cloud-2", ["gpu-cloud-2"]) == "gpu-cloud-3"
    p = NM.propose("GPU cloud", "cloud", ["gpu-cloud"])
    assert p == {"label": "GPU cloud", "id": "gpu-cloud", "taken": True, "next": "gpu-cloud-2",
                 "text": "You already have a cloud account called gpu-cloud. Captain will call this one gpu-cloud-2."}
    assert NM.propose("GPU cloud", "cloud", [])["text"] == "Captain will call it gpu-cloud."


def test_is_id_holds_the_one_rule():
    for good in ("a", "gpu-cloud", "a1-b2", "x" * 60):
        assert NM.is_id(good)
    for bad in ("", "GPU", "gpu cloud", "-a", "a-", "a--b", "a_b", "a/b", "x" * 61):
        assert not NM.is_id(bad), bad


@pytest.fixture
def api(tmp_path, monkeypatch):
    ctx = init_workspace("t", str(tmp_path / "store"))
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ctx)
    have: dict = {}
    monkeypatch.setattr(cloud, "profiles", lambda: [dict(name=n, **p) for n, p in have.items()])

    def add(name, provider, auth, region, fields, secrets):
        cloud.check_name(name)   # the real rule: a bad id would raise the raw "bad name" sentence here
        have[name] = {"provider": provider, "auth": auth, "region": region, "fields": fields}
        return {"name": name, **have[name]}

    monkeypatch.setattr(cloud, "add_profile", add)
    monkeypatch.setattr(cloud, "set_label", lambda name, label: have[name].update(label=label))
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    call = lambda method, path, **kw: getattr(c, method)(path, headers=hd, **kw)  # noqa: E731
    call.have = have
    return call


def test_the_name_edgar_typed_connects_and_no_raw_sentence_ever_comes_back(api):
    assert api("get", "/api/names/propose", params={"label": "gpu cloud", "kind": "cloud"}).json()["id"] == "gpu-cloud"
    r = api("post", "/api/cloud/profiles", json={"name": "gpu cloud", "provider": "modal", "auth": "local"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "gpu-cloud" and api.have["gpu-cloud"]["label"] == "gpu cloud"
    assert "bad name" not in r.text
    # the same words again: said plainly, with the status a form can act on, and nothing is overwritten
    r = api("post", "/api/cloud/profiles", json={"name": "GPU Cloud", "provider": "modal", "auth": "local"})
    assert r.status_code == 409 and (r.json().get("detail") or r.json().get("error")) == "You already have a cloud account called gpu-cloud. Give this one another name."
    p = api("get", "/api/names/propose", params={"label": "GPU Cloud", "kind": "cloud"}).json()
    assert p["taken"] and p["next"] == "gpu-cloud-2" and p["text"].endswith("Captain will call this one gpu-cloud-2.")
    # a form that sends the id it was offered, with the words as the label
    r = api("post", "/api/cloud/profiles", json={"name": p["next"], "label": "GPU Cloud", "provider": "modal", "auth": "local"})
    assert r.status_code == 200 and r.json()["name"] == "gpu-cloud-2" and api.have["gpu-cloud-2"]["label"] == "GPU Cloud"
    for kind in ("project", "compute", "connection", "notebook"):
        got = api("get", "/api/names/propose", params={"label": "My Thing", "kind": kind})
        assert got.status_code == 200 and got.json()["id"] == "my-thing", kind


def test_the_modal_sign_in_is_looked_for_without_a_button_and_the_label_holds_no_command(api, monkeypatch):
    modal = next(p for p in cloud.PROVIDERS if p["provider"] == "modal")
    assert modal["auth"][0]["label"] == "This computer's Modal sign-in", "no command in a label"
    monkeypatch.setattr(cloud, "_test_modal", lambda: {"ok": False})
    got = api("get", "/api/cloud/signin", params={"provider": "modal", "auth": "local"}).json()
    assert got == {"found": False, "text": "No Modal sign-in on this computer yet. Run `modal token new` in a terminal, then come back."}
    monkeypatch.setattr(cloud, "_test_modal", lambda: {"ok": True})
    assert api("get", "/api/cloud/signin", params={"provider": "modal", "auth": "local"}).json() == {"found": True, "text": None}
    assert api("get", "/api/cloud/signin", params={"provider": "aws", "auth": "profile"}).json() == {"found": None, "text": None}
