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
    r = c.get("/api/mlflow/models", headers=h)
    assert r.status_code == 502 and r.json()["error"] == ("Couldn't reach MLflow at mlflow.example.com. Check the address in "
                                                         "Connect MLflow, and that the server is running.")


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
