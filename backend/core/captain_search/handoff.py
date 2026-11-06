"""Handoff: Captain prepares a candidate. The customer's own deployment process puts it in production.

Spec (2026-11-06): "Captain is outside the live production search request path." Registration, training, agent
completion and offline evaluation never promote a model, and production promotion is "a distinct, attributable
action in the customer's deployment system". So for a live environment Captain:
  1. runs the readiness checks (the old promote gates) and records them,
  2. writes a write-once candidate bundle: pipeline spec and lineage, evaluation evidence with its labels,
     cost, measured latency, known limitations, serving requirements and reproducibility status,
  3. stops. Nothing is called, nothing is served, and the environment's "current" pointer does not move.
When the customer deploys it, `link` records their deployment id, and `outcome` attaches online results.
Only a link moves "current", because only the customer's system knows what is live.

  handoffs/<id>.yaml              the bundle (write-once)
  handoffs/<id>/links.yaml        external deployments and online outcomes (append-only, compare-and-swap)
"""
from __future__ import annotations

import datetime as dt

import yaml

from .model.entities import Evidence, new_id
from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def bundle(ctx: Context, pipeline_ref: str, env_name: str, gates: list[dict], override_reason: str | None) -> dict:
    from . import costs as COST
    from .ops import _latest_run_for, env_load, ref

    pm, _ = ctx.registry.resolve("pipeline", pipeline_ref)
    env = env_load(ctx, env_name)
    run = _latest_run_for(ctx, ref(pm))
    evidence = None
    if run is not None:
        arm = next(a for a in run.arms if a.arm == ref(pm))
        bt = ctx.registry.load("backtest", run.name)
        prim = next((c for c in run.comparisons if c["b"] == ref(pm) and c.get("primary")), None)
        evidence = {"run": f"{run.name}@v{run.version}", "evidence_label": arm.evidence.value,
                    "simulated_label": arm.simulated_evidence.value, "primary": prim, "queries": bt.queries,
                    "judgments": bt.judgments, "complete": run.complete, "independent": run.independent,
                    "segments_failing": [s for s in run.segments if s.get("verdict") == "loss"],
                    "surface_guardrails": [g for sf in run.surfaces for g in sf.get("guardrails", []) if g["arm"] == ref(pm)]}
    cost = COST.get(ctx, ref(pm))
    limitations = list(pm.known_issues)
    if evidence is None:
        limitations.append("no backtest has this version as a challenger")
    elif Evidence(evidence["evidence_label"]).rank < Evidence.REAL_TRAFFIC.rank:
        limitations.append(f"offline evidence only ({evidence['evidence_label']}): not proof of online or profit lift")
    if override_reason:
        limitations.append(f"readiness checks overridden: {override_reason}")
    return {
        "id": new_id().lower(), "pipeline": ref(pm), "environment": env_name, "tier": env.tier.value,
        "created_by": whoami(), "created_at": _now(), "status": "handed-off",
        "candidate": {"engine": pm.engine, "spec": pm.spec, "catalog": pm.catalog, "registry_ref": pm.registry_ref,
                      "dvc_alias": pm.dvc_alias, "training_commit": pm.training_commit, "repro_status": pm.repro_status.value,
                      "parent": pm.parent, "files": [f.model_dump() for f in pm.files]},
        "readiness": {"gates": gates, "overridden": [g["gate"] for g in gates if not g["ok"]] if override_reason else [],
                      "override_reason": override_reason},
        "evidence": evidence, "cost": {k: cost.get(k) for k in ("train_usd", "train_gpu_hours", "gpu_type", "serve_usd_per_1k")},
        "serving_requirements": {"p99_ms_budget": env.gates.get("p99_ms"), "target": env.target},
        "limitations": limitations,
        "next": "Deploy it through your own deployment process, then record it with `cst handoff link`.",
    }


def write(ctx: Context, b: dict) -> dict:
    ctx.store.put_new(f"handoffs/{b['id']}.yaml", dump_yaml(b))
    return b


def get(ctx: Context, hid: str) -> dict:
    try:
        b = yaml.safe_load(ctx.store.get(f"handoffs/{hid}.yaml"))
    except NotFound:
        raise NotFound(f"no handoff {hid}") from None
    b["links"] = _links(ctx, hid)[0].get("events", [])
    return b


def listing(ctx: Context, pipeline: str | None = None) -> list[dict]:
    out = []
    for k in ctx.store.list("handoffs/"):
        if k.count("/") == 1 and k.endswith(".yaml"):
            b = yaml.safe_load(ctx.store.get(k))
            if pipeline is None or b["pipeline"].startswith(pipeline):
                b["links"] = _links(ctx, b["id"])[0].get("events", [])
                out.append(b)
    return sorted(out, key=lambda b: b["created_at"], reverse=True)


def _links(ctx: Context, hid: str):
    try:
        data, tag = ctx.store.get_with_tag(f"handoffs/{hid}/links.yaml")
        return yaml.safe_load(data) or {"events": []}, tag
    except NotFound:
        return {"events": []}, None


def _append(ctx: Context, hid: str, event: dict) -> dict:
    get(ctx, hid)  # must exist
    doc, tag = _links(ctx, hid)
    doc["events"].append({**event, "at": _now(), "by": whoami()})
    try:
        ctx.store.put_if(f"handoffs/{hid}/links.yaml", dump_yaml(doc), tag)
    except Conflict:
        raise Conflict("someone recorded on this handoff at the same moment. Try again.") from None
    return get(ctx, hid)


def link(ctx: Context, hid: str, system: str, deployment_id: str, url: str | None = None, live: bool = True) -> dict:
    """Record that the customer's system deployed this candidate. Moves the environment's current pointer."""
    from .ops import current_key

    if not system.strip() or not deployment_id.strip():
        raise ValueError("a link needs the deployment system (e.g. argo, spinnaker, sagemaker) and its deployment id")
    b = get(ctx, hid)
    out = _append(ctx, hid, {"kind": "deployment", "system": system, "deployment_id": deployment_id, "url": url, "live": live})
    if live:
        try:
            _, tag = ctx.store.get_with_tag(current_key(b["environment"]))
        except NotFound:
            tag = None
        ctx.store.put_if(current_key(b["environment"]), dump_yaml({"pipeline": b["pipeline"], "handoff": hid,
                                                                    "deployment_id": deployment_id, "system": system}), tag)
    return out


def outcome(ctx: Context, hid: str, metric: str, value: float, source: str, experiment: str | None = None,
            ci_low: float | None = None, ci_high: float | None = None) -> dict:
    """Attach an online result from the customer's experiment system. These are REAL_TRAFFIC numbers."""
    if not source.strip():
        raise ValueError("say where the number comes from (the experiment system and its report)")
    return _append(ctx, hid, {"kind": "outcome", "metric": metric, "value": float(value), "ci_low": ci_low, "ci_high": ci_high,
                              "source": source, "experiment": experiment, "evidence": Evidence.REAL_TRAFFIC.value})
