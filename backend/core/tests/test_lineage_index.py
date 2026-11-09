"""The lineage index: edge records written at publish time match a full scan, old workspaces backfill, and a
missing source is a node, not an error."""
import shutil

import pyarrow as pa

from captain_search import demo, ops
from captain_search import lineage_index as LI
from captain_search.data.datasets import snapshot
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.workspace import init_workspace


def _seeded(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    return c


def _shape(g):
    return ({n["id"] for n in g["nodes"]}, {(e["src"], e["dst"], e["relation"]) for e in g["edges"]})


# ---- the lineage index -------------------------------------------------------------------------


def test_index_matches_a_full_scan_on_the_demo_store_and_backfills_old_workspaces(tmp_path):
    c = _seeded(tmp_path)
    assert LI.is_complete(c.store)
    from_index = LI.graph_from(LI.read(c.store))
    from_scan = LI.graph_from(LI.scan(c.registry))
    assert _shape(from_index) == _shape(from_scan)
    rel = {(e["src"].split("@")[0], e["relation"], e["dst"].split("@")[0]) for e in from_index["edges"]}
    # judgments linked to the queries they grade, and to the pipelines whose results were graded
    assert ("dataset:demo-queries", "derived_from", "dataset:demo-judgments") in rel
    assert ("pipeline:demo-tuned", "candidates_from", "dataset:demo-judgments") in rel
    # grades made from logs point back at the logs (historic.py)
    assert any(r == "derived_from" and d.startswith("dataset:") and "judgments" in d and s.startswith("dataset:")
               and "log" in s for s, r, d in rel)
    # ops.lineage serves the index and still draws what environments serve
    g = ops.lineage(c)
    assert ("pipeline:demo-tuned@v1", "environment:staging", "deployed_from") in _shape(g)[1]

    # a workspace from before the index: no marker, so lineage scans, and reindex backfills it
    shutil.rmtree(tmp_path / "b" / "lineage" / "edges")
    (tmp_path / "b" / "lineage" / "index.yaml").unlink()
    assert not LI.is_complete(c.store)
    assert _shape(ops.lineage(c)) == _shape(g)
    r = LI.reindex(c)
    assert r["added"] > 0 and LI.is_complete(c.store)
    assert _shape(LI.graph_from(LI.read(c.store))) == _shape(from_scan)
    assert LI.reindex(c)["added"] == 0  # safe to run again


def test_index_matches_a_full_scan_on_the_north_pole_sample():
    from captain_search import sample_data as S
    from captain_search.workspace import open_context

    S.seed()
    c = open_context(S.NAME)
    assert LI.is_complete(c.store)
    assert _shape(LI.graph_from(LI.read(c.store))) == _shape(LI.graph_from(LI.scan(c.registry)))
    assert any(n["kind"] == "dataset" for n in ops.lineage(c)["nodes"])


def test_a_missing_source_is_an_unknown_node_not_an_error(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    t = pa.Table.from_pylist([{"query_id": "q1", "query": "boots"}])
    m = snapshot(c, "orphan-queries", t, kind=DatasetKind.queries,
                 provenance=Provenance(source="generated", params={"catalog": "elsewhere/catalog@v3", "logs": "gone-logs"}))
    g = ops.lineage(c)
    unknown = [n for n in g["nodes"] if n["kind"] == "unknown"]
    assert {n["label"] for n in unknown} == {"unknown source: elsewhere/catalog@v3", "unknown source: gone-logs"}
    assert all((n["id"], f"dataset:{m.name}@v1", "derived_from") in _shape(g)[1] for n in unknown)
    assert _shape(g) == _shape(LI.graph_from(LI.scan(c.registry)))


def test_retrains_record_their_parent(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    a = ops.pipeline_publish(c, {"name": "base", "engine": "http", "spec": {"url": "https://x/rank"}})
    m = ops.pipeline_publish(c, {"name": "retrained", "engine": "http", "spec": {"url": "https://x/rank"}}, dry_run=True)
    c.registry.publish(m.model_copy(update={"tags": {"retrained_from": ops.ref(a)}}))
    assert ("pipeline:base@v1", "pipeline:retrained@v1", "retrained_from") in _shape(ops.lineage(c))[1]


def test_trainer_contract_fields_become_edges(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    cat = snapshot(c, "cat", pa.Table.from_pylist([{"id": "1", "title": "Tent"}]), kind=DatasetKind.catalog,
                   provenance=Provenance(source="generated"))
    a = ops.pipeline_publish(c, {"name": "base", "engine": "http", "spec": {"url": "https://x/rank"}})
    draft = ops.pipeline_publish(c, {"name": "trained", "engine": "http", "spec": {"url": "https://x/rank"}}, dry_run=True)
    calls = []
    real = c.registry.versions
    c.registry.versions = lambda *x: (calls.append(x), real(*x))[1]
    retrained = c.registry.publish(draft.model_copy(update={
        "parents": [ops.ref(a)], "source": {"kind": "retrain"}, "tags": {"retrained_from": ops.ref(a)},
        "train_inputs": [{"ref": ops.ref(cat), "kind": "catalog", "role": "catalog"}]}))
    imported = c.registry.publish(draft.model_copy(update={
        "parents": ["mlflow-model:ranker@3", "mlflow-run:abc123", "registry:outdoor@v1", "other:thing", ops.ref(a)],
        "source": {"kind": "mlflow"}}))
    c.registry.versions = real
    assert not [x for x in calls if x[1] in ("mlflow-model", "mlflow-run", "registry", "other")]  # no lookups
    edges = _shape(ops.lineage(c))[1]
    me, im = f"pipeline:{ops.ref(retrained)}", f"pipeline:{ops.ref(imported)}"
    assert ("pipeline:base@v1", me, "retrained_from") in edges
    assert sum(1 for e in edges if e[1] == me and e[2] == "retrained_from") == 1  # tag and parents[0] are one edge
    assert ("dataset:cat@v1", me, "trained_on") in edges
    for src in ("mlflow-model:ranker@3", "mlflow-run:abc123", "model:outdoor@v1", "unknown:other:thing"):
        assert (src, im, "imported_from") in edges
    assert ("pipeline:base@v1", im, "parent") in edges
    nodes = {n["id"]: n for n in ops.lineage(c)["nodes"]}
    assert nodes["unknown:other:thing"]["label"] == "unknown source: other:thing"
    assert nodes["mlflow-model:ranker@3"]["kind"] == "mlflow-model"
