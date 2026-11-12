"""One table of measured step times (staging/timings.py) feeds the confirm, the plan's steps and the lines a run
prints, so they cannot disagree. On 2026-11-12 the confirm said about four minutes while the run's own lines said
"about 3 minutes" for the image and "4 to 10 minutes" for the endpoint, in a deploy that took 3 min 50 s."""
import pathlib
import re

import pytest

from captain_search import staging as SG
from captain_search.staging import deploy as DP
from captain_search.staging import timings as TM

from test_deploy_one_call import LOGINS, logins  # noqa: F401  (fixtures)
from test_hosting_modal import modal  # noqa: F401  (fixtures)
from test_serving_setup import Fake as FakeAwsSetup
from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import aws  # noqa: F401  (fixture)

SRC = pathlib.Path(DP.__file__).resolve().parents[1]


@pytest.mark.parametrize("host", sorted(TM.STEPS))
@pytest.mark.parametrize("first", [True, False])
def test_the_steps_add_up_to_the_confirms_total(host, first):
    m = DP._minutes(host, first)
    steps = DP._typical(DP._steps(host), host, first)
    assert sum(s["typical_s"] or 0 for s in steps) == m["seconds"] == TM.total(host, first)
    # Every step in the table is a step of the plan: a mistyped id would be counted in the total and shown nowhere.
    assert set(TM.STEPS[host]["first" if first else "again"]) <= {s["id"] for s in steps}
    assert m["measured"] == TM.measured_on(host) and f"Measured on {TM.measured_on(host)}." in m["text"]
    assert m["low"] <= max(1, m["seconds"] / 60) <= m["high"]


def test_the_aws_confirm_says_what_the_run_took():
    first = DP._minutes("aws", True)
    assert first["seconds"] == 230 and first["text"] == ("About 4 minutes: about a minute and a half to build the image and about 2 minutes "
                                                         "to start the model. Measured on 2026-11-12.")
    assert DP._minutes("modal", False)["text"] == "Under a minute. Measured on 2026-11-11."
    assert "cautious guess" in DP._minutes("gcp", True)["text"] and DP._minutes("gcp", True)["seconds"] is None


def test_the_plan_carries_each_steps_typical_time(ws, logins, modal):  # noqa: F811
    logins.append(LOGINS["modal"])
    p = DP.plan(ws, "shop", "modal")
    by = {s["id"]: s for s in p["steps"]}
    assert by["start"]["typical"] == "about a minute and a half" and by["start"]["typical_s"] == 96 and by["image"]["typical"] is None
    assert sum(s["typical_s"] or 0 for s in p["steps"]) == p["minutes"]["seconds"]


def test_words_for_a_measured_time():
    assert [TM.words(s) for s in (1, 49, 50, 74, 92, 104, 105, 134, 150, 230)] == [
        "under a minute", "under a minute", "about a minute", "about a minute", "about a minute and a half", "about a minute and a half",
        "about 2 minutes", "about 2 minutes", "about 3 minutes", "about 4 minutes"]
    assert TM.words(None) is None and TM.aside("gcp", "image") == " (not timed on a real account yet)"


def test_the_endpoint_line_a_run_prints_reads_the_table(ws, aws):  # noqa: F811
    said = []
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True, progress=said.append)
    assert "Starting the endpoint (about 2 minutes)" in [str(x) for x in said]
    SG.stage(ws, "shop", candidate=ws.exp, target="sm", confirm=True, progress=said.append)
    move = next(str(x) for x in said if str(x).startswith("Moving the endpoint"))
    assert "a new endpoint takes about 2 minutes" in move and "has not been timed yet" in move
    SG.unstage(ws, "shop")


def test_the_image_line_and_the_setup_plan_read_the_table(ws, monkeypatch):  # noqa: F811
    from captain_search import cloud
    from captain_search.staging import serving_setup as SS

    f = FakeAwsSetup()
    monkeypatch.setattr(cloud, "aws_session", lambda profile, region=None: f)
    monkeypatch.setattr(SS, "docker_answers", lambda timeout=5.0: False)
    ws.workspace.storage_uri = "s3://team-bucket/captain/shop"
    p = SS.plan(ws, "main", "us-west-1")
    w = TM.step_words("aws", "image")
    assert w == "about a minute and a half" and f"Building the image takes {w} in AWS CodeBuild" in p["cost"]["text"]
    assert any(r["why"].endswith(f"It runs once, for {w}.") for r in p["creates"])


def test_no_line_states_a_time_of_its_own():
    """A duration typed into a message is how the lines came to disagree. They come from the table."""
    typed = re.compile(r"\(about \w+ minutes?\)|this takes \d|takes \d+ to \d+ minutes|about two minutes|about \{BUILD_MINUTES\} minutes")
    files = [SRC / "staging" / "serving_setup.py", SRC / "staging" / "deploy.py", *sorted((SRC / "staging" / "hosts").glob("*.py")),
             *sorted((SRC / "compute_providers").glob("*serving.py")), SRC / "compute_providers" / "aws.py"]
    found = [f"{f.name}:{i}: {ln.strip()[:100]}" for f in files for i, ln in enumerate(f.read_text().splitlines(), 1)
             if typed.search(ln) and not ln.lstrip().startswith("#")]
    assert not found, found
