"""One call deploys end to end on every host: which places are offered, the plan a person reads once, the run with
its named steps (first-time work included), a failure midway that says where and what exists, retry and remove.
Against the stand-ins for each cloud. Nothing here calls a real cloud."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import cloud
from captain_search import compute as C
from captain_search import staging as SG
from captain_search.cli.main import app as cli
from captain_search.staging import deploy as DP
from captain_search.staging import hosts as H
from captain_search.staging import serving_setup as SS

from test_hosting_clouds import azure, google  # noqa: F401  (fixtures)
from test_hosting_modal import ENV, modal  # noqa: F401  (fixtures)
from test_serving_setup import Fake as FakeAwsSetup
from test_staging import keychain, ws  # noqa: F401  (fixtures)

LOGINS = {"aws": {"name": "aws-main", "provider": "aws", "auth": "profile", "region": "us-west-1", "fields": {"profile": "main"}},
          "modal": {"name": "modal", "provider": "modal", "auth": "local", "region": None, "fields": {"environment": ENV}},
          "gcp": {"name": "g", "provider": "gcp", "auth": "adc", "region": None, "fields": {"project": "demo-project"}},
          "azure": {"name": "a", "provider": "azure", "auth": "default", "region": None, "fields": {"subscription_id": "0000"}}}


@pytest.fixture
def logins(monkeypatch):
    """Which cloud logins are connected in Captain. Tests add to the list."""
    have: list[dict] = []
    monkeypatch.setattr(cloud, "profiles", lambda: [dict(p) for p in have])
    real = cloud.get_profile
    monkeypatch.setattr(cloud, "get_profile", lambda name: next((dict(p) for p in have if p["name"] == name), None) or real(name))
    return have


def _events():
    seen = []
    return seen, (lambda kind, **d: seen.append((kind, d)))


# ---- which places are offered --------------------------------------------------------------------


def test_only_this_computer_and_the_connected_logins_are_offered(ws, logins, modal):  # noqa: F811
    o = DP.offers(ws, "shop")
    assert [h["host"] for h in o["hosts"]] == ["local"] and o["model_version"] == ws.candidate and o["beside"] == ws.baseline
    here = o["hosts"][0]
    assert here["can_deploy"] and here["fit"]["outcome"] in ("fits", "tight") and not here["first_time"] and not here["reachable"]
    # Modal is on this computer, and still not offered until someone connects it in Captain.
    logins.append(LOGINS["modal"])
    logins.append(LOGINS["gcp"])
    o = DP.offers(ws, "shop")
    assert [(h["host"], h["login"]) for h in o["hosts"]] == [("local", None), ("modal", "modal"), ("gcp", "g")]
    m, g = o["hosts"][1], o["hosts"][2]
    assert m["can_deploy"] and m["first_time"] and m["proven"] == "2026-11-11" and m["sleeps"] and m["reachable"]
    assert g["first_time"] and g["proven"] is None and g["proven_text"] == "Not run on a real account yet. Tested against a stand-in only."
    assert g["region"] == "us-central1" and "cautious guess" in g["minutes"]["text"] and "Measured on 2026-11-11" in m["minutes"]["text"]
    assert not g["estimate"]["available"]  # no price for a place that has not run on a real account
    assert [h["id"] for h in o["hosts"]] == ["local", "modal:modal", "gcp:g"] and o["connected"] == 2 and o["last_id"] is None
    assert "nothing while it sleeps. Up to $" in m["estimate"]["text"] and "if a copy never slept" in m["estimate"]["text"] and 40 < m["estimate"]["monthly_usd"] < 55
    assert modal.calls == [] and not modal.volumes  # asking what is offered creates nothing and calls nothing


def test_a_login_whose_check_failed_and_a_model_too_big_for_here_say_why(ws, logins, monkeypatch):  # noqa: F811
    logins.append({**LOGINS["aws"], "last_test": {"ok": False, "error": "ExpiredToken: the security token is expired"}})
    monkeypatch.setattr("captain_search.staging.fit.check", lambda *a, **k: {"outcome": "wont-fit", "text": "This catalog needs about 9 GB of memory, and this computer has 3 GB free. Stage it in your cloud."})
    o = DP.offers(ws, "shop")["hosts"]
    assert not o[0]["can_deploy"] and "9 GB of memory" in o[0]["why_not"] and o[0]["why_not_action"] is None
    assert not o[1]["can_deploy"] and "last check failed: ExpiredToken" in o[1]["why_not"]
    with pytest.raises(ValueError, match="No Google Cloud login is connected in Captain"):
        DP.plan(ws, "shop", "gcp")


# ---- this computer -------------------------------------------------------------------------------


def test_this_computer_deploys_with_no_confirm_and_skips_the_cloud_steps(ws, logins):  # noqa: F811
    p = DP.plan(ws, "shop", "local")
    assert not p["needs_confirm"] and p["cost"]["free"] and p["creates"]["count"] == 0 and [s["id"] for s in p["steps"]] == list(DP.STEPS)
    seen, hear = _events()
    out = DP.run(ws, "shop", "local", on_event=hear)
    assert out["status"] == "done" and out["address"].startswith("http://127.0.0.1:")
    assert [(s["id"], s["status"]) for s in out["steps"]] == [("check", "done"), ("access", "skipped"), ("image", "skipped"), ("start", "done"), ("ready", "done")]
    assert [d["title"] for k, d in seen if k == "step"] == ["Checking your account", "Starting the model", "Checking it answers"] and seen[-1][0] == "done"
    assert SG.state(ws, "shop")["status"] == "running" and DP.status(ws, "shop")["can"] == []
    assert DP.remove(ws, "shop")["status"] == "removed" and SG.state(ws, "shop")["status"] == "stopped"


# ---- Modal: the first time, the second time, a failure, retry, remove ----------------------------------------


def test_modal_first_deploy_sets_up_then_starts_as_one_run(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    p = DP.plan(ws, "shop", "modal")
    assert p["first_time"] and p["needs_confirm"] and p["login"] == "modal"
    assert [g["title"] for g in p["creates"]["groups"]] == ["The first time only", "For this model"] and p["creates"]["count"] == 4
    assert "A Modal volume" in p["creates"]["short"] and p["creates"]["groups"][0]["summary"] == p["creates"]["short"] and p["id"] == "modal:modal" and p["exists"] == []
    assert p["cost"]["sleeps"] and "nothing while it sleeps" in p["cost"]["text"] and 40 < p["cost"]["monthly_usd"] < 55 and p["cost"]["setup_usd"] == 0
    assert p["minutes"]["measured"] == "2026-11-11" and p["minutes"]["low"] == 2
    assert not modal.volumes and DP.status(ws, "shop")["status"] == "none"  # the plan made nothing
    with pytest.raises(SG.NeedsConfirm) as e:
        DP.run(ws, "shop", "modal")
    assert e.value.plan["first_time"]
    seen, hear = _events()
    out = DP.run(ws, "shop", "modal", confirm=True, on_event=hear)
    assert out["status"] == "done" and out["address"].startswith("https://") and out["made_setup"]
    assert [(s["id"], s["status"]) for s in out["steps"]] == [("check", "done"), ("access", "done"), ("image", "skipped"), ("start", "done"), ("ready", "done")]
    assert [d["title"] for k, d in seen if k == "step"] == ["Checking your account", "Creating storage", "Starting the model", "Checking it answers"]
    assert any(k == "line" and "Creating the Modal volume" in d["text"] and d["step"] == "access" for k, d in seen)
    assert (ENV, "captain-serving") in modal.volumes and SG.state(ws, "shop")["kind"] == "modal-endpoint"  # the login's environment was used
    assert (out["staged"]["host"], out["staged"]["login"]) == ("modal", "modal") and DP.offers(ws, "shop")["last_id"] == "modal:modal"
    assert p["cost"]["text"] == DP.offers(ws, "shop")["hosts"][1]["estimate"]["text"]  # the card and the confirm say the same thing
    # The second time there is nothing to set up: the plan is the model alone, and the run says so.
    p2 = DP.plan(ws, "shop", "modal")
    assert not p2["first_time"] and [g["title"] for g in p2["creates"]["groups"]] == [] and "already there" in p2["creates"]["short"]
    assert p2["replaces"] == ws.candidate and p2["minutes"]["low"] == 1 and {x["resource"] for x in p2["exists"]} == {"modal-volume", "compute-target"}
    out2 = DP.run(ws, "shop", "modal", confirm=True)
    assert [s["status"] for s in out2["steps"]][1:3] == ["skipped", "skipped"] and out2["status"] == "done" and len(modal.volumes) == 1


def test_a_failure_midway_says_which_step_what_exists_and_retry_picks_it_up(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    modal.fail_deploy = "Image build failed: pip could not resolve dependencies"
    seen, hear = _events()
    with pytest.raises(Exception, match="Modal could not deploy the endpoint"):
        DP.run(ws, "shop", "modal", confirm=True, on_event=hear)
    st = DP.status(ws, "shop")
    assert st["status"] == "failed" and st["failed_step"] == {"id": "start", "title": "Starting the model"} and "Image build failed" in st["error"]
    assert [(s["id"], s["status"]) for s in st["steps"]][:4] == [("check", "done"), ("access", "done"), ("image", "skipped"), ("start", "failed")]
    assert {x["resource"] for x in st["exists_now"]} == {"modal-volume", "compute-target"} and st["can"] == ["retry", "remove"]
    assert 'stopped at "Starting the model"' in st["text"] and seen[-1] == ("failed", {"step": "start", "title": "Starting the model", "error": st["error"]})
    # Retry is the same call again: the volume is kept, and only the model is started.
    modal.fail_deploy = None
    out = DP.run(ws, "shop", "modal", confirm=True)
    assert out["status"] == "done" and out["made_setup"] and len(modal.volumes) == 1 and out["steps"][1]["status"] == "skipped"
    # Remove takes the model and, since this deploy made the first-time work and nothing else uses it, that too.
    r = DP.remove(ws, "shop")
    assert r["status"] == "removed" and r["removed_setup"] and not modal.volumes and not modal.secrets
    assert H.get("modal").leftovers(ws) == [] and DP.status(ws, "shop")["status"] == "removed"


def test_a_failed_first_step_can_be_removed_and_leaves_nothing(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    modal.fail_deploy = "no capacity"
    with pytest.raises(Exception):
        DP.run(ws, "shop", "modal", confirm=True)
    assert DP.remove(ws, "shop")["removed_setup"] and not modal.volumes and C.get(ws, "modal-serving").get("deleted")


def test_two_deploys_of_one_project_do_not_run_at_once_and_a_dead_one_frees_itself(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    DP._write(ws, "shop", "staging", {"host": "modal", "status": "running", "steps": [{"id": "start", "title": "Starting the model", "status": "running", "lines": []}]})
    with pytest.raises(ValueError, match="already running"):
        DP.run(ws, "shop", "modal", confirm=True)
    doc, _ = DP._read(ws, "shop", "staging")
    doc["updated_at"] = "2026-01-01T00:00:00+00:00"  # it said nothing for a long time: its process is gone
    ws.store.put_if(DP._key("shop", "staging"), json.dumps(doc).encode(), ws.store.get_with_tag(DP._key("shop", "staging"))[1])
    st = DP.status(ws, "shop")
    assert st["status"] == "failed" and st["failed_step"]["id"] == "start" and "Retry picks up where it stopped" in st["error"]
    assert DP.run(ws, "shop", "modal", confirm=True)["status"] == "done"


# ---- Google Cloud and Azure: the first time has an image step, and a missing permission is a step failure ------


def test_google_first_deploy_has_all_five_steps_and_a_missing_permission_names_its_step(ws, logins, google):  # noqa: F811
    logins.append(LOGINS["gcp"])
    p = DP.plan(ws, "shop", "gcp")
    assert p["first_time"] and p["region"] == "us-central1" and p["proven"] is None and "Not run on a real account yet" in p["text"]
    assert [i["resource"] for i in p["creates"]["groups"][0]["items"]][:2] == ["API switched on", "API switched on"]
    assert p["cost"]["hourly_usd"] is None and "bills only while a copy is awake" in p["cost"]["text"] and "nothing while it sleeps" in p["cost"]["text"] and p["cost"]["setup_usd"] == 0.02
    assert p["login_policy"]["roles"] and p["minutes"]["measured"] is None
    google.deny = {"POST https://storage.googleapis.com/storage/v1/b"}
    with pytest.raises(H.MissingPermission):
        DP.run(ws, "shop", "gcp", confirm=True)
    st = DP.status(ws, "shop")
    assert st["failed_step"] == {"id": "access", "title": "Creating permissions and storage"} and "storage.buckets.create" in st["error"]
    assert "roles/storage.admin" in [r["role"] for r in st["policy"]["roles"]] and st["can"] == ["retry", "remove"]
    assert {x["resource"] for x in st["exists_now"]} == {"api", "service-account"}
    google.deny = set()
    seen, hear = _events()
    out = DP.run(ws, "shop", "gcp", confirm=True, on_event=hear)
    assert out["status"] == "done" and [s["status"] for s in out["steps"]] == ["done"] * 5 and out["address"].endswith(".run.app")
    assert [d["title"] for k, d in seen if k == "step"] == ["Checking your account", "Creating permissions and storage", "Building the model's image",
                                                           "Starting the model", "Checking it answers"]
    assert len(google.accounts) == 2  # the two made before the failure were kept, not made again
    assert DP.remove(ws, "shop")["removed_setup"] and not google.services and not google.buckets and not google.accounts


def test_azure_first_deploy_and_the_registry_in_the_monthly_cost(ws, logins, azure):  # noqa: F811
    logins.append(LOGINS["azure"])
    p = DP.plan(ws, "shop", "azure")
    assert p["first_time"] and "One resource group" in p["creates"]["short"] and "Its registry costs about $5" in p["cost"]["text"]
    out = DP.run(ws, "shop", "azure", confirm=True)
    assert out["status"] == "done" and [s["status"] for s in out["steps"]] == ["done"] * 5 and out["steps"][1]["title"] == "Creating permissions and storage"
    assert DP.remove(ws, "shop")["removed_setup"] and not azure.res


def test_remove_leaves_the_first_time_work_when_another_project_has_a_model_there(ws, logins, modal):  # noqa: F811
    from captain_search import project_state as PS
    from captain_search import projects as PR

    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    other = PR.create(ws, "Outlet")
    PS._update(ws, other["id"], lambda d: d.update(staging={"staging": {"status": "running", "target": "modal-serving", "kind": "modal-endpoint"}}))
    said = []
    r = DP.remove(ws, "shop", progress=said.append)
    assert r["status"] == "removed" and not r["removed_setup"] and modal.volumes and any("Outlet has a model there" in t for t in said)


# ---- AWS: the first-time steps, against the setup stand-in ---------------------------------------


def test_aws_first_deploy_runs_setup_as_named_steps(ws, logins, monkeypatch):  # noqa: F811
    f = FakeAwsSetup()
    monkeypatch.setattr(cloud, "aws_session", lambda profile, region=None: f)
    monkeypatch.setattr(SS, "docker_answers", lambda timeout=5.0: False)
    ws.workspace.storage_uri = "s3://team-bucket/captain/shop"
    logins.append(LOGINS["aws"])
    o = DP.offers(ws, "shop")["hosts"][1]
    assert o["host"] == "aws" and o["first_time"] and o["proven"] == "2026-11-11" and not o["sleeps"] and o["region"] == "us-west-1"
    assert "About 4 minutes" in o["minutes"]["text"] and o["estimate"]["available"]
    p = DP.plan(ws, "shop", "aws")
    assert "A role that can only read this model" in p["creates"]["short"] and p["creates"]["groups"][1]["items"][0]["resource"] == "SageMaker endpoint"
    assert p["cost"]["hourly_usd"] > 0 and "a month if it runs all the time" in p["cost"]["text"] and p["login_policy"]["Statement"]
    staged = {}
    monkeypatch.setattr(DP, "stage", lambda ctx, pid, *a, **k: staged.update(target=a[3]) or {"endpoint": {"name": "captain-shop-staging"}})
    seen, hear = _events()
    out = DP.run(ws, "shop", "aws", confirm=True, on_event=hear)
    assert out["status"] == "done" and staged["target"] == "aws-serving" and C.get(ws, "aws-serving")["kind"] == "aws-sagemaker"
    assert [d["title"] for k, d in seen if k == "step"] == ["Checking your account", "Creating permissions", "Building the model's image", "Starting the model", "Checking it answers"]
    lines = {d["step"]: d["text"] for k, d in seen if k == "line"}
    assert "role" in lines["access"].lower() or "repository" in lines["access"].lower()


# ---- the command and the API ---------------------------------------------------------------------


def test_the_command_deploys_on_a_host_with_one_confirm_and_a_failure_says_what_to_do(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    run = CliRunner()
    dry = run.invoke(cli, ["--json", "--dry-run", "deploy", "shop", "--on", "modal"])
    assert dry.exit_code == 0 and json.loads(dry.output)["first_time"] and not modal.volumes
    no = run.invoke(cli, ["--json", "deploy", "shop", "--on", "modal"])
    assert no.exit_code != 0 and "Confirm first with --yes" in no.output and not modal.volumes
    modal.fail_deploy = "no capacity right now"
    bad = run.invoke(cli, ["deploy", "shop", "--on", "modal", "--yes"])
    assert bad.exit_code != 0 and 'stopped at "Starting the model"' in bad.output and "What exists now: modal-volume captain-serving" in bad.output
    assert "Run the same command again" in bad.output and "cst deploy shop --remove" in bad.output
    modal.fail_deploy = None
    ok = run.invoke(cli, ["deploy", "shop", "--on", "modal", "--yes"])
    assert ok.exit_code == 0 and "[4/5] Starting the model" in ok.output and ok.output.strip().splitlines()[-1].startswith("https://")
    gone = run.invoke(cli, ["deploy", "shop", "--remove"])
    assert gone.exit_code == 0 and "Nothing of this deploy is left" in gone.output and not modal.volumes
    assert run.invoke(cli, ["deploy", "shop", "--on", "azure", "--yes"]).exit_code != 0  # no Azure login connected


def test_the_api_plans_confirms_runs_with_live_steps_and_removes(ws, logins, modal, monkeypatch, tmp_path):  # noqa: F811
    from captain_search import live as LV
    from captain_search import server

    monkeypatch.setenv("CST_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server, "ctx", lambda: ws)
    logins.append(LOGINS["modal"])
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    hosts = c.get("/api/projects/shop/deploy/hosts", headers=h).json()
    assert [x["host"] for x in hosts["hosts"]] == ["local", "modal"]
    assert c.get("/api/projects/shop/deploy", headers=h).json()["status"] == "none"
    r = c.post("/api/projects/shop/deploy", json={"host": "modal"}, headers=h)
    assert r.status_code == 409 and r.json()["needs_confirm"] and r.json()["plan"]["creates"]["count"] == 4 and not modal.volumes
    assert c.post("/api/projects/shop/deploy", json={"host": "gcp", "confirm": True}, headers=h).status_code == 400
    got = c.post("/api/projects/shop/deploy", json={"host": "modal", "confirm": True}, headers=h).json()
    for _ in range(200):
        j = c.get(f"/api/jobs/{got['job']}", headers=h).json()
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "done", j
    run = LV.read(got["run"])
    stages = [e["name"] for e in run["events"] if e["type"] == "stage"]
    assert stages == ["Checking your account", "Creating storage", "Starting the model", "Checking it answers"] and run["run"]["status"] == "done"
    st = c.get("/api/projects/shop/deploy", headers=h).json()
    assert st["status"] == "done" and st["staged"]["status"] == "running" and st["staged"]["endpoint"]["url"]
    job = c.delete("/api/projects/shop/deploy", headers=h).json()["job"]
    for _ in range(200):
        j = c.get(f"/api/jobs/{job}", headers=h).json()
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "done" and j["result"]["removed_setup"] and not modal.volumes


def test_steps_can_follow_the_five_and_one_that_fails_does_not_undo_the_deploy(ws, logins, monkeypatch):  # noqa: F811
    monkeypatch.setattr(DP, "AFTER", [])
    ran = []
    DP.after_step("match", "Checking it matches training", lambda ctx, pid, env, say: (_ for _ in ()).throw(ValueError("Served features don't match training: 91% of 50 checked")))
    DP.after_step("publish", "Saving this version to your model registry", lambda ctx, pid, env, say: ran.append(pid) or say("Saved."))
    DP.after_step("never", "Not for this project", lambda *a: ran.append("never"), when=lambda ctx, pid: False)
    p = DP.plan(ws, "shop", "local")
    assert [s["id"] for s in p["steps"]] == [*DP.STEPS, "match", "publish"]
    seen, hear = _events()
    out = DP.run(ws, "shop", "local", on_event=hear)
    assert out["status"] == "done" and SG.state(ws, "shop")["status"] == "running" and ran == ["shop"]  # still up, and the next step still ran
    steps = {s["id"]: s for s in out["steps"]}
    assert steps["match"]["status"] == "failed" and "91% of 50 checked" in steps["match"]["lines"][-1] and steps["publish"]["status"] == "done"
    assert out["soft_failed"] == [{"id": "match", "title": "Checking it matches training", "error": "Served features don't match training: 91% of 50 checked"}]
    assert [d["index"] for k, d in seen if k == "step"][-1] == 7 and [d["of"] for k, d in seen if k == "step"][-1] == 7
    # "Checking it answers" sent a few of the project's own queries through, as test traffic, for what follows to read.
    assert any("test request" in ln for ln in steps["ready"]["lines"])
    h = SG.call(ws, SG.state(ws, "shop"), "status", {})
    assert h["traffic"]["test"] == DP.TEST_REQUESTS and h["traffic"]["real"] == 0
    DP.after_step("hard", "Must pass", lambda *a: (_ for _ in ()).throw(ValueError("no")), soft=False)
    with pytest.raises(ValueError, match="no"):
        DP.run(ws, "shop", "local")
    assert DP.status(ws, "shop")["failed_step"]["id"] == "hard"
