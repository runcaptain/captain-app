"""The same truth from the terminal: `cst deployments list | show | sync | start | remove | link`, each with --json
for agents. Against the stand-ins."""
import json

import pytest
from typer.testing import CliRunner

from captain_search import staging as SG
from captain_search.cli.main import app as cli
from captain_search.staging import deploy as DP
from captain_search.staging import fleet as FL
from captain_search.staging import numbers as NB

from test_deploy_one_call import LOGINS, logins  # noqa: F401  (fixtures)
from test_fleet import _loose
from test_hosting_modal import modal  # noqa: F401  (fixtures)
from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import aws  # noqa: F401  (fixture)

run = CliRunner()


@pytest.fixture(autouse=True)
def nothing_kept():
    FL.forget()
    NB.forget()
    yield
    FL.forget()
    NB.forget()


def cst(*args):
    r = run.invoke(cli, ["-w", "t", *args])
    return r.exit_code, r.output


def test_list_show_and_sync_read_the_cloud(ws, aws):  # noqa: F811
    code, out = cst("deployments", "list")
    assert code == 0 and "Nothing is deployed." in out
    rec = SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True)
    FL.forget()
    code, out = cst("deployments", "list")
    assert code == 0 and "Running" in out and "AWS, us-west-1, ml.t2.medium" in out and ws.candidate in out
    code, out = cst("--json", "deployments", "list")
    rows = json.loads(out)["rows"]
    assert code == 0 and rows[0]["state"] == "running" and rows[0]["name"] == rec["endpoint"]["name"] and rows[0]["link"] == "linked"
    code, out = cst("deployments", "show", "shop")
    assert code == 0 and "Running on 1 copy." in out and "Where     AWS, us-west-1, ml.t2.medium" in out
    assert "Traffic   7,200 requests in the last hour, no errors." in out and "120 requests a minute on average" in out
    assert "Cost      About $" in out and "Source    From AWS's own metrics" in out and "CPU 20% on average" in out
    code, out = cst("--json", "deployments", "show", "shop", "--range", "24h")
    d = json.loads(out)
    assert d["deployment"]["cloud"] == "aws" and d["numbers"]["range"] == "24h" and d["numbers"]["cost"]["month_usd"] > 0
    code, out = cst("deployments", "sync")
    assert code == 0 and "Captain's records match the cloud. Nothing to correct." in out
    # Deleted in the cloud's console: sync says so, and the list moves it to the stopped ones.
    del aws.endpoints[rec["endpoint"]["name"]]
    code, out = cst("deployments", "sync")
    assert code == 0 and "shop:" in out and "removed outside Captain" in out
    FL.forget()
    assert "Nothing is deployed." not in cst("deployments", "list", "--stopped")[1] and "Removed outside Captain" in cst("deployments", "list", "--stopped")[1]
    assert cst("deployments", "show", "nothing-here")[0] != 0


def test_a_stopped_deployment_is_a_row_in_the_list_and_in_its_json(ws):  # noqa: F811
    """On a real account on 2026-11-12 the list printed only "0 deployed, 1 not running." and its JSON had no rows:
    it counted the stopped model and did not show it."""
    SG.stage(ws, "shop", baseline=True, target="local", confirm=True)
    version = SG.state(ws, "shop")["model_version"]
    SG.unstage(ws, "shop")
    FL.forget()
    code, out = cst("deployments", "list")
    assert code == 0 and "Stopped" in out and version in out and "0 deployed, 1 not running." in out
    rows = json.loads(cst("--json", "deployments", "list")[1])["rows"]
    assert [(r["project"], r["state"]) for r in rows] == [("shop", "stopped")]
    assert json.loads(cst("--json", "deployments", "list", "--running")[1])["rows"] == []
    assert len(json.loads(cst("--json", "deployments", "list", "--stopped")[1])["rows"]) == 1


def test_something_found_in_the_cloud_is_listed_linked_or_removed(ws, aws):  # noqa: F811
    rid = _loose(aws)
    code, out = cst("deployments", "list")
    assert code == 0 and "other-team-ranker-st" in out and "Found in your AWS account, not linked to a project." in out
    code, out = cst("deployments", "remove", rid)
    assert code != 0 and "Confirm first" in out and aws.endpoints
    code, out = cst("deployments", "link", rid, "shop")
    assert code == 0 and "is now this project's deployment" in out
    rid2 = _loose(aws, "captain-old-experiment-staging")
    code, out = cst("deployments", "remove", rid2, "--yes")
    assert code == 0 and "Removed the SageMaker endpoint captain-old-experiment-staging" in out
    assert sorted(aws.endpoints) == ["other-team-ranker-staging"]


def test_start_again_and_remove(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    DP.run(ws, "shop", "modal", confirm=True)
    version = SG.state(ws, "shop")["model_version"]
    FL.forget()
    code, out = cst("deployments", "start", "shop")
    assert code != 0 and "already deployed" in out
    SG.unstage(ws, "shop")
    FL.forget()
    code, out = cst("deployments", "start", "shop", "--yes")
    assert code == 0 and f"Started {version} again on Modal" in out and SG.state(ws, "shop")["status"] == "running"
    code, out = cst("deployments", "remove", "shop")
    stops = len(modal.stops)
    assert code != 0 and "Confirm with --yes" in out and len(modal.stops) == stops
    code, out = cst("deployments", "remove", "shop", "--yes")
    # Modal keeps a stopped app's name in its list: what matters is that it was stopped, and that the cloud read agrees.
    assert code == 0 and len(modal.stops) == stops + 1 and SG.state(ws, "shop")["status"] != "running"
    FL.forget()
    assert [r["state"] for r in FL.deployed(ws, fresh=True)["rows"]] == ["stopped"]
