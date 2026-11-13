"""A network, a neuron or one weight pinned for an agent (`cst context`): what the pin names, and a short summary.

The ref is the one the Weights page writes: `weights:<model>`, `weights:<model>@layer=2,neuron=6` or
`weights:<model>@layer=2,neuron=6,from=3`. In the ref, `layer` counts from 1 (the first layer after the input)
and `neuron` and `from` count from 0, as in the routes. In the label and the summary everything counts from 1.

The summary reads the model's saved numbers, not a session's: it is for knowing where to look. Reading a network
through `cst weights` is what gets recorded.
"""
from __future__ import annotations

import re

import numpy as np

from . import store as WS
from .net import WeightsError

TOP = 5


def parse(rest: str) -> tuple[str, int | None, int | None, int | None, int | None]:
    """(model, version, layer from 1, neuron from 0, from-neuron from 0) for the text after `weights:`."""
    m = re.fullmatch(r"(?P<model>[^@]+)(?:@(?P<v>v?\d+))?(?:@(?P<at>[a-z]+=\d+(?:,[a-z]+=\d+)*))?", rest.strip())
    if not m:
        raise WeightsError("A pinned network is written weights:<model>, and a neuron weights:<model>@layer=2,neuron=6.")
    at = dict(kv.split("=") for kv in (m.group("at") or "").split(",") if kv)
    unknown = set(at) - {"layer", "neuron", "from"}
    if unknown or ("neuron" in at) != ("layer" in at) or ("from" in at and "neuron" not in at):
        raise WeightsError("A pinned neuron is written weights:<model>@layer=2,neuron=6, with ,from=3 added for one weight.")
    num = lambda k: int(at[k]) if k in at else None  # noqa: E731
    return m.group("model"), (int(m.group("v").lstrip("v")) if m.group("v") else None), num("layer"), num("neuron"), num("from")


def _check(ctx, rest: str):
    model, version, layer, neuron, frm = parse(rest)
    man = WS.manifest(ctx, model, version)
    if layer is not None:
        if not 1 <= layer <= len(man["layers"]):
            raise WeightsError(f"{model} has {len(man['layers'])} layers, so `layer` goes from 1 to {len(man['layers'])}.")
        n_out, n_in = man["sizes"][layer], man["sizes"][layer - 1]
        if not 0 <= neuron < n_out:
            raise WeightsError(f"Layer {layer} of {model} has {n_out} neurons, so `neuron` goes from 0 to {n_out - 1}.")
        if frm is not None and not 0 <= frm < n_in:
            raise WeightsError(f"A neuron in layer {layer} of {model} has {n_in} incoming weights, so `from` goes from 0 to {n_in - 1}.")
    return model, version, layer, neuron, frm, man


def resolve(ctx, rest: str) -> dict:
    model, version, layer, neuron, frm, man = _check(ctx, rest)
    ref = f"weights:{model}" + (f"@v{version}" if version else "")
    if layer is None:
        return {"ref": ref, "kind": "weights", "label": f"{model}: the whole network"}
    ref += f"@layer={layer},neuron={neuron}" + (f",from={frm}" if frm is not None else "")
    what = f"layer {layer}, neuron {neuron + 1}" + (f", weight from neuron {frm + 1}" if frm is not None else "")
    return {"ref": ref, "kind": "weights", "label": f"{model}: {what}"}


def expand(ctx, rest: str) -> dict:
    """What an agent needs to know about the pin before it opens a session, and the commands that read more."""
    model, version, layer, neuron, frm, _ = _check(ctx, rest)
    man, tensors = WS.load(ctx, model, version)
    data = man.get("data") or {}
    out = {"model": WS.ref_of(model, man["version"]), "sizes": man["sizes"], "numbers": man.get("parameters"),
           "counting": "Layers and neurons here count from 1. The cst weights flags count neurons from 0."}
    if data.get("synthetic"):
        out["data"] = data.get("note") or "synthetic"
    opens = f"cst weights open {model}" + (f"@v{version}" if version else "") + " --agent --name <you>"
    if layer is None:
        out["layers"] = [f"layer {k + 1}: {ly['inputs']} in, {ly['outputs']} out, {ly['activation']}" for k, ly in enumerate(man["layers"])]
        out["read_more"] = [opens, "cst weights layers", "cst weights stats --neurons", "cst weights heatmap --layer 0"]
        return out
    ly = man["layers"][layer - 1]
    w = np.asarray(tensors[ly["weight"]], dtype=float)[neuron]
    bias = float(tensors[ly["bias"]][neuron])
    order = np.argsort(np.abs(w))[::-1][:TOP]
    out["layer"], out["neuron"], out["of"] = layer, neuron + 1, int(len(tensors[ly["bias"]]))
    out["activation"], out["bias"], out["incoming_weights"] = ly["activation"], round(bias, 6), int(w.size)
    out["largest_incoming"] = [{"from_neuron": int(i) + 1, "weight": round(float(w[i]), 6)} for i in order]
    if layer < len(man["layers"]):
        nxt = np.asarray(tensors[man["layers"][layer]["weight"]], dtype=float)[:, neuron]
        out["largest_outgoing"] = [{"to_neuron": int(j) + 1, "weight": round(float(nxt[j]), 6)} for j in np.argsort(np.abs(nxt))[::-1][:TOP]]
    if frm is not None:
        out["weight"] = {"from_neuron": frm + 1, "value": round(float(w[frm]), 6)}
    out["read_more"] = [opens, f"cst weights heatmap --layer {layer - 1} --neuron {neuron}",
                        f"cst weights read {ly['weight']} --rows {neuron}", f"cst weights read {ly['bias']} --rows {neuron}",
                        "cst weights stats --neurons"]
    return out
