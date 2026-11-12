"""More than one copy of a staged model: the request log, the quiet test and stopping when idle must not
depend on one process. Two runtimes over the same bundle and the same store stand in for two copies."""
import json

from captain_search.staging import logs as L
from captain_search.staging import runtime as RT

from test_staging import _runtime, keychain, ws  # noqa: F401  (fixtures)


def _two(ws, **kw):  # noqa: F811
    a = _runtime(ws, **kw)
    b = RT.Runtime(ws, a.bundle_key if hasattr(a, "bundle_key") else _key(ws, a), **kw)
    assert a.stage == b.stage and a.copy != b.copy
    return a, b


def _key(ws, a):  # noqa: F811
    return next(k for k in ws.store.list(f"staging/shop/staging/") if k.endswith("bundle.json") and a.stage in k)


def _control(rt, **fields):
    k = RT.control_key(rt.pid, rt.env)
    try:
        body, tag = rt.store.get_with_tag(k)
        doc = json.loads(body)
    except Exception:  # noqa: BLE001
        doc, tag = {}, None
    rt.store.put_if(k, json.dumps({**doc, **fields, "stage": rt.stage}).encode(), tag)


def test_two_copies_write_one_log_and_lose_nothing(ws):  # noqa: F811
    a, b = _two(ws)
    for i in range(30):
        (a if i % 2 else b).rank({"query": "jacket", "session_id": f"s{i}", "request_id": f"r{i}", "traffic": "test"})
    a.flush()
    b.flush()
    got = L.read(ws, "shop", "staging", a.stage)
    assert sorted(r["request_id"] for r in got["requests"]) == sorted(f"r{i}" for i in range(30))
    # Each copy wrote its own chunk under the same folder, and each request says which copy answered it.
    chunks = [k for k in ws.store.list(L.prefix("shop", "staging", a.stage)) if "/requests-" in k or k.split("/")[-1].startswith("requests-")]
    assert len(chunks) == 2 and {r.get("copy") for r in got["requests"]} == {a.copy, b.copy}


def test_a_split_left_by_an_older_version_is_ignored_on_every_copy(ws):  # noqa: F811
    """Captain runs no A/B tests: no copy assigns a shopper to anything, whatever an old control document says."""
    a, b = _two(ws)
    _control(a, split=0.5, salt="ab-1", serve="candidate")
    a.control(force=True)
    b.control(force=True)
    arms_a = [a.rank({"query": "tent", "session_id": f"s{i}", "traffic": "test"})["arm"] for i in range(200)]
    arms_b = [b.rank({"query": "tent", "session_id": f"s{i}", "traffic": "test"})["arm"] for i in range(200)]
    assert set(arms_a) == set(arms_b) == {"candidate"}


def test_shadow_mode_on_two_copies_serves_the_baseline_and_logs_both(ws):  # noqa: F811
    a, b = _two(ws)
    _control(a, shadow={"since": "2026-11-12T00:00:00+00:00"}, serve="candidate")
    a.control(force=True)
    b.control(force=True)
    for i, rt in enumerate([a, b, a, b]):
        assert rt.rank({"query": "tent", "session_id": f"s{i}", "request_id": f"r{i}", "traffic": "test"})["arm"] == "baseline"
    a.flush()
    b.flush()
    reqs = L.read(ws, "shop", "staging", a.stage)["requests"]
    assert len(reqs) == 4 and all(r.get("shadow") for r in reqs)


def test_a_quiet_copy_does_not_stop_the_endpoint_while_another_is_busy(ws):  # noqa: F811
    now = [1000.0]
    stopped = []
    a, b = _two(ws, idle_minutes=30, on_stop=stopped.append, clock=lambda: now[0])
    # Every request lands on b for 40 minutes: a is never used.
    for _ in range(40):
        now[0] += 60
        b.rank({"query": "jacket", "session_id": "s", "traffic": "test"})
        b.tick()
        a.tick()
    assert not a.stopped and not b.stopped and stopped == []
    # Then nothing for 31 minutes: now it stops, once.
    for _ in range(31):
        now[0] += 60
        a.tick()
        b.tick()
    assert (a.stopped or b.stopped) and stopped and stopped[0] == "no requests for 30 minutes"


def test_one_copy_alone_stops_as_before_and_a_sleeping_host_writes_no_marker(ws):  # noqa: F811
    now = [1000.0]
    rt = _runtime(ws, idle_minutes=30, clock=lambda: now[0])
    rt.rank({"query": "jacket", "session_id": "s", "traffic": "test"})
    now[0] += 31 * 60
    rt.tick()
    assert rt.stopped
    assert len(ws.store.list(RT.copies_prefix("shop", "staging", rt.stage))) <= 1
