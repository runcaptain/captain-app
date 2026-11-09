"""Projects as a product: the pin doc, stages without registry listings, and experiments judged against a
champion with label independence."""
import threading

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from captain_search import compare as CMP
from captain_search import demo, ops
from captain_search import project_state as PS
from captain_search import projects as PR
from captain_search.data.datasets import snapshot, to_parquet
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.storage.backend import Conflict
from captain_search.workspace import init_workspace


def _seeded(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def _shape(g):
    return ({n["id"] for n in g["nodes"]}, {(e["src"], e["dst"], e["relation"]) for e in g["edges"]})


def _judgments(c, name, source, model="", rows=None):
    rows = rows or [{"query_id": "q1", "product_id": "p1", "grade": 2}]
    t = pa.Table.from_pylist(rows)
    m = snapshot(c, name, t, kind=DatasetKind.judgments, dry_run=True, provenance=Provenance(source="generated"))
    m = m.model_copy(update={"judgment_source": source, "judge": {"model": model, "prompt_version": "p1"},
                             "files": [c.registry.put_blob(to_parquet(t), "data/part-0.parquet")]})
    return c.registry.publish(m)


# ---- the pin doc --------------------------------------------------------------------------------


def test_pin_doc_compare_and_swap_keeps_every_concurrent_write(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    PR.create(c, "Acme")
    refs = [ops.ref(ops.pipeline_publish(c, {"name": "ranker", "engine": "http", "spec": {"url": f"https://x/{i}"}}))
            for i in range(12)]
    errors = []

    def add(r):
        try:
            PS.experiment_add(c, "acme", r, trainer="team script")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=add, args=(r,)) for r in refs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    doc = PS.get(c, "acme")
    assert sorted(e["pipeline"] for e in doc["experiments"]) == sorted(refs)
    assert len({e["id"] for e in doc["experiments"]}) == 12
    # the champion is a compare-and-swap too: a stale "expect" writes nothing
    PS.set_champion(c, "acme", refs[0], expect=None)
    with pytest.raises(Conflict):
        PS.set_champion(c, "acme", refs[1], expect=None)
    assert PS.get(c, "acme")["champion"] == refs[0]


def test_stages_come_from_the_pin_doc_and_index_without_listing_the_registry(tmp_path):
    c = _seeded(tmp_path)
    PR.create(c, "Outdoor")
    PS.pin(c, "outdoor", {"catalog": "demo-catalog", "queries": "demo-queries@v1", "holdout": "demo-queries",
                          "labels": "demo-judgments"})
    with pytest.raises(ValueError, match="isn't sealed"):
        PS.pin(c, "outdoor", {"holdout": "demo-queries@v1"})
    # a newer catalog shows as information, not an error
    t = pa.Table.from_pylist([{"id": "1", "title": "Tent"}])
    snapshot(c, "demo-catalog", t, kind=DatasetKind.catalog, provenance=Provenance(source="generated"))

    calls = []
    real = c.store.list
    c.store.list = lambda prefix="": (calls.append(prefix), real(prefix))[1]
    st = PS.stages(c, "outdoor")
    c.store.list = real
    assert calls == ["lineage/edges/"]

    rows = {r["slot"]: r for r in st["inputs"]}
    cat = rows["catalog"]
    assert cat["pinned"] == "demo-catalog@v1" and cat["status"] == "pinned"
    assert cat["info"][0]["code"] == "newer-version" and cat["next"] == {"action": "pin", "slot": "catalog",
                                                                          "ref": "demo-catalog@v2"}
    assert rows["queries"]["info"][0]["ref"] == "demo-queries@v2"  # the sealed version is newer than v1
    # the labels were graded on the sealed queries, but the queries pin is v1
    assert any(i["code"] == "made-from-other-version" for i in rows["labels"]["info"])
    assert rows["events"]["status"] == "missing" and rows["events"]["next"] == {"action": "pin", "slot": "events"}
    assert st["experiments"]["next"] == {"action": "new-experiment"} and st["index_complete"]


# ---- experiments against the champion -----------------------------------------------------------


def test_independence_reads_the_labeler(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    _judgments(c, "llm-train", "llm", "judge-model-a")
    _judgments(c, "llm-holdout", "llm", "judge-model-a")
    _judgments(c, "llm-other", "llm", "judge-model-b")
    _judgments(c, "human-holdout", "human")
    assert PS.label_independence(c, "llm-train@v1", "llm-train@v1")["status"] == "shared"
    assert PS.label_independence(c, "llm-train@v1", "llm-holdout@v1")["status"] == "shared"
    assert PS.label_independence(c, "llm-train@v1", "llm-other@v1")["status"] == "independent"
    assert PS.label_independence(c, "llm-train@v1", "human-holdout@v1")["status"] == "independent"
    assert PS.label_independence(c, "llm", "llm-holdout@v1")["status"] == "shared"
    assert PS.label_independence(c, None, "human-holdout@v1")["status"] == "unknown"
    assert PS.verdict("win", {"status": "shared"}) == "agrees with its own labeler"
    assert PS.verdict("win", {"status": "independent"}) == "beats the champion"
    assert PS.verdict("loss", {"status": "independent"}) == "loses to the champion"


def test_an_experiment_against_the_champion_says_when_it_only_agrees_with_its_labeler(tmp_path):
    c = _seeded(tmp_path)
    PR.create(c, "Outdoor")
    PS.pin(c, "outdoor", {"holdout": "demo-queries", "labels": "demo-judgments"})
    PS.experiment_add(c, "outdoor", "demo-bm25")
    e = PS.experiment_add(c, "outdoor", "demo-tuned", labels="demo-judgments")
    with pytest.raises(ValueError, match="no champion"):
        PS.experiment_backtest(c, "outdoor", e["id"])
    PS.set_champion(c, "outdoor", "demo-bm25@v1")

    # trained on the same judgments that grade the holdout
    r = PS.experiment_backtest(c, "outdoor", e["id"])
    assert r["run"] and r["label_independence"]["status"] == "shared"
    row = next(x for x in PS.experiments(c, "outdoor")["experiments"] if x["id"] == e["id"])
    ev = row["evaluation"]
    assert row["status"] == "evaluated" and ev["against_is_champion"]
    assert ev["comparison"] in ("win", "loss", "noise") and ev["verdict"] == PS.OWN_LABELER
    assert ev["recall"]["metric"] == "r@50" and ev["online"] == PS.NOT_ONLINE
    w = CMP.compatibility(c, ev["run"])
    assert not w["trustworthy"] and "own-labeler" in {x["code"] for x in w["warnings"]}

    # human grades on the holdout: the same win now reads as a win
    _, jrows, _ = ops.rows_of(c, "demo-judgments")
    _judgments(c, "human-grades", "human", rows=[{k: r[k] for k in ("query_id", "product_id", "grade")} for r in jrows])
    PS.pin(c, "outdoor", {"labels": "human-grades"})
    r2 = PS.experiment_backtest(c, "outdoor", e["id"])
    assert r2["label_independence"]["status"] == "independent"
    ev2 = next(x for x in PS.experiments(c, "outdoor")["experiments"] if x["id"] == e["id"])["evaluation"]
    assert ev2["verdict"] == PS.VERDICTS[ev2["comparison"]]
    assert "own-labeler" not in {x["code"] for x in CMP.compatibility(c, ev2["run"])["warnings"]}
    st = PS.stages(c, "outdoor")
    assert st["champion"]["pinned"] == "demo-bm25@v1" and st["experiments"]["by_status"]["evaluated"] == 1


def test_project_routes(tmp_path, monkeypatch):
    from captain_search import server

    _seeded(tmp_path)
    monkeypatch.setattr(server, "_ctx", None)
    cl = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    cl.post("/api/projects", headers=h, json={"name": "Outdoor"})
    assert cl.post("/api/projects/outdoor/pin", headers=h, json={"holdout": "demo-queries", "labels": "demo-judgments",
                                                                  "arch": "bm25", "brand": "outdoor"}).status_code == 200
    assert cl.post("/api/projects/outdoor/pin", headers=h, json={"catalog": "demo-queries"}).status_code == 400
    e = cl.post("/api/projects/outdoor/experiments", headers=h, json={"pipeline": "demo-tuned"}).json()
    cl.post("/api/projects/outdoor/experiments", headers=h, json={"pipeline": "demo-bm25"})
    assert cl.post("/api/projects/outdoor/champion", headers=h,
                   json={"pipeline": "demo-bm25", "expect": "someone-else@v1"}).status_code == 409
    assert cl.post("/api/projects/outdoor/champion", headers=h, json={"pipeline": "demo-bm25"}).json()["champion"] == "demo-bm25@v1"
    dry = cl.post(f"/api/projects/outdoor/experiments/{e['id']}/backtest", headers=h, json={"dry_run": True}).json()
    assert dry["arms"] == ["demo-bm25@v1", "demo-tuned@v1"] and dry["dry_run"]
    st = cl.get("/api/projects/outdoor/stages", headers=h).json()
    assert st["ship"]["registry"] == {"arch": "bm25", "brand": "outdoor", "uri": None} and st["ship"]["ready"]
    ex = cl.get("/api/projects/outdoor/experiments", headers=h).json()
    assert {x["pipeline"] for x in ex["experiments"]} == {"demo-tuned@v1", "demo-bm25@v1"}
    assert cl.get("/api/projects/outdoor/state", headers=h).json()["champion"] == "demo-bm25@v1"
    tmpl = cl.get("/api/engine-templates", headers=h).json()
    assert {t["engine"] for t in tmpl} >= {"local-bm25", "http"}


def test_labels_come_from_the_trainer_contracts_train_inputs(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    _judgments(c, "clicks-holdout", "click")
    _judgments(c, "sim-holdout", "heuristic")
    inputs = [{"ref": "cat@v1", "kind": "catalog", "role": "catalog"},
              {"ref": "first@v1", "kind": "judgments", "role": "judgments", "label_source": "llm"},
              {"ref": "grades@v2:train", "kind": "judgments", "role": "labels", "label_source": "human"}]
    assert PS.labels_from_inputs(inputs) == "grades@v2:train"
    assert PS.labels_from_inputs([{"kind": "judgments", "label_source": "clicks"}]) == "clicks"
    assert PS.labels_from_inputs([]) is None  # older and imported versions: unknown, never independent
    # the contract says clicks and simulated, judgment datasets say click and heuristic
    assert PS.label_independence(c, "clicks", "clicks-holdout@v1")["status"] == "shared"
    assert PS.label_independence(c, "simulated", "sim-holdout@v1")["status"] == "shared"
    assert PS.label_independence(c, "human", "sim-holdout@v1")["status"] == "independent"


def test_training_labels_come_from_a_retrained_versions_inputs(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    PR.create(c, "Acme")
    j = _judgments(c, "llm-grades", "llm", "judge-a")
    d = ops.pipeline_publish(c, {"name": "trained", "engine": "http", "spec": {"url": "https://x/rank"}}, dry_run=True)
    pm = c.registry.publish(d.model_copy(update={"train_inputs": [{"ref": ops.ref(j), "kind": "judgments",
                                                                    "role": "labels", "label_source": "llm"}]}))
    imp = c.registry.publish(d.model_copy(update={"source": {"kind": "mlflow"}, "train_inputs": []}))
    e = PS.experiment_add(c, "acme", ops.ref(pm))
    assert PS.training_labels(c, e) == "llm-grades@v1"
    assert PS.label_independence(c, PS.training_labels(c, e), "llm-grades@v1")["status"] == "shared"
    e2 = PS.experiment_add(c, "acme", ops.ref(imp))
    assert PS.label_independence(c, PS.training_labels(c, e2), "llm-grades@v1")["status"] == "unknown"
