"""The two starter networks and the data they are trained on. Everything here is made by code with a seed.

Nothing is downloaded. The digit pictures are SYNTHETIC: ten stroke templates drawn at 28 by 28 with jitter and
noise. They are not handwriting and not MNIST, and the manifest says so.
"""
from __future__ import annotations

import functools

import numpy as np

from . import net as N

SIDE = 28

# One polyline per digit, in a unit square with x to the right and y down.
_STROKES: dict[int, list[list[tuple[float, float]]]] = {
    0: [[(0.50, 0.14), (0.33, 0.24), (0.28, 0.50), (0.33, 0.76), (0.50, 0.86), (0.67, 0.76), (0.72, 0.50), (0.67, 0.24), (0.50, 0.14)]],
    1: [[(0.36, 0.30), (0.52, 0.15), (0.52, 0.86)]],
    2: [[(0.27, 0.30), (0.40, 0.16), (0.60, 0.16), (0.72, 0.30), (0.64, 0.50), (0.27, 0.85), (0.75, 0.85)]],
    3: [[(0.27, 0.18), (0.70, 0.18), (0.48, 0.46), (0.68, 0.54), (0.72, 0.70), (0.56, 0.85), (0.27, 0.80)]],
    4: [[(0.62, 0.86), (0.62, 0.15), (0.24, 0.62), (0.80, 0.62)]],
    5: [[(0.72, 0.15), (0.31, 0.15), (0.29, 0.46), (0.55, 0.42), (0.72, 0.56), (0.70, 0.75), (0.50, 0.86), (0.27, 0.78)]],
    6: [[(0.68, 0.15), (0.42, 0.30), (0.29, 0.60), (0.36, 0.82), (0.55, 0.86), (0.70, 0.70), (0.60, 0.52), (0.33, 0.58)]],
    7: [[(0.25, 0.17), (0.75, 0.17), (0.45, 0.86)]],
    8: [[(0.50, 0.14), (0.31, 0.25), (0.36, 0.42), (0.50, 0.50), (0.68, 0.62), (0.66, 0.80), (0.50, 0.87), (0.33, 0.80),
         (0.32, 0.62), (0.50, 0.50), (0.65, 0.40), (0.68, 0.25), (0.50, 0.14)]],
    9: [[(0.68, 0.40), (0.50, 0.50), (0.32, 0.42), (0.32, 0.25), (0.50, 0.14), (0.68, 0.25), (0.68, 0.40), (0.66, 0.70),
         (0.50, 0.86), (0.32, 0.80)]],
}
_POINTS = 72


@functools.lru_cache(maxsize=None)
def _template(digit: int) -> np.ndarray:
    """The digit's strokes as `_POINTS` points spread evenly along their length."""
    segs = [(np.array(a), np.array(b)) for line in _STROKES[digit] for a, b in zip(line[:-1], line[1:])]
    lengths = np.array([np.linalg.norm(b - a) for a, b in segs])
    at = np.linspace(0.0, lengths.sum(), _POINTS, endpoint=True)
    ends = np.cumsum(lengths)
    pts = []
    for d in at:
        i = min(int(np.searchsorted(ends, d)), len(segs) - 1)
        a, b = segs[i]
        t = (d - (ends[i] - lengths[i])) / lengths[i]
        pts.append(a + np.clip(t, 0.0, 1.0) * (b - a))
    return np.array(pts)


@functools.lru_cache(maxsize=32)
def digits(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """`n` synthetic digit pictures as rows of 784 numbers between 0 and 1, and their digits.

    Each picture is its digit's stroke template, turned a little, stretched, moved, with every point shaken and
    the pen's width varied, then speckled with noise. The same `n` and `seed` give the same pictures."""
    rng = np.random.default_rng(seed)
    labels = np.arange(n) % 10
    rng.shuffle(labels)
    grid = (np.stack(np.meshgrid(np.arange(SIDE), np.arange(SIDE)), axis=-1).reshape(-1, 2) + 0.5) / SIDE   # (784, 2) as x, y
    out = np.empty((n, SIDE * SIDE))
    for i, d in enumerate(labels):
        p = _template(int(d)) - 0.5
        turn = rng.normal(0.0, 0.12)
        c, s = np.cos(turn), np.sin(turn)
        p = p @ np.array([[c, -s], [s, c]]).T
        p = p * rng.normal(1.0, 0.08, size=2) + rng.normal(0.0, 0.03, size=2) + 0.5
        p = p + rng.normal(0.0, 0.008, size=p.shape)
        width = max(0.02, rng.normal(0.034, 0.005))
        d2 = ((grid[:, None, :] - p[None, :, :]) ** 2).sum(axis=-1).min(axis=1)
        img = np.exp(-d2 / (2.0 * width * width))
        img = img + rng.normal(0.0, 0.06, size=img.shape) * (rng.random(img.shape) < 0.25)
        out[i] = np.clip(img, 0.0, 1.0)
    return out, labels.astype(np.int64)


@functools.lru_cache(maxsize=32)
def one_high(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """The tiny task: two numbers between 0 and 1. The answer is 1 when exactly one of them is above one half.

    Points closer than 0.1 to one half are not drawn, so a 2-3-1 network can learn it."""
    rng = np.random.default_rng(seed)
    x = rng.random((n, 2)) * 0.4
    x = np.where(rng.random((n, 2)) < 0.5, x, x + 0.6)
    y = ((x[:, 0] > 0.5) != (x[:, 1] > 0.5)).astype(np.int64)
    return x, y


GENERATORS = {"synthetic-digits": digits, "one-high": one_high}


def generate(spec: dict) -> tuple[np.ndarray, np.ndarray]:
    """The examples a set's spec names: {generator, n, seed}."""
    fn = GENERATORS.get(str(spec.get("generator")))
    if fn is None:
        raise N.WeightsError(f"The data generator {spec.get('generator')!r} is not known.")
    x, y = fn(int(spec["n"]), int(spec["seed"]))
    return x.copy(), y.copy()


def _sets(generator: str, practice: int, pool: int, held: int) -> dict:
    return {
        "practice": {"generator": generator, "n": practice, "seed": 101, "says": "examples anyone may look at. They are never part of a score"},
        "evaluation": {"generator": generator, "n": pool, "seed": 202, "says": "the pool the target and should-not-change sets are drawn from, never shown"},
        "held_out": {"generator": generator, "n": held, "seed": 303, "says": "the held-out set, never shown"},
    }


def tiny() -> tuple[N.Network, dict]:
    """The 2-3-1 network: 13 numbers, trained here on the tiny task. Returns (network, what to put in its manifest)."""
    x, y = one_high(400, 11)
    net = N.init([2, 3, 1], "sigmoid", "cross_entropy", seed=4)
    costs = [c for _, c in N.train(net, x, y, rate=3.0, steps=3000)]
    about = {
        "inputs": {"meaning": "two numbers between 0 and 1", "names": ["x1", "x2"]},
        "outputs": {"meaning": "1 when exactly one input is above one half", "names": ["exactly one is high"], "kind": "classes"},
        "data": {"synthetic": True, "note": "Points drawn by code with a seed. None is closer than 0.1 to one half."},
        "sets": _sets("one-high", 24, 300, 300),
        "says": "Two inputs, three hidden neurons, one output. Small enough to see every number.",
        "training": {"by": "Captain, on first use", "examples": 400, "steps": 3000, "rate": 3.0, "seed": 4, "batch": None,
                     "cost_first": costs[0], "cost_last": costs[-1]},
    }
    return net, about


def digit_net() -> tuple[N.Network, dict]:
    """The 784-16-16-10 network: 13,002 numbers, trained here on synthetic digit pictures."""
    x, y = digits(2000, 12)
    net = N.init([784, 16, 16, 10], "sigmoid", "squared_error", seed=6)
    costs = [c for _, c in N.train(net, x, y, rate=1.5, steps=2500, batch=32, seed=6)]
    about = {
        "inputs": {"meaning": "a 28 by 28 picture, one number per pixel, 0 for empty and 1 for ink, row by row from the top",
                   "shape": [SIDE, SIDE]},
        "outputs": {"meaning": "one number per digit, and the largest is the answer", "names": [str(d) for d in range(10)],
                    "kind": "classes"},
        "data": {"synthetic": True,
                 "note": "Synthetic. The pictures are drawn by code from ten stroke templates with jitter and noise. "
                         "They are not handwriting and not MNIST. A score on them says nothing about real handwriting."},
        "sets": _sets("synthetic-digits", 40, 400, 400),
        "says": "Reads a 28 by 28 picture and names a digit. Trained on synthetic pictures drawn by code.",
        "training": {"by": "Captain, on first use", "examples": 2000, "steps": 2500, "rate": 1.5, "seed": 6, "batch": 32,
                     "cost_first": costs[0], "cost_last": costs[-1]},
    }
    return net, about


STARTERS = {"tiny": tiny, "digits": digit_net}
