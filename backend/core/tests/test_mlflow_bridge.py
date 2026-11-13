"""The MLflow bridge reads MLflow's REST API (faked here), never writes, and draws lineage from links."""
import httpx
import pytest

from captain_search import demo
from captain_search import mlflow_bridge as MF
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("m", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def fake(calls):
    def req(method, url, json=None, params=None, headers=None, timeout=None):
        calls.append((method, url))
        path = url.split("/api/2.0/mlflow/")[1]
        body = {
            "experiments/search": {"experiments": [{"experiment_id": "1", "name": "/rankers", "lifecycle_stage": "active",
                                                    "last_update_time": 1762400000000}]},
            "runs/search": {"runs": [{"info": {"run_id": "abc123def456", "run_name": "lgbm", "status": "FINISHED",
                                               "start_time": 1762400000000},
                                      "data": {"metrics": [{"key": "ndcg_10", "value": 0.71}],
                                               "params": [{"key": "lr", "value": "0.05"}],
                                               "tags": [{"key": "mlflow.user", "value": "x"}, {"key": "team", "value": "s"}]}}]},
            "registered-models/search": {"registered_models": [{"name": "ranker"}]},
            "model-versions/search": {"model_versions": [{"version": "3", "run_id": "abc123def456", "status": "READY"}]},
        }[path]
        return httpx.Response(200, json=body, request=httpx.Request(method, url))
    return req


def test_reads_runs_and_models_and_never_writes(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "request", fake(calls))
    with pytest.raises(ValueError, match="isn't set up"):
        MF.experiments(ctx)
    MF.configure(ctx, uri="https://mlflow.example.com/")
    assert MF.experiments(ctx)[0]["name"] == "/rankers"
    run = MF.runs(ctx, "1")[0]
    assert run["metrics"] == {"ndcg_10": 0.71} and run["params"] == {"lr": "0.05"} and run["tags"] == {"team": "s"}
    assert MF.models(ctx)[0]["versions"][0]["run_id"] == "abc123def456"
    assert all(u.startswith("https://mlflow.example.com/api/2.0/mlflow/") for _, u in calls)
    assert all(p.split("/")[-1] in ("search",) for _, p in calls)  # only search endpoints: read-only


def test_link_puts_mlflow_upstream_of_the_pipeline(ctx, monkeypatch):
    monkeypatch.setattr(httpx, "request", fake([]))
    MF.configure(ctx, uri="https://mlflow.example.com")
    MF.link(ctx, "ranker", "3", "demo-tuned")
    g = MF.lineage(ctx)
    edges = {(e["src"], e["dst"]) for e in g["edges"]}
    assert ("mlflow-run:abc123def456", "mlflow-model:ranker@3") in edges
    assert ("mlflow-model:ranker@3", "pipeline:demo-tuned@v1") in edges
    assert MF.status(ctx)["links"] == {"ranker@3": "demo-tuned@v1"}  # pinned to the version linked
    with pytest.raises(Exception):
        MF.link(ctx, "ranker", "4", "no-such-pipeline")


def test_errors_in_words(ctx, monkeypatch):
    MF.configure(ctx, uri="https://mlflow.example.com")
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(403, request=httpx.Request("POST", "https://x")))
    with pytest.raises(PermissionError, match="refused the login"):
        MF.experiments(ctx)


def test_a_refused_login_reaches_the_app_as_a_sentence(ctx, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import server

    monkeypatch.setattr(server, "_ctx", ctx)
    c = TestClient(server.app, raise_server_exceptions=False)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert c.put("/api/mlflow", headers=h, json={"uri": "https://mlflow.example.com"}).status_code == 200
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(403, request=httpx.Request("POST", "https://x")))
    r = c.get("/api/mlflow/experiments", headers=h)
    assert r.status_code == 403 and r.json()["error"].startswith("MLflow refused the login")

    def down(*a, **k):
        raise httpx.ConnectError("no route", request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(httpx, "request", down)
    r = c.get("/api/mlflow/experiments", headers=h)
    assert r.status_code == 502 and r.json()["error"] == ("Couldn't reach MLflow at mlflow.example.com. Check the address in "
                                                         "Connect MLflow, and that the server is running.")
    # The models are read behind the answer, so that route says the same sentence as a state, not as a failure.
    import time

    from captain_search import mlflow_bridge as MF

    MF.forget_models()
    assert c.get("/api/mlflow/models", headers=h).status_code == 200
    for _ in range(100):
        r = c.get("/api/mlflow/models", headers=h)
        if r.json()["state"] == "failed":
            break
        time.sleep(0.05)
    assert r.status_code == 200 and r.json()["text"].startswith("Could not be read: Couldn't reach MLflow at mlflow.example.com. Check the address in Connect MLflow")
    MF.forget_models()


def test_databricks_reads_unity_catalog_and_a_disabled_registry_isnt_a_login_error(ctx, monkeypatch):
    calls = []

    def req(method, url, json=None, params=None, headers=None, timeout=None):
        calls.append(url)
        msg = "The legacy workspace model registry is disabled for the current Databricks workspace."
        return httpx.Response(403, json={"error_code": "PERMISSION_DENIED", "message": msg},
                              request=httpx.Request(method, url))

    def get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/unity-catalog/models"):
            body = {"registered_models": [
                {"full_name": "main.search.ranker", "catalog_name": "main", "comment": "ours", "updated_at": 1762400000000},
                {"full_name": "system.ai.llama", "catalog_name": "system"}]}
        else:
            body = {"model_versions": [{"version": 2, "run_id": "r1", "status": "READY", "source": "dbfs:/m",
                                        "aliases": [{"alias_name": "champion", "version_num": 2}]}]}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "request", req)
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(MF, "_endpoint", lambda c: ("https://dbc-1.cloud.databricks.com", {"Authorization": "Bearer x"}))
    monkeypatch.setattr(MF, "_read", lambda c: ({"databricks_connection": "dbx"}, None))
    m = MF.models(ctx)
    assert [x["name"] for x in m] == ["main.search.ranker"]  # the system catalog is hidden
    assert m[0]["registry"] == "unity-catalog" and m[0]["versions"][0] == {
        "version": "2", "run_id": "r1", "status": "READY", "stage": None, "source": "dbfs:/m", "created": None,
        "aliases": ["champion"]}
    assert len(MF.models(ctx, include_system=True)) == 2
    with pytest.raises(MF.RegistryDisabled):
        MF._legacy_models(ctx)
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(
        403, json={"message": "Provided access token does not have required scopes: mlflow"},
        request=httpx.Request("POST", "https://x")))
    with pytest.raises(PermissionError, match="mlflow scope"):
        MF.experiments(ctx)


# ---- the models: always answered at once, read gently behind, and a rate limit is respected -----------------------
# A real hosted catalog has one rate limit for a whole organisation. Reading eight models' versions at once tripped
# it: the route answered 500 twice at every start and the sidebar said "MLflow: nothing yet".

import threading  # noqa: E402
import time  # noqa: E402

from captain_search import changes as CH  # noqa: E402
from captain_search import mlflow_bridge as MF  # noqa: E402


class Catalog:
    """A stand-in tracking server: a list of models, one call for each model's versions, and a rate limit it can
    switch on. It counts the calls and how many were in flight at once."""

    def __init__(self, n=6):
        self.names = [f"m{i}" for i in range(n)]
        self.calls, self.now, self.most, self.times = 0, 0, 0, []
        self.limit_after = None      # answer 429 from this many calls on
        self.retry_after = None
        self.lock = threading.Lock()

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        with self.lock:
            self.calls += 1
            self.now += 1
            self.most = max(self.most, self.now)
            self.times.append((time.monotonic(), timeout))
            limited = self.limit_after is not None and self.calls > self.limit_after
        try:
            time.sleep(0.02)
            if limited:
                hd = {"Retry-After": str(self.retry_after)} if self.retry_after else {}
                return httpx.Response(429, json={"message": "Your request was rejected since your organization has exceeded the rate limit. Please retry your request later."},
                                      headers=hd, request=httpx.Request(method, url))
            if "registered-models/search" in url:
                return httpx.Response(200, json={"registered_models": [{"name": n} for n in self.names]}, request=httpx.Request(method, url))
            return httpx.Response(200, json={"model_versions": [{"version": "1"}]}, request=httpx.Request(method, url))
        finally:
            with self.lock:
                self.now -= 1


@pytest.fixture
def catalog(ctx, monkeypatch):
    MF.configure(ctx, uri="https://mlflow.example.test")
    MF.forget_models()
    CH.reset_for_tests()
    c = Catalog()
    monkeypatch.setattr(httpx, "request", c.request)
    monkeypatch.setattr(MF, "PAUSE_S", 0.05)
    yield c
    MF.forget_models()
    CH.reset_for_tests()


def _until(what, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if what():
            return True
        time.sleep(0.02)
    return False


def test_with_nothing_kept_the_answer_is_at_once_and_says_it_is_reading(ctx, catalog):
    seq = CH.FEED.seq
    t0 = time.perf_counter()
    a = MF.models_kept(ctx)
    assert time.perf_counter() - t0 < 0.05, "the screen never waits for the tracking server"
    assert (a["models"], a["state"]) == ([], "reading") and a["text"].startswith("Reading the registered models.")
    b = MF.models_kept(ctx)                                  # asked twice at a start: one read, not two
    assert b["state"] == "reading"
    got = CH.FEED.wait(seq, {"mlflow"}, timeout=5.0)
    assert [c["topic"] for c in got["changed"]] == ["mlflow"], "told when the read lands, so the screen reads again"
    c = MF.models_kept(ctx)
    assert c["state"] == "ready" and c["text"] is None and [m["name"] for m in c["models"]] == catalog.names
    # gently: one call at a time, a pause between calls, ten seconds a call at most, and the list read once
    assert catalog.calls == 1 + len(catalog.names), f"one read for the two asks: {catalog.calls} calls"
    assert catalog.most == 1, f"{catalog.most} calls were in flight at once"
    gaps = [b[0] - a[0] for a, b in zip(catalog.times, catalog.times[1:])]
    assert min(gaps) >= 0.045, f"a pause between calls: the shortest gap was {min(gaps) * 1000:.0f} ms"
    assert {t for _, t in catalog.times} == {10.0}


def test_a_rate_limit_stops_the_read_and_is_said_in_words_never_as_an_error(ctx, catalog):
    catalog.limit_after, catalog.retry_after = 3, 40        # the fourth call is refused: wait 40 seconds
    a = MF.models_kept(ctx)
    assert a["state"] == "reading"
    assert _until(lambda: MF.models_kept(ctx)["state"] == "limited")
    b = MF.models_kept(ctx)
    assert b["models"] == [] and 38 <= b["retry_in_s"] <= 40
    assert b["text"].startswith("Could not be read: the model catalog is limiting requests, trying again in ") and b["text"].endswith(" s.")
    assert catalog.calls == 4, f"it stopped at the refusal: {catalog.calls} calls, not all {1 + len(catalog.names)}"
    # asked again and again while told to wait: nothing more is sent
    for _ in range(20):
        MF.models_kept(ctx)
    time.sleep(0.3)
    assert catalog.calls == 4, "no call is made before the wait is over, however often the screen asks"
    # the wait is over and the limit is lifted: the next ask reads, and the list arrives
    key = next(iter(MF._STATE))
    MF._STATE[key]["until"] = time.monotonic() - 1
    catalog.limit_after = None
    seq = CH.FEED.seq
    assert MF.models_kept(ctx)["state"] == "reading"
    assert CH.FEED.wait(seq, {"mlflow"}, timeout=5.0)["changed"]
    done = MF.models_kept(ctx)
    assert done["state"] == "ready" and len(done["models"]) == len(catalog.names)


def test_a_limit_with_no_wait_given_backs_off_longer_each_time(ctx, catalog):
    catalog.limit_after = 0                                  # refused from the first call, and no Retry-After
    MF.models_kept(ctx)
    assert _until(lambda: MF.models_kept(ctx)["state"] == "limited")
    first = MF.models_kept(ctx)["retry_in_s"]
    key = next(iter(MF._STATE))
    MF._STATE[key]["until"] = time.monotonic() - 1
    MF.models_kept(ctx)
    assert _until(lambda: MF._STATE[key].get("tries") == 2 and MF._STATE[key]["state"] == "limited")
    second = MF.models_kept(ctx)["retry_in_s"]
    assert 28 <= first <= 30 and 58 <= second <= 60, f"half a minute, then a minute: {first} s, {second} s"
    assert catalog.calls == 2, "one call for each try, never a loop"


def test_what_was_read_last_is_given_at_a_start_and_stands_while_the_catalog_limits(ctx, catalog):
    MF.models_kept(ctx)
    assert _until(lambda: MF.models_kept(ctx)["state"] == "ready")
    MF.forget_models()                                       # the services start again: nothing in memory
    catalog.limit_after, catalog.retry_after = catalog.calls, 120
    t0 = time.perf_counter()
    a = MF.models_kept(ctx)
    assert time.perf_counter() - t0 < 0.05 and len(a["models"]) == len(catalog.names), "the last read, at once, from its file"
    assert _until(lambda: MF.models_kept(ctx)["state"] == "limited")
    b = MF.models_kept(ctx)
    assert len(b["models"]) == len(catalog.names) and b["text"].startswith("Showing the last read: the model catalog is limiting requests, trying again in 2 min")


def test_the_route_never_fails_for_what_the_tracking_server_does(ctx, catalog, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import server

    monkeypatch.setattr(server, "_ctx", ctx)
    monkeypatch.setattr(server, "ctx", lambda *a, **k: ctx)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}
    c = TestClient(server.app)
    catalog.limit_after = 0
    r = c.get("/api/mlflow/models", headers=hd)
    assert r.status_code == 200 and r.json()["state"] == "reading" and r.json()["models"] == []
    assert _until(lambda: c.get("/api/mlflow/models", headers=hd).json()["state"] == "limited")
    r = c.get("/api/mlflow/models", headers=hd)
    assert r.status_code == 200 and "limiting requests" in r.json()["text"]
    # a server that can't be reached at all: the same, in words
    MF.forget_models()
    monkeypatch.setattr(httpx, "request", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("no route", request=httpx.Request("GET", "https://x"))))
    assert c.get("/api/mlflow/models", headers=hd).status_code == 200
    assert _until(lambda: c.get("/api/mlflow/models", headers=hd).json()["state"] == "failed")
    r = c.get("/api/mlflow/models", headers=hd)
    assert r.status_code == 200 and r.json()["text"].startswith("Could not be read: Couldn't reach MLflow at mlflow.example.test")


def test_the_calls_made_are_counted_and_said_in_the_log(ctx, catalog, tmp_path):
    from captain_search import applog

    applog.start(str(tmp_path / "logs"))
    try:
        before = MF.CALLS["n"]
        MF.models_kept(ctx)
        assert _until(lambda: MF.models_kept(ctx)["state"] == "ready")
        assert MF.CALLS["n"] - before == 1 + len(catalog.names)
        text = (tmp_path / "logs" / "services.log").read_text()
        assert f"read {len(catalog.names)} registered models with {1 + len(catalog.names)} calls" in text and "calls to the server since the services started" in text
    finally:
        applog.stop()
