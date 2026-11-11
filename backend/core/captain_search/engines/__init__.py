"""Search engine adapters. A PipelineVersion names an engine and a spec. An adapter turns that into
`search(query, k) -> [product ids]` and `fingerprint() -> IndexFingerprint`.

Built in:
- `local-bm25`: in-process BM25 over a catalog dataset, with field weights, boosts and filters from
  the spec. Fully deterministic, so backtests on it reproduce. This is what demos and tests use.
- `bm25-ltr`: `local-bm25` finds the candidates, then a trained model reorders the top K. The features come from
  the same module that built the model's training set (features/), and the model and its feature tables are the
  version's own artifact files. With no model yet it is plain keyword order.
- `http`: POST {query, k} to any endpoint that returns {"ids": [...]}, for a team's own service.
- `elastic`, `typesense`, `algolia`, `captain`: thin HTTP adapters (written, not yet tested against
  live services).

Third-party adapters register under the `captain_search.engines` entry point group.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter, defaultdict
from importlib.metadata import entry_points

from ..model.entities import IndexFingerprint

TOKEN = re.compile(r"[a-z0-9]+")


def tokens(s) -> list[str]:
    return TOKEN.findall(str(s or "").lower())


class Engine:
    def search(self, query: str, k: int = 50) -> list[str]:
        raise NotImplementedError

    def search_scored(self, query: str, k: int = 50) -> list[tuple[str, float | None]]:
        """Ids with the score each got, best first. An adapter that can't report scores gives None for each."""
        return [(i, None) for i in self.search(query, k)]

    def fingerprint(self) -> IndexFingerprint | None:
        return None


class LocalBM25(Engine):
    """spec keys:
    fields: {title: 3.0, brand: 1.5, category: 1.0, description: 0.5}
    k1, b: BM25 params
    boosts: [{field: in_stock, equals: true, weight: 1.2}]  multiplies the score
    numeric_filter: true  -> "under N" in a query filters price <= N
    prefix_match: true    -> query tokens also match words they prefix (helps typos only a little)
    synonyms: {tv: [television]}
    """

    def __init__(self, products: list[dict], spec: dict):
        self.spec = spec
        self.fields = spec.get("fields") or {"title": 3.0, "brand": 1.5, "category": 1.0, "description": 0.5}
        self.k1 = float(spec.get("k1", 1.2))
        self.b = float(spec.get("b", 0.75))
        self.products = {str(p["id"]): p for p in products}
        self.syn = {k.lower(): [v.lower() for v in vs] for k, vs in (spec.get("synonyms") or {}).items()}
        self.tf: dict[str, dict[str, Counter]] = {}
        self.len: dict[str, dict[str, int]] = defaultdict(dict)
        df: dict[str, Counter] = defaultdict(Counter)
        self.post: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        for pid, p in self.products.items():
            self.tf[pid] = {}
            for f in self.fields:
                toks = tokens(p.get(f))
                c = Counter(toks)
                self.tf[pid][f] = c
                self.len[f][pid] = len(toks)
                for t in c:
                    df[f][t] += 1
                    self.post[f][t].add(pid)
        n = max(1, len(self.products))
        self.idf = {f: {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df[f].items()} for f in self.fields}
        self.avg = {f: (sum(self.len[f].values()) / n) or 1.0 for f in self.fields}
        self.vocab = sorted({t for f in self.fields for t in self.idf[f]})

    def _expand(self, q: list[str]) -> list[str]:
        out = []
        for t in q:
            out.append(t)
            out += self.syn.get(t, [])
            if self.spec.get("prefix_match") and len(t) >= 3:
                out += [v for v in self.vocab if v.startswith(t) and v != t][:5]
        return out

    def search(self, query: str, k: int = 50) -> list[str]:
        return [p for p, _ in self.search_scored(query, k)]

    def search_scored(self, query: str, k: int = 50) -> list[tuple[str, float]]:
        q = tokens(query)
        limit = None
        if self.spec.get("numeric_filter"):
            m = re.search(r"\b(?:under|below|less than)\s+\$?(\d+(?:\.\d+)?)", query.lower())
            if m:
                limit = float(m.group(1))
                q = [t for t in q if t not in ("under", "below", "less", "than", m.group(1).split(".")[0])]
        q = self._expand(q)
        scores: dict[str, float] = defaultdict(float)
        for f, w in self.fields.items():
            for t in q:
                idf = self.idf[f].get(t)
                if idf is None:
                    continue
                for pid in self.post[f][t]:
                    tf = self.tf[pid][f][t]
                    norm = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self.len[f][pid] / self.avg[f]))
                    scores[pid] += w * idf * norm
        for boost in self.spec.get("boosts") or []:
            for pid in scores:
                if str(self.products[pid].get(boost["field"])).lower() == str(boost.get("equals")).lower():
                    scores[pid] *= float(boost.get("weight", 1.0))
        if limit is not None:
            scores = {p: s for p, s in scores.items() if _price(self.products[p]) is not None and _price(self.products[p]) <= limit}
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]

    def fingerprint(self) -> IndexFingerprint:
        ids = sorted(self.products)
        sample = ids[:: max(1, len(ids) // 200)]
        h = hashlib.sha256("|".join(f"{i}:{self.products[i].get('title')}" for i in sample).encode()).hexdigest()
        return IndexFingerprint(doc_count=len(ids), sample_hash=h)


def _price(p):
    try:
        return float(p.get("price"))
    except (TypeError, ValueError):
        return None


class BM25Rerank(Engine):
    """Keyword search for the candidates, a trained model for their order.

    spec keys:
    retrieve: a local-bm25 spec ({fields, k1, b, ...})
    rerank:   {model: model.json, features: features.json, top_k: 50}: artifact files of this version. The model
              reorders the first top_k candidates; anything past them keeps its keyword order. Without `model`
              the version is untrained and answers in keyword order.

    A reranker can't add a product the first stage missed, so its recall at top_k is the first stage's recall.
    """

    def __init__(self, products: list[dict], spec: dict, artifact=None):
        from .. import features as F
        from ..features import model as FM

        self.spec = spec
        self.bm25 = LocalBM25(products, spec.get("retrieve") or {})
        rr = spec.get("rerank") or {}
        self.top_k = max(1, int(rr.get("top_k") or 50))
        self.model = self.feat = None
        if rr.get("model"):
            m = artifact(rr["model"]) if artifact else None
            f = artifact(rr.get("features") or "features.json") if artifact else None
            if m is None or f is None:
                raise ValueError("this trained version's model files can't be read, so it can't rank. They are stored "
                                 "with the version under artifact/.")
            import json

            self.model = FM.load(m)
            self.feat = F.load(products, json.loads(f))
            if self.model.names and self.model.names != self.feat.names:
                raise ValueError("the model's features don't match its feature file. Train it again.")

    def search_scored(self, query: str, k: int = 50) -> list[tuple[str, float]]:
        return self.search_detailed(query, k)[0]

    def search_detailed(self, query: str, k: int = 50) -> tuple[list[tuple[str, float]], dict[str, list[float]]]:
        """The ranked results, and the feature row the model was given for each reranked product. Serving logs
        these rows, so what was served can be checked against what training computes (staging/consistency.py)."""
        found = self.bm25.search_scored(query, max(k, self.top_k))
        if self.model is None:
            return found[:k], {}
        head, tail = found[:self.top_k], found[self.top_k:]
        rows = self.feat.features(query, [p for p, _ in head])
        scores = self.model.predict(rows)
        order = sorted(range(len(head)), key=lambda i: (-scores[i], i))
        low = min(scores) if scores else 0.0
        # the tail keeps its keyword order below every reranked result
        ranked = ([(head[i][0], scores[i]) for i in order] + [(p, low - 1 - n) for n, (p, _) in enumerate(tail)])[:k]
        return ranked, {head[i][0]: rows[i] for i in range(len(head))}

    def search(self, query: str, k: int = 50) -> list[str]:
        return [p for p, _ in self.search_scored(query, k)]

    def retrieve(self, query: str, k: int | None = None) -> list[str]:
        """What the first stage found, before reranking."""
        return self.bm25.search(query, k or self.top_k)

    def fingerprint(self) -> IndexFingerprint:
        return self.bm25.fingerprint()


class HTTPEngine(Engine):
    """spec: {url, headers?, fingerprint_url?}. POST {"query", "k"} -> {"ids": [...]}"""

    def __init__(self, spec: dict):
        self.spec = spec

    def search(self, query: str, k: int = 50) -> list[str]:
        import httpx

        from ..workspace import render_secrets

        headers = {h: render_secrets(v) for h, v in (self.spec.get("headers") or {}).items()}
        r = httpx.post(self.spec["url"], json={"query": query, "k": k}, headers=headers, timeout=30)
        r.raise_for_status()
        return [str(i) for i in r.json()["ids"]][:k]

    def fingerprint(self):
        if not self.spec.get("fingerprint_url"):
            return None
        import httpx

        j = httpx.get(self.spec["fingerprint_url"], timeout=30).json()
        return IndexFingerprint(doc_count=int(j["doc_count"]), sample_hash=str(j["sample_hash"]))


class ElasticEngine(HTTPEngine):
    """spec: {url: https://host:9200, index, fields: ["title^3", "description"], api_key_secret?}"""

    def search(self, query: str, k: int = 50) -> list[str]:
        import httpx

        from ..workspace import get_secret

        h = {"Authorization": f"ApiKey {get_secret(self.spec['api_key_secret'])}"} if self.spec.get("api_key_secret") else {}
        body = {"size": k, "_source": False, "query": {"multi_match": {"query": query, "fields": self.spec.get("fields", ["*"])}}}
        r = httpx.post(f"{self.spec['url'].rstrip('/')}/{self.spec['index']}/_search", json=body, headers=h, timeout=30)
        r.raise_for_status()
        return [hit["_id"] for hit in r.json()["hits"]["hits"]]

    def fingerprint(self):
        import httpx

        from ..workspace import get_secret

        h = {"Authorization": f"ApiKey {get_secret(self.spec['api_key_secret'])}"} if self.spec.get("api_key_secret") else {}
        base = f"{self.spec['url'].rstrip('/')}/{self.spec['index']}"
        n = httpx.get(f"{base}/_count", headers=h, timeout=30).json()["count"]
        ids = httpx.post(f"{base}/_search", json={"size": 200, "_source": False, "sort": ["_doc"]}, headers=h, timeout=30).json()
        sample = hashlib.sha256("|".join(x["_id"] for x in ids["hits"]["hits"]).encode()).hexdigest()
        return IndexFingerprint(doc_count=int(n), sample_hash=sample)


class TypesenseEngine(HTTPEngine):
    """spec: {url, collection, query_by: "title,description", api_key_secret}"""

    def search(self, query: str, k: int = 50) -> list[str]:
        import httpx

        from ..workspace import get_secret

        r = httpx.get(f"{self.spec['url'].rstrip('/')}/collections/{self.spec['collection']}/documents/search",
                      params={"q": query, "query_by": self.spec.get("query_by", "title"), "per_page": min(k, 250)},
                      headers={"X-TYPESENSE-API-KEY": get_secret(self.spec["api_key_secret"])}, timeout=30)
        r.raise_for_status()
        return [h["document"]["id"] for h in r.json()["hits"]]


class AlgoliaEngine(HTTPEngine):
    """spec: {app_id, index, api_key_secret}"""

    def search(self, query: str, k: int = 50) -> list[str]:
        import httpx

        from ..workspace import get_secret

        app = self.spec["app_id"]
        r = httpx.post(f"https://{app}-dsn.algolia.net/1/indexes/{self.spec['index']}/query",
                       json={"query": query, "hitsPerPage": k, "attributesToRetrieve": ["objectID"]},
                       headers={"X-Algolia-Application-Id": app, "X-Algolia-API-Key": get_secret(self.spec["api_key_secret"])},
                       timeout=30)
        r.raise_for_status()
        return [h["objectID"] for h in r.json()["hits"]]


class CaptainEngine(HTTPEngine):
    """spec: {url: https://…/v1/rank, api_key_secret}. Sends {query, k} and reads {ids}."""

    def search(self, query: str, k: int = 50) -> list[str]:
        import httpx

        from ..workspace import get_secret

        r = httpx.post(self.spec["url"], json={"query": query, "k": k},
                       headers={"Authorization": f"Bearer {get_secret(self.spec['api_key_secret'])}"}, timeout=30)
        r.raise_for_status()
        j = r.json()
        return [str(i) for i in (j.get("ids") or [h["id"] for h in j.get("results", [])])][:k]


BUILTIN = {"http": HTTPEngine, "elastic": ElasticEngine, "typesense": TypesenseEngine, "algolia": AlgoliaEngine,
           "captain": CaptainEngine}


def artifact_reader(ctx, pm):
    """`read(path) -> bytes or None` over a pipeline version's own artifact files (stored under artifact/)."""
    by = {f.path: f for f in (getattr(pm, "files", None) or []) if f.path.startswith("artifact/")}

    def read(path: str):
        f = by.get("artifact/" + str(path).lstrip("/"))
        return ctx.registry.get_blob(f) if f else None

    return read


def make_engine(engine: str, spec: dict, products: list[dict] | None = None, workspace: str | None = None,
                artifact=None) -> Engine:
    """`artifact` reads a file of the version's trained artifact by path (see artifact_reader). Only engines that
    rank with trained files need it."""
    from .. import extensions as X

    e = X.load(workspace).get("engine", engine)
    if e is not None and e.source != "builtin":
        return e.obj(spec=spec, products=products)
    if engine == "local-bm25":
        if products is None:
            raise ValueError("local-bm25 needs the pipeline's catalog dataset")
        return LocalBM25(products, spec)
    if engine == "bm25-ltr":
        if products is None:
            raise ValueError("bm25-ltr needs the pipeline's catalog dataset")
        return BM25Rerank(products, spec, artifact)
    if engine in BUILTIN:
        return BUILTIN[engine](spec)
    for ep in entry_points(group="captain_search.engines"):
        if ep.name == engine:
            return ep.load()(spec=spec, products=products)
    raise ValueError(f"unknown engine {engine!r}. Built in: local-bm25, bm25-ltr, {', '.join(BUILTIN)}")


def timed_search(engine: Engine, query: str, k: int) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    ids = engine.search(query, k)
    return ids, (time.perf_counter() - t0) * 1000
