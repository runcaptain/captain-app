"""Projects that start from what you're building: recipes, editable slots, inputs from any source (snapshot or live),
what each run read, honest evaluation for any slot set, delete, and AI-assisted setup."""
import json
import os
import sys
import time

import duckdb
import pyarrow as pa
import pytest
import yaml

from captain_search import ops
from captain_search import project_assist as PA
from captain_search import project_inputs as PI
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search import recipes as RC
from captain_search import retrain as RT
from captain_search.data.datasets import snapshot
from captain_search.model.entities import Connection, DatasetKind, Provenance
from captain_search.workspace import init_workspace, whoami


@pytest.fixture
def ws(tmp_path):
    ctx = init_workspace("t", str(tmp_path / "store"))
    db = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table events as select * from (values ('hat', 'a', 1, true, '2026-10-01'), "
                "('hat', 'b', 2, false, '2026-10-02'), ('cap', 'b', 1, true, '2026-10-20')) "
                "t(query, product_id, position, clicked, ts)")
    con.close()
    ctx.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="wh", engine="duckdb",
                                   url=f"duckdb:///{db}"))
    snapshot(ctx, "shop-catalog", pa.table({"id": ["a", "b"], "title": ["red hat", "blue cap"]}),
             kind=DatasetKind.catalog, provenance=Provenance(source="generated"))
    return ctx, tmp_path


def _csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("query,product_id,position,clicked,ts\n" + "".join(",".join(map(str, r)) + "\n" for r in rows))


# ---- recipes and slots -------------------------------------------------------------------------


def test_four_kinds_ship_and_teams_add_their_own(ws, monkeypatch):
    ctx, tmp = ws
    ids = [r["id"] for r in RC.listing(ctx)]
    assert ids[:4] == ["search-events", "search-judgments", "recommendations", "byo-pipeline"]
    RC.save(ctx, {"id": "facets", "label": "Facet ranking", "slots": [{"id": "facets", "role": "custom"}]})
    repo = tmp / "repo" / "captain"
    (repo / "recipes").mkdir(parents=True)
    (repo / "captain.toml").write_text("")
    (repo / "recipes" / "ads.yaml").write_text("id: ads\nlabel: Sponsored results\nslots: [{id: bids, role: custom}]\n")
    (repo / "recipes" / "broken.yaml").write_text("id: Bad Id\n")
    monkeypatch.setenv("CST_PROJECT", str(repo))
    rs, errors = RC.load(ctx)
    assert rs["facets"]["source"] == "workspace" and rs["ads"]["source"] == "repo"
    assert errors and errors[0]["file"].endswith("broken.yaml")
    sj = RC.get(ctx, "search-judgments")
    assert sj["evaluation"]["holdout"] is True and any(s["role"] == "holdout" and not s["required"] for s in sj["slots"])


def test_a_project_owns_its_slots(ws):
    ctx, _ = ws
    PR.create(ctx, "Recs")
    st = PS.set_kind(ctx, "recs", "recommendations")
    assert [s["id"] for s in st["slots"]] == ["interactions", "items", "users"] and st["kind"] == "recommendations"
    st = PS.slot_add(ctx, "recs", {"label": "Promotions calendar", "role": "custom"})
    assert st["slots"][-1] == {"id": "promotions-calendar", "label": "Promotions calendar", "role": "custom",
                               "accepts": ["any"], "required": False, "why": "", "origin": "custom"}
    st = PS.slot_update(ctx, "recs", "items", {"label": "Products"})
    assert next(s for s in st["slots"] if s["id"] == "items")["label"] == "Products"
    st = PS.slot_remove(ctx, "recs", "users")
    assert "users" not in [s["id"] for s in st["slots"]]
    with pytest.raises(ValueError, match="required"):
        PS.slot_remove(ctx, "recs", "interactions")
    PS.slot_update(ctx, "recs", "interactions", {"required": False})
    PS.slot_remove(ctx, "recs", "interactions")
    stg = PS.stages(ctx, "recs")
    assert [r["slot"] for r in stg["inputs"]] == ["items", "promotions-calendar"]
    assert all(r["status"] == "empty" for r in stg["inputs"])
    assert stg["evaluation"]["method"] == "none" or stg["evaluation"]["ready"] is False


def test_an_older_project_keeps_its_five_slots_until_edited(ws):
    ctx, _ = ws
    PR.create(ctx, "Old")
    assert [s["id"] for s in PS.get(ctx, "old")["slots"]] == list(PS.SLOTS)
    PS.slot_remove(ctx, "old", "queries")
    assert [s["id"] for s in PS.get(ctx, "old")["slots"]] == ["catalog", "events", "labels", "holdout"]


# ---- inputs from sources -----------------------------------------------------------------------


def test_inputs_from_a_local_file_folder_or_dataset(ws):
    ctx, tmp = ws
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    with pytest.raises(ValueError, match="snapshot .* or live"):
        PI.set_input(ctx, "shop", "catalog", {"from": "dataset", "ref": "shop-catalog"}, None)
    r = PI.set_input(ctx, "shop", "catalog", {"from": "dataset", "ref": "shop-catalog"}, "snapshot")
    assert r["input"]["ref"] == "shop-catalog@v1" and r["input"]["rows"] == 2
    _csv(tmp / "logs" / "a.csv", [("hat", "a", 1, "true", "2026-10-01")])
    _csv(tmp / "logs" / "b.csv", [("cap", "b", 1, "true", "2026-10-20")])
    snap = PI.set_input(ctx, "shop", "events", {"from": "local", "path": str(tmp / "logs")}, "snapshot")["input"]
    m = ctx.registry.resolve("dataset", snap["ref"])[0]
    assert m.row_count == 2 and m.dataset_kind == DatasetKind.events and not m.external  # copied
    live = PI.set_input(ctx, "shop", "events", {"from": "local", "path": str(tmp / "logs")}, "live")["input"]
    lm = ctx.registry.resolve("dataset", live["ref"])[0]
    assert lm.external and live["rows"] == 2 and live["fingerprint"].startswith("sha256:")  # by reference
    assert PR.Resolver(ctx).of("dataset", lm.name) == "shop"
    with pytest.raises(ValueError, match="takes catalog"):
        PI.set_input(ctx, "shop", "catalog", {"from": "local", "path": str(tmp / "logs" / "a.csv")}, "snapshot",
                     kind="events")


def test_connection_inputs_snapshot_or_live(ws):
    ctx, _ = ws
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    s = PI.set_input(ctx, "shop", "events", {"from": "connection", "connection": "wh", "table": "events"}, "snapshot")
    m = ctx.registry.resolve("dataset", s["input"]["ref"])[0]
    assert m.row_count == 3 and m.provenance.sql == "select * from events"
    live = PI.set_input(ctx, "shop", "events", {"from": "connection", "connection": "wh",
                                                "sql": "select * from events where clicked"}, "live")["input"]
    assert live["ref"] is None and live["rows"] == 2 and PS.get(ctx, "shop")["pins"]["events"] is None
    with pytest.raises(ValueError, match="isn't a table name"):
        PI.query_of(ctx, {"from": "connection", "connection": "wh", "table": "events; drop table x"})
    ctx.save_connection(Connection(org="captain", workspace="t", created_by=whoami(), name="ph", engine="posthog",
                                   url="posthog+https://:key@eu.posthog.com/1"))
    q = PI.query_of(ctx, {"from": "connection", "connection": "ph", "event": {"name": "search", "days": 7}})
    assert "event = 'search'" in q and "interval 7 day" in q
    with pytest.raises(ValueError, match="event name"):
        PI.query_of(ctx, {"from": "connection", "connection": "ph", "event": {"name": "x' or 1=1 --"}})
    src = PI.sources(ctx)
    by = {c["name"]: c for c in src["connections"]}
    assert by["ph"]["offers"] == ["event", "sql"] and by["ph"]["events_route"] == "/api/sources/ph/events"
    assert by["wh"]["offers"] == ["table", "sql"] and any(d["name"] == "shop-catalog" for d in src["datasets"])


def test_each_run_records_what_it_read_and_live_inputs_are_read_again(ws):
    ctx, tmp = ws
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    PI.set_input(ctx, "shop", "catalog", {"from": "dataset", "ref": "shop-catalog"}, "live")
    _csv(tmp / "logs" / "a.csv", [("hat", "a", 1, "true", "2026-10-01")])
    PI.set_input(ctx, "shop", "events", {"from": "local", "path": str(tmp / "logs")}, "live", as_of="2026-10-15")
    PI.set_input(ctx, "shop", "queries", {"from": "connection", "connection": "wh",
                                          "sql": "select distinct query from events"}, "live")
    first = {i["slot"]: i for i in PI.run_inputs(ctx, "shop")}
    assert not any(i["changed_since_pin"] for i in first.values())
    assert first["queries"]["ref"] is None and first["queries"]["rows"] == 2 and first["queries"]["source"]["connection"] == "wh"
    time.sleep(0.01)
    _csv(tmp / "logs" / "b.csv", [("cap", "b", 1, "true", "2026-10-20")])
    snapshot(ctx, "shop-catalog", pa.table({"id": ["a"], "title": ["red hat"]}), kind=DatasetKind.catalog,
             provenance=Provenance(source="generated"))
    now = {i["slot"]: i for i in PI.run_inputs(ctx, "shop")}
    assert now["catalog"]["ref"] == "shop-catalog@v2" and now["catalog"]["changed_since_pin"]
    assert now["events"]["changed_since_pin"] and now["events"]["ref"] != first["events"]["ref"]
    assert now["events"]["rows"] == 2 and now["events"]["as_of"] == "2026-10-15"
    # A trainer gets all three, the live query as a source it reads itself, and the version records what it read.
    trainer = tmp / "train.py"
    trainer.write_text("import os, pathlib\n"
                       "o = pathlib.Path(os.environ['CST_OUT'])\n"
                       "(o / 'm').mkdir()\n"
                       "(o / 'm' / 'seen.json').write_text(os.environ['CST_TRAIN_INPUTS'])\n"
                       "(o / 'result.yaml').write_text('metrics:\\n  ndcg@10: 0.5\\nartifact_dir: m\\n')\n")
    ops.pipeline_publish(ctx, {"name": "shop-ltr", "engine": "local-bm25", "catalog": "shop-catalog",
                               "spec": {"train": {"command": f"{sys.executable} {trainer}"}}})
    r = RT.retrain(ctx, "shop-ltr", local_exec=True, inputs=PI.run_inputs(ctx, "shop"))
    new = ctx.registry.load("pipeline", "shop-ltr", r["version"])
    got = {i["slot"]: i for i in json.loads(ctx.registry.get_blob(next(f for f in new.files if f.path == "artifact/seen.json")))}
    assert got["queries"]["source"]["query"] == "select distinct query from events" and got["queries"]["rows"] == 2
    assert got["events"]["as_of"] == "2026-10-15" and got["events"]["files"]
    e = PS.experiment_add(ctx, "shop", r["pipeline"])
    read = {i["slot"]: i for i in e["inputs_read"]}
    assert read["catalog"]["ref"] == "shop-catalog@v2" and read["catalog"]["fingerprint"].startswith("sha256:")
    assert read["queries"]["mode"] == "live" and read["queries"]["rows"] == 2 and read["events"]["as_of"] == "2026-10-15"
    assert read["queries"]["read_at"]
    rows = {x["slot"]: x for x in PS.stages(ctx, "shop")["inputs"]}
    assert rows["queries"]["last_read"]["rows"] == 2 and rows["queries"]["last_read"]["at"]


# ---- evaluation for any slot set ----------------------------------------------------------------


def _trained(ctx, tmp, name, ndcg):
    t = tmp / f"{name}.py"
    t.write_text(f"import os, pathlib\npathlib.Path(os.environ['CST_OUT'], 'result.yaml').write_text('metrics:\\n  ndcg@10: {ndcg}\\n')\n")
    ops.pipeline_publish(ctx, {"name": name, "engine": "local-bm25", "catalog": "shop-catalog",
                               "spec": {"train": {"command": f"{sys.executable} {t}"}}})
    return RT.retrain(ctx, name, local_exec=True)["pipeline"]


def test_no_evaluation_set_says_so_and_trainer_reported_is_labelled(ws):
    ctx, tmp = ws
    PR.create(ctx, "Recs")
    PS.set_kind(ctx, "recs", "byo-pipeline")
    ev = PS.stages(ctx, "recs")["evaluation"]
    assert ev["method"] == "none" and ev["text"].startswith("No evaluation set yet")
    assert PS.stages(ctx, "recs")["next"]["action"] == "new-experiment"
    a, b = _trained(ctx, tmp, "rec-a", 0.30), _trained(ctx, tmp, "rec-b", 0.34)
    PS.experiment_add(ctx, "recs", a)
    e = PS.experiment_add(ctx, "recs", b)
    PS.set_champion(ctx, "recs", a)
    r = PS.experiment_backtest(ctx, "recs", e["id"])
    assert r["method"] == "trainer-reported" and r["reported"]["delta"] == pytest.approx(0.04)
    assert r["reported"]["verdict"] == "reports a higher number" and "not checked by Captain" in r["reported"]["text"]
    row = next(x for x in PS.experiments(ctx, "recs")["experiments"] if x["id"] == e["id"])
    assert row["evaluation"]["evidence"] == "TRAINER_REPORTED" and row["status"] == "evaluated"
    PS.set_evaluation(ctx, "recs", "none")
    assert PS.stages(ctx, "recs")["next"]["action"] == "add-evaluation"
    with pytest.raises(ValueError, match="No evaluation set yet"):
        PS.experiment_backtest(ctx, "recs", e["id"])


def test_logged_clicks_replays_only_searches_after_the_cutoff(ws):
    ctx, tmp = ws
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    words = ["hat", "cap", "red", "blue", "wool", "warm", "sun", "rain"]
    before = [(w, "a", 1, "true", "2026-10-01") for w in words[:4]] + [(w, "b", 2, "false", "2026-10-01") for w in words[:4]]
    after = [(w, "b", 1, "true", "2026-10-20") for w in words] + [(w, "a", 2, "false", "2026-10-20") for w in words]
    _csv(tmp / "logs" / "a.csv", before * 3 + after * 3)  # the replay grades pairs seen 3 or more times
    PI.set_input(ctx, "shop", "catalog", {"from": "dataset", "ref": "shop-catalog"}, "snapshot")
    PI.set_input(ctx, "shop", "events", {"from": "local", "path": str(tmp / "logs")}, "snapshot", kind="events")
    a, b = _trained(ctx, tmp, "ltr-a", 0.3), _trained(ctx, tmp, "ltr-b", 0.4)
    PS.experiment_add(ctx, "shop", a)
    e = PS.experiment_add(ctx, "shop", b, inputs_read=[{"slot": "events", "role": "events", "as_of": "2026-10-10"}])
    PS.set_champion(ctx, "shop", a)
    ev = PS.stages(ctx, "shop")["evaluation"]
    assert ev["method"] == "logged-clicks" and ev["evidence"] == "LOGGED_CLICKS" and ev["ready"]
    r = PS.experiment_backtest(ctx, "shop", e["id"])
    later = ctx.registry.resolve("dataset", f"shop/{e['id']}/logs-after")[0]
    assert later.row_count == 48 and later.provenance.params["after"] == "2026-10-10"
    bt = ctx.registry.resolve("backtest", r["backtest"])[0]
    assert bt.mode == "historic" and bt.logs.startswith(f"shop/{e['id']}/logs-after") and bt.tags["evaluation"] == "logged-clicks"
    assert r["eval_inputs"][0]["as_of"] == "2026-10-10" and r["run"]


# ---- delete --------------------------------------------------------------------------------------


def test_delete_keeps_the_data_and_waits_for_live_handoffs(ws):
    ctx, tmp = ws
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    a = _trained(ctx, tmp, "shop-a", 0.3)
    PS.experiment_add(ctx, "shop", a)
    PS.set_champion(ctx, "shop", a)
    ctx.store.put_new("handoffs/h1.yaml", yaml.safe_dump({"id": "h1", "pipeline": a, "environment": "prod",
                                                         "created_at": "2026-11-09T00:00:00+00:00", "status": "handed-off"}).encode())
    with pytest.raises(ValueError, match="launch review"):
        PR.delete(ctx, "shop")
    r = PR.delete(ctx, "shop", force=True)
    assert r["deleted"] == "shop" and r["forced_past"][0]["id"] == "h1"
    assert "shop" not in [p["id"] for p in PR.listing(ctx)]
    assert ctx.registry.versions("pipeline", "shop-a") and ctx.registry.versions("dataset", "shop-catalog")
    PR.create(ctx, "Shop")  # the name is free again, and starts clean
    assert PS.get(ctx, "shop")["experiments"] == [] and PS.get(ctx, "shop")["kind"] is None


# ---- assisted setup -------------------------------------------------------------------------------


def test_an_agent_proposes_and_a_person_accepts(ws, monkeypatch, tmp_path):
    ctx, tmp = ws
    PR.create(ctx, "Shop")
    PS.set_kind(ctx, "shop", "search-events")
    a = PA.assist(ctx, "shop", ask="use the warehouse")
    assert "wh (" in a["prompt"] and "cst projects propose shop --file proposal.yaml --assist " + a["assist"] in a["prompt"]
    assert "shop-catalog@v1" in a["prompt"] and "duckdb:///" not in a["prompt"]  # names, never connection urls
    (tmp_path / "train.py").write_text("print('train')\n")
    prop = {"note": "events are in the warehouse",
            "slots": [{"slot": "catalog", "source": {"from": "dataset", "ref": "shop-catalog@v1"}, "why": "the only catalog"},
                      {"slot": "events", "source": {"from": "connection", "connection": "wh", "table": "events"},
                       "mode": "live", "why": "one row per impression"},
                      {"slot": "sessions", "add": True, "label": "Sessions", "role": "custom", "why": "for features"}],
            "remove_slots": ["queries"],
            "trainer": {"command": "python train.py", "script": {"path": "train.py", "file": "train.py"}, "why": "LTR"},
            "evaluation": {"method": "logged-clicks"}}
    monkeypatch.setenv("CST_AGENT", "claude")
    p = PA.propose(ctx, "shop", prop, a["assist"], base_dir=str(tmp_path))
    assert p["agent"] == "claude" and p["status"] == "proposed" and p["trainer"]["script"]["bytes"] == 15
    assert PS.get(ctx, "shop")["pins"]["catalog"] is None  # nothing applied yet
    with pytest.raises(ValueError, match="only when the person asked"):
        PA.accept(ctx, "shop", p["id"])
    with pytest.raises(ValueError, match="no slot 'nope'"):
        PA.propose(ctx, "shop", {"slots": [{"slot": "nope"}]})
    with pytest.raises(Exception, match="no connection"):
        PA.propose(ctx, "shop", {"slots": [{"slot": "events", "source": {"from": "connection", "connection": "x", "table": "t"}}]})
    monkeypatch.delenv("CST_AGENT")
    r = PA.accept(ctx, "shop", p["id"])
    st = r["state"]
    assert r["applied"]["pinned"] == [{"slot": "catalog", "ref": "shop-catalog@v1"}] and r["applied"]["added"] == ["sessions"]
    assert r["applied"]["removed"] == ["queries"] and "queries" not in [s["id"] for s in st["slots"]]
    assert r["to_connect"] == [{"slot": "events", "source": prop["slots"][1]["source"], "suggested_mode": "live",
                                "why": "one row per impression"}]
    assert st["trainer_suggestion"]["command"] == "python train.py" and st["evaluation"]["method"] == "logged-clicks"
    assert ctx.store.get(p["trainer"]["script"]["key"]) == b"print('train')\n"
    assert PA.script(ctx, "shop", p["id"])["text"] == "print('train')\n"
    assert PA.listing(ctx, "shop")[0]["status"] == "accepted"
    with pytest.raises(ValueError, match="already accepted"):
        PA.reject(ctx, "shop", p["id"])


# ---- routes ---------------------------------------------------------------------------------------


def test_routes(ws, monkeypatch):
    from fastapi.testclient import TestClient

    from captain_search import server

    ctx, tmp = ws
    monkeypatch.setattr(server, "ctx", lambda: ctx)
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert [r["id"] for r in c.get("/api/recipes", headers=h).json()["recipes"]][:4] == [
        "search-events", "search-judgments", "recommendations", "byo-pipeline"]
    assert c.post("/api/projects", headers=h, json={"name": "Bad", "kind": "nope"}).status_code == 400
    assert c.post("/api/projects", headers=h, json={"name": "Shop", "kind": "search-events"}).json()["kind"] == "search-events"
    st = c.post("/api/projects/shop/slots", headers=h, json={"label": "Promos"}).json()
    assert st["slots"][-1]["id"] == "promos"
    assert c.delete("/api/projects/shop/slots/catalog", headers=h).status_code == 400
    assert c.post("/api/projects/shop/inputs/catalog", headers=h, json={"source": {"from": "dataset", "ref": "shop-catalog"}}).status_code == 400
    r = c.post("/api/projects/shop/inputs/catalog", headers=h,
               json={"source": {"from": "dataset", "ref": "shop-catalog"}, "mode": "snapshot"}).json()
    assert r["job"] is None and r["input"]["ref"] == "shop-catalog@v1"
    r = c.post("/api/projects/shop/inputs/events", headers=h,
               json={"source": {"from": "connection", "connection": "wh", "table": "events"}, "mode": "snapshot"}).json()
    assert r["job"] and r["input"] is None
    for _ in range(200):
        if server.JOBS[r["job"]]["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert server.JOBS[r["job"]]["status"] == "done", server.JOBS[r["job"]]
    assert server.JOBS[r["job"]]["result"]["input"]["rows"] == 3
    stg = c.get("/api/projects/shop/stages", headers=h).json()
    assert {x["slot"]: x["status"] for x in stg["inputs"]}["events"] == "pinned" and stg["evaluation"]["method"] == "logged-clicks"
    # a catalog and no baseline yet: the first step is the keyword baseline, which needs no training
    assert stg["next"]["action"] in ("baseline-start", "set-input")
    assert c.put("/api/projects/shop/evaluation", headers=h, json={"method": "nope"}).status_code == 400
    assert {x["name"] for x in c.get("/api/sources", headers=h).json()["connections"]} == {"wh"}
    a = c.post("/api/projects/shop/assist", headers=h, json={}).json()
    assert a["assist"].startswith("assist-") and "cst projects propose shop" in a["prompt"]
    assert c.get("/api/projects/shop/proposals", headers=h).json() == []
    assert c.delete("/api/projects/shop/inputs/events", headers=h).json()["pins"]["events"] is None
    d = c.delete("/api/projects/shop", headers=h).json()
    assert d["deleted"] == "shop" and d["kept"]["datasets"] >= 1
    assert c.get("/api/projects/shop/state", headers=h).status_code == 404


def test_cli_new_input_and_delete(ws):
    from typer.testing import CliRunner

    from captain_search.cli.main import app

    ctx, _ = ws
    run = CliRunner()
    j = lambda *a: run.invoke(app, ["--json", "-w", "t", *a])
    r = j("projects", "new", "Shop", "--kind", "search-judgments")
    assert r.exit_code == 0, r.output and json.loads(r.output)["slots"]
    r = j("projects", "input", "shop", "catalog", "--dataset", "shop-catalog")
    assert r.exit_code != 0 and "--mode" in r.output
    r = j("projects", "input", "shop", "catalog", "--dataset", "shop-catalog", "--mode", "snapshot")
    assert r.exit_code == 0, r.output
    r = j("projects", "slot-add", "shop", "Promotions", "--role", "custom")
    assert r.exit_code == 0, r.output
    r = j("projects", "delete", "shop")
    assert r.exit_code != 0 and "--yes" in r.output
    r = j("projects", "delete", "shop", "--yes")
    assert r.exit_code == 0, r.output and json.loads(r.output)["deleted"] == "shop"
