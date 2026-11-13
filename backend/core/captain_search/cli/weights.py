"""cst weights: open a network as a session, read it, change it, and get the record of what was done.

Every command prints the same shape its route returns when --json is given (`cst --json weights layers`, or
`cst weights --json layers`). Without it, a few plain lines. The shapes are in `cst docs weights`.

Commands act on the session opened last on this computer. Name another with --session.
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


def _run_args(inputs: list[str], input_file: Optional[str], target: Optional[str], example: Optional[str]) -> dict:
    out: dict = {}
    if example not in (None, ""):
        out["example"] = _ints(example, "--example")
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
    return "\n".join([f"{ev['id']}  {ev['sentence']}", *_cost_lines(ev.get("cost") or {})])


# ---- models and sessions -------------------------------------------------------------------------------------------------

@weights_app.command("models")
def models(project: Optional[str] = typer.Option(None, "--project", help="only the models in this project")):
    """The networks that can be opened. The two starters are made on first use."""
    rows = _do(WST.models, ctx(), project)
    emit(rows, "\n".join(f"{r['id']:16} {'-'.join(str(s) for s in r['sizes']):16} {r['parameters']:>8,} numbers  v{r['latest']}  "
                         f"{'synthetic data. ' if r['synthetic'] else ''}{r['says']}" for r in rows) or "no weights models")


@weights_app.command("show")
def show(model: str, session: Optional[str] = typer.Option(None, "--session", "-s", help="with this session's edits applied")):
    """A network whole: every weight and bias, and its practice examples."""
    w = _do(WSN.whole, ctx(), model, session)
    emit(w, f"{w['name']} v{w['version']}: {'-'.join(str(s) for s in w['sizes'])}, {w['parameters']:,} numbers, {w['cost']} cost. "
            f"{len(w['examples'])} practice examples." + (f" {w['data_note']}" if w["data_note"] else "") + " Use --json for the numbers.")


@weights_app.command("open")
def open_(model: str = typer.Argument(..., help="a model, or one version of it: digits or digits@v2"),
          agent: bool = typer.Option(False, "--agent", help="the session is an agent's, not a person's"),
          name: Optional[str] = typer.Option(None, "--name", help="who is working"),
          raw_weights: Optional[bool] = typer.Option(None, "--raw-weights/--no-raw-weights", help="may raw numbers be shown (default yes)"),
          sandbox: Optional[bool] = typer.Option(None, "--sandbox/--no-sandbox", help="may code work on the tensors (default no)"),
          forward_passes: Optional[int] = typer.Option(None, "--forward-passes", help="how many times the network may be run (default 50 for a person, 0 for an agent)"),
          pass_inputs: Optional[int] = typer.Option(None, "--pass-inputs", help="how many inputs one pass may hold (default 32)")):
    """Open a session. What it is allowed is fixed here and recorded."""
    if S.dry_run:
        return emit({"dry_run": True, "would": f"open a session on {model}"}, f"would open a session on {model}")
    s = _do(WSN.open_session, ctx(), model, by="agent" if agent else "you", name=name, raw_weights=raw_weights, sandbox=sandbox,
            forward_passes=forward_passes, pass_inputs=pass_inputs)
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
    lines = [f"{'-'.join(str(s) for s in r['sizes'])}, {r['parameters']:,} numbers, {r['cost']} cost"]
    lines += [f"layer {ly['layer']} (column {ly['column']}): {ly['inputs']} in, {ly['outputs']} out, {ly['activation']}, "
              f"{ly['weight']['tensor']} and {ly['bias']['tensor']}, {ly['parameters']:,} numbers" for ly in r["layers"]]
    emit(r, "\n".join(lines))


@weights_app.command("read")
def read(tensor: str, rows: Optional[str] = typer.Option(None, "--rows", help="a row or a range: 3 or 0:4"),
         cols: Optional[str] = typer.Option(None, "--cols", help="a column or a range"), session: Optional[str] = SESSION):
    """A slice of raw numbers. Counted against "raw weights shown"."""
    r = _do(WSN.read, ctx(), _sid(session), tensor, rows, cols)
    vals = r["values"] if r["cols"] else [r["values"]]
    emit(r, f"{r['tensor']} rows {r['rows'][0]}:{r['rows'][1]}" + (f", columns {r['cols'][0]}:{r['cols'][1]}" if r["cols"] else "") + "\n"
            + "\n".join("  ".join(f"{v: .4f}" for v in row) for row in vals))


@weights_app.command("stats")
def stats(tensor: Optional[str] = typer.Argument(None), neurons: bool = typer.Option(False, "--neurons", help="also one line per neuron"),
          session: Optional[str] = SESSION):
    """Numbers about the numbers, weights and biases apart."""
    r = _do(WSN.stats, ctx(), _sid(session), tensor, neurons)
    lines = [f"{t['tensor']:16} n={t['count']:<6} mean {_f(t['mean'])}  variance {_f(t['variance'])}  min {_f(t['min'])}  median {_f(t['median'])}  "
             f"max {_f(t['max'])}  zeros {t['zero_share'] * 100:.0f}%  distinct {t['distinct']}" for t in r["tensors"]]
    lines += [f"column {n['column']} neuron {n['neuron']}: bias {_f(n['bias'])}, weight size {_f(n['weight_norm'])}, "
              f"from {_f(n['weight_min'])} to {_f(n['weight_max'])}" + (", every weight is zero" if n["dead"] else "") for n in r.get("neurons", [])]
    lines.append(f"bias range: first layer {_f(r['bias_range']['first'])}, last layer {_f(r['bias_range']['last'])}")
    emit(r, "\n".join(lines))


@weights_app.command("heatmap")
def heatmap(layer: int = typer.Option(0, "--layer", help="the layer, from 0"), neuron: Optional[str] = typer.Option(None, "--neuron", help="one neuron or several: 3 or 0,3"),
            session: Optional[str] = SESSION):
    """A layer's weights per input as grids, one per neuron."""
    r = _do(WSN.heatmap, ctx(), _sid(session), layer, neuron)
    emit(r, f"layer {r['layer']}: {len(r['grids'])} grids of {r['grid_shape'][0]} by {r['grid_shape'][1]}, largest weight {_f(r['scale'])}. "
            "Use --json for the numbers.")


@weights_app.command("template")
def template(output: int = typer.Argument(0), session: Optional[str] = SESSION):
    """One output's incoming weights: which neurons before it vote for this output."""
    r = _do(WSN.template, ctx(), _sid(session), output)
    emit(r, f"output {r['output']} ({r['name']}): bias {_f(r['bias'])}\n" + "  ".join(f"{v: .3f}" for v in r["weights"]))


# ---- run -------------------------------------------------------------------------------------------------------------------

INPUT = typer.Option([], "--input", "-i", help="one input as numbers with commas: 0.9,0.1. Repeat for more")
INPUT_FILE = typer.Option(None, "--input-file", help="a JSON file: a list of inputs, or {inputs, targets}")
TARGET = typer.Option(None, "--target", "-t", help="the wanted output, as numbers with commas or one class number per input")
EXAMPLE = typer.Option(None, "--example", "-e", help="a practice example by number, or several: 3 or 0,1,2")


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
                session: Optional[str] = SESSION):
    """Every neuron's weighted sum and activation for one input. Costs one forward pass."""
    r = _do(WSN.activations, ctx(), _sid(session), **_run_args(input, input_file, target, example))
    emit(r, "\n".join(f"column {c['column']}: " + "  ".join(f"{v:.3f}" for v in c["a"]) for c in r["columns"])
            + f"\nanswer {r['predicted']}" + (f", cost {_f(r['cost'])}" if "cost" in r else "") + "\n" + _passes(r))


@weights_app.command("gradients")
def gradients(input: list[str] = INPUT, input_file: Optional[str] = INPUT_FILE, target: Optional[str] = TARGET, example: Optional[str] = EXAMPLE,
              layer: Optional[int] = typer.Option(None, "--layer", help="with --neuron: also that neuron's whole row of gradients"),
              neuron: Optional[int] = typer.Option(None, "--neuron"), session: Optional[str] = SESSION):
    """The cost and its gradient for every weight and bias, and what each neuron wants. Costs one forward pass."""
    r = _do(WSN.gradients, ctx(), _sid(session), layer=layer, neuron=neuron, **_run_args(input, input_file, target, example))
    lines = [f"cost {_f(r['cost'])} over {r['examples']} example{'s' if r['examples'] != 1 else ''}", "the numbers the cost is most sensitive to:"]
    lines += [f"  {t['tensor']}{t['index']}: value {_f(t['value'])}, gradient {_f(t['gradient'])}" for t in r["top"][:10]]
    lines += [f"column {ly['column']} wants: " + "  ".join(f"{v:+.3g}" for v in ly["wanted_nudge"]) for ly in r["layers"]]
    emit(r, "\n".join(lines + [_passes(r)]))


@weights_app.command("trace")
def trace(input: list[str] = INPUT, input_file: Optional[str] = INPUT_FILE, target: Optional[str] = TARGET, example: Optional[str] = EXAMPLE,
          path: Optional[str] = typer.Option(None, "--path", help="one neuron per column, input first: 0,2,0"), session: Optional[str] = SESSION):
    """The chain rule along one path: for each layer, the three factors and their product. Costs one forward pass."""
    r = _do(WSN.trace, ctx(), _sid(session), path=_ints(path, "--path"), **_run_args(input, input_file, target, example))
    lines = [f"path {', '.join(str(p) for p in r['path'])}, cost {_f(r['cost'])}"]
    for s in r["steps"]:
        lines.append(f"layer {s['layer']}, {s['weight']['tensor']}{s['weight']['index']} = {_f(s['weight']['value'])}: "
                     f"neuron before {_f(s['a_before'])} times steepness {_f(s['da_dz'])} times how far off {_f(s['dcost_da'])} "
                     f"gives gradient {_f(s['gradient'])}" + ("" if s["product_is_exact"] else " (softmax: the product is not exact here)"))
    lines.append(f"this path adds {_f(r['path_product'])} to its first weight's gradient of {_f(r['first_weight_gradient'])}, "
                 f"which has {_n(r['paths_from_first_weight'], 'path')} to the cost")
    emit(r, "\n".join(lines + [_passes(r)]))


# ---- change ----------------------------------------------------------------------------------------------------------------

@weights_app.command("sets")
def sets(target: Optional[str] = typer.Option(None, "--target", help="classes the edits should fix: 9 or 4,9"),
         keep: Optional[str] = typer.Option(None, "--keep", help="classes the edits must leave alone: 0,1,2"),
         target_examples: Optional[str] = typer.Option(None, "--target-examples", help="practice examples as the target instead"),
         keep_examples: Optional[str] = typer.Option(None, "--keep-examples", help="practice examples to leave alone instead"),
         session: Optional[str] = SESSION):
    """Name the target set and the "should not change" set. With nothing given: show the three sets."""
    t = {"classes": _ints(target, "--target")} if target else {"examples": _ints(target_examples, "--target-examples")} if target_examples else None
    k = {"classes": _ints(keep, "--keep")} if keep else {"examples": _ints(keep_examples, "--keep-examples")} if keep_examples else None
    if t is None and k is None:
        r = _do(WSN.session, ctx(), _sid(session))["sets"]
    else:
        r = _do(WSN.set_sets, ctx(), _sid(session), t, k)
    emit(r, "\n".join(f"{name.replace('_', ' ')}: " + (f"{_n(v['of'], 'example')}, {v['says']}" if v["defined"] else "not defined") for name, v in r.items()))


@weights_app.command("edit")
def edit(op: str = typer.Argument(..., help="set, scale, zero, add, add-direction or step"),
         layer: Optional[int] = typer.Option(None, "--layer", help="the neuron's column: 1 is the first after the input"),
         neuron: Optional[int] = typer.Option(None, "--neuron"),
         from_: Optional[int] = typer.Option(None, "--from", help="which incoming weight. Left out: the bias"),
         value: Optional[float] = typer.Option(None, "--value"), factor: Optional[float] = typer.Option(None, "--factor"),
         bias: bool = typer.Option(False, "--bias", help="with scale on a neuron: scale its bias too"),
         tensor: Optional[str] = typer.Option(None, "--tensor", help="a tensor by name, instead of a neuron"),
         index: list[str] = typer.Option([], "--index", help="a position in the tensor: 2,0. Repeat for more"),
         values: Optional[str] = typer.Option(None, "--values", help="one number per position, or one for all"),
         neurons: Optional[str] = typer.Option(None, "--neurons", help="whole rows of the tensor: 0,2"),
         direction: Optional[str] = typer.Option(None, "--direction", help="for add-direction: numbers with commas"),
         scale: Optional[float] = typer.Option(None, "--scale", help="for add-direction: how much of the direction to add"),
         along: Optional[str] = typer.Option(None, "--along", help="for add-direction: inputs (default) or outputs"),
         example: Optional[str] = typer.Option(None, "--example", help="for step: the practice examples to train on"),
         rate: Optional[float] = typer.Option(None, "--rate", help="for step: the learning rate"),
         target: Optional[int] = typer.Option(None, "--target", help="measure this edit against one practice example"),
         why: Optional[str] = typer.Option(None, "--why", help="one sentence: what you expect this edit to do. An agent must give it"),
         session: Optional[str] = SESSION):
    """Change numbers. The edit is stored as a patch and measured on the three sets before and after."""
    req = {"op": op, "layer": layer, "neuron": neuron, "from": from_, "value": value, "factor": factor, "bias": bias or None,
           "tensor": tensor, "indices": [_ints(i, "--index") for i in index] or None, "values": _nums(values, "--values"),
           "neurons": _ints(neurons, "--neurons"), "direction": _nums(direction, "--direction"), "scale": scale, "along": along,
           "example": _ints(example, "--example"), "rate": rate, "target": target, "why": why}
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
    lines = [f"{r['before_is']} against the network now, {r['changed']:,} of {r['total']:,} numbers changed"]
    for v in r["sets"].values():
        lines.append(f"  {v['name']}: " + (f"cost {_f(v['before'])} to {_f(v['after'])}, right {v['right_before']} to {v['right_after']} of {v['of']}, "
                                           f"{v['answers_changed']} answers changed ({v['says']})" if v["defined"] else "not defined"))
    if r["drift"]:
        lines.append(f"how far each column moved from the base, on the {r['drift_on']} set:")
        lines += [f"  column {d['column']}: mean change {_f(d['mean_change'])}, relative {_f(d['relative'])}, similarity {_f(d['similarity'])}" for d in r["drift"]]
    emit(r, "\n".join(lines))


@weights_app.command("save")
def save(note: str = typer.Option("", "--as", "--note", help="a note saying what this version is"), session: Optional[str] = SESSION):
    """Save the network as a new version: the base plus patches, with the record attached. Nothing is overwritten."""
    if S.dry_run:
        return emit({"dry_run": True, "would": "save a new version"}, "would save a new version")
    r = _do(WSN.save, ctx(), _sid(session), note)
    emit(r, f"saved {r['ref']}: {r['base']} plus {_n(r['changed'], 'changed number')} ({r['share'] * 100:.2f}%). Label: {r['label']}.")


@weights_app.command("export")
def export(folder: str, npz: bool = typer.Option(False, "--npz", help="write tensors.npz, not tensors.safetensors"), session: Optional[str] = SESSION):
    """Write the tensors into a folder for code to work on. Only for a session opened with --sandbox, and recorded."""
    r = _do(WSN.export, ctx(), _sid(session), folder, "tensors.npz" if npz else "tensors.safetensors")
    emit(r, f"wrote {', '.join(r['files'])} into {r['folder']}. The session now says a sandbox was used.")


@weights_app.command("import")
def import_(folder: str, name: Optional[str] = typer.Option(None, "--name", help="the model's name here. Left out: the manifest's"),
            project: Optional[str] = typer.Option(None, "--project", help="also put it in this project")):
    """Bring in a model folder (manifest.json and its tensor files) made by any training code, as a new version."""
    if S.dry_run:
        return emit({"dry_run": True, "would": f"import {folder}"}, f"would import {folder}")
    c = ctx()
    m = _do(WST.import_folder, c, folder, name)
    if project:
        _do(WST.assign, c, m["name"], project)
    out = {"model": m["name"], "version": m["version"], "ref": WST.ref_of(m["name"], m["version"]), "parameters": m["parameters"],
           "sizes": m["sizes"], "project": project}
    emit(out, f"imported {out['ref']}: {'-'.join(str(s) for s in m['sizes'])}, {m['parameters']:,} numbers. Open it with `cst weights open {m['name']}`.")


@weights_app.command("assign")
def assign(model: str, project: str):
    """Put a weights model in a project."""
    r = _do(WST.assign, ctx(), WST.split_ref(model)[0], project)
    emit(r, f"{r['model']} is now in the project {r['project']}.")
