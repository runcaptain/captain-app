"""Live runs: the event stream Runs draws, the CLI pipe for a team's own tools, and a live backtest."""
import json
import sys

import pytest

from captain_search import demo, live, ops
from captain_search.workspace import init_workspace


def test_events_round_trip_and_stopped_runs():
    r = live.start("training", "ranker v7", of="ranker")
    r.stage("Train", 1, 2)
    for i in range(3):
        r.metric("loss", 1.0 / (i + 1), step=i)
    r.progress(0.5, done=1, total=2)
    got = live.read(r.id)
    assert [e["type"] for e in got["events"]] == ["stage", "metric", "metric", "metric", "progress"]
    assert got["run"]["stage"] == "Train" and got["run"]["status"] == "running"
    assert live.read(r.id, since=got["next"])["events"] == []
    assert [x["id"] for x in live.listing(active=True)] == [r.id]
    r.end(ref="ranker@v7", summary="trained")
    assert live.header(r.id)["status"] == "done" and live.read(r.id)["events"][-1]["type"] == "result"
    gone = live.start("custom", "orphan", pid=2 ** 22 + 12345)  # no such process
    assert live.header(gone.id)["status"] == "stopped"
    with pytest.raises(ValueError):
        live.read("../etc")


def test_pipe_turns_json_lines_into_events():
    prog = 'import json; print("warming up"); print(json.dumps({"type": "metric", "name": "loss", "value": 0.5, "step": 1})); print(json.dumps({"type": "shopper", "arm": "a", "shopper": 1, "query": "q", "bought": True}))'
    out = live.pipe([sys.executable, "-c", prog], "training", "piped")
    ev = live.read(out["id"])["events"]
    assert out["exit_code"] == 0 and live.header(out["id"])["status"] == "done"
    assert [e["type"] for e in ev] == ["log", "log", "metric", "shopper"] and ev[1]["text"] == "warming up"
    bad = live.pipe([sys.executable, "-c", "raise SystemExit(3)"], "custom", "fails")
    assert bad["exit_code"] == 3 and live.header(bad["id"])["status"] == "failed"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    import os

    old = os.environ.get("CST_HOME")
    home = str(tmp_path_factory.mktemp("home"))
    os.environ["CST_HOME"] = home
    c = init_workspace("t", str(tmp_path_factory.mktemp("bucket")))
    demo.seed(c, progress=lambda s: None)
    if old is not None:
        os.environ["CST_HOME"] = old
    c.test_home = home  # where the workspace is registered, for the CLI
    return c


def test_live_backtest_matches_a_plain_one_and_shows_both_arms(seeded):
    plain = ops.backtest_run(seeded, "demo-tuned-vs-bm25")
    r = live.start("backtest", "demo")
    watched = ops.backtest_run(seeded, "demo-tuned-vs-bm25", live=r)
    same = lambda run: [{k: v for k, v in a.simulated.items() if k != "p99_ms"} for a in run.arms]  # latency is timed
    assert same(plain) == same(watched)  # batching changes nothing
    assert [c["delta"] for c in plain.comparisons] == [c["delta"] for c in watched.comparisons]
    ev = live.read(r.id)["events"]
    shoppers = [e for e in ev if e["type"] == "shopper"]
    arms = [a.arm for a in watched.arms]
    by_arm = {a: [e["shopper"] for e in shoppers if e["arm"] == a] for a in arms}
    assert by_arm[arms[0]] and by_arm[arms[0]] == by_arm[arms[1]]  # the same shoppers on each arm, in order
    conv = [e for e in ev if e["type"] == "metric" and e["name"] == "conversion"]
    assert {e["arm"] for e in conv} == set(arms) and conv[-1]["step"] > conv[0]["step"]
    stages = [e["name"] for e in ev if e["type"] == "stage"]
    assert stages[0].startswith("Rank") and stages[-1] == "Compare the arms"
    assert [e["fraction"] for e in ev if e["type"] == "progress"][-1] == 1.0
    json.dumps(ev)  # every event is plain JSON


def test_a_run_on_a_compute_target_streams_through_the_bucket(seeded, monkeypatch):
    """The app makes the run in the workspace store, the job's container writes to it, and the app reads it back:
    the same events a local run gets, in batches, read incrementally."""
    import os

    from typer.testing import CliRunner

    from captain_search.cli.main import app

    rid = live.start_remote(seeded, "backtest", "demo-tuned-vs-bm25", of="demo-tuned-vs-bm25", arms=["a", "b"], where="aws-test")
    assert [r["status"] for r in live.listing_remote(seeded) if r["id"] == rid] == ["queued"]
    live.note_remote(seeded, rid, job="job-123")
    assert live.is_remote(seeded, rid) and not live.is_remote(seeded, live.start("custom", "here").id)

    # In the container: `cst backtest run` with the job's variables. It runs in place even if the backtest names a
    # compute target (it must never submit itself again), and streams to the run the app made.
    monkeypatch.setenv("CST_JOB_ID", "job-123")
    monkeypatch.setenv("CST_LIVE_ID", rid)
    monkeypatch.setenv("CST_HOME", seeded.test_home)
    out = CliRunner().invoke(app, ["--json", "-w", seeded.workspace.name, "backtest", "run", "demo-tuned-vs-bm25", "--compute", "no-such-target"])
    assert out.exit_code == 0, out.output
    monkeypatch.delenv("CST_JOB_ID")

    first = live.read_remote(seeded, rid, since=0, limit=50)
    rest = live.read_remote(seeded, rid, since=first["next"])
    ev = first["events"] + rest["events"]
    assert live.read_remote(seeded, rid, since=rest["next"])["events"] == []
    assert [e["n"] for e in ev] == list(range(len(ev)))  # every event once, in order, across batches
    head = rest["run"]
    assert head["status"] == "done" and head["job"] == "job-123" and head["where"] == "aws-test"
    assert 1 < head["chunks"] < len(ev)  # batched, not one write per event
    shoppers = [e for e in ev if e["type"] == "shopper"]
    arms = sorted({e["arm"] for e in shoppers})
    assert len(arms) == 2 and [e["shopper"] for e in shoppers if e["arm"] == arms[0]] == [e["shopper"] for e in shoppers if e["arm"] == arms[1]]
    assert ev[-1]["type"] == "result"


def test_live_watch_reads_a_run_on_a_compute_target(seeded, monkeypatch):
    """`cst backtest run --compute` prints a live id. `cst live watch <id>` failed on it with "no live run <id>",
    because it read only this computer's runs (found on the first real Modal run, 2026-11-10)."""
    from typer.testing import CliRunner

    from captain_search.cli.main import app

    monkeypatch.setenv("CST_HOME", seeded.test_home)
    monkeypatch.setattr("captain_search.cli.live.time.sleep", lambda s: None)
    rid = live.start_remote(seeded, "training", "on a compute target", where="modal-test")
    run = live.attach_store(seeded, rid)
    run.stage("Train")
    run.log("hello from the job")
    run.end("done", ref="p@v2")
    out = CliRunner().invoke(app, ["-w", seeded.workspace.name, "live", "watch", rid])
    assert out.exit_code == 0, out.output
    assert "hello from the job" in out.output and "stage" in out.output

    # a run on this computer still reads as before, and an unknown id is one sentence, not a traceback
    here = live.start("custom", "here")
    here.log("local line")
    here.end()
    out = CliRunner().invoke(app, ["-w", seeded.workspace.name, "live", "watch", here.id])
    assert out.exit_code == 0 and "local line" in out.output
    out = CliRunner().invoke(app, ["-w", seeded.workspace.name, "live", "watch", "01nope"])
    assert out.exit_code != 0 and "no live run 01nope" in out.output and "Traceback" not in out.output

    # a remote run with no word from its job stops with the note instead of waiting forever
    quiet = live.start_remote(seeded, "training", "far away")
    body, tag = seeded.store.get_with_tag(f"live-heads/{quiet}.json")
    h = json.loads(body)
    h.update(status="running", updated="2020-01-01T00:00:00+00:00")
    seeded.store.put_if(f"live-heads/{quiet}.json", json.dumps(h).encode(), tag)
    out = CliRunner().invoke(app, ["-w", seeded.workspace.name, "live", "watch", quiet])
    assert out.exit_code == 0 and "No word from the job" in out.output


def test_a_quiet_remote_run_says_so(seeded):
    import json as _json

    rid = live.start_remote(seeded, "training", "far away")
    body, tag = seeded.store.get_with_tag(f"live-heads/{rid}.json")
    h = _json.loads(body)
    h["updated"] = "2020-01-01T00:00:00+00:00"
    seeded.store.put_if(f"live-heads/{rid}.json", _json.dumps(h).encode(), tag)
    assert "No word from the job" in live.read_remote(seeded, rid)["run"]["note"]


TRAINER = r'''python3 -c '
import json, os
print("loading data")
for step in range(4):
    print(json.dumps({"type": "metric", "name": "loss", "value": round(1 / (step + 1), 3), "step": step}))
open(os.environ["CST_TRAIN_OUT"], "w").write(json.dumps({"training_commit": "abc", "metrics": {"ndcg@10": 0.81}}))
print("saved")
'
'''


def _trainable(ctx, command):
    pm = ctx.registry.load("pipeline", "demo-tuned")
    return ops.pipeline_publish(ctx, {"name": "demo-tuned", "engine": pm.engine, "catalog": pm.catalog,
                                      "spec": {**pm.spec, "train": {"command": command}}})


def test_retrain_streams_its_training(seeded):
    from captain_search import retrain as RT

    _trainable(seeded, TRAINER)
    r = RT.retrain(seeded, "demo-tuned", local_exec=True)
    run = next(x for x in live.listing() if x["kind"] == "training" and x.get("ref") == r["pipeline"])
    ev = live.read(run["id"])["events"]
    assert run["status"] == "done" and run["stages"] == ["Train demo-tuned", "Publish the new version"]
    assert [e["text"] for e in ev if e["type"] == "log"] == ["loading data", "saved"]
    assert [e["value"] for e in ev if e["type"] == "metric" and e["name"] == "loss"] == [1.0, 0.5, 0.333, 0.25]
    assert any(e["type"] == "metric" and e["name"] == "ndcg@10" and e["value"] == 0.81 for e in ev)  # the trainer's own result
    assert ev[-1]["type"] == "result" and ev[-1]["ref"] == r["pipeline"]

    _trainable(seeded, "echo nearly; exit 3")
    with pytest.raises(ValueError, match="exit code 3"):
        RT.retrain(seeded, "demo-tuned", local_exec=True)
    failed = live.listing()[0]
    assert failed["status"] == "failed" and "exit code 3" in failed["error"]


def test_retrain_in_a_job_streams_through_the_bucket(seeded, monkeypatch):
    from captain_search import retrain as RT

    _trainable(seeded, TRAINER)
    rid = live.start_remote(seeded, "training", "Retrain demo-tuned", where="aws-test")
    monkeypatch.setenv("CST_JOB_ID", "job-9")
    monkeypatch.setenv("CST_LIVE_ID", rid)
    RT.retrain(seeded, "demo-tuned", compute="no-such-target", local_exec=True)  # in the job: trains here
    got = live.read_remote(seeded, rid)
    assert got["run"]["status"] == "done"
    assert [e["value"] for e in got["events"] if e["type"] == "metric" and e["name"] == "loss"] == [1.0, 0.5, 0.333, 0.25]
