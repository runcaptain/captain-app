"""The two steps every deploy ends with: the trained-numbers check, and saving the version to the registry."""
from __future__ import annotations

import pytest

from captain_search import project_state as PS
from captain_search import staging as SG
from captain_search.staging import consistency as SC
from captain_search.staging import deploy as DP
from captain_search.staging import deploy_steps as DS

from test_staging import keychain, ws  # noqa: F401  (fixtures)


def test_every_deploy_ends_by_checking_it_matches_training(ws):  # noqa: F811
    p = DP.plan(ws, "shop", "local")
    assert [s["id"] for s in p["steps"]] == [*DP.STEPS, "match"]  # no registry on this project: no saving step
    assert p["steps"][-1]["title"] == "Checking it matches training"
    out = DP.run(ws, "shop", "local")
    step = out["steps"][-1]
    assert out["status"] == "done" and (step["id"], step["status"]) == ("match", "done")
    assert step["lines"] and step["lines"][-1].startswith("Served features match training: 100% of "), step["lines"]
    assert out.get("soft_failed") == []
    assert SG.state(ws, "shop")["check"]["status"] == "pass"  # kept with the deployment, where the screen reads it


def test_a_mismatch_fails_the_step_not_the_deploy_and_is_reported(ws, monkeypatch):  # noqa: F811
    bad = {"status": "fail", "text": "Served features differ from training on 4% of 250 values checked.", "checked": 250}
    monkeypatch.setattr(SC, "check", lambda ctx, pid, env="staging", *a, **k: bad)
    out = DP.run(ws, "shop", "local")
    step = out["steps"][-1]
    assert out["status"] == "done" and SG.state(ws, "shop")["status"] == "running", "the model stays up"
    assert (step["id"], step["status"]) == ("match", "failed") and step["lines"][-1] == bad["text"]
    assert out["soft_failed"] == [{"id": "match", "title": "Checking it matches training", "error": bad["text"]}]
    assert DP.status(ws, "shop")["soft_failed"][0]["id"] == "match"  # what the deployed screen reads


def test_saving_to_the_registry_runs_only_with_a_registry_files_a_candidate_and_never_twice(ws, monkeypatch):  # noqa: F811
    from captain_search import model_versions as MV

    assert not DS.has_registry(ws, "shop")
    PS.set_registry(ws, "shop", "two-tower", "shop", "s3://registry-bucket")
    assert DS.has_registry(ws, "shop")
    calls = []

    def publish(ctx, pipeline, arch, brand, registry=None, profile=None, status="candidate", progress=lambda f: None):
        calls.append({"pipeline": pipeline, "arch": arch, "brand": brand, "registry": registry, "status": status})
        return {"path": f"{registry}/{arch}/{brand}/v1/", "version": "v1"}

    monkeypatch.setattr(MV, "publish", publish)
    assert [s["id"] for s in DP.plan(ws, "shop", "local")["steps"]][-2:] == ["match", "publish"]
    out = DP.run(ws, "shop", "local")
    step = out["steps"][-1]
    version = SG.state(ws, "shop")["model_version"]
    assert (step["id"], step["title"], step["status"]) == ("publish", "Saving this version to your model registry", "done")
    assert step["lines"][-1] == "Saved as s3://registry-bucket/two-tower/shop/v1/, as a candidate."
    # Filing, not approval: it is saved as a candidate, and nothing about review or evidence changes.
    assert calls == [{"pipeline": version, "arch": "two-tower", "brand": "shop", "registry": "s3://registry-bucket", "status": "candidate"}]
    doc = PS.get(ws, "shop")
    assert doc["registry_saved"] == {version: "s3://registry-bucket/two-tower/shop/v1/"}
    assert not (doc.get("ab") or {}).get("decision") and "handoff" not in str(doc.get("ship") or "")
    # The same version deployed again is not saved again.
    SG.unstage(ws, "shop")
    out = DP.run(ws, "shop", "local")
    assert len(calls) == 1 and out["steps"][-1]["lines"][-1] == "Already saved as s3://registry-bucket/two-tower/shop/v1/."


def test_a_registry_that_cannot_be_written_fails_only_its_step(ws, monkeypatch):  # noqa: F811
    from captain_search import model_versions as MV

    PS.set_registry(ws, "shop", "two-tower", "shop", "s3://registry-bucket")

    def boom(*a, **k):
        raise ValueError("Captain can't write to s3://registry-bucket with this login.")

    monkeypatch.setattr(MV, "publish", boom)
    out = DP.run(ws, "shop", "local")
    assert out["status"] == "done" and SG.state(ws, "shop")["status"] == "running"
    assert out["soft_failed"] == [{"id": "publish", "title": "Saving this version to your model registry", "error": "Captain can't write to s3://registry-bucket with this login."}]
    assert "registry_saved" not in PS.get(ws, "shop") or not PS.get(ws, "shop")["registry_saved"]


def test_launch_review_keeps_its_evidence_gate_from_the_terminal_and_is_on_no_screen(ws, monkeypatch):  # noqa: F811
    """The honesty gates are unchanged by one-step Deploy and by Captain no longer running A/B tests: a deployed and
    saved version with only simulated evidence still cannot launch. Real-traffic evidence now comes from the
    team's own testing tool, brought in from the terminal. Launch review itself is off every screen."""
    from captain_search import handoff as HO
    from captain_search import model_versions as MV
    from captain_search import ops

    PS.set_registry(ws, "shop", "two-tower", "shop", "s3://registry-bucket")
    monkeypatch.setattr(MV, "publish", lambda *a, **k: {"path": "s3://registry-bucket/two-tower/shop/v1/"})
    DP.run(ws, "shop", "local")
    PS.experiment_backtest(ws, "shop", ws.exp)
    env = ops.env_create(ws, "prod", "live", {"type": "record"})
    gate = next(g for g in ops.gates_for(ws, env, ws.candidate) if g["gate"] == "evidence")
    assert not gate["ok"] and gate["detail"].startswith("SIMULATED"), gate  # simulated evidence never launches
    with pytest.raises(ops.GateFailed, match="evidence"):
        ops.promote(ws, ws.candidate, "prod", confirm=True)
    # launch review is no longer a step on any screen: the path ends at a running endpoint with its address
    sh = SG.ship_state(ws, PS.get(ws, "shop"))
    assert [s["id"] for s in sh["steps"]] == ["stage", "try", "connect"]
    said = str([sh["steps"], sh["next"]]).lower()
    assert "launch" not in said and "handoff" not in said and "review" not in said
    ag = SG.agent_summary(ws, "shop")
    assert "launch" not in str([ag["steps"], ag["next"], ag["commands"]]).lower()
    # and the way to bring it in is still there, labelled as what it is
    d = ops.promote(ws, ws.candidate, "prod", confirm=True, override_reason="graded by hand outside Captain")
    b = HO.outcome(ws, d.handoff, "conversion", 0.012, "our testing tool, report 41")
    assert b["links"][-1]["evidence"] == "REAL_TRAFFIC" and b["links"][-1]["source"] == "our testing tool, report 41"

