"""Compute jobs tell the change feed themselves (jobs_watch.py): while someone listens, each unfinished job's host
is asked for its state, and a change is told. With nobody listening no host is asked. Against a stand-in host."""
from __future__ import annotations

import time

import pytest

from captain_search import changes as CH
from captain_search import jobs as J
from captain_search import jobs_watch as JW
from captain_search.workspace import init_workspace


class Host:
    def __init__(self):
        self.state, self.asks = {"state": "running"}, 0

    def status(self, handle):
        self.asks += 1
        return dict(self.state)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    CH.reset_for_tests()
    JW.stop_all()
    JW.looks = 0
    monkeypatch.setattr(CH, "LINGER_S", 0.3)
    monkeypatch.setattr(JW, "EVERY_S", 0.3)   # the rhythm is 4 s: shorter here so the test is quick
    monkeypatch.setattr(JW, "IDLE_S", 0.1)
    ctx = init_workspace("t", str(tmp_path / "store"))
    CH.watch(ctx.store)
    host = Host()
    monkeypatch.setattr(J, "_target", lambda ctx, rec: {"kind": "demo"})
    monkeypatch.setattr(J.P, "get", lambda target, ctx=None: host)
    monkeypatch.setattr(J, "_with_cost", lambda ctx, rec: rec)
    J._save(ctx, {"id": "j1", "state": "running", "handle": {"id": "h1"}, "target": "gpu-batch", "target_kind": "demo", "label": "a demo job"})
    J._save(ctx, {"id": "j0", "state": "succeeded", "handle": {"id": "h0"}, "target": "gpu-batch", "target_kind": "demo", "label": "an older job"})
    ctx.host = host
    yield ctx
    JW.stop_all()
    CH.reset_for_tests()


def _names(a):
    return [c["topic"] for c in a["changed"]]


def test_with_nobody_listening_no_host_is_asked(ws):
    JW.start(ws)
    time.sleep(0.8)
    assert JW.looks == 0 and ws.host.asks == 0
    CH.FEED.listen({"projects"})   # listening to something else does not start it either
    time.sleep(0.6)
    CH.FEED.unlisten({"projects"})
    assert JW.looks == 0 and ws.host.asks == 0


def test_a_job_that_ends_on_its_host_is_told_and_a_finished_one_is_not_asked_again(ws):
    JW.start(ws)
    seq = CH.FEED.seq
    CH.FEED.listen({"jobs"})        # the Runs screen is open
    try:
        got = CH.FEED.wait(seq, {"job:j1"}, timeout=2.0)
        assert _names(got) == ["job:j1"], "a running job is told on each look: its log on the host may have grown"
        assert _names(CH.FEED.answer(seq, {"jobs", "job:j0"})) == [], "its state did not change, and the finished job is left alone"
        ws.host.state = {"state": "succeeded", "exit_code": 0}   # it ends, on the host
        ended = time.perf_counter()
        seq = CH.FEED.seq
        for _ in range(40):
            got = CH.FEED.wait(seq, {"jobs"}, timeout=0.1)
            if got["changed"]:
                break
        assert _names(got) == ["jobs"] and time.perf_counter() - ended < 1.5, "the list is told when the state is saved"
        assert J.load(ws, "j1")["state"] == "succeeded" and J.load(ws, "j1")["finished_at"]
        time.sleep(0.5)
        n = ws.host.asks
        time.sleep(1.0)
        assert ws.host.asks == n, "a finished job's host is never asked again"
    finally:
        CH.FEED.unlisten({"jobs"})
    time.sleep(0.6)
    n = JW.looks
    time.sleep(0.8)
    assert JW.looks == n, "and with the last listener gone it stops looking"
