"""Retrain a pipeline from the training command it declares, and publish the result as a new version.

The team's training code stays theirs. A pipeline declares it in its spec:
  spec:
    train:
      command: python train.py                          # any shell command
      inputs: [shop-catalog@v3, {ref: shop-judgments@v2, role: labels, as_of: "2026-10-01"}]   # optional
      params: {learning_rate: 0.05}                     # optional
      timeout_s: 7200                                   # optional, default 3 hours
      compute: my-gpu-batch                             # optional compute target, default local

The full contract is docs/trainer-contract.md. In short, the command runs with:
  CST_TRAIN_INPUTS  JSON list of the exact inputs: {ref, kind, role, uri, files, label_source, as_of}
  CST_PARAMS        JSON object of params (spec.train.params, then --param on the command line)
  CST_OUT           an empty folder. The command writes CST_OUT/result.yaml with any of:
                    metrics, params, artifact_dir, registry_ref, spec (merged into the new spec), training_commit,
                    known_issues, description, cost {train_usd, train_gpu_hours, gpu_type, serve_usd_per_1k}
  CST_PIPELINE, CST_WORKSPACE, and CST_TRAIN_OUT (a file path, the older way to write the same result; still read)
The new version's parent is the old one. It records the inputs, params and metrics. Files under artifact_dir are
stored with the version under artifact/. Its training cost goes into the cost ledger, and the wall time is always
recorded.

Running a shell command from a workspace file is code execution, so the local path needs an explicit
--local-exec. On a compute target the job runs the same retrain inside the runner image and publishes
the new version to the workspace bucket itself when training ends. The call returns at once with
{pending: true, job, live_id} and no pipeline ref, because no new version exists yet. `wait=True` (the CLI's
--wait) polls the job and returns the new version once the job has recorded it.
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

ALLOWED_OUT = {"spec", "registry_ref", "training_commit", "known_issues", "description", "cost", "metrics",
               "params", "artifact_dir"}
LABEL_SOURCE_VALUES = ("human", "llm", "clicks", "simulated")
# judgment_source on a judgments dataset, in the words of the contract
LABEL_SOURCES = {"human": "human", "llm": "llm", "click": "clicks", "clicks": "clicks", "heuristic": "simulated",
                 "simulated": "simulated"}
POLL_S = 10.0  # how often --wait asks the compute target for the job's state
ARTIFACT_LIMIT = 2 << 30  # artifact_dir larger than this is refused: keep big weights in the team's own storage


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _parse_input(x) -> dict:
    """`ref`, `role=ref`, or a dict {ref, role?, label_source?, as_of?}."""
    if isinstance(x, dict):
        if not x.get("ref"):
            raise ValueError(f"a training input needs a ref: {x}")
        return dict(x)
    s = str(x).strip()
    role, sep, ref = s.partition("=")
    return {"ref": ref.strip(), "role": role.strip()} if sep else {"ref": s}


def resolve_inputs(ctx: Context, items, as_of: str | None = None) -> list[dict]:
    """Pin each input to an exact version and say where its parts are, so the trainer reads exactly what was asked."""
    from .model.entities import DatasetKind

    root = ctx.store.uri.rstrip("/")
    out = []
    for it in items or []:
        d = _parse_input(it)
        dv, split = ctx.registry.resolve("dataset", d["ref"])
        exact = f"{dv.name}@v{dv.version}" + (f":{split}" if split else "")
        files = [{"path": f.path, "uri": f"{root}/{ctx.registry.blob_key(f.sha256)}", "bytes": f.bytes} for f in dv.files]
        files += [{"path": None, "uri": e.uri, "bytes": e.bytes} for e in dv.external]
        label = d.get("label_source") or dv.tags.get("label_source")
        if label is None and dv.dataset_kind == DatasetKind.judgments:
            label = LABEL_SOURCES.get(dv.judgment_source or "")
        if label is not None and label not in LABEL_SOURCE_VALUES:
            raise ValueError(f"label_source for {exact} is {label!r}: use {', '.join(LABEL_SOURCE_VALUES)}")
        out.append({"ref": exact, "kind": dv.dataset_kind.value, "role": d.get("role") or dv.dataset_kind.value,
                    "uri": f"{root}/{ctx.registry.version_dir('dataset', dv.name, dv.version)}", "files": files,
                    "label_source": label, "as_of": str(d.get("as_of") or dv.tags.get("as_of") or as_of or "") or None})
    return out


def _params(train: dict, params: dict | None) -> dict:
    base = train.get("params") or {}
    if not isinstance(base, dict):
        raise ValueError("spec.train.params must be a mapping of name to value")
    return {**base, **(params or {})}


def retrain(ctx: Context, name: str, compute: str | None = None, local_exec: bool = False, dry_run: bool = False,
            live=None, live_id: str | None = None, inputs: list | None = None, params: dict | None = None,
            as_of: str | None = None, wait: bool = False, wait_timeout_s: float | None = None) -> dict:
    """Retrain, streaming to Runs: `live` is a run to write to here; without one, a run is made (in a job's container,
    the one the app made for it). On a compute target the job streams through the bucket to `live_id`, made here
    when not given. `name` is a pipeline name (its latest version) or name@vN. `inputs` (refs) replace
    spec.train.inputs when given, `params` add to spec.train.params."""
    from . import compute as C
    from . import live as LV
    from .ops import ref

    pm, _ = ctx.registry.resolve("pipeline", name)
    train = (pm.spec or {}).get("train")
    if not isinstance(train, dict) or not train.get("command"):
        raise ValueError(f"{ref(pm)} declares no training command. Add spec.train.command (and publish a new version) "
                         "so a retrain knows what to run.")
    chosen = list(inputs) if inputs else list(train.get("inputs") or [])
    resolved = resolve_inputs(ctx, chosen, as_of)  # fail now on a missing input, not inside a job
    prm = _params(train, params)
    in_job = bool(os.environ.get("CST_JOB_ID"))  # inside a submitted job: train here, never submit again
    target = C.get(ctx, "local" if in_job else compute or train.get("compute") or "local")
    if target["kind"] != "local":
        # The job trains the same version on the same exact inputs this call resolved, even if newer ones land meanwhile.
        args = ["pipeline", "retrain", ref(pm), "--local-exec"]
        for r in resolved:
            args += ["--input", f"{r['role']}={r['ref']}"]
        for k, v in (params or {}).items():
            args += ["--param", f"{k}={json.dumps(v)}"]
        if as_of:
            args += ["--as-of", as_of]
        plan, job = C.plan_and_job(ctx, target, args, label=f"retrain-{pm.name}")
        if dry_run:
            return {"pending": False, "parent": ref(pm), "compute": target["name"], "plan": plan, "inputs": resolved}
        live_id = live_id or LV.start_remote(ctx, "training", f"Retrain {pm.name}", of=ref(pm), where=target["name"])
        job.env["CST_LIVE_ID"] = live_id
        plan = C.P.get(target, ctx).plan(job)
        try:
            rec = C.submit_job(ctx, target, job.argv, label=job.label, kind="retrain", job=job)
        except Exception as e:
            LV.note_remote(ctx, live_id, status="failed", error=f"Couldn't submit the job: {e}")
            raise
        LV.note_remote(ctx, live_id, job=rec["id"])
        if wait:
            return {**wait_job(ctx, rec["id"], wait_timeout_s), "live_id": live_id}
        # No `pipeline` here: the new version doesn't exist until the job finishes, and the old one is not the result.
        return {"pending": True, "job": rec["id"], "live_id": live_id, "parent": ref(pm), "compute": target["name"],
                "plan": plan, "inputs": resolved,
                "submitted": {"provider_job": C.provider_job(rec["handle"]), "job": rec["id"]},
                "detail": f"the new version lands in the workspace bucket when the job finishes. Follow it with "
                          f"`cst job show {rec['id']}`."}
    if dry_run:
        return {"parent": ref(pm), "compute": "local", "would_run": train["command"], "inputs": resolved, "params": prm}
    if not local_exec:
        raise ValueError("retraining runs the pipeline's own shell command on this machine. Pass --local-exec to allow it, "
                         "or retrain on a compute target with --compute.")
    own = live is None
    if own:
        live = LV.for_job(ctx, "training", f"Retrain {pm.name}", of=ref(pm))
    try:
        res = _train_here(ctx, pm, train, live, own, resolved, prm)
    except BaseException as e:
        if own:
            live.end("failed", error=str(e) or "stopped")
        raise
    if in_job:
        from . import jobs as J

        J.save_result(ctx, os.environ["CST_JOB_ID"],
                      {k: res[k] for k in ("pipeline", "name", "version", "parent", "wall_s", "metrics")})
    return res


def wait_job(ctx: Context, job_id: str, timeout_s: float | None = None) -> dict:
    """Poll a retrain job until it finishes and return the version it recorded. Fails if the job failed, or finished
    without recording one: the caller never gets the old version back in place of the new one."""
    from . import compute_providers as P
    from . import jobs as J

    t0 = time.time()
    while True:
        r = J.show(ctx, job_id)
        if r["state"] in P.TERMINAL:
            break
        if timeout_s is not None and time.time() - t0 > timeout_s:
            raise TimeoutError(f"job {job_id} is still {r['state']} after {timeout_s:.0f} s. "
                               f"Follow it with `cst job show {job_id} --wait`.")
        time.sleep(POLL_S)
    if r["state"] != "succeeded":
        raise ValueError(f"the retrain job {job_id} {r['state']}: {r.get('reason') or 'no reason given'}")
    res = r.get("result")
    if not res or not res.get("pipeline"):
        raise ValueError(f"the retrain job {job_id} finished but recorded no new version. Check `cst job logs {job_id}`.")
    return {"pending": False, "job": job_id, "compute": r.get("target"), **res}


def _read_result(path: str) -> dict:
    with open(path) as f:
        text = f.read()
    try:
        out = json.loads(text) if text.lstrip().startswith("{") else (yaml.safe_load(text) or {})
    except (json.JSONDecodeError, yaml.YAMLError):
        raise ValueError(f"the training command wrote {os.path.basename(path)}, but it is not valid YAML or JSON") from None
    if not isinstance(out, dict):
        raise ValueError(f"{os.path.basename(path)} must hold a mapping, e.g. `metrics: {{ndcg@10: 0.41}}`")
    unknown = set(out) - ALLOWED_OUT
    if unknown:
        raise ValueError(f"{os.path.basename(path)} has unknown keys {sorted(unknown)}: use {', '.join(sorted(ALLOWED_OUT))}")
    return out


def _artifact_files(ctx: Context, out_dir: str, artifact_dir: str) -> list:
    base = os.path.realpath(artifact_dir if os.path.isabs(artifact_dir) else os.path.join(out_dir, artifact_dir))
    if not os.path.isdir(base):
        raise ValueError(f"artifact_dir {artifact_dir!r} is not a folder")
    paths = [os.path.join(d, f) for d, _, fs in os.walk(base) for f in fs]
    total = sum(os.path.getsize(p) for p in paths)
    if total > ARTIFACT_LIMIT:
        raise ValueError(f"artifact_dir holds {total / 1e9:.1f} GB, over the {ARTIFACT_LIMIT / 1e9:.1f} GB Captain stores "
                         "with a version. Keep the weights in your own storage and write registry_ref instead.")
    out = []
    for p in sorted(paths):
        with open(p, "rb") as f:
            out.append(ctx.registry.put_blob(f.read(), "artifact/" + os.path.relpath(p, base).replace(os.sep, "/")))
    return out


def _train_here(ctx: Context, pm, train: dict, live, own: bool, inputs: list[dict], params: dict) -> dict:
    """Run the training command on this machine, its output streaming to the live run, and publish the result."""
    import threading

    from . import live as LV
    from .ops import ref

    timeout = int(train.get("timeout_s") or 10800)
    live.stage(f"Train {pm.name}", 1, 2)
    with tempfile.TemporaryDirectory(prefix="cst-train-") as tmp:
        out_path = os.path.join(tmp, "train-out.yaml")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir)
        env = {**os.environ, "CST_PIPELINE": ref(pm), "CST_WORKSPACE": ctx.workspace.name, "CST_TRAIN_OUT": out_path,
               "CST_TRAIN_INPUTS": json.dumps(inputs), "CST_PARAMS": json.dumps(params), "CST_OUT": out_dir,
               "PYTHONUNBUFFERED": "1"}  # so a Python trainer's lines arrive as it prints them
        t0 = time.time()
        p = subprocess.Popen(["sh", "-c", str(train["command"])], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, cwd=tmp)
        timed_out = threading.Event()
        killer = threading.Timer(timeout, lambda: (timed_out.set(), p.kill()))
        killer.start()
        lines: list[str] = []
        try:
            assert p.stdout is not None
            for line in p.stdout:
                lines.append(line)
                del lines[:-200]
                LV.feed(live, line)
            code = p.wait()
        finally:
            killer.cancel()
        if timed_out.is_set():
            raise ValueError(f"the training command ran past its {timeout} s timeout and was stopped")
        wall = round(time.time() - t0, 2)
        tail = "".join(lines).strip()[-3000:]
        if code != 0:
            raise ValueError(f"the training command failed with exit code {code}: {tail[-600:]}")
        result: dict = {}
        if os.path.exists(out_path):  # the older CST_TRAIN_OUT file
            result = _read_result(out_path)
        new_path = os.path.join(out_dir, "result.yaml")
        if os.path.exists(new_path):
            result = _merge(result, _read_result(new_path))
        art = _artifact_files(ctx, out_dir, str(result["artifact_dir"])) if result.get("artifact_dir") else None
    live.stage("Publish the new version", 2, 2)
    metrics = result.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("result metrics must be a mapping of name to number")
    for k, v in metrics.items():
        if isinstance(v, (int, float)):
            live.metric(str(k), v)
    # A new artifact replaces the old one. Other files the pipeline carries come along as before.
    files = pm.files if art is None else [f for f in pm.files if not f.path.startswith("artifact/")] + art
    new = pm.model_copy(update={
        "id": new_id(), "created_by": whoami(), "created_at": utcnow(), "version": 0, "parent": pm.version,
        "spec": _merge(pm.spec, result.get("spec") or {}),
        "registry_ref": result.get("registry_ref", pm.registry_ref),
        "training_commit": result.get("training_commit", pm.training_commit),
        "known_issues": result.get("known_issues", pm.known_issues),
        "description": result.get("description") or f"retrained from {ref(pm)} in {wall:.0f} s",
        "repro_status": ReproStatus.not_tested, "files": files,
        "train_inputs": [{k: v for k, v in i.items() if k != "files"} for i in inputs],
        "train_metrics": metrics, "train_params": {**params, **(result.get("params") or {})},
        "parents": [ref(pm)], "source": {"kind": "retrain", "command": str(train["command"])[:2000],
                                         **({"job": os.environ["CST_JOB_ID"]} if os.environ.get("CST_JOB_ID") else {})},
        "tags": {**pm.tags, "retrained_from": ref(pm)}})
    new = ctx.registry.publish(type(pm).model_validate(new.model_dump()))
    cost = dict(result.get("cost") or {})
    note = f"training wall time {wall:.0f} s on this machine"
    cost["note"] = (cost.get("note") + "; " if cost.get("note") else "") + note
    carry = COST.get(ctx, ref(pm))
    if carry.get("serve_usd_per_1k") is not None and "serve_usd_per_1k" not in cost:
        cost["serve_usd_per_1k"] = carry["serve_usd_per_1k"]  # serving price carries over unless training says otherwise
    COST.set_cost(ctx, ref(new), cost)
    if own:
        live.end("done", ref=ref(new), summary=f"Published {ref(new)} from {ref(pm)} in {wall:.0f} s")
    return {"pipeline": ref(new), "name": new.name, "version": new.version, "parent": ref(pm), "compute": "local",
            "wall_s": wall, "metrics": metrics or None, "params": new.train_params, "inputs": new.train_inputs,
            "artifact_files": len(art or []), "log_tail": tail[-800:]}
