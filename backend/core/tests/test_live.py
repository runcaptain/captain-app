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
    os.environ["CST_HOME"] = str(tmp_path_factory.mktemp("home"))
    c = init_workspace("t", str(tmp_path_factory.mktemp("bucket")))
    demo.seed(c, progress=lambda s: None)
    if old is not None:
        os.environ["CST_HOME"] = old
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
