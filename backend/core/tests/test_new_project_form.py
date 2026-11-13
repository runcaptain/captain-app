"""The New project form: any kind of model, inputs the person edits before creating, and a trainer of their choice.
Every kind must give a project whose inputs, models and deploy sections work with whatever inputs were chosen."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from captain_search import project_state as PS
from captain_search import server
from captain_search.workspace import init_workspace


@pytest.fixture
def api(tmp_path, monkeypatch):
    ctx = init_workspace("t", str(tmp_path / "store"))
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ctx)
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    post = lambda path, body: c.post(path, headers=hd, json=body)  # noqa: E731
    post.get = lambda path: c.get(path, headers=hd)
    post.ctx = ctx
    return post


def _slots(ctx, pid):
    return [(s["id"], s["label"], s["accepts"], s["required"]) for s in PS.slots_of(PS.get(ctx, pid))]


def test_something_else_with_inputs_of_the_persons_own_and_their_own_training_command(api):
    r = api("/api/projects", {"name": "Fraud scores", "kind": "other", "what": "A fraud score for each payment, from past chargebacks",
                              "slots": [{"label": "Payments", "kind": "generic", "required": True, "why": "One row a payment."},
                                        {"label": "Chargebacks", "kind": "generic", "required": True},
                                        {"label": "Merchant notes"}],
                              "trainer": {"how": "command", "command": "python train_fraud.py --out result.yaml"}})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    doc = PS.get(api.ctx, pid)
    assert doc["kind"] == "other" and doc["recipe"] == {"id": "other", "label": "A fraud score for each payment, from past chargebacks", "source": "you"}
    assert doc["building"].startswith("A fraud score")
    assert _slots(api.ctx, pid) == [("payments", "Payments", ["generic"], True), ("chargebacks", "Chargebacks", ["generic"], True),
                                    ("merchant-notes", "Merchant notes", ["any"], False)]
    assert set(doc["pins"]) == {"payments", "chargebacks", "merchant-notes"}
    assert doc["trainer_choice"] == {"how": "command", "command": "python train_fraud.py --out result.yaml"}
    assert doc["trainer_suggestion"]["command"] == "python train_fraud.py --out result.yaml"
    # every section of its page answers: inputs, models and what is deployed
    for path in (f"/api/projects/{pid}/stages", f"/api/projects/{pid}/experiments", f"/api/projects/{pid}/staging", f"/api/projects/{pid}/deploy/hosts"):
        got = api.get(path)
        assert got.status_code == 200, f"{path}: {got.status_code} {got.text[:300]}"
    stages = api.get(f"/api/projects/{pid}/stages").json()
    assert [i["slot"] for i in stages["inputs"]] == ["payments", "chargebacks", "merchant-notes"]
    assert stages["next"] and "Payments" in str(stages["next"]) or stages["next"].get("slot") == "payments", stages["next"]


def test_a_recipes_inputs_are_a_starting_point_that_can_be_renamed_removed_and_added_to(api):
    r = api("/api/projects", {"name": "Store search", "kind": "search-events",
                              "slots": [{"id": "catalog", "label": "Products we sell"},      # renamed: it is still the catalog
                                        {"id": "events", "label": "Search events"},
                                        {"label": "Product embeddings", "kind": "generic"}],   # added; the recipe's others are removed
                              "trainer": {"how": "builtin"}})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    doc = PS.get(api.ctx, pid)
    by = {s["id"]: s for s in PS.slots_of(doc)}
    assert list(by) == ["catalog", "events", "product-embeddings"]
    assert by["catalog"]["label"] == "Products we sell" and by["catalog"]["role"] == "catalog" and by["catalog"]["required"], "renamed, and still what the recipe knows it as"
    assert by["product-embeddings"]["role"] == "custom" and by["product-embeddings"]["origin"] == "custom"
    assert doc["kind"] == "search-events" and doc["trainer_choice"] == {"how": "builtin"} and doc["trainer_suggestion"] is None
    assert api.get(f"/api/projects/{pid}/stages").status_code == 200


def test_their_own_repo_and_the_four_kinds_as_they_were(api):
    r = api("/api/projects", {"name": "Next best offer", "kind": "recommendations",
                              "trainer": {"how": "repo", "repo": "https://example.invalid/team/offers.git", "command": "make train"}})
    assert r.status_code == 200, r.text
    doc = PS.get(api.ctx, r.json()["id"])
    assert doc["trainer_choice"] == {"how": "repo", "command": "make train", "repo": "https://example.invalid/team/offers.git"}
    assert doc["trainer_suggestion"]["repo"] == "https://example.invalid/team/offers.git" and [s["id"] for s in PS.slots_of(doc)] == ["interactions", "items", "users"]
    # the plain call the app made before still works, for every built-in kind
    for kind in ("search-events", "search-judgments", "recommendations", "byo-pipeline"):
        r = api("/api/projects", {"name": f"Plain {kind}", "kind": kind})
        assert r.status_code == 200 and PS.get(api.ctx, r.json()["id"])["kind"] == kind


def test_a_form_that_is_wrong_makes_no_project(api):
    before = len(api.get("/api/projects").json())
    for body, why in (({"name": "A", "kind": "other"}, "Say what you are building"),
                      ({"name": "B", "kind": "other", "what": "x", "slots": [{"label": "  "}]}, "Every input needs a name"),
                      ({"name": "C", "kind": "other", "what": "x", "slots": [{"label": "Rows", "kind": "spreadsheet"}]}, "accepts dataset kinds"),
                      ({"name": "D", "kind": "other", "what": "x", "trainer": {"how": "magic"}}, "builtin, command or repo"),
                      ({"name": "E", "kind": "other", "what": "x", "trainer": {"how": "repo"}}, "address of your training repo"),
                      ({"name": "F", "kind": "no-such-kind"}, "")):
        r = api("/api/projects", body)
        assert r.status_code in (400, 404) and why in r.text, (body, r.status_code, r.text[:200])
    r = api("/api/projects", {"name": "G", "kind": "other", "what": "x", "slots": [{"label": "Rows"}, {"label": "rows"}]})
    assert r.status_code == 400 and "Give each its own name" in r.text
    assert len(api.get("/api/projects").json()) == before, "nothing was made"
