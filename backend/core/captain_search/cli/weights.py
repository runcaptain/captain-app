"""cst weights: open a network as a session, read it, change it, and get the record of what was done.

Every command prints the same shape its route returns when --json is given (`cst --json weights layers`, or
`cst weights --json layers`). Without it, a few plain lines. The shapes are in `cst docs weights`.

Commands act on the session opened last on this computer. Name another with --session.

Counting. Everything a person types to name a place counts from 1, the way the page and every sentence do:
--layer, --neuron, --from, --neurons, --path, --example and the output given to `template`. Layer 1 is the first
layer after the input. Two things count from 0, because they are positions inside a tensor: --index (with --tensor)
and --rows / --cols. Everything in --json output counts from 0 (see `cst docs weights`).
"""
from __future__ import annotations

import json
import os
from typing import Optional

import typer

from .. import workspace as wsp
from ..storage.backend import Conflict, NotFound
from ..weights import session as WSN
from ..weights import store as WST
from ..weights.net import WeightsError
from .main import S, app, ctx, emit, fail

weights_app = typer.Typer(no_args_is_help=True, help="Weights: read, fix and write a network's numbers, with a record")
app.add_typer(weights_app, name="weights")


@weights_app.callback()
def _group(as_json: bool = typer.Option(False, "--json", help="Machine-readable output")):
    if as_json:
        S.json = True


SESSION = typer.Option(None, "--session", "-s", help="the session, if not the one opened last")


def _last_path() -> str:
    return os.path.join(wsp.home(), "weights-session")


def _sid(session: Optional[str]) -> str:
    held = os.environ.get("CST_WEIGHTS_SESSION")   # an agent Captain started works on one session and no other
    if held:
        if session and session != held:
            fail(f"This run works on session {held} and no other.")
        return held
    if session:
        return session
    try:
        with open(_last_path()) as f:
            sid = f.read().strip()
    except OSError:
        sid = ""
    if not sid:
        fail("No session is open. Open one with `cst weights open <model>`, or name one with --session.")
    return sid


def _do(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except WSN.Refused as e:
        fail(f"Refused. {e}", 3)
    except (WeightsError, NotFound) as e:
        fail(str(e).strip("'\""))
    except Conflict:
        fail("Something else wrote this at the same moment. Try again.")
    except (ValueError, KeyError) as e:
        fail(str(e).strip("'\""))


def _nums(text: Optional[str], what: str) -> list[float] | None:
    if text in (None, ""):
        return None
    try:
        return [float(v) for v in str(text).replace(" ", "").split(",") if v != ""]
    except ValueError:
        fail(f"{what} is numbers with commas between them, like 0.9,0.1.")


def _ints(text: Optional[str], what: str) -> list[int] | None:
    if text in (None, ""):
        return None
    try:
        return [int(v) for v in str(text).replace(" ", "").split(",") if v != ""]
    except ValueError:
        fail(f"{what} is whole numbers with commas between them, like 0,2.")


def _one(n: Optional[int], what: str) -> Optional[int]:
    """A number a person typed, counted from 1, as the services count it, from 0."""
    if n is None:
        return None
    if n < 1:
        fail(f"{what} counts from 1.")
    return n - 1


def _ones(text: Optional[str], what: str) -> list[int] | None:
    vals = _ints(text, what)
    return None if vals is None else [_one(v, what) for v in vals]


def _layer_tensors(sid: str, layer: int) -> dict:
    """The tensors of a layer a person named, counted from 1."""
    rows = _do(WSN.session_layers, ctx(), sid)
    if not 1 <= layer <= len(rows):
        fail(f"This network has {len(rows)} layers, and --layer counts from 1.")
    return rows[layer - 1]


def _run_args(inputs: list[str], input_file: Optional[str], target: Optional[str], example: Optional[str]) -> dict:
    out: dict = {}
    if example not in (None, ""):
        out["example"] = _ones(example, "--example")
    elif input_file:
        try:
            with open(input_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            fail(f"{input_file} could not be read as JSON. It should hold a list of inputs, or {{\"inputs\": [...], \"targets\": [...]}}.")
        out["inputs"] = data.get("inputs") if isinstance(data, dict) else data
        if isinstance(data, dict) and data.get("targets") is not None:
            out["targets"] = data["targets"]
    elif inputs:
        out["inputs"] = [_nums(i, "--input") for i in inputs]
    if target not in (None, ""):
        t = _nums(target, "--target")
        out["targets"] = [t] if out.get("inputs") is None or len(out["inputs"]) == 1 else [[v] for v in t]
    return out


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _f(x) -> str:
    return "none" if x is None else f"{float(x):.4g}"


def _ledger_lines(led: dict) -> list[str]:
    return [f"label: {led['label']}",
            f"raw weights: {'allowed' if led['raw_weights'] else 'not allowed'}, {led['weights_read']:,} shown",
            f"sandbox: {'allowed' if led['sandbox'] else 'not allowed'}" + (", used" if led["sandbox_used"] else ""),
            f"forward passes: {led['forward_used']} of {led['forward_allowed']} used",
            f"gradients computed: {'yes' if led['gradients_computed'] else 'no'}",
            f"evaluations run by the tool: {led['evaluations']}",
            "scores: " + ("hidden until the session is saved or closed" if not led["feedback"] and not led["scores_shown"]
                          else f"first shown by {led['first_score']['op']} ({led['first_score']['event']})" if led["scores_shown"] else "none shown yet"),
            f"looked at by the screen: {led['screen_looks']} times, {led['screen_numbers']:,} numbers",
            f"changed: {led['changed']:,} of {led['total']:,} numbers ({led['share'] * 100:.2f}%)"]


def _session_text(s: dict) -> str:
    sets = "\n".join(f"  {k.replace('_', ' ')}: " + (f"{_n(v['of'], 'example')}, {v['says']}" if v["defined"] else "not defined")
                     for k, v in s["sets"].items())
    return "\n".join([f"session {s['id']} on {s['base']}, opened by {s['who']['name']} ({s['who']['kind']})",
                      *_ledger_lines(s["ledger"]), "sets:", sets, f"edits: {len(s['edits'])}, events: {s['event_count']}"])


def _cost_lines(cost: dict) -> list[str]:
    out = []
    for key in ("target", "keep", "test"):
        c = cost.get(key)
        if c:
            out.append(f"  {c['name']}: cost {_f(c['before'])} to {_f(c['after'])}, right {c['right_before']} to {c['right_after']} of {c['of']}, "
                       f"{c['answers_changed']} answers changed")
    return out


def _event_text(ev: dict) -> str:
    return "\n".join([f"{ev['id']}  {ev['sentence']}", *_cost_lines(ev.get("cost") or {}), *([f"  {WSN.BLIND}"] if ev.get("scores") == "hidden" else [])])


# ---- models and sessions -------------------------------------------------------------------------------------------------

@weights_app.command("models")
def models(project: Optional[str] = typer.Option(None, "--project", help="only the models in this project")):
    """The networks that can be opened. The two starters are made on first use."""
    rows = _do(WST.models, ctx(), project)
    emit(rows, "\n".join(f"{r['id']:16} {'-'.join(str(s) for s in r['sizes']):16} {r['parameters']:>8,} numbers  v{r['latest']}  "
                         f"{'synthetic data. ' if r['synthetic'] else ''}{r['says']}" for r in rows) or "no weights models")


@weights_app.command("show")
def show(model: str):
    """A network's description: its sizes, what its inputs and outputs mean, and its examples. No weights: those are
    read through a session, where they are counted."""
    w = _do(WSN.whole, ctx(), model)
    if w.get("parts") is not None:    # a trained session model: its parts, in words
        return emit(w, weights_real.show_text(w))
    emit(w, f"{w['name']} v{w['version']}: {'-'.join(str(s) for s in w['sizes'])}, {w['parameters']:,} numbers, {w['cost']} cost.\n"
            + "\n".join(f"layer {k + 1}: {ly['inputs']} in, {ly['outputs']} out, {ly['activation']}" for k, ly in enumerate(w["layers"]))
            + f"\ninputs: {w['inputMeaning']}\noutputs: {w['outputMeaning']}\n{_n(len(w['examples']), 'example')} to look at."
            + (f" {w['data_note']}" if w["data_note"] else "") + "\nTo read its numbers, open a session: `cst weights open " + w["name"] + "`.")


@weights_app.command("open")
def open_(model: str = typer.Argument(..., help="a model, or one version of it: digits or digits@v2"),
          agent: bool = typer.Option(False, "--agent", help="the session is an agent's, not a person's"),
          name: Optional[str] = typer.Option(None, "--name", help="who is working"),
          raw_weights: Optional[bool] = typer.Option(None, "--raw-weights/--no-raw-weights", help="may raw numbers be shown (default yes)"),
          sandbox: Optional[bool] = typer.Option(None, "--sandbox/--no-sandbox", help="may code work on the tensors (default no)"),
          forward_passes: Optional[int] = typer.Option(None, "--forward-passes", help="how many times the network may be run (default 50 for a person, 0 for an agent)"),
          pass_inputs: Optional[int] = typer.Option(None, "--pass-inputs", help="how many inputs one pass may hold (default 32)"),
          blind: bool = typer.Option(False, "--blind", help="measure and keep every score, and show none until the session is saved or closed")):
    """Open a session. What it is allowed is fixed here and recorded."""
    if S.dry_run:
        return emit({"dry_run": True, "would": f"open a session on {model}"}, f"would open a session on {model}")
    s = _do(WSN.open_session, ctx(), model, by="agent" if agent else "you", name=name, raw_weights=raw_weights, sandbox=sandbox,
            forward_passes=forward_passes, pass_inputs=pass_inputs, feedback=not blind)
    os.makedirs(wsp.home(), exist_ok=True)
    with open(_last_path(), "w") as f:
        f.write(s["id"])
    emit(s, _session_text(s))


@weights_app.command("sessions")
def sessions(model: str):
    """The sessions opened on a model, newest first."""
    rows = _do(WSN.sessions, ctx(), model)
    emit(rows, "\n".join(f"{r['id']}  {r['opened_at']}  {r['who']['name']} ({r['who']['kind']})  {r['edits']} edits" for r in rows) or "no sessions")


@weights_app.command("session")
def session(session: Optional[str] = SESSION):
    """The session: what it was allowed, what it used, and its label."""
    s = _do(WSN.session, ctx(), _sid(session))
    emit(s, _session_text(s))


@weights_app.command("report")
def report(session: Optional[str] = SESSION):
    """The whole record: allowances, every operation, every edit kept or taken back, and the label."""
    r = _do(WSN.report, ctx(), _sid(session))
    lines = [_session_text(r), "kept: " + (", ".join(f"{e['id']} {e['op']}" for e in r["kept"]) or "nothing"),
             "taken back: " + (", ".join(f"{e['id']} {e['op']}" for e in r["taken_back"]) or "nothing"),
             "saved as: " + (", ".join(r["saved"]) or "nothing"), "record:", *[f"  {e['id']:>4}  {e['at']}  {e['sentence']}" for e in r["events"]]]
    emit(r, "\n".join(lines))


@weights_app.command("events")
def events(session: Optional[str] = SESSION, since: int = typer.Option(0, "--since", help="only events after this number")):
    """What has happened in the session, one sentence each."""
    r = _do(WSN.events, ctx(), _sid(session), since)
    emit(r, "\n".join(f"{e['id']:>4}  {e['sentence']}" for e in r["events"]) or "nothing yet")


# ---- read ------------------------------------------------------------------------------------------------------------------

@weights_app.command("layers")
def layers(session: Optional[str] = SESSION):
    """The layers: sizes, activation, tensor names and how many numbers each holds."""
    r = _do(WSN.layers, ctx(), _sid(session))
    if r.get("parts") is not None:
        return emit(r, weights_real.parts_text(r))
    lines = [f"{'-'.join(str(s) for s in r['sizes'])}, {r['parameters']:,} numbers, {r['cost']} cost"]
    lines += [f"layer {ly['layer'] + 1}: {ly['inputs']} in, {ly['outputs']} out, {ly['activation']}, "
              f"{ly['weight']['tensor']} and {ly['bias']['tensor']}, {ly['parameters']:,} numbers" for ly in r["layers"]]
    emit(r, "\n".join(lines))


@weights_app.command("read")
def read(tensor: Optional[str] = typer.Argument(None, help="a tensor by name, such as layer1.weight. Or use --layer"),
         layer: Optional[int] = typer.Option(None, "--layer", help="the layer, counted from 1, instead of a tensor name"),
         neuron: Optional[int] = typer.Option(None, "--neuron", help="one neuron of that layer, counted from 1: its incoming weights"),
         bias: bool = typer.Option(False, "--bias", help="with --layer: the biases, not the weights"),
         rows: Optional[str] = typer.Option(None, "--rows", help="with a tensor name: a row or a range, counted from 0: 3 or 0:4"),
         cols: Optional[str] = typer.Option(None, "--cols", help="a column or a range, counted from 0"), session: Optional[str] = SESSION):
    """A slice of raw numbers. Counted against "raw weights shown"."""
    sid = _sid(session)
    if layer is not None:
        if tensor or rows:
            fail("Name a tensor with --rows, or a layer with --neuron, not both.")
        ly = _layer_tensors(sid, layer)
        tensor = ly["bias" if bias else "weight"]["tensor"]
        rows = None if neuron is None else str(_one(neuron, "--neuron"))
    elif neuron is not None or bias:
        fail("--neuron and --bias go with --layer.")
    if not tensor:
        fail("Name a tensor, such as layer1.weight, or a layer with --layer.")
    r = _do(WSN.read, ctx(), sid, tensor, rows, cols)
    vals = r["values"] if r["cols"] else [r["values"]]
    where = (f"layer {layer}" + (f", neuron {neuron}" if neuron is not None else "") + (", biases" if bias else ", incoming weights")) if layer is not None \
        else f"rows {r['rows'][0]}:{r['rows'][1]}" + (f", columns {r['cols'][0]}:{r['cols'][1]}" if r["cols"] else "") + " (counted from 0)"
    emit(r, f"{r['tensor']}, {where}\n" + "\n".join("  ".join(f"{v: .4f}" for v in row) for row in vals))


@weights_app.command("stats")
def stats(tensor: Optional[str] = typer.Argument(None, help="one tensor by name. Left out: every tensor"),
          layer: Optional[int] = typer.Option(None, "--layer", help="one layer, counted from 1, instead of a tensor name"),
          neuron: Optional[int] = typer.Option(None, "--neuron", help="with --layer: only this neuron's line, counted from 1"),
          neurons: bool = typer.Option(False, "--neurons", help="also one line per neuron"), session: Optional[str] = SESSION):
    """Numbers about the numbers, weights and biases apart."""
    sid = _sid(session)
    if layer is not None:
        if tensor:
            fail("Name a tensor or a layer, not both.")
        tensor = _layer_tensors(sid, layer)["weight"]["tensor"]
    elif neuron is not None:
        fail("--neuron goes with --layer.")
    r = _do(WSN.stats, ctx(), sid, tensor, neurons or neuron is not None)
    if neuron is not None:
        r["neurons"] = [n for n in r["neurons"] if n["neuron"] == _one(neuron, "--neuron")]
        if not r["neurons"]:
            fail(f"Layer {layer} has no neuron {neuron}.")
    lines = [f"{t['tensor']:16} n={t['count']:<6} mean {_f(t['mean'])}  variance {_f(t['variance'])}  min {_f(t['min'])}  median {_f(t['median'])}  "
             f"max {_f(t['max'])}  zeros {t['zero_share'] * 100:.0f}%  distinct {t['distinct']}" for t in r["tensors"]]
    lines += [f"layer {n['layer'] + 1}, neuron {n['neuron'] + 1}: bias {_f(n['bias'])}, weight size {_f(n['weight_norm'])}, "
              f"from {_f(n['weight_min'])} to {_f(n['weight_max'])}" + (", every weight is zero" if n["dead"] else "") for n in r.get("neurons", [])]
    if "bias_range" not in r:    # a model read by tensor: one line per row, when asked for
        lines += [f"row {n['row']}: size {_f(n['norm'])}, from {_f(n['min'])} to {_f(n['max'])}" + (", every number is zero" if n["dead"] else "")
                  for n in r.get("rows", [])]
        return emit(r, "\n".join(lines))
    lines.append(f"bias range: first layer {_f(r['bias_range']['first'])}, last layer {_f(r['bias_range']['last'])}")
    emit(r, "\n".join(lines))


@weights_app.command("heatmap")
def heatmap(layer: int = typer.Option(1, "--layer", help="the layer, counted from 1"),
            neuron: Optional[str] = typer.Option(None, "--neuron", help="one neuron or several, counted from 1: 3 or 1,3"),
            tensor: Optional[str] = typer.Option(None, "--tensor", help="a matrix by name, instead of a layer. Needed for a trained session model"),
            rows: Optional[str] = typer.Option(None, "--rows", help="with --tensor: a row or a range, counted from 0: 3 or 0:48"),
            cols: Optional[str] = typer.Option(None, "--cols", help="with --tensor: a column or a range, counted from 0"),
            session: Optional[str] = SESSION):
    """A layer's weights per input as grids, one per neuron. With --tensor: any matrix, or a part of it, as one grid."""
    r = _do(WSN.heatmap, ctx(), _sid(session), _one(layer, "--layer"), _ones(neuron, "--neuron"), tensor=tensor, rows=rows, cols=cols)
    if "grid" in r:
        return emit(r, f"{r['tensor']}, rows {r['rows'][0]}:{r['rows'][1]}, columns {r['cols'][0]}:{r['cols'][1]} (counted from 0): one grid of "
                       f"{r['grid_shape'][0]} by {r['grid_shape'][1]}, from {_f(r['min'])} to {_f(r['max'])}. Use --json for the numbers.")
    emit(r, f"layer {r['layer'] + 1}: {len(r['grids'])} grids of {r['grid_shape'][0]} by {r['grid_shape'][1]}, largest weight {_f(r['scale'])}. "
            "Use --json for the numbers.")


@weights_app.command("template")
def template(output: int = typer.Argument(1, help="the output neuron, counted from 1"), session: Optional[str] = SESSION):
    """One output's incoming weights: which neurons before it vote for this output."""
    r = _do(WSN.template, ctx(), _sid(session), _one(output, "The output"))
    emit(r, f"output {r['output'] + 1} ({r['name']}): bias {_f(r['bias'])}\n" + "  ".join(f"{v: .3f}" for v in r["weights"]))


# ---- run -------------------------------------------------------------------------------------------------------------------

INPUT = typer.Option([], "--input", "-i", help="one input as numbers with commas: 0.9,0.1. Repeat for more")
INPUT_FILE = typer.Option(None, "--input-file", help="a JSON file: a list of inputs, or {inputs, targets}")
TARGET = typer.Option(None, "--target", "-t", help="the wanted output, as numbers with commas or one class number per input")
EXAMPLE = typer.Option(None, "--example", "-e", help="one of the model's examples, counted from 1, or several: 3 or 1,2,3")


def _pos(layer: int, index: list) -> str:
    """One number by where it sits, counted from 1."""
    if len(index) == 1:
        return f"the bias of layer {layer + 1}, neuron {index[0] + 1}"
    return f"the weight into layer {layer + 1}, neuron {index[0] + 1} from neuron {index[1] + 1}"


def _passes(r: dict) -> str:
    return f"{_n(r['forward_used'], 'forward pass', 'forward passes')} used, {r['forward_left']} left."


@weights_app.command("forward")
def forward(input: list[str] = INPUT, input_file: Optional[str] = INPUT_FILE, target: Optional[str] = TARGET, example: Optional[str] = EXAMPLE,
            session: Optional[str] = SESSION):
    """Run the network. Costs one forward pass, and is refused when none are left."""
    r = _do(WSN.forward, ctx(), _sid(session), **_run_args(input, input_file, target, example))
    emit(r, "\n".join(f"answer {p}: " + "  ".join(f"{v:.3f}" for v in o) for p, o in zip(r["predicted"], r["outputs"]))
            + (f"\ncost {_f(r['cost'])}" if "cost" in r else "") + "\n" + _passes(r))


@weights_app.command("activations")
def activations(input: list[str] = INPUT, input_file: Optional[str] = INPUT_FILE, target: Optional[str] = TARGET, example: Optional[str] = EXAMPLE,
                layer: Optional[int] = typer.Option(None, "--layer", help="only this layer, counted from 1"),
                neuron: Optional[int] = typer.Option(None, "--neuron", help="with --layer: only this neuron, counted from 1"),
                session: Optional[str] = SESSION):
    """Every neuron's weighted sum and activation for one input. Costs one forward pass."""
    r = _do(WSN.activations, ctx(), _sid(session), **_run_args(input, input_file, target, example))
    if layer is not None:
        r["columns"] = [c for c in r["columns"] if c["column"] == layer]
        if not r["columns"]:
            fail(f"This network has no layer {layer}. --layer counts from 1.")
        if neuron is not None:
            j = _one(neuron, "--neuron")
            if j >= len(r["columns"][0]["a"]):
                fail(f"Layer {layer} has no neuron {neuron}.")
            c = r["columns"][0]
            return emit(r, f"layer {layer}, neuron {neuron}: weighted sum {_f(c['z'][j])}, activation {_f(c['a'][j])}\n" + _passes(r))
    elif neuron is not None:
        fail("--neuron goes with --layer.")
    emit(r, "\n".join(f"layer {c['column']}: " + "  ".join(f"{v:.3f}" for v in c["a"]) for c in r["columns"])
            + f"\nanswer {r['predicted']}" + (f", cost {_f(r['cost'])}" if "cost" in r else "") + "\n" + _passes(r))


@weights_app.command("gradients")
def gradients(input: list[str] = INPUT, input_file: Optional[str] = INPUT_FILE, target: Optional[str] = TARGET, example: Optional[str] = EXAMPLE,
              layer: Optional[int] = typer.Option(None, "--layer", help="a layer, counted from 1. With --neuron: that neuron's whole row of gradients"),
              neuron: Optional[int] = typer.Option(None, "--neuron", help="a neuron of that layer, counted from 1"), session: Optional[str] = SESSION):
    """The cost and its gradient for every weight and bias, and what each neuron wants. Costs one forward pass."""
    r = _do(WSN.gradients, ctx(), _sid(session), layer=_one(layer, "--layer"), neuron=_one(neuron, "--neuron"),
            **_run_args(input, input_file, target, example))
    lines = [f"cost {_f(r['cost'])} over {r['examples']} example{'s' if r['examples'] != 1 else ''}", "the numbers the cost is most sensitive to:"]
    lines += [f"  {_pos(t['layer'], t['index'])}: value {_f(t['value'])}, gradient {_f(t['gradient'])}" for t in r["top"][:10]]
    lines += [f"layer {ly['column']} wants: " + "  ".join(f"{v:+.3g}" for v in ly["wanted_nudge"]) for ly in r["layers"]]
    if r.get("row"):
        lines.append(f"layer {layer}, neuron {neuron}: bias gradient {_f(r['row']['bias_gradient'])}, weight gradients "
                     + "  ".join(f"{v:+.3g}" for v in r["row"]["gradient"][:16]) + (" and more" if len(r["row"]["gradient"]) > 16 else ""))
    emit(r, "\n".join(lines + [_passes(r)]))


@weights_app.command("trace")
def trace(input: list[str] = INPUT, input_file: Optional[str] = INPUT_FILE, target: Optional[str] = TARGET, example: Optional[str] = EXAMPLE,
          path: Optional[str] = typer.Option(None, "--path", help="one neuron per column, input first, each counted from 1: 1,3,1"),
          session: Optional[str] = SESSION):
    """The chain rule along one path: for each layer, the three factors and their product. Costs one forward pass."""
    r = _do(WSN.trace, ctx(), _sid(session), path=_ones(path, "--path"), **_run_args(input, input_file, target, example))
    lines = [f"path through neurons {', '.join(str(p + 1) for p in r['path'])} (input first), cost {_f(r['cost'])}"]
    for s in r["steps"]:
        lines.append(f"{_pos(s['layer'], s['weight']['index'])} = {_f(s['weight']['value'])}: "
                     f"neuron before {_f(s['a_before'])} times steepness {_f(s['da_dz'])} times how far off {_f(s['dcost_da'])} "
                     f"gives gradient {_f(s['gradient'])}" + ("" if s["product_is_exact"] else " (softmax: the product is not exact here)"))
    lines.append(f"this path adds {_f(r['path_product'])} to its first weight's gradient of {_f(r['first_weight_gradient'])}, "
                 f"which has {_n(r['paths_from_first_weight'], 'path')} to the cost")
    emit(r, "\n".join(lines + [_passes(r)]))


@weights_app.command("slice")
def slice_(tensor: Optional[str] = typer.Argument(None, help="a tensor by name, with the position after it. Or use --layer"),
           index: Optional[str] = typer.Argument(None, help="with a tensor name: the position, counted from 0: 2,0 for a weight, 2 for a bias"),
           layer: Optional[int] = typer.Option(None, "--layer", help="the neuron's layer, counted from 1"),
           neuron: Optional[int] = typer.Option(None, "--neuron", help="the neuron, counted from 1"),
           from_: Optional[int] = typer.Option(None, "--from", help="which incoming weight, counted from 1. Left out: the bias"),
           input: list[str] = INPUT, input_file: Optional[str] = INPUT_FILE, target: Optional[str] = TARGET, example: Optional[str] = EXAMPLE,
           span: Optional[float] = typer.Option(None, "--span", help="how wide a range to cover, centred on the number's value"),
           points: int = typer.Option(21, "--points", help="how many values to try. Each one costs a forward pass"),
           session: Optional[str] = SESSION):
    """The cost as one number moves and the rest stay put. Costs one forward pass per point. Changes nothing."""
    sid = _sid(session)
    if layer is not None:
        if tensor or neuron is None:
            fail("Name a tensor and a position, or --layer with --neuron (and --from for a weight).")
        ly = _layer_tensors(sid, layer)
        j = _one(neuron, "--neuron")
        tensor, pos = (ly["bias"]["tensor"], [j]) if from_ is None else (ly["weight"]["tensor"], [j, _one(from_, "--from")])
    else:
        if not tensor or index is None:
            fail("Name a tensor and a position, or --layer with --neuron (and --from for a weight).")
        pos = _ints(index, "the position")
    r = _do(WSN.cost_slice, ctx(), sid, tensor, pos, span=span, points=points, **_run_args(input, input_file, target, example))
    k = int(next(i for i, ly in enumerate(_do(WSN.session_layers, ctx(), sid)) if tensor in (ly["weight"]["tensor"], ly["bias"]["tensor"])))
    if r.get("scores") == "hidden":
        return emit(r, f"measured at {points} values. {r['says']}\n" + _passes(r))
    low = min(range(len(r["costs"])), key=lambda i: r["costs"][i])
    emit(r, f"{_pos(k, r['index']).capitalize()} is {_f(r['value'])}, where the cost is {_f(r['cost'])} and its slope is {_f(r['slope'])}.\n"
            f"Over {_f(r['values'][0])} to {_f(r['values'][-1])} the cost is lowest at {_f(r['values'][low])}: {_f(r['costs'][low])}.\n"
            f"This is a slice through 1 of {r['dimensions']:,} dimensions.\n" + _passes(r))


# ---- change ----------------------------------------------------------------------------------------------------------------

@weights_app.command("sets")
def sets(target: Optional[str] = typer.Option(None, "--target", help="classes the edits should fix: 9 or 4,9"),
         keep: Optional[str] = typer.Option(None, "--keep", help="classes the edits must leave alone: 0,1,2"),
         target_example: Optional[int] = typer.Option(None, "--target-example", help="fix this one: one of the model's examples, counted from 1, as a target of one"),
         target_input: Optional[str] = typer.Option(None, "--target-input", help="fix this one: an input as numbers with commas. Give --target-output with it"),
         target_output: Optional[str] = typer.Option(None, "--target-output", help="the wanted output for --target-input: numbers with commas, or one class number"),
         session: Optional[str] = SESSION):
    """Name the target set and the "should not change" set. With nothing given: show the three sets.

    --target and --keep take class numbers, which are the classes' own labels. A single wrong answer can be the
    target: --target-example 3, or --target-input with --target-output."""
    if sum(x is not None for x in (target, target_example, target_input)) > 1:
        fail("Give one target: --target, --target-example, or --target-input with --target-output.")
    t = {"classes": _ints(target, "--target")} if target else None
    if target_example is not None:
        t = {"example": _one(target_example, "--target-example")}
    if target_input is not None:
        if target_output is None:
            fail("--target-input needs --target-output: the answer that is wanted for it.")
        want = _nums(target_output, "--target-output")
        t = {"inputs": [_nums(target_input, "--target-input")], "targets": [int(want[0])] if len(want) == 1 and float(want[0]).is_integer() else [want]}
    k = {"classes": _ints(keep, "--keep")} if keep else None
    if t is None and k is None:
        r = _do(WSN.session, ctx(), _sid(session))["sets"]
    else:
        r = _do(WSN.set_sets, ctx(), _sid(session), t, k)
    emit(r, "\n".join(f"{name.replace('_', ' ')}: " + (f"{_n(v['of'], 'example')}, {v['says']}" if v["defined"] else "not defined") for name, v in r.items()))


@weights_app.command("edit")
def edit(op: str = typer.Argument(..., help="set, scale, zero, add, add-direction or step"),
         layer: Optional[int] = typer.Option(None, "--layer", help="the neuron's layer, counted from 1: 1 is the first after the input"),
         neuron: Optional[int] = typer.Option(None, "--neuron", help="the neuron, counted from 1"),
         from_: Optional[int] = typer.Option(None, "--from", help="which incoming weight, by the neuron it comes from, counted from 1. Left out: the bias"),
         value: Optional[float] = typer.Option(None, "--value"), factor: Optional[float] = typer.Option(None, "--factor"),
         bias: bool = typer.Option(False, "--bias", help="with scale on a neuron: scale its bias too"),
         tensor: Optional[str] = typer.Option(None, "--tensor", help="a tensor by name, instead of a neuron"),
         index: list[str] = typer.Option([], "--index", help="with --tensor: a position in the tensor, counted from 0: 2,0. Repeat for more"),
         values: Optional[str] = typer.Option(None, "--values", help="one number per position, or one for all"),
         neurons: Optional[str] = typer.Option(None, "--neurons", help="with --tensor: whole neurons of that layer, counted from 1: 1,3"),
         direction: Optional[str] = typer.Option(None, "--direction", help="for add-direction: numbers with commas"),
         scale: Optional[float] = typer.Option(None, "--scale", help="for add-direction: how much of the direction to add"),
         along: Optional[str] = typer.Option(None, "--along", help="for add-direction: inputs (default) or outputs"),
         example: Optional[str] = typer.Option(None, "--example", help="for step: the examples to train on, counted from 1"),
         rate: Optional[float] = typer.Option(None, "--rate", help="for step: the learning rate"),
         why: Optional[str] = typer.Option(None, "--why", help="one sentence: what you expect this edit to do. An agent must give it"),
         session: Optional[str] = SESSION):
    """Change numbers. The edit is stored as a patch and measured on the three sets before and after."""
    req = {"op": op, "layer": layer, "neuron": _one(neuron, "--neuron"), "from": _one(from_, "--from"), "value": value, "factor": factor,
           "bias": bias or None,
           "tensor": tensor, "indices": [_ints(i, "--index") for i in index] or None, "values": _nums(values, "--values"),
           "neurons": _ones(neurons, "--neurons"), "direction": _nums(direction, "--direction"), "scale": scale, "along": along,
           "example": _ones(example, "--example"), "rate": rate, "why": why}
    req = {k: v for k, v in req.items() if v is not None}
    if tensor and neuron is not None and "neurons" not in req:
        req["neurons"] = [req.pop("neuron")]
    if S.dry_run:
        return emit({"dry_run": True, "would": req}, f"would apply: {json.dumps(req)}")
    ev = _do(WSN.edit, ctx(), _sid(session), req)
    emit(ev, _event_text(ev))


@weights_app.command("undo")
def undo(edit: Optional[str] = typer.Argument(None, help="the edit to take back, such as e4. Left out: the last one"), session: Optional[str] = SESSION):
    """Take an edit back. The exact old numbers return, and the edit stays in the record."""
    ev = _do(WSN.undo, ctx(), _sid(session), edit)
    emit(ev, _event_text(ev))


@weights_app.command("redo")
def redo(edit: Optional[str] = typer.Argument(None, help="the edit to put back. Left out: the one taken back last"), session: Optional[str] = SESSION):
    """Put back an edit that was taken back."""
    ev = _do(WSN.redo, ctx(), _sid(session), edit)
    emit(ev, _event_text(ev))


@weights_app.command("diff")
def diff(session: Optional[str] = SESSION):
    """What differs from the base, counted from the tensors."""
    r = _do(WSN.diff, ctx(), _sid(session))
    lines = [f"{r['changed']:,} of {r['total']:,} numbers differ from {r['base']} ({r['share'] * 100:.2f}%)"]
    lines += [f"  {t['tensor']}: {t['changed']:,} of {t['total']:,}, largest change {_f(t['largest_change'])}" for t in r["by_tensor"]]
    emit(r, "\n".join(lines))


@weights_app.command("compare")
def compare(a: str, b: str):
    """What differs between two versions of one line of edits. Two networks trained separately are refused."""
    r = _do(WSN.compare, ctx(), a, b)
    lines = [f"{r['changed']:,} of {r['total']:,} numbers differ between {r['a']} and {r['b']} ({r['share'] * 100:.2f}%)"]
    lines += [f"  {t['tensor']}: {t['changed']:,} of {t['total']:,}, largest change {_f(t['largest_change'])}" for t in r["by_tensor"]]
    emit(r, "\n".join(lines))


@weights_app.command("evaluate")
def evaluate(session: Optional[str] = SESSION):
    """The base against the network now, on the three sets, and how far each layer's outputs moved."""
    r = _do(WSN.evaluate, ctx(), _sid(session))
    if r.get("scores") == "hidden":
        return emit(r, f"measured against {r['before_is']}, {r['changed']:,} of {r['total']:,} numbers changed. {r['says']}")
    lines = [f"{r['before_is']} against the network now, {r['changed']:,} of {r['total']:,} numbers changed"]
    for v in r["sets"].values():
        lines.append(f"  {v['name']}: " + (f"cost {_f(v['before'])} to {_f(v['after'])}, right {v['right_before']} to {v['right_after']} of {v['of']}, "
                                           f"{v['answers_changed']} answers changed ({v['says']})" if v["defined"] else "not defined"))
    if r["drift"]:
        lines.append(f"how far each layer's outputs moved from the base, on the {r['drift_on']} set:")
        lines += [f"  layer {d['column']}: mean change {_f(d['mean_change'])}, relative {_f(d['relative'])}, similarity {_f(d['similarity'])}" for d in r["drift"]]
    emit(r, "\n".join(lines))


@weights_app.command("save")
def save(note: str = typer.Option("", "--as", "--note", help="a note saying what this version is"), session: Optional[str] = SESSION):
    """Save the network as a new version: the base plus patches, with the record attached. Nothing is overwritten."""
    if S.dry_run:
        return emit({"dry_run": True, "would": "save a new version"}, "would save a new version")
    r = _do(WSN.save, ctx(), _sid(session), note)
    emit(r, f"saved {r['ref']}: {r['base']} plus {_n(r['changed'], 'changed number')} ({r['share'] * 100:.2f}%). Label: {r['label']}."
            + (" The scores can be read now: `cst weights evaluate`." if r["scores_shown_now"] else ""))


@weights_app.command("note")
def note(text: str = typer.Argument(..., help="a hypothesis before an edit, or a conclusion after"), session: Optional[str] = SESSION):
    """Put a thought in the record. It shows on the live feed as "The agent thinks: ..."."""
    ev = _do(WSN.note, ctx(), _sid(session), text)
    emit(ev, f"{ev['id']}  {ev['sentence']}")


@weights_app.command("done")
def done(text: str = typer.Argument(..., help="one short paragraph: what you found and what you did"), session: Optional[str] = SESSION):
    """Say the work is finished. A run started by `cst weights program` ends when it sees this."""
    ev = _do(WSN.done, ctx(), _sid(session), text)
    emit(ev, f"{ev['id']}  {ev['sentence']}")


@weights_app.command("program")
def program(model: str = typer.Argument(..., help="a model, or one version of it: digits or digits@v2"),
            goal: str = typer.Option(..., "--goal", help="in plain words: what should the network do differently"),
            agent: Optional[str] = typer.Option(None, "--agent", help="claude or codex. Left out: whichever is installed, claude first"),
            blind: bool = typer.Option(False, "--blind", help="hide every score from the agent until the end"),
            passes: int = typer.Option(0, "--passes", help="how many times the agent may run the network (default 0)"),
            max_edits: int = typer.Option(5, "--max-edits", help="how many edits the agent may make"),
            target_example: Optional[int] = typer.Option(None, "--target-example", help="fix this one: one of the model's examples, counted from 1"),
            target_input: Optional[str] = typer.Option(None, "--target-input", help="fix this one: an input as numbers with commas"),
            target_output: Optional[str] = typer.Option(None, "--target-output", help="the wanted output for --target-input: numbers, or one class number"),
            leave_alone: Optional[str] = typer.Option(None, "--leave-alone", help="classes that must not change: 0,1,2. Left out: every class in the model's evaluation pool"),
            keep: Optional[bool] = typer.Option(None, "--keep/--undo-if-worse", help="keep the result whatever the scores say and even if the run is stopped, or take it back if it did harm or was stopped (the default)"),
            project: Optional[str] = typer.Option(None, "--project", help="with --deploy-on: the project that deploys a kept result"),
            deploy_on: Optional[str] = typer.Option(None, "--deploy-on", help="deploy a kept result: local"),
            time_limit: int = typer.Option(600, "--time-limit", help="seconds before the agent is stopped (default 600)")):
    """Captain starts an agent on the network with a goal, follows it, and gives the verdict itself.

    The agent can run only `cst weights` commands on its own session. Every edit is a patch in the record and shows
    on the Weights page as it happens. When the agent stops, Captain measures the result: one that moved "should
    not change", made the held-out set worse or did not help the target is taken back whole. One that is kept is
    saved as a new version. Ctrl-C, or the time limit, stops the agent: its edits are then taken back and nothing is
    saved, unless --keep was given. They stay in the record as taken back."""
    from ..weights import program as WPG

    if bool(project) != bool(deploy_on):
        fail("--project and --deploy-on go together.")
    if sum(x is not None for x in (target_example, target_input)) > 1:
        fail("Give one target: --target-example, or --target-input with --target-output.")
    c = ctx()
    plan = _do(WPG.prepare, c, model, goal, agent=agent, blind=blind, passes=passes, max_edits=max_edits,
               target_example=_one(target_example, "--target-example"), target_input=_nums(target_input, "--target-input"),
               target_output=_nums(target_output, "--target-output"), should_not_change=_ints(leave_alone, "--leave-alone"),
               time_limit_s=time_limit, dry_run=S.dry_run)
    if S.dry_run:
        return emit(plan, "\n".join([
            f"Would start {plan['agent']} ({plan['agent_path']}) on {plan['model']}, in {plan['session']}.",
            f"Goal: {plan['goal']}",
            f"Allowed: {plan['passes']} forward passes, {plan['max_edits']} edits, {plan['time_limit_s']} seconds, scores "
            f"{'hidden from the agent' if plan['blind'] else 'shown to the agent'}.",
            f"Target: {plan['target']}. Should not change: {plan['should_not_change']}.",
            "Command: " + " ".join(plan["command"]),
            plan["restriction"], "The agent could run only: " + ", ".join(plan["allowed_commands"]) + ".",
            "Nothing was started. The instructions the agent would get, before this run's own details:", "", plan["playbook"]]))
    quiet = S.json
    say = (lambda text: None) if quiet else (lambda text: typer.echo(text))

    deploying = bool(project)
    r = _do(WPG.run, c, plan, keep=keep, save=not deploying, say=say)
    if deploying and r["kept"]:
        from .deployments import _deploy_on

        try:
            _deploy_on(project, deploy_on, None, None, False, "staging", False, False, False, r["session"], f"{r['agent']}: {r['goal']}"[:200])
            saved = WSN.session(c, r["session"])["saved"]
            r["saved"], r["deployed"] = (saved[-1] if saved else None), f"{project} on {deploy_on}"
        except typer.Exit:
            r["deployed"] = None
            r["deploy_failed"] = True
    if deploying:
        _do(WSN.close, c, r["session"], "captain")
    emit(r, ("" if quiet else "\n") + WPG.report_text(r))
    if r["stopped"]:
        raise typer.Exit(130)


@weights_app.command("close")
def close(session: Optional[str] = SESSION):
    """End the session. Nothing more can be run or changed in it. A blind session's scores are shown from here on."""
    s = _do(WSN.close, ctx(), _sid(session))
    emit(s, "closed.\n" + _session_text(s))


@weights_app.command("export")
def export(folder: str, npz: bool = typer.Option(False, "--npz", help="write tensors.npz, not tensors.safetensors"), session: Optional[str] = SESSION):
    """Write the tensors into a folder for code to work on. Only for a session opened with --sandbox, and recorded."""
    r = _do(WSN.export, ctx(), _sid(session), folder, "tensors.npz" if npz else "tensors.safetensors")
    emit(r, f"wrote {', '.join(r['files'])} into {r['folder']}. The session now says a sandbox was used.")


@weights_app.command("import")
def import_(folder: str, name: Optional[str] = typer.Option(None, "--name", "--as", help="the model's name here. Left out: the manifest's"),
            project: Optional[str] = typer.Option(None, "--project", help="also put it in this project")):
    """Bring in a model folder (manifest.json and its tensor files) made by any training code, as a new version.
    A cost.jsonl in the folder is kept with it as the training's cost per step.

    A folder of trained weights (sess.npz, the rank_*.txt tree rankers, model.json), or an s3:// address of one,
    comes in as a session model with rankers: `cst weights import <folder> --as <name>`. Its files are kept
    unchanged. An address is read with this computer's default cloud login, and nothing is written to it."""
    if S.dry_run:
        return emit({"dry_run": True, "would": f"import {folder}"}, f"would import {folder}")
    c = ctx()
    if weights_real.R.looks_real(folder):
        out = _do(weights_real.R.import_from, c, folder, name)
        if project:
            _do(WST.assign, c, out["model"], project)
        out["project"] = project
        return emit(out, weights_real.import_text(out))
    from ..weights import pipeline as WP

    out = _do(WP.collect, c, os.path.abspath(folder), os.path.abspath(folder), name)
    if project:
        _do(WST.assign, c, out["model"], project)
    out["project"] = project
    emit(out, f"imported {out['ref']}: {'-'.join(str(s) for s in out['sizes'])}, {out['parameters']:,} numbers"
              + (f", with {out['cost_points']:,} cost points" if out["cost_points"] else "") + f". Open it with `cst weights open {out['model']}`.")


@weights_app.command("assign")
def assign(model: str, project: str):
    """Put a weights model in a project."""
    r = _do(WST.assign, ctx(), WST.split_ref(model)[0], project)
    emit(r, f"{r['model']} is now in the project {r['project']}.")


from . import weights_real  # noqa: E402  reading a trained session model: its parts, nearest rows, directions, attention, trees
