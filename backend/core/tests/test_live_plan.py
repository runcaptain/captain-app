"""A deploy's run records its steps and when each began, so the screen can count time while a step prints nothing
(the owner, deploying to AWS: "freezes on deploying a model, doesn't show live time")."""
import time

from captain_search import live as LV


def test_a_run_records_its_plan_and_when_each_step_began(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    assert "deploy" in LV.KINDS
    r = LV.start("deploy", "Deploying starter-ranker, version 2 to AWS", of="shop", source="deploy")
    r.plan([{"title": "Checking your account", "seconds": 1, "usually": "under a minute"},
            {"title": "Building the model's image", "seconds": 92, "usually": "about a minute and a half"},
            {"title": "Starting the model", "seconds": None, "usually": None}])
    h = LV.header(r.id)
    assert h["kind"] == "deploy" and h["title"] == "Deploying starter-ranker, version 2 to AWS" and h["started"]
    assert [s["title"] for s in h["steps"]] == h["stages"] == ["Checking your account", "Building the model's image", "Starting the model"]
    assert h["steps"][1] == {"title": "Building the model's image", "seconds": 92, "usually": "about a minute and a half"}
    r.stage("Checking your account", 1, 3)
    first = LV.header(r.id)["stage_at"]
    time.sleep(0.05)
    r.stage("Building the model's image", 2, 3)
    r.progress(1 / 3)
    h = LV.header(r.id)
    assert h["stage"] == "Building the model's image" and h["stage_at"] > first >= 0
    assert h["stages"] == [s["title"] for s in h["steps"]], "a step in the plan is not listed twice"
    # What really began is recorded: a planned step that is never announced was passed over.
    assert h["seen"] == ["Checking your account", "Building the model's image"]
    r.end("done", summary="Ready")
    assert LV.header(r.id)["status"] == "done"


def test_the_deploy_route_names_the_run_in_plain_words():
    import inspect

    from captain_search import server

    src = inspect.getsource(server)
    assert 'LV.start("deploy", f"Deploying {model_words} to {p[\'label\']}"' in src
    assert "lr.plan(" in src and "lr.progress((d[\"index\"] - 1) / d[\"of\"])" in src
