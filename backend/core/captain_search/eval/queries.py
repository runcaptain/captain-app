"""Query generation from a catalog, and holdout sealing.

Every query is built from the catalog itself,
deterministically from a seed, tagged with a failure class, and the holdout split is sealed with a
sha256 before any pipeline is tuned against it. This version is self-contained and simpler.

Catalog columns used (any missing ones are skipped): id, title, brand, category, price, description.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from collections import defaultdict

CLASSES = ["head", "brand", "multi-attr", "numeric", "model-code", "typo", "zero-result"]
STOP = set("a an the and or of for with to on at by from in is are be as it its this that new".split())
CODE = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]{2,}(?:-[A-Za-z0-9]+)*\b")
OFF_CATALOG = ["hot tub cover", "wedding dress", "jet ski", "piano tuning", "garden gnome", "snow tires",
               "dog grooming table", "kayak paddle", "telescope mount", "espresso grinder"]


def words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 1]


def qid(*parts) -> str:
    return "q-" + hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:12]


def _typo(w: str, rng: random.Random) -> str:
    if len(w) < 5:
        return w
    i = rng.randrange(1, len(w) - 2)
    return w[:i] + w[i + 1] + w[i] + w[i + 2:]


def generate(products: list[dict], classes: list[str], n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    prods = [p for p in products if p.get("title")]
    if not prods:
        raise ValueError("the catalog has no rows with a title column")
    by_cat = defaultdict(list)
    for p in prods:
        if p.get("category"):
            by_cat[str(p["category"]).split("|")[0].strip().lower()].append(p)
    prices = sorted(float(p["price"]) for p in prods if _num(p.get("price")) is not None)
    per = max(1, n // max(1, len(classes)))
    out, seen = [], set()

    def add(cls, text, src=None):
        text = re.sub(r"\s+", " ", text).strip().lower()
        if not text or text in seen:
            return
        seen.add(text)
        out.append({"query_id": qid(cls, text), "query": text, "class": cls, "source_id": None if src is None else str(src)})

    for cls in classes:
        target = len(out) + per
        for _ in range(per * 20):
            if len(out) >= target:
                break
            p = rng.choice(prods)
            tw = words(p["title"])
            if cls == "head":
                if by_cat:
                    add(cls, rng.choice(list(by_cat)))
                elif tw:
                    add(cls, " ".join(tw[:2]), p.get("id"))
            elif cls == "brand" and p.get("brand") and tw:
                add(cls, f"{p['brand']} {rng.choice(tw)}", p.get("id"))
            elif cls == "multi-attr" and len(tw) >= 3:
                add(cls, " ".join(rng.sample(tw, 3)), p.get("id"))
            elif cls == "numeric" and prices and tw:
                limit = prices[int(rng.random() * (len(prices) - 1))]
                limit = max(1, round(limit / 5) * 5)
                add(cls, f"{' '.join(tw[:2])} under {int(limit)}", p.get("id"))
            elif cls == "model-code":
                codes = CODE.findall(p["title"])
                if codes:
                    c = rng.choice(codes)
                    add(cls, c.replace("-", rng.choice(["", " ", "-"])), p.get("id"))
            elif cls == "typo" and tw:
                w = max(tw, key=len)
                add(cls, " ".join(_typo(x, rng) if x == w else x for x in tw[:3]), p.get("id"))
            elif cls == "zero-result":
                add(cls, rng.choice(OFF_CATALOG) + ("" if rng.random() < 0.5 else f" {rng.randint(2, 99)}"))
    rng.shuffle(out)
    return out[:n]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def split_of(query_id: str, holdout: float, salt: str) -> str:
    h = int(hashlib.md5(f"{salt}:{query_id}".encode()).hexdigest(), 16) % 10_000
    return "holdout" if h < holdout * 10_000 else "train"


def seal(rows: list[dict], ids: list[str]) -> str:
    """sha256 over the sorted (query_id, query) pairs of the holdout split."""
    by = {r["query_id"]: r["query"] for r in rows}
    payload = json.dumps(sorted((i, by[i]) for i in ids), separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
