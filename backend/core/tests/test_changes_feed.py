"""The change feed the app's live channel reads: one counter, topics, held open until something changes."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from captain_search import changes as CH
from captain_search import demo
from captain_search.workspace import init_workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    from captain_search import server

    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("shop", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    server._ctx = None
    return TestClient(server.app), {"Authorization": f"Bearer {server.TOKEN}"}


def test_a_thing_bumps_its_list_and_since_lists_what_changed():
    start = CH.seq()
    CH.bump("run:abc")
    got = CH.since(start, ["runs", "deployed"])
    assert [c["topic"] for c in got["changed"]] == ["runs"] and got["seq"] == CH.seq() and not got["reset"]
    assert [c["topic"] for c in CH.since(start, ["run:"])["changed"]] == ["run:abc"], '"run:" covers every run'
    assert sorted(c["topic"] for c in CH.since(start, ["run:abc", "runs"])["changed"]) == ["run:abc", "runs"]
    assert CH.since(CH.seq(), ["runs"])["changed"] == [], "nothing after the latest"
    assert CH.since(None, ["runs"]) == {"seq": CH.seq(), "changed": [], "reset": False}, "no since: where the feed is"
    assert CH.since(CH.seq() + 50, ["runs"])["reset"] is True, "ahead of the counter: the service restarted"
    for thing, parent in (("project:p1", "projects"), ("table:wh/main/orders", "data"), ("deployment:p1", "deployed")):
        at = CH.seq()
        CH.bump(thing)
        assert parent in [c["topic"] for c in CH.since(at, [parent])["changed"]]


def test_which_topics_a_write_changes():
    assert CH.topics_for("GET", "/api/datasets") == [] and CH.topics_for("POST", "/api/changes") == []
    assert CH.topics_for("POST", "/api/notebooks/weekly/rename") == ["notebooks"]
    assert CH.topics_for("POST", "/api/backtests/demo/run") == ["runs", "jobs"]
    assert CH.topics_for("PUT", "/api/settings/compute") == ["settings"]
    assert CH.topics_for("POST", "/api/projects/shop/deploy") == ["projects", "project:shop", "deployed", "deployment:shop"]
    assert CH.topics_for("DELETE", "/api/projects/shop/deploy") == ["projects", "project:shop", "deployed", "deployment:shop"]
    assert CH.topics_for("POST", "/api/library/move") == ["data"]
    assert set(CH.topics_for("POST", "/api/something-new")) == set(CH.LISTS), "an unknown write changes every list"


def test_the_route_answers_at_once_or_holds_until_a_change(client):
    c, h = client
    assert c.get("/api/changes").status_code == 401
    first = c.get("/api/changes?topics=notebooks", headers=h).json()
    assert first["changed"] == [] and first["reset"] is False
    # A write through the service changes its topic, and a reader asking from before it hears so.
    assert c.post("/api/notebooks/demo-zero-results/rename", headers=h, json={"to": "Weekly"}).status_code == 200
    got = c.get(f"/api/changes?since={first['seq']}&topics=notebooks,runs", headers=h).json()
    assert [x["topic"] for x in got["changed"]] == ["notebooks"] and got["seq"] > first["seq"]
    # A failed write changes nothing.
    at = got["seq"]
    assert c.post("/api/notebooks/demo-zero-results/rename", headers=h, json={"to": " "}).status_code == 400
    assert c.get(f"/api/changes?since={at}&topics=notebooks", headers=h).json()["changed"] == []
    # Held open: nothing to say comes back after the wait, and a change during the wait comes back at once.
    t0 = time.monotonic()
    quiet = c.get(f"/api/changes?since={at}&topics=deployed&wait=0.4", headers=h).json()
    assert quiet["changed"] == [] and 0.35 <= time.monotonic() - t0 < 2
    threading.Timer(0.2, lambda: CH.bump("deployment:shop")).start()
    t0 = time.monotonic()
    woke = c.get(f"/api/changes?since={at}&topics=deployed&wait=5", headers=h).json()
    took = time.monotonic() - t0
    assert [x["topic"] for x in woke["changed"]] == ["deployed"] and took < 1.0, f"answered the moment it changed: {took:.2f} s"


def test_what_changes_without_a_write_is_noticed_while_someone_is_watching(monkeypatch):
    monkeypatch.setattr(CH, "_sources", [])
    monkeypatch.setattr(CH, "_asked", {})
    state = {"cloud": "running"}
    reads = []
    CH.source(["deployed"], 2.0, lambda: (reads.append(1), state["cloud"])[1])
    start = CH.seq()
    # Nobody is asking about deployments: the cloud is not read at all.
    CH.check_sources(now=100.0)
    assert reads == []
    # A screen watching one model's page counts as watching what is deployed.
    CH._asked["deployment:shop"] = 100.0
    CH.check_sources(now=100.0)
    assert len(reads) == 1 and CH.seq() == start, "the first look only learns what is there"
    CH.check_sources(now=101.0)
    assert len(reads) == 1, "not read again before its two seconds are up"
    # The model is stopped in the cloud's own console. Within two seconds the topic changes.
    state["cloud"] = "gone"
    CH.check_sources(now=102.1)
    assert [c["topic"] for c in CH.since(start, ["deployed"])["changed"]] == ["deployed"]
    # The same state again changes nothing, and once nobody has asked for a while it is left alone.
    at = CH.seq()
    CH.check_sources(now=104.2)
    assert CH.seq() == at
    CH.check_sources(now=100.0 + CH.ASKED_FOR_S + 10)
    assert len(reads) == 3, "no longer watched: not read"


def test_a_jobs_progress_reaches_the_feed(client):
    from captain_search import server

    c, h = client
    first = c.get("/api/changes?topics=jobs", headers=h).json()
    done = threading.Event()

    def work(progress):
        progress(0.5)
        done.wait(5)
        return {"text": "ok"}

    jid = server._job(work, "a demo job")
    woke = c.get(f"/api/changes?since={first['seq']}&topics=jobs&wait=5", headers=h).json()
    assert "jobs" in [x["topic"] for x in woke["changed"]], "a job starting is a change"
    at = woke["seq"]
    done.set()
    end = c.get(f"/api/changes?since={at}&topics=jobs,deployed&wait=5", headers=h).json()
    assert "jobs" in [x["topic"] for x in end["changed"]], "and so is its end"
    assert server.JOBS[jid]["status"] == "done"
