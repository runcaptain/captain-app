"""Did serving compute the features training computes? The check behind "Served features match training".

A ranker trained on one set of numbers and served another is a different ranker, and nothing offline shows it.
The runtime logs the feature row it gave the model for every result it reranked (logs.py), each with its request
id and timestamp. This recomputes those rows offline, for the same (query, product) pairs, with the feature
module and the feature file of the model version in the registry (what the training set was built with), and
compares.

The four measures are the ones Chronon reports for online and offline consistency
(research/2026-11-10, "Online_Offline_Consistency"):

  mismatch rate    share of feature values that differ between served and offline
  missing rate     share of the values offline computes that the served row does not have
  extra rate       share of the served values offline does not compute
  relative error   per feature, the mean of |served - offline| / max(|served|, |offline|)

One line sums it up: the share of (query, product) pairs whose whole row matched. The check passes at or above
`min_match` (0.999 unless told otherwise) with no missing or extra values, and a failed check blocks "Send to
launch review" as a launch criterion (ops.gates_for), which a person can override with a reason.

The pairs match on (query, product) alone, which is right while feature tables are frozen at one cutoff. Once
serving refreshes click counts, the comparison has to be made as of each request's timestamp; the request id and
timestamp are logged now so that it can be.
"""
from __future__ import annotations

import datetime as dt
import json

from ..storage.backend import NotFound
from ..workspace import Context, whoami
from . import logs as L

MIN_MATCH = 0.999
TOLERANCE = 1e-6
SAMPLE = 5000


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def check_key(pipeline: str) -> str:
    return "staging/checks/" + pipeline.replace("/", "__").replace("@", "-") + ".json"


def _offline(ctx: Context, pipeline: str):
    """The featurizer training used for this version: its own feature file, over its own catalog."""
    from .. import features as F
    from .. import ops
    from ..engines import artifact_reader

    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    rr = (pm.spec or {}).get("rerank") or {}
    if pm.engine != "bm25-ltr" or not rr.get("model"):
        return None
    doc = artifact_reader(ctx, pm)(rr.get("features") or "features.json")
    if doc is None:
        return None
    return F.load(ops.products_of(ctx, pm.catalog), json.loads(doc))


def measure(ctx: Context, got: dict, sample: int = SAMPLE, tolerance: float = TOLERANCE) -> dict:
    """Compare the feature rows in a staging log (logs.read) with the rows computed offline. No verdict here."""
    reqs = [r for r in got["requests"] if r.get("features")]
    pairs = [(r, pid, row) for r in reqs for pid, row in r["features"].items()]
    step = max(1, len(pairs) // max(1, sample))
    pairs = pairs[::step][:sample]
    feats: dict = {}
    per: dict[str, dict] = {}
    checked = matched = values = bad = missing = extra = expected = served_n = no_id = 0
    versions = set()
    examples = []
    for r, pid, served in pairs:
        ver = r.get("model_version")
        if ver not in feats:
            feats[ver] = _offline(ctx, ver)
        f = feats[ver]
        if f is None:
            continue
        versions.add(ver)
        names_served = (got.get("names") or {}).get(r.get("arm")) or f.names
        s = dict(zip(names_served, served))
        o = dict(zip(f.names, f.features(r["query"], [pid])[0]))
        checked += 1
        no_id += 0 if r.get("request_id") and r.get("ts") else 1
        ok = True
        expected += len(o)
        served_n += len(s)
        for name, want in o.items():
            d = per.setdefault(name, {"n": 0, "bad": 0, "err": 0.0})
            have = s.get(name)
            if have is None:
                missing += 1
                ok = False
                continue
            values += 1
            d["n"] += 1
            diff = abs(float(have) - float(want))
            top = max(abs(float(have)), abs(float(want)))
            if diff > tolerance * max(1.0, top):
                bad += 1
                d["bad"] += 1
                d["err"] += diff / top if top else 0.0
                ok = False
                if len(examples) < 5:
                    examples.append({"request_id": r.get("request_id"), "query": r["query"], "product_id": pid,
                                     "feature": name, "served": have, "offline": want})
        for name in s:
            if name not in o:
                extra += 1
                ok = False
        matched += ok
    worst = sorted(({"feature": n, "mismatch_rate": round(d["bad"] / d["n"], 6), "relative_error": round(d["err"] / d["n"], 6)}
                    for n, d in per.items() if d["n"] and d["bad"]), key=lambda x: -x["mismatch_rate"])
    return {"requests": len(reqs), "checked": checked, "matched": matched,
            "match_rate": round(matched / checked, 6) if checked else None,
            "mismatch_rate": round(bad / values, 6) if values else None,
            "missing_rate": round(missing / expected, 6) if expected else None,
            "extra_rate": round(extra / served_n, 6) if served_n else None,
            "relative_error": {x["feature"]: x["relative_error"] for x in worst},
            "features": worst[:20], "examples": examples, "without_request_id": no_id,
            "model_versions": sorted(versions)}


def check(ctx: Context, pid: str, env: str = "staging", sample: int = SAMPLE, min_match: float = MIN_MATCH,
          dry_run: bool = False) -> dict:
    """Run the check on what the project's staging endpoint has served so far, and record the result."""
    from . import _save, call, state

    rec = state(ctx, pid, env)
    if not rec:
        raise ValueError(f"Nothing is staged for {pid}. Stage a model version and try it first.")
    if rec.get("how") == "image":
        out = {"status": "unavailable", "text": "Not checked: your own container doesn't log the features it serves."}
    else:
        if rec.get("status") == "running":
            try:
                call(ctx, rec, "flush", {})
            except Exception:  # noqa: BLE001  the chunks already written are still checked
                pass
        m = measure(ctx, L.read(ctx, pid, env, rec["id"]), sample)
        if not m["checked"]:
            out = {**m, "status": "nothing-to-check",
                   "text": ("Nothing to check yet. Try a query first, so the endpoint serves something." if not m["requests"]
                            else "Not checked: the staged version ranks without trained features, so there is nothing to compare.")}
        else:
            ok = m["match_rate"] >= min_match and not m["missing_rate"] and not m["extra_rate"] and not m["without_request_id"]
            pct = f"{m['match_rate'] * 100:.1f}".rstrip("0").rstrip(".")
            out = {**m, "status": "pass" if ok else "fail",
                   "text": f"Served features match training: {pct}% of {m['checked']:,} checked"
                           + ("" if ok else f". It needs {min_match * 100:g}%."
                              + (f" Worst: {m['features'][0]['feature']}." if m["features"] else ""))}
    out.update(project=pid, env=env, stage=rec["id"], model_version=rec.get("model_version"), min_match=min_match,
               checked_at=_now(), checked_by=whoami())
    if dry_run:
        return {**out, "dry_run": True}
    _save(ctx, pid, env, check={k: out[k] for k in ("status", "text", "checked", "matched", "match_rate", "mismatch_rate",
                                                    "missing_rate", "extra_rate", "min_match", "checked_at", "stage",
                                                    "model_version") if k in out})
    if out["status"] in ("pass", "fail"):
        for ver in out.get("model_versions") or []:
            k = check_key(ver)
            body = json.dumps({**{x: out[x] for x in ("status", "text", "checked", "match_rate", "checked_at", "project", "stage")},
                               "pipeline": ver}).encode()
            try:
                _, tag = ctx.store.get_with_tag(k)
            except NotFound:
                tag = None
            ctx.store.put_if(k, body, tag)
    return out


def latest_for(ctx: Context, pipeline: str) -> dict | None:
    """The newest served-features check of this model version, from any project that staged it."""
    try:
        return json.loads(ctx.store.get(check_key(pipeline)))
    except NotFound:
        return None
