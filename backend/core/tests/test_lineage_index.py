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


def test_trained_on_and_parents_from_the_trainer_contract():
    from types import SimpleNamespace

    m = SimpleNamespace(train_inputs=[{"ref": "cat@v1", "kind": "catalog"}, {"kind": "judgments"}], parents=["a@v1"])
    assert LI._training_refs(m) == ["cat@v1"]
    assert LI._training_refs(SimpleNamespace()) == []


def test_outside_parents_are_unknown_nodes_without_a_lookup(tmp_path):
    from types import SimpleNamespace

    c = init_workspace("t", str(tmp_path / "b"))
    a = ops.pipeline_publish(c, {"name": "base", "engine": "http", "spec": {"url": "https://x/rank"}})
    m = ops.pipeline_publish(c, {"name": "imported", "engine": "http", "spec": {"url": "https://x/rank"}}, dry_run=True)
    m = m.model_copy(update={"version": 1})
    calls = []
    real = c.registry.versions
    c.registry.versions = lambda *x: (calls.append(x), real(*x))[1]
    fake = SimpleNamespace(**{k: getattr(m, k) for k in ("kind", "name", "version", "id", "catalog", "tags", "registry_ref")},
                           parents=["mlflow-run:abc123", "registry:outdoor@v1", ops.ref(a)], train_inputs=[])
    edges, labels = LI.derive(c.registry, fake)
    c.registry.versions = real
    srcs = {e["src"] for e in edges if e["relation"] == "parent"}
    assert srcs == {"unknown:mlflow-run:abc123", "unknown:registry:outdoor@v1", "pipeline:base@v1"}
    assert labels["unknown:mlflow-run:abc123"] == "unknown source: mlflow-run:abc123"
    assert calls == []  # exact refs are a direct get, outside ids aren't looked up at all
    g = LI.graph_from([{"from": e["src"], "to": e["dst"], "relation": e["relation"], "labels": labels} for e in edges])
    assert {n["kind"] for n in g["nodes"] if n["id"].startswith("unknown:")} == {"unknown"}
