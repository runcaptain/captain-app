"""Shopper simulators. A simulator takes a population config, a query split and one search arm, and
returns one event log per shopper. Metrics (CTR, conversion, revenue per visitor, latency) are
computed from the stored logs, never from seeds, because simulated logs don't always regenerate from the
same seeds.

Built in: `cascade-v1`. A cascade click model:
- traffic over queries is Zipf-shaped, each shopper has a device, patience and budget;
- common random numbers: shopper i makes the same draws in every arm, so arms differ only by ranking;
- position bias: the shopper examines rank k with probability patience^k (cascade model);
- click probability rises with the relevance the click model assigns to the product;
- purchase probability rises with relevance and falls when the price is over budget;
- a zero-result page sends the shopper to one reformulation (drop the last word), then they leave.

The click model reads relevance from `heuristic` text match by default, which is independent of the
judgments that score nDCG. A population can instead name a judgment dataset; then the backtest marks
itself "not independent".

Third-party simulators register under the `captain_search.simulators` entry point group.
"""
from __future__ import annotations

import json
import random
from importlib.metadata import entry_points
from typing import Callable

SIMULATORS = {"cascade-v1": "1.0.0"}

CLICK = {0: 0.02, 1: 0.10, 2: 0.35, 3: 0.60}
BUY = {0: 0.0, 1: 0.02, 2: 0.10, 3: 0.25}
DEVICES = [("mobile", 0.62, 0.70), ("desktop", 0.33, 0.80), ("tablet", 0.05, 0.75)]  # name, share, patience


def _zipf_pick(rng: random.Random, n: int, s: float) -> int:
    # inverse CDF over precomputed weights is done by the caller; this keeps the draw count fixed
    return int(n * (rng.random() ** s)) % max(1, n)


def personas(size: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(size):
        r, acc = rng.random(), 0.0
        dev, pat = DEVICES[-1][0], DEVICES[-1][2]
        for name, share, patience in DEVICES:
            acc += share
            if r < acc:
                dev, pat = name, patience
                break
        out.append({"shopper": i, "device": dev, "patience": round(pat * (0.85 + 0.3 * rng.random()), 3),
                    "budget": round(20 + rng.lognormvariate(3.5, 0.8), 2), "seed": rng.randrange(2**31)})
    return out


def run_cascade(population: list[dict], queries: list[dict], search: Callable[[str], tuple[list[str], float]],
                relevance: Callable[[str, str], int], products: dict[str, dict], zipf_s: float = 2.0,
                progress=None, weights: dict[str, float] | None = None, exam: list[float] | None = None) -> list[dict]:
    """Returns one log dict per shopper: {shopper, device, events:[...], clicked, bought, revenue, ms:[...]}

    weights: query_id -> traffic weight (historic mode: query frequencies from the logs). Without it,
    traffic is power-law over the sorted query ids.
    exam: probability a shopper looks at rank k (historic mode: calibrated from the logs' clicks by rank).
    Without it, the shopper's patience^k is used."""
    ordered = sorted(queries, key=lambda q: q["query_id"])
    cum = None
    if weights:
        import bisect
        import itertools

        w = [max(0.0, float(weights.get(q["query_id"], 0.0))) for q in ordered]
        cum = list(itertools.accumulate(w))
    logs = []
    for n, p in enumerate(population):
        rng = random.Random(p["seed"])
        if cum and cum[-1] > 0:
            q = ordered[min(len(ordered) - 1, bisect.bisect_left(cum, rng.random() * cum[-1]))]
        else:
            q = ordered[_zipf_pick(rng, len(ordered), zipf_s)]
        draws = [rng.random() for _ in range(60)]  # fixed count: identical across arms
        text = q["query"]
        ev, ms = [], []
        ids, t = search(text)
        ms.append(round(t, 3))
        ev.append({"t": "search", "query_id": q["query_id"], "query": text, "results": len(ids)})
        if not ids:
            words = text.split()
            if len(words) > 1:
                text = " ".join(words[:-1])
                ids, t = search(text)
                ms.append(round(t, 3))
                ev.append({"t": "reformulate", "query": text, "results": len(ids)})
        clicked = bought = False
        revenue = 0.0
        for k, pid in enumerate(ids[:20]):
            look = (exam[k] if k < len(exam) else exam[-1] * 0.8 ** (k - len(exam) + 1)) if exam else p["patience"] ** k
            if draws[2 * k] > look:
                break  # stopped examining
            g = relevance(q["query_id"], pid)
            if draws[2 * k + 1] < CLICK[g]:
                clicked = True
                ev.append({"t": "click", "rank": k + 1, "product_id": pid, "grade": g})
                price = _price(products.get(pid, {}))
                buy_p = BUY[g] * (0.3 if price is not None and price > p["budget"] else 1.0)
                if draws[40 + min(k, 19)] < buy_p:
                    bought = True
                    revenue = price or 0.0
                    ev.append({"t": "purchase", "product_id": pid, "price": revenue})
                    break
        logs.append({"shopper": p["shopper"], "device": p["device"], "query_id": q["query_id"], "events": ev,
                     "clicked": clicked, "bought": bought, "revenue": round(revenue, 2), "ms": ms})
        if progress and n % 200 == 0:
            progress(n / max(1, len(population)))
    return logs


def _price(p):
    try:
        return float(p.get("price"))
    except (TypeError, ValueError):
        return None


def summarize(logs: list[dict]) -> dict[str, float]:
    from ..eval.metrics import pct

    n = max(1, len(logs))
    ms = [m for l in logs for m in l["ms"]]
    return {"shoppers": len(logs), "ctr": round(sum(l["clicked"] for l in logs) / n, 4),
            "conversion": round(sum(l["bought"] for l in logs) / n, 4),
            "rpv": round(sum(l["revenue"] for l in logs) / n, 4),
            "zero_result_rate": round(sum(1 for l in logs if l["events"][0]["results"] == 0) / n, 4),
            "p99_ms": round(pct(ms, 99) or 0.0, 3)}


def per_shopper(logs: list[dict], metric: str) -> dict[str, float]:
    key = {"ctr": "clicked", "conversion": "bought", "rpv": "revenue"}[metric]
    return {str(l["shopper"]): float(l[key]) for l in logs}


def logs_to_bytes(logs: list[dict]) -> bytes:
    return ("\n".join(json.dumps(l, separators=(",", ":"), sort_keys=True) for l in logs) + "\n").encode()


def logs_from_bytes(b: bytes) -> list[dict]:
    return [json.loads(x) for x in b.decode().splitlines() if x]


def get_simulator(name: str, workspace: str | None = None):
    from .. import extensions as X

    e = X.load(workspace).get("simulator", name)
    if e is not None and e.source != "builtin":
        return e.obj
    if name == "cascade-v1":
        return run_cascade
    for ep in entry_points(group="captain_search.simulators"):
        if ep.name == name:
            return ep.load()
    raise ValueError(f"unknown simulator {name!r}. Built in: {', '.join(SIMULATORS)}")
