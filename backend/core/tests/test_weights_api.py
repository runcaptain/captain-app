"""Weights through the app and through `cst weights`: the routes answer in the shapes of docs/weights.md, a refusal
is a sentence, and every operation tells the change feed, also from a second process."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import addresses as A
from captain_search import changes as CH
from captain_search import server
from captain_search.cli.main import S as CLI_STATE
from captain_search.cli.main import app as cli
from captain_search.weights import session as WSN
from captain_search.workspace import init_workspace

DOC = Path(server.__file__).parent / "docs" / "weights.md"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    CH.reset_for_tests()
    ctx = init_workspace("t", str(tmp_path / "store"))
    monkeypatch.setattr(server, "ctx", lambda *a, **k: (CH.watch(ctx.store), ctx)[1])
    c = TestClient(server.app)
    hd = {"Authorization": f"Bearer {server.TOKEN}"}

    class Svc:
        pass

    s = Svc()
    s.ctx, s.c, s.hd, s.root = ctx, c, hd, str(tmp_path / "store")

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


def has(d: dict, *keys):
    missing = [k for k in keys if k not in d]
    assert not missing, f"missing {missing} in {sorted(d)}"


def test_the_routes_answer_in_the_documented_shapes(svc):
    rows = svc.get("/api/weights/models")
    has(rows[0], "id", "name", "says", "sizes", "parameters", "versions", "synthetic")
    net = svc.get("/api/weights/models/tiny")
    has(net, "id", "name", "version", "sizes", "squish", "cost", "layers", "examples", "test", "inputNames", "outputNames", "synthetic", "numbers")
    has(net["layers"][0], "activation", "inputs", "outputs")
    assert net["test"] == [] and net["numbers"] is False and "W" not in net["layers"][0], "no numbers without a session"

    s = svc.post("/api/weights/sessions", {"model": "tiny", "by": "agent", "name": "claude", "forward_passes": 8})
    sid = s["id"]
    has(s, "id", "model", "version", "who", "ledger", "sets", "edits", "events")
    has(s["ledger"], "raw_weights", "weights_read", "sandbox", "sandbox_used", "forward_allowed", "forward_used", "pass_inputs",
        "gradients_computed", "evaluations", "changed", "total", "share", "feedback", "scores_shown", "first_score", "screen_looks",
        "screen_numbers", "weights_alone", "label")
    assert svc.get("/api/weights/models/tiny/session")["id"] == sid and svc.get(f"/api/weights/sessions/{sid}")["id"] == sid
    assert svc.get("/api/weights/models/tiny/sessions")[0]["id"] == sid
    p = f"/api/weights/sessions/{sid}"

    ly = svc.get(p + "/layers")
    has(ly, "sizes", "cost", "parameters", "layers")
    has(ly["layers"][0], "layer", "name", "inputs", "outputs", "activation", "weight", "bias", "parameters")
    r = svc.get(p + "/read", tensor="layer1.weight", rows="0:2", cols="0:2")
    assert set(r) == {"tensor", "shape", "rows", "cols", "values", "count"} and r["count"] == 4
    st = svc.get(p + "/stats", neurons=1)
    has(st["tensors"][0], "tensor", "shape", "count", "mean", "variance", "min", "p25", "median", "p75", "max", "norm", "zero_share", "distinct")
    has(st["neurons"][0], "layer", "neuron", "bias", "weight_norm", "weight_mean", "weight_min", "weight_max", "zero_share", "dead")
    has(st["bias_range"], "first", "last")
    hm = svc.get(p + "/heatmap", layer=0, neuron="1")
    has(hm, "layer", "grid_shape", "scale", "grids")
    has(hm["grids"][0], "neuron", "bias", "min", "max", "grid")
    has(svc.get(p + "/template", output=0), "layer", "output", "name", "bias", "weights", "grid_shape", "grid")

    f = svc.post(p + "/forward", {"inputs": [[0.9, 0.1]], "targets": [[1]]})
    assert set(f) == {"outputs", "predicted", "cost", "forward_used", "forward_left"} and f["forward_left"] == 7
    a = svc.post(p + "/activations", {"example": 3})
    has(a, "input", "columns", "output", "predicted", "wanted", "cost", "forward_used", "forward_left")
    has(a["columns"][0], "column", "z", "a")
    g = svc.post(p + "/gradients", {"example": 3})
    has(g, "cost", "examples", "layers", "input_dcost_da", "top", "forward_used", "forward_left")
    has(g["layers"][0], "layer", "name", "weight", "bias", "dcost_da", "wanted_nudge", "dcost_dz", "da_dz")
    has(g["top"][0], "tensor", "index", "value", "gradient")
    t = svc.post(p + "/trace", {"example": 3, "path": [0, 2, 0]})
    has(t, "path", "cost", "steps", "path_product", "first_weight_gradient", "paths_from_first_weight")
    has(t["steps"][0], "layer", "neuron", "from_neuron", "weight", "bias", "z", "a", "a_before", "da_dz", "dcost_da", "dcost_dz", "gradient",
        "product", "product_is_exact")

    sets = svc.post(p + "/sets", {"target": {"classes": [1]}, "should_not_change": {"classes": [0]}})
    assert list(sets) == ["target", "should_not_change", "held_out"] and all(v["defined"] for v in sets.values())
    ev = svc.post(p + "/edits", {"op": "set", "layer": 1, "neuron": 2, "from": None, "value": -1.5, "why": "the bias keeps this unit on", "by": "agent"})
    has(ev, "id", "seq", "at", "by", "kind", "op", "tensor", "layer", "neuron", "why", "touched", "changes", "changed", "share", "cost", "sentence")
    assert ev["kind"] == "edit" and ev["touched"]["neurons"] == [[1, 2]] and set(ev["changes"][0]) >= {"k", "j", "i", "old", "new"}
    for key in ("target", "keep", "test"):
        has(ev["cost"][key], "name", "of", "before", "after", "right_before", "right_after", "answers_changed")
    d = svc.get(p + "/diff")
    has(d, "base", "changed", "total", "share", "by_tensor", "changes")
    has(d["by_tensor"][0], "tensor", "changed", "total", "share", "largest_change")
    e = svc.post(p + "/evaluate")
    has(e, "sets", "drift", "drift_on", "before_is", "evaluations")
    has(e["sets"]["target"], "name", "defined", "of", "before", "after", "right_before", "right_after", "answers_changed")
    has(e["drift"][0], "layer", "column", "mean_change", "relative", "similarity")
    u = svc.post(p + "/undo", {"edit": ev["id"], "by": "you"})
    assert u["kind"] == "undo" and u["undoes"] == ev["id"] and u["by"] == "you"
    assert svc.post(p + "/redo")["redoes"] == ev["id"]
    sv = svc.post(p + "/save", {"note": "from a test"})
    assert set(sv) >= {"model", "version", "ref", "base", "patches", "changed", "share", "label"} and sv["ref"] == "tiny@v2"
    evs = svc.get(p + "/events", since=2)
    assert evs["events"][0]["seq"] == 3 and all(e["sentence"] for e in evs["events"])
    rep = svc.get(p + "/report")
    has(rep, "ledger", "events", "edits", "kept", "taken_back", "passes", "diff", "saved")
    assert svc.get("/api/weights/compare", a="tiny@v1", b="tiny@v2")["changed"] == 1
    sl = svc.post(p + "/slice", {"tensor": "layer1.bias", "index": [0], "example": [0, 1], "points": 3})
    has(sl, "tensor", "index", "value", "cost", "slope", "values", "costs", "examples", "dimensions", "forward_used", "forward_left")
    blind = svc.post("/api/weights/sessions", {"model": "tiny", "by": "agent", "feedback": False})["id"]
    ev2 = svc.post(f"/api/weights/sessions/{blind}/edits", {"op": "zero", "layer": 1, "neuron": 0, "why": "blind"})
    assert ev2["scores"] == "hidden" and "cost" not in ev2
    hidden = svc.post(f"/api/weights/sessions/{blind}/evaluate")
    assert hidden["scores"] == "hidden" and hidden["measured"] is True and "sets" not in hidden
    closed = svc.post(f"/api/weights/sessions/{blind}/close")
    assert closed["closed"] is True and closed["ledger"]["weights_alone"] is True and "cost" in closed["events"][1]
    full = svc.get("/api/weights/models/tiny", session=sid, watch=1)
    has(full["layers"][0], "W", "b", "activation")
    assert full["layers"][0]["b"][2] == -1.5 and full["numbers"] is True
    assert svc.get(f"/api/weights/sessions/{sid}")["ledger"]["screen_numbers"] >= 13


def test_a_refusal_is_a_plain_sentence_with_its_own_status(svc):
    sid = svc.post("/api/weights/sessions", {"model": "tiny", "by": "agent", "raw_weights": False})["id"]
    p = f"/api/weights/sessions/{sid}"
    r = svc.post(p + "/forward", {"example": 0}, code=403)
    assert r == {"error": "This session was opened with no forward passes, so the network cannot be run in it.", "refused": True}
    assert svc.get(p + "/read", code=403, tensor="layer1.weight")["refused"] is True
    assert svc.get(p + "/heatmap", watch=1)["grids"], "a screen looking is not the agent"
    assert svc.post(p + "/forward", {"example": 0, "watch": True}, code=403)["refused"] is True, "a run cannot be passed off as a look"
    assert svc.c.post(p + "/activations?watch=1", headers=svc.hd, json={"example": 0}).status_code == 403
    assert svc.get(f"/api/weights/sessions/{sid}")["ledger"]["screen_looks"] == 1
    assert "needs `why`" in svc.post(p + "/edits", {"op": "zero", "layer": 1, "neuron": 0}, code=400)["error"]
    assert "no weights session" in svc.get("/api/weights/sessions/ws-nope", code=404)["error"]
    assert "no weights model named nope" in svc.get("/api/weights/models/nope", code=404)["error"]
    assert "trained separately" in svc.get("/api/weights/compare", code=400, a="tiny", b="digits")["error"]
    for text in (r["error"], WSN.SEPARATE):
        assert not re.search(r"[—–←-⇿]|\bengine\b|\bdaemon\b|Traceback|Error\(", text), text


def test_every_weights_route_is_in_the_guide_and_the_guide_names_no_route_that_is_not_served():
    served = {(m, p) for m, p in A.routes(server.app) if p.startswith("/api/weights")}
    assert len(served) >= 28
    text = DOC.read_text()
    named = set()
    for m, path in re.findall(r"`(GET|POST)\s+(/api/weights[^`?\s]*)", text.replace("\n", " ")):
        named.add((m, path.replace("{s}", "{sid}")))
    assert not {f"{m} {p}" for m, p in named - served}, "the guide names a route the app does not serve"
    assert not {f"{m} {p}" for m, p in served - named}, "the app serves a weights route the guide does not name"


def test_what_a_person_reads_has_no_dashes_arrows_or_internal_words():
    here = Path(server.__file__).parent
    texts = {"weights.md": DOC.read_text()}
    for f in ("weights/session.py", "weights/store.py", "weights/files.py", "weights/net.py", "weights/starters.py", "weights/routes.py",
              "weights/program.py", "weights/pins.py", "weights/playbook.md", "cli/weights.py"):
        texts[f] = (here / f).read_text()
    for name, text in texts.items():
        assert not re.search(r"[—–←-⇿⟵-⟿]", text), f"{name} has a dash or an arrow a person could see"
        assert not re.search(r"\bengines?\b|\bdaemon\b", text, re.I), f"{name} says a word people should not see"


# ---- live ---------------------------------------------------------------------------------------------------------------

def _waiter(svc, since, topics):
    box: dict = {}

    def go():
        box["answer"] = svc.get("/api/changes", since=since, topics=topics)
        box["at"] = time.perf_counter()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    time.sleep(0.15)
    assert "answer" not in box, f"it answered before anything changed: {box}"
    return t, box


def _topics(answer) -> list[str]:
    return [c["topic"] for c in answer["changed"]]


@pytest.mark.parametrize("op", ["read", "run", "edit", "undo", "evaluate", "sets", "save"])
def test_every_operation_tells_the_feed_under_the_model_and_the_session(svc, op):
    sid = svc.post("/api/weights/sessions", {"model": "tiny"})["id"]
    p = f"/api/weights/sessions/{sid}"
    if op == "undo":
        svc.post(p + "/edits", {"op": "zero", "layer": 1, "neuron": 0})
    if op == "save":
        svc.post(p + "/edits", {"op": "zero", "layer": 1, "neuron": 1})
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, f"weights:tiny,weights-session:{sid}")
    calls = {"read": lambda: svc.get(p + "/layers"), "run": lambda: svc.post(p + "/forward", {"example": 0}),
             "edit": lambda: svc.post(p + "/edits", {"op": "zero", "layer": 1, "neuron": 2}), "undo": lambda: svc.post(p + "/undo"),
             "evaluate": lambda: svc.post(p + "/evaluate"), "sets": lambda: svc.post(p + "/sets", {"target": {"classes": [1]}}),
             "save": lambda: svc.post(p + "/save")}
    calls[op]()
    t.join(3)
    assert "answer" in box and _topics(box["answer"]), "the waiting screen was not told"
    told = svc.get("/api/changes", since=seq, topics=f"weights:tiny,weights-session:{sid}", wait=0)
    assert set(_topics(told)) == {"weights:tiny", f"weights-session:{sid}"}, told
    # A screen looking is not an operation: it tells nobody.
    seq = svc.get("/api/changes")["seq"]
    svc.get(p + "/layers", watch=1)
    svc.get(f"/api/weights/sessions/{sid}")
    assert svc.get("/api/changes", since=seq, topics=f"weights:tiny,weights-session:{sid}", wait=0).get("changed", []) == []


def test_an_edit_made_by_cst_in_a_second_process_tells_a_listening_screen(svc, tmp_path):
    sid = svc.post("/api/weights/sessions", {"model": "tiny", "by": "agent", "name": "claude"})["id"]
    home = tmp_path / "other-home"
    env = {**os.environ, "CST_HOME": str(home), "PYTHONPATH": str(Path(server.__file__).parents[1])}
    run = lambda *args: subprocess.run([sys.executable, "-c", "from captain_search.cli.main import main; main()", "--json", *args],  # noqa: E731
                                       capture_output=True, text=True, env=env)
    assert run("init", "t", "--storage", svc.root).returncode == 0
    seq = svc.get("/api/changes")["seq"]
    t, box = _waiter(svc, seq, f"weights:tiny,weights-session:{sid}")
    time.sleep(1.2)   # the folder watch has taken its first look
    assert "answer" not in box
    r = run("weights", "edit", "set", "--session", sid, "--layer", "1", "--neuron", "3", "--value", "-1.5", "--why", "made in a terminal")
    done = time.perf_counter()
    assert r.returncode == 0, r.stdout + r.stderr
    ev = json.loads(r.stdout)
    assert ev["kind"] == "edit" and ev["by"] == "agent" and ev["changed"] == 1
    t.join(4)
    assert "answer" in box, "the other process's edit was never seen"
    assert {"weights:tiny", f"weights-session:{sid}"} <= set(_topics(box["answer"])), box["answer"]
    assert box["at"] - done < 1.5, f"told {(box['at'] - done):.2f} s after the command finished"
    # And what the screen then reads is the edit.
    s = svc.get(f"/api/weights/sessions/{sid}")
    assert s["events"][-1]["id"] == ev["id"] and s["ledger"]["changed"] == 1
    assert svc.get("/api/weights/models/tiny", session=sid)["layers"][0]["b"][2] == -1.5


# ---- cst weights ---------------------------------------------------------------------------------------------------------

R = CliRunner()


def cst(*args, code=0):
    CLI_STATE.json = False
    r = R.invoke(cli, ["--json", *args])
    assert r.exit_code == code, r.output
    return json.loads(r.output) if r.output.strip() else None


def test_the_commands_print_the_same_shapes_and_follow_the_session_opened_last(tmp_path):
    cst("init", "demo", "--storage", str(tmp_path / "bucket"))
    assert [m["id"] for m in cst("weights", "models")] == ["tiny", "digits"]
    s = cst("weights", "open", "tiny", "--agent", "--name", "claude", "--forward-passes", "2")
    assert s["who"] == {"kind": "agent", "name": "claude"} and s["ledger"]["forward_allowed"] == 2
    assert cst("weights", "session")["id"] == s["id"]
    assert cst("weights", "layers")["sizes"] == [2, 3, 1]
    assert cst("weights", "read", "layer1.weight", "--rows", "0", "--cols", "0:2")["count"] == 2
    assert len(cst("weights", "stats", "--neurons")["neurons"]) == 4
    assert cst("weights", "heatmap", "--neuron", "1,3")["grids"][1]["neuron"] == 2
    assert cst("weights", "template", "1")["output"] == 0
    assert cst("weights", "forward", "-i", "0.9,0.1", "-t", "1")["predicted"] == [1]
    assert cst("weights", "trace", "-e", "1", "--path", "1,2,1")["path"] == [0, 1, 0]
    refused = cst("weights", "gradients", "-e", "1", code=3)
    assert refused["error"].startswith("Refused. All 2 forward passes")
    assert "needs `why`" in cst("weights", "edit", "zero", "--layer", "1", "--neuron", "1", code=1)["error"]
    assert cst("weights", "sets", "--target", "1", "--keep", "0")["target"]["defined"]
    ev = cst("weights", "edit", "add", "--tensor", "layer1.weight", "--index", "0,0", "--index", "0,1", "--values", "0.5,-0.5", "--why", "tilt it")
    assert ev["changed"] == 2 and ev["touched"]["weights"] == [[0, 0, 0], [0, 0, 1]]
    ev2 = cst("weights", "edit", "add-direction", "--tensor", "layer1.weight", "--direction", "1,-1", "--scale", "0.1", "--neurons", "2", "--why", "x")
    assert ev2["changed"] == 2 and ev2["op"] == "add-direction"
    assert cst("weights", "diff")["changed"] == 4
    assert cst("weights", "undo")["undoes"] == ev2["id"] and cst("weights", "redo")["redoes"] == ev2["id"]
    assert cst("weights", "undo", ev["id"])["undoes"] == ev["id"]
    assert cst("weights", "evaluate")["sets"]["held_out"]["of"] == 300
    assert cst("weights", "save", "--as", "tilted")["ref"] == "tiny@v2"
    assert cst("weights", "compare", "tiny@v1", "tiny@v2")["changed"] == 2
    assert "trained separately" in cst("weights", "compare", "tiny", "digits", code=1)["error"]
    rep = cst("weights", "report")
    assert rep["ledger"]["label"] == "with 2 of 2 forward passes" and rep["ledger"]["weights_alone"] is False and rep["saved"] == ["tiny@v2"]
    assert [e["kind"] for e in cst("weights", "events", "--since", str(len(rep["events"]) - 1))["events"]] == ["save"]
    assert "sandbox" in cst("weights", "export", str(tmp_path / "out"), code=3)["error"]
    other = cst("weights", "open", "tiny@v2")
    assert other["base"] == "tiny@v2" and cst("weights", "session")["id"] == other["id"]
    assert cst("weights", "session", "--session", s["id"])["id"] == s["id"]
    b = cst("weights", "open", "tiny", "--agent", "--blind")
    assert b["ledger"]["feedback"] is False
    assert cst("weights", "edit", "zero", "--layer", "1", "--neuron", "1", "--why", "blind")["scores"] == "hidden"
    assert cst("weights", "evaluate")["scores"] == "hidden"
    assert cst("weights", "close")["ledger"]["label"] == "from the weights alone"
    assert "closed" in cst("weights", "undo", code=1)["error"]
    assert {r["id"] for r in cst("weights", "sessions", "tiny")} == {s["id"], other["id"], b["id"]}


def test_the_plain_output_is_sentences_and_the_guide_is_shipped(tmp_path):
    R.invoke(cli, ["init", "demo", "--storage", str(tmp_path / "bucket")])
    CLI_STATE.json = False
    out = R.invoke(cli, ["weights", "open", "digits"]).output
    assert "label: from the weights alone" in out and "forward passes: 0 of 50 used" in out
    out = R.invoke(cli, ["weights", "edit", "scale", "--layer", "2", "--neuron", "3", "--factor", "0.5"]).output
    assert re.search(r"e\d+  You scaled 16 numbers in layer2\.weight \(layer 2, neuron 3\)\. Cost on the held-out set went from", out), out
    assert "held out: cost" in out
    out = R.invoke(cli, ["weights", "--json", "diff"]).output
    assert json.loads(out)["changed"] == 16
    CLI_STATE.json = False
    r = R.invoke(cli, ["weights", "layers", "--session", "ws-nope"])
    assert r.exit_code == 1 and "no weights session" in r.output
    docs = R.invoke(cli, ["docs", "weights"]).output
    assert docs.startswith("# Weights") and "synthetic" in docs


def test_the_neuron_a_person_types_is_the_neuron_the_sentence_names_and_the_one_that_changes(tmp_path):
    """`--layer`, `--neuron` and `--from` count from 1, as the page and every sentence do."""
    R.invoke(cli, ["init", "demo", "--storage", str(tmp_path / "bucket")])
    CLI_STATE.json = False
    R.invoke(cli, ["weights", "open", "tiny", "--forward-passes", "60"])
    before = cst("weights", "read", "layer1.bias")["values"]
    for layer, neuron in ((1, 1), (1, 2), (1, 3), (2, 1)):
        CLI_STATE.json = False
        out = R.invoke(cli, ["weights", "edit", "set", "--layer", str(layer), "--neuron", str(neuron), "--value", "-9"]).output
        assert f"(layer {layer}, neuron {neuron})" in out, out
    after = cst("weights", "read", "layer1.bias")["values"]
    assert after == [-9.0, -9.0, -9.0] and before != after
    ev = cst("weights", "edit", "set", "--layer", "1", "--neuron", "2", "--from", "1", "--value", "0.5")
    assert (ev["changes"][0]["k"], ev["changes"][0]["j"], ev["changes"][0]["i"], ev["changes"][0]["new"]) == (0, 1, 0, 0.5), "typed from 1, stored from 0"
    # Every command that names a layer or a neuron reads it the same way.
    assert cst("weights", "read", "--layer", "1", "--neuron", "2")["values"][0][0] == 0.5
    assert cst("weights", "read", "--layer", "1", "--bias", "--neuron", "3")["values"] == [-9.0]
    assert cst("weights", "heatmap", "--layer", "1", "--neuron", "2")["grids"][0]["grid"][0][0] == 0.5
    assert [n["neuron"] for n in cst("weights", "stats", "--layer", "1", "--neuron", "2")["neurons"]] == [1]
    assert cst("weights", "gradients", "-e", "1", "--layer", "1", "--neuron", "2")["row"]["neuron"] == 1
    assert len(cst("weights", "activations", "-e", "1", "--layer", "1")["columns"]) == 1
    sl = cst("weights", "slice", "--layer", "1", "--neuron", "2", "--from", "1", "-e", "1", "--points", "3")
    assert sl["tensor"] == "layer1.weight" and sl["index"] == [1, 0] and sl["value"] == 0.5
    CLI_STATE.json = False
    for args, words in ((["trace", "-e", "1", "--path", "1,2,1"], "path through neurons 1, 2, 1"),
                        (["template", "1"], "output 1 ("),
                        (["activations", "-e", "1", "--layer", "1", "--neuron", "2"], "layer 1, neuron 2: weighted sum"),
                        (["slice", "--layer", "1", "--neuron", "2", "--from", "1", "-e", "1", "--points", "3"], "The weight into layer 1, neuron 2 from neuron 1 is 0.5"),
                        (["read", "--layer", "1", "--neuron", "2"], "layer 1, neuron 2, incoming weights"),
                        (["forward", "-e", "3"], "answer")):
        out = R.invoke(cli, ["weights", *args]).output
        assert words in out, out
    assert "ran the network on example 3." in R.invoke(cli, ["weights", "events"]).output
    assert "counts from 1" in R.invoke(cli, ["weights", "edit", "zero", "--layer", "1", "--neuron", "0"]).output
    assert "counts from 1" in R.invoke(cli, ["weights", "heatmap", "--layer", "0"]).output
    assert "Layer 1 has 3 neurons. The neuron asked for is not one of them." in R.invoke(cli, ["weights", "edit", "zero", "--layer", "1", "--neuron", "4"]).output
    help_text = " ".join(R.invoke(cli, ["weights", "edit", "--help"]).output.split())
    assert help_text.count("counted from 1") >= 3 and "counted from 0" in help_text


def test_show_gives_a_description_and_a_single_wrong_answer_can_be_named_as_the_target(tmp_path):
    cst("init", "demo", "--storage", str(tmp_path / "bucket"))
    w = cst("weights", "show", "digits")
    assert w["numbers"] is False and "W" not in w["layers"][0] and len(w["examples"]) == 40 and w["inputShape"] == [28, 28]
    cst("weights", "open", "tiny")
    assert cst("weights", "sets", "--target-example", "3")["target"]["of"] == 1
    t = cst("weights", "sets", "--target-input", "0.9,0.1", "--target-output", "1")["target"]
    assert t == {"defined": True, "of": 1, "says": "1 input given when the set was named"}
    ev = cst("weights", "edit", "add", "--layer", "2", "--neuron", "1", "--value", "0.2")
    assert ev["cost"]["target"]["of"] == 1 and ev["cost"]["target"]["after"] < ev["cost"]["target"]["before"]
    assert "needs --target-output" in cst("weights", "sets", "--target-input", "0.9,0.1", code=1)["error"]
