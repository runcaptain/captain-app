"""Every screen against the app: the addresses its code names are served, none is registered twice, and the main
read each screen makes answers, through the app, in the shape the screen reads.

A screen breaks without a failing test when its address is not the one the app serves, or when another route
answers on it first. test_deployment_addresses.py does this tab by tab for the deployment page. This file covers
every other screen under defaults/mods, the frame's screens and the shared kit."""
from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient

import addresses as A
from captain_search import server
from captain_search import staging as SG

from test_staging import keychain, ws  # noqa: F401  (fixtures)


def test_every_address_a_screen_names_is_served_with_the_method_it_is_called_with():
    all_routes = A.routes(server.app)
    found, variable = A.named(A.screen_files())
    assert len(found) > 150, f"the screens' addresses were not found: {len(found)}"
    lost = sorted(f"{m} {p.replace(chr(0), '{...}')}  ({where[0]})" for (m, p), where in found.items() if not A.served(all_routes, m, p))
    assert not lost, "\n".join(lost)
    # Calls that pass a variable can't be read as text. They are helpers that are handed an address named
    # elsewhere, and a few addresses the services hand out. If this grows, look at what was added.
    assert variable <= 40, f"{variable} calls pass their address as a variable, which this test can't read"


def test_the_check_itself_rejects_a_wrong_address_and_a_wrong_method():
    r = A.routes(server.app)
    z = "\x00"
    assert A.served(r, "GET", f"/api/projects/{z}/staging/copies") and A.served(r, "GET", f"/api/datasets{z}")
    assert not A.served(r, "GET", f"/api/projects/{z}/staging/copiez")   # no such address
    assert not A.served(r, "PUT", f"/api/projects/{z}/staging/copies")   # the address, with a method nothing serves
    assert not A.served(r, "GET", f"/api/datasetz{z}") and not A.served(r, "DELETE", "/api/deployments")
    assert ("GET", "/api/team/connection") in r, "routes of an included router are read too"


def test_no_method_and_address_is_registered_twice_including_included_routers():
    twice = [k for k, n in Counter(A.routes(server.app)).items() if n > 1]
    assert not twice, twice


@pytest.fixture
def app(ws, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ws)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}

    def get(path, **params):
        r = c.get(path, headers=h, params=params)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
        return r.json()

    return get


def has(d: dict, *keys):
    missing = [k for k in keys if k not in d]
    assert not missing, f"missing {missing} in {sorted(d)}"


def test_project_page(app):
    has(app("/api/projects")[0], "id", "name", "description", "counts", "archived")
    st = app("/api/projects/shop/stages")
    has(st, "project", "inputs", "next", "notes", "evaluation", "experiments", "champion", "ship", "start")
    has(st["inputs"][0], "role", "label", "pinned", "required", "info", "rows")
    has(st["ship"], "steps")
    ex = app("/api/projects/shop/experiments")
    has(ex, "project", "champion", "pins", "experiments", "evaluation")
    has(ex["experiments"][0], "id", "pipeline", "is_champion", "evaluation", "created_at")
    has(app("/api/projects/shop/home"), "project", "datasets", "pipelines", "models", "runs", "counts", "empty")
    has(app("/api/projects/shop/state"), "project", "slots", "inputs", "pins", "experiments", "champion")


def test_runs_page(app):
    assert isinstance(app("/api/jobs"), list) and isinstance(app("/api/backtests"), list) and isinstance(app("/api/workflows"), list)
    live = app("/api/live")
    assert isinstance(live, list)
    if live:
        has(live[0], "id", "kind", "title", "status", "progress", "stage", "stages", "started", "by")


def test_data_page(app):
    ds = app("/api/datasets")
    has(ds[0], "name", "kind", "rows", "latest", "columns", "description", "by", "created")
    ex = app("/api/explorer")
    has(ex, "workspace", "tree", "actions")
    has(ex["tree"][0], "id", "kind", "label")
    assert isinstance(app("/api/connections"), list)


def test_compare_page(app):
    assert isinstance(app("/api/backtests"), list)


def test_launch_review_page(app):
    assert isinstance(app("/api/handoffs"), list)


def test_settings_page(app):
    sv = app("/api/services")
    has(sv, "storage", "connections", "compute", "registries", "mlflow", "model_keys")
    has(sv["model_keys"][0], "name", "label", "set")
    st = app("/api/settings")
    has(st, "compute", "costs", "staging")
    assert st["costs"]["confirm_above_usd"] == 5.0 and st["staging"]["local_limit_mb"] == 1024
    has(app("/api/compute")[0], "name", "kind", "purposes")
    has(app("/api/cloud/summary"), "active", "profiles", "compute", "storage_provider", "can_run_in_cloud")


def test_deployments_list(app, ws):  # noqa: F811
    assert app("/api/staging/deployments", which="live")["rows"] == []
    SG.stage(ws, "shop", target="local", allow_large=True)
    row = app("/api/staging/deployments", which="live")["rows"][0]
    has(row, "project", "name", "status", "state", "model_version", "where", "since_text", "copies", "running", "series", "errors", "line")
    SG.unstage(ws, "shop")
    assert app("/api/staging/deployments", which="stopped")["rows"][0]["state"] == "stopped"
