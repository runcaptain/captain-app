"""The cloud watcher: while someone is looking it asks each cloud's free listing every second and says "changed"
only when the answer changes. Against the AWS stand-in."""
import threading
import time

import pytest

from captain_search import staging as SG
from captain_search.staging import announce as AN
from captain_search.staging import fleet as FL
from captain_search.staging import watch as WT

from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import aws  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def quiet():
    AN.reset()
    WT.stop_all()
    FL.forget()
    yield
    WT.stop_all()
    AN.reset()
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
    before = _asks(aws)
    stop = WT.start(ws)
    time.sleep(0.5)
    assert _asks(aws) == before and WT.status(ws) == []  # silent: no call to the cloud, no place being watched
    assert AN.seq("deployed") == 0
    stop.set()
    SG.unstage(ws, "shop")


def test_an_endpoint_that_disappears_is_told_within_two_seconds(ws, aws, monkeypatch):  # noqa: F811
    """A model stopped in the cloud's own console, or by another computer: nothing here did it, and a screen that is
    looking still hears within two seconds."""
    monkeypatch.setattr(WT, "EVERY_S", 0.1)
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
    monkeypatch.setattr(WT, "EVERY_S", 0.05)
    monkeypatch.setattr(WT, "SLOW_MIN_S", 0.6)
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
    assert _until(lambda: not any(x["slowed"] for x in WT.status(ws)) and all(x["every_s"] == WT.EVERY_S for x in WT.status(ws)), 3.0)
    SG.unstage(ws, "shop")


def test_a_plain_error_backs_off_and_is_not_called_slowed():
    assert WT._slow("An error occurred (ThrottlingException): Rate exceeded") and WT._slow("429 Too Many Requests") and WT._slow("RESOURCE_EXHAUSTED: quota")
    assert not WT._slow("Could not connect to the endpoint URL") and not WT._slow(None)


def test_a_change_in_captains_own_records_is_told_too(ws, aws, monkeypatch):  # noqa: F811
    """A model on this computer, or stopped from a terminal: no cloud says so, the record does."""
    monkeypatch.setattr(WT, "EVERY_S", 0.1)
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
    real = AN.bump
    monkeypatch.setattr(AN, "bump", lambda *t: (said.append(t), real(*t))[1])
    hold = threading.Event()

    def work(progress):
        SV.JOBS[jid]["log"].append("Removing the endpoint.")
        SV.JOBS[jid]["message"] = "Removing the endpoint."
        progress(0.5)
        hold.wait(2)
        return {"text": "Stopped."}

    jid = "pending"
    jid = SV._job(work, "stopping the deployed model of shop", SV._deployed_topics("shop"))
    assert _until(lambda: SV.JOBS[jid].get("progress") == 0.5)
    topics = (f"job:{jid}", "deployed", "deployment:shop")
    assert said and all(t == topics for t in said)  # every announcement names the job, the list and the project
    n = len(said)
    assert n >= 3  # running, the line, the message, the progress
    SV.JOBS[jid]["message"] = "Removing the endpoint."  # the same again says nothing
    assert len(said) == n
    hold.set()
    assert _until(lambda: SV.JOBS[jid]["status"] == "done")
    assert len(said) > n and AN.seq(f"job:{jid}") == len(said) and AN.seq("deployed") == len(said)
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
