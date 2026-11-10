"""Synthetic shopper events for any catalog: `cst sim events`.

A project with a catalog and no traffic yet has nothing to train a ranker on. This writes a stand-in: search
sessions over the catalog's own products, as one row per impression, the shape `historic.py` replays and the
trainer contract hands over as an `events` input. Everything it makes is SIMULATED. No shopper did any of this,
so a model trained on it has learned this generator, and a result judged on it says nothing about real traffic.

It reuses what the workspace already has rather than a second simulator:
- queries come from `eval.queries.generate` (the catalog's own words, by failure class);
- the page each search shows is the top results of a local BM25 ranker (`engines.LocalBM25`);
- shoppers are `sim.personas` (device and patience), and clicks and orders use the cascade simulator's own
  `CLICK` and `BUY` tables over `eval.judge.heuristic_grade`.

What it adds is the log itself: sessions with one or more searches, traffic over queries that follows Zipf's law,
impressions down to how far the shopper scrolled, add-to-cart before purchase, a budget per shopper scaled to the
catalog's prices, timestamps spread over a date range, and anonymous user and session ids.

Relevance reads more than the ranker does: attribute columns (color, gender, size, tags) count as product text for
the click model and not for BM25, so the clicks hold a signal the baseline ranking doesn't already encode.

The same catalog rows and the same parameters give the same rows, byte for byte: every draw comes from
`random.Random` seeded from `seed`, and ids are hashes of the seed.
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import itertools
import math
import random

from ..eval import queries as Q
from ..eval.judge import heuristic_grade
from . import BUY, CLICK, personas

GENERATOR = "captain_search.sim.events"
VERSION = "1.0.0"

# Zero-result queries are left out: an impression log has no row for a search that showed nothing.
CLASSES = ["head", "brand", "multi-attr", "numeric", "model-code", "typo"]
RANKER = {"fields": {"title": 3.0, "brand": 1.5, "category": 1.0, "description": 0.5}, "k1": 1.2, "b": 0.75}
ATTRS = ("color", "colour", "gender", "size", "sizes", "material", "product_type", "tags")
PAGE = 10          # results on a page
FOLD = 2           # results seen without scrolling
ZIPF_S = 1.1       # traffic over queries: weight 1 / rank^s
ATTENTION = 0.9    # a result one place lower gets this much of the attention
NOTICE = 0.5       # share of looked-at results a shopper considers at all, on top of CLICK[grade]
SCROLL = 0.9       # chance of scrolling one result further, as a multiple of the shopper's patience
CART = 1.2         # add-to-cart chance after a click, as a multiple of BUY[grade]
CHECKOUT = 0.35    # share of carted items that are bought
OVER_BUDGET = 0.3  # the cascade simulator's own factor for a price over the shopper's budget
MORE = 0.15        # chance of another search in the same session
HOURS = [1, 1, 1, 1, 1, 2, 3, 5, 6, 7, 7, 8, 9, 8, 7, 7, 7, 8, 9, 10, 10, 8, 5, 2]  # traffic by hour of day

COLUMNS = ["session_id", "user_id", "ts", "device", "query", "query_class", "product_id", "position", "clicked",
           "added_to_cart", "purchased", "price", "simulated"]
LIMITS = {"sessions": (1, 200_000), "days": (1, 365)}


def _hex(*parts, n: int = 12) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:n]


def _price(p: dict) -> float | None:
    try:
        v = float(p.get("price"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 and math.isfinite(v) else None


def _text(v) -> str:
    if isinstance(v, (list, tuple, set)):
        return " ".join(str(x) for x in v)
    if isinstance(v, dict):
        return " ".join(str(x) for x in v.values())
    return str(v or "")


def check(sessions: int, days: int) -> None:
    for name, v in (("sessions", sessions), ("days", days)):
        lo, hi = LIMITS[name]
        if not lo <= int(v) <= hi:
            raise ValueError(f"{name} is between {lo:,} and {hi:,}, not {v}")


def generate(products: list[dict], sessions: int, days: int, seed: int, end: str, n_queries: int | None = None,
             progress=None) -> tuple[list[dict], dict]:
    """(rows, stats). One row per impression a shopper could see, sorted by time. `end` is the last day
    (YYYY-MM-DD); the range is the `days` days ending on it."""
    from ..engines import LocalBM25

    check(sessions, days)
    last = dt.date.fromisoformat(end)
    prods = [{**p, "id": str(p["id"])} for p in products if p.get("title")]
    if not prods:
        raise ValueError("the catalog has no rows with a title column, so there is nothing to search for")
    pmap = {p["id"]: p for p in prods}
    # What the click model reads: the ranker's fields plus the attribute columns.
    seen_as = {p["id"]: {**p, "description": " ".join(filter(None, [_text(p.get("description"))] + [_text(p.get(a)) for a in ATTRS]))}
               for p in prods}
    prices = sorted(x for x in (_price(p) for p in prods) if x is not None)
    median = prices[len(prices) // 2] if prices else None
    classes = [c for c in CLASSES if c != "numeric" or prices]
    n_queries = n_queries or max(40, min(1500, len(prods)))
    qs = Q.generate(prods, classes, n_queries, seed)
    eng = LocalBM25(prods, RANKER)  # the built-in one, never a workspace override: the rows must reproduce
    pages, grade = {}, {}

    def page(q: str) -> list[str]:
        if q not in pages:
            pages[q] = [str(i) for i in eng.search(q, PAGE)]
        return pages[q]

    # Queries nobody could have a page for are dropped before traffic is spread, so the popularity stays Zipf.
    qs = [q for q in qs if page(q["query"])]
    if not qs:
        raise ValueError("no generated query finds a product in this catalog")
    cum = list(itertools.accumulate(1.0 / (r + 1) ** ZIPF_S for r in range(len(qs))))
    hours = list(itertools.accumulate(HOURS))
    dayw = [1.2 if (last - dt.timedelta(days=days - 1 - d)).weekday() >= 5 else 1.0 for d in range(days)]
    dayc = list(itertools.accumulate(dayw))
    n_users = max(1, int(sessions * 0.7))
    rows, clicks, carts, orders, searches = [], 0, 0, 0, 0
    for i, person in enumerate(personas(sessions, seed)):
        rng = random.Random(person["seed"])
        sid = "s-" + _hex(GENERATOR, seed, "session", i)
        uid = "u-" + _hex(GENERATOR, seed, "user", int(n_users * rng.random() ** 1.4), n=10)
        budget = median * math.exp(rng.gauss(0.1, 0.6)) if median else None
        d = bisect.bisect_left(dayc, rng.random() * dayc[-1])
        h = bisect.bisect_left(hours, rng.random() * hours[-1])
        t = dt.datetime.combine(last - dt.timedelta(days=days - 1 - d), dt.time(h, rng.randrange(60), rng.randrange(60)))
        for _ in range(4):
            q = qs[min(len(qs) - 1, bisect.bisect_left(cum, rng.random() * cum[-1]))]
            ids = page(q["query"])
            depth = FOLD
            while depth < len(ids) and rng.random() < person["patience"] * SCROLL:
                depth += 1
            ts = t.isoformat(timespec="seconds") + "Z"
            searches += 1
            for k, pid in enumerate(ids[:depth]):
                key = (q["query"], pid)
                if key not in grade:
                    grade[key] = heuristic_grade(q["query"], seen_as[pid])
                g = grade[key]
                price = _price(pmap[pid])
                over = budget is not None and price is not None and price > budget
                clicked = rng.random() < ATTENTION ** k * NOTICE * CLICK[g] * (0.6 if over else 1.0)
                # Fixed draw count per impression, so one shopper's later draws don't depend on an earlier click.
                a, b = rng.random(), rng.random()
                value = 1.0 if budget is None or price is None else max(0.5, min(1.5, (budget / price) ** 0.5))
                carted = clicked and a < min(1.0, BUY[g] * CART * value * (OVER_BUDGET if over else 1.0))
                bought = carted and b < CHECKOUT
                clicks, carts, orders = clicks + clicked, carts + carted, orders + bought
                rows.append({"session_id": sid, "user_id": uid, "ts": ts, "device": person["device"],
                             "query": q["query"], "query_class": q["class"], "product_id": pid, "position": k + 1,
                             "clicked": clicked, "added_to_cart": carted, "purchased": bought, "price": price,
                             "simulated": True})
            if rng.random() >= MORE:
                break
            t += dt.timedelta(seconds=20 + rng.randrange(220))
        if progress and i % 500 == 0:
            progress(0.1 + 0.8 * i / sessions)
    rows.sort(key=lambda r: (r["ts"], r["session_id"]))
    stats = {"rows": len(rows), "sessions": sessions, "searches": searches, "queries": len(qs), "products": len(prods),
             "clicks": clicks, "add_to_carts": carts, "purchases": orders,
             "first_day": (last - dt.timedelta(days=days - 1)).isoformat(), "last_day": last.isoformat(),
             "price_sensitivity": median is not None}
    return rows, stats


def params(catalog: str, sessions: int, days: int, seed: int, end: str, stats: dict) -> dict:
    """The provenance of one generated dataset: enough to make the same rows again, and the SIMULATED mark."""
    return {"generator": GENERATOR, "generator_version": VERSION, "simulated": True, "evidence": "SIMULATED",
            "catalog": catalog, "sessions": sessions, "days": days, "seed": seed, "end": end,
            "query_classes": CLASSES, "ranker": {"engine": "local-bm25", "spec": RANKER},
            "click_model": {"relevance": "heuristic text and attribute match", "attributes": list(ATTRS),
                            "click_by_grade": CLICK, "buy_by_grade": BUY, "attention": ATTENTION, "zipf_s": ZIPF_S,
                            "page": PAGE, "fold": FOLD, "notice": NOTICE, "scroll": SCROLL, "more_searches": MORE, "cart": CART, "checkout": CHECKOUT, "over_budget": OVER_BUDGET},
            # No prices in the catalog: shoppers get no budget and no price is made up. Said here, not left to be noticed.
            "price_sensitivity": "on" if stats["price_sensitivity"] else "off: the catalog has no prices, and none were invented",
            "stats": stats}
