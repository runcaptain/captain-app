"""`cst demo import`: register an organization's store demos from what already exists.

The demos themselves are private data: a YAML file at $CST_DEMOS or <CST_HOME>/demos.yaml names a model registry
(s3://<bucket>, in the <architecture>/<brand>/<version>/ layout) and, per brand, its catalog, logs, models, local
deploy and A/B readouts. Nothing about any one organization is in this code.

What it writes into the workspace, all write-once and nothing large copied:
- catalogs/<brand>        by reference to the registry copy (plus the Modal source, marked mutable)
- logs/<brand>/training   by reference; every brand's logs are SIMULATED shoppers
- models/<brand>-<label>  one pipeline per registered model, linked to its registry path
- <brand>-localhost       a demo-tier environment per demo, with the live.json choice backfilled
- demos/<brand>/<readout> imported backtest runs from the existing A/B readouts (copied: small JSON)

Every imported number keeps its label. No brand has its own shopper data: "REAL" results in the
registry are replays of other stores' public logs, so they are imported as SIMULATED with a note.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import yaml

from . import models as MD
from . import ops
from .external import register
from .model.entities import (
    ArmResult, Backtest, BacktestRun, DatasetKind, Deployment, Evidence, ExternalFile, Preregistration, Provenance,
)
from .storage.backend import AlreadyExists, NotFound
from .workspace import Context, whoami

T = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


def demos_file() -> str:
    from .workspace import home

    return os.path.expanduser(os.environ.get("CST_DEMOS") or os.path.join(home(), "demos.yaml"))


def load_demos() -> tuple[str, dict]:
    """(registry URI, brands) from the private demos file. Store demos are an organization's own data, so they are
    never part of Captain's code."""
    f = demos_file()
    if not os.path.exists(f):
        raise FileNotFoundError(f"no demos file at {f}. Put your organization's store demos there (or set CST_DEMOS). "
                                "`cst demo seed` makes a synthetic store instead.")
    with open(f) as fh:
        doc = yaml.safe_load(fh) or {}
    return doc.get("registry") or "", doc.get("brands") or {}


def model_pipeline_name(brand: str, label: str) -> str:
    return f"models/{brand}-{label}"


def _s3():
    from .cloud import aws_session

    return aws_session(None, "us-west-1").client("s3")


def _read_json(path: str, s3, reg: str) -> tuple[dict, str]:
    if path.startswith("local:"):
        p = os.path.expanduser(path[len("local:"):])
        with open(p) as fh:
            return json.load(fh), p
    bucket = reg.removeprefix("s3://").split("/", 1)[0]
    body = s3.get_object(Bucket=bucket, Key=path)["Body"].read()
    return json.loads(body), f"{reg}/{path}"


def extract_comparisons(obj, path: str = "", out: list | None = None, limit: int = 60) -> list[dict]:
    """Find every interval in a readout: [value, lo, hi] lists, {rel, rel_ci}, {lift, lo, hi},
    {b_minus_a, lo, hi}. Each becomes a comparison with a verdict from its CI."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out

    def add(metric, v, lo, hi, extra=None):
        try:
            v, lo, hi = float(v), float(lo), float(hi)
        except (TypeError, ValueError):
            return
        if not (lo <= v <= hi or hi <= v <= lo):
            return
        verdict = "noise" if lo <= 0 <= hi else ("win" if v > 0 else "loss")
        out.append({"metric": metric, "delta": round(v, 6), "ci_low": round(lo, 6), "ci_high": round(hi, 6),
                    "verdict": verdict, **(extra or {})})

    if isinstance(obj, dict):
        if "rel" in obj and isinstance(obj.get("rel_ci"), list) and len(obj["rel_ci"]) == 2:
            add(f"{path}.rel" if path else "rel", obj["rel"], *obj["rel_ci"], {"a": obj.get("a", obj.get("baseline")), "b": obj.get("b", obj.get("model"))})
        if {"lift", "lo", "hi"} <= set(obj):
            add(f"{path}.lift" if path else "lift", obj["lift"], obj["lo"], obj["hi"], {"a": obj.get("baseline"), "b": obj.get("model")})
        if {"b_minus_a", "lo", "hi"} <= set(obj):
            add(f"{path}.b_minus_a" if path else "b_minus_a", obj["b_minus_a"], obj["lo"], obj["hi"],
                {"a": obj.get("a_ndcg"), "b": obj.get("b_ndcg"), "n": obj.get("n")})
        for k, v in obj.items():
            extract_comparisons(v, f"{path}.{k}" if path else str(k), out, limit)
    elif isinstance(obj, list) and len(obj) in (3, 4) and all(isinstance(x, (int, float)) for x in obj[:3]) and path:
        add(path, obj[0], obj[1], obj[2])
    return out


def import_demos(ctx: Context, progress=print) -> dict:
    REG, BRANDS = load_demos()
    if not REG:
        raise ValueError(f"the demos file {demos_file()} names no model registry (registry: s3://...)")
    s3 = _s3()
    snap = MD.latest(ctx)
    if snap is None or snap.uri != REG:
        snap = MD.import_registry(ctx, REG, s3=s3)
        progress(f"imported registry {snap.name}@v{snap.version}")
    models = {m["id"]: m for m in snap.models}
    summary = {}
    for brand, b in BRANDS.items():
        done = {"catalog": None, "logs": None, "pipelines": [], "environment": None, "runs": []}
        # catalog by reference
        cat = _once(ctx, "dataset", f"catalogs/{brand}", lambda: register(
            ctx, f"catalogs/{brand}", [b["catalog"]], DatasetKind.catalog, row_count=b["rows"],
            description=f"{b['label']} catalog, {b['rows']:,} products (registry copy). {b['note']}",
            tags={"brand": brand}, s3=s3,
            extra_files=[ExternalFile(uri=u, bytes=n, mutable=u.startswith("modal://")) for u, n in b["catalog_src"]],
            provenance=Provenance(source="import", params={"registry_copy": b["catalog"], "sources": [u for u, _ in b["catalog_src"]]})))
        done["catalog"] = ops.ref(cat)
        # training logs by reference, all simulated
        if b.get("logs_s3"):
            lg = _once(ctx, "dataset", f"logs/{brand}/training", lambda: register(
                ctx, f"logs/{brand}/training", [b["logs_s3"]], DatasetKind.logs, s3=s3,
                description=f"{b['label']} training sessions from simulated shoppers (copied to the registry).",
                tags={"brand": brand, "synthetic": "true"}))
        else:
            lg = _once(ctx, "dataset", f"logs/{brand}/training", lambda: register(
                ctx, f"logs/{brand}/training", [], DatasetKind.logs,
                extra_files=[ExternalFile(uri=u, bytes=n, mutable=True) for u, n in b["logs"]],
                description=f"{b['label']} training sessions from simulated shoppers. On Modal volumes only, which can change; "
                            "the registry keeps their checksums in inputs/REFERENCE.sha256.",
                tags={"brand": brand, "synthetic": "true"}))
        done["logs"] = ops.ref(lg)
        # one pipeline per registered model
        for mid, m in sorted(models.items()):
            if m.get("brand") != brand:
                continue
            name = model_pipeline_name(brand, m["version"])
            pm = _once(ctx, "pipeline", name, lambda m=m, name=name: _model_pipeline(ctx, m, name, ops.ref(cat), b))
            done["pipelines"].append(ops.ref(pm))
        if b.get("baseline"):  # a demo that serves a hand-built ranker as well as its registered models
            pm = _once(ctx, "pipeline", model_pipeline_name(brand, "baseline"), lambda: ops.pipeline_publish(ctx, {
                "name": model_pipeline_name(brand, "baseline"), "engine": "http", "catalog": ops.ref(cat),
                "spec": {"url": f"http://localhost:{b['port']}/api/search"},
                "description": b["baseline"]}))
            done["pipelines"].append(ops.ref(pm))
        # localhost staging environment with the live.json history backfilled
        env_name = f"{brand}-localhost" if b["port"] else f"{brand}-modal"
        url = f"http://localhost:{b['port']}" if b["port"] else f"modal app {b.get('modal_app')}"
        try:
            ops.env_create(ctx, env_name, "demo", {"type": "record", "url": url})
        except AlreadyExists:
            pass
        if not ops.history(ctx, env_name):
            prev = None
            for label, when, action in b["history"]:
                pref = ops.ref(ctx.registry.load("pipeline", model_pipeline_name(brand, label)))
                d = Deployment(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), environment=env_name,
                               pipeline=pref, action=action, rollback_to=prev, backfilled=True,
                               occurred_at=T(when) if when else None,
                               source=f"live.json ({b['live_json']}) and the registry manifest status",
                               gate_results={"tier": "demo", "gates": [], "target": {"type": "record", "endpoint": url,
                                                                                    "detail": "backfilled: recorded after the fact"}})
                ops._record(ctx, d, ops.current(ctx, env_name))
                prev = pref
        done["environment"] = env_name
        # imported backtests
        for label, path, baseline, note in b["readouts"]:
            name = f"demos/{brand}/{os.path.splitext(os.path.basename(path))[0].replace('_', '-').lower()}"
            try:
                ctx.registry.load("backtest-run", name)
                done["runs"].append(name)
                continue
            except NotFound:
                pass
            try:
                data, src = _read_json(path, s3, REG)
            except Exception as e:  # noqa: BLE001
                progress(f"  skipped {path}: {e}")
                continue
            done["runs"].append(_import_run(ctx, name, brand, label, baseline, data, src, note, models))
        summary[brand] = done
        progress(f"{b['label']}: {done['catalog']}, {len(done['pipelines'])} model pipelines, {env_name}, {len(done['runs'])} imported runs")
    return summary


def _once(ctx, kind, name, make):
    try:
        return ctx.registry.load(kind, name)
    except NotFound:
        return make()


def _model_pipeline(ctx, m, name, catalog_ref, b):
    pm = ops.pipeline_publish(ctx, {
        "name": name, "engine": "captain", "catalog": catalog_ref,
        "spec": {"url": f"http://localhost:{b['port']}/v1/rank" if b["port"] else f"modal://{b.get('modal_app')}",
                 "api_key_secret": "CAPTAIN_API_KEY", "architecture": m["architecture"]},
        "description": f"{b['label']} {m['version']} on {m['architecture']}: {m['status']}",
        "registry_ref": m["path"], "dvc_alias": m.get("dvc_alias"), "training_commit": m.get("training_commit"),
        "known_issues": m.get("known_issues") or [],
    }, dry_run=True)
    rs = m.get("repro_status") if m.get("repro_status") in ("exact-verified", "equivalent-verified", "not-tested") else "not-tested"
    tags = {"brand": m["brand"], "model": m["id"], "status_kind": m["status_kind"],
            "parents": ",".join(model_pipeline_name(m["brand"], p.split("@")[1]) for p in m.get("parent_ids", []))}
    from .model.entities import ReproStatus

    return ctx.registry.publish(pm.model_copy(update={"repro_status": ReproStatus(rs), "tags": tags}))


def _import_run(ctx, name, brand, label, baseline, data, src, note, models) -> str:
    model_ref = ops.ref(ctx.registry.load("pipeline", model_pipeline_name(brand, label)))
    raw = json.dumps(data, indent=1).encode()
    entry = ctx.registry.put_blob(raw, f"readout/{os.path.basename(src)}")
    comps = extract_comparisons(data)
    is_ndcg = any("ndcg" in c["metric"].lower() or "b_minus_a" in c["metric"] for c in comps)
    for c in comps:
        c.update({"a": c.get("a") if isinstance(c.get("a"), str) else baseline, "b": model_ref, "evidence": "SIMULATED",
                  "p": None, "mde": 0.0, "primary": False})
    if comps:
        pick = next((c for c in comps if "conversion" in c["metric"] or "conv" in c["metric"]), None) or \
               next((c for c in comps if "ndcg" in c["metric"] or "b_minus_a" in c["metric"]), comps[0])
        pick["primary"] = True
    m = models.get(f"{brand}@{label}", {})
    headline = "; ".join(f"[{r.get('label')}] {r.get('text')}" for r in m.get("results", []))[:900]
    bt = Backtest(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0,
                  mode="imported", arms=[baseline, model_ref], queries="(from the original readout)",
                  preregistration=Preregistration(hypothesis=f"Imported readout for {brand} {label}. Manifest results: {headline or 'none'}",
                                                  primary_metric=(pick["metric"] if comps else "n/a"), mde=0.0),
                  description=f"imported from {src}", tags={"brand": brand, "source": src, **({"note": note} if note else {})})
    bt = ctx.registry.publish(bt)
    arms = [ArmResult(arm=baseline, evidence=Evidence.SIMULATED),
            ArmResult(arm=model_ref, evidence=Evidence.SIMULATED, logs=[entry])]
    run = BacktestRun(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0,
                      backtest=ops.ref(bt), mode="imported", source=src, arms=arms, comparisons=comps, files=[entry],
                      complete=True, independent=True,
                      description=("public-log replay, not brand data. " if note else "") +
                                  f"{len(comps)} intervals from {os.path.basename(src)}; simulated shoppers" + (" (nDCG on simulated sessions)" if is_ndcg else ""))
    run = ctx.registry.publish(run)
    return ops.ref(run)
