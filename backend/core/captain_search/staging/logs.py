"""What a staging endpoint wrote down: one record per request, one per outcome, in small write-once files.

The runtime (runtime.py) buffers records and writes them as chunks under
`staging/<project>/<env>/logs/<stage id>/`:

  requests-<id>.jsonl   {request_id, ts, query, k, user_id, session_id, arm, model_version, ids, scores,
                         features: {product id: [floats]}, ms, traffic, experiment}
                        The first line of a chunk is {"_meta": {"names": {arm: [feature names]}}}.
  outcomes-<id>.jsonl   {request_id, event: click | add-to-cart | purchase, product_id, value, ts}

Everything that reads staging traffic (the consistency check, the A/B readout, `monitor record`) reads these
chunks or the dataset version published from them. `publish` turns the chunks of one stage into an `events`
dataset version, one row per result shown, in the columns `monitor.measure` and `historic` already read, plus
request_id, arm, model_version and traffic. `traffic` says where a request came from: `real`, `simulated` (the
shopper simulator) or `test` (the Try it console). Only `real` is ever REAL_TRAFFIC evidence.
"""
from __future__ import annotations

import json

EVENTS = ("click", "add-to-cart", "purchase")
TRAFFIC = ("real", "simulated", "test")
DATASET = "staging-logs"


def prefix(pid: str, env: str, stage: str | None = None) -> str:
    return f"staging/{pid}/{env}/logs/" + (f"{stage}/" if stage else "")


def dataset_name(pid: str, env: str = "staging") -> str:
    return f"{pid}/{DATASET}" if env == "staging" else f"{pid}/{env}-logs"


def _lines(data: bytes) -> list[dict]:
    return [json.loads(x) for x in data.decode().splitlines() if x.strip()]


def read(ctx, pid: str, env: str, stage: str | None = None) -> dict:
    """Every request and outcome logged for a project's staging endpoint (one stage, or all of them)."""
    reqs, outs, names = [], [], {}
    for k in sorted(ctx.store.list(prefix(pid, env, stage))):
        base = k.rsplit("/", 1)[-1]
        if base.startswith("requests-"):
            for r in _lines(ctx.store.get(k)):
                if "_meta" in r:
                    names.update(r["_meta"].get("names") or {})
                else:
                    reqs.append(r)
        elif base.startswith("outcomes-"):
            outs += _lines(ctx.store.get(k))
    return {"requests": reqs, "outcomes": outs, "names": names}


def impressions(reqs: list[dict], outs: list[dict]) -> list[dict]:
    """One row per result shown, with what happened to it: the shape events datasets have."""
    by: dict[tuple[str, str], dict] = {}
    for o in outs:
        d = by.setdefault((str(o["request_id"]), str(o.get("product_id") or "")), {"click": 0, "cart": 0, "buy": 0, "value": 0.0})
        ev = o.get("event")
        if ev == "click":
            d["click"] = 1
        elif ev == "add-to-cart":
            d["cart"] = 1
        elif ev == "purchase":
            d["buy"] = 1
            d["value"] += float(o.get("value") or 0.0)
    rows = []
    for r in reqs:
        rid = str(r["request_id"])
        for i, pid in enumerate(r.get("ids") or []):
            d = by.get((rid, str(pid))) or {}
            f = (r.get("features") or {}).get(str(pid))
            rows.append({"request_id": rid, "ts": r.get("ts"), "session_id": r.get("session_id") or rid,
                         "user_id": r.get("user_id"), "query": r.get("query"), "product_id": str(pid), "position": i + 1,
                         "clicked": bool(d.get("click") or d.get("cart") or d.get("buy")),
                         "added_to_cart": bool(d.get("cart") or d.get("buy")), "purchased": bool(d.get("buy")),
                         "value": float(d.get("value") or 0.0), "arm": r.get("arm"),
                         "model_version": r.get("model_version"), "traffic": r.get("traffic") or "real",
                         "ms": r.get("ms"), "features": json.dumps(f) if f is not None else None})
    return rows


def publish(ctx, pid: str, env: str, stage: str):
    """Publish this stage's log so far as a new version of the project's staging-logs dataset. None when it is empty."""
    import pyarrow as pa

    from ..data.datasets import snapshot
    from ..model.entities import DatasetKind, Provenance

    got = read(ctx, pid, env, stage)
    rows = impressions(got["requests"], got["outcomes"])
    if not rows:
        return None
    kinds = sorted({r["traffic"] for r in rows})
    real = kinds == ["real"]
    tags = {"staging": "true", "traffic": ",".join(kinds), "stage": stage}
    if not real:
        # anything judged on rows a simulator or the console made must never read as real shoppers
        tags.update({"synthetic": "true", "label_source": "simulated"})
    return snapshot(ctx, dataset_name(pid, env), pa.Table.from_pylist(rows), kind=DatasetKind.events,
                    provenance=Provenance(source="generated", params={"generator": "captain_search.staging",
                                                                      "project": pid, "env": env, "stage": stage,
                                                                      "requests": len(got["requests"]),
                                                                      "outcomes": len(got["outcomes"]),
                                                                      "feature_names": got["names"]}),
                    description=f"What the {env} endpoint of {pid} served: {len(got['requests']):,} requests, "
                                f"{len(got['outcomes']):,} outcomes. Traffic: {', '.join(kinds)}."
                                + ("" if real else " Not real shoppers."),
                    tags=tags)
