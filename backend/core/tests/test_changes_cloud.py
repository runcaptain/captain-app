"""What only a cloud knows, told the moment it changes (staging/cloud_watch.py). While someone listens to what is
deployed, each connected login's cloud is asked what is there once a second, and the feed is bumped only when the
answer changes. With no listeners it asks nothing. Against the AWS stand-in: nothing here calls a real cloud."""
from __future__ import annotations

import time

import pytest

from captain_search import changes as CH
from captain_search import staging as SG
from captain_search.staging import cloud_watch as CW
from captain_search.staging import fleet as FL

from test_staging import keychain, ws  # noqa: F401  (fixtures)
from test_staging_cloud import IMAGE, ROLE, aws  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    CH.reset_for_tests()
    CW.reset_for_tests()
    FL.forget()
    monkeypatch.setattr(CH, "LINGER_S", 0.3)   # a listener that left is forgotten quickly, so a test need not wait
    yield
    CH.reset_for_tests()
    for _ in range(40):                          # the watchers end by themselves once nobody listens
        if not CW.running():
            break
        time.sleep(0.1)
    CW.reset_for_tests()
    FL.forget()


def _asks_of_the_model(aws):  # noqa: F811
    return [c for c, _ in aws.calls if c == "invoke_endpoint"]


def test_with_no_listeners_no_cloud_is_asked(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    before = len(aws.calls)
    assert CW.ensure(ws) == 0, "nobody is listening: no watcher starts"
    time.sleep(2.5)
    assert CW.asks == [] and CW.running() == 0
    assert len(aws.calls) == before, f"the cloud was asked {len(aws.calls) - before} times with nobody listening"
    # listening to something else does not start it either
    CH.FEED.listen({"projects", "data"})
    assert CW.ensure(ws) == 0
    time.sleep(1.2)
    assert CW.asks == [] and len(aws.calls) == before


def test_an_endpoint_that_disappears_wakes_deployed_within_two_seconds_and_no_model_is_called(ws, aws):  # noqa: F811
    rec = SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    model_calls = len(_asks_of_the_model(aws))
    CH.FEED.listen({"deployed"})                 # a Deployments screen is open
    assert CW.ensure(ws) == 1, "one watcher for the one connected login"
    for _ in range(40):                          # its first look is what "unchanged" is measured from
        if CW.asks:
            break
        time.sleep(0.05)
    time.sleep(0.3)
    seq = CH.FEED.seq
    assert CH.FEED.answer(seq, {"deployed"})["changed"] == [], "nothing changed yet, so nothing is told"
    time.sleep(1.2)
    assert CH.FEED.answer(seq, {"deployed"})["changed"] == [], "asked again, the same answer: still nothing told"

    del aws.endpoints[rec["endpoint"]["name"]]   # someone deleted it in the cloud's console
    gone = time.perf_counter()
    got = CH.FEED.wait(seq, {"deployed", "deployment:shop"}, timeout=4.0)
    took = time.perf_counter() - gone
    assert got["changed"] == ["deployed", "deployment:shop"], got
    assert took < 2.0, f"told {took:.2f} s after it disappeared"
    # the read a screen makes next sets Captain's record right
    row = FL.one(ws, "shop")
    assert row["state"] == "gone" and SG.state(ws, "shop")["status"] == "stopped"
    assert len(_asks_of_the_model(aws)) == model_calls, "the watcher never calls a model"
    # it asked about once a second, not in a tight loop
    n, span = len(CW.asks), CW.asks[-1][0] - CW.asks[0][0]
    assert n <= span / 0.5 + 3, f"{n} asks in {span:.1f} s"


def test_it_stops_when_the_last_listener_leaves_and_starts_again_for_the_next(ws, aws):  # noqa: F811
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    CH.FEED.listen({"deployment:shop"})          # one project's page counts too
    assert CW.ensure(ws) == 1
    time.sleep(0.6)
    assert CW.asks
    CH.FEED.unlisten({"deployment:shop"})
    for _ in range(40):
        if not CW.running():
            break
        time.sleep(0.1)
    assert CW.running() == 0, "the watcher ended after the last listener left"
    n = len(CW.asks)
    time.sleep(1.5)
    assert len(CW.asks) == n, "and asked nothing more"
    CH.FEED.listen({"deployed"})
    assert CW.ensure(ws) == 1 and CW.running() == 1, "the next listener starts it again"


def test_a_cloud_that_errors_or_says_too_often_is_asked_less(ws, aws, monkeypatch):  # noqa: F811
    assert CW._wait_after(0.05, None, 0) == pytest.approx(0.95), "a quick answer: once a second"
    assert CW._wait_after(1.4, None, 0) == pytest.approx(1.4), "a slow listing is never asked more than half the time"
    assert [CW._wait_after(0.1, "no network", n) for n in (1, 2, 3, 4, 9)] == [2.0, 4.0, 8.0, 16.0, 60.0], "errors back off, up to a minute"
    assert CW._wait_after(0.1, "ThrottlingException: Rate exceeded", 1) == 15.0, "asked too often: wait at least 15 seconds"
    # and in the loop: a cloud that keeps failing is asked a few times, not once a second
    SG.stage(ws, "shop", baseline=True, target="sm", confirm=True)
    monkeypatch.setattr(FL, "_ask", lambda ctx, src: ([], "ThrottlingException: Rate exceeded"))
    CH.FEED.listen({"deployed"})
    CW.ensure(ws)
    time.sleep(2.5)
    assert 1 <= len(CW.asks) <= 2, f"a throttled cloud was asked {len(CW.asks)} times in 2.5 s"
