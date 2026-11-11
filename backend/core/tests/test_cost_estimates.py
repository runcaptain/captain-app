"""Cost estimates before a run, what a run cost after, and the starter ranker trained on a compute target.

Prices: the live path is exercised with a faked AWS pricing client; with no pricing client the bundled table answers
and says its date. A provider Captain has never run a real job on gets no number.
"""
import datetime as dt
import json

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from captain_search import compute as C
from captain_search import cost_estimates as CE
from captain_search import demo as D
from captain_search import jobs as J
from captain_search import ops
from captain_search import price_table as PT
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search import settings as ST
from captain_search import starter as STR
from captain_search.cli.main import app
from captain_search.data.datasets import snapshot
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.workspace import init_workspace

from test_compute_providers import TARGETS, World, cloud_ctx, world  # noqa: F401  (fixtures)
from test_starter_ranker import _fake, _logs


@pytest.fixture(autouse=True)
def fresh_prices():
    CE._price_cache.clear()
    yield
    CE._price_cache.clear()


SAGEMAKER = ("aws", "us-west-1", {"image": "img", "execution_role_arn": "arn:aws:iam::1:role/r"})


def _add(ctx, kind, name=None):
    prov, region, cfg = SAGEMAKER if kind == "aws-sagemaker" else TARGETS[kind]
    return C.add(ctx, name or kind.replace("-", "")[:12], kind, "p" if prov else None, region, cfg)


def _product(usagetype, usd, **attrs):
    return json.dumps({"product": {"attributes": {"usagetype": usagetype, **attrs}},
                       "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"USD": str(usd)}}}}}}})


class FakePricing:
    """The AWS Price List API's shape: JSON strings in PriceList, filtered by region."""

    def __init__(self):
        self.asked = []

    def get_products(self, ServiceCode, Filters, NextToken=None):
        self.asked.append((ServiceCode, {f["Field"]: f["Value"] for f in Filters}))
        if ServiceCode == "AmazonECS":
            return {"PriceList": [_product("USW1-Fargate-vCPU-Hours:perCPU", 0.05), _product("USW1-Fargate-GB-Hours", 0.006),
                                  _product("USW1-Fargate-ARM-vCPU-Hours:perCPU", 0.01), _product("USW1-Fargate-Windows-GB-Hours", 0.9)]}
        return {"PriceList": [_product("USW1-Train:ml.m5.large", 9.0, component="Training"),
                              _product("USW1-Host:ml.m5.large", 0.2, component="Hosting")]}


# ---- this computer ---------------------------------------------------------------------------------------------


def test_this_computer_is_free_and_says_so(tmp_path):
    ctx = init_workspace("w", str(tmp_path / "s"))
    for kind in ("training", "evaluation", "synthetic-events", "staging", "workflow"):
        e = CE.estimate(ctx, kind, "local", {"rows": 100000, "queries": 200, "sessions": 20000})
        assert e["free"] and e["low"] == 0 and e["high"] == 0 and e["currency"] == "USD"
        assert e["text"] == "Runs on this computer. No cloud cost."
        assert e["basis"][0] == e["text"] and not e["confirm"]
    assert CE.estimate(ctx, "training", None, {})["free"], "no place named means this computer"
    with pytest.raises(ValueError, match="unknown kind"):
        CE.estimate(ctx, "mining", "local", {})


# ---- prices ------------------------------------------------------------------------------------------------------


def test_bundled_table_answers_with_its_date_when_live_prices_cannot_be_read(world, cloud_ctx):
    t = _add(cloud_ctx, "aws-batch")  # the fake session has no pricing client, as a login without that permission
    e = CE.estimate(cloud_ctx, "training", t["name"], {"how": "starter", "rows": 300000})
    p = PT.FARGATE["us-west-1"]
    hourly = 4 * p["vcpu_hour"] + 16 * p["gb_hour"]
    assert e["available"] and not e["free"] and e["unit"] == "run"
    assert e["price_source"] == "bundled" and e["priced_as_of"] == PT.AWS_PRICED_AS_OF
    assert e["hourly"] == pytest.approx(hourly, abs=1e-6)
    assert f"Estimate, prices as of {PT.AWS_PRICED_AS_OF}." in e["text"]
    assert any("bundled AWS table" in b and PT.AWS_PRICED_AS_OF in b for b in e["basis"])
    assert any("4 vCPU, 16 GB on AWS Fargate" in b for b in e["basis"])


def test_estimator_math_is_machine_price_times_time(world, cloud_ctx):
    t = _add(cloud_ctx, "aws-batch")
    e = CE.estimate(cloud_ctx, "training", t["name"], {"how": "starter", "rows": 3_000_000})
    fixed, per, _ = CE.ROUGH[("training", "starter")]
    mid = fixed + 3_000_000 * per
    lo_s, hi_s = CE.CLOUD_START_S + mid * CE.ROUGH_SPREAD[0], CE.CLOUD_START_S + mid * CE.ROUGH_SPREAD[1]
    assert e["minutes_low"] == pytest.approx(lo_s / 60, abs=0.06) and e["minutes_high"] == pytest.approx(hi_s / 60, abs=0.06)
    assert e["low"] == pytest.approx(e["hourly"] * lo_s / 3600, abs=1e-4)
    assert e["high"] == pytest.approx(e["hourly"] * hi_s / 3600, abs=1e-4)
    assert e["low"] < e["high"] and e["confidence"] == "rough"
    assert e["text"].startswith(CE.money_range(e["low"], e["high"])) and f"on {t['name']}" in e["text"]
    # a tiny job still pays Fargate's one-minute minimum
    small = CE.estimate(cloud_ctx, "evaluation", t["name"], {"queries": 1, "arms": 1})
    assert small["minutes_low"] >= 1.0


def test_live_prices_come_from_the_providers_price_list(world, cloud_ctx, monkeypatch):
    fp = FakePricing()
    monkeypatch.setattr(CE, "_pricing_client", lambda target: fp)
    t = _add(cloud_ctx, "aws-batch")
    e = CE.estimate(cloud_ctx, "evaluation", t["name"], {"queries": 500, "arms": 2})
    assert e["price_source"] == "live" and e["priced_as_of"] == dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert e["hourly"] == pytest.approx(4 * 0.05 + 16 * 0.006)  # the x86 Linux rows, not ARM or Windows
    assert ("AmazonECS", {"regionCode": "us-west-1"}) in fp.asked
    assert any("AWS's own price list" in b for b in e["basis"])
    CE.estimate(cloud_ctx, "evaluation", t["name"], {"queries": 900, "arms": 2})
    assert len(fp.asked) == 1, "the price list is read once, then kept for a while"
    sm = _add(cloud_ctx, "aws-sagemaker")
    s = CE.estimate(cloud_ctx, "staging", sm["name"], {"instances": 2, "instance_type": "ml.m5.large"})
    assert s["unit"] == "hour" and s["low"] == s["high"] == pytest.approx(0.4) and s["price_source"] == "live"
    assert "an hour" in s["text"] and "a day" in s["text"]


def test_staging_per_hour_from_the_bundled_table_and_the_target_record(world, cloud_ctx):
    sm = _add(cloud_ctx, "aws-sagemaker")
    s = CE.estimate(cloud_ctx, "staging", sm, {})  # the target record itself, as the serving code holds it
    inst = sm["config"].get("instance_type") or "ml.m5.large"
    assert s["low"] == pytest.approx(PT.SAGEMAKER_HOSTING["us-west-1"][inst], abs=1e-4) and s["unit"] == "hour"
    assert s["priced_as_of"] == PT.AWS_PRICED_AS_OF


@pytest.mark.parametrize("kind", [k for k in TARGETS if not k.startswith("aws-")])
def test_untested_providers_get_no_number(kind, world, cloud_ctx):
    t = _add(cloud_ctx, kind)
    label = C.kind_map()[kind]["label"]
    for what in ("training", "evaluation", "staging"):
        e = CE.estimate(cloud_ctx, what, t["name"], {"rows": 100000, "queries": 100})
        assert e["low"] is None and e["high"] is None and not e["available"] and not e["free"]
        assert e["text"] == f"No estimate for {label} yet."
        assert not e["confirm"]


def test_a_region_with_no_price_gets_no_number(world, cloud_ctx):
    prov, _, cfg = TARGETS["aws-batch"]
    t = C.add(cloud_ctx, "far", "aws-batch", "p", "xx-nowhere-9", cfg)
    e = CE.estimate(cloud_ctx, "training", t["name"], {"rows": 1000})
    assert e["low"] is None and "no price" in e["text"]


def test_own_training_command_shows_the_hourly_price_until_it_has_run(world, cloud_ctx):
    t = _add(cloud_ctx, "aws-batch")
    e = CE.estimate(cloud_ctx, "training", t["name"], {"how": "command"})
    assert e["unit"] == "hour" and e["low"] == e["high"] == pytest.approx(e["hourly"], abs=1e-4)
    assert "can't tell how long your own training command runs" in e["text"]
    assert e["confirm"] == (e["hourly"] * 24 >= 5.0), "with no length, a day at that rate is what is asked about"
    ST.set_costs(cloud_ctx, 0)
    assert CE.estimate(cloud_ctx, "training", t["name"], {"how": "command"})["confirm"], "0 asks every time"


# ---- warehouses and LLM judging ----------------------------------------------------------------------------------


def test_warehouse_snapshots_are_billed_by_the_warehouse(tmp_path, monkeypatch):
    from captain_search.model.entities import Connection

    ctx = init_workspace("w", str(tmp_path / "s"))
    e = CE.estimate(ctx, "snapshot", "no-such-connection", {"sql": "select 1"})
    assert e["low"] is None and e["text"] == "Billed by your warehouse. Captain can't estimate it."
    # BigQuery can say what a query scans without running it: the provider's own figure is shown, with no price on it.
    monkeypatch.setattr(type(ctx), "load_connection", lambda self, name: Connection(name=name, engine="bigquery", url="bigquery://p", org="o", workspace="w", created_by="t"))
    monkeypatch.setattr(CE, "_bigquery_dry_run", lambda conn, sql: 1_200_000_000)
    b = CE.estimate(ctx, "snapshot", "bq", {"sql": "select * from events"})
    assert b["bytes_scanned"] == 1_200_000_000 and b["low"] is None
    assert b["text"] == "BigQuery says this query scans 1.20 GB. It bills by bytes scanned, at your own rate."
    assert CE.estimate(ctx, "snapshot", "bq", {})["text"].startswith("Billed by your warehouse")


def test_llm_judging_is_priced_from_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("CST_MODEL", raising=False)
    ctx = init_workspace("w", str(tmp_path / "s"))
    e = CE.estimate(ctx, "judging", None, {"queries": 100, "depth": 10, "arms": 2})
    t = CE.judge_tokens(2000)
    assert t["calls"] == 100 and e["tokens"] == t
    pin, pout = PT.LLM["claude-sonnet-5-5"]
    assert e["low"] == pytest.approx((t["input_low"] * pin + t["output"] * pout) / 1e6, abs=1e-4)
    assert e["high"] == pytest.approx((t["input_high"] * pin + t["output"] * pout) / 1e6, abs=1e-4)
    assert e["priced_as_of"] == PT.LLM_PRICED_AS_OF and "2,000 pairs" in e["text"]
    assert CE.estimate(ctx, "judging", "some-other-model", {"pairs": 50})["text"] == "No estimate for some-other-model yet."
    assert CE.estimate(ctx, "judging", "anthropic/claude-sonnet-5.5", {"pairs": 2000})["low"] == e["low"]


# ---- the threshold -----------------------------------------------------------------------------------------------


def test_confirm_above_the_threshold_which_settings_can_change(world, cloud_ctx):
    t = _add(cloud_ctx, "aws-batch")
    big = {"how": "starter", "rows": 4_000_000_000}
    assert CE.threshold(cloud_ctx) == 5.0
    e = CE.estimate(cloud_ctx, "training", t["name"], big)
    assert e["high"] > 5 and e["confirm"] and e["threshold"] == 5.0
    small = CE.estimate(cloud_ctx, "training", t["name"], {"how": "starter", "rows": 1000})
    assert small["high"] < 5 and not small["confirm"]
    ST.set_costs(cloud_ctx, 0.001)
    assert CE.estimate(cloud_ctx, "training", t["name"], {"how": "starter", "rows": 1000})["confirm"]
    ST.set_costs(cloud_ctx, 1e5)
    assert not CE.estimate(cloud_ctx, "training", t["name"], big)["confirm"]
    with pytest.raises(ValueError):
        ST.set_costs(cloud_ctx, "lots")
    assert ST.get(cloud_ctx)["compute"]["backtest"] == "local", "the other settings are kept"


# ---- actuals -----------------------------------------------------------------------------------------------------


def test_a_finished_job_records_its_time_machine_and_cost(world, cloud_ctx):
    t = _add(cloud_ctx, "aws-ecs")
    rec = C.submit_job(cloud_ctx, t, ["backtest", "run", "x"], label="x", kind="backtest",
                       meta={"project": "shop", "size": {"queries": 100, "arms": 2}})
    assert J.show(cloud_ctx, rec["id"]).get("cost") is None, "nothing to price while it runs"
    world.state = "succeeded"
    done = J.show(cloud_ctx, rec["id"])
    p = PT.FARGATE["us-west-1"]
    hourly = 4 * p["vcpu_hour"] + 16 * p["gb_hour"]
    c = done["cost"]
    assert c["seconds"] == 95 and c["usd"] == pytest.approx(hourly * 95 / 3600, abs=1e-4)
    assert c["machine"] == "4 vCPU, 16 GB on AWS Fargate" and c["priced_as_of"] == PT.AWS_PRICED_AS_OF
    assert c["usd"] < 0.01 and c["text"] == f"Cost under $0.01 (2 min on {t['name']})"
    assert CE.cost_of(cloud_ctx, t, 3600)["text"] == f"Cost about {CE.money(hourly)} (60 min on {t['name']})"
    assert c["provider_usd"] is None, "AWS reports no cost for one task, and Captain doesn't invent one"
    assert J.load(cloud_ctx, rec["id"])["cost"] == c, "saved on the job record"
    row = CE.get(cloud_ctx, rec["id"])
    assert row["kind"] == "evaluation" and row["project"] == "shop" and row["units"] == 200 and row["actual"]["usd"] == c["usd"]
    # a failed job is billed for the time it ran, too
    world.state = "queued"
    rec2 = C.submit_job(cloud_ctx, t, ["backtest", "run", "y"], label="y", kind="backtest", meta={"project": "shop"})
    world.state = "failed"
    assert J.show(cloud_ctx, rec2["id"])["cost"]["state"] == "failed"


def test_running_total_per_project_and_estimates_that_learn(world, cloud_ctx):
    t = _add(cloud_ctx, "aws-batch")
    assert CE.project_total(cloud_ctx, "shop")["text"] == "Nothing in this project has run on your cloud yet."
    modal = _add(cloud_ctx, "modal")
    CE.note_actual(cloud_ctx, "job-m", modal, 60.0, kind="training", project="other")
    assert CE.project_total(cloud_ctx, "other")["text"] == "1 run in this project ran on your cloud, where Captain has no price."
    size = {"how": "starter", "rows": 1_000_000}
    first = CE.estimate(cloud_ctx, "training", t["name"], size)
    assert first["confidence"] == "rough"
    for i, secs in enumerate((400.0, 500.0, 600.0)):
        CE.note_estimate(cloud_ctx, f"job-{i}", first, kind="training", size=size, project="shop", target=t)
        CE.note_actual(cloud_ctx, f"job-{i}", t, secs)
    CE.note_actual(cloud_ctx, "run-local", C.get(cloud_ctx, "local"), 30.0, kind="training", size=size, project="shop")
    CE.note_actual(cloud_ctx, "job-other", t, 3600.0, kind="training", size=size, project="another")
    tot = CE.project_total(cloud_ctx, "shop")
    hourly = first["hourly"]
    assert tot["runs"] == 3 and tot["local_runs"] == 1
    assert tot["usd"] == pytest.approx(hourly * 1500 / 3600, abs=1e-3)
    assert tot["text"] == f"This project has cost about {CE.money(tot['usd'])} on your cloud so far (3 runs)."
    assert CE.get(cloud_ctx, "job-0")["estimate"]["text"] == first["text"], "the estimate is kept beside the actual"
    # with earlier cloud runs of the same kind, the time comes from them
    again = CE.estimate(cloud_ctx, "training", t["name"], {"how": "starter", "rows": 2_000_000})
    assert again["confidence"] == "measured"
    assert any("4 earlier runs of this kind in this workspace" in b for b in again["basis"])
    assert again["minutes_low"] > first["minutes_high"], "twice the rows of runs that took 7 to 60 minutes"


# ---- the starter ranker on a compute target ----------------------------------------------------------------------


@pytest.fixture
def shop(cloud_ctx):
    cat = snapshot(cloud_ctx, "shop-catalog", pa.Table.from_pylist(D.catalog()), kind=DatasetKind.catalog,
                   provenance=Provenance(source="generated"))
    logs, _ = _logs(cloud_ctx, cat, sessions=1500)
    PR.create(cloud_ctx, "shop")
    PS.pin(cloud_ctx, "shop", {"catalog": ops.ref(cat), "events": ops.ref(logs)})
    return cloud_ctx


def test_starter_train_on_a_compute_target_end_to_end(world, shop, monkeypatch, tmp_path):
    ctx = shop
    _fake(monkeypatch, tmp_path)
    t = _add(ctx, "aws-ecs", "cloud-train")
    dry = STR.train_on(ctx, "shop", "cloud-train", dry_run=True)
    assert dry["dry_run"] and dry["plan"][0]["call"] == "run_task" and dry["estimate"]["available"]
    assert not J.listing(ctx), "a dry run submits nothing"

    r = STR.train_on(ctx, "shop", "cloud-train", params={"rounds": 20})
    assert r["pending"] and r["job"] and r["live_id"] and "pipeline" not in r, "no version exists until the job finishes"
    assert r["estimate"]["text"] in r["text"]
    call = next(kw for name, kw in world.calls if name == "run_task")
    over = call["overrides"]["containerOverrides"][0]
    assert over["command"] == ["-w", "ws", "projects", "starter-train", "shop", "--param", "rounds=20"]
    env = {e["name"]: e["value"] for e in over["environment"]}
    assert env["CST_JOB_ID"] == r["job"] and env["CST_LIVE_ID"] == r["live_id"] and env["CST_STORAGE"].startswith("s3://")
    rec = J.load(ctx, r["job"])
    assert rec["kind"] == "starter-train" and rec["meta"]["project"] == "shop" and rec["meta"]["size"]["rows"] > 0
    assert not PS.get(ctx, "shop")["experiments"], "pending: nothing recorded yet"
    assert CE.get(ctx, r["job"])["estimate"]["text"] == r["estimate"]["text"]
    assert ctx.registry.versions("pipeline", "shop/starter-ranker") == []

    # The job's container runs the same command against the same workspace (here, on disk).
    monkeypatch.setenv("CST_JOB_ID", r["job"])
    monkeypatch.setenv("CST_LIVE_ID", r["live_id"])
    monkeypatch.setenv("CST_COMPUTE", "cloud-train")
    out = CliRunner().invoke(app, over["command"])
    assert out.exit_code == 0, out.output
    for k in ("CST_JOB_ID", "CST_LIVE_ID", "CST_COMPUTE"):
        monkeypatch.delenv(k)
    assert J.listing(ctx)[0]["id"] == r["job"] and len(J.listing(ctx)) == 1, "the job trains where it is, never submits again"

    world.state = "succeeded"
    done = STR.wait_job(ctx, "shop", r["job"], timeout_s=5)
    assert not done["pending"] and done["compute"] == "cloud-train"
    assert done["pipeline"] == "shop/starter-ranker@v2", "the trained version, not the untrained seed it started from"
    exp = PS.get(ctx, "shop")["experiments"]
    assert len(exp) == 1 and exp[0]["pipeline"] == done["pipeline"] and exp[0]["id"] == done["experiment"]
    assert exp[0]["cost"]["text"] == "Cost under $0.01 (2 min on cloud-train)"
    assert exp[0]["train_job"] == r["job"]
    assert done["cost"]["seconds"] == 95
    hist = PS.get(ctx, "shop")["history"]
    assert any(h.get("cost", "").startswith("Cost under $0.01") and "trained shop/starter-ranker@v2 on cloud-train" in h["what"] for h in hist)
    assert any("started training a starter ranker on cloud-train" in h["what"] for h in hist)
    tot = CE.project_total(ctx, "shop")
    assert tot["runs"] == 1 and tot["usd"] == done["cost"]["usd"]
    row = CE.get(ctx, r["job"])
    assert row["actual"]["usd"] == done["cost"]["usd"] and row["size"]["training_rows"] > 0, "the job's real size is kept"
    # the live run the app made for it was written by the job, through the workspace store
    from captain_search import live as LV

    head = LV.read_remote(ctx, r["live_id"])["run"]
    assert head["status"] == "done" and head["job"] == r["job"] and head["ref"] == done["pipeline"]
    # settling again changes nothing
    assert STR.settle_all(ctx, "shop") == 1 and STR.settle_all(ctx, "shop") == 0
    assert len([h for h in PS.get(ctx, "shop")["history"] if h.get("cost")]) == 1


def test_cloud_training_needs_a_cloud_workspace_and_says_so(world, tmp_path, monkeypatch):
    ctx = init_workspace("disk", str(tmp_path / "s"))
    prov, region, cfg = TARGETS["aws-ecs"]
    C.add(ctx, "cloud-train", "aws-ecs", "p", region, cfg)
    PR.create(ctx, "shop")
    with pytest.raises(ValueError, match="Cloud training needs a cloud workspace"):
        STR.train_on(ctx, "shop", "cloud-train")
    out = CliRunner().invoke(app, ["-w", "disk", "projects", "starter-train", "shop", "--compute", "cloud-train"])
    assert out.exit_code == 1 and "Cloud training needs a cloud workspace" in out.output
    assert not world.calls and not J.listing(ctx)


def test_a_failed_training_job_reports_failure_and_is_still_priced(world, shop):
    _add(shop, "aws-ecs", "cloud-train")
    r = STR.train_on(shop, "shop", "cloud-train")
    world.state = "failed"
    with pytest.raises(ValueError, match="training job .* failed"):
        STR.wait_job(shop, "shop", r["job"], timeout_s=5)
    assert J.load(shop, r["job"])["cost"]["state"] == "failed"
    assert not PS.get(shop, "shop")["experiments"]


# ---- the CLI and the route ---------------------------------------------------------------------------------------


def test_cli_and_route(world, cloud_ctx, monkeypatch):
    t = _add(cloud_ctx, "aws-batch")
    run = CliRunner()
    out = run.invoke(app, ["-w", "ws", "costs", "estimate", "evaluation", "--where", t["name"], "--size", "queries=400", "--size", "arms=2"])
    assert out.exit_code == 0, out.output
    assert "How this is estimated:" in out.output and f"prices as of {PT.AWS_PRICED_AS_OF}" in out.output
    j = run.invoke(app, ["-w", "ws", "--json", "costs", "estimate", "training", "--where", "local"])
    assert json.loads(j.output)["free"] is True
    assert run.invoke(app, ["-w", "ws", "costs", "estimate", "nonsense"]).exit_code == 1
    assert "Forms ask again above $5.00." in run.invoke(app, ["-w", "ws", "costs", "confirm-above"]).output
    assert "$12.00" in run.invoke(app, ["-w", "ws", "costs", "confirm-above", "12"]).output
    assert "no runs recorded yet" in run.invoke(app, ["-w", "ws", "costs", "runs"]).output

    from fastapi.testclient import TestClient

    from captain_search import server

    monkeypatch.setattr(server, "ctx", lambda: cloud_ctx)
    api = TestClient(server.app)
    hdr = {"Authorization": f"Bearer {server.TOKEN}"}
    r = api.post("/api/costs/estimate", json={"kind": "evaluation", "where": t["name"], "size": {"queries": 400, "arms": 2}}, headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"low", "high", "currency", "unit", "basis", "priced_as_of", "confidence", "free"} <= set(body)
    assert body["unit"] == "run" and body["basis"] and not body["free"]
    assert api.post("/api/costs/estimate", json={"kind": "nonsense"}, headers=hdr).status_code == 400
    assert api.put("/api/settings/costs", json={"confirm_above_usd": 2}, headers=hdr).json()["costs"]["confirm_above_usd"] == 2
    PR.create(cloud_ctx, "shop")
    assert api.get("/api/projects/shop/costs", headers=hdr).json()["runs"] == 0
    assert api.get("/api/costs/runs", headers=hdr).json() == []
