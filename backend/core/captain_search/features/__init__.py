"""Ranking features for a (query, product) pair: one module, used to build training sets and to score at search time.

A ranker trained on one set of features and served with another is a different ranker. So there is exactly one
place that turns a query and a product into numbers: `Featurizer.features`. The training-set builder
(training_set.py) calls it to write the rows a trainer reads, and the rerank model version
(engines.BM25Rerank) calls it on the candidates it reorders. Both give it the same `spec` and the same
popularity `tables`, which travel with the trained model as `features.json`.

What it computes, from the columns the catalog has:
- text match: a BM25 score per text column, how many of the query's words the title holds (whole words, and word
  beginnings), whether the title holds the query as a phrase;
- brand and attribute match, when those columns exist;
- price, only when a price column exists: its size, its place in the catalog, and whether it is under a limit the
  query names ("under 50");
- popularity, only when `tables` are given: how often each product, and each (query, product), was shown and
  clicked, corrected for the rank it was shown at (clicks over expected clicks, as historic.py grades clicks).

Popularity is the leak risk. A click rate counted from the same searches the labels come from tells the model the
answer. `popularity_tables` therefore records the time window of the rows it was built from, and `check_windows`
refuses a feature window that reaches into the label window. Every value is a plain float and a missing one is
0.0, so the model never sees NaN.
"""
from __future__ import annotations

import math
import re
from collections import Counter

VERSION = "1"
ATTRS = ("color", "colour", "gender", "size", "material", "product_type", "tags")
TS_COLUMNS = ("ts", "timestamp", "event_time", "time")
K1, B = 1.2, 0.75
PRIOR = 1.0  # smoothing for clicks over expected clicks: one expected click at the average rate
_TOKEN = re.compile(r"[a-z0-9]+")
_LIMIT = re.compile(r"\b(?:under|below|less than)\s+\$?(\d+(?:\.\d+)?)")


class Leak(ValueError):
    """A training set that would let the model see its own labels. The message is the sentence shown to the person."""


def tokens(s) -> list[str]:
    if isinstance(s, (list, tuple, set)):
        s = " ".join(str(x) for x in s)
    return _TOKEN.findall(str(s or "").lower())


def norm_query(q) -> str:
    return " ".join(str(q or "").lower().split())


def ts_key(v) -> str:
    """A timestamp as text that sorts in time order (ISO dates and datetimes do)."""
    return str(v if v is not None else "")


def ts_column(columns) -> str | None:
    return next((c for c in TS_COLUMNS if c in columns), None)


# ---- which columns to read ----------------------------------------------------------------------


def detect_spec(columns: list[str], title: str | None = None) -> dict:
    """The feature spec for a catalog with these columns. `title` names the title column when it can't be guessed.
    Raises ValueError, in a sentence, when no title column can be found."""
    from ..data.prepare import auto_map

    m = auto_map(list(columns), "catalog")
    if title:
        if title not in columns:
            raise ValueError(f"the catalog has no column named {title}. Its columns: {', '.join(columns)}")
        m["title"] = title
    if not m.get("title"):
        raise ValueError("Captain can't tell which column holds the product title. Its columns: "
                         f"{', '.join(columns)}. Say which one with --title.")
    text = [m[k] for k in ("description", "category") if m.get(k)]
    attrs = [c for c in columns if c.lower() in ATTRS and c not in text and c != m["title"]]
    return {"version": VERSION, "title": m["title"], "text": text, "brand": m.get("brand"), "attributes": attrs,
            "price": m.get("price")}


def bm25_fields(spec: dict) -> dict:
    """The keyword-search field weights that match a feature spec: the title counts most."""
    out = {spec["title"]: 3.0}
    if spec.get("brand"):
        out[spec["brand"]] = 1.5
    for i, f in enumerate(spec.get("text") or []):  # detect_spec lists the description first, then the category
        out[f] = 0.5 if i == 0 and len(spec["text"]) > 1 or re.search(r"desc|body|detail|summary", f.lower()) else 1.0
    return out


def feature_names(spec: dict, popularity: bool) -> list[str]:
    fields = [spec["title"]] + list(spec.get("text") or [])
    names = [f"bm25_{f}" for f in fields] + ["bm25_all", "title_overlap", "title_partial", "title_phrase",
                                             "title_cover", "any_overlap", "query_len", "title_len"]
    if spec.get("brand"):
        names.append("brand_match")
    names += [f"attr_{a}_match" for a in spec.get("attributes") or []]
    if spec.get("price"):
        names += ["price_log", "price_rank", "price_has_limit", "price_under_limit"]
    if popularity:
        names += ["pop_impressions_log", "pop_clicks_log", "pop_coec", "pop_purchase_rate", "qp_impressions_log",
                  "qp_coec"]
    return names


# ---- popularity, from events before a cutoff ----------------------------------------------------


def popularity_tables(rows: list[dict], depth: int = 20) -> dict:
    """Click and impression counts per product and per (query, product), from exactly these event rows. The window
    it records is the first and last timestamp found in the rows themselves, never what a caller says it is."""
    from ..historic import ctr_by_rank

    col = ts_column(rows[0]) if rows else None
    stamps = sorted(ts_key(r.get(col)) for r in rows) if col else []
    ctr = ctr_by_rank(rows, depth) if rows else [0.0] * depth
    prod: dict[str, list[float]] = {}
    pair: dict[str, list[float]] = {}
    for r in rows:
        k = int(r["position"]) - 1
        exp = ctr[k] if 0 <= k < len(ctr) else ctr[-1]
        pid = str(r["product_id"])
        a = prod.setdefault(pid, [0, 0.0, 0, 0])
        b = pair.setdefault(f"{norm_query(r['query'])}\t{pid}", [0, 0.0, 0])
        c = 1 if r.get("clicked") else 0
        a[0] += 1
        a[1] += exp
        a[2] += c
        a[3] += 1 if r.get("purchased") else 0
        b[0] += 1
        b[1] += exp
        b[2] += c
    rnd = lambda v: [round(x, 6) if isinstance(x, float) else x for x in v]  # noqa: E731
    return {"window": {"from": stamps[0] if stamps else None, "to": stamps[-1] if stamps else None, "rows": len(rows),
                       "column": col},
            "product": {k: rnd(v) for k, v in sorted(prod.items())}, "pair": {k: rnd(v) for k, v in sorted(pair.items())}}


def check_windows(feature_window: dict | None, label_window: dict | None) -> None:
    """Refuse popularity features that were counted from the searches the labels come from."""
    if not feature_window or not feature_window.get("rows"):
        return
    if not label_window:
        return
    f_to, l_from = feature_window.get("to"), label_window.get("from")
    if f_to is None or l_from is None:
        raise Leak("The events have no timestamp, so Captain can't prove the click rates used as features were "
                   "counted before the clicks used as labels. Leave popularity features out, or add a timestamp column.")
    if f_to >= l_from:
        raise Leak(f"The click rates used as features were counted from searches up to {f_to}, and the labels start "
                   f"at {l_from}. A feature counted from the same searches as the labels gives the model the answer. "
                   "Count features only from searches before the cutoff.")


# ---- the features -------------------------------------------------------------------------------


class Featurizer:
    """Features for (query, product) pairs over one catalog. Build it once, then call `features` per query."""

    def __init__(self, products: list[dict], spec: dict, tables: dict | None = None):
        if str(spec.get("version")) != VERSION:
            raise ValueError(f"this model was trained with feature version {spec.get('version')}, and this Captain "
                             f"computes version {VERSION}. Train it again.")
        self.spec, self.tables = spec, tables
        self.fields = [spec["title"]] + list(spec.get("text") or [])
        self.weights = bm25_fields(spec)
        self.names = feature_names(spec, tables is not None)
        self.products = {str(p["id"]): p for p in products}
        self.toks: dict[str, dict[str, Counter]] = {}
        df = {f: Counter() for f in self.fields}
        total = {f: 0 for f in self.fields}
        for pid, p in self.products.items():
            self.toks[pid] = {}
            for f in self.fields:
                c = Counter(tokens(p.get(f)))
                self.toks[pid][f] = c
                total[f] += sum(c.values())
                df[f].update(c.keys())
        n = max(1, len(self.products))
        self.idf = {f: {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df[f].items()} for f in self.fields}
        self.avg = {f: (total[f] / n) or 1.0 for f in self.fields}
        self.brand = {pid: set(tokens(p.get(spec["brand"]))) for pid, p in self.products.items()} if spec.get("brand") else {}
        self.attrs = {a: {pid: set(tokens(p.get(a))) for pid, p in self.products.items()}
                      for a in spec.get("attributes") or []}
        self.price: dict[str, float] = {}
        self.price_rank: dict[str, float] = {}
        if spec.get("price"):
            for pid, p in self.products.items():
                try:
                    v = float(p.get(spec["price"]))
                except (TypeError, ValueError):
                    continue
                if v > 0 and math.isfinite(v):
                    self.price[pid] = v
            order = sorted(self.price, key=lambda i: (self.price[i], i))
            self.price_rank = {pid: (i + 1) / len(order) for i, pid in enumerate(order)}

    def _bm25(self, q: list[str], pid: str, f: str) -> float:
        c = self.toks[pid][f]
        if not c:
            return 0.0
        ln = sum(c.values())
        s = 0.0
        for t in q:
            tf = c.get(t)
            if tf:
                s += self.idf[f][t] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * ln / self.avg[f]))
        return s

    def features(self, query: str, product_ids: list[str]) -> list[list[float]]:
        """One row of floats per product id, in `self.names` order. An id the catalog doesn't hold gets zeros."""
        q = tokens(query)
        qset = set(q)
        nq = norm_query(query)
        lim = _LIMIT.search(str(query).lower())
        limit = float(lim.group(1)) if lim else None
        tb = self.tables
        out = []
        for pid in product_ids:
            pid = str(pid)
            if pid not in self.products:
                out.append([0.0] * len(self.names))
                continue
            row = []
            total = 0.0
            for f in self.fields:
                s = self._bm25(q, pid, f)
                total += self.weights.get(f, 1.0) * s
                row.append(s)
            title = self.toks[pid][self.spec["title"]]
            ttoks = list(title)
            exact = sum(1 for t in qset if t in title)
            partial = sum(1 for t in qset if t in title or (len(t) >= 3 and any(x.startswith(t) or (len(x) >= 3 and t.startswith(x)) for x in ttoks)))
            words = tokens(self.products[pid].get(self.spec["title"]))
            phrase = 1.0 if q and any(words[i:i + len(q)] == q for i in range(len(words) - len(q) + 1)) else 0.0
            everything = set().union(*(self.toks[pid][f].keys() for f in self.fields), self.brand.get(pid, set()),
                                     *(self.attrs[a][pid] for a in self.attrs))
            row += [total, exact / len(qset) if qset else 0.0, partial / len(qset) if qset else 0.0, phrase,
                    (sum(1 for t in ttoks if t in qset) / len(ttoks)) if ttoks else 0.0,
                    (sum(1 for t in qset if t in everything) / len(qset)) if qset else 0.0,
                    float(len(q)), float(len(words))]
            if self.spec.get("brand"):
                b = self.brand[pid]
                row.append((sum(1 for t in b if t in qset) / len(b)) if b else 0.0)
            for a in self.attrs:
                v = self.attrs[a][pid]
                row.append(1.0 if v and v & qset else 0.0)
            if self.spec.get("price"):
                p = self.price.get(pid)
                row += [math.log1p(p) if p else 0.0, self.price_rank.get(pid, 0.0), 1.0 if limit is not None else 0.0,
                        1.0 if limit is not None and p is not None and p <= limit else 0.0]
            if tb is not None:
                a = tb["product"].get(pid) or [0, 0.0, 0, 0]
                b = tb["pair"].get(f"{nq}\t{pid}") or [0, 0.0, 0]
                row += [math.log1p(a[0]), math.log1p(a[2]), (a[2] + PRIOR) / (a[1] + PRIOR), (a[3] / a[0]) if a[0] else 0.0,
                        math.log1p(b[0]), (b[2] + PRIOR) / (b[1] + PRIOR)]
            out.append([round(float(x), 6) for x in row])
        return out


def bundle(spec: dict, tables: dict | None, extra: dict | None = None) -> dict:
    """What travels with a training set and then with the trained model: everything `load` needs."""
    return {"version": VERSION, "spec": spec, "names": feature_names(spec, tables is not None), "tables": tables,
            **(extra or {})}


def load(products: list[dict], doc: dict) -> Featurizer:
    f = Featurizer(products, doc["spec"], doc.get("tables"))
    if f.names != doc.get("names"):
        raise ValueError("this model's feature list doesn't match what this Captain computes. Train it again.")
    return f
