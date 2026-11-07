"""Settings > Connected services: one list, saved compute defaults, project home screens, warehouse compute."""
import pytest

from captain_search import compute as C
from captain_search import demo
from captain_search import ops
from captain_search import projects as PR
from captain_search import services as SV
from captain_search import settings as ST
from captain_search.compute_providers import Job
from captain_search.compute_providers.warehouses import DatabricksJobsProvider, SnowparkContainersProvider
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("s", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def test_services_list_names_everything_without_secrets(ctx):
    s = SV.listing(ctx)
    assert s["storage"]["uri"] and s["compute"]["defaults"]["backtest"] == "local"
    kinds = {k["kind"] for k in s["compute"]["kinds"]}
    assert {"aws-ecs", "aws-batch", "databricks-jobs", "snowflake-spcs"} <= kinds
    assert any(t["name"] == "local" and "backtest" in t["default_for"] for t in s["compute"]["targets"])
    assert {k["label"] for k in s["model_keys"]} == {"Anthropic", "OpenAI"}
    assert "token" not in str(s).lower() or "token_secret" not in str(s)


def test_saved_default_compute_is_used_by_backtests(ctx):
    C.add(ctx, "dbx", "databricks-jobs", None, None, {"connection": "x", "job_id": "42"})
    ST.set_compute(ctx, backtest="dbx")
    assert ST.default_compute(ctx) == "dbx"
    # The saved default is chosen: a local workspace can't use cloud compute, which proves it didn't fall back to local.
    with pytest.raises(ValueError, match="needs a cloud workspace"):
        ops.backtest_launch(ctx, ctx.registry.names("backtest")[0], dry_run=True)
    assert ops.backtest_launch(ctx, ctx.registry.names("backtest")[0], compute="local", dry_run=True)["compute"] == "local"
    with pytest.raises(ValueError, match="can't be used for serves"):
        ST.set_compute(ctx, serve="dbx")


def test_project_home_lists_its_work(ctx):
    p = PR.create(ctx, "Outdoor gear")
    PR.assign(ctx, p["id"], [{"kind": "dataset", "name": "demo-catalog"}, {"kind": "pipeline", "name": "demo-bm25"}])
    h = PR.home(ctx, p["id"])
    assert [d["name"] for d in h["datasets"]] == ["demo-catalog"] and h["datasets"][0]["rows"]
    assert h["pipelines"][0]["name"] == "demo-bm25" and h["counts"]["datasets"] == 1 and not h["empty"]
    empty = PR.home(ctx, PR.create(ctx, "Nothing yet")["id"])
    assert empty["empty"] and empty["counts"]["runs"] == 0


def test_warehouse_compute_plans():
    job = Job(id="abc-1", argv=["-w", "s", "backtest", "run", "b"], label="b", env={"CST_WORKSPACE": "s"})
    d = DatabricksJobsProvider({"config": {"connection": "dbx", "job_id": "42"}}).plan(job)[0]
    assert d["call"].endswith("run-now") and d["args"]["job_id"] == 42 and d["args"]["python_params"] == job.argv
    s = SnowparkContainersProvider({"config": {"connection": "sf", "compute_pool": "CAPTAIN_POOL",
                                               "image": "/db/s/repo/captain-runner:latest"}}).plan(job)[0]
    sql = s["args"]["sql"]
    assert sql.startswith("EXECUTE JOB SERVICE IN COMPUTE POOL CAPTAIN_POOL NAME = CST_ABC_1 ASYNC = TRUE")
    with pytest.raises(ValueError, match="plain Snowflake name"):
        SnowparkContainersProvider({"config": {"connection": "sf", "compute_pool": "x; drop table t",
                                               "image": "i"}}).plan(job)


def test_services_list_says_each_connections_database(ctx):
    """Settings shows a connection's database (its mark and name), so `kind` is the engine, not the record type."""
    conns = SV.listing(ctx)["connections"]
    demo_store = next(c for c in conns if c["name"] == "demo-store")
    assert demo_store["kind"] == ctx.load_connection("demo-store").engine != "connection"
