"""Retrain a pipeline from the training command it declares, and publish the result as a new version.

The team's training code stays theirs. A pipeline declares it in its spec:
  spec:
    train:
      command: python train.py --out "$CST_TRAIN_OUT"   # any shell command
      timeout_s: 7200                                   # optional, default 3 hours
      compute: my-gpu-batch                             # optional compute target, default local

The command runs with these environment variables:
  CST_PIPELINE   the version being retrained, name@vN
  CST_WORKSPACE  the workspace name
  CST_TRAIN_OUT  a file path. The command may write YAML or JSON there with any of:
                 spec (merged into the new spec), registry_ref, training_commit, known_issues, description,
                 cost {train_usd, train_gpu_hours, gpu_type, serve_usd_per_1k}
The new version's parent is the old one. Its training cost goes into the cost ledger, and the wall time
is always recorded.

Running a shell command from a workspace file is code execution, so the local path needs an explicit
--local-exec. On a compute target the job runs the same retrain inside the runner image and publishes
the new version to the workspace bucket itself, so the call returns before training ends.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

import yaml

from . import costs as COST
from .model.entities import ReproStatus, new_id, utcnow
from .workspace import Context, whoami

ALLOWED_OUT = {"spec", "registry_ref", "training_commit", "known_issues", "description", "cost", "metrics"}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def retrain(ctx: Context, name: str, compute: str | None = None, local_exec: bool = False, dry_run: bool = False) -> dict:
    from . import compute as C
    from .ops import ref

    pm = ctx.registry.load("pipeline", name)
    train = (pm.spec or {}).get("train")
    if not isinstance(train, dict) or not train.get("command"):
        raise ValueError(f"{ref(pm)} declares no training command. Add spec.train.command (and publish a new version) "
                         "so a retrain knows what to run.")
    target = C.get(ctx, compute or train.get("compute") or "local")
    if target["kind"] != "local":
        plan, job = C.plan_and_job(ctx, target, ["pipeline", "retrain", name, "--local-exec"], label=f"retrain-{name}")
        if dry_run:
            return {"pipeline": ref(pm), "compute": target["name"], "plan": plan}
        rec = C.submit_job(ctx, target, job.argv, label=job.label, kind="retrain", job=job)
        return {"pipeline": ref(pm), "compute": target["name"], "plan": plan, "job": rec["id"],
                "submitted": {"provider_job": C.provider_job(rec["handle"]), "job": rec["id"]},
                "detail": f"the new version lands in the workspace bucket when the job finishes. Follow it with `cst job show {rec['id']}`."}
    if dry_run:
        return {"pipeline": ref(pm), "compute": "local", "would_run": train["command"]}
    if not local_exec:
        raise ValueError("retraining runs the pipeline's own shell command on this machine. Pass --local-exec to allow it, "
                         "or retrain on a compute target with --compute.")
    with tempfile.TemporaryDirectory(prefix="cst-train-") as tmp:
        out_path = os.path.join(tmp, "train-out.yaml")
        env = {**os.environ, "CST_PIPELINE": ref(pm), "CST_WORKSPACE": ctx.workspace.name, "CST_TRAIN_OUT": out_path}
        t0 = time.time()
        try:
            p = subprocess.run(["sh", "-c", str(train["command"])], env=env, capture_output=True, text=True,
                               timeout=int(train.get("timeout_s") or 10800), cwd=tmp)
        except subprocess.TimeoutExpired:
            raise ValueError(f"the training command ran past its {train.get('timeout_s') or 10800} s timeout and was stopped") from None
        wall = round(time.time() - t0, 2)
        tail = (p.stdout[-1500:] + ("\n" if p.stdout and p.stderr else "") + p.stderr[-1500:]).strip()
        if p.returncode != 0:
            raise ValueError(f"the training command failed with exit code {p.returncode}: {tail[-600:]}")
        result = {}
        if os.path.exists(out_path):
            with open(out_path) as f:
                text = f.read()
            try:
                result = json.loads(text) if text.lstrip().startswith("{") else (yaml.safe_load(text) or {})
            except (json.JSONDecodeError, yaml.YAMLError):
                raise ValueError("the training command wrote CST_TRAIN_OUT, but it is not valid YAML or JSON") from None
            unknown = set(result) - ALLOWED_OUT
            if unknown:
                raise ValueError(f"CST_TRAIN_OUT has unknown keys {sorted(unknown)}: use {', '.join(sorted(ALLOWED_OUT))}")
    new = pm.model_copy(update={
        "id": new_id(), "created_by": whoami(), "created_at": utcnow(), "version": 0, "parent": pm.version,
        "spec": _merge(pm.spec, result.get("spec") or {}),
        "registry_ref": result.get("registry_ref", pm.registry_ref),
        "training_commit": result.get("training_commit", pm.training_commit),
        "known_issues": result.get("known_issues", pm.known_issues),
        "description": result.get("description") or f"retrained from {ref(pm)} in {wall:.0f} s",
        "repro_status": ReproStatus.not_tested,
        "tags": {**pm.tags, "retrained_from": ref(pm)}})
    new = ctx.registry.publish(type(pm).model_validate(new.model_dump()))
    cost = dict(result.get("cost") or {})
    note = f"training wall time {wall:.0f} s on this machine"
    cost["note"] = (cost.get("note") + "; " if cost.get("note") else "") + note
    carry = COST.get(ctx, ref(pm))
    if carry.get("serve_usd_per_1k") is not None and "serve_usd_per_1k" not in cost:
        cost["serve_usd_per_1k"] = carry["serve_usd_per_1k"]  # serving price carries over unless training says otherwise
    COST.set_cost(ctx, ref(new), cost)
    return {"pipeline": ref(new), "name": new.name, "version": new.version, "parent": ref(pm), "compute": "local",
            "wall_s": wall, "metrics": result.get("metrics"), "log_tail": tail[-800:]}
