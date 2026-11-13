"""The one change feed (changes.py): what changed, the moment it changes. A write through the service wakes a
waiting client within 100 ms. A write by another process to the same workspace on this computer wakes it within
1.5 s. A client that fell behind, or that holds a sequence from before a restart, is told to read everything."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from captain_search import changes as CH
from captain_search import projects as PR
from captain_search import server
from captain_search.workspace import init_workspace


@pytest.fixture
def svc(tmp_path, monkeypatch):
    CH.reset_for_tests()
    ctx = init_workspace("t", str(tmp_path / "store"))
    monkeypatch.setattr(server, "ctx", lambda *a, **k: (CH.watch(ctx.store), ctx)[1])
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}

    class S:
        pass

    s = S()
    s.ctx, s.c, s.hd, s.root = ctx, c, hd, str(tmp_path / "store")
    s.get = lambda path, **params: c.get(path, headers=hd, params=params).json()
    yield s
    CH.reset_for_tests()


def _t(answer) -> list[str]:
    """The names of the topics an answer says changed."""
    return [c["topic"] for c in answer["changed"]]


def _waiter(svc, since, topics):
    """A client held on the long poll, in its own thread. Returns (thread, box): box gets the answer and when."""
    box: dict = {}

    def go():
        box["answer"] = svc.get("/api/changes", since=since, topics=topics)
        box["at"] = time.perf_counter()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    time.sleep(0.15)  # it is waiting now
    assert "answer" not in box, f"it answered before anything changed: {box}"
    return t, box


# ---- which topics a write touches -----------------------------------------------------------------------------------

@pytest.mark.parametrize("key,topics", [
    ("projects.yaml", {"projects"}),
    ("projects/shop/state.yaml", {"projects", "project:shop"}),
    ("staging/shop/staging/deploy.json", {"deployed", "deployment:shop", "projects", "project:shop"}),
    ("staging/shop/staging/logs/stg-1/requests-a.jsonl", {"deployed", "deployment:shop"}),
    ("datasets/demo-catalog/v1/manifest.yaml", {"data", "table:demo-catalog"}),
    ("datasets/logs/demo-sessions/v2/manifest.yaml", {"data", "table:logs/demo-sessions"}),
    ("backtest-runs/tuned-vs-current/v1.yaml", {"runs", "run:tuned-vs-current"}),
    ("live-heads/r1.json", {"runs", "run:r1"}),
    ("jobs/j1.result.yaml", {"jobs", "job:j1"}),
    ("notebooks/zero-results/v1.yaml", {"notebooks"}),
    ("compute/gpu-batch.yaml", {"compute"}),
    ("serving/setup.yaml", {"compute", "deployed"}),
    ("settings.yaml", {"settings"}),
    ("objects/sha256/ab/cdef", set()),
])
def test_a_write_is_mapped_to_the_topics_a_screen_listens_to(key, topics):
    assert CH.topics_for(key) == topics


def test_every_stores_writes_tell_the_feed_so_no_caller_can_forget():
    from captain_search.storage import backend as B

    for cls in (B.LocalStore, B.S3Store, B.GCSStore, B.AzureStore):
        for name in ("put_new", "put_if"):
            assert getattr(getattr(cls, name), "_told", False), f"{cls.__name__}.{name} does not tell the feed"

    class Another(B.LocalStore):   # a store written tomorrow is hooked the same way, with nothing to remember
        def put_new(self, key, data):
            return super().put_new(key, data)

    assert Another.put_new._told


# ---- the wire --------------------------------------------------------------------------------------------------------

def test_the_first_call_answers_at_once_and_a_quiet_wait_ends_with_nothing(svc):
    t0 = time.perf_counter()
    a = svc.get("/api/changes")
    assert time.perf_counter() - t0 < 0.5 and a["changed"] == [] and a["seq"] > 0 and a["reset"] is False and a["at"].endswith("Z")
    b = svc.get("/api/changes", since=a["seq"], topics="projects", wait=0.3)
    assert b == {"seq": a["seq"], "changed": [], "reset": False, "at": b["at"]}, "nothing changed: the same sequence, and nothing to read"
    assert svc.c.get("/api/changes").status_code == 401, "it needs the token, like every other address"


def test_a_write_through_the_service_wakes_a_waiting_client_within_100_ms(svc):
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, "projects,deployed")
    wrote = time.perf_counter()
    assert svc.c.post("/api/projects", headers=svc.hd, json={"name": "Demo shop"}).status_code == 200
    t.join(3)
    took = box["at"] - wrote
    assert "projects" in _t(box["answer"]) and box["answer"]["seq"] > seq, box
    assert took < 0.1 + 0.15, f"woken {took * 1000:.0f} ms after the request that wrote began (the write itself is inside that)"
    # measured from the write itself, not from the start of the request around it
    seq = box["answer"]["seq"]
    t, box = _waiter(svc, seq, "project:demo-shop")
    wrote = time.perf_counter()
    svc.ctx.store.put_new("projects/demo-shop/notes.yaml", b"x: 1\n")
    t.join(3)
    assert _t(box["answer"]) == ["project:demo-shop"], "only the topics it listed"
    assert box["at"] - wrote < 0.1, f"woken {(box['at'] - wrote) * 1000:.0f} ms after the write"


def test_a_client_is_not_woken_by_a_topic_it_did_not_list(svc):
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, "notebooks")
    svc.ctx.store.put_new("projects/demo-shop/state.yaml", b"x: 1\n")
    time.sleep(0.3)
    assert "answer" not in box, "a project changed, and it listens to notebooks"
    svc.ctx.store.put_new("notebooks/zero-results/v1.yaml", b"x: 1\n")
    t.join(3)
    assert _t(box["answer"]) == ["notebooks"]


def test_a_write_by_another_process_to_the_same_workspace_wakes_it_within_a_second_and_a_half(svc):
    """A `cst` command in a terminal, or an agent, writes straight to the workspace's folder."""
    PR.create(svc.ctx, "Demo shop")
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, "projects,project:demo-shop")
    time.sleep(1.2)   # the folder watch has taken its first look
    assert "answer" not in box
    code = ("import sys; from captain_search.storage.backend import LocalStore; "
            "LocalStore(sys.argv[1]).put_new('projects/demo-shop/state.yaml', b'from: another process\\n')")
    wrote = time.perf_counter()
    assert subprocess.run([sys.executable, "-c", code, svc.root], capture_output=True, text=True).returncode == 0
    done = time.perf_counter()
    t.join(4)
    assert "answer" in box, "the other process's write was never seen"
    assert "project:demo-shop" in _t(box["answer"]) and "projects" in _t(box["answer"]), box["answer"]
    assert box["at"] - done < 1.5, f"woken {(box['at'] - done):.2f} s after the other process finished ({done - wrote:.2f} s to start and write)"


def test_this_process_own_write_is_told_once_not_again_by_the_folder_watch(svc):
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, "projects")
    time.sleep(1.2)
    svc.ctx.store.put_new("projects/demo-shop/state.yaml", b"x: 1\n")
    t.join(3)
    seq = box["answer"]["seq"]
    time.sleep(2.2)   # two more looks by the folder watch
    again = svc.get("/api/changes", since=seq, topics="projects,project:demo-shop", wait=0.1)
    assert again["changed"] == [], f"the folder watch reported this process's own write a second time: {again}"


def test_no_listeners_is_cheap_and_a_topic_stops_counting_ten_seconds_after_its_last_request(svc, monkeypatch):
    assert CH.LINGER_S == 10.0, "a client that vanishes simply stops asking: ten seconds later nobody is listening"
    assert not CH.listening() and not CH.listening("deployed"), "nobody has asked yet"
    w = CH._WATCHERS[svc.ctx.store.uri] if CH._WATCHERS else None
    assert w is None or w.thread is None, "with no listeners nothing watches the workspace's folder"
    t0 = time.perf_counter()
    for i in range(10000):
        CH.bump("jobs", f"job:{i}")
    assert time.perf_counter() - t0 < 0.5, "a bump with nobody listening costs next to nothing"
    monkeypatch.setattr(CH, "LINGER_S", 0.4)
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, "deployed,deployment:shop")
    assert CH.listening("deployed") and CH.listening("deployment:") and not CH.listening("notebooks"), "while its request is open"
    CH.bump("deployed")
    t.join(3)
    assert CH.listening("deployed"), "its request just ended: it still counts, since a long poll comes straight back"
    time.sleep(0.6)
    assert not CH.listening("deployed") and not CH.listening(), "and no longer once the time has passed"
    # a client that polls without waiting counts the same way
    svc.get("/api/changes", since=seq, topics="runs", wait=0)
    assert CH.listening("runs")
    time.sleep(0.6)
    assert not CH.listening("runs")


def test_a_missed_sequence_gets_reset(svc):
    seq = svc.get("/api/changes")["seq"]
    assert svc.get("/api/changes", since=seq - 10 ** 9, topics="projects")["reset"] is True, "from before this service started"
    assert svc.get("/api/changes", since=seq + 10 ** 6, topics="projects")["reset"] is True, "from a service that ran longer"
    # further behind than the feed remembers
    small = CH.Feed(keep=5)
    start = small.seq
    for i in range(12):
        small.bump(f"project:p{i}")
    a = small.answer(start, None)
    assert a["reset"] is True and a["changed"] == [] and a["seq"] == start + 12
    b = small.answer(start + 10, None)
    assert b["reset"] is False and b["changed"] == [{"topic": "project:p10", "seq": start + 11}, {"topic": "project:p11", "seq": start + 12}], "each with the sequence of its latest change"
    # a reset answers at once: it is not held
    t0 = time.perf_counter()
    assert svc.get("/api/changes", since=1, topics="projects")["reset"] is True and time.perf_counter() - t0 < 0.5


def test_a_job_and_a_live_run_and_a_login_tell_the_feed_without_a_store_write(svc):
    from captain_search import live as LV
    from captain_search import workspace as W

    seq = svc.get("/api/changes")["seq"]
    jid = server._job(lambda progress: (progress(0.5), "done")[1], "a demo job")
    for _ in range(50):
        if server.JOBS[jid]["status"] == "done":
            break
        time.sleep(0.02)
    a = svc.get("/api/changes", since=seq, topics=f"jobs,job:{jid}", wait=0.1)
    assert _t(a) == [f"job:{jid}", "jobs"], a
    seq = a["seq"]
    run = LV.start("custom", "A demo run", of="shop")
    run.log("one line")
    a = svc.get("/api/changes", since=seq, topics=f"runs,run:{run.id}", wait=0.1)
    assert _t(a) == [f"run:{run.id}", "runs"], a
    run.end("done")
    seq = svc.get("/api/changes")["seq"]
    W.save_config(W.load_config())
    assert _t(svc.get("/api/changes", since=seq, topics="logins", wait=0.1)) == ["logins"]


def test_the_socket_carries_the_same_messages(svc):
    with svc.c.websocket_connect(f"/api/changes/ws?token={server.TOKEN}") as ws:
        ws.send_json({"topics": ["projects", "project:demo-shop"]})
        first = ws.receive_json()
        assert first["changed"] == [] and first["seq"] > 0
        wrote = time.perf_counter()
        svc.ctx.store.put_new("projects/demo-shop/state.yaml", b"x: 1\n")
        got = ws.receive_json()
        assert _t(got) == ["project:demo-shop", "projects"] and got["seq"] > first["seq"] and time.perf_counter() - wrote < 0.1
        svc.ctx.store.put_new("notebooks/a/v1.yaml", b"x: 1\n")       # not listed: nothing arrives for it
        ws.send_json({"topics": ["notebooks"], "since": got["seq"]})   # change what it listens to
        assert _t(ws.receive_json()) == ["notebooks"], "what it missed since, for the topics it now lists"
        ws.send_json({"topics": ["notebooks"], "since": 1})
        assert ws.receive_json()["reset"] is True
    with pytest.raises(Exception):  # noqa: B017,PT011  no token, no connection
        with svc.c.websocket_connect("/api/changes/ws") as ws:
            ws.receive_json()
