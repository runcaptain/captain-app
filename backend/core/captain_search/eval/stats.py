"""Paired comparisons: a sign-flip permutation test, and a paired bootstrap that resamples units and keeps
pairs together."""
from __future__ import annotations

import numpy as np


def sign_flip_p(d: np.ndarray, iters: int = 20000, seed: int = 0) -> float:
    if len(d) == 0:
        return 1.0
    obs = abs(d.mean())
    rng = np.random.default_rng(seed)
    signs = rng.choice([-1.0, 1.0], size=(iters, len(d)))
    null = np.abs((signs * d).mean(axis=1))
    return float((np.sum(null >= obs - 1e-12) + 1) / (iters + 1))


def bootstrap_ci(d: np.ndarray, iters: int = 5000, seed: int = 0, alpha: float = 0.05) -> tuple[float, float]:
    if len(d) == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(iters, len(d)))
    means = d[idx].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def compare(a: dict[str, float], b: dict[str, float], metric: str, mde: float, *, higher_is_better: bool = True,
            arm_a: str = "a", arm_b: str = "b") -> dict:
    """a, b: unit id -> value (per query or per shopper). Delta is b - a."""
    ids = sorted(set(a) & set(b))
    d = np.array([b[i] - a[i] for i in ids], dtype=float)
    delta = float(d.mean()) if len(d) else 0.0
    lo, hi = bootstrap_ci(d)
    p = sign_flip_p(d)
    good = delta if higher_is_better else -delta
    if lo <= 0 <= hi or abs(delta) < mde:
        verdict = "noise"
    else:
        verdict = "win" if good > 0 else "loss"
    return {"metric": metric, "a": arm_a, "b": arm_b, "n": len(ids), "delta": round(delta, 5),
            "ci_low": round(lo, 5), "ci_high": round(hi, 5), "p": round(p, 4), "mde": mde, "verdict": verdict,
            "wins": int((d > 1e-9).sum()), "losses": int((d < -1e-9).sum())}


def quadratic_kappa(x: list[int], y: list[int], k: int = 4) -> float:
    """Quadratic-weighted Cohen's kappa between two graders on the same items (grades 0..k-1)."""
    if not x:
        return 0.0
    O = np.zeros((k, k))
    for i, j in zip(x, y):
        O[i, j] += 1
    W = np.array([[(i - j) ** 2 / (k - 1) ** 2 for j in range(k)] for i in range(k)])
    E = np.outer(O.sum(1), O.sum(0)) / O.sum()
    den = (W * E).sum()
    return float(1 - (W * O).sum() / den) if den else 1.0
