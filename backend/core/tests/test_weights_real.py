"""A trained session model with rankers: brought in from its folder unchanged, described from its shapes, and read.

Every file here is made up by this test: seeded random numbers under the array names PyTorch gives a small
transformer, a model.json, and a three-tree model written by hand in LightGBM's text format. No real model's
file, name or number is used.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from captain_search import changes as CH
from captain_search import server
from captain_search.cli.main import S as CLI_STATE
from captain_search.cli.main import app as cli
from captain_search.staging import predict as PD
from captain_search.weights import real as R
from captain_search.weights import session as WSN
from captain_search.weights import store as WS
from captain_search.weights import trees as T
from captain_search.weights.net import WeightsError
from captain_search.workspace import init_workspace

TEXT_WIDTH = 12
TABLES = {"act": 5, "gap": 4, "dur": 4, "price": 6, "rep": 3, "pos": 10, "cat.s": 7}
CANDIDATE_TABLES = {"c_price": 6, "c_cat.s": 7, "c_rep": 3}

TREES = """tree
version=v4
num_class=1
num_tree_per_iteration=1
label_index=0
max_feature_idx=4
objective=lambdarank
feature_names=Column_0 Column_1 Column_2 Column_3 Column_4
feature_infos=[0:1] [0:1] [0:10] [-1:1] none
tree_sizes=300 200 200

Tree=0
num_leaves=3
num_cat=0
split_feature=0 2
split_gain=10 5
threshold=0.5 3.5
decision_type=2 2
left_child=1 -1
right_child=-3 -2
leaf_value=0.1 -0.2 0.3
leaf_weight=1 1 1
leaf_count=10 20 30
internal_value=0 0.05
internal_weight=0 0
internal_count=60 30
is_linear=0
shrinkage=1


Tree=1
num_leaves=2
num_cat=0
split_feature=1
split_gain=4
threshold=0.25
decision_type=2
left_child=-1
right_child=-2
leaf_value=0.05 -0.05
leaf_weight=1 1
leaf_count=25 35
internal_value=0
internal_weight=0
internal_count=60
is_linear=0
shrinkage=0.1


Tree=2
num_leaves=2
num_cat=0
split_feature=3
split_gain=1
threshold=0
decision_type=0
left_child=-1
right_child=-2
leaf_value=0.01 -0.01
leaf_weight=1 1
leaf_count=40 20
internal_value=0
internal_weight=0
internal_count=60
is_linear=0
shrinkage=0.1


end of trees

feature_importances:
Column_0=1
Column_1=1
Column_2=1
Column_3=1

parameters:
[boosting: gbdt]
[objective: lambdarank]
[num_leaves: 3]
end of parameters

pandas_categorical:null
"""
COLS = ["f_views", "f_carts", "m_price", "s1_recency", "s1_never"]


def arrays(width: int = 8, layers: int = 1, seed: int = 0, over: dict | None = None) -> dict[str, np.ndarray]:
    """The arrays of a made-up session model, under the names PyTorch saves a small transformer with."""
    rng = np.random.default_rng(seed)
    f = lambda *shape: rng.standard_normal(shape).astype("float32")  # noqa: E731
    a = {"logit_scale": np.float32(2.5)}
    for name, rows in TABLES.items():
        a[f"{name}.weight"] = f(rows, width)
    a["text.weight"], a["text.bias"] = f(width, TEXT_WIDTH), f(width)
    a["ln.weight"], a["ln.bias"] = f(width), f(width)
    a["c_text.weight"], a["c_text.bias"] = f(width, TEXT_WIDTH), f(width)
    for name, rows in CANDIDATE_TABLES.items():
        a[f"{name}.weight"] = f(rows, width)
    for name in ("q", "rq"):
        a[f"{name}.weight"], a[f"{name}.bias"] = f(width, width), f(width)
    a["intent.weight"], a["intent.bias"] = f(1, width), f(1)
    for k in range(layers):
        p = f"L{k}"
        a[f"{p}.self_attn.in_proj_weight"], a[f"{p}.self_attn.in_proj_bias"] = f(3 * width, width), f(3 * width)
        a[f"{p}.self_attn.out_proj.weight"], a[f"{p}.self_attn.out_proj.bias"] = f(width, width), f(width)
        a[f"{p}.linear1.weight"], a[f"{p}.linear1.bias"] = f(4 * width, width), f(4 * width)
        a[f"{p}.linear2.weight"], a[f"{p}.linear2.bias"] = f(width, 4 * width), f(width)
        for n in ("norm1", "norm2"):
            a[f"{p}.{n}.weight"], a[f"{p}.{n}.bias"] = f(width), f(width)
    a["cfg"] = np.array(json.dumps({"d": width, "layers": layers}))
    a["edges"] = np.linspace(1.0, 4.0, 4)                                  # float64, and not a learned tensor
    a["cat_vocab"] = np.array([f"category {i}" for i in range(6)])         # one fewer than the table's 7 rows
    a.update(over or {})
    return a


def make_folder(root: Path, width: int = 8, layers: int = 1, heads: int | None = 2, seed: int = 0, over: dict | None = None,
                nested: bool = True) -> tuple[Path, dict[str, np.ndarray]]:
    """Write a made-up version folder: weights/sess.npz, model.json, three rankers, two lookup tables, MANIFEST.yaml."""
    w = root / "weights" if nested else root
    w.mkdir(parents=True, exist_ok=True)
    a = arrays(width, layers, seed, over)
    np.savez(w / "sess.npz", **a)
    said = f"XS ({layers} layer{'s' if layers != 1 else ''}, width {width}" + (f", {heads} heads)" if heads else ")")
    (w / "model.json").write_text(json.dumps({"session_model": said, "feats": COLS}))
    for name in ("full", "patc", "pclick"):
        (w / f"rank_{name}.txt").write_text(TREES.replace("lambdarank", "binary") if name == "pclick" else TREES)
        (w / f"rank_{name}.cols.json").write_text(json.dumps(COLS))
    np.savez(w / "covisit.npz", pairs=np.arange(6, dtype="int32").reshape(3, 2), counts=np.ones(3, dtype="float32"))
    np.savez(w / "pop7.npz", item=np.arange(4, dtype="int64"))
    if nested:
        (root / "MANIFEST.yaml").write_text("architecture: made-up\nversion: v1\nreproducibility: not-tested\n")
    return root, a


def tensors_of(a: dict) -> dict:
    return {k: v for k, v in a.items() if R._is_tensor(k, np.asarray(v))}


@pytest.fixture
def svc(tmp_path, monkeypatch):
    CH.reset_for_tests()
    R._KINDS.clear()
    ctx = init_workspace("t", str(tmp_path / "store"))
    monkeypatch.setattr(server, "ctx", lambda *a, **k: (CH.watch(ctx.store), ctx)[1])
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}

    class Svc:
        pass

    s = Svc()
    s.ctx, s.tmp = ctx, tmp_path

    def get(path, code=200, **params):
        r = c.get(path, headers=hd, params=params)
        assert r.status_code == code, f"{path}: {r.status_code} {r.text[:300]}"
        return r.json()

    def post(path, body=None, code=200):
        r = c.post(path, headers=hd, json=body if body is not None else {})
        assert r.status_code == code, f"{path}: {r.status_code} {r.text[:300]}"
        return r.json()

    s.get, s.post = get, post
    yield s
    CH.reset_for_tests()


def bring(svc, name="sample", **kw):
    folder, a = make_folder(svc.tmp / f"folder-{name}", **kw)
    out = R.import_from(svc.ctx, str(folder), name)
    return out, a, folder


def has(d: dict, *keys):
    missing = [k for k in keys if k not in d]
    assert not missing, f"missing {missing} in {sorted(d)}"


def plain(text: str):
    assert not re.search(r"[—–←-⇿⟵-⟿]|\bengines?\b|\bdaemon\b|Traceback|Error\(", text), text


# ---- import ---------------------------------------------------------------------------------------------------------------

def test_import_keeps_every_file_and_every_tensor_bit_for_bit(svc):
    out, a, folder = bring(svc)
    assert out["ref"] == "sample@v1" and out["kind"] == "session-ranker" and out["kind_says"] == "session model with rankers"
    m = WS.manifest(svc.ctx, "sample")
    # every file of the folder is stored unchanged, and its digest is in the manifest
    for f in sorted((folder / "weights").iterdir()):
        raw = f.read_bytes()
        assert svc.ctx.store.get(f"weights/sample/v1/files/{f.name}") == raw
        assert m["source"]["files"][f.name] == {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    assert svc.ctx.store.get("weights/sample/v1/files/MANIFEST.yaml") == (folder / "MANIFEST.yaml").read_bytes()
    assert m["source"]["declared"]["reproducibility"] == "not-tested" and m["source"]["from"] == str(folder)
    # the tensors: the same names, types and bytes as the arrays that were written
    want = tensors_of(a)
    assert m["sha256"] == WS.digest(want) == out["sha256"]
    _, got = WS.load(svc.ctx, "sample")
    assert sorted(got) == sorted(want) and len(want) == len(m["tensors"])
    for k in want:
        assert got[k].dtype == np.asarray(want[k]).dtype and got[k].tobytes() == np.asarray(want[k]).tobytes(), k
    assert m["parameters"] == sum(np.asarray(v).size for v in want.values()) == out["parameters"]
    # strings and arrays that are not learned are facts, not tensors
    assert set(m["facts"]) == {"cfg", "edges", "cat_vocab"} and not set(m["facts"]) & set(m["tensors"])
    assert m["facts"]["cat_vocab"]["value"][0] == "category 0" and m["facts"]["edges"]["value"] == [1.0, 2.0, 3.0, 4.0]
    assert json.loads(m["facts"]["cfg"]["value"])["d"] == 8
    assert out["rankers"] == ["full", "patc", "pclick"] and out["tables"] == ["covisit", "pop7"]
    assert [t["arrays"][0]["name"] for t in m["tables"]] == ["pairs", "item"]


def test_a_version_is_written_once_and_the_folder_itself_can_be_named(svc):
    bring(svc)
    before = svc.ctx.store.get("weights/sample/v1/manifest.json")
    folder, _ = make_folder(svc.tmp / "again", seed=1, nested=False)
    out = R.import_from(svc.ctx, str(folder), "sample")
    assert out["version"] == 2 and svc.ctx.store.get("weights/sample/v1/manifest.json") == before
    assert WS.manifest(svc.ctx, "sample", 2)["source"]["manifest_yaml"] is False
    assert WS.manifest(svc.ctx, "sample", 1)["sha256"] != WS.manifest(svc.ctx, "sample", 2)["sha256"]
    with pytest.raises(WeightsError, match="--as"):
        R.import_from(svc.ctx, str(folder), None)
    empty = svc.tmp / "empty"
    empty.mkdir()
    with pytest.raises(WeightsError, match="no sess.npz"):
        R.import_from(svc.ctx, str(empty), "x")
    assert not R.looks_real(str(empty)) and R.looks_real(str(folder)) and R.looks_real("s3://some-bucket/a/b")


def test_an_s3_address_is_read_with_the_default_login_and_nothing_is_written_there(svc, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    for k, v in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"), ("AWS_DEFAULT_REGION", "us-east-1")):
        monkeypatch.setenv(k, v)
    folder, a = make_folder(svc.tmp / "up")
    with moto.mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="made-up-models")
        for f in folder.rglob("*"):
            if f.is_file():
                s3.put_object(Bucket="made-up-models", Key=f"arch/demo/v1/{f.relative_to(folder).as_posix()}", Body=f.read_bytes())
        s3.put_object(Bucket="made-up-models", Key="arch/demo/v1/inputs/big.bin", Body=b"not fetched")
        before = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="made-up-models")["Contents"])
        out = R.import_from(svc.ctx, "s3://made-up-models/arch/demo/v1/", "cloudy")
        assert sorted(o["Key"] for o in s3.list_objects_v2(Bucket="made-up-models")["Contents"]) == before
        with pytest.raises(WeightsError) as e:
            R.import_from(svc.ctx, "s3://no-such-made-up-bucket/x", "cloudy")
        plain(str(e.value))
    m = WS.manifest(svc.ctx, "cloudy")
    assert out["sha256"] == WS.digest(tensors_of(a)) and m["source"]["from"] == "s3://made-up-models/arch/demo/v1"
    assert "MANIFEST.yaml" in m["source"]["files"] and not any("big" in k for k in m["source"]["files"])


# ---- the description --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("layers", [1, 2])
def test_the_parts_are_worked_out_from_the_shapes(svc, layers):
    out, a, _ = bring(svc, layers=layers, heads=2)
    d = svc.get("/api/weights/models/sample")
    has(d, "id", "version", "kind", "kind_says", "width", "depth", "heads", "heads_from", "heads_says", "head_width", "feed_forward",
        "parameters", "parts", "wiring_says", "facts", "rankers", "tables", "source", "can", "numbers", "sizes", "layers", "examples")
    assert (d["width"], d["depth"], d["heads"], d["head_width"], d["feed_forward"]) == (8, layers, 2, 4, 32)
    assert d["heads_from"] == "model.json" and d["numbers"] is False and d["declared"]["layers_agree"] is True
    assert d["can"] == {"read": True, "edit": True, "run": False, "deploy": False, "says": R.NOT_YET, "edits_say": R.NOT_MEASURED}
    parts = {p["id"]: p for p in d["parts"]}
    for p in d["parts"]:
        has(p, "id", "kind", "name", "says", "technical", "tensors", "shape", "parameters", "feeds_from")
        plain(p["name"] + p["says"])
    # every tensor belongs to exactly one part, and the parts add up to the whole
    owned = [t for p in d["parts"] for t in p["tensors"]]
    assert sorted(owned) == sorted(tensors_of(a)) and sum(p["parameters"] for p in d["parts"]) == d["parameters"] == out["parameters"]
    assert not [p for p in d["parts"] if p["kind"] == "other"]
    # the tables, each with what it embeds
    tables = {p["id"]: (p["rows"], p["width"], p["side"]) for p in d["parts"] if p["kind"] == "embedding"}
    assert tables == {**{k: (r, 8, "session") for k, r in TABLES.items()}, **{k: (r, 8, "candidate") for k, r in CANDIDATE_TABLES.items()}}
    assert parts["act"]["name"] == "action table" and parts["act"]["meaning_from"] == "name" and "embedding table" in parts["act"]["says"]
    assert parts["c_cat.s"]["name"] == "candidate category table"
    assert (parts["text"]["kind"], parts["text"]["inputs"], parts["text"]["outputs"]) == ("projection", TEXT_WIDTH, 8)
    assert parts["ln"]["kind"] == "norm" and set(parts["ln"]["feeds_from"]) == set(TABLES) | {"text"}
    assert {p["id"] for p in d["parts"] if p["kind"] == "head"} == {"q", "rq", "intent"} and parts["intent"]["shape"] == [1, 8]
    assert parts["logit_scale"]["kind"] == "scale" and parts["logit_scale"]["parameters"] == 1
    # each layer: attention with its four matrices, a norm, feed-forward, a norm, in that order
    last = "ln"
    for k in range(layers):
        att = parts[f"L{k}.self_attn"]
        assert att["kind"] == "attention" and att["layer"] == k and att["feeds_from"] == [last] and att["heads"] == 2
        assert "which earlier events this one looks at" in att["says"]
        assert [(mt["role"], mt["rows"], mt["shape"]) for mt in att["matrices"]] == [
            ("query", [0, 8], [8, 8]), ("key", [8, 16], [8, 8]), ("value", [16, 24], [8, 8]), ("output", [0, 8], [8, 8])]
        assert att["matrices"][1]["tensor"] == f"L{k}.self_attn.in_proj_weight" and att["matrices"][3]["tensor"] == f"L{k}.self_attn.out_proj.weight"
        ff = parts[f"L{k}.ff"]
        assert (ff["kind"], ff["inputs"], ff["hidden"]) == ("feed_forward", 8, 32) and ff["feeds_from"] == [f"L{k}.norm1"]
        assert parts[f"L{k}.norm1"]["feeds_from"] == [f"L{k}.self_attn"] and parts[f"L{k}.norm2"]["feeds_from"] == [f"L{k}.ff"]
        last = f"L{k}.norm2"
    assert parts["q"]["feeds_from"] == [last]
    assert [r["name"] for r in d["rankers"]] == ["full", "patc", "pclick"] and d["rankers"][0]["trees"] == 3
    assert {f["name"] for f in d["facts"]} == {"cfg", "edges", "cat_vocab"}
    # the list of models says what kind each is, and the starters are still there
    kinds = {r["id"]: r["kind"] for r in svc.get("/api/weights/models")}
    assert kinds == {"tiny": "dense", "digits": "dense", "sample": "session-ranker"}


def test_heads_are_unknown_when_no_file_says(svc):
    bring(svc, heads=None)
    d = R.whole(svc.ctx, "sample")
    assert d["heads"] is None and d["head_width"] is None and d["heads_says"].startswith("Unknown")
    sid = svc.post("/api/weights/sessions", {"model": "sample"})["id"]
    assert "do not say how many heads" in svc.get(f"/api/weights/sessions/{sid}/attention", code=400)["error"]
    assert svc.get(f"/api/weights/sessions/{sid}/attention", heads=4)["heads_from"] == "given with the request"
    assert "does not split evenly" in svc.get(f"/api/weights/sessions/{sid}/attention", code=400, heads=3)["error"]


# ---- reading through a session --------------------------------------------------------------------------------------------------

def known():
    """Arrays with a known attention block and known tables, so what is read can be checked by hand."""
    eye = np.eye(8, dtype="float32")
    act = np.zeros((5, 8), dtype="float32")
    act[0, :4] = 1.0            # row 0 lives in the first head's half
    act[1, :4] = 2.0            # row 1 points the same way as row 0
    act[2, :4] = -1.0           # row 2 points the opposite way
    act[3, 4:] = 1.0            # row 3 lives in the second head's half
    act[4, 0], act[4, 7] = 1.0, 1.0
    pos = np.outer(np.arange(1, 11), np.ones(8)).astype("float32")     # every row the same direction: one direction holds it all
    return {"L0.self_attn.in_proj_weight": np.concatenate([eye, 2 * eye, 3 * eye]), "L0.self_attn.out_proj.weight": eye,
            "act.weight": act, "pos.weight": pos, "cat_vocab": np.array([f"category {i}" for i in range(7)])}


def test_the_query_key_and_value_matrices_are_the_three_stacked_blocks(svc):
    bring(svc, over=known())
    s = svc.post("/api/weights/sessions", {"model": "sample"})
    p = f"/api/weights/sessions/{s['id']}"
    att = next(x for x in svc.get(p + "/layers")["parts"] if x["kind"] == "attention")
    for mt, scale in zip(att["matrices"], (1.0, 2.0, 3.0, 1.0)):
        got = svc.get(p + "/read", tensor=mt["tensor"], rows=f"{mt['rows'][0]}:{mt['rows'][1]}")
        assert got["values"] == (scale * np.eye(8)).tolist() and got["count"] == 64, mt["role"]
    a = svc.get(p + "/attention", layer=0)
    has(a, "layer", "part", "width", "heads", "heads_from", "head_width", "of", "says", "reads_says", "run")
    assert (a["heads"], a["head_width"], a["run"]) == (2, 4, False) and len(a["of"]) == 2
    h0, h1 = a["of"]
    # head 1 uses rows 0:4 of each block. Q^T K is 2 on its four diagonal places: size sqrt(4 * 4), singular values 2.
    assert h0["query_key"]["size"] == pytest.approx(4.0) and h0["query_key"]["top"][:4] == pytest.approx([2, 2, 2, 2]) and h0["query_key"]["top"][4] == pytest.approx(0)
    assert h0["output_value"]["size"] == pytest.approx(6.0) and h0["output_value"]["top"][:4] == pytest.approx([3, 3, 3, 3])
    assert h0["query_key"]["rank_at_most"] == 4 and h0["query_key"]["shape"] == [8, 8]
    # how strongly the action table is read: each row's part inside the head's half, through each matrix
    act = known()["act.weight"]
    r0 = next(t for t in h0["reads"] if t["table"] == "act")
    r1 = next(t for t in h1["reads"] if t["table"] == "act")
    half0, half1 = np.linalg.norm(act[:, :4], axis=1), np.linalg.norm(act[:, 4:], axis=1)
    rms = lambda v: float(np.sqrt((v ** 2).mean()))  # noqa: E731
    assert (r0["query"], r0["key"], r0["value"]) == pytest.approx((rms(half0), 2 * rms(half0), 3 * rms(half0)))
    assert (r1["query"], r1["key"], r1["value"]) == pytest.approx((rms(half1), 2 * rms(half1), 3 * rms(half1)))
    assert r0["strongest"][0]["row"] == 1 and r1["strongest"][0]["row"] == 3 and r0["rows"] == 5
    assert {t["table"] for t in h0["reads"]} == set(TABLES)          # the candidate's tables do not pass through attention
    one = svc.get(p + "/attention", layer=0, head=1, matrix="output_value")
    assert len(one["of"]) == 1 and np.array(one["of"][0]["matrix"]["grid"]).tolist() == np.diag([0, 0, 0, 0, 3, 3, 3, 3.0]).tolist()
    assert "not one of them" in svc.get(p + "/attention", code=400, layer=3)["error"]
    assert "Name one head" in svc.get(p + "/attention", code=400, matrix="query_key")["error"]


def test_nearest_rows_largest_directions_slices_stats_and_a_heatmap(svc):
    bring(svc, over=known())
    s = svc.post("/api/weights/sessions", {"model": "sample"})
    p = f"/api/weights/sessions/{s['id']}"
    n = svc.get(p + "/nearest", tensor="act.weight", row=0, top=2)
    has(n, "tensor", "rows", "width", "row", "label", "norm", "nearest", "farthest", "labels_says", "says")
    assert n["nearest"][0]["row"] == 1 and n["nearest"][0]["cosine"] == pytest.approx(1.0) and len(n["nearest"]) == 2
    assert n["farthest"][0]["row"] == 2 and n["farthest"][0]["cosine"] == pytest.approx(-1.0) and n["label"] is None
    # rows get names only when a saved list has exactly one name per row
    c = svc.get(p + "/nearest", tensor="cat.s.weight", row=3)
    assert c["label"] == "category 3" and c["nearest"][0]["label"].startswith("category ") and "cat_vocab" in c["labels_says"]
    d = svc.get(p + "/directions", tensor="pos.weight", top=2)
    has(d, "tensor", "shape", "singular_values", "share", "needed_for_90", "of", "directions", "says")
    assert d["share"][0] == pytest.approx(1.0) and d["needed_for_90"] == 1 and d["directions"][0]["strongest_rows"][0]["row"] == 9
    assert d["singular_values"][0] == pytest.approx(np.linalg.norm(known()["pos.weight"]))
    # a slice of any tensor: a table, a list, a single number
    assert svc.get(p + "/read", tensor="act.weight", rows="1", cols="0:5")["values"] == [[2.0, 2.0, 2.0, 2.0, 0.0]]
    one = svc.get(p + "/read", tensor="logit_scale")
    assert one["values"] == [2.5] and one["shape"] == [] and one["cols"] is None
    assert len(svc.get(p + "/read", tensor="L0.self_attn.in_proj_bias", rows="8:16")["values"]) == 8
    assert "no tensor named" in svc.get(p + "/read", code=400, tensor="cat_vocab")["error"]
    st = svc.get(p + "/stats")
    assert len(st["tensors"]) == len(tensors_of(arrays())) and "neurons" not in st
    rows = svc.get(p + "/stats", tensor="act.weight", neurons=1)
    assert [r["norm"] for r in rows["rows"]][:3] == pytest.approx([2.0, 4.0, 2.0]) and rows["rows_of"] == 5
    h = svc.get(p + "/heatmap", tensor="L0.self_attn.in_proj_weight", rows="16:24")
    has(h, "tensor", "shape", "rows", "cols", "grid_shape", "scale", "min", "max", "grid")
    assert h["grid"] == (3 * np.eye(8)).tolist() and h["grid_shape"] == [8, 8] and h["scale"] == 3.0
    assert "Name the matrix" in svc.get(p + "/heatmap", code=400)["error"]
    # every number shown is counted, and each read is in the record in a sentence
    led = svc.get(p)["ledger"]
    assert led["weights_read"] == 64 + 5 + 1 + 8 and led["total"] == sum(v.size for v in tensors_of(arrays()).values()) and led["changed"] == 0
    for e in svc.get(p + "/events")["events"]:
        plain(e["sentence"])
    # a session opened without raw weights is refused the numbers, and still gets the description
    blind = svc.post("/api/weights/sessions", {"model": "sample", "by": "agent", "raw_weights": False})["id"]
    assert svc.get(f"/api/weights/sessions/{blind}/read", code=403, tensor="act.weight")["refused"] is True
    assert svc.get(f"/api/weights/sessions/{blind}/heatmap", code=403, tensor="act.weight")["refused"] is True
    assert svc.get(f"/api/weights/sessions/{blind}/layers")["width"] == 8
    # the same two table readings work on a fully connected network too
    tiny = svc.post("/api/weights/sessions", {"model": "tiny"})["id"]
    assert len(svc.get(f"/api/weights/sessions/{tiny}/nearest", tensor="layer1.weight", row=0)["nearest"]) == 2
    assert "no attention" in svc.get(f"/api/weights/sessions/{tiny}/attention", code=400)["error"]


# ---- the tree rankers -----------------------------------------------------------------------------------------------------------

def test_the_tree_file_is_read_with_its_splits_gains_and_leaves():
    m = T.parse(TREES)
    assert (m["inputs"], m["objective"], len(m["trees"]), m["parameters"]["boosting"]) == (5, "lambdarank", 3, "gbdt")
    t0 = m["trees"][0]
    assert (t0["feature"], t0["threshold"], t0["gain"], t0["leaf_value"]) == ([0, 2], [0.5, 3.5], [10.0, 5.0], [0.1, -0.2, 0.3])
    s = T.summary(m, COLS, trees=2)
    has(s, "kind", "says", "rule", "objective", "trees", "inputs", "leaves", "splits", "gain", "unused", "unused_says", "by_input", "first_trees")
    assert (s["trees"], s["inputs"], s["leaves"], s["splits"], s["gain"]) == (3, 5, 7, 4, 20.0)
    assert [(r["name"], r["splits"], r["gain"], r["share"], r["trees"]) for r in s["by_input"]] == [
        ("f_views", 1, 10.0, 0.5, 1), ("m_price", 1, 5.0, 0.25, 1), ("f_carts", 1, 4.0, 0.2, 1), ("s1_recency", 1, 1.0, 0.05, 1),
        ("s1_never", 0, 0.0, 0.0, 0)]
    assert s["unused"] == [{"input": 4, "name": "s1_never", "group": "s1_", "constant": True}] and "1 of 5 inputs are never asked about" in s["unused_says"]
    assert "not neurons" in s["says"] and "no weights or biases" in s["says"]
    # the first tree, as a screen draws it: is f_views at most 0.5? yes: is m_price at most 3.5? ...
    root = s["first_trees"][0]["root"]
    assert (root["name"], root["threshold"], root["gain"], root["count"], root["missing_goes"]) == ("f_views", 0.5, 10.0, 60, "left")
    assert (root["left"]["name"], root["left"]["left"], root["left"]["right"]) == ("m_price", {"leaf": 0, "value": 0.1, "count": 10},
                                                                                 {"leaf": 1, "value": -0.2, "count": 20})
    assert root["right"] == {"leaf": 2, "value": 0.3, "count": 30}
    assert (s["first_trees"][0]["depth"], s["first_trees"][0]["leaves"], len(s["first_trees"])) == (2, 3, 2)
    # with no names beside it, the file's own names are used
    assert T.summary(m)["by_input"][0]["name"] == "Column_0"
    assert T.names_from({"cols": COLS}, 5) == COLS and T.names_from(["a"], 5) == []
    # a tree that is one leaf, and files that are not a model
    lone = T.parse("tree\nmax_feature_idx=1\nobjective=binary\n\nTree=0\nnum_leaves=1\nleaf_value=0.25\n\nend of trees\n")
    assert T.summary(lone)["first_trees"][0]["root"] == {"leaf": 0, "value": 0.25} and len(T.summary(lone)["unused"]) == 2
    for bad in ("hello", "tree\nobjective=binary\n", TREES.replace("split_feature=0 2", "split_feature=0 9"),
                TREES.replace("left_child=1 -1", "left_child=7 -1")):
        with pytest.raises(WeightsError) as e:
            T.parse(bad)
        plain(str(e.value))


def test_a_models_rankers_are_served_and_the_unused_input_is_named(svc):
    bring(svc)
    r = svc.get("/api/weights/models/sample/trees")
    has(r, "model", "version", "ranker", "rankers", "names_from", "file", "sha256", "kind", "says", "unused", "unused_says", "by_input",
        "first_trees", "cannot")
    assert (r["ranker"], r["rankers"], r["objective"], r["names_from"]) == ("full", ["full", "patc", "pclick"], "lambdarank", "the cols file")
    assert r["unused"][0]["name"] == "s1_never" and len(r["first_trees"]) == 3 and r["cannot"] == R.TREES_CANNOT
    assert r["sha256"] == hashlib.sha256(TREES.encode()).hexdigest()
    click = svc.get("/api/weights/models/sample/trees", ranker="pclick", trees=1)
    assert click["objective"] == "binary" and len(click["first_trees"]) == 1
    assert "no ranker named" in svc.get("/api/weights/models/sample/trees", code=400, ranker="nope")["error"]
    assert "no tree rankers" in svc.get("/api/weights/models/tiny/trees", code=400)["error"]


# ---- what it cannot do yet ----------------------------------------------------------------------------------------------------

def test_running_is_refused_in_one_plain_sentence(svc):
    bring(svc)
    assert R.NOT_YET == "This model can be read and changed here, not run yet."
    s = svc.post("/api/weights/sessions", {"model": "sample"})
    p = f"/api/weights/sessions/{s['id']}"
    for path, body in (("/forward", {"inputs": [[0.1]]}),
                       ("/activations", {"example": 0}), ("/gradients", {"example": 0}), ("/trace", {"example": 0}),
                       ("/slice", {"tensor": "act.weight", "index": [0, 0]}), ("/evaluate", {}),
                       ("/sets", {"target": {"classes": [1]}}), ("/export", {"folder": str(svc.tmp / "out")})):
        assert svc.post(p + path, body, code=400) == {"error": R.NOT_YET}, path
    assert svc.get(p + "/template", code=400)["error"] == R.NOT_YET
    with pytest.raises(ValueError, match="not run yet"):
        PD.resolve(svc.ctx, "weights:sample@v1")
    # nothing was changed, and the session still reads, reports and closes
    assert svc.get(p + "/diff")["changed"] == 0 and svc.get(p + "/report")["ledger"]["changed"] == 0
    assert svc.post(p + "/close")["closed"] is True
    _, got = WS.load(svc.ctx, "sample")
    assert WS.digest(got) == WS.manifest(svc.ctx, "sample")["sha256"]
    # a fully connected network is not touched by any of this
    tiny = svc.post("/api/weights/sessions", {"model": "tiny"})["id"]
    assert svc.post(f"/api/weights/sessions/{tiny}/edits", {"op": "zero", "layer": 1, "neuron": 0})["kind"] == "edit"
    assert svc.get(f"/api/weights/sessions/{tiny}/heatmap", tensor="layer2.weight")["layer"] == 1


# ---- live ---------------------------------------------------------------------------------------------------------------------

def _told(svc, since, topics, call):
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(answer=svc.get("/api/changes", since=since, topics=topics)), daemon=True)
    t.start()
    time.sleep(0.15)
    assert "answer" not in box, f"it answered before anything changed: {box}"
    call()
    t.join(3)
    assert "answer" in box, "the waiting screen was not told"
    return {c["topic"] for c in svc.get("/api/changes", since=since, topics=topics, wait=0)["changed"]}


def test_an_import_and_a_read_tell_a_listening_screen(svc):
    folder, _ = make_folder(svc.tmp / "live")
    seq = svc.get("/api/changes")["seq"]
    got = _told(svc, seq, "weights,weights:sample", lambda: svc.post("/api/weights/import", {"folder": str(folder), "name": "sample"}))
    assert got == {"weights", "weights:sample"}
    sid = svc.post("/api/weights/sessions", {"model": "sample"})["id"]
    seq = svc.get("/api/changes")["seq"]
    topics = f"weights:sample,weights-session:{sid}"
    assert _told(svc, seq, topics, lambda: svc.get(f"/api/weights/sessions/{sid}/nearest", tensor="act.weight", row=0)) == set(topics.split(","))
    # a screen looking on tells nobody, and is counted beside the record
    seq = svc.get("/api/changes")["seq"]
    svc.get(f"/api/weights/sessions/{sid}/read", tensor="act.weight", watch=1)
    svc.get("/api/weights/models/sample", session=sid)
    assert svc.get("/api/changes", since=seq, topics=topics, wait=0).get("changed", []) == []
    led = svc.get(f"/api/weights/sessions/{sid}")["ledger"]
    assert (led["screen_looks"], led["screen_numbers"], led["weights_read"]) == (2, 40, 0)


# ---- cst weights ----------------------------------------------------------------------------------------------------------------

RUN = CliRunner()


def cst(*args, code=0):
    CLI_STATE.json = False
    r = RUN.invoke(cli, ["--json", *args])
    assert r.exit_code == code, r.output
    return json.loads(r.output) if r.output.strip() else None


def words(*args, code=0) -> str:
    CLI_STATE.json = False
    r = RUN.invoke(cli, list(args))
    assert r.exit_code == code, r.output
    plain(r.output)
    return r.output


def test_the_commands_bring_it_in_describe_it_read_it_and_refuse_to_change_it(tmp_path):
    R._KINDS.clear()
    cst("init", "demo", "--storage", str(tmp_path / "bucket"))
    folder, a = make_folder(tmp_path / "folder", layers=2, over=known() | {"L1.self_attn.in_proj_weight": arrays(layers=2)["L1.self_attn.in_proj_weight"]})
    out = cst("weights", "import", str(folder), "--as", "sample")
    assert out["ref"] == "sample@v1" and out["depth"] == 2 and out["sha256"] == WS.digest(tensors_of(a))
    assert [m["id"] for m in cst("weights", "models")] == ["tiny", "digits", "sample"]
    assert cst("weights", "show", "sample")["parts"] == cst("weights", "show", "sample@v1")["parts"]
    s = cst("weights", "open", "sample")
    assert s["base"] == "sample@v1" and s["ledger"]["total"] == out["parameters"]
    d = cst("weights", "layers")
    assert d["depth"] == 2 and d["parts"][0]["kind"] == "embedding"
    assert cst("weights", "read", "act.weight", "--rows", "0", "--cols", "0:2")["values"] == [[1.0, 1.0]]
    assert cst("weights", "nearest", "act.weight", "0")["nearest"][0]["row"] == 1
    assert cst("weights", "directions", "pos.weight")["needed_for_90"] == 1
    assert cst("weights", "attention", "--layer", "1", "--head", "2")["of"][0]["head"] == 1       # typed from 1, answered from 0
    assert cst("weights", "heatmap", "--tensor", "L0.self_attn.out_proj.weight")["grid_shape"] == [8, 8]
    assert cst("weights", "stats", "act.weight", "--neurons")["rows_of"] == 5
    assert cst("weights", "trees", "sample", "--ranker", "patc")["unused"][0]["name"] == "s1_never"
    r = RUN.invoke(cli, ["--json", "weights", "gradients", "--example", "1"])
    assert r.exit_code == 1 and R.NOT_YET in r.output
    assert R.NOT_YET in RUN.invoke(cli, ["weights", "forward", "--input", "0.1"]).output
    # what a person reads, without --json
    text = words("weights", "show", "sample")
    for want in ("session model with rankers: width 8, 2 layers, 2 heads", "action table (act): 5 by 8", "attention, layer 2",
                 "query: what this event is looking for: rows 0:8", "tree ranker full: 3 trees", "kept as facts, not tensors: cat_vocab, cfg, edges",
                 R.NOT_YET):
        assert want in text, want
    text = words("weights", "trees", "sample")
    for want in ("NOT USED: 1 of 5 inputs are never asked about", "s1_never", "These are trees, not neurons", "is f_views at most 0.5?",
                 "f_views: 1 question, 50.0% of the gain"):
        assert want in text, want
    for args in (("weights", "layers"), ("weights", "nearest", "cat.s.weight", "2"), ("weights", "directions", "act.weight"),
                 ("weights", "attention"), ("weights", "stats", "act.weight", "--neurons"), ("weights", "heatmap", "--tensor", "act.weight"),
                 ("weights", "import", str(folder), "--as", "sample"), ("weights", "models"), ("weights", "session")):
        assert words(*args).strip()
    assert "parts, not numbered layers" in words("weights", "read", "--layer", "1", code=1)


def test_what_a_person_reads_in_these_files_has_no_dashes_arrows_or_internal_words():
    here = Path(server.__file__).parent
    for f in ("weights/real.py", "weights/trees.py", "weights/real_routes.py", "cli/weights_real.py"):
        text = (here / f).read_text()
        assert not re.search(r"[—–←-⇿⟵-⟿]", text), f"{f} has a dash or an arrow a person could see"
        assert not re.search(r"\bengines?\b|\bdaemon\b", text, re.I), f"{f} says a word people should not see"


# ---- row names, input groups, rankers side by side, what can be brought in ----------------------------------------------------

def _labels(svc, name, **over):
    bring(svc, name=name, over=over)
    sid = svc.post("/api/weights/sessions", {"model": name})["id"]
    return sid, lambda tensor: svc.get(f"/api/weights/sessions/{sid}/labels", tensor=tensor)


def test_rows_are_named_from_a_list_one_short_and_the_extra_row_is_found_or_said_to_be_a_guess(svc):
    cat = arrays()["cat.s.weight"].copy()
    # the made-up layout as it comes: 6 names for 7 rows, 4 edges (5 ranges) for 6 price rows, and no row stands out
    sid, lab = _labels(svc, "guess")
    c = lab("cat.s.weight")
    has(c, "tensor", "rows", "labels", "labels_from", "labels_says", "extra_row", "sure")
    assert c["labels"] == ["none or unknown"] + [f"category {i}" for i in range(6)] and (c["extra_row"], c["sure"]) == (0, False)
    assert "guess" in c["labels_says"] and "6 names for 7 rows" in c["labels_says"]
    pr = lab("price.weight")
    assert pr["labels"] == ["none or unknown", "below 1.00", "1.00 to 2.00", "2.00 to 3.00", "3.00 to 4.00", "4.00 and up"]
    assert pr["labels_from"] == "edges" and "4 edges, which make 5 ranges for 6 rows" in pr["labels_says"] and pr["sure"] is False
    assert lab("c_price.weight")["labels"] == pr["labels"]
    assert lab("act.weight")["labels"] is None and lab("act.weight")["labels_says"] == ""
    # the names reach every reading that lists rows
    p = f"/api/weights/sessions/{sid}"
    assert svc.get(p + "/nearest", tensor="cat.s.weight", row=1)["label"] == "category 0"
    assert svc.get(p + "/stats", tensor="price.weight", neurons=1)["rows"][2]["label"] == "1.00 to 2.00"
    assert svc.get(p + "/directions", tensor="cat.s.weight")["directions"][0]["strongest_rows"][0]["label"]
    # a last row of zeros is the extra one, and that is sure
    last = cat.copy()
    last[-1] = 0
    c = _labels(svc, "last", **{"cat.s.weight": last})[1]("cat.s.weight")
    assert c["labels"] == [f"category {i}" for i in range(6)] + ["none or unknown"] and (c["extra_row"], c["sure"]) == (6, True)
    assert "near zero" in c["labels_says"]
    first = cat.copy()
    first[0] = 1e-6
    c = _labels(svc, "first", **{"cat.s.weight": first})[1]("cat.s.weight")
    assert c["labels"][0] == "none or unknown" and c["labels"][1] == "category 0" and (c["extra_row"], c["sure"]) == (0, True)
    # one name per row needs no guess, and a list of another length names nothing
    c = _labels(svc, "exact", cat_vocab=np.array([f"category {i}" for i in range(7)]), edges=np.linspace(1.0, 5.0, 5))[1]
    assert c("cat.s.weight")["labels"][0] == "category 0" and c("cat.s.weight")["extra_row"] is None and c("cat.s.weight")["sure"] is True
    assert c("price.weight")["labels"][0] == "below 1.00" and len(c("price.weight")["labels"]) == 6 and c("price.weight")["extra_row"] is None
    c = _labels(svc, "short", cat_vocab=np.array(["a", "b", "c"]))[1]("cat.s.weight")
    assert c["labels"] is None and "which row is which is not known" in c["labels_says"]
    for text in (pr["labels_says"], c["labels_says"]):
        plain(text)


def test_tree_inputs_are_grouped_by_the_start_of_their_names():
    s = T.summary(T.parse(TREES), COLS)
    assert [(g["prefix"], g["count"], g["used"], g["unused"], g["splits"], g["gain"], g["share"], g["inputs"]) for g in s["groups"]] == [
        ("f_", 2, 2, 0, 2, 14.0, 0.7, [0, 1]), ("m_", 1, 1, 0, 1, 5.0, 0.25, [2]), ("s1_", 2, 1, 1, 1, 1.0, 0.05, [3, 4])]
    assert [r["group"] for r in s["by_input"]] == ["f_", "m_", "f_", "s1_", "s1_"] and "groups_says" in s
    assert T.group_of("pf_ctr") == "pf_" and T.group_of("s1_pos") == "s1_" and T.group_of("plain") == "other"


def test_the_rankers_side_by_side_and_the_inputs_none_of_them_uses(svc):
    folder, _ = make_folder(svc.tmp / "three")
    # the second ranker stops asking about the input its third tree used: it is now unused there, and used by the others
    text = (folder / "weights" / "rank_patc.txt").read_text().replace("split_feature=3", "split_feature=1")
    (folder / "weights" / "rank_patc.txt").write_text(text)
    R.import_from(svc.ctx, str(folder), "sample")
    r = svc.get("/api/weights/models/sample/rankers")
    has(r, "model", "version", "says", "rankers", "inputs", "used_by_all", "used_by_some", "unused_by_all", "unused_by_all_says", "same_inputs")
    assert [(k["name"], k["trees"], k["inputs"], k["used"], k["unused"]) for k in r["rankers"]] == [
        ("full", 3, 5, 4, ["s1_never"]), ("patc", 3, 5, 3, ["s1_recency", "s1_never"]), ("pclick", 3, 5, 4, ["s1_never"])]
    assert r["unused_by_all"] == ["s1_never"] and r["used_by_some"] == ["s1_recency"] and r["used_by_all"] == ["f_views", "f_carts", "m_price"]
    assert "1 of 5 inputs are asked about by none of the 3 rankers" in r["unused_by_all_says"] and r["same_inputs"] is True
    rec = next(i for i in r["inputs"] if i["name"] == "s1_recency")
    assert rec["used_by"] == ["full", "pclick"] and rec["unused_by"] == ["patc"] and rec["by"]["patc"]["splits"] == 0 and rec["group"] == "s1_"
    assert rec["by"]["full"] == {"splits": 1, "gain": 1.0, "share": 0.05}
    assert "no tree rankers" in svc.get("/api/weights/models/tiny/rankers", code=400)["error"]
    plain(r["unused_by_all_says"])


def test_the_versions_under_a_folder_or_an_address_are_listed_and_nothing_is_written(svc, monkeypatch):
    root = svc.tmp / "registry"
    make_folder(root / "arch-a" / "shop" / "v1")
    make_folder(root / "arch-a" / "shop" / "v2", layers=2)
    make_folder(root / "arch-b" / "trail" / "v1")
    (root / "arch-a" / "shop" / "v2" / "MANIFEST.yaml").write_text("description: wider,  two layers\nversion: v2\n")
    (root / "arch-b" / "notes").mkdir()
    (root / "INDEX.md").write_text("made up")
    before = sorted(str(f) for f in root.rglob("*"))
    r = svc.get("/api/weights/registry", **{"from": str(root)})
    has(r, "from", "versions", "count", "listed", "read_only", "says")
    assert [v["path"] for v in r["versions"]] == ["arch-a/shop/v1", "arch-a/shop/v2", "arch-b/trail/v1"] and r["count"] == 3 and r["read_only"] is True
    v1, v2, _ = r["versions"]
    has(v1, "path", "address", "name", "description", "has_manifest", "files")
    assert v2["description"] == "wider, two layers" and v1["description"] == "architecture: made-up, version: v1, reproducibility: not-tested"
    assert v1["address"] == str(root / "arch-a" / "shop" / "v1") and v1["name"] == "v1" and v1["files"] == 10
    assert sorted(str(f) for f in root.rglob("*")) == before
    # an address a row gives is what import takes
    assert R.import_from(svc.ctx, v2["address"], "picked")["depth"] == 2
    empty = svc.tmp / "nothing"
    empty.mkdir()
    assert svc.get("/api/weights/registry", **{"from": str(empty)})["says"].startswith("No version was found here")
    assert "is not a folder" in svc.get("/api/weights/registry", code=400, **{"from": str(svc.tmp / "missing")})["error"]
    boto3, moto = pytest.importorskip("boto3"), pytest.importorskip("moto")
    for k, v in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"), ("AWS_DEFAULT_REGION", "us-east-1")):
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="made-up-models")
        for f in root.rglob("*"):
            if f.is_file():
                s3.put_object(Bucket="made-up-models", Key=f.relative_to(root).as_posix(), Body=f.read_bytes())
        keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="made-up-models")["Contents"])
        cloud = R.registry("s3://made-up-models/")
        assert sorted(o["Key"] for o in s3.list_objects_v2(Bucket="made-up-models")["Contents"]) == keys
        one = R.registry("s3://made-up-models/arch-b")
    assert [(v["path"], v["description"], v["files"]) for v in cloud["versions"]] == [(v["path"], v["description"], v["files"]) for v in r["versions"]]
    assert cloud["versions"][1]["address"] == "s3://made-up-models/arch-a/shop/v2" and one["versions"][0]["address"] == "s3://made-up-models/arch-b/trail/v1"


def test_the_new_commands_say_it_in_words(tmp_path):
    R._KINDS.clear()
    cst("init", "demo", "--storage", str(tmp_path / "bucket"))
    make_folder(tmp_path / "reg" / "arch" / "shop" / "v1")
    listed = cst("weights", "registry", str(tmp_path / "reg"))
    assert listed["count"] == 1
    cst("weights", "import", listed["versions"][0]["address"], "--as", "sample")
    cst("weights", "open", "sample")
    assert cst("weights", "labels", "price.weight")["labels"][1] == "below 1.00"
    assert cst("weights", "rankers", "sample")["unused_by_all"] == ["s1_never"]
    assert "score sharpness (logit_scale): one number. " in words("weights", "show", "sample")
    assert "row 1: below 1.00" in words("weights", "labels", "price.weight") and "numbered" in words("weights", "labels", "act.weight")
    assert "NOT USED BY ANY: 1 of 5 inputs" in words("weights", "rankers", "sample")
    assert "s1_: 2 inputs, 1 not used" in words("weights", "trees", "sample")
    assert "1 version can be brought in from here" in words("weights", "registry", str(tmp_path / "reg"))


# ---- changing it ------------------------------------------------------------------------------------------------------------------

def test_an_edit_is_an_exact_patch_that_is_never_measured_and_can_be_taken_back_to_the_bit(svc):
    out, a, _ = bring(svc)
    s = svc.post("/api/weights/sessions", {"model": "sample"})
    p = f"/api/weights/sessions/{s['id']}"
    start = WS.digest(tensors_of(a))
    digest = lambda: WSN.current_digest(svc.ctx, s["id"])["digest"]  # noqa: E731
    act = a["act.weight"]
    # set, scale, zero and add, on rows and columns, on whole rows, and on single positions
    e1 = svc.post(p + "/edits", {"op": "set", "tensor": "act.weight", "rows": "0", "cols": "0:3", "value": 0.5, "why": "try it"})
    has(e1, "id", "kind", "op", "tensor", "tensors", "why", "changes", "changed", "share", "undone", "measured", "not_measured", "sentence", "ledger")
    assert (e1["kind"], e1["op"], e1["changed"], e1["measured"], e1["not_measured"]) == ("edit", "set", 3, False, R.NOT_MEASURED)
    assert e1["changes"][0] == {"tensor": "act.weight", "index": [0, 0], "old": float(act[0, 0]), "new": 0.5}
    assert e1["sentence"] == "You set 3 numbers in act.weight. Not measured: this model cannot be run here yet." and "cost" not in e1
    e2 = svc.post(p + "/edits", {"op": "scale", "tensor": "act.weight", "neurons": [1], "factor": 2})
    assert e2["changed"] == 8 and svc.get(p + "/read", tensor="act.weight", rows="1", watch=1)["values"][0] == (act[1] * np.float32(2)).astype(float).tolist()
    e3 = svc.post(p + "/edits", {"op": "zero", "tensor": "L0.self_attn.in_proj_bias", "rows": "8:16"})
    e4 = svc.post(p + "/edits", {"op": "add", "tensor": "q.weight", "indices": [[0, 0], [7, 7]], "values": [1.0, -1.0]})
    e5 = svc.post(p + "/edits", {"op": "set", "tensor": "logit_scale", "value": 3})
    assert (e3["changed"], e4["changed"], e5["changed"]) == (8, 2, 1) and e5["changes"][0]["index"] == []
    led = svc.get(p)["ledger"]
    assert (led["changed"], led["total"], led["forward_used"], led["scores_shown"]) == (22, out["parameters"], 0, False)
    assert led["weights_alone"] is True and led["label"] == "from the weights alone"
    d = svc.get(p + "/diff")
    assert d["changed"] == 22 and {t["tensor"]: t["changed"] for t in d["by_tensor"]} == {
        "act.weight": 11, "L0.self_attn.in_proj_bias": 8, "q.weight": 2, "logit_scale": 1}
    # take one back from the middle, put it back, then take everything back: the exact numbers return
    u = svc.post(p + "/undo", {"edit": e2["id"]})
    assert (u["kind"], u["undoes"], u["changed"], u["measured"]) == ("undo", e2["id"], 8, False)
    assert svc.get(p + "/read", tensor="act.weight", rows="1", watch=1)["values"][0] == act[1].astype(float).tolist()
    assert svc.post(p + "/redo")["redoes"] == e2["id"] and svc.get(p)["ledger"]["changed"] == 22
    edited = digest()
    for _ in range(5):
        svc.post(p + "/undo")
    assert digest() == start and svc.get(p)["ledger"]["changed"] == 0 and svc.get(p + "/diff")["changed"] == 0
    assert "no edit to take back" in svc.post(p + "/undo", code=400)["error"]
    for _ in range(5):
        svc.post(p + "/redo")
    assert digest() == edited != start
    # an edit that overlaps a later one cannot be taken back first
    e6 = svc.post(p + "/edits", {"op": "add", "tensor": "act.weight", "rows": "0", "cols": "0", "value": 1})
    assert f"Take back edit {e6['id']} first" in svc.post(p + "/undo", {"edit": e1["id"]}, code=400)["error"]
    # what is refused, each in a sentence
    for body, want in (({"op": "step", "tensor": "act.weight"}, "set, scale, zero, add"), ({"op": "zero", "tensor": "nope"}, "no tensor named"),
                       ({"op": "set", "tensor": "act.weight", "rows": "0"}, "Give the number to write"),
                       ({"op": "set", "tensor": "act.weight", "rows": "0", "value": float("1e39")}, "not finite"),
                       ({"op": "zero", "tensor": "L0.self_attn.in_proj_bias", "rows": "8:16"}, "changes nothing"),
                       ({"op": "zero", "layer": 1, "neuron": 0, "tensor": "act.weight"}, "parts, not numbered layers"),
                       ({"op": "set", "tensor": "act.weight", "indices": [[9, 9]], "value": 1}, "A position names one number"),
                       ({"op": "set", "tensor": "act.weight", "indices": [[0, 0], [0, 1]], "values": [1, 2, 3]}, "gives 3 values")):
        err = svc.post(p + "/edits", body, code=400)["error"]
        assert want in err, err
        plain(err)
    agent = svc.post("/api/weights/sessions", {"model": "sample", "by": "agent", "name": "helper"})["id"]
    assert "needs `why`" in svc.post(f"/api/weights/sessions/{agent}/edits", {"op": "zero", "tensor": "act.weight", "rows": "0"}, code=400)["error"]
    # the base is untouched until a deliberate save
    _, base = WS.load(svc.ctx, "sample", 1)
    assert WS.digest(base) == start and WS.versions(svc.ctx, "sample") == [1]
    rep = svc.get(p + "/report")
    assert len(rep["kept"]) == 6 and rep["diff"]["changed"] == 22 and all(R.NOT_MEASURED in e["sentence"] for e in rep["events"] if e["kind"] == "edit")


def test_a_save_is_a_new_version_that_is_the_base_plus_the_edits_and_reads_like_any_other(svc):
    out, a, _ = bring(svc)
    s = svc.post("/api/weights/sessions", {"model": "sample"})
    p = f"/api/weights/sessions/{s['id']}"
    assert "nothing to save" in svc.post(p + "/save", code=400)["error"]
    svc.post(p + "/edits", {"op": "zero", "tensor": "cat.s.weight", "neurons": [6]})
    svc.post(p + "/edits", {"op": "scale", "tensor": "text.bias", "factor": 0.5})
    seq = svc.get("/api/changes")["seq"]
    saved = svc.post(p + "/save", {"note": "last category row cleared"})
    has(saved, "model", "version", "ref", "base", "patches", "changed", "share", "label", "weights_alone", "note", "measured", "not_measured", "sha256")
    assert (saved["ref"], saved["base"], saved["patches"], saved["changed"], saved["measured"]) == ("sample@v2", "sample@v1", 2, 16, False)
    assert saved["label"] == "from the weights alone" and saved["weights_alone"] is True
    assert {c["topic"] for c in svc.get("/api/changes", since=seq, topics="weights:sample", wait=0)["changed"]} == {"weights:sample"}
    # the new version holds no files: its manifest, the patches and the record. Its numbers are the base plus the edits.
    assert [k for k in svc.ctx.store.list("weights/sample/v2/")] == ["weights/sample/v2/manifest.json"]
    m2 = WS.manifest(svc.ctx, "sample", 2)
    assert (m2["base"], m2["files_at"], m2["kind"], m2["note"]) == ("sample@v1", "sample@v1", "session-ranker", "last category row cleared")
    assert m2["record"]["measured"] is False and m2["record"]["ledger"]["changed"] == 16 and len(m2["patches"]) == 2
    want = {k: np.array(v) for k, v in tensors_of(a).items()}
    want["cat.s.weight"][6] = 0
    want["text.bias"] = (want["text.bias"] * np.float32(0.5)).astype("float32")
    _, got = WS.load(svc.ctx, "sample", 2)
    assert WS.digest(got) == WS.digest(want) == saved["sha256"] == m2["sha256"]
    _, base = WS.load(svc.ctx, "sample", 1)
    assert WS.digest(base) == out["sha256"]
    # it is described, read, named and compared like the version it came from
    d = svc.get("/api/weights/models/sample")
    assert (d["version"], d["base"], d["width"], len(d["parts"])) == (2, "sample@v1", 8, len(svc.get("/api/weights/models/sample@v1")["parts"]))
    assert svc.get("/api/weights/models/sample@v2/trees")["unused"][0]["name"] == "s1_never"
    assert svc.get("/api/weights/models/sample/rankers")["unused_by_all"] == ["s1_never"]
    s2 = svc.post("/api/weights/sessions", {"model": "sample"})
    assert s2["base"] == "sample@v2"
    lab = svc.get(f"/api/weights/sessions/{s2['id']}/labels", tensor="cat.s.weight")
    assert (lab["extra_row"], lab["sure"]) == (6, True)            # the cleared row is now the one that stands out
    cmp = svc.get("/api/weights/compare", a="sample@v1", b="sample@v2")
    assert cmp["changed"] == 16 and {t["tensor"] for t in cmp["by_tensor"]} == {"cat.s.weight", "text.bias"}
    # a second line of edits on top of the saved one
    p2 = f"/api/weights/sessions/{s2['id']}"
    svc.post(p2 + "/edits", {"op": "set", "tensor": "logit_scale", "value": 1.5})
    assert svc.post(p2 + "/save")["ref"] == "sample@v3" and WS.manifest(svc.ctx, "sample", 3)["files_at"] == "sample@v1"
    _, got3 = WS.load(svc.ctx, "sample", 3)
    assert float(got3["logit_scale"]) == 1.5 and (got3["cat.s.weight"][6] == 0).all()
    # still nothing runs, and nothing deploys
    assert svc.post(p2 + "/evaluate", code=400) == {"error": R.NOT_YET}
    with pytest.raises(ValueError, match="not run yet"):
        PD.resolve(svc.ctx, "weights:sample@v3")


def test_cst_edits_takes_back_and_saves(tmp_path):
    R._KINDS.clear()
    cst("init", "demo", "--storage", str(tmp_path / "bucket"))
    folder, a = make_folder(tmp_path / "folder")
    cst("weights", "import", str(folder), "--as", "sample")
    cst("weights", "open", "sample")
    ev = cst("weights", "edit", "zero", "--tensor", "act.weight", "--rows", "0:2", "--cols", "0:4")
    assert ev["changed"] == 8 and ev["measured"] is False
    assert cst("weights", "edit", "scale", "--tensor", "pos.weight", "--neurons", "1", "--factor", "0.5")["changes"][0]["index"] == [0, 0]
    assert cst("weights", "undo")["undoes"] == "e3" and cst("weights", "diff")["changed"] == 8
    assert "Not measured: this model cannot be run here yet." in words("weights", "edit", "add", "--tensor", "intent.bias", "--values", "0.25")
    assert "9 of" in words("weights", "diff") and "taken back: e3 scale" in words("weights", "report")
    out = words("weights", "save", "--as", "two rows cleared")
    assert "saved sample@v2: sample@v1 plus 9 changed numbers" in out
    assert cst("weights", "show", "sample")["base"] == "sample@v1" and "sample v2" in words("weights", "show", "sample")


# ---- the older layout: enc.npz, item vectors, heads and layers stated in the file ------------------------------------------------

ITEMS = 20


def make_enc_folder(root: Path, seed: int = 3) -> tuple[Path, dict[str, np.ndarray]]:
    """A made-up version folder in the older layout: weights/enc.npz with three layers, the item vectors `cn`, heads
    and layers saved as single numbers, no candidate tensors but one, rankers under other names, and other files."""
    w = root / "weights"
    w.mkdir(parents=True, exist_ok=True)
    a = arrays(8, 3, seed)
    for k in [k for k in a if k.startswith(("c_text", "c_price", "c_cat", "cat.s")) or k == "cfg"]:
        del a[k]
    rng = np.random.default_rng(seed)
    a["cat.store.weight"] = rng.standard_normal((7, 8)).astype("float32")
    a["cat_vocab"] = np.array([f"category {i}" for i in range(7)])
    a["layers"], a["heads"] = np.int64(3), np.int64(2)
    a["logit_scale"] = np.float64(2.5)
    cn = rng.standard_normal((ITEMS, 8)).astype("float32")
    cn[1] = 3 * cn[0]            # item 1 points the same way as item 0
    a["cn"] = cn
    a["priceb"] = (np.arange(ITEMS) % 6).astype("int64")
    a["catid"] = (np.arange(ITEMS) % 7).astype("int64")
    np.savez(w / "enc.npz", **a)
    for name in ("conv", "full_nopcat_v1"):
        (w / f"rank_{name}.txt").write_text(TREES)
        (w / f"rank_{name}.cols.json").write_text(json.dumps(COLS))
    for name in ("blend.json", "icp.json", "offline_eval_a.json"):
        (w / name).write_text(json.dumps({"made_up": True}))
    (root / "MANIFEST.yaml").write_text("description: the older design\n")
    return root, a


def test_the_older_layout_is_listed_brought_in_described_and_read(svc):
    root = svc.tmp / "both"
    folder, a = make_enc_folder(root / "arch-old" / "shop" / "v1")
    make_folder(root / "arch-new" / "shop" / "v1")
    reg = svc.get("/api/weights/registry", **{"from": str(root)})
    assert [(v["path"], v["description"]) for v in reg["versions"]] == [
        ("arch-new/shop/v1", "architecture: made-up, version: v1, reproducibility: not-tested"), ("arch-old/shop/v1", "the older design")]
    assert R.looks_real(str(folder)) and R.looks_real(str(folder / "weights"))
    out = svc.post("/api/weights/import", {"folder": reg["versions"][1]["address"], "name": "older"})
    assert (out["width"], out["depth"], out["heads"], out["rankers"], out["tables"]) == (8, 3, 2, ["conv", "full_nopcat_v1"], [])
    m = WS.manifest(svc.ctx, "older")
    for f in sorted((folder / "weights").iterdir()):
        assert svc.ctx.store.get(f"weights/older/v1/files/{f.name}") == f.read_bytes()
    # float64 and whole-number arrays: the float64 scalar is a tensor kept as float64, the whole numbers are facts
    want = tensors_of(a)
    assert "cn" in want and "logit_scale" in want and not {"layers", "heads", "priceb", "catid"} & set(want)
    _, got = WS.load(svc.ctx, "older")
    assert got["logit_scale"].dtype == np.float64 and got["cn"].tobytes() == a["cn"].tobytes() and WS.digest(got) == WS.digest(want) == m["sha256"]
    assert (m["facts"]["heads"]["value"], m["facts"]["layers"]["value"], m["facts"]["catid"]["value"][:3]) == (2, 3, [0, 1, 2])
    assert m["other_files"] == ["blend.json", "icp.json", "offline_eval_a.json"] and m["model_json"] is None
    # the description: three layers, heads as the file says, the item vectors as their own part, no candidate text or price
    d = svc.get("/api/weights/models/older")
    assert (d["width"], d["depth"], d["heads"], d["heads_from"], d["session_file"]) == (8, 3, 2, "the file", "enc.npz")
    assert d["heads_says"].startswith("2 heads, as the file says") and d["declared"] == {
        "session_model": None, "layers": 3, "layers_from": "the file", "layers_agree": True}
    parts = {p["id"]: p for p in d["parts"]}
    assert sorted(t for p in d["parts"] for t in p["tensors"]) == sorted(want) and not [p for p in d["parts"] if p["kind"] == "other"]
    cn = parts["cn"]
    assert (cn["kind"], cn["name"], cn["rows"], cn["width"], cn["side"], cn["editable"]) == ("items", "item vectors", ITEMS, 8, "candidate", True)
    assert "worked out ahead of time: one row" in cn["says"]
    assert {p["id"] for p in d["parts"] if p["side"] == "candidate"} == {"cn", "c_rep"}
    assert parts["cat.store"]["name"] == "category table" and parts["logit_scale"]["editable"] is False and parts["act"]["editable"] is True
    assert [p["layer"] for p in d["parts"] if p["kind"] == "attention"] == [0, 1, 2] and parts["q"]["feeds_from"] == ["L2.norm2"]
    assert [f["name"] for f in d["other_files"]] == m["other_files"] and all(f["says"] == "Kept unchanged. Not read." for f in d["other_files"])
    assert {f["name"] for f in d["facts"]} == {"cat_vocab", "catid", "edges", "heads", "layers", "priceb"}
    # reading it
    sid = svc.post("/api/weights/sessions", {"model": "older"})["id"]
    p = f"/api/weights/sessions/{sid}"
    att = svc.get(p + "/attention", layer=2)
    assert (att["part"], att["heads"], att["heads_from"], len(att["of"])) == ("L2.self_attn", 2, "the file", 2)
    assert {t["table"] for t in att["of"][0]["reads"]} == {"act", "gap", "dur", "price", "rep", "pos", "cat.store"}     # not the item vectors
    assert "the layers before this one" in att["reads_says"]
    n = svc.get(p + "/nearest", tensor="cn", row=0, top=3)
    assert n["rows"] == ITEMS and n["label"] == "item 0, price bucket 0, category category 0"
    assert n["nearest"][0]["row"] == 1 and n["nearest"][0]["cosine"] == pytest.approx(1.0) and n["nearest"][0]["label"] == "item 1, price bucket 1, category category 1"
    lab = svc.get(p + "/labels", tensor="cn")
    assert len(lab["labels"]) == ITEMS and lab["labels"][13] == "item 13, price bucket 1, category category 6" and lab["labels_from"] == "priceb, catid"
    assert "named from the rows of cat.store.weight" in lab["labels_says"]
    assert svc.get(p + "/labels", tensor="cat.store.weight")["labels"][6] == "category 6"
    assert svc.get(p + "/labels", tensor="price.weight")["labels"][1] == "below 1.00"
    assert svc.get(p + "/directions", tensor="cn")["directions"][0]["strongest_rows"][0]["label"].startswith("item ")
    assert svc.get(p + "/read", tensor="logit_scale")["dtype"] == "float64"
    r = svc.get("/api/weights/models/older/rankers")
    assert [k["name"] for k in r["rankers"]] == ["conv", "full_nopcat_v1"] and r["unused_by_all"] == ["s1_never"]
    assert svc.get("/api/weights/models/older/trees", ranker="full_nopcat_v1")["trees"] == 3
    # only float32 is edited: the float64 scalar is refused in a sentence, an item's row is changed and taken back
    err = svc.post(p + "/edits", {"op": "set", "tensor": "logit_scale", "value": 3}, code=400)["error"]
    assert "stored as float64" in err and "left alone" in err
    plain(err)
    assert svc.post(p + "/edits", {"op": "zero", "tensor": "cn", "neurons": [5]})["changed"] == 8
    svc.post(p + "/undo")
    assert WSN.current_digest(svc.ctx, sid)["digest"] == m["sha256"]


def test_item_rows_are_numbered_by_category_when_names_cannot_be_matched_and_bare_without_the_lists(svc):
    folder, a = make_enc_folder(svc.tmp / "plain")
    del a["cat_vocab"], a["priceb"]
    np.savez(folder / "weights" / "enc.npz", **a)
    R.import_from(svc.ctx, str(folder), "plain")
    sid = svc.post("/api/weights/sessions", {"model": "plain"})["id"]
    lab = svc.get(f"/api/weights/sessions/{sid}/labels", tensor="cn")
    assert lab["labels"][8] == "item 8, category 1" and "Categories are numbered" in lab["labels_says"]
    del a["catid"], a["heads"]
    np.savez(folder / "weights" / "enc.npz", **a)
    R.import_from(svc.ctx, str(folder), "bare")
    sid = svc.post("/api/weights/sessions", {"model": "bare"})["id"]
    assert svc.get(f"/api/weights/sessions/{sid}/labels", tensor="cn")["labels"][8] == "item 8"
    assert R.whole(svc.ctx, "bare")["heads"] is None


def test_cst_reads_the_older_layout(tmp_path):
    R._KINDS.clear()
    cst("init", "demo", "--storage", str(tmp_path / "bucket"))
    folder, _ = make_enc_folder(tmp_path / "old")
    assert cst("weights", "registry", str(tmp_path))["versions"][0]["path"] == "old"
    assert cst("weights", "import", str(folder), "--as", "older")["depth"] == 3
    cst("weights", "open", "older")
    text = words("weights", "show", "older")
    for want in ("width 8, 3 layers, 2 heads", "2 heads, as the file says", "item vectors (cn): 20 by 8", "attention, layer 3",
                 "tree ranker conv: 3 trees", "tree ranker full_nopcat_v1", "other files, kept unchanged and not read: blend.json, icp.json, offline_eval_a.json"):
        assert want in text, want
    assert cst("weights", "attention", "--layer", "3")["layer"] == 2
    assert "item 1, price bucket 1, category category 1" in words("weights", "nearest", "cn", "0")
    assert "stored as float64" in words("weights", "edit", "set", "--tensor", "logit_scale", "--values", "3", code=1)
