"""Weights sessions: the record of what was allowed and used, edits as patches, exact undo, the three sets, drift."""
from __future__ import annotations

import numpy as np
import pytest

from captain_search.weights import files as F
from captain_search.weights import net as N
from captain_search.weights import session as S
from captain_search.weights import store as WS
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    return init_workspace("t", str(tmp_path / "store"))


def _now(ctx, sid):
    st = S._load(ctx, sid)
    return {k: t.copy() for k, t in st.cur.items()}, st


def test_a_session_records_who_opened_it_and_what_they_were_allowed(ctx):
    s = S.open_session(ctx, "tiny", by="agent", name="claude", forward_passes=4, sandbox=False)
    assert s["id"].startswith("ws-") and s["model"] == "tiny" and s["base"] == "tiny@v1"
    assert s["who"] == {"kind": "agent", "name": "claude"}
    led = s["ledger"]
    assert (led["raw_weights"], led["sandbox"], led["forward_allowed"], led["forward_used"]) == (True, False, 4, 0)
    assert led["changed"] == 0 and led["total"] == 13 and led["weights_alone"] is True and led["label"] == "from the weights alone"
    assert s["events"][0]["kind"] == "open" and s["events"][0]["by"] == "agent" and "4 forward passes" in s["events"][0]["sentence"]
    assert S.session(ctx, s["id"])["ledger"] == led and S.session_for(ctx, "tiny")["id"] == s["id"]


def test_weights_alone_is_worked_out_from_the_record_and_no_caller_can_set_it(ctx):
    plain = S.open_session(ctx, "tiny", by="agent", forward_passes=2, feedback=False)["id"]
    S.read(ctx, plain, "layer1.weight")
    S.stats(ctx, plain, neurons=True)
    S.edit(ctx, plain, {"op": "set", "layer": 1, "neuron": 0, "from": None, "value": 0.5, "why": "a guess", "weights_alone": True})
    led = S.session(ctx, plain)["ledger"]
    assert led["weights_alone"] is True and led["label"] == "from the weights alone" and led["weights_read"] == 6
    S.forward(ctx, plain, inputs=[[0.9, 0.1]])
    led = S.session(ctx, plain)["ledger"]
    assert led["weights_alone"] is False and led["label"] == "with 1 of 2 forward passes"
    # The stored record has no such field to tamper with: it is computed on every read.
    assert "weights_alone" not in S._load(ctx, plain).doc and "weights_alone" not in S._load(ctx, plain).doc["used"]

    boxed = S.open_session(ctx, "tiny", by="agent", sandbox=True)["id"]
    led = S.session(ctx, boxed)["ledger"]
    assert led["weights_alone"] is False and led["label"] == "from the weights, with code, network not run"


def test_a_forward_pass_is_counted_and_refused_past_the_allowance_and_the_refusal_is_recorded(ctx):
    sid = S.open_session(ctx, "tiny", by="agent", forward_passes=2, pass_inputs=2)["id"]
    r = S.forward(ctx, sid, inputs=[[0.9, 0.1], [0.9, 0.9]], targets=[1, 0])
    assert r["predicted"] == [1, 0] and r["forward_used"] == 1 and r["forward_left"] == 1 and r["cost"] < 0.1
    with pytest.raises(S.Refused, match="at most 2 inputs"):
        S.forward(ctx, sid, inputs=[[0.9, 0.1]] * 3)
    a = S.activations(ctx, sid, example=0)
    assert a["forward_left"] == 0 and len(a["columns"]) == 2 and len(a["columns"][0]["a"]) == 3 and "wanted" in a
    for call in (lambda: S.forward(ctx, sid, example=1), lambda: S.gradients(ctx, sid, example=1), lambda: S.trace(ctx, sid, example=1)):
        with pytest.raises(S.Refused, match="All 2 forward passes"):
            call()
    doc = S._load(ctx, sid).doc
    assert doc["used"]["forward_used"] == 2 and [p["op"] for p in doc["used"]["passes"]] == ["forward", "activations"]
    assert [e["kind"] for e in doc["events"]].count("refused") == 4
    none = S.open_session(ctx, "tiny", by="agent")["id"]
    with pytest.raises(S.Refused, match="no forward passes"):
        S.forward(ctx, none, example=0)


def test_raw_weights_are_refused_when_not_allowed_and_a_watching_screen_is_not_counted(ctx):
    sid = S.open_session(ctx, "tiny", by="agent", raw_weights=False)["id"]
    for call in (lambda: S.read(ctx, sid, "layer1.weight"), lambda: S.heatmap(ctx, sid), lambda: S.template(ctx, sid, 0)):
        with pytest.raises(S.Refused, match="may not see raw weights"):
            call()
    assert S.stats(ctx, sid)["tensors"][0]["count"] == 6   # numbers about the numbers are not raw weights
    before = len(S._load(ctx, sid).doc["events"])
    assert len(S.heatmap(ctx, sid, watch=True)["grids"]) == 3
    assert S.whole(ctx, "tiny", sid)["layers"][0]["W"]
    doc = S._load(ctx, sid).doc
    assert len(doc["events"]) == before and doc["used"]["weights_read"] == 0 and doc["used"]["forward_used"] == 0
    # A look by the screen is no event, and it is not traceless either: it is counted beside the record.
    led = S.session(ctx, sid)["ledger"]
    assert led["screen_looks"] == 2 and led["screen_numbers"] == 9 + 13
    # Nothing that runs the network can be done as a look.
    for run in (S.forward, S.activations, S.gradients, S.trace, S.cost_slice):
        assert "watch" not in run.__code__.co_varnames[:run.__code__.co_argcount + run.__code__.co_kwonlyargcount]


def test_gradients_match_the_network_code_and_mark_the_session(ctx):
    sid = S.open_session(ctx, "tiny", forward_passes=5)["id"]
    g = S.gradients(ctx, sid, inputs=[[0.9, 0.2]], targets=[[1]], layer=0, neuron=1)
    m, t = WS.load(ctx, "tiny")
    want = N.backprop(F.network_from(m, t), [[0.9, 0.2]], [[1]])
    assert np.allclose(g["layers"][0]["weight"], want["layers"][0]["weight"]) and g["cost"] == pytest.approx(want["cost"])
    assert set(g["layers"][0]) >= {"weight", "bias", "dcost_da", "wanted_nudge", "dcost_dz", "da_dz"}
    assert g["row"]["gradient"] == pytest.approx(want["layers"][0]["weight"][1].tolist()) and g["top"][0]["tensor"]
    tr = S.trace(ctx, sid, example=0, path=[0, 2, 0])
    for step in tr["steps"]:
        assert step["a_before"] * step["da_dz"] * step["dcost_da"] == pytest.approx(step["gradient"])
    led = S.session(ctx, sid)["ledger"]
    assert led["gradients_computed"] is True and led["forward_used"] == 2
    big = S.open_session(ctx, "digits", forward_passes=1)["id"]
    g = S.gradients(ctx, big, example=0)
    assert g["layers"][0]["weight"] is None and len(g["layers"][1]["weight"]) == 16 and len(g["top"]) == 20


def test_every_kind_of_edit_is_a_patch_with_old_and_new_numbers_and_the_share_comes_from_the_tensors(ctx):
    sid = S.open_session(ctx, "tiny")["id"]
    base, _ = _now(ctx, sid)
    ev = S.edit(ctx, sid, {"op": "set", "layer": 1, "neuron": 2, "from": None, "value": -1.5, "why": "turn it down"})
    assert ev["kind"] == "edit" and ev["op"] == "set" and ev["tensor"] == "layer1.bias" and ev["by"] == "you" and ev["why"] == "turn it down"
    assert ev["changes"] == [{"tensor": "layer1.bias", "k": 0, "j": 2, "i": None, "old": float(base["layer1.bias"][2]), "new": -1.5}]
    assert ev["touched"] == {"neurons": [[1, 2]], "weights": []} and ev["changed"] == 1 and ev["share"] == round(1 / 13, 6)
    assert "set 1 number in layer1.bias (layer 1, neuron 3)" in ev["sentence"]

    ev = S.edit(ctx, sid, {"op": "scale", "layer": 1, "neuron": 0, "factor": 0.5})
    assert ev["touched"]["weights"] == [[0, 0, 0], [0, 0, 1]] and [c["new"] for c in ev["changes"]] == [float(np.float32(v * 0.5)) for v in base["layer1.weight"][0]]
    ev = S.edit(ctx, sid, {"op": "zero", "layer": 2, "neuron": 0})
    assert ev["changed"] == 4 and ev["tensors"] == ["layer2.weight", "layer2.bias"]
    ev = S.edit(ctx, sid, {"op": "add", "tensor": "layer1.weight", "indices": [[1, 0], [1, 1]], "values": [0.25, -0.25]})
    assert [c["new"] - c["old"] for c in ev["changes"]] == pytest.approx([0.25, -0.25])
    ev = S.edit(ctx, sid, {"op": "add-direction", "tensor": "layer1.weight", "direction": [1, -1], "scale": 0.5, "neurons": [1, 2]})
    cur, st = _now(ctx, sid)
    assert ev["changed"] == 4 and cur["layer1.weight"][2].tolist() == pytest.approx((base["layer1.weight"][2] + [0.5, -0.5]).tolist())
    ev = S.edit(ctx, sid, {"op": "add_direction", "tensor": "layer1.weight", "direction": [1, 0, 0], "along": "outputs", "from_neurons": [1]})
    cur2, st = _now(ctx, sid)
    assert ev["changed"] == 1 and cur2["layer1.weight"][0, 1] == pytest.approx(cur["layer1.weight"][0, 1] + 1)

    # The stored patch holds exact old and new numbers, and the share is counted from the tensors.
    p = st.patch(1)
    assert p["parts"] == [{"tensor": "layer1.bias", "indices": [2], "old": [float(base["layer1.bias"][2])], "new": [-1.5]}]
    led = S.ledger(st)
    truly = sum(int((cur2[k] != base[k]).sum()) for k in base)
    assert led["changed"] == truly == S.diff(ctx, sid)["changed"] and led["share"] == round(truly / 13, 6)


@pytest.mark.parametrize("req,words", [
    ({"op": "melt", "layer": 1, "neuron": 0}, "An edit is one of"),
    ({"op": "set", "layer": 1, "neuron": 7, "value": 1}, "`neuron` goes from 0 to 2"),
    ({"op": "set", "layer": 0, "neuron": 0, "value": 1}, "in layer 1 to 2, and `layer` was 0"),
    ({"op": "set", "layer": 1, "neuron": 0, "from": 5, "value": 1}, "`from` goes from 0 to 1"),
    ({"op": "set", "layer": 1, "neuron": 0}, "needs `value`"),
    ({"op": "set", "layer": 1, "neuron": 0, "value": float("nan")}, "finite"),
    ({"op": "set", "tensor": "nope", "indices": [[0]], "values": [1]}, "no tensor named"),
    ({"op": "set", "tensor": "layer1.weight", "indices": [[9, 9]], "values": [1]}, "outside layer1.weight"),
    ({"op": "set", "tensor": "layer1.weight", "indices": [[0, 0], [0, 1]], "values": [1, 2, 3]}, "that many finite values"),
    ({"op": "add_direction", "tensor": "layer1.weight", "direction": [1, 2, 3]}, "needs 2 finite numbers"),
    ({"op": "add_direction", "tensor": "layer1.bias", "direction": [1, 2, 3]}, "not to a bias"),
    ({"op": "scale", "layer": 1, "neuron": 0, "factor": 1}, "changes nothing"),
])
def test_an_edit_that_cannot_be_made_is_refused_in_a_sentence_and_changes_nothing(ctx, req, words):
    sid = S.open_session(ctx, "tiny")["id"]
    with pytest.raises(N.WeightsError, match=words):
        S.edit(ctx, sid, req)
    led = S.session(ctx, sid)["ledger"]
    assert led["changed"] == 0 and S.session(ctx, sid)["edits"] == []


def test_an_agents_edit_needs_its_one_sentence_first(ctx):
    sid = S.open_session(ctx, "tiny", by="agent")["id"]
    with pytest.raises(N.WeightsError, match="needs `why`"):
        S.edit(ctx, sid, {"op": "zero", "layer": 1, "neuron": 0})
    assert S.edit(ctx, sid, {"op": "zero", "layer": 1, "neuron": 0, "by": "you"})["by"] == "you"   # the person at the screen


def test_undo_and_redo_put_back_exact_numbers_and_the_edit_stays_in_the_record(ctx):
    sid = S.open_session(ctx, "digits")["id"]
    base, _ = _now(ctx, sid)
    rng = np.random.default_rng(0)
    e1 = S.edit(ctx, sid, {"op": "add_direction", "tensor": "layer1.weight", "direction": rng.standard_normal(784).tolist(), "scale": 0.013})
    e2 = S.edit(ctx, sid, {"op": "scale", "layer": 2, "neuron": 3, "factor": 1.0 / 3.0})
    e3 = S.edit(ctx, sid, {"op": "set", "layer": 3, "neuron": 9, "from": None, "value": 0.1})
    after, _ = _now(ctx, sid)
    assert e1["changed"] == 16 * 784 and len(e1["changes"]) == 200 and len(e1["touched"]["weights"]) == 200

    u3, u2, u1 = S.undo(ctx, sid), S.undo(ctx, sid), S.undo(ctx, sid)
    assert (u3["undoes"], u2["undoes"], u1["undoes"]) == (e3["id"], e2["id"], e1["id"]) and u1["kind"] == "undo"
    back, st = _now(ctx, sid)
    assert all(np.array_equal(back[k], base[k]) for k in base), "three undos give the base back bit for bit"
    assert S.ledger(st)["changed"] == 0
    with pytest.raises(N.WeightsError, match="no edit to take back"):
        S.undo(ctx, sid)

    r1, r2, r3 = S.redo(ctx, sid), S.redo(ctx, sid), S.redo(ctx, sid)
    assert (r1["redoes"], r2["redoes"], r3["redoes"]) == (e1["id"], e2["id"], e3["id"])
    again, _ = _now(ctx, sid)
    assert all(np.array_equal(again[k], after[k]) for k in after), "three redos give the edited numbers back bit for bit"
    with pytest.raises(N.WeightsError, match="no edit to put back"):
        S.redo(ctx, sid)

    S.undo(ctx, sid, e2["id"])   # an edit that is not the last, with nothing later on the same numbers
    s = S.session(ctx, sid)
    assert [e["state"] for e in s["edits"]] == ["applied", "undone", "applied"]
    marked = {e["id"]: e.get("undone") for e in S.events(ctx, sid)["events"] if e["kind"] == "edit"}
    assert marked == {e1["id"]: False, e2["id"]: True, e3["id"]: False}
    rep = S.report(ctx, sid)
    assert [e["id"] for e in rep["kept"]] == [e1["id"], e3["id"]] and [e["id"] for e in rep["taken_back"]] == [e2["id"]]


def test_an_edit_under_a_later_one_on_the_same_numbers_cannot_be_taken_back_first(ctx):
    sid = S.open_session(ctx, "tiny")["id"]
    a = S.edit(ctx, sid, {"op": "set", "layer": 1, "neuron": 0, "from": 0, "value": 1.0})
    b = S.edit(ctx, sid, {"op": "scale", "layer": 1, "neuron": 0, "factor": 2.0})
    with pytest.raises(N.WeightsError) as e:
        S.undo(ctx, sid, a["id"])
    assert str(e.value) == f"Take back edit {b['id']} first: it changed the same numbers after edit {a['id']}."
    S.undo(ctx, sid, b["id"])
    S.undo(ctx, sid, a["id"])
    S.edit(ctx, sid, {"op": "set", "layer": 1, "neuron": 0, "from": 0, "value": 9.0})
    with pytest.raises(N.WeightsError, match="started from have changed"):
        S.redo(ctx, sid, a["id"])


def test_an_edit_is_measured_on_three_named_sets_before_and_after(ctx):
    sid = S.open_session(ctx, "digits", by="agent")["id"]
    sets = S.set_sets(ctx, sid, target={"classes": [9]}, should_not_change={"classes": [0, 1, 2, 3, 4, 5, 6, 7, 8]})
    assert sets["target"]["of"] == 40 and sets["should_not_change"]["of"] == 360 and sets["held_out"]["of"] == 400
    ev = S.edit(ctx, sid, {"op": "set", "layer": 3, "neuron": 9, "from": None, "value": -30.0, "why": "switch the nine output off"})
    c = ev["cost"]
    assert set(c) == {"target", "keep", "test"} and (c["target"]["name"], c["keep"]["name"], c["test"]["name"]) == ("target", "should not change", "held out")
    assert c["target"]["of"] == 40 and c["target"]["right_before"] == 40 and c["target"]["right_after"] == 0 and c["target"]["answers_changed"] == 40
    assert c["target"]["after"] > c["target"]["before"] + 0.5
    assert c["keep"]["right_after"] == c["keep"]["right_before"] and c["keep"]["answers_changed"] == 0
    assert c["test"]["right_before"] - c["test"]["right_after"] == 40 and c["test"]["of"] == 400
    assert ev["cost_before"] == c["target"]["before"] and ev["cost_after"] == c["target"]["after"] and "Cost on the target went from" in ev["sentence"]
    assert "(layer 3, neuron 10)" in ev["sentence"]

    r = S.evaluate(ctx, sid)
    assert list(r["sets"]) == ["target", "should_not_change", "held_out"] and r["before_is"] == "digits@v1"
    t = r["sets"]["target"]
    assert t["defined"] and t["right_before"] == 40 and t["right_after"] == 0 and "classes 9" in t["says"]
    # Tool-run evaluations are counted apart, never return an example, and do not cost a forward pass.
    led = S.session(ctx, sid)["ledger"]
    assert led["evaluations"] == 1 and led["forward_used"] == 0
    assert "inputs" not in str(r) and "examples" not in r

    # The examples shown with a network are for looking at. They cannot be made into a scored set.
    with pytest.raises(N.WeightsError, match="never scored"):
        S.set_sets(ctx, sid, target={"examples": [3]})
    fresh = S.open_session(ctx, "digits")
    assert fresh["sets"]["target"] == {"defined": False} and fresh["sets"]["should_not_change"] == {"defined": False}
    ev = S.edit(ctx, fresh["id"], {"op": "scale", "layer": 1, "neuron": 0, "factor": 0.9, "target": 3})
    assert set(ev["cost"]) == {"test"}, "with no sets named, only the held-out set is scored"


def test_drift_says_how_far_each_layers_outputs_moved_from_the_base_on_the_same_inputs(ctx):
    sid = S.open_session(ctx, "digits")["id"]
    assert all(d["mean_change"] == 0 and d["similarity"] == 1.0 for d in S.evaluate(ctx, sid)["drift"])
    S.edit(ctx, sid, {"op": "scale", "layer": 2, "neuron": 5, "factor": -1.0})
    r = S.evaluate(ctx, sid)
    d = r["drift"]
    assert [x["column"] for x in d] == [1, 2, 3] and r["drift_on"] == "held out"
    assert d[0]["mean_change"] == 0 and d[0]["relative"] == 0 and d[0]["similarity"] == 1.0, "the layer before the edit did not move"
    assert d[1]["mean_change"] > 0 and 0 < d[1]["relative"] and d[1]["similarity"] < 1.0 and d[2]["mean_change"] > 0
    # The same numbers by hand, on the same inputs.
    _, st = _now(ctx, sid)
    x, _y = WS.sets(ctx, st.manifest, "held_out")
    a0, a1 = N.forward(st.net(base=True), x)[1][2], N.forward(st.net(), x)[1][2]
    assert d[1]["mean_change"] == pytest.approx(float(np.abs(a1 - a0).mean()))
    assert d[1]["relative"] == pytest.approx(float(np.linalg.norm(a1 - a0) / np.linalg.norm(a0)))


def test_a_saved_version_is_its_base_plus_patches_with_the_record_attached(ctx):
    sid = S.open_session(ctx, "tiny", by="agent", name="claude", forward_passes=3)["id"]
    with pytest.raises(N.WeightsError, match="nothing to save"):
        S.save(ctx, sid)
    S.forward(ctx, sid, example=0)
    S.edit(ctx, sid, {"op": "set", "layer": 1, "neuron": 0, "from": 1, "value": 0.75, "why": "first"})
    S.edit(ctx, sid, {"op": "zero", "layer": 1, "neuron": 2, "why": "second"})
    S.undo(ctx, sid)
    r = S.save(ctx, sid, note="one weight")
    assert r == {"model": "tiny", "version": 2, "ref": "tiny@v2", "base": "tiny@v1", "patches": 1, "changed": 1, "share": round(1 / 13, 6),
                 "label": "with 1 of 3 forward passes", "weights_alone": False, "note": "one weight", "scores_shown_now": False}
    m = WS.manifest(ctx, "tiny", 2)
    assert m["base"] == "tiny@v1" and len(m["patches"]) == 1 and m["patches"][0]["new"] == [0.75]
    assert not ctx.store.exists("weights/tiny/v2/tensors.safetensors"), "a saved version holds patches, not tensors"
    rec = m["record"]
    assert rec["who"]["name"] == "claude" and rec["ledger"]["forward_used"] == 1 and [e["state"] for e in rec["edits"]] == ["applied", "undone"]
    cur, _ = _now(ctx, sid)
    _, opened = WS.load(ctx, "tiny", 2)
    assert all(np.array_equal(opened[k], cur[k]) for k in cur)
    assert WS.versions(ctx, "tiny") == [1, 2] and S.save(ctx, sid)["version"] == 3, "a save never overwrites"
    # A session can be opened on the saved version, and its diff is against that version.
    s2 = S.open_session(ctx, "tiny@v2")
    assert s2["base"] == "tiny@v2" and S.diff(ctx, s2["id"])["changed"] == 0


def test_diff_is_between_a_network_and_its_own_edits_and_two_separate_networks_are_refused(ctx, tmp_path):
    sid = S.open_session(ctx, "tiny")["id"]
    S.edit(ctx, sid, {"op": "set", "layer": 2, "neuron": 0, "from": None, "value": 0.3})
    d = S.diff(ctx, sid)
    assert d["base"] == "tiny@v1" and d["changed"] == 1 and d["total"] == 13
    assert d["by_tensor"] == [{"tensor": "layer2.bias", "changed": 1, "total": 1, "share": 1.0, "largest_change": pytest.approx(d["by_tensor"][0]["largest_change"])}]
    assert d["changes"][0]["new"] == pytest.approx(0.3) and d["changes"][0]["i"] is None
    S.save(ctx, sid)
    c = S.compare(ctx, "tiny@v1", "tiny@v2")
    assert c["changed"] == 1 and c["a"] == "tiny@v1" and c["b"] == "tiny@v2"

    other = N.init([2, 3, 1], "sigmoid", "cross_entropy", seed=99)   # the same shape, trained apart
    WS.save_new(ctx, "other", other)
    with pytest.raises(N.WeightsError) as e:
        S.compare(ctx, "tiny@v1", "other@v1")
    assert str(e.value) == S.SEPARATE and "trained separately" in S.SEPARATE and "another order" in S.SEPARATE


def test_writing_the_tensors_out_for_code_needs_a_sandbox_and_is_recorded(ctx, tmp_path):
    sid = S.open_session(ctx, "tiny", by="agent")["id"]
    with pytest.raises(S.Refused, match="without a sandbox"):
        S.export(ctx, sid, str(tmp_path / "x"))
    boxed = S.open_session(ctx, "tiny", by="agent", sandbox=True)["id"]
    S.edit(ctx, boxed, {"op": "set", "layer": 1, "neuron": 0, "from": 0, "value": 2.0, "why": "x"})
    r = S.export(ctx, boxed, str(tmp_path / "x"))
    assert r["files"] == ["manifest.json", "tensors.safetensors"]
    _, tensors = F.read_model(str(tmp_path / "x"))
    assert tensors["layer1.weight"][0, 0] == 2.0
    assert S.session(ctx, boxed)["ledger"]["sandbox_used"] is True


def test_a_training_step_is_an_edit_that_is_labelled_as_training(ctx):
    sid = S.open_session(ctx, "tiny", forward_passes=2)["id"]
    ev = S.edit(ctx, sid, {"op": "step", "example": [0, 1, 2, 3], "rate": 0.5})
    assert ev["op"] == "step" and ev["changed"] == 13 and "training step" in ev["sentence"]
    led = S.session(ctx, sid)["ledger"]
    assert led["training_steps"] == 1 and led["forward_used"] == 1 and led["label"] == "with 1 of 2 forward passes, and 1 training step"
    S.undo(ctx, sid)
    assert S.session(ctx, sid)["ledger"]["changed"] == 0


def test_two_writers_on_one_session_both_land(ctx):
    import threading

    sid = S.open_session(ctx, "tiny")["id"]
    errs = []

    def work(j):
        try:
            for i in range(2):
                S.edit(ctx, sid, {"op": "set", "layer": 1, "neuron": j, "from": i, "value": float(j + i + 1)})
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=work, args=(j,)) for j in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    s = S.session(ctx, sid)
    assert len(s["edits"]) == 6 and s["ledger"]["changed"] == 6 and sorted(e["n"] for e in s["edits"]) == [1, 2, 3, 4, 5, 6]


def test_the_quantities_a_lesson_draws_are_returned_by_name(ctx):
    sid = S.open_session(ctx, "tiny", forward_passes=40)["id"]
    a = S.activations(ctx, sid, example=1)
    assert sum(t["term"] for t in a["cost_terms"]) == pytest.approx(a["cost"]) and set(a["cost_terms"][0]) == {"output", "a", "wanted", "term"}
    d = S.open_session(ctx, "digits", forward_passes=1)["id"]
    a = S.activations(ctx, d, example=0)
    assert len(a["cost_terms"]) == 10 and sum(t["term"] for t in a["cost_terms"]) == pytest.approx(a["cost"])

    # Adding up the desires: what each later neuron asks of the ones before it, and the sums are their dcost_da.
    g = S.gradients(ctx, sid, example=1, layer=1)
    des = g["desires"]
    assert (des["of_column"], des["asked_by_column"], des["exact"]) == (1, 2, True) and len(des["cells"]) == 3 and len(des["cells"][0]) == 1
    assert des["sums"] == pytest.approx(g["layers"][0]["dcost_da"])
    assert "desires" not in S.gradients(ctx, sid, example=1)

    # A slice of the cost along one number: its middle is the number now, and its slope there is the gradient.
    r = S.cost_slice(ctx, sid, "layer1.weight", [2, 0], example=[0, 1, 2, 3], span=2.0, points=11)
    assert len(r["values"]) == len(r["costs"]) == 11 and r["values"][5] == pytest.approx(r["value"]) and r["costs"][5] == pytest.approx(r["cost"])
    assert r["dimensions"] == 13 and r["forward_used"] == 3 + 11
    h = r["values"][6] - r["values"][5]
    assert r["slope"] == pytest.approx((r["costs"][6] - r["costs"][4]) / (2 * h), rel=0.2, abs=1e-3)
    assert S.session(ctx, sid)["ledger"]["changed"] == 0, "a slice changes nothing"
    with pytest.raises(S.Refused, match="runs the network 30 times"):
        S.cost_slice(ctx, sid, "layer1.bias", 0, example=0, points=30)
    assert S.session(ctx, sid)["ledger"]["forward_used"] == 14, "a slice that does not fit is refused whole"


def test_an_edit_after_gradients_carries_what_the_gradient_predicted_beside_what_happened(ctx):
    sid = S.open_session(ctx, "tiny", forward_passes=5)["id"]
    S.set_sets(ctx, sid, target={"classes": [1]}, should_not_change={"classes": [0]})
    ev = S.edit(ctx, sid, {"op": "add", "layer": 2, "neuron": 0, "from": None, "value": 0.001})
    assert "predicted" not in ev, "no gradients were computed in this session yet"
    S.gradients(ctx, sid, example=0)
    small = S.edit(ctx, sid, {"op": "add", "layer": 2, "neuron": 0, "from": None, "value": 0.001})["predicted"]
    assert small["on"] == "target" and small["change"] == pytest.approx(small["actual_change"], rel=0.05)
    big = S.edit(ctx, sid, {"op": "add", "layer": 2, "neuron": 0, "from": None, "value": -25.0})["predicted"]
    assert abs(big["gap"]) > 10 * abs(small["gap"]), "a large edit lands far from the straight-line prediction"


def test_a_score_shown_to_the_caller_ends_from_the_weights_alone_and_the_record_says_which_operation(ctx):
    sid = S.open_session(ctx, "tiny", by="agent")["id"]
    assert S.session(ctx, sid)["ledger"]["weights_alone"] is True, "reading alone shows no score"
    S.read(ctx, sid, "layer1.weight")
    ev = S.edit(ctx, sid, {"op": "add", "layer": 2, "neuron": 0, "from": None, "value": 0.1, "why": "a nudge"})
    assert "cost" in ev and "Cost on the held-out set went from" in ev["sentence"]
    led = S.session(ctx, sid)["ledger"]
    assert led["weights_alone"] is False and led["label"] == "from the weights, with scores shown along the way"
    assert led["feedback"] is True and led["scores_shown"] is True and led["first_score"] == {"event": ev["id"], "op": "edit"}

    by_eval = S.open_session(ctx, "tiny", by="agent")["id"]
    S.evaluate(ctx, by_eval)
    led = S.session(ctx, by_eval)["ledger"]
    assert led["weights_alone"] is False and led["first_score"]["op"] == "evaluate"


def test_a_blind_session_keeps_every_score_and_shows_none_until_it_is_saved_or_closed(ctx):
    s = S.open_session(ctx, "digits", by="agent", feedback=False)
    sid = s["id"]
    assert s["ledger"]["feedback"] is False and "hidden until it is saved or closed" in s["events"][0]["sentence"]
    S.set_sets(ctx, sid, target={"classes": [9]}, should_not_change={"classes": [0, 1, 2, 3, 4, 5, 6, 7, 8]})
    ev = S.edit(ctx, sid, {"op": "set", "layer": 3, "neuron": 9, "from": None, "value": -30.0, "why": "switch the nine output off"})
    for key in ("cost", "cost_before", "cost_after", "predicted"):
        assert key not in ev
    assert ev["scores"] == "hidden" and "Cost" not in ev["sentence"] and ev["changed"] == 1 and ev["changes"][0]["new"] == -30.0
    r = S.evaluate(ctx, sid)
    assert r["measured"] is True and r["scores"] == "hidden" and r["says"] == S.BLIND and "sets" not in r and "drift" not in r
    u = S.undo(ctx, sid)
    assert "cost" not in u and u["scores"] == "hidden"
    S.redo(ctx, sid)
    seen = str(S.session(ctx, sid)) + str(S.events(ctx, sid)) + str(S.report(ctx, sid))
    assert "right_after" not in seen and "Cost on" not in seen and "similarity" not in seen, "no route shows a score while it is blind"
    led = S.session(ctx, sid)["ledger"]
    assert led["weights_alone"] is True and led["label"] == "from the weights alone" and led["scores_shown"] is False and led["evaluations"] == 1
    # The scores were measured and stored all along.
    stored = S._load(ctx, sid).doc["events"]
    assert next(e for e in stored if e["id"] == ev["id"])["cost"]["target"]["right_after"] == 0

    sliced = S.open_session(ctx, "tiny", by="agent", feedback=False, forward_passes=3)["id"]
    r = S.cost_slice(ctx, sliced, "layer1.bias", 0, example=0, points=3)
    assert r["scores"] == "hidden" and "costs" not in r and "slope" not in r and r["forward_used"] == 3
    assert len(S._load(ctx, sliced).doc["events"][-1]["result"]["costs"]) == 3 and "result" not in S.events(ctx, sliced)["events"][-1]
    assert S.session(ctx, sliced)["ledger"]["weights_alone"] is False, "a slice runs the network"

    saved = S.save(ctx, sid, note="blind fix")
    assert saved["weights_alone"] is True and saved["label"] == "from the weights alone" and saved["scores_shown_now"] is True
    assert WS.manifest(ctx, "digits", 2)["record"]["ledger"]["weights_alone"] is True
    shown = next(e for e in S.events(ctx, sid)["events"] if e["id"] == ev["id"])
    assert shown["cost"]["target"]["right_after"] == 0 and "Cost on the target went from" in shown["sentence"]
    assert S.evaluate(ctx, sid)["sets"]["target"]["right_after"] == 0
    led = S.session(ctx, sid)["ledger"]
    assert led["weights_alone"] is True and led["first_score"]["op"] == "save", "saved work keeps its label after its scores are shown"
    # Going on editing after seeing the scores is no longer blind work.
    S.edit(ctx, sid, {"op": "set", "layer": 3, "neuron": 9, "from": None, "value": -1.0, "why": "a second try, having seen the score"})
    led = S.session(ctx, sid)["ledger"]
    assert led["weights_alone"] is False and led["label"] == "from the weights, with scores shown along the way"


def test_a_closed_session_shows_its_scores_and_takes_no_more_changes(ctx):
    sid = S.open_session(ctx, "tiny", by="agent", feedback=False, forward_passes=2)["id"]
    ev = S.edit(ctx, sid, {"op": "add", "layer": 2, "neuron": 0, "from": None, "value": 0.1, "why": "a nudge"})
    s = S.close(ctx, sid)
    assert s["closed"] is True and s["ledger"]["weights_alone"] is True and s["events"][-1]["kind"] == "close"
    assert "cost" in next(e for e in s["events"] if e["id"] == ev["id"])
    for call in (lambda: S.edit(ctx, sid, {"op": "zero", "layer": 1, "neuron": 0, "why": "x"}), lambda: S.undo(ctx, sid),
                 lambda: S.forward(ctx, sid, example=0), lambda: S.close(ctx, sid)):
        with pytest.raises(N.WeightsError, match="This session is closed"):
            call()
    assert S.read(ctx, sid, "layer1.bias")["count"] == 3 and S.report(ctx, sid)["ledger"]["changed"] == 1


def test_a_pinned_neuron_is_summarised_for_an_agent_counted_from_one(ctx):
    from captain_search import agent_context as AC
    from captain_search.weights import pins as P

    WS.ensure_starters(ctx)
    item = AC.resolve(ctx, "weights:digits@layer=2,neuron=6")
    assert item == {"ref": "weights:digits@layer=2,neuron=6", "kind": "weights", "label": "digits: layer 2, neuron 7"}
    out = AC.expand(ctx, item)
    _, t = WS.load(ctx, "digits")
    row = t["layer2.weight"][6].astype(float)
    assert out["model"] == "digits@v1" and (out["layer"], out["neuron"], out["of"]) == (2, 7, 16) and out["incoming_weights"] == 16
    assert out["bias"] == pytest.approx(float(t["layer2.bias"][6]), abs=1e-6)
    top = out["largest_incoming"][0]
    assert top["from_neuron"] == int(np.abs(row).argmax()) + 1 and top["weight"] == pytest.approx(float(row[np.abs(row).argmax()]), abs=1e-6)
    assert len(out["largest_incoming"]) == 5 and len(out["largest_outgoing"]) == 5 and "not handwriting" in out["data"]
    assert "cst weights heatmap --layer 1 --neuron 6" in out["read_more"] and out["read_more"][0].startswith("cst weights open digits --agent")
    one = AC.expand(ctx, AC.resolve(ctx, "weights:tiny@layer=1,neuron=2,from=0"))
    assert one["weight"]["from_neuron"] == 1 and "largest_outgoing" in one
    whole = AC.expand(ctx, AC.resolve(ctx, "weights:tiny"))
    assert whole["layers"][0] == "layer 1: 2 in, 3 out, sigmoid" and "problem" not in whole
    assert "problem" in AC.expand(ctx, {"ref": "weights:tiny@layer=9,neuron=0"}) and "unknown kind" not in str(AC.expand(ctx, {"ref": "weights:tiny@layer=9,neuron=0"}))
    with pytest.raises(N.WeightsError, match="`neuron` goes from 0 to 2"):
        P.resolve(ctx, "tiny@layer=1,neuron=5")
