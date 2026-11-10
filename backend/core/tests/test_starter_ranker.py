"""The path for a project with no ranker yet: a keyword baseline, a leak-checked training set, the starter trainer,
and a trained candidate the project's own evaluation can judge."""
import json
import re
import sys
import tomllib
from pathlib import Path

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from captain_search import demo as D
from captain_search import features as F
from captain_search import historic as H
from captain_search import ops
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search import starter as ST
from captain_search import trainers as TR
from captain_search import training_set as TS
from captain_search.cli.main import app
from captain_search.data.datasets import snapshot, to_parquet
from captain_search.engines import artifact_reader, make_engine
from captain_search.features import model as FM
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.workspace import init_workspace

QUERIES = [t + s for t in D.TYPES for s in ("", " " + D.COLORS[0], " under 50", " waterproof")]

# A trainer with no training library: one split that puts cheaper products first. It follows the contract the real one follows.
FAKE_TRAINER = r'''
import json, os, pathlib
out = pathlib.Path(os.environ["CST_OUT"])
ts = next(i for i in json.loads(os.environ["CST_TRAIN_INPUTS"]) if i["role"] == "training")
uri = next(f["uri"] for f in ts["files"] if f["path"] == "features.json")
bundle = pathlib.Path(uri.replace("file://", "")).read_bytes()
names = json.loads(bundle)["names"]
tree = {"split_feature": names.index("price_log"), "threshold": 4.0, "decision_type": "<=", "default_left": True,
        "missing_type": "None", "left_child": {"leaf_value": 1.0}, "right_child": {"leaf_value": 0.0}}
(out / "model").mkdir()
(out / "model" / "model.json").write_text(json.dumps({"feature_names": names, "tree_info": [{"tree_structure": tree}]}))
(out / "model" / "features.json").write_bytes(bundle)
print(json.dumps({"type": "metric", "name": "ndcg@10", "value": 0.5, "arm": "validation", "step": 1}), flush=True)
(out / "result.yaml").write_text(json.dumps({"metrics": {"ndcg@10": 0.5}, "artifact_dir": "model",
    "spec": {"rerank": {"model": "model.json", "features": "features.json", "top_k": 50}}}))
'''


def _logs(ctx, cat, name="logs/sessions", sessions=6000, synthetic=True):
    prods = ops.products_of(ctx, ops.ref(cat))
    eng = make_engine("local-bm25", {"fields": {"title": 1.0}}, prods)
    rows = H.make_synthetic_logs(prods, lambda q: eng.search(q, 10), QUERIES, sessions, 21)
    m = snapshot(ctx, name, pa.Table.from_pylist(rows), kind=DatasetKind.events, dry_run=True,
                 provenance=Provenance(source="generated"))
    e = ctx.registry.put_blob(to_parquet(pa.Table.from_pylist(rows)), "data/part-0.parquet")
    return ctx.registry.publish(m.model_copy(update={"files": [e], "tags": {"synthetic": "true"} if synthetic else {}})), rows


@pytest.fixture
def ws(tmp_path):
    """A catalog and a month of simulated search sessions, in a project with nothing else."""
    ctx = init_workspace("t", str(tmp_path / "store"))
    cat = snapshot(ctx, "shop-catalog", pa.Table.from_pylist(D.catalog()), kind=DatasetKind.catalog,
                   provenance=Provenance(source="generated"))
    logs, rows = _logs(ctx, cat)
    PR.create(ctx, "shop")
    PS.pin(ctx, "shop", {"catalog": ops.ref(cat), "events": ops.ref(logs)})
    ctx.rows = rows
    return ctx


def _fake(monkeypatch, tmp_path):
    (tmp_path / "fake_trainer.py").write_text(FAKE_TRAINER)
    monkeypatch.setattr(TR, "command", lambda name="lightgbm-ltr": f'"$CST_PYTHON" {tmp_path / "fake_trainer.py"}')


# ---- features -------------------------------------------------------------------------------------


def test_the_same_pairs_get_the_same_features_in_training_and_in_scoring(ws, monkeypatch, tmp_path):
    _fake(monkeypatch, tmp_path)
    r = ST.train(ws, "shop")
    tm, rows, _ = ops.rows_of(ws, r["training_set"])
    pm, _ = ws.registry.resolve("pipeline", r["pipeline"])
    eng = make_engine(pm.engine, pm.spec, ops.products_of(ws, pm.catalog), artifact=artifact_reader(ws, pm))
    names = eng.feat.names
    assert names == json.loads(ws.registry.get_blob(next(f for f in tm.files if f.path == "features.json")))["names"]
    checked = 0
    for row in rows[::7]:
        served = eng.feat.features(row["query"], [row["product_id"]])[0]
        assert served == [row[n] for n in names], (row["query"], row["product_id"])
        checked += 1
    assert checked > 30 and "qp_coec" in names and "price_log" in names


def test_features_follow_the_catalogs_columns():
    with_price = F.detect_spec(["id", "title", "brand", "price", "color"])
    assert with_price["price"] == "price" and with_price["attributes"] == ["color"]
    bare = F.detect_spec(["id", "name"])
    assert bare["title"] == "name" and bare["price"] is None
    names = F.feature_names(bare, popularity=False)
    assert not any(n.startswith(("price", "pop_", "qp_", "brand")) for n in names)
    with pytest.raises(ValueError, match="which column holds the product title"):
        F.detect_spec(["id", "blurb"])
    assert F.detect_spec(["id", "blurb"], title="blurb")["title"] == "blurb"


def test_tree_scorer_walks_a_dump_without_the_training_library():
    dump = {"feature_names": ["a", "b"], "tree_info": [
        {"tree_structure": {"split_feature": 0, "threshold": 1.0, "decision_type": "<=", "default_left": False,
                            "missing_type": "NaN", "left_child": {"leaf_value": -1.0},
                            "right_child": {"split_feature": 1, "threshold": 0.5, "decision_type": "<=",
                                            "default_left": True, "missing_type": "None",
                                            "left_child": {"leaf_value": 2.0}, "right_child": {"leaf_value": 5.0}}}},
        {"tree_structure": {"leaf_value": 0.25}}]}
    m = FM.TreeModel(dump)
    assert m.predict([[0.0, 9.0], [2.0, 0.1], [2.0, 0.9], [float("nan"), 0.9]]) == [-0.75, 2.25, 5.25, 5.25]
    assert m.predict([]) == []
    with pytest.raises(ValueError, match="2 features"):
        m.predict([[1.0]])
    with pytest.raises(ValueError, match="categorical"):
        FM.TreeModel({"tree_info": [{"tree_structure": {"split_feature": 0, "threshold": "1||2", "decision_type": "==",
                                                        "left_child": {"leaf_value": 0}, "right_child": {"leaf_value": 1}}}]})


# ---- the leak rules -------------------------------------------------------------------------------


def test_a_click_rate_counted_from_the_label_window_is_refused(ws):
    cut = TS.cutoffs(ws.rows)
    label_rows = [r for r in ws.rows if cut["as_of"] < r["ts"] <= cut["train_until"]]
    leaky = F.popularity_tables(label_rows)  # the planted leak: CTR from the same searches the labels come from
    with pytest.raises(F.Leak, match="gives the model the answer"):
        TS.plan(ws, "shop-catalog", events="logs/sessions", tables=leaky)
    honest = F.popularity_tables([r for r in ws.rows if r["ts"] <= cut["as_of"]])
    assert TS.plan(ws, "shop-catalog", events="logs/sessions", tables=honest)["summary"]["popularity"]


def test_a_training_query_in_the_sealed_test_set_is_refused(ws):
    with pytest.raises(F.Leak, match="in the sealed test set"):
        TS.check_holdout(["Rain Jacket", "tent"], {"rain jacket"}, "shop-queries@v2")
    qs = [{"query_id": f"q{i}", "query": q, "class": "head"} for i, q in enumerate(QUERIES[:8])]
    q = snapshot(ws, "shop-queries", pa.Table.from_pylist(qs), kind=DatasetKind.queries, provenance=Provenance(source="generated"))
    sealed = ops.queries_seal(ws, ops.ref(q), 0.5)
    held = TS.holdout_queries(ws, ops.ref(sealed))
    assert held and held < {F.norm_query(x) for x in QUERIES[:8]}
    p = TS.plan(ws, "shop-catalog", events="logs/sessions", holdout=ops.ref(sealed) + ":holdout")
    trained_on = {r["query"] for r in p["rows"]}
    assert not trained_on & held and p["summary"]["holdout_queries_dropped"] >= 1
    assert any("sealed test set holds them" in n for n in p["notes"])


def test_as_of_is_respected_and_later_events_never_reach_training(ws):
    p = TS.plan(ws, "shop-catalog", events="logs/sessions")
    s = p["summary"]
    assert s["feature_window"]["to"] <= s["as_of"] < s["label_window"]["from"]
    assert s["label_window"]["to"] <= s["train_until"] < max(r["ts"] for r in ws.rows)
    # Rewrite everything after train_until: a model that reads only what it should gets the same rows.
    changed = [dict(r, clicked=True, purchased=True) if r["ts"] > s["train_until"] else r for r in ws.rows]
    m = snapshot(ws, "logs/changed", pa.Table.from_pylist(changed), kind=DatasetKind.events, provenance=Provenance(source="generated"))
    again = TS.plan(ws, "shop-catalog", events=ops.ref(m), as_of=s["as_of"], train_until=s["train_until"], salt="x")
    first = TS.plan(ws, "shop-catalog", events="logs/sessions", salt="x")
    assert again["rows"] == first["rows"]
    # An explicit cutoff moves both windows.
    early = TS.plan(ws, "shop-catalog", events="logs/sessions", as_of="2026-10-10", train_until="2026-10-20")["summary"]
    assert early["feature_window"]["to"] == "2026-10-10" and early["label_window"] == {"from": "2026-10-11", "to": "2026-10-20", "rows": early["label_window"]["rows"]}
    with pytest.raises(ValueError, match="must be earlier"):
        TS.plan(ws, "shop-catalog", events="logs/sessions", as_of="2026-10-20", train_until="2026-10-10")


def test_events_without_a_timestamp_are_refused_in_a_sentence(ws):
    rows = [{k: v for k, v in r.items() if k != "ts"} for r in ws.rows[:500]]
    snapshot(ws, "logs/no-ts", pa.Table.from_pylist(rows), kind=DatasetKind.events, provenance=Provenance(source="generated"))
    with pytest.raises(ValueError, match="no timestamp column"):
        TS.plan(ws, "shop-catalog", events="logs/no-ts")


def test_the_training_set_is_deterministic_and_records_what_it_read(ws):
    a, pa_ = TS.build(ws, "shop/training", "shop-catalog", events="logs/sessions")
    b, _ = TS.build(ws, "shop/training", "shop-catalog", events="logs/sessions")
    assert (a.version, b.version) == (1, 2)
    assert [(f.path, f.sha256) for f in a.files] == [(f.path, f.sha256) for f in b.files]
    assert a.dataset_kind == DatasetKind.training and a.tags["synthetic"] == "true" and a.tags["label_source"] == "clicks"
    prm = a.provenance.params
    assert prm["label_source"] == "clicks" and prm["simulated"] is True and prm["as_of"] < prm["train_until"]
    assert prm["feature_window"]["to"] <= prm["as_of"] and prm["split"]["by"] == "query"
    assert {i["ref"] for i in prm["inputs"]} == {"shop-catalog@v1", "logs/sessions@v1"}
    assert all(i["fingerprint"].startswith("sha256:") for i in prm["inputs"])
    # one query never straddles the split
    by_q = {}
    for r in pa_["rows"]:
        by_q.setdefault(r["query_id"], set()).add(r["split"])
    assert all(len(v) == 1 for v in by_q.values()) and {"train", "valid"} == set().union(*by_q.values())
    # lineage: the catalog and the events are its sources
    from captain_search import lineage_index as LI

    up = {e["from"] for e in LI.Index(ws.store).upstream("dataset:shop/training@v1")}
    assert {"dataset:shop-catalog@v1", "dataset:logs/sessions@v1"} <= up


def test_real_events_are_not_marked_simulated(ws):
    cat, _ = ws.registry.resolve("dataset", "shop-catalog")
    _logs(ws, cat, "logs/real", sessions=6000, synthetic=False)
    m, p = TS.build(ws, "shop/real-training", "shop-catalog", events="logs/real", dry_run=True)
    assert not p["simulated"] and "synthetic" not in m.tags and not m.description.startswith("SIMULATED")


# ---- the baseline ---------------------------------------------------------------------------------


def test_baseline_start_makes_keyword_search_the_baseline(ws):
    assert PS.stages(ws, "shop")["next"] == {"action": "baseline-start", "text": "Set a baseline to compare against."}
    run = CliRunner().invoke(app, ["-w", "t", "--json", "projects", "baseline-start", "shop"])
    assert run.exit_code == 0, run.output
    r = json.loads(run.output)
    assert r["baseline"] == "shop/keyword-baseline@v1" and r["created"] and r["title"] == "title"
    pm = ws.registry.load("pipeline", "shop/keyword-baseline")
    assert pm.engine == "local-bm25" and pm.spec["fields"]["title"] == 3.0 and "train" not in pm.spec
    st = PS.stages(ws, "shop")
    assert st["champion"]["pinned"] == "shop/keyword-baseline@v1" and st["start"]["baseline"]["done"]
    assert st["next"] == {"action": "new-experiment", "text": "Train a starter ranker."}
    again = CliRunner().invoke(app, ["-w", "t", "projects", "baseline-start", "shop"])
    assert again.exit_code == 1 and "already the baseline" in again.output


def test_baseline_start_asks_for_the_title_column_when_it_cannot_tell(ws):
    snapshot(ws, "odd-catalog", pa.table({"id": ["a", "b"], "blurb": ["red hat", "blue hat"]}), kind=DatasetKind.catalog,
             provenance=Provenance(source="generated"))
    PR.create(ws, "odd")
    PS.pin(ws, "odd", {"catalog": "odd-catalog"})
    p = ST.baseline_plan(ws, "odd")
    assert not p["ready"] and p["ask_title"] and p["columns"] == ["id", "blurb"]
    r = ST.baseline_start(ws, "odd", title="blurb")
    assert ws.registry.load("pipeline", "odd/keyword-baseline").spec["fields"] == {"blurb": 3.0} and r["created"]
    PR.create(ws, "empty")
    with pytest.raises(ValueError, match="Choose a catalog first"):
        ST.baseline_start(ws, "empty")


# ---- training through the contract ----------------------------------------------------------------


def test_starter_plan_says_what_it_uses_and_why(ws):
    p = ST.plan(ws, "shop")
    assert p["ready"] and p["simulated"] and p["labels_from"] == "clicks"
    assert p["as_of"] < p["train_until"] and p["as_of"] in p["why"] and p["train_until"] in p["why"]
    assert [u["role"] for u in p["uses"]] == ["catalog", "events"]
    assert p["baseline"]["will_set"] and any("simulated" in w for w in p["warnings"])
    PR.create(ws, "bare")
    PS.pin(ws, "bare", {"catalog": "shop-catalog"})
    q = ST.plan(ws, "bare")
    assert not q["ready"] and q["missing"] == ["events"] and "neither" in q["text"]


def test_a_starter_candidate_is_trained_and_judged_against_the_baseline(ws, monkeypatch, tmp_path):
    """The contract round trip with a trainer that always runs, then the project's own evaluation end to end."""
    from captain_search import live as LV

    _fake(monkeypatch, tmp_path)
    lv = LV.start("training", "Starter ranker for shop", of="shop")
    r = ST.train(ws, "shop", live=lv)
    lv.end("done")
    assert r["baseline_set"] == "shop/keyword-baseline@v1" and r["simulated"] and r["metrics"] == {"ndcg@10": 0.5}
    pm, _ = ws.registry.resolve("pipeline", r["pipeline"])
    assert pm.engine == "bm25-ltr" and pm.version == 2 and pm.parents == ["shop/starter-ranker@v1"]
    assert pm.train_inputs[0]["ref"] == r["training_set"] and pm.train_inputs[0]["role"] == "training"
    assert pm.train_inputs[0]["label_source"] == "clicks" and pm.train_inputs[0]["as_of"] == r["summary"]["train_until"]
    assert {f.path for f in pm.files} >= {"artifact/model.json", "artifact/features.json"}
    events = LV.read(lv.id)["events"]
    assert [e["name"] for e in events if e["type"] == "stage"][:3] == [
        "Set keyword search as the baseline", "Build the training set", "Train the starter ranker"]
    assert any(e["type"] == "metric" and e["name"] == "ndcg@10" and e.get("step") == 1 for e in events)
    # the trained version reorders, and never finds a product the first stage didn't
    eng = make_engine(pm.engine, pm.spec, ops.products_of(ws, pm.catalog), artifact=artifact_reader(ws, pm))
    probes = ["waterproof rain jacket", "red insulated tent ultralight", "navy northpeak backpack laptop", "black cork yoga mat travel"]
    assert all(sorted(eng.search(q, 50)) == sorted(eng.retrieve(q, 50)) for q in probes)
    assert any(eng.search(q, 50) != eng.retrieve(q, 50) for q in probes)

    out = PS.experiment_backtest(ws, "shop", r["experiment"])
    assert out["method"] == "logged-clicks" and out["run"]
    after, _ = ws.registry.resolve("dataset", out["eval_inputs"][0]["ref"])
    assert after.provenance.params["after"] == r["summary"]["train_until"]  # only searches the model never trained on
    ev = PS.experiments(ws, "shop")["experiments"][0]["evaluation"]
    assert ev["evidence"] == "SIMULATED" and ev["online"] == PS.NOT_ONLINE and ev["click_grades"]
    assert ev["label_independence"]["status"] == "shared"
    assert ev["verdict"] in (PS.OWN_LABELER, PS.TOO_FEW_JUDGED) and "beats" not in ev["verdict"]
    assert ev["recall"]["metric"] == "r@50" and ev["recall"]["value"] == ev["recall"]["champion_value"]
    assert PS.stages(ws, "shop")["start"]["starter"]["done"]


def test_a_replay_with_few_judged_results_does_not_name_a_winner(ws, monkeypatch, tmp_path):
    _fake(monkeypatch, tmp_path)
    r = ST.train(ws, "shop")
    PS.experiment_backtest(ws, "shop", r["experiment"])
    run_ref = PS.get(ws, "shop")["experiments"][0]["run"]
    name, _, v = run_ref.partition("@v")
    run = ws.registry.load("backtest-run", name, int(v))
    real = ws.registry.load

    def thin(kind, n, ver=None):
        m = real(kind, n, ver)
        if kind == "backtest-run":
            arms = [a.model_copy(update={"judged_at_10": 0.2}) if a.arm == r["pipeline"] else a for a in m.arms]
            m = m.model_copy(update={"arms": arms})
        return m

    monkeypatch.setattr(ws.registry, "load", thin)
    ev = PS.experiments(ws, "shop")["experiments"][0]["evaluation"]
    assert run.arms and ev["verdict"] == PS.TOO_FEW_JUDGED and "20%" in ev["coverage_note"]


def test_the_trainer_environment_pins_match_the_extra():
    doc = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert doc["project"]["optional-dependencies"]["train-ltr"] == [TR.LIGHTGBM]
    assert TR.LIGHTGBM in TR.REQUIREMENTS and re.fullmatch(r"lightgbm==\d+\.\d+\.\d+", TR.LIGHTGBM)
    assert not any("lightgbm" in d for d in doc["project"]["dependencies"])  # never in the base install or the app
    assert TR.command() == '"$CST_PYTHON" -m captain_search.trainers lightgbm-ltr'


def test_a_failed_setup_is_one_plain_sentence(monkeypatch, tmp_path):
    monkeypatch.setattr(TR.importlib.util, "find_spec", lambda n: None)
    calls = []

    def fake_run(argv, log, env=None):
        calls.append(argv)
        if "install" in argv:
            return 1, "error: Failed to fetch: connection refused"
        Path(TR._python(TR.env_dir())).parent.mkdir(parents=True, exist_ok=True)
        Path(TR._python(TR.env_dir())).write_text("")
        return 0, ""

    monkeypatch.setattr(TR, "_run", fake_run)
    said = []
    with pytest.raises(TR.SetupFailed, match="needs the internet once"):
        TR.ensure(said.append, stage=said.append)
    assert said[0] == "Setting up the starter trainer, one time" and not Path(TR.env_dir()).exists()


def test_the_real_lightgbm_trainer_round_trip_on_the_demo_store(tmp_path, monkeypatch):
    """`cst demo seed --legacy`, then the real trainer through the contract. It checks that the contract works and
    that the app's scorer agrees with LightGBM. It makes no claim about quality."""
    lgb = pytest.importorskip("lightgbm", reason="the train-ltr extra isn't installed (pip install 'captain-search[train-ltr]'), "
                                                 "so the real LightGBM trainer can't run here")
    ctx = init_workspace("t", str(tmp_path / "store"))
    D.seed(ctx, progress=lambda *_: None)
    PR.create(ctx, "shop")
    PS.pin(ctx, "shop", {"catalog": "demo-catalog", "events": "logs/demo-search-sessions"})
    r = ST.train(ctx, "shop")
    pm, _ = ctx.registry.resolve("pipeline", r["pipeline"])
    assert r["metrics"]["trees"] >= 1 and 0 < r["metrics"]["ndcg@10"] <= 1 and r["simulated"]
    files = {f.path: f for f in pm.files}
    assert {"artifact/model.txt", "artifact/model.json", "artifact/features.json", "artifact/rank.py"} <= set(files)
    assert pm.spec["rerank"]["trainer"] == "lightgbm-ltr" and pm.train_metrics["ndcg@10"] == r["metrics"]["ndcg@10"]
    # the scorer that ships in the app gives LightGBM's own scores
    _, rows, _ = ops.rows_of(ctx, r["training_set"])
    names = json.loads(ctx.registry.get_blob(files["artifact/features.json"]))["names"]
    x = [[row[n] for n in names] for row in rows[:200]]
    booster = lgb.Booster(model_str=ctx.registry.get_blob(files["artifact/model.txt"]).decode())
    ours = FM.load(ctx.registry.get_blob(files["artifact/model.json"])).predict(x)
    assert max(abs(a - b) for a, b in zip(ours, booster.predict(x))) < 1e-9
    out = PS.experiment_backtest(ctx, "shop", r["experiment"])
    ev = PS.experiments(ctx, "shop")["experiments"][0]["evaluation"]
    assert out["run"] and ev["evidence"] == "SIMULATED" and "beats" not in (ev["verdict"] or "")
