"""A dense network in numpy: the forward pass, the cost, and backpropagation with the quantities a lesson draws.

A layer is `a_out = act(W @ a_in + b)`. `W` has one row per neuron of the layer and one column per neuron of the
layer before it, so row j is "the weights coming into neuron j". Everything is computed in float64 and is the same
on every run with the same seed.

Names used throughout, the same ones a screen draws (see docs/weights.md):
    z            the weighted sum of a neuron, before its activation function
    a            the activation, after it
    dcost_da     how much the cost changes per unit change of that activation
    wanted_nudge the negative of it: positive means the cost falls if this neuron is more active
    dcost_dz     the same for the weighted sum
A weight's gradient is `a_before * dcost_dz`, and for every activation but softmax `dcost_dz = da_dz * dcost_da`,
which are the three chain-rule factors.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ACTIVATIONS = ("sigmoid", "relu", "tanh", "linear", "softmax")
COSTS = ("squared_error", "cross_entropy")
_EPS = 1e-12


class WeightsError(ValueError):
    """Something a person asked for that the network code will not do. The text is a plain sentence."""


@dataclass
class Layer:
    name: str
    weight: np.ndarray   # (outputs, inputs)
    bias: np.ndarray     # (outputs,)
    activation: str

    @property
    def inputs(self) -> int:
        return int(self.weight.shape[1])

    @property
    def outputs(self) -> int:
        return int(self.weight.shape[0])


@dataclass
class Network:
    layers: list[Layer]
    cost: str = "squared_error"

    @property
    def sizes(self) -> list[int]:
        return [self.layers[0].inputs] + [ly.outputs for ly in self.layers]

    @property
    def parameters(self) -> int:
        return int(sum(ly.weight.size + ly.bias.size for ly in self.layers))

    def copy(self) -> "Network":
        return Network([Layer(ly.name, ly.weight.copy(), ly.bias.copy(), ly.activation) for ly in self.layers], self.cost)

    def tensors(self) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for ly in self.layers:
            out[f"{ly.name}.weight"] = ly.weight
            out[f"{ly.name}.bias"] = ly.bias
        return out


def check(net: Network) -> None:
    """Refuse a network this code cannot run, in a sentence."""
    if not net.layers:
        raise WeightsError("A network needs at least one layer.")
    if net.cost not in COSTS:
        raise WeightsError(f"The cost {net.cost!r} is not known. Use one of: {', '.join(COSTS)}.")
    before = net.layers[0].inputs
    for i, ly in enumerate(net.layers):
        if ly.activation not in ACTIVATIONS:
            raise WeightsError(f"Layer {ly.name} has the activation {ly.activation!r}. Use one of: {', '.join(ACTIVATIONS)}.")
        if ly.weight.ndim != 2 or ly.bias.ndim != 1 or ly.weight.shape[0] != ly.bias.shape[0]:
            raise WeightsError(f"Layer {ly.name} needs a weight table of outputs by inputs and one bias per output. "
                               f"It has a weight of shape {list(ly.weight.shape)} and a bias of shape {list(ly.bias.shape)}.")
        if ly.inputs != before:
            raise WeightsError(f"Layer {ly.name} takes {ly.inputs} inputs but the layer before it gives {before}.")
        if ly.activation == "softmax" and i != len(net.layers) - 1:
            raise WeightsError("Softmax is only supported on the last layer.")
        before = ly.outputs
    if net.cost == "cross_entropy" and net.layers[-1].activation not in ("sigmoid", "softmax"):
        raise WeightsError("Cross-entropy needs a last layer that gives probabilities: sigmoid or softmax.")


def init(sizes: list[int], activations: list[str] | str = "sigmoid", cost: str = "squared_error", seed: int = 0,
         names: list[str] | None = None) -> Network:
    """A fresh network with small random weights and zero biases. The same seed gives the same numbers."""
    if len(sizes) < 2:
        raise WeightsError("Give at least an input size and an output size.")
    acts = [activations] * (len(sizes) - 1) if isinstance(activations, str) else list(activations)
    if len(acts) != len(sizes) - 1:
        raise WeightsError(f"{len(sizes) - 1} layers need {len(sizes) - 1} activations, and {len(acts)} were given.")
    rng = np.random.default_rng(seed)
    layers = []
    for i, (n_in, n_out) in enumerate(zip(sizes[:-1], sizes[1:])):
        w = rng.standard_normal((n_out, n_in)) / np.sqrt(n_in)
        layers.append(Layer(names[i] if names else f"layer{i + 1}", w, np.zeros(n_out), acts[i]))
    net = Network(layers, cost)
    check(net)
    return net


# ---- the forward pass ------------------------------------------------------------------------------------------------

def _act(name: str, z: np.ndarray) -> np.ndarray:
    if name == "sigmoid":
        return 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))
    if name == "relu":
        return np.maximum(z, 0.0)
    if name == "tanh":
        return np.tanh(z)
    if name == "softmax":
        e = np.exp(z - z.max(axis=-1, keepdims=True))
        return e / e.sum(axis=-1, keepdims=True)
    return z


def _slope(name: str, z: np.ndarray, a: np.ndarray) -> np.ndarray:
    """da/dz for each neuron by itself. For softmax this is only each output's effect on itself."""
    if name in ("sigmoid", "softmax"):
        return a * (1.0 - a)
    if name == "relu":
        return (z > 0).astype(float)
    if name == "tanh":
        return 1.0 - a * a
    return np.ones_like(z)


def _batch(net: Network, x) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        x = x[None, :]
    if x.ndim > 2:
        x = x.reshape(x.shape[0], -1)
    if x.shape[1] != net.layers[0].inputs:
        raise WeightsError(f"This network takes {net.layers[0].inputs} numbers per input and was given {x.shape[1]}.")
    return x


def _targets(net: Network, y, n: int) -> np.ndarray:
    """Targets as one row per example. A list of class numbers becomes one-hot rows."""
    outs = net.layers[-1].outputs
    y = np.asarray(y)
    if y.ndim <= 1 and outs > 1 and y.size == n and np.issubdtype(y.dtype, np.integer):
        if y.size and (y.min() < 0 or y.max() >= outs):
            raise WeightsError(f"A class number must be between 0 and {outs - 1}.")
        return np.eye(outs)[y.reshape(-1)]
    y = np.asarray(y, dtype=float)
    if y.ndim == 0:
        y = y.reshape(1, 1)
    elif y.ndim == 1:
        y = y[None, :] if (n == 1 and y.size == outs) else y[:, None]
    if y.shape != (n, outs):
        raise WeightsError(f"The wanted outputs should be {n} rows of {outs} numbers. They are {list(y.shape)}.")
    return y


def forward(net: Network, x) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Run the network. Returns (zs, acts): `acts[0]` is the input and `acts[i + 1]`, `zs[i]` belong to layer i."""
    a = _batch(net, x)
    zs, acts = [], [a]
    for ly in net.layers:
        z = a @ np.asarray(ly.weight, dtype=float).T + np.asarray(ly.bias, dtype=float)
        a = _act(ly.activation, z)
        zs.append(z)
        acts.append(a)
    return zs, acts


def predict(net: Network, x) -> np.ndarray:
    return forward(net, x)[1][-1]


# ---- the cost ------------------------------------------------------------------------------------------------------

def cost_each(net: Network, out: np.ndarray, y: np.ndarray) -> np.ndarray:
    """The cost of each example by itself. Squared error is the sum over outputs of (output - wanted) squared."""
    if net.cost == "squared_error":
        return ((out - y) ** 2).sum(axis=1)
    p = np.clip(out, _EPS, 1.0 - _EPS)
    if net.layers[-1].activation == "softmax":
        return -(y * np.log(p)).sum(axis=1)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)).sum(axis=1)


def cost(net: Network, x, y) -> float:
    """The average cost over the examples given."""
    xb = _batch(net, x)
    return float(cost_each(net, predict(net, xb), _targets(net, y, xb.shape[0])).mean())


def score(net: Network, x, y) -> float | None:
    """The share of examples answered right, for a network that picks a class. One output is read as yes above 0.5."""
    xb = _batch(net, x)
    yb = _targets(net, y, xb.shape[0])
    out = predict(net, xb)
    if out.shape[1] == 1:
        return float(((out[:, 0] > 0.5) == (yb[:, 0] > 0.5)).mean())
    return float((out.argmax(axis=1) == yb.argmax(axis=1)).mean())


# ---- backpropagation ---------------------------------------------------------------------------------------------------

def _backward(net: Network, zs, acts, y):
    """Per example: dcost_dz and dcost_da for every layer, last layer first worked out, returned in layer order."""
    last = net.layers[-1]
    out = acts[-1]
    if net.cost == "squared_error":
        da = 2.0 * (out - y)
        if last.activation == "softmax":
            s = out * da
            dz = s - out * s.sum(axis=1, keepdims=True)
        else:
            dz = da * _slope(last.activation, zs[-1], out)
    else:
        p = np.clip(out, _EPS, 1.0 - _EPS)
        da = -y / p if last.activation == "softmax" else (p - y) / (p * (1.0 - p))
        dz = out - y
    dzs, das = [dz], [da]
    for i in range(len(net.layers) - 1, 0, -1):
        da = dzs[0] @ np.asarray(net.layers[i].weight, dtype=float)
        dz = da * _slope(net.layers[i - 1].activation, zs[i - 1], acts[i])
        dzs.insert(0, dz)
        das.insert(0, da)
    d_in = dzs[0] @ np.asarray(net.layers[0].weight, dtype=float)
    return dzs, das, d_in


def backprop(net: Network, x, y) -> dict:
    """The cost and its gradient for every weight and bias, averaged over the examples given, plus what each
    neuron wants.

    Returns {cost, examples, layers: [{name, weight, bias, dcost_da, wanted_nudge, dcost_dz, da_dz}], input_dcost_da}.
    `weight` and `bias` are gradients shaped like the layer's own tensors. The per-neuron lists are averages over the
    examples, so for one example they are exact."""
    xb = _batch(net, x)
    n = xb.shape[0]
    yb = _targets(net, y, n)
    zs, acts = forward(net, xb)
    dzs, das, d_in = _backward(net, zs, acts, yb)
    layers = []
    for i, ly in enumerate(net.layers):
        layers.append({
            "name": ly.name,
            "weight": dzs[i].T @ acts[i] / n,
            "bias": dzs[i].mean(axis=0),
            "dcost_da": das[i].mean(axis=0),
            "wanted_nudge": -das[i].mean(axis=0),
            "dcost_dz": dzs[i].mean(axis=0),
            "da_dz": _slope(ly.activation, zs[i], acts[i + 1]).mean(axis=0),
        })
    return {"cost": float(cost_each(net, acts[-1], yb).mean()), "examples": n, "layers": layers,
            "input_dcost_da": d_in.mean(axis=0)}


def trace(net: Network, x, y, path: list[int] | None = None) -> dict:
    """The chain rule along one path through the network, for one example.

    `path` names one neuron per column, input first: [input, neuron in layer 1, ..., output]. Left out, it follows
    the most active input and, in each layer, the neuron whose weight on the path has the largest gradient.

    For each layer on the path this gives the weight that joins the two neurons and its three factors:
        a_before   how active the neuron before is            (dz/dw)
        da_dz      how steep the activation function is here  (da/dz)
        dcost_da   how far off this neuron is, as the cost sees it (dcost/da)
    and `gradient`, the cost's slope for that weight. For every activation but softmax the gradient is the product
    of the three. Softmax ties its outputs together, so there `product_is_exact` is false and the gradient is
    `a_before * dcost_dz`.

    `path_product` is what this one path adds to the gradient of its first weight, and `first_weight_gradient` is
    the sum over every path from that weight to the cost."""
    xb = _batch(net, x)
    if xb.shape[0] != 1:
        raise WeightsError("A trace follows one example. Give one input.")
    yb = _targets(net, y, 1)
    zs, acts = forward(net, xb)
    dzs, das, _ = _backward(net, zs, acts, yb)
    n_layers = len(net.layers)
    if path is None:
        path = [int(np.abs(acts[0][0]).argmax())]
        for i in range(n_layers):
            path.append(int(np.abs(acts[i][0, path[-1]] * dzs[i][0]).argmax()))
    path = [int(p) for p in path]
    if len(path) != n_layers + 1:
        raise WeightsError(f"A path names one neuron per column: {n_layers + 1} numbers for this network, and {len(path)} were given.")
    for col, (p, size) in enumerate(zip(path, net.sizes)):
        if not 0 <= p < size:
            raise WeightsError(f"Column {col} has {size} neurons, numbered from 0. There is no neuron {p}.")
    steps = []
    for i, ly in enumerate(net.layers):
        j, k = path[i + 1], path[i]
        slope = float(_slope(ly.activation, zs[i], acts[i + 1])[0, j])
        a_before, dcost_da, dcost_dz = float(acts[i][0, k]), float(das[i][0, j]), float(dzs[i][0, j])
        exact = ly.activation != "softmax"
        steps.append({
            "layer": i, "name": ly.name, "neuron": j, "from_neuron": k,
            "weight": {"tensor": f"{ly.name}.weight", "index": [j, k], "value": float(ly.weight[j, k])},
            "bias": {"tensor": f"{ly.name}.bias", "index": [j], "value": float(ly.bias[j]), "gradient": dcost_dz},
            "z": float(zs[i][0, j]), "a": float(acts[i + 1][0, j]),
            "a_before": a_before, "da_dz": slope, "dcost_da": dcost_da, "dcost_dz": dcost_dz,
            "gradient": a_before * dcost_dz, "product": a_before * slope * dcost_da, "product_is_exact": exact,
        })
    # What this single path carries back from the cost to its first weight.
    carry = float(das[-1][0, path[-1]])
    exact = True
    for i in range(n_layers - 1, -1, -1):
        ly = net.layers[i]
        exact = exact and ly.activation != "softmax"
        carry *= float(_slope(ly.activation, zs[i], acts[i + 1])[0, path[i + 1]])
        if i > 0:
            carry *= float(ly.weight[path[i + 1], path[i]])
    paths = 1
    for s in net.sizes[2:]:
        paths *= s
    return {"path": path, "cost": float(cost_each(net, acts[-1], yb)[0]), "steps": steps,
            "path_product": float(acts[0][0, path[0]]) * carry, "path_product_is_exact": exact,
            "first_weight_gradient": steps[0]["gradient"], "paths_from_first_weight": int(paths),
            "output": [float(v) for v in acts[-1][0]], "wanted": [float(v) for v in yb[0]]}


# ---- training ---------------------------------------------------------------------------------------------------------

def step(net: Network, x, y, rate: float = 0.5) -> dict:
    """One gradient step, in place: every weight and bias moves by `rate` times its negative gradient."""
    g = backprop(net, x, y)
    for ly, gl in zip(net.layers, g["layers"]):
        ly.weight = np.asarray(ly.weight, dtype=float) - rate * gl["weight"]
        ly.bias = np.asarray(ly.bias, dtype=float) - rate * gl["bias"]
    return {"cost_before": g["cost"], "cost_after": cost(net, x, y), "rate": rate, "examples": g["examples"]}


def train(net: Network, x, y, rate: float = 0.5, steps: int = 100, batch: int | None = None, seed: int = 0):
    """A small training loop, in place. Yields (step number, cost of that step's batch before the step).

    With `batch`, each step uses that many examples picked with the seed, so the run repeats exactly."""
    xb = _batch(net, x)
    yb = _targets(net, y, xb.shape[0])
    rng = np.random.default_rng(seed)
    n = xb.shape[0]
    for s in range(1, int(steps) + 1):
        if batch and batch < n:
            pick = rng.choice(n, size=int(batch), replace=False)
            r = step(net, xb[pick], yb[pick], rate)
        else:
            r = step(net, xb, yb, rate)
        yield s, r["cost_before"]


def numerical_gradient(net: Network, x, y, h: float = 1e-6) -> list[dict]:
    """The gradient by nudging each number and measuring the cost. Slow: for tests and for checking small networks."""
    out = []
    for ly in net.layers:
        g = {}
        for key in ("weight", "bias"):
            t = np.asarray(getattr(ly, key), dtype=float).copy()
            setattr(ly, key, t)
            grad = np.zeros_like(t)
            it = np.nditer(t, flags=["multi_index"])
            for _ in it:
                i = it.multi_index
                keep = t[i]
                t[i] = keep + h
                up = cost(net, x, y)
                t[i] = keep - h
                down = cost(net, x, y)
                t[i] = keep
                grad[i] = (up - down) / (2 * h)
            g[key] = grad
        out.append(g)
    return out
