"""`cst sim events`: synthetic shopper events for any catalog. They reproduce from a seed, have the columns a replay
and a trainer read, link back to their catalog, and everything judged on them reads SIMULATED."""
import json

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from captain_search import historic as H
from captain_search import lineage as LN
from captain_search import ops
from captain_search import project_inputs as PI
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search import retrain as RT
from captain_search.cli.main import app
from captain_search.data.datasets import load_table, snapshot
from captain_search.demo import catalog as demo_catalog
from captain_search.model.entities import DatasetKind, Evidence, Provenance
from captain_search.sim import events as SE
from captain_search.workspace import init_workspace

END = "2026-10-31"


@pytest.fixture
def ws(tmp_path):
    ctx = init_workspace("t", str(tmp_path / "store"))
    cat = snapshot(ctx, "shop-catalog", pa.Table.from_pylist(demo_catalog(240)), kind=DatasetKind.catalog,
                   provenance=Provenance(source="generated"))
    return ctx, ops.ref(cat)


def _project(ctx, cat=None):
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    if cat:
        PI.set_input(ctx, "shop", "catalog", {"from": "dataset", "ref": cat}, "snapshot")
    return "shop"


def test_same_seed_same_rows_and_another_seed_differs():
    prods = demo_catalog(200)
    a, sa = SE.generate(prods, 300, 14, 7, END)
    b, sb = SE.generate(prods, 300, 14, 7, END)
    c, _ = SE.generate(prods, 300, 14, 8, END)
    assert a == b and sa == sb
    assert a != c
    assert pa.Table.from_pylist(a).equals(pa.Table.from_pylist(b))


def test_rows_are_what_a_replay_and_a_trainer_read():
    rows, st = SE.generate(demo_catalog(200), 400, 10, 3, END)
    assert set(rows[0]) == set(SE.COLUMNS) and H.REQUIRED <= set(rows[0])
    assert st["rows"] == len(rows) and st["sessions"] == 400
    assert all(r["simulated"] is True for r in rows)
    assert all(1 <= r["position"] <= SE.PAGE for r in rows)
    assert all("2026-10-22" <= r["ts"][:10] <= END and r["ts"].endswith("Z") for r in rows)
    assert rows == sorted(rows, key=lambda r: (r["ts"], r["session_id"]))
    # a purchase was carted and a cart was clicked: the funnel only narrows
    assert all(r["added_to_cart"] or not r["purchased"] for r in rows)
    assert all(r["clicked"] or not r["added_to_cart"] for r in rows)
    assert st["clicks"] > st["add_to_carts"] >= st["purchases"] > 0
    assert len({r["session_id"] for r in rows}) <= 400 < len(rows)
    assert all(r["session_id"].startswith("s-") and r["user_id"].startswith("u-") for r in rows)
    # position bias: the first result is clicked more often than the fifth
    ctr = H.ctr_by_rank(rows, 10)
    assert ctr[0] > ctr[4]
    # traffic is head-heavy: the busiest query gets many times the median query's searches
    mix = sorted((q["weight"] for q in H.query_mix(rows)), reverse=True)
    assert mix[0] >= 5 * mix[len(mix) // 2]


def test_a_catalog_without_prices_still_generates_and_says_so():
    prods = [{k: v for k, v in p.items() if k != "price"} for p in demo_catalog(120)]
    rows, st = SE.generate(prods, 200, 5, 1, END)
    assert rows and st["price_sensitivity"] is False
    assert all(r["price"] is None for r in rows)
    assert "under" not in " ".join(r["query"] for r in rows)
    assert SE.params("c@v1", 200, 5, 1, END, st)["price_sensitivity"].startswith("off: the catalog has no prices")
    assert SE.params("c@v1", 200, 5, 1, END, {**st, "price_sensitivity": True})["price_sensitivity"] == "on"


def test_the_dataset_is_versioned_marked_simulated_and_linked_to_its_catalog(ws):
    ctx, cat = ws
    m = ops.sim_events(ctx, "shop-catalog", "sim/shop-events", 300, 14, 7, END)
    assert (m.name, m.version, m.dataset_kind) == ("sim/shop-events", 1, DatasetKind.events)
    assert m.tags == {"synthetic": "true", "label_source": "simulated"} and H.synthetic(m)
    assert m.description.startswith("SIMULATED")
    p = m.provenance.params
    assert p["generator"] == SE.GENERATOR and p["generator_version"] == SE.VERSION
    assert p["simulated"] is True and p["evidence"] == "SIMULATED"
    assert (p["catalog"], p["seed"], p["sessions"], p["days"], p["end"]) == (cat, 7, 300, 14, END)
    assert [f["name"] for f in m.schema_fields] == SE.COLUMNS
    assert load_table(ctx, m).num_rows == m.row_count == p["stats"]["rows"]
    # lineage: the catalog version is upstream of the events
    g = LN.graph(ctx)
    assert {"src": f"dataset:{cat}", "dst": "dataset:sim/shop-events@v1", "relation": "derived_from"} in [
        {k: e[k] for k in ("src", "dst", "relation")} for e in g["edges"]]
    # the same parameters make the same file again, as the next version
    m2 = ops.sim_events(ctx, cat, "sim/shop-events", 300, 14, 7, END)
    assert m2.version == 2 and m2.files[0].sha256 == m.files[0].sha256
    # and a trainer is told the labels are simulated
    item = RT.resolve_inputs(ctx, [f"events={ops.ref(m)}"])[0]
    assert (item["kind"], item["role"], item["label_source"]) == ("events", "events", "simulated")


def test_it_refuses_what_is_not_a_catalog_and_bad_sizes(ws):
    ctx, cat = ws
    ev = ops.sim_events(ctx, cat, "sim/shop-events", 200, 7, 1, END)
    with pytest.raises(ValueError, match="isn't a catalog"):
        ops.sim_events(ctx, ops.ref(ev), "sim/again", 200, 7, 1, END)
    with pytest.raises(ValueError, match="sessions is between"):
        ops.sim_events(ctx, cat, "sim/none", 0, 7, 1, END)
    with pytest.raises(ValueError, match="days is between"):
        ops.sim_events(ctx, cat, "sim/none", 100, 0, 1, END)
    with pytest.raises(ValueError, match="--end is a date"):
        ops.sim_events(ctx, cat, "sim/none", 100, 7, 1, "soon")
    assert ctx.registry.versions("dataset", "sim/none") == []


def test_a_project_without_a_catalog_is_refused(ws):
    ctx, _ = ws
    pid = _project(ctx)
    with pytest.raises(ValueError, match="choose a catalog first"):
        PI.generate_events(ctx, pid, "events", 200, 7, 1, END)
    assert PS.get(ctx, pid)["pins"].get("events") is None
    assert ctx.registry.versions("dataset", f"{pid}/synthetic-events") == []
    with pytest.raises(ValueError, match="doesn't take events"):
        PI.events_plan(ctx, pid, "catalog")


def test_a_project_on_generated_events_is_judged_as_a_replay_labelled_simulated(ws):
    ctx, cat = ws
    pid = _project(ctx, cat)
    r = PI.generate_events(ctx, pid, "events", 1500, 14, 7, END)
    assert r["simulated"] is True and r["input"]["simulated"] is True and r["dataset"] == f"{pid}/synthetic-events@v1"
    st = PS.stages(ctx, pid)
    rows = {i["slot"]: i for i in st["inputs"]}
    assert rows["events"]["pinned"] == r["dataset"] and rows["events"]["simulated"] is True
    assert rows["catalog"]["simulated"] is False
    ev = st["evaluation"]
    assert ev["method"] == "logged-clicks" and ev["ready"] is True
    assert ev["label"] == "Replay of logged searches, graded from clicks"
    assert ev["evidence"] == "SIMULATED" and ev["simulated"] is True and "simulated" in ev["text"]
    # pinning the same dataset by hand carries the mark too
    PS.pin(ctx, pid, {"events": None})
    PS.pin(ctx, pid, {"events": r["dataset"]})
    assert PS.get(ctx, pid)["inputs"]["events"]["simulated"] is True


def test_real_events_are_not_marked_simulated(ws):
    ctx, cat = ws
    pid = _project(ctx, cat)
    rows, _ = SE.generate(demo_catalog(240), 300, 7, 2, END)
    real = snapshot(ctx, "shop-logs", pa.Table.from_pylist(rows).drop_columns(["simulated"]), kind=DatasetKind.events,
                    provenance=Provenance(source="file", file="logs.parquet"))
    PI.set_input(ctx, pid, "events", {"from": "dataset", "ref": ops.ref(real)}, "snapshot")
    st = PS.stages(ctx, pid)
    assert next(i for i in st["inputs"] if i["slot"] == "events")["simulated"] is False
    assert st["evaluation"]["evidence"] == "LOGGED_CLICKS" and st["evaluation"]["simulated"] is False


def test_simulated_reaches_the_evidence_of_an_evaluation_run(ws):
    ctx, cat = ws
    ev = ops.sim_events(ctx, cat, "sim/shop-events", 2500, 14, 7, END)
    base = ops.pipeline_publish(ctx, {"name": "base", "engine": "local-bm25", "catalog": cat, "spec": {"fields": {"title": 1.0}}})
    cand = ops.pipeline_publish(ctx, {"name": "cand", "engine": "local-bm25", "catalog": cat,
                                      "spec": {"fields": {"title": 3.0, "brand": 1.5, "category": 1.0, "description": 0.7}}})
    bt = ops.backtest_plan(ctx, "replay", [ops.ref(base), ops.ref(cand)], None, None, None, "cand ranks better",
                           primary="ndcg@10_tw", mode="historic", logs=ops.ref(ev), population_size=300)
    jm, _ = ctx.registry.resolve("dataset", bt.judgments)
    assert jm.judgment_source == "click" and H.synthetic(jm)
    run = ops.backtest_run(ctx, bt.name)
    assert run.arms and all(a.evidence == Evidence.SIMULATED for a in run.arms)


def test_a_cut_of_simulated_events_after_a_date_stays_simulated(ws):
    ctx, cat = ws
    ev = ops.sim_events(ctx, cat, "sim/shop-events", 400, 14, 7, END)
    later = PS._after(ctx, ops.ref(ev), "2026-10-25", "shop/logs-after")
    lm, _ = ctx.registry.resolve("dataset", later)
    assert H.synthetic(lm) and 0 < lm.row_count < ev.row_count
    jm = H.judgments_from_logs(ctx, later, "shop/judgments", min_impressions=1)
    assert H.synthetic(jm)


def test_cli_generates_and_prints_the_simulated_mark(ws):
    ctx, cat = ws
    run = CliRunner()
    r = run.invoke(app, ["-w", "t", "sim", "events", "--catalog", cat, "--name", "sim/cli-events", "--sessions", "200",
                         "--days", "7", "--seed", "3", "--end", END])
    assert r.exit_code == 0, r.output
    assert "sim/cli-events@v1 [SIMULATED]" in r.output and "No real shopper" in r.output
    r = run.invoke(app, ["-w", "t", "--dry-run", "--json", "sim", "events", "--catalog", cat, "--name", "sim/dry", "--sessions", "100"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["tags"]["synthetic"] == "true" and ctx.registry.versions("dataset", "sim/dry") == []
    pid = _project(ctx, cat)
    r = run.invoke(app, ["-w", "t", "projects", "generate-events", pid, "--sessions", "200", "--days", "7", "--end", END])
    assert r.exit_code == 0 and "[SIMULATED]" in r.output, r.output
    r = run.invoke(app, ["-w", "t", "projects", "stages", pid])
    assert f"{pid}/synthetic-events@v1 [SIMULATED]" in r.output
    r = run.invoke(app, ["-w", "t", "sim", "events", "--catalog", "sim/cli-events", "--name", "sim/x"])
    assert r.exit_code != 0 and "isn't a catalog" in r.output


def test_route_runs_as_a_job_and_refuses_without_a_catalog(ws, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from captain_search import server

    ctx, cat = ws
    monkeypatch.setattr(server, "ctx", lambda: ctx)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    pid = _project(ctx)
    r = c.post(f"/api/projects/{pid}/inputs/events/generate", headers=h, json={"sessions": 200, "days": 7})
    assert r.status_code == 400 and "choose a catalog first" in r.json()["error"]
    PI.set_input(ctx, pid, "catalog", {"from": "dataset", "ref": cat}, "snapshot")
    assert c.post(f"/api/projects/{pid}/inputs/events/generate", headers=h, json={"sessions": 0}).status_code == 400
    assert c.post(f"/api/projects/{pid}/inputs/catalog/generate", headers=h, json={}).status_code == 400
    assert c.post(f"/api/projects/{pid}/inputs/nope/generate", headers=h, json={}).status_code in (400, 404)
    r = c.post(f"/api/projects/{pid}/inputs/events/generate", headers=h, json={"sessions": 200, "days": 7, "end": END}).json()
    assert r["simulated"] is True and r["catalog"] == cat and r["dataset"] == f"{pid}/synthetic-events"
    for _ in range(200):
        j = c.get(f"/api/jobs/{r['job']}", headers=h).json()
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert j["status"] == "done", j
    assert j["result"]["dataset"] == f"{pid}/synthetic-events@v1" and j["result"]["simulated"] is True
    st = c.get(f"/api/projects/{pid}/stages", headers=h).json()
    assert next(i for i in st["inputs"] if i["slot"] == "events")["simulated"] is True
    assert st["evaluation"]["evidence"] == "SIMULATED"
