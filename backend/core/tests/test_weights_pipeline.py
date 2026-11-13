"""Bring your own training: a training command writes a network and its cost per step, and Captain picks both up."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from captain_search import live as LV
from captain_search import ops
from captain_search import retrain as RT
from captain_search import server
from captain_search.data.datasets import snapshot
from captain_search.model.entities import DatasetKind, Provenance
from captain_search.weights import files as F
from captain_search.weights import net as N
from captain_search.weights import pipeline as WP
from captain_search.weights import session as S
from captain_search.weights import store as WS
from captain_search.workspace import init_workspace

DOC = Path(server.__file__).parent / "docs" / "trainer-contract.md"


def _example() -> str:
    """The ten-line trainer, taken from the guide itself, so the guide's example is the one that is run."""
    text = DOC.read_text()
    block = re.search(r"A ten-line trainer.*?```python\n(.*?)```", text, re.S).group(1)
    assert len(block.strip().splitlines()) == 10
    return block


@pytest.fixture
def ws(tmp_path):
    ctx = init_workspace("t", str(tmp_path / "store"))
    (tmp_path / "train.py").write_text(_example())
    snapshot(ctx, "shop-catalog", pa.table({"id": ["a", "b"], "title": ["red hat", "blue hat"]}), kind=DatasetKind.catalog,
             provenance=Provenance(source="generated"))
    ops.pipeline_publish(ctx, {"name": "sums", "engine": "local-bm25", "catalog": "shop-catalog",
                               "spec": {"train": {"command": f"{sys.executable} {tmp_path / 'train.py'}"}}})
    return ctx


def test_the_guides_ten_line_trainer_makes_a_network_captain_opens_and_its_cost_reaches_the_live_run(ws):
    r = RT.retrain(ws, "sums", local_exec=True)
    net = r["network"]
    assert net["ref"] == "sum-above-one@v1" and net["sizes"] == [2, 4, 1] and net["parameters"] == 17 and net["cost_points"] == 300
    new = ws.registry.load("pipeline", "sums", r["version"])
    assert new.tags["weights_model"] == "sum-above-one@v1" and r["metrics"]["cost"] < 0.3

    m = WS.manifest(ws, "sum-above-one")
    t = m["training"]
    assert t["steps"] == 300 and t["cost"][0][0] == 1.0 and t["cost_last"] < t["cost_first"] and t["pipeline"] == "sums@v1"
    assert m["inputs"]["meaning"] == "two numbers"

    # It opens as a session like any other network, and the record starts clean.
    s = S.open_session(ws, "sum-above-one", by="agent", forward_passes=1)
    assert s["ledger"]["total"] == 17 and s["ledger"]["weights_alone"] is True
    assert S.forward(ws, s["id"], inputs=[[0.9, 0.9], [0.1, 0.1]])["predicted"] == [1, 0]
    assert [row["id"] for row in WS.models(ws)][-1] == "sum-above-one"

    # Every cost point went to the live run as it was written, as the metric "cost".
    run = next(h for h in LV.listing(20) if h.get("ref") == r["pipeline"] or h.get("of") == "sums" or "sums" in str(h.get("title")))
    events = LV.read(run["id"])["events"]
    costs = [e for e in events if e["type"] == "metric" and e["name"] == "cost" and e.get("step") is not None]
    assert len(costs) == 300 and [e["step"] for e in costs[:3]] == [1.0, 2.0, 3.0] and costs[-1]["value"] < costs[0]["value"]


def test_a_cost_file_is_followed_while_it_is_being_written_and_a_half_written_line_waits(tmp_path):
    path = tmp_path / "cost.jsonl"
    got = []
    tail = WP.CostTail(str(path), lambda step, cost: got.append((step, cost))).start()
    with open(path, "a") as f:
        f.write('{"step": 1, "cost": 0.9}\n{"step": 2, "lo')
        f.flush()
        deadline = time.time() + 3
        while not got and time.time() < deadline:
            time.sleep(0.05)
        assert got == [(1.0, 0.9)], "the first whole line arrived while the file was still open, and the half line did not"
        f.write('ss": 0.5}\nnot json\n{"cost": true}\n{"cost": 0.25}\n')
    assert tail.stop() == 3
    assert got == [(1.0, 0.9), (2.0, 0.5), (3.0, 0.25)], "loss is read as cost, a line with no step is numbered, junk is skipped"
    assert WP.read_costs(str(path)) == [[1.0, 0.9], [2.0, 0.5], [3.0, 0.25]] and WP.read_costs(str(tmp_path / "none")) == []


def test_a_folder_written_by_hand_with_its_own_sets_is_imported_and_scored_on_them(tmp_path):
    ctx = init_workspace("t", str(tmp_path / "store"))
    folder = tmp_path / "mine"
    (folder / "sets").mkdir(parents=True)
    rng = np.random.default_rng(0)
    w1, b1, w2, b2 = rng.standard_normal((3, 2)), np.zeros(3), rng.standard_normal((2, 3)), np.zeros(2)
    np.savez(folder / "tensors.npz", **{"h.weight": w1, "h.bias": b1, "o.weight": w2, "o.bias": b2})
    x = rng.random((30, 2))
    for name in ("held_out", "evaluation", "practice"):
        np.savez(folder / "sets" / f"{name}.npz", x=x, y=(x[:, 0] > x[:, 1]).astype(int))
    manifest = {"format": "captain-weights/1", "name": "by-hand", "cost": "cross_entropy",
                "layers": [{"name": "h", "activation": "tanh", "weight": "h.weight", "bias": "h.bias"},
                           {"name": "o", "activation": "softmax", "weight": "o.weight", "bias": "o.bias"}],
                "tensors": {k: {"file": "tensors.npz"} for k in ("h.weight", "h.bias", "o.weight", "o.bias")},
                "inputs": {"meaning": "two numbers"}, "outputs": {"meaning": "which is larger", "names": ["second", "first"]},
                "data": {"synthetic": True, "note": "random points"},
                "sets": {k: {"file": f"sets/{k}.npz"} for k in ("held_out", "evaluation", "practice")}}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    (folder / "cost.jsonl").write_text('{"step": 1, "cost": 0.7}\n{"step": 2, "cost": 0.6}\n')

    got = WP.collect(ctx, str(folder), str(folder), None)
    assert got == {"model": "by-hand", "version": 1, "ref": "by-hand@v1", "parameters": 17, "sizes": [2, 3, 2], "cost_points": 2}
    m = WS.manifest(ctx, "by-hand")
    assert m["sets"]["held_out"]["n"] == 30 and m["sets"]["held_out"]["file"].startswith("sets/") and m["training"]["cost"] == [[1.0, 0.7], [2.0, 0.6]]
    sid = S.open_session(ctx, "by-hand")["id"]
    S.set_sets(ctx, sid, target={"classes": [1]}, should_not_change={"classes": [0]})
    ev = S.edit(ctx, sid, {"op": "add", "tensor": "o.bias", "indices": [[1]], "values": [5.0]})
    assert ev["cost"]["test"]["of"] == 30 and ev["cost"]["target"]["of"] + ev["cost"]["keep"]["of"] == 30
    assert ev["cost"]["target"]["after"] < ev["cost"]["target"]["before"] and ev["cost"]["keep"]["after"] > ev["cost"]["keep"]["before"]
    assert WS.whole(*WS.load(ctx, "by-hand"), ctx)["examples"][0]["name"].startswith("practice 0: ")
    # The same folder again is the next version, and its set files are not stored twice.
    assert WP.collect(ctx, str(folder), str(folder), None)["version"] == 2
    assert len([k for k in ctx.store.list("weights/by-hand/sets/")]) == 3


@pytest.mark.parametrize("break_it,words", [
    (lambda d: (d / "manifest.json").unlink(), "no manifest.json"),
    (lambda d: (d / "tensors.safetensors").write_bytes(b"xx"), "not a safetensors file"),
    (lambda d: (d / "manifest.json").write_text(json.dumps({**json.loads((d / "manifest.json").read_text()), "sets": {"held_out": {"file": "gone.npz"}}})), "not in the folder"),
    (lambda d: (d / "manifest.json").write_text("{nope"), "not valid JSON"),
])
def test_a_network_folder_that_cannot_be_opened_fails_the_import_in_a_sentence(tmp_path, break_it, words):
    ctx = init_workspace("t", str(tmp_path / "store"))
    F.write_model(str(tmp_path / "n"), N.init([2, 3, 1], seed=0), "toys")
    break_it(tmp_path / "n")
    with pytest.raises(N.WeightsError, match=words):
        WP.collect(ctx, str(tmp_path / "n"), str(tmp_path / "n"), None)
    assert WS.versions(ctx, "toys") == []
    with pytest.raises(N.WeightsError, match="is not a folder"):
        WP.collect(ctx, str(tmp_path), "missing", "toys")
