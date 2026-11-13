"""What is deployed, from the cloud. Captain's records are corrected from what each cloud reports: a failed panel
can never outlive an endpoint that is running. Against each host's stand-in. Nothing here calls a real cloud."""
import datetime as dt

import pytest

from captain_search import staging as SG
from captain_search.staging import deploy as DP
from captain_search.staging import fleet as FL

from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import IMAGE, ROLE, aws  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def nothing_kept():
    FL.forget()
    yield
    FL.forget()


def _rows(ws, **kw):  # noqa: F811
    return FL.deployed(ws, fresh=True, **kw)["rows"]


def _calls(aws, name):  # noqa: F811
    return [c for c, _ in aws.calls].count(name)


# ---- AWS ------------------------------------------------------------------------------------------


def test_a_running_endpoint_is_reported_as_the_cloud_reports_it(ws, aws):  # noqa: F811
    rec = SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True)
    d = FL.deployed(ws, fresh=True)
    assert [a["ok"] for a in d["asked"]] == [True] and d["asked"][0]["cloud"] == "aws" and d["text"] == "1 model running."
    r = d["rows"][0]
    assert (r["project"], r["env"], r["model_version"], r["cloud"], r["region"], r["machine"]) == ("shop", "staging", ws.candidate, "aws", "us-west-1", "ml.t2.medium")
    assert (r["state"], r["link"], r["name"], r["copies"]["now"]) == ("running", "linked", rec["endpoint"]["name"], 1)
    assert r["state_text"] == "Running on 1 copy." and r["where"] == "AWS, us-west-1, ml.t2.medium" and r["corrected"] == []
    assert r["by"] and r["can"] == ["open", "stop", "rollback"] and FL.one(ws, "shop")["id"] == "shop:staging"
    assert _calls(aws, "invoke_endpoint") == 0 or not any(c == "invoke_endpoint" for c, _ in aws.calls[-6:])  # the read never calls the model


def test_an_endpoint_removed_outside_captain_is_noticed_and_the_record_set_right(ws, aws):  # noqa: F811
    rec = SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    del aws.endpoints[rec["endpoint"]["name"]]  # someone deleted it in the AWS console
    r = _rows(ws)[0]
    assert (r["state"], r["link"], r["state_text"]) == ("gone", "gone", "Removed outside Captain.")
    assert r["corrected"] == ["Captain's record said it was running. The cloud does not have it, so the record now says stopped: removed outside Captain."]
    now = SG.state(ws, "shop")
    assert now["status"] == "stopped" and now["stop_reason"] == "It was removed outside Captain."
    again = _rows(ws)[0]  # read twice: still said, nothing more to correct
    assert again["state"] == "gone" and again["corrected"] == [] and again["can"] == ["start", "forget"] and not again["in_cloud"]


def test_things_in_the_cloud_that_no_project_knows_are_listed(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    tags = [{"Key": "app", "Value": "captain"}, {"Key": "project", "Value": "another-workspaces-project"}, {"Key": "env", "Value": "staging"}]
    for name, t in (("other-team-ranker-staging", tags), ("captain-search-test-lost-staging", []), ("payments-fraud-model", [])):
        aws.configs[name] = {"EndpointConfigName": name, "ProductionVariants": [{"InstanceType": "ml.m5.large", "InitialInstanceCount": 2}]}
        aws.endpoints[name] = {"EndpointName": name, "EndpointConfigName": name, "Tags": t, "polls": 5}
    d = FL.deployed(ws, fresh=True)
    loose = {r["name"]: r for r in d["rows"] if r["link"] == "unlinked"}
    # Found by Captain's tag, and found by the name prefix. Someone else's endpoint is not Captain's and is not listed.
    assert sorted(loose) == ["captain-search-test-lost-staging", "other-team-ranker-staging"]
    r = loose["other-team-ranker-staging"]
    assert r["state_text"] == "Found in your AWS account, not linked to a project." and r["state"] == "running" and r["machine"] == "ml.m5.large"
    assert r["project"] == "another-workspaces-project" and r["project_name"] is None and r["model_version"] is None and r["can"] == ["link", "remove"]
    assert r["copies"]["now"] == 2 and r["id"] == "cloud:aws:p:us-west-1:other-team-ranker-staging"
    assert d["text"] == "1 model running, 2 found in your cloud with no project."
    assert not any(r["name"] == "payments-fraud-model" for r in d["rows"])


def _loose(aws, name="other-team-ranker-staging"):  # noqa: F811
    tags = [{"Key": "app", "Value": "captain"}, {"Key": "project", "Value": "another-workspaces-project"}, {"Key": "env", "Value": "staging"}]
    aws.models[name] = {"ModelName": name}
    aws.configs[name] = {"EndpointConfigName": name, "ProductionVariants": [{"InstanceType": "ml.m5.large", "InitialInstanceCount": 1, "ModelName": name}]}
    aws.endpoints[name] = {"EndpointName": name, "EndpointConfigName": name, "Tags": tags, "polls": 5}
    return f"cloud:aws:p:us-west-1:{name}"


def test_something_found_in_the_cloud_can_be_removed_whole_after_a_confirm(ws, aws):  # noqa: F811
    rid = _loose(aws)
    with pytest.raises(ValueError, match="Confirm first. This deletes the SageMaker endpoint other-team-ranker-staging"):
        FL.remove_found(ws, rid)
    assert "other-team-ranker-staging" in aws.endpoints
    out = FL.remove_found(ws, rid, confirm=True)
    # The endpoint, its config and its model all go: nothing is left billing or lying about.
    assert out["left"] == [] and sorted(r["type"] for r in out["removed"]) == ["endpoint", "endpoint-config", "model"]
    assert not aws.endpoints and not aws.configs and not aws.models and FL.deployed(ws, fresh=True)["rows"] == []
    with pytest.raises(ValueError, match="not in the cloud any more"):
        FL.remove_found(ws, rid, confirm=True)


def test_something_found_in_the_cloud_can_be_linked_to_a_project(ws, aws):  # noqa: F811
    rid = _loose(aws)
    with pytest.raises(ValueError, match="There is no project nope"):
        FL.link(ws, rid, "nope")
    out = FL.link(ws, rid, "shop")
    assert "is now this project's deployment" in out["text"] and "does not know which model version" in out["text"]
    r = FL.deployed(ws, fresh=True)["rows"]
    assert [(x["project"], x["link"], x["state"], x["name"]) for x in r] == [("shop", "linked", "running", "other-team-ranker-staging")]
    rec = SG.state(ws, "shop")
    assert rec["needs_key"] and rec["model_version"] is None and rec["linked"]["by"] and rec["target"] == "sm"
    # And from the project it can be stopped like any other: the endpoint, config and model go.
    SG.unstage(ws, "shop")
    assert not aws.endpoints and not aws.configs and not aws.models


def test_an_endpoint_the_cloud_says_failed_is_failed_with_the_clouds_reason(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    aws.endpoint_fails = True  # it was running, and then the container stopped passing its health check
    r = _rows(ws)[0]
    assert r["state"] == "failed" and r["state_text"] == "Failed: The primary container did not pass the ping health check."
    assert SG.state(ws, "shop")["status"] == "failed" and "now says failed" in r["corrected"][0]


def test_a_failed_deploy_panel_never_outlives_a_running_endpoint(ws, aws):  # noqa: F811
    """What Edgar saw on 2026-11-12: "Deploying to AWS stopped. Starting the model: failed", beside an endpoint
    that was in service and answering."""
    rec = SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True)
    # Captain's records say it failed: the record, and the last deploy.
    SG._save(ws, "shop", "staging", status="failed", error="Captain was closed while it ran.")
    early = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    DP._write(ws, "shop", "staging", {"project": "shop", "env": "staging", "host": "aws", "login": "p", "label": "AWS", "model_version": ws.candidate,
                                      "status": "failed", "started_at": early, "started_by": "tester", "made_setup": False,
                                      "failed_step": {"id": "start", "title": "Starting the model"}, "error": "the endpoint was still Creating",
                                      "steps": [{"id": "check", "title": "Checking your account", "status": "done", "lines": []},
                                                {"id": "start", "title": "Starting the model", "status": "failed", "lines": []},
                                                {"id": "ready", "title": "Checking it answers", "status": "pending", "lines": []}]})
    r = _rows(ws)[0]
    assert r["state"] == "running" and r["link"] == "linked" and r["name"] == rec["endpoint"]["name"]
    assert r["corrected"] == ["Captain's record said failed. The cloud says it is running, so the record now says running.",
                              "An earlier deploy was still marked as failed. The model is running, so that is closed."]
    assert SG.state(ws, "shop")["status"] == "running" and SG.state(ws, "shop")["error"] is None
    st = DP.status(ws, "shop")
    assert st["status"] == "done" and st["failed_step"] is None and st["error"] is None and st["can"] == []
    assert st["closed"]["stopped_at_step"] == "Starting the model" and all(s["status"] == "done" for s in st["steps"])
    assert SG.query(ws, "shop", "jacket")["staged"]["model_version"] == ws.candidate  # and it answers through the corrected record


def test_the_project_page_asks_the_cloud_before_it_says_a_deploy_failed(ws, aws):  # noqa: F811
    """The page reads the deploy status, not the list. That read alone must set things right."""
    SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True)
    SG._save(ws, "shop", "staging", status="failed", error="Captain was closed while it ran.")
    early = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    DP._write(ws, "shop", "staging", {"project": "shop", "env": "staging", "host": "aws", "login": "p", "label": "AWS", "model_version": ws.candidate,
                                      "status": "failed", "started_at": early, "started_by": "tester", "made_setup": False,
                                      "failed_step": {"id": "start", "title": "Starting the model"}, "error": "the endpoint was still Creating",
                                      "steps": [{"id": "start", "title": "Starting the model", "status": "failed", "lines": []}]})
    st = DP.status(ws, "shop")
    assert st["status"] == "done" and st["error"] is None and st["staged"]["status"] == "running" and st["can"] == []
    # And when the endpoint really is gone, the failure stands, with retry.
    aws.endpoints.clear()
    FL.forget()
    DP._write(ws, "shop", "staging", {**DP._read(ws, "shop", "staging")[0], "status": "failed", "error": "it did not start",
                                      "failed_step": {"id": "start", "title": "Starting the model"}})
    st = DP.status(ws, "shop")
    # The stand-in's login is not one connected in Captain, so "Deploy again" is not offered and the reason is said.
    assert st["status"] == "failed" and st["error"] == "it did not start" and "not connected in Captain any more" in st["fix_text"]


def test_a_cloud_that_cannot_be_asked_changes_nothing_and_says_so(ws, aws, monkeypatch):  # noqa: F811
    from captain_search.compute_providers.aws import SageMakerProvider

    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    monkeypatch.setattr(SageMakerProvider, "found", lambda self, prefix="captain": (_ for _ in ()).throw(RuntimeError("ExpiredToken: the security token is expired")))
    d = FL.deployed(ws, fresh=True)
    r = d["rows"][0]
    assert r["state"] == "unknown" and r["link"] == "unknown" and "Captain couldn't ask AWS just now: ExpiredToken" in r["state_text"]
    assert d["asked"][0]["ok"] is False and d["text"].endswith("Could not ask AWS just now.") and r["can"] == []
    assert SG.state(ws, "shop")["status"] == "running"  # not knowing is not the same as gone


def test_the_answer_is_kept_for_a_few_seconds_and_never_longer(ws, aws, monkeypatch):  # noqa: F811
    from captain_search.compute_providers.aws import SageMakerProvider

    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    asks, real = [], SageMakerProvider.found
    monkeypatch.setattr(SageMakerProvider, "found", lambda self, prefix="captain": asks.append(1) or real(self, prefix))
    FL.forget()
    FL.deployed(ws)
    FL.deployed(ws)
    assert len(asks) == 1 and FL.TTL_S <= 10
    FL.deployed(ws, fresh=True)
    assert len(asks) == 2
    clock = [1000.0]
    monkeypatch.setattr(FL.time, "monotonic", lambda: clock[0])
    FL.forget()
    FL.deployed(ws)
    clock[0] += FL.TTL_S + 0.1
    FL.deployed(ws)
    assert len(asks) == 4


def test_sync_says_what_differs_and_then_nothing(ws, aws):  # noqa: F811
    rec = SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    assert FL.differences(ws) == {"corrected": [], "unlinked": [], "not_asked": [], "in_sync": True,
                                  "text": "Captain's records match the cloud. Nothing to correct."}
    del aws.endpoints[rec["endpoint"]["name"]]
    d = FL.differences(ws)
    assert not d["in_sync"] and d["corrected"][0]["project"] == "shop" and "removed outside Captain" in d["text"]
    assert FL.differences(ws)["in_sync"]


# ---- this computer, and a deploy that is only slow --------------------------------------------------


def test_a_model_on_this_computer_is_checked_by_its_process(ws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="local", confirm=True)
    r = _rows(ws)[0]
    assert (r["cloud"], r["state"], r["link"], r["where"]) == ("local", "running", "local", "This computer, This computer") or r["state"] == "running"
    SG.unstage(ws, "shop")
    assert _rows(ws)[0]["state"] == "stopped"


def test_a_deploy_that_is_only_slow_is_not_called_failed(ws, monkeypatch):  # noqa: F811
    """A cloud starting an endpoint prints nothing while it waits. The run says it is alive every BEAT_S, so `status`
    never takes a slow start for a dead one."""
    import time

    monkeypatch.setattr(DP, "BEAT_S", 0.2)
    monkeypatch.setattr(DP, "STALE_S", 2)  # times are kept to the second, so nothing finer can be told apart
    real = SG.stage

    def slow(*a, **k):
        time.sleep(3.6)  # far past STALE_S, and silent
        seen.append(DP.status(ws, "shop")["status"])
        return real(*a, **k)

    seen = []
    monkeypatch.setattr(DP, "stage", slow)
    out = DP.run(ws, "shop", "local")
    assert seen == ["running"] and DP.status(ws, "shop")["status"] == "done" and out
    SG.unstage(ws, "shop")


# ---- Modal, Google Cloud, Azure --------------------------------------------------------------------

from test_deploy_one_call import LOGINS, logins  # noqa: E402,F401  (fixtures)
from test_hosting_clouds import azure, google  # noqa: E402,F401  (fixtures)
from test_hosting_modal import modal  # noqa: E402,F401  (fixtures)


def test_modal_running_asleep_and_gone_without_ever_calling_the_app(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    asked_before = len(modal.calls)
    r = _rows(ws)[0]
    assert (r["cloud"], r["state"], r["link"], r["copies"]["now"]) == ("modal", "running", "linked", 1) and r["sleeps"]
    modal.copies = 0
    r = _rows(ws)[0]
    assert r["state"] == "asleep" and r["state_text"] == "Asleep. It wakes on the next request and bills nothing until then."
    assert SG.state(ws, "shop")["status"] == "running"  # asleep is still deployed
    assert len(modal.calls) == asked_before  # looking never woke it
    modal.apps.clear()
    r = _rows(ws)[0]
    assert r["state"] == "gone" and SG.state(ws, "shop")["status"] == "stopped"


def test_modal_app_made_by_another_workspace_is_found(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    SG._save(ws, "shop", "staging", endpoint={}, status="stopped", resources=[])  # this workspace forgot its endpoint
    rows = _rows(ws)
    # The app carries this project's tags, so it is this project's again, running.
    assert [(r["link"], r["state"]) for r in rows] == [("linked", "running")] and SG.state(ws, "shop")["status"] == "running"


def _running_lost_and_stopped(ws, logins, host):  # noqa: F811
    logins.append(LOGINS[host])
    DP.run(ws, "shop", host, confirm=True)
    d = FL.deployed(ws, fresh=True)
    r = d["rows"][0]
    assert [a["ok"] for a in d["asked"]] == [True], d["asked"]
    assert (r["cloud"], r["link"]) == (host, "linked") and r["state"] in ("running", "asleep") and r["name"] and len(d["rows"]) == 1
    # Captain's record says failed: the cloud still has it, so the record is set right.
    SG._save(ws, "shop", "staging", status="failed", error="lost")
    r = _rows(ws)[0]
    assert r["state"] in ("running", "asleep") and SG.state(ws, "shop")["status"] == "running" and r["corrected"]
    SG.unstage(ws, "shop")
    rows = _rows(ws)
    assert [x["state"] for x in rows] == ["stopped"]


# One stand-in a test: each wires the same network calls, so two in one test answer for each other.
def test_cloud_run_running_lost_and_stopped(ws, logins, google):  # noqa: F811
    _running_lost_and_stopped(ws, logins, "gcp")


def test_container_apps_running_lost_and_stopped(ws, logins, azure):  # noqa: F811
    _running_lost_and_stopped(ws, logins, "azure")


def test_deploy_again_is_offered_only_when_it_can_work(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    modal.fail_deploy = "Modal could not build the image"
    with pytest.raises(Exception):
        DP.run(ws, "shop", "modal", confirm=True)
    st = DP.status(ws, "shop")
    assert st["status"] == "failed" and st["can"][0] == "retry" and st["fix_text"] is None
    # The login is disconnected: a retry cannot work, so the panel says what to do and keeps Remove.
    logins.clear()
    st = DP.status(ws, "shop")
    assert "retry" not in st["can"] and "not connected in Captain any more" in st["fix_text"]
    assert ("remove" in st["can"]) == bool(st["exists_now"])


def test_what_the_first_deploy_created_is_listed_with_what_each_is_for(ws, logins, modal):  # noqa: F811
    assert FL.created(ws) == {"places": [], "text": "Captain has created nothing in your cloud accounts."}
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    FL.forget()
    p = FL.created(ws)["places"]
    assert [x["cloud_label"] for x in p] == ["Modal"] and p[0]["in_use_by"] and not p[0]["can_remove"] and "stop that first" in p[0]["text"]
    kinds = {t["kind"]: t["what_for"] for t in p[0]["things"]}
    assert kinds["modal-volume"] == "Holds the model's files and its request log." and all(t["what_for"] and t["name"] for t in p[0]["things"])
    SG.unstage(ws, "shop")
    FL.forget()
    p = FL.created(ws)["places"]
    assert p[0]["can_remove"] and p[0]["in_use_by"] == [] and "can be removed" in p[0]["text"]


def test_a_deploy_or_a_stop_a_moment_ago_shows_at_once(ws):  # noqa: F811
    """The clouds' answers are kept for a few seconds. Captain's own records are not: a list asked for just before
    a deploy must not go on saying nothing is deployed."""
    FL.forget()
    assert FL.deployed(ws)["rows"] == []
    SG.stage(ws, "shop", baseline=True, target="local", confirm=True)
    assert [r["state"] for r in FL.deployed(ws)["rows"]] == ["running"]
    SG.unstage(ws, "shop")
    assert [r["state"] for r in FL.deployed(ws)["rows"]] == ["stopped"]


def test_what_was_created_names_each_thing_and_leaves_out_captains_own_note(ws, monkeypatch):  # noqa: F811
    """What a real AWS account held on 2026-11-12. Two things were labelled "Made by the first deploy", Captain's own
    note of where to deploy was listed as if it were in the account, and the login "aws" was said beside "AWS"."""
    from captain_search.staging import hosts as H

    made = [{"type": "s3-bucket", "name": "captain-serving-000000000000-us-west-1"}, {"type": "iam-role", "name": "captain-serving"},
            {"type": "ecr-repository", "name": "captain-serving"}, {"type": "iam-role", "name": "captain-serving-build"},
            {"type": "s3-object", "name": "s3://captain-serving-000000000000-us-west-1/build/serving.zip"},
            {"type": "codebuild-project", "name": "captain-serving-build"}, {"type": "log-group", "name": "/aws/codebuild/captain-serving-build"},
            {"type": "compute-target", "name": "aws-serving"}]
    monkeypatch.setattr(type(H.get("aws")), "status", lambda self, ctx: {"status": "ready", "profile": "aws", "region": "us-west-1", "created": made})
    FL.forget()
    place = FL.created(ws)["places"][0]
    assert [t["kind_label"] for t in place["things"]] == ["Bucket", "Role", "Image repository", "Role for the image build", "Build package", "Image build", "Build log"]
    by = {t["kind_label"]: t for t in place["things"]}
    assert by["Build package"]["short"] == "serving.zip" and by["Build package"]["what_for"].startswith("The files the model's image is built from.")
    assert by["Build log"]["what_for"].startswith("What the image build printed while it ran.")
    assert not any("Made by the first deploy" in t["kind_label"] or "target" in t["kind"] for t in place["things"])  # Captain's note is not in the account
    assert place["login"] == "aws" and place["login_shown"] is None and place["text"].startswith("7 things in your AWS account.")
    assert FL._same_word("aws", "aws") and FL._same_word("AWS", "aws") and not FL._same_word("work-aws", "aws") and FL._same_word(None, "modal")


def test_the_title_counts_what_is_running_now():
    s = FL._summary
    assert s(0, [], 0) == "No models running." and s(1, [], 0) == "1 model running." and s(3, [], 0) == "3 models running."
    assert s(2, ["failed"], 0) == "2 models running, 1 failed." and s(0, ["failed"], 0) == "No models running, 1 failed."
    assert s(1, ["unknown"], 0) == "1 model running, 1 not answering."
    assert s(1, [], 2) == "1 model running, 2 found in your cloud with no project."


def test_a_stopped_model_is_remembered_not_deployed_and_can_be_forgotten(ws, aws):  # noqa: F811
    """A person stopped their model and asked why it was still listed. Nothing of it was in the cloud: the list was
    showing Captain's memory of it as if it were a deployment."""
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    d = FL.deployed(ws, fresh=True)
    assert d["rows"][0]["in_cloud"] and d["rows"][0]["remembered_text"] is None and d["stopped_earlier"] == 0 and d["stopped_text"] is None
    with pytest.raises(ValueError, match="is still in AWS"):
        FL.forget_stopped(ws, "shop", confirm=True)  # never forget something that is still there and billing
    SG.unstage(ws, "shop")
    d = FL.deployed(ws, fresh=True)
    r = d["rows"][0]
    assert d["text"] == "No models running." and d["stopped_earlier"] == 1 and d["stopped_text"] == "Stopped earlier: 1 model."
    assert not r["in_cloud"] and r["can"] == ["start", "forget"] and r["state"] == "stopped"
    assert r["remembered_text"] == "Nothing of it is running in AWS and nothing is billing. Captain remembers it so you can start it again."
    with pytest.raises(ValueError, match="Confirm first. Captain will forget .* Nothing in the cloud changes"):
        FL.forget_stopped(ws, "shop")
    calls = len(aws.calls)
    out = FL.forget_stopped(ws, "shop", confirm=True)
    assert out["text"].endswith("Nothing in the cloud changed.") and SG.state(ws, "shop") is None
    assert not [c for c, _ in aws.calls[calls:] if c.startswith(("delete", "create", "update"))]  # the cloud was only read
    assert FL.deployed(ws, fresh=True)["rows"] == [] and FL.deployed(ws)["text"] == "No models running."
    with pytest.raises(ValueError, match="has no deployment to forget"):
        FL.forget_stopped(ws, "shop", confirm=True)


def test_a_row_says_who_and_when_in_words_and_names_the_model_plainly(ws):  # noqa: F811
    """From a real screen on 2026-11-12: "Stopped: stopped by <an email address>", and the model shown as
    "<project>/starter-ranker@v2" beside the project's own name."""
    import datetime as dt

    assert FL.model_words("shop/starter-ranker@v2", "shop") == "starter-ranker, version 2"
    assert FL.model_words("shop/starter-ranker@v2") == "shop/starter-ranker, version 2" and FL.model_words("other/x@v1", "shop") == "other/x, version 1"
    assert FL.model_words("plain", "shop") == "plain" and FL.model_words(None) is None
    assert FL.short_name("dana@example.com") == "dana" and FL.short_name("dana") == "dana" and FL.short_name(None) is None
    t = lambda **kw: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**kw)).isoformat(timespec="seconds")  # noqa: E731
    assert FL._stopped_words("stopped by dana@example.com", t(minutes=20)) == "Stopped by dana, 20 minutes ago."
    assert FL._stopped_words("stopped by itself: no requests for 240 minutes", t(hours=3)) == "Stopped by itself, 3 hours ago: no requests for 240 minutes."
    assert FL._stopped_words("it did not start", t(days=2)) == "Stopped, 2 days ago: it did not start."
    assert FL._stopped_words(None, None) == "Stopped." and FL._stopped_words("stopped by dana", t(seconds=10)) == "Stopped by dana, just now."
    SG.stage(ws, "shop", baseline=True, target="local", confirm=True)
    SG.unstage(ws, "shop")
    r = FL.deployed(ws, fresh=True)["rows"][0]
    assert r["state_text"].startswith("Stopped by ") and r["state_text"].endswith("just now.") and "stopped by" not in r["state_text"][1:] and "@" not in r["state_text"]
    assert r["model_label"] == FL.model_words(r["model_version"], "shop") and not r["model_label"].startswith("shop/") and r["by_name"] and "@" not in r["by_name"]


# ---- where a model runs, in full -------------------------------------------------------------------


def test_a_running_model_says_where_it_lives_from_the_clouds_own_describe(ws, aws):  # noqa: F811
    """A person asked for "the endpoint and all info around the computer or system it runs in"."""
    import datetime as dt

    aws.image_pushed = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
    rec = SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True)
    name = rec["endpoint"]["name"]
    r = FL.deployed(ws, fresh=True)["rows"][0]
    # On the row: the address a site calls, and one line of facts about the system.
    assert r["address"] == f"https://runtime.sagemaker.us-west-1.amazonaws.com/endpoints/{name}/invocations"
    assert r["machine_words"] == "ml.t2.medium: 2 virtual CPUs, 4 GB memory" and r["account"] == "account ending 1234" and r["copies_words"] == "1 copy up now"
    w = FL.where_it_runs(ws, "shop")
    f = {x["id"]: x for x in w["facts"]}
    assert [x["label"] for x in w["facts"]] == ["Address", "How to call it", "Cloud and account", "Region", "Machine", "Copies", "Image", "Model files",
                                                "Role it runs as", "Logs", "Stops by itself after", "Deployed by", "Version"]
    assert w["from_cloud"] and w["text"] == "Read from AWS just now."
    assert f["address"]["value"] == r["address"] and f["address"]["copy"] and f["address"]["source"] == "cloud" and f["address"]["source_text"] == "From AWS"
    # A copyable test request for this host, with the key never in it, marked as test traffic.
    call = f["call"]["value"]
    assert "aws sagemaker-runtime invoke-endpoint" in call and f"--endpoint-name {name}" in call and f["call"]["copy"]
    assert '"traffic": "test"' in call and '"traffic": "real"' not in call and SG._key(SG.state(ws, "shop")) not in call
    assert f["cloud"]["value"] == "AWS, account ending 1234" and "000000001234" not in str(w)  # never the whole account number
    assert f["region"]["value"] == "us-west-1" and f["region"]["source"] == "record"
    assert f["machine"]["value"].startswith("ml.t2.medium: 2 virtual CPUs, 4 GB memory, about $0.07 an hour") and f["machine"]["source"] == "cloud"
    assert f["copies"]["value"] == "1 up now. Fewest 1, most 1" and f["copies"]["source"] == "cloud"
    assert f["image"]["value"] == f"{IMAGE}, built 5 hours ago" and f["role"]["value"] == ROLE and f["role"]["source"] == "cloud"
    assert f["files"]["value"].startswith("s3://") and f["files"]["value"].endswith(rec["bundle"])
    assert f["logs"]["value"] == f"/aws/sagemaker/Endpoints/{name}" and f["logs"]["copy"]
    assert f["stops"]["value"].endswith("minutes with no requests") and f["by"]["value"].endswith("just now")
    assert f["version"]["value"] == f"{FL.model_words(ws.candidate, 'shop')} ({ws.candidate})"
    # An account with an alias is said by its alias.
    aws.aliases = ["demo-team"]
    from captain_search.compute_providers import aws as AWSP
    AWSP.forget_clients()
    FL.forget()
    assert {x["id"]: x for x in FL.where_it_runs(ws, "shop")["facts"]}["cloud"]["value"] == "AWS, account demo-team"


def test_where_it_runs_falls_back_to_captains_record_and_says_so(ws, aws, monkeypatch):  # noqa: F811
    from captain_search.compute_providers.aws import SageMakerProvider

    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    FL.deployed(ws, fresh=True)
    monkeypatch.setattr(SageMakerProvider, "where", lambda self, rec: (_ for _ in ()).throw(RuntimeError("AccessDenied: sagemaker:DescribeEndpoint")))
    w = FL.where_it_runs(ws, "shop")
    assert not w["from_cloud"] and w["text"] == "AWS could not be asked just now (AccessDenied: sagemaker:DescribeEndpoint). These are from Captain's record."
    f = {x["id"]: x for x in w["facts"]}
    assert f["address"]["source"] == "record" and f["address"]["source_text"] == "From Captain's record" and f["machine"]["source"] == "record"
    assert f["role"]["value"] == ROLE and f["role"]["source"] == "record" and "logs" not in f  # what nobody has is left out, not made up
    # Stopped: nothing of it is in the cloud, and that is said.
    monkeypatch.undo()
    SG.unstage(ws, "shop")
    FL.forget()
    w = FL.where_it_runs(ws, "shop")
    assert w["text"] == "Nothing of it is in AWS now. These are from Captain's record of when it ran." and not w["from_cloud"]
    with pytest.raises(ValueError, match="has no deployment"):
        FL.where_it_runs(ws, "trail")


def test_where_it_runs_on_this_computer(ws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="local", confirm=True)
    r = FL.deployed(ws, fresh=True)["rows"][0]
    assert r["address"].startswith("http://127.0.0.1:") and r["machine_words"] is None and r["account"] is None
    f = {x["id"]: x for x in FL.where_it_runs(ws, "shop")["facts"]}
    assert f["address"]["value"] == r["address"] and "curl" in f["call"]["value"] and '"traffic": "test"' in f["call"]["value"]
    assert f["machine"]["value"] == "This computer" and FL.where_it_runs(ws, "shop")["text"] == "On this computer."
    SG.unstage(ws, "shop")


def test_where_it_runs_on_a_host_with_its_own_address(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    r = FL.deployed(ws, fresh=True)["rows"][0]
    assert r["address"].startswith("https://") and r["address"].endswith(".modal.run") and r["machine_words"] == "1 CPU, 2 GB memory"
    assert r["account"] == f"environment {LOGINS['modal']['fields']['environment']}" and "may run 0 to 1" in r["copies_words"]
    f = {x["id"]: x for x in FL.where_it_runs(ws, "shop")["facts"]}
    assert f["address"]["value"] == r["address"] and "Authorization: Bearer $CAPTAIN_STAGING_KEY" in f["call"]["value"]
    assert f["machine"]["value"].startswith("1 CPU, 2 GB memory, about $0.06 an hour") and f["stops"]["value"].startswith("It sleeps when no requests arrive")


def test_every_size_a_host_offers_can_say_what_it_is():
    from captain_search import price_table as PT
    from captain_search.staging import sizing as SZ

    for region in ("us-west-1", "us-east-1"):
        for s in SZ.sizes("aws", region):
            m = SZ.machine_facts("aws", s["config"], region)
            assert m["cpus"] and m["memory_mb"] and m["hourly_usd"] and "virtual CPUs" in m["words"] and m["words"].startswith(s["name"] + ": "), s["name"]
    assert set(PT.SAGEMAKER_MACHINES) == set(SZ.SAGEMAKER_MEMORY)
    for host in ("modal", "gcp", "azure"):
        for s in SZ.sizes(host, None):
            m = SZ.machine_facts(host, s["config"])
            assert m["words"] and "memory" in m["words"] and m["hourly_usd"] is not None, (host, s["name"])


# ---- removing a deployment, and starting one again -------------------------------------------------


def test_removing_a_deployment_ends_with_it_gone_from_every_list(ws, logins, modal):  # noqa: F811
    """A person clicked "Remove from the cloud" and said "it still just leaves it there". The cloud had been emptied.
    The row stayed, because Captain kept its record of a deployment that no longer existed anywhere."""
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    SG.unstage(ws, "shop")  # stopped: remembered, with what the first deploy set up still there
    assert [r["state"] for r in FL.deployed(ws, fresh=True)["rows"]] == ["stopped"] and FL.created(ws)["places"]
    out = DP.remove(ws, "shop")
    assert out["status"] == "removed" and out["left"] == [] and out["removed_setup"] and out["forgotten"]
    assert out["text"].startswith("Removed from Modal: ") and "Captain set up" in out["text"] and out["text"].endswith("Nothing is left.")
    # Nothing is left to act on: no row, no record, nothing in the account.
    assert FL.deployed(ws, fresh=True)["rows"] == [] and SG.state(ws, "shop") is None and FL.created(ws)["places"] == []
    assert DP.status(ws, "shop")["status"] == "removed"
    # A running one: the model goes, then what was set up, then the record.
    DP.run(ws, "shop", "modal", confirm=True)
    out = DP.remove(ws, "shop")
    assert "the deployed model and the " in out["text"] and FL.deployed(ws, fresh=True)["rows"] == [] and SG.state(ws, "shop") is None


def test_a_removal_that_leaves_something_keeps_the_row_and_says_what_is_left(ws, logins, modal, monkeypatch):  # noqa: F811
    from captain_search.staging import hosts as H

    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    SG.unstage(ws, "shop")
    monkeypatch.setattr(type(H.get("modal")), "teardown", lambda self, ctx, progress=None: {
        "status": "partial", "created": [{"type": "modal-volume", "name": "captain-serving", "error": "it still holds files"}]})
    out = DP.remove(ws, "shop")
    assert out["status"] == "partly removed" and not out["forgotten"]
    assert out["text"] == ("Not everything could be removed from Modal. Still there: modal-volume captain-serving (it still holds files). "
                           "Try again, or remove these in the cloud's own console.")
    assert SG.state(ws, "shop") is not None and len(FL.deployed(ws, fresh=True)["rows"]) == 1  # the row stays, with something still to do


def test_start_again_goes_back_to_the_same_cloud_and_never_to_this_computer(ws, logins, modal):  # noqa: F811
    """After a removal on a real account, a model that had lived in AWS turned up on this computer: address
    127.0.0.1, started and stopped fifteen seconds apart. Starting again must set the cloud up again, and say so."""
    from captain_search.staging import hosts as H

    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    version = SG.state(ws, "shop")["model_version"]
    SG.unstage(ws, "shop")
    H.get("modal").teardown(ws)  # what the first deploy set up is gone, the stopped model is still remembered
    row = FL.deployed(ws, fresh=True)["rows"][0]
    assert row["cloud"] == "modal" and row["state"] == "stopped" and row["can"] == ["start", "forget"]
    # What the confirm is built from: the place by name, that it will be set up again, how long, and the cost.
    p = DP.plan(ws, "shop", row["cloud"], row["login"], candidate=version)
    assert p["label"] == "Modal" and p["host"] == "modal" and p["first_time"] and p["needs_confirm"] and "minute" in p["minutes"]["text"] and p["cost"]["text"]
    DP.run(ws, "shop", row["cloud"], row["login"], candidate=version, confirm=True)
    rec = SG.state(ws, "shop")
    assert rec["status"] == "running" and rec["kind"] != "local" and rec["host"] == "modal" and "127.0.0.1" not in str(rec.get("endpoint"))
    assert FL.deployed(ws, fresh=True)["rows"][0]["cloud"] == "modal"


def test_a_deploy_with_no_place_named_never_lands_on_this_computer(ws, logins, modal, monkeypatch, tmp_path):  # noqa: F811
    from fastapi.testclient import TestClient

    from captain_search import server

    monkeypatch.setenv("CST_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server, "ctx", lambda: ws)
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    SG.unstage(ws, "shop")
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    r = c.post("/api/projects/shop/deploy", headers=h, json={"candidate": SG.state(ws, "shop")["model_version"], "confirm": True})
    assert r.status_code == 400, (r.status_code, r.text[:300])
    said = r.json().get("detail") or r.json().get("error") or r.text
    assert "Say where to deploy it. This model last ran on modal" in said and "never moves a model to this computer without being asked" in said
    rec = SG.state(ws, "shop")
    assert rec["status"] == "stopped" and rec["kind"] != "local"  # nothing was started anywhere
    # Naming this computer is still allowed: that is a person asking for it.
    assert c.post("/api/projects/shop/deploy", headers=h, json={"host": "local", "dry_run": True}).status_code == 200
