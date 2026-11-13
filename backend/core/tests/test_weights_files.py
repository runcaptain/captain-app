"""Model files: safetensors and npz round trips with numpy alone, the manifest, and the two starter networks."""
from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from captain_search import projects as PR
from captain_search.weights import files as F
from captain_search.weights import net as N
from captain_search.weights import starters as ST
from captain_search.weights import store as WS
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    return init_workspace("t", str(tmp_path / "store"))


def test_safetensors_round_trips_every_number_and_has_the_published_layout():
    rng = np.random.default_rng(0)
    t = {"a.weight": rng.standard_normal((3, 2)).astype("float32"), "a.bias": rng.standard_normal(3), "n": np.arange(4, dtype="int64")}
    raw = F.safetensors_dumps(t, {"made_by": "test"})
    (n,) = struct.unpack("<Q", raw[:8])
    head = json.loads(raw[8:8 + n])
    assert head["a.weight"] == {"dtype": "F32", "shape": [3, 2], "data_offsets": [0, 24]} and head["a.bias"]["dtype"] == "F64"
    assert n % 8 == 0 and head["__metadata__"] == {"made_by": "test"}
    assert raw[8 + n:8 + n + 24] == t["a.weight"].tobytes()
    back, meta = F.safetensors_loads(raw)
    assert meta == {"made_by": "test"} and list(back) == list(t)
    for k in t:
        assert back[k].dtype == t[k].dtype and np.array_equal(back[k], t[k])


@pytest.mark.parametrize("raw,words", [
    (b"abc", "shorter than"),
    (struct.pack("<Q", 999) + b"{}", "does not fit the file"),
    (struct.pack("<Q", 4) + b"nope", "not JSON"),
    (struct.pack("<Q", 62) + json.dumps({"w": {"dtype": "F32", "shape": [4], "data_offsets": [0, 16]}}).encode().ljust(62) + b"1234", "does not fit the bytes"),
])
def test_a_broken_safetensors_file_is_refused_in_a_sentence(raw, words):
    with pytest.raises(N.WeightsError, match=words):
        F.safetensors_loads(raw)


def test_npz_round_trips_and_a_file_that_is_not_one_is_refused():
    t = {"w": np.arange(6, dtype="float32").reshape(2, 3)}
    assert np.array_equal(F.npz_loads(F.npz_dumps(t))["w"], t["w"])
    with pytest.raises(N.WeightsError, match="not an npz file"):
        F.npz_loads(b"not a zip")


@pytest.mark.parametrize("fname", ["tensors.safetensors", "tensors.npz"])
def test_a_model_folder_round_trips_through_either_file_type(tmp_path, fname):
    net = N.init([4, 5, 3], ["tanh", "softmax"], "cross_entropy", seed=1)
    m = F.write_model(str(tmp_path / "m"), net, "mine", file=fname, inputs={"meaning": "four measurements"})
    assert m["sizes"] == [4, 5, 3] and m["parameters"] == 43 and (tmp_path / "m" / fname).exists()
    m2, tensors = F.read_model(str(tmp_path / "m"))
    back = F.network_from(m2, tensors)
    x = np.random.default_rng(2).standard_normal((5, 4))
    assert np.allclose(N.predict(back, x), N.predict(net, x), atol=1e-5)
    assert m2["inputs"]["meaning"] == "four measurements" and [ly["activation"] for ly in m2["layers"]] == ["tanh", "softmax"]


def test_a_manifest_that_does_not_fit_its_tensors_is_refused_in_a_sentence(tmp_path):
    net = N.init([2, 3, 1], seed=0)
    m = F.manifest_for(net, "x")
    with pytest.raises(N.WeightsError, match="not in the tensor file"):
        F.network_from(m, {"layer1.weight": net.layers[0].weight})
    bad = {**net.tensors(), "layer1.bias": np.zeros(4)}
    with pytest.raises(N.WeightsError, match="manifest says"):
        F.network_from(m, bad)
    with pytest.raises(N.WeightsError, match="format should be"):
        F.check_manifest({**m, "format": "other"})
    with pytest.raises(N.WeightsError, match="inside the model's own folder"):
        F.check_manifest({**m, "tensors": {**m["tensors"], "layer1.bias": {"file": "../x.npz"}}})
    with pytest.raises(N.WeightsError, match="no manifest.json"):
        F.read_model(str(tmp_path))


def test_the_synthetic_digits_repeat_with_a_seed_and_look_like_pictures():
    a, la = ST.digits(30, 5)
    b, lb = ST.digits(30, 5)
    c, _ = ST.digits(30, 6)
    assert np.array_equal(a, b) and np.array_equal(la, lb) and not np.array_equal(a, c)
    assert a.shape == (30, 784) and a.min() >= 0 and a.max() <= 1 and sorted(set(la.tolist())) == list(range(10))
    assert 0.03 < a.mean() < 0.3   # mostly empty, with a stroke of ink


def test_the_two_starters_are_made_by_the_code_on_first_use_and_say_their_data_is_synthetic(ctx):
    rows = WS.models(ctx)
    assert [r["id"] for r in rows[:2]] == ["tiny", "digits"]
    tiny, digits = rows[0], rows[1]
    assert tiny["sizes"] == [2, 3, 1] and tiny["parameters"] == 13
    assert digits["sizes"] == [784, 16, 16, 10] and digits["parameters"] == 13002
    assert digits["synthetic"] and "not handwriting" in digits["data_note"] and "Synthetic" in digits["data_note"]
    assert WS.versions(ctx, "digits") == [1]
    WS.models(ctx)   # a second look makes nothing new
    assert WS.versions(ctx, "digits") == [1] and WS.versions(ctx, "tiny") == [1]

    m, tensors = WS.load(ctx, "digits")
    assert tensors["layer1.weight"].dtype == np.float32 and m["tensors"]["layer1.weight"]["file"].endswith(".safetensors")
    net = F.network_from(m, tensors)
    x, y = WS.sets(ctx, m, "held_out")
    assert N.score(net, x, y) > 0.9, "the digits starter reads its own synthetic pictures"
    m, tensors = WS.load(ctx, "tiny")
    x, y = WS.sets(ctx, m, "held_out")
    assert N.score(F.network_from(m, tensors), x, y) > 0.95


def test_the_whole_network_carries_practice_examples_and_never_the_held_out_ones(ctx):
    m, tensors = WS.load(ctx, "tiny")
    w = WS.whole(m, tensors, ctx)
    assert w["sizes"] == [2, 3, 1] and len(w["layers"]) == 2 and len(w["layers"][0]["W"]) == 3 and len(w["layers"][0]["W"][0]) == 2
    assert w["test"] == [] and len(w["examples"]) == 24 and set(w["examples"][0]) == {"name", "x", "y"}
    assert w["inputNames"] == ["x1", "x2"] and w["squish"] == "sigmoid" and w["synthetic"] is True
    d = WS.whole(*WS.load(ctx, "digits"), ctx)
    assert d["inputShape"] == [28, 28] and len(d["examples"][0]["y"]) == 10 and d["examples"][0]["name"].startswith("practice 0: ")


def test_a_folder_from_any_trainer_comes_in_as_a_version_and_can_be_put_in_a_project(ctx, tmp_path):
    net = N.init([3, 4, 2], "relu", "squared_error", seed=3)
    F.write_model(str(tmp_path / "out"), net, "trail-ranker", file="tensors.npz")
    m = WS.import_folder(ctx, str(tmp_path / "out"))
    assert m["version"] == 1 and m["name"] == "trail-ranker"
    assert WS.import_folder(ctx, str(tmp_path / "out"))["version"] == 2
    _, tensors = WS.load(ctx, "trail-ranker", 2)
    assert np.allclose(tensors["layer1.weight"], net.layers[0].weight, atol=1e-6)
    PR.create(ctx, "trail")
    WS.assign(ctx, "trail-ranker", "trail")
    assert [r["id"] for r in WS.models(ctx, project="trail")] == ["trail-ranker"]
    with pytest.raises(N.WeightsError, match="lower-case"):
        WS.import_folder(ctx, str(tmp_path / "out"), name="Bad Name")
