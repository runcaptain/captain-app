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
    assert b == {"seq": a["seq"], "changed": [], "reset": False, "at": b["at"], "partial": []}, "nothing changed: the same sequence, and nothing to read"
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


def test_a_write_by_another_process_to_the_same_workspace_wakes_it_well_inside_a_second(svc):
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
    assert box["at"] - done < 0.8, f"woken {(box['at'] - done):.2f} s after the other process finished ({done - wrote:.2f} s to start and write)"


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
    # A client's first request, which learns where the feed is, counts as listening to the topics it names.
    seq = svc.get("/api/changes", topics="settings")["seq"]
    assert CH.listening("settings") and not CH.listening("deployed")
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


def test_a_topic_that_ends_in_a_colon_covers_every_one_of_its_kind(svc):
    """The kit's watcher can list "run:" for every run, in place of each run's own topic."""
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, "run:,notebooks")
    svc.ctx.store.put_new("projects/demo-shop/state.yaml", b"x: 1\n")
    time.sleep(0.2)
    assert "answer" not in box, "a project is not a run"
    svc.ctx.store.put_new("backtest-runs/tuned-vs-current/v1.yaml", b"x: 1\n")
    t.join(3)
    assert _t(box["answer"]) == ["run:tuned-vs-current"], "the run's own topic is what comes back, so the screen knows which"
    assert CH.covered({"run:a", "runs", "job:b"}, {"run:", "jobs"}) == {"run:a"} and CH.covered({"a"}, None) == {"a"}


def test_a_client_that_asks_again_lets_its_older_request_go(svc):
    """A page that opens another project must listen to it at once. It cannot end the request it has open, so
    it asks again under the same name, and the older request answers with nothing."""
    seq = svc.get("/api/changes")["seq"]
    box: dict = {}

    def old():
        box["answer"] = svc.get("/api/changes", since=seq, topics="project:shop", client="m1")
        box["at"] = time.perf_counter()

    t = threading.Thread(target=old, daemon=True)
    t.start()
    time.sleep(0.15)
    other, other_box = _waiter(svc, seq, "project:shop")       # another client, with no name: left alone
    asked = time.perf_counter()
    t2, new = threading.Thread(target=lambda: new_box.update(a=svc.get("/api/changes", since=seq, topics="project:gifts", client="m1")), daemon=True), None
    new_box: dict = {}
    t2.start()
    t.join(2)
    assert box["answer"]["changed"] == [] and box["at"] - asked < 0.3, "the older request of the same client answered at once, with nothing"
    assert "answer" not in other_box and "a" not in new_box, "nobody else was let go, and the new request is held"
    svc.ctx.store.put_new("projects/gifts/state.yaml", b"x: 1\n")
    t2.join(2)
    assert _t(new_box["a"]) == ["project:gifts"]
    svc.ctx.store.put_new("projects/shop/state.yaml", b"x: 1\n")
    other.join(2)
    assert _t(other_box["answer"]) == ["project:shop"]


def test_a_live_run_tells_its_end_at_once_and_one_written_by_another_process_is_seen(svc, tmp_path):
    from captain_search import live as LV

    seq = svc.get("/api/changes")["seq"]
    run = LV.start("custom", "A demo run", of="shop")
    assert _t(svc.get("/api/changes", since=seq, topics="runs", wait=0)) == ["runs"], "a run that began is news for the list"
    seq = CH.FEED.seq
    for i in range(20):
        run.log(f"line {i}")
    a = svc.get("/api/changes", since=seq, topics=f"runs,run:{run.id}", wait=0)
    assert _t(a) in ([f"run:{run.id}"], []), f"its lines tell the run, not the list: {a}"
    seq = CH.FEED.seq
    run.end("done")   # right after a line, inside the five-a-second limit: it must not be held back
    assert _t(svc.get("/api/changes", since=seq, topics=f"runs,run:{run.id}", wait=0)) == [f"run:{run.id}", "runs"], "the end is told at once"
    # a run started from a terminal writes the same folder from another process
    seq = CH.FEED.seq
    t, box = _waiter(svc, seq, "runs,run:")
    time.sleep(1.2)   # the folder's first look
    assert "answer" not in box
    code = ("from captain_search import live as LV; r = LV.start('custom', 'From a terminal', of='shop'); "
            "r.log('hello'); print(r.id)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-400:]
    done = time.perf_counter()
    t.join(4)
    rid = out.stdout.strip().splitlines()[-1]
    assert "answer" in box and set(_t(box["answer"])) == {"runs", f"run:{rid}"}, box
    assert box["at"] - done < 0.8, f"told {box['at'] - done:.2f} s after the other process wrote it"


def test_the_agent_pins_and_the_teams_views_are_told_when_their_files_change(svc, tmp_path, monkeypatch):
    """Two folders on this computer that a terminal writes and the sidebar shows, neither in the workspace. The
    sidebar read each on a clock (every 3 s and every 2 s). They are told on `pins` and `views` now."""
    import os

    home = tmp_path / "app-home"
    project = tmp_path / "captain"
    (home / "context").mkdir(parents=True)
    (project / "views" / "returns").mkdir(parents=True)
    (project / "views" / "returns" / "mod.json").write_text('{"title": "Returns"}')
    (project / "views" / "returns" / "main.js").write_text("// one")
    pins = CH.watch_folder(lambda: str(home / "context"), lambda name: {"pins"}, ("pins",))
    views = CH.watch_folder(lambda: str(project / "views"), lambda name: {"views"}, ("views",), deep=True)
    try:
        seq = svc.get("/api/changes")["seq"]
        t, box = _waiter(svc, seq, "pins")
        time.sleep(0.5)   # the folder's first look
        wrote = time.perf_counter()
        (home / "context" / "t.json").write_text('{"items": [{"ref": "dataset:demo-catalog"}]}')   # `cst context add`
        t.join(3)
        assert _t(box["answer"]) == ["pins"] and box["at"] - wrote < 0.8, box
        # a view's file saved in place by an editor: its folder's time does not move, the file's does
        seq = CH.FEED.seq
        t, box = _waiter(svc, seq, "views")
        time.sleep(0.5)
        wrote = time.perf_counter()
        f = project / "views" / "returns" / "main.js"
        with open(f, "r+") as fh:
            fh.write("// two, saved in place")
        os.utime(f, None)
        t.join(3)
        assert _t(box["answer"]) == ["views"] and box["at"] - wrote < 0.8, box
        # nobody listens to them: nothing looks
        time.sleep(11.0 if CH.LINGER_S > 5 else CH.LINGER_S + 1.0)
        assert pins.thread is None and views.thread is None, "with no listener the folders are not looked at"
    finally:
        CH._FOLDERS.remove(pins)
        CH._FOLDERS.remove(views)


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


# ---- what the watch for outside writes costs ----------------------------------------------------------------------
# On a real workspace the installed app's services sat at 50 to 75 percent of a processor with nobody doing anything:
# the watch looked at every folder three times a second. It is told by the system now where the system can, and a
# watch that has to look keeps to a budget.

def _big(tmp_path, dirs=2000, per=10):
    import os

    root = tmp_path / "big"
    for d in range(dirs):
        p = root / "datasets" / f"demo-{d:05d}" / "v1"
        p.mkdir(parents=True)
        for f in range(per):
            (p / f"part-{f:03d}.parquet").touch()
    (root / "projects" / "shop").mkdir(parents=True)
    return str(root)


@pytest.mark.parametrize("how", ["events", "looks"])
def test_an_idle_workspace_of_20000_files_costs_under_half_a_second_of_processor_in_15_seconds(tmp_path, monkeypatch, how):
    import select

    from captain_search.storage.backend import LocalStore

    if how == "events" and not hasattr(select, "kqueue"):
        pytest.skip("this system has no kqueue: the watch looks, and that is the other case")
    CH.reset_for_tests()
    if how == "looks":
        monkeypatch.setattr(CH.Watcher, "_events_loop", lambda self: False)   # as on a system without file events
    store = LocalStore(_big(tmp_path))                                        # 20,000 files in 4,000 folders
    CH.watch(store)
    w = CH._WATCHERS[store.uri]
    cpu = time.process_time()
    CH.FEED.listen({"data", "projects"})
    try:
        time.sleep(15.0)
        used = time.process_time() - cpu
        assert w.mode == how
        assert used < 0.5, f"{used:.2f} s of processor in 15 idle seconds ({how})"
        if how == "looks":
            # It has slowed its looks to keep to its budget, and says so: a screen keeps a slow read of its own.
            assert w.gap > 1.0 and "data" in CH.partial(store, ["data", "notebooks"]) and CH.partial(store, ["logins"]) == []
        else:
            assert CH.partial(store) == [], "told by the system: nothing is late"
        # and it still sees a write, by this test standing in for another process
        seq = CH.FEED.seq
        (tmp_path / "big" / "projects" / "shop" / "state.yaml").write_text("x: 1")
        got = CH.FEED.wait(seq, {"project:shop"}, timeout=6.0)
        assert [c["topic"] for c in got["changed"]] == ["project:shop"]
    finally:
        CH.FEED.unlisten({"data", "projects"})
        CH.reset_for_tests()


def test_the_watch_holds_only_the_folders_the_listeners_topics_come_from_and_stops_when_they_leave(tmp_path, monkeypatch):
    from captain_search.storage.backend import LocalStore

    CH.reset_for_tests()
    monkeypatch.setattr(CH, "LINGER_S", 0.4)
    root = tmp_path / "ws"
    for d in ("notebooks/zero-results", "datasets/demo-catalog/v1", "projects/shop", "objects/sha256/ab"):
        (root / d).mkdir(parents=True)
    store = LocalStore(str(root))
    CH.watch(store)
    w = CH._WATCHERS[store.uri]
    assert w.thread is None and w.mode == "idle", "nobody listens: nothing watches"
    CH.FEED.listen({"notebooks"})
    try:
        for _ in range(40):
            if w.tops is not None and w.mode != "idle":
                break
            time.sleep(0.05)
        assert w.tops == {"notebooks"}, f"only where notebooks are kept: {w.tops}"
        seq = CH.FEED.seq
        (root / "datasets" / "demo-catalog" / "v1" / "manifest.yaml").write_text("x: 1")   # not what is listened to
        (root / "notebooks" / "zero-results" / "v1.yaml").write_text("x: 1")
        got = CH.FEED.wait(seq, None, timeout=3.0)
        time.sleep(0.5)
        assert {c["topic"] for c in CH.FEED.answer(seq, None)["changed"]} == {"notebooks"}, "the dataset's folder is not watched"
        assert got["changed"]
    finally:
        CH.FEED.unlisten({"notebooks"})
    for _ in range(60):
        if w.thread is None:
            break
        time.sleep(0.1)
    assert w.thread is None and w.mode == "idle", "the last listener left: the watch stopped, and holds nothing open"
    CH.reset_for_tests()


def test_another_process_writing_a_folder_a_moment_after_this_one_did_is_still_told(svc):
    """This process's own write is not told twice by the watch. That must not hide someone else's write to the
    same folder just after it: it once did, for half a second, and that write was then never told at all."""
    import os

    seq = svc.get("/api/changes", topics="projects")["seq"]
    t, box = _waiter(svc, seq, "project:demo-shop")
    time.sleep(0.6)                                                        # the watch is up
    svc.ctx.store.put_new("projects/demo-shop/state.yaml", b"x: 1\n")     # this process
    t.join(3)
    seq = box["answer"]["seq"]
    t, box = _waiter(svc, seq, "project:demo-shop")
    with open(os.path.join(svc.root, "projects", "demo-shop", "notes-from-a-terminal.yaml"), "w") as fh:   # another, 0.15 s later
        fh.write("x: 2\n")
    t.join(3)
    assert "answer" in box and _t(box["answer"]) == ["project:demo-shop"], "the other process's write, just after ours, was not told"


def test_a_clients_first_answer_names_what_changed_just_before_it_asked(svc):
    """A screen reads what it shows, then its channel first asks where the feed is. A change in between was told
    to nobody: the models of a tracking server that landed in that moment never showed."""
    CH.bump("mlflow")
    CH.bump("project:shop", "projects")
    a = svc.get("/api/changes", topics="mlflow,project:")
    assert [c["topic"] for c in a["recent"]] == ["mlflow", "project:shop"] and a["changed"] == [], a
    assert svc.get("/api/changes", topics="notebooks")["recent"] == [], "only the topics it asked for"
    assert "recent" not in svc.get("/api/changes", since=a["seq"], topics="mlflow", wait=0), "a later ask is told through `changed`"
    # and not what changed long ago
    with CH.FEED.cond:
        CH.FEED.log = type(CH.FEED.log)([(s_, t_, at - 60) for s_, t_, at in CH.FEED.log], maxlen=CH.FEED.log.maxlen)
    assert svc.get("/api/changes", topics="mlflow")["recent"] == []
