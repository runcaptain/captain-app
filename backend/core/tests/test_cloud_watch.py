"""The cloud watcher: while someone is looking it asks each cloud's free listing every second and says "changed"
only when the answer changes. Against the AWS stand-in."""
import threading
import time

import pytest

from captain_search import staging as SG
from captain_search.staging import announce as AN
from captain_search.staging import fleet as FL
from captain_search import changes as CH
from captain_search.staging import cloud_watch as WT

from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import aws  # noqa: F401  (fixture)


SEQ: dict = {}


class _Feed:
    """The real change feed, with what a test needs to read from it: how many times each topic was told, and a
    listener that comes and goes at once."""

    @staticmethod
    def seq(topic):
        return SEQ.get(topic, 0)

    @staticmethod
    def listen(*topics):
        CH.FEED.listen(set(topics))

    @staticmethod
    def unlisten(*topics):
        CH.FEED.unlisten(set(topics) or set(CH.FEED._listening))


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    CH.reset_for_tests()
    SEQ.clear()
    CH.FEED.hear(lambda _seq, topics: [SEQ.__setitem__(t, SEQ.get(t, 0) + 1) for t in topics])
    monkeypatch.setattr(AN, "LINGER_S", 0.0)  # a listener that leaves is gone at once, so a test need not wait the feed's 10 seconds
    for name in ("seq", "listen", "unlisten"):
        monkeypatch.setattr(AN, name, getattr(_Feed, name), raising=False)
    WT.stop_all()
    FL.forget()
    yield
    WT.stop_all()
    CH.reset_for_tests()
    FL.forget()


ASKED = {"n": 0}


@pytest.fixture(autouse=True)
def counted(monkeypatch):
    """Count every time a cloud's listing is asked for, by anyone."""
    real = FL._ask

    def ask(ctx, src):
        ASKED["n"] += 1
        return real(ctx, src)

    ASKED["n"] = 0
    monkeypatch.setattr(FL, "_ask", ask)


def _asks(aws=None):  # noqa: F811
    return ASKED["n"]


def _until(what, seconds=2.0):
    end = time.time() + seconds
    while time.time() < end:
        if what():
            return True
        time.sleep(0.02)
    return bool(what())


def test_with_nobody_listening_nothing_is_asked(ws, aws, monkeypatch):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    before, told = _asks(aws), AN.seq("deployed")  # the deploy's own writes were told, by the store
    stop = WT.start(ws)
    time.sleep(0.5)
    assert _asks(aws) == before and WT.status(ws) == []  # silent: no call to the cloud, no place being watched
    assert AN.seq("deployed") == told
    # Someone listening to another part of the app does not start it.
    AN.listen("projects")
    time.sleep(0.4)
    assert _asks(aws) == before and WT.status(ws) == []
    stop.set()
    SG.unstage(ws, "shop")


def test_an_endpoint_that_disappears_is_told_within_two_seconds(ws, aws, monkeypatch):  # noqa: F811
    """A model stopped in the cloud's own console, or by another computer: nothing here did it, and a screen that is
    looking still hears within two seconds."""
    monkeypatch.setattr(WT, "SCALE", 0.1)
    rec = SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    AN.listen("deployed")
    WT.start(ws)
    assert _until(lambda: any(w["cloud"] == "aws" for w in WT.status(ws)))  # the first listener started it
    time.sleep(0.4)
    quiet_at, asks = AN.seq("deployed"), _asks(aws)
    time.sleep(0.4)
    assert AN.seq("deployed") == quiet_at and _asks(aws) > asks  # asked again and again, and silent while nothing changes
    # The list read uses the watcher's answer: it makes no call of its own while a watcher is asking.
    during = _asks(aws)
    rows = FL.deployed(ws, fresh=True)["rows"]
    assert [r["state"] for r in rows] == ["running"] and _asks(aws) - during <= 1
    began = time.time()
    del aws.endpoints[rec["endpoint"]["name"]]  # gone, and nothing told Captain
    assert _until(lambda: AN.seq("deployed") > quiet_at, 2.0), "told within two seconds"
    assert time.time() - began < 2.0 and AN.seq("deployment:shop") >= 1
    # And what the list says next is what the cloud says: it is not running, and the record is corrected.
    r = FL.deployed(ws)["rows"]
    assert [x["state"] for x in r] != ["running"] and not any(x["in_cloud"] and x["state"] == "running" for x in r)
    # The last listener leaves: the asking stops.
    AN.unlisten()
    assert _until(lambda: WT.status(ws) == [], 2.0)
    stopped = _asks(aws)
    time.sleep(0.4)
    assert _asks(aws) == stopped


def test_a_cloud_that_says_slow_down_is_asked_less_and_the_list_says_so(ws, aws, monkeypatch):  # noqa: F811
    monkeypatch.setattr(WT, "SCALE", 0.05)
    monkeypatch.setattr(WT, "SLOW_MIN_S", 0.6)
    monkeypatch.setattr(WT, "SLOW_MAX_S", 0.6)
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    AN.listen("deployed")
    real = FL._ask
    throttled = {"on": True, "n": 0}

    def ask(ctx, src):
        if throttled["on"] and src["cloud"] == "aws":
            throttled["n"] += 1
            return [], "An error occurred (ThrottlingException) when calling the ListEndpoints operation: Rate exceeded"
        return real(ctx, src)

    monkeypatch.setattr(FL, "_ask", ask)
    WT.start(ws)
    assert _until(lambda: any(w["slowed"] for w in WT.status(ws)))
    w = next(x for x in WT.status(ws) if x["cloud"] == "aws")
    assert w["every_s"] >= 0.6 and w["text"] == f"AWS asked Captain to slow down. Asking every {w['every_s']:g} seconds."
    n = throttled["n"]
    time.sleep(0.5)
    assert throttled["n"] - n <= 1, "slowed: at 20 a second it would have asked ten times"
    # The list carries the sentence beside that cloud, and still shows what Captain remembers.
    d = FL.deployed(ws, fresh=True)
    assert next(a for a in d["asked"] if a["cloud"] == "aws")["watching"] == w["text"]
    # When the cloud answers again it goes back to once a second.
    throttled["on"] = False
    assert _until(lambda: not any(x["slowed"] for x in WT.status(ws)) and all(x["every_s"] == WT.every(x["cloud"]) for x in WT.status(ws)), 3.0)
    SG.unstage(ws, "shop")


def test_a_plain_error_backs_off_and_is_not_called_slowed():
    assert WT._slow("An error occurred (ThrottlingException): Rate exceeded") and WT._slow("429 Too Many Requests") and WT._slow("RESOURCE_EXHAUSTED: quota")
    assert not WT._slow("Could not connect to the endpoint URL") and not WT._slow(None)


def test_a_change_in_captains_own_records_is_told_too(ws, aws, monkeypatch):  # noqa: F811
    """A model on this computer, or stopped from a terminal: no cloud says so, the record does."""
    monkeypatch.setattr(WT, "SCALE", 0.1)
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    AN.listen("deployment:shop")
    stop = threading.Event()
    t = threading.Thread(target=WT.run, args=(ws, stop, 0.1), daemon=True)
    t.start()
    time.sleep(0.5)
    before = AN.seq("deployment:shop")
    SG.unstage(ws, "shop")
    assert _until(lambda: AN.seq("deployment:shop") > before, 2.0)
    stop.set()


# ---- jobs announce themselves ---------------------------------------------------------------------


def test_a_job_announces_every_step_line_and_its_end(ws, aws, monkeypatch):  # noqa: F811
    """A screen pressed Stop and then asked after the job once a second. When that asking broke, the row said
    "Stopping" for two hours. A job now says so itself: each change bumps its own topic, the list's and the
    project's, so nothing has to ask."""
    from captain_search import server as SV

    said = []
    CH.FEED.hear(lambda _seq, t: said.append(tuple(t)))
    hold, named = threading.Event(), threading.Event()

    def work(progress):
        named.wait(2)
        SV.JOBS[jid]["log"].append("Removing the endpoint.")
        SV.JOBS[jid]["message"] = "Removing the endpoint."
        progress(0.5)
        hold.wait(2)
        return {"text": "Stopped."}

    jid = "pending"
    jid = SV._job(work, "stopping the deployed model of shop", SV._deployed_topics("shop"))
    named.set()
    assert _until(lambda: SV.JOBS[jid].get("progress") == 0.5)
    topics = {"jobs", f"job:{jid}", "deployed", "deployment:shop"}
    assert said and all(set(t) == topics for t in said)  # every announcement names the job, the list and the project
    n = len(said)
    assert n >= 3  # running, the line, the message, the progress
    SV.JOBS[jid]["message"] = "Removing the endpoint."  # the same again says nothing
    assert len(said) == n
    hold.set()
    assert _until(lambda: SV.JOBS[jid]["status"] == "done")
    # The job's last words come a moment after its status: wait for the counts to agree, not for one instant.
    assert _until(lambda: len(said) > n and AN.seq(f"job:{jid}") == len(said) == AN.seq("deployed"))
    # It still reads as a plain record for the route that returns it.
    import json
    assert json.loads(json.dumps(SV.JOBS[jid]))["result"] == {"text": "Stopped."}


def test_a_job_that_fails_announces_that_too(monkeypatch):
    from captain_search import server as SV

    def work(_p):
        raise RuntimeError("the cloud said no")

    jid = SV._job(work, "removing the deploy of shop", SV._deployed_topics("shop"))
    assert _until(lambda: SV.JOBS[jid]["status"] == "failed")
    assert AN.seq(f"job:{jid}") >= 2 and AN.seq("deployment:shop") >= 2 and SV.JOBS[jid]["message"] == "the cloud said no"


def test_each_cloud_has_its_own_pace_and_a_slow_ask_is_never_more_than_half_the_time(ws, aws, monkeypatch):  # noqa: F811
    """Asking Modal what is there starts a process that takes about a second and real CPU: every two seconds, not
    one. And any place whose listing is slow is left alone for at least as long as the ask took."""
    assert [WT.every(c) for c in ("aws", "gcp", "azure", "modal")] == [1.0, 1.0, 1.0, 5.0]             # something of Captain's is there
    assert [WT.every(c, False) for c in ("aws", "gcp", "azure", "modal")] == [15.0, 15.0, 15.0, 60.0]  # nothing is
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    real = FL._ask

    def slow(ctx, src):
        time.sleep(0.3)
        return real(ctx, src)

    monkeypatch.setattr(FL, "_ask", slow)
    monkeypatch.setattr(WT, "SCALE", 0.05)
    src = next(x for x in FL.sources(ws) if x["cloud"] == "aws")
    WT.look(ws, src)
    with WT._lock:
        p = dict(WT._places[WT._key(ws, src)])
    assert p["took"] >= 0.3 and p["wait"] >= p["took"] > p["every"]
    assert WT.status(ws)[0]["took_s"] >= 0.3
    SG.unstage(ws, "shop")


def test_errors_wait_two_four_eight_and_up_to_a_minute(ws, aws, monkeypatch):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    monkeypatch.setattr(FL, "_ask", lambda ctx, src: ([], "Could not connect to the endpoint URL"))
    src = next(x for x in FL.sources(ws) if x["cloud"] == "aws")
    seen = []
    for _ in range(8):
        WT.look(ws, src)
        seen.append(WT.status(ws)[0]["every_s"])
    assert seen == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0] and not WT.status(ws)[0]["slowed"]
    assert WT.status(ws)[0]["text"] == "AWS could not be asked. Trying again every 60 seconds."
    monkeypatch.setattr(FL, "_ask", lambda ctx, src: ([], "ThrottlingException: Rate exceeded"))
    WT.stop_all()
    WT.look(ws, src)
    assert WT.status(ws)[0]["every_s"] == 15.0 and WT.status(ws)[0]["slowed"]


def test_a_login_with_nothing_deployed_is_asked_every_fifteen_seconds_and_every_second_once_something_is(ws, aws, monkeypatch):  # noqa: F811
    """Most logins have nothing of Captain's on them most of the time. A listing every second for nothing is waste:
    such a place is asked every 15 seconds, only to find things made there with no project."""
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    src = next(x for x in FL.sources(ws) if x["cloud"] == "aws")
    assert WT.busy(ws, src)
    WT.look(ws, src)
    st = WT.status(ws)[0]
    assert (st["busy"], st["every_s"]) == (True, 1.0)
    SG.unstage(ws, "shop")   # stopped: Captain remembers it, and nothing of it is in the cloud
    assert not WT.busy(ws, src)
    WT.look(ws, src)
    st = WT.status(ws)[0]
    assert (st["busy"], st["every_s"]) == (False, 15.0) and st["text"] is None
    # A Modal login with nothing on it: once a minute.
    modal_src = {"cloud": "modal", "login": "modal", "region": None, "target": src["target"]}
    assert not WT.busy(ws, modal_src) and WT.every("modal", WT.busy(ws, modal_src)) == 60.0


def test_an_idle_place_is_asked_at_once_when_a_deploy_to_it_starts(ws, aws, monkeypatch):  # noqa: F811
    monkeypatch.setattr(WT, "SCALE", 0.02)   # idle: every 0.3 s. busy: every 0.02 s
    monkeypatch.setitem(WT.IDLE_S, "aws", 500.0)   # idle on AWS: 10 s in this test, far longer than the test
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    SG.unstage(ws, "shop")
    AN.listen("deployed")
    WT.start(ws)
    assert _until(lambda: any(w["cloud"] == "aws" and not w["busy"] for w in WT.status(ws)))
    asked = _asks()
    time.sleep(0.3)
    assert _asks() == asked   # idle: not asked again for a long while
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)   # something of Captain's is there now
    assert _until(lambda: any(w["cloud"] == "aws" and w["busy"] for w in WT.status(ws)), 2.0), "asked at once, not ten seconds later"
    SG.unstage(ws, "shop")


# ---- what the watcher costs while nothing happens --------------------------------------------------------------------

def test_an_idle_watcher_never_counts_what_the_projects_hold_and_costs_next_to_nothing(ws, aws, monkeypatch):  # noqa: F811
    """On a real workspace the services sat at a third of a processor with nobody doing anything. The watcher read
    Captain's deployment records every second through the list of projects that also counts what each project holds:
    every dataset, run and model of the workspace, four times a second (81 ms each there). It reads the projects
    alone now, and one read stands until something is written."""
    from captain_search import projects as PR

    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    counted = {"listing": 0, "records": 0}
    real_listing, real_records = PR.listing, FL._records
    monkeypatch.setattr(PR, "listing", lambda *a, **k: (counted.__setitem__("listing", counted["listing"] + 1), real_listing(*a, **k))[1])
    monkeypatch.setattr(FL, "_records", lambda *a, **k: (counted.__setitem__("records", counted["records"] + 1), real_records(*a, **k))[1])
    AN.listen("deployed")                       # a Deployments screen is open, and nothing happens
    WT.start(ws)
    assert _until(lambda: counted["records"] >= 1)
    time.sleep(1.5)                             # the first looks, at the place and at the records
    counted.update(listing=0, records=0)
    cpu, secs = time.process_time(), 20.0
    time.sleep(secs)
    used = time.process_time() - cpu
    AN.unlisten("deployed")
    assert counted["listing"] == 0, f"the counted list of projects was read {counted['listing']} times by an idle watcher"
    assert counted["records"] <= 3, f"Captain's records were read {counted['records']} times in {secs:.0f} idle seconds: a read stands until something is written"
    # Under a second of processor for an idle minute, measured over a third of one.
    assert used < 1.0 * secs / 60, f"{used:.2f} s of processor in {secs:.0f} idle seconds"
    # And a write is still seen at once: the read does not stand past it.
    before = counted["records"]
    AN.listen("deployed")
    SG._save(ws, "shop", "staging", status="stopped", stop_reason="stopped by dana@example.com")
    assert _until(lambda: counted["records"] > before), "after a write the records are read again"
