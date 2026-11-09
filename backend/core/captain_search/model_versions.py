"""Move model versions between Captain and a model registry, in both directions.

publish: write a pipeline version to a registry in the <architecture>/<brand>/<version>/ layout that models.py reads.
Versions there are write-once and numbered v1, v2, ... The write order keeps a half-written version invisible:
  1. <arch>/<brand>/vN/.claim, create-only. It takes the number, so two publishes at once get different numbers.
  2. The parts: weights/, code/, env/, inputs/ (refs, never data), results/, REPRODUCE.md, reproduce.sh.
     Large files go up as a multipart upload that completes create-only.
  3. SHA256SUMS, over every part and the claim.
  4. MANIFEST.yaml, create-only and last. Readers only see versions that have one (models.scan), so a publish that
     dies before this step leaves nothing anyone can pull, and its number stays used.
Then INDEX.md gets a line and the registry is imported again, so the new version shows.

import_version: the other way. A model trained outside Captain (an MLflow model version or run, or a version already
in an attached registry) becomes a new pipeline version that carries its metrics and where it came from.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import re

import yaml

from . import models as MD
from .storage.backend import AlreadyExists, Conflict, NotFound, open_store
from .storage.registry import dump_yaml
from .workspace import Context, whoami

SEG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
LARGE = 64 << 20  # files at least this big go up in parts
CHUNK = 8 << 20


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _check_seg(what: str, v: str) -> str:
    if not SEG.match(v or "") or re.fullmatch(r"v\d+", v):
        raise ValueError(f"bad {what} {v!r}: use lowercase letters, digits, . _ and -, not a version label")
    return v


def _put(st, key: str, data: bytes) -> None:
    """One part, create-only. Big ones in chunks, so no single request carries the whole file."""
    if len(data) >= LARGE:
        st.put_new_chunks(key, (data[i:i + CHUNK] for i in range(0, len(data), CHUNK)))
    else:
        st.put_new(key, data)


def _registry_profile(ctx: Context, uri: str, profile: str | None) -> str | None:
    if profile is not None:
        return profile
    from .attach import _read

    return next((r.get("profile") for r in _read(ctx)[0]["registries"] if r["uri"].rstrip("/") == uri.rstrip("/")), None)


def _claim(st, base: str, body: bytes, tries: int = 100) -> int:
    """The next free number under base, taken by a create-only .claim. A claim or a manifest both mark it used."""
    taken = set()
    for k in st.list(base):
        m = re.match(rf"^{re.escape(base)}v(\d+)/", k)
        if m:
            taken.add(int(m.group(1)))
    n = max(taken) + 1 if taken else 1
    for _ in range(tries):
        try:
            st.put_new(f"{base}v{n}/.claim", body)
            return n
        except AlreadyExists:
            n += 1
    raise Conflict(f"couldn't claim a version under {base} after {tries} tries")


def _parent_label(st, base: str, pm) -> str:
    """The registry label of the version this one was retrained from, when that one was published here too."""
    want = set(pm.parents or []) | ({f"{pm.name}@v{pm.parent}"} if pm.parent else set())
    if not want:
        return "none"
    for k in st.list(base):
        m = re.match(rf"^{re.escape(base)}(v\d+)/MANIFEST\.yaml$", k)
        if m:
            try:
                man = yaml.safe_load(st.get(k)) or {}
            except Exception:  # noqa: BLE001  an unreadable manifest just isn't a parent
                continue
            if man.get("pipeline") in want:
                return f"{m.group(1)} ({man['pipeline']})"
    return f"none in this registry (Captain parent {sorted(want)[0]})"


def _parts(ctx: Context, pm) -> dict[str, bytes]:
    from . import __version__
    from .ops import ref

    parts: dict[str, bytes] = {}
    for f in pm.files:
        data = ctx.registry.get_blob(f)
        if f.path.startswith("artifact/"):
            parts["weights/" + f.path[len("artifact/"):]] = data
        else:
            parts["code/" + f.path] = data
    parts["code/pipeline.yaml"] = dump_yaml(pm.model_dump(mode="json"))
    parts["env/env.yaml"] = dump_yaml({"captain_search": __version__, "python": platform.python_version(),
                                        "platform": platform.platform(), "engine": pm.engine})
    parts["inputs/inputs.yaml"] = dump_yaml({
        "note": "References to the data this version was trained on. The data itself stays where it is.",
        "catalog": pm.catalog, "train_inputs": pm.train_inputs, "registry_ref": pm.registry_ref})
    parts["results/metrics.yaml"] = dump_yaml({
        "kind": "offline, reported by the trainer" if pm.train_metrics else "none recorded",
        "metrics": pm.train_metrics, "params": pm.train_params})
    src = pm.source or {}
    if src.get("kind") == "retrain" and pm.parents:
        cmd = f"cst pipeline retrain {pm.parents[0]} --local-exec" + "".join(
            f" --input {i['role']}={i['ref']}" for i in pm.train_inputs) + "".join(
            f" --param {k}={json.dumps(v)!r}" for k, v in (pm.train_params or {}).items())
    else:
        cmd = f"cst pipeline show {ref(pm)}   # not trained by Captain: retrain it where it was trained ({src.get('kind') or 'unknown'})"
    parts["reproduce.sh"] = f"#!/bin/sh\n# Reproduce {ref(pm)} from the Captain workspace {ctx.workspace.name}.\nset -e\n{cmd}\n".encode()
    parts["REPRODUCE.md"] = (
        f"# Reproduce {ref(pm)}\n\nPublished from the Captain workspace `{ctx.workspace.name}`. Not yet reproduced "
        f"(repro_status: not-tested).\n\n```sh\n{cmd}\n```\n\nInputs are listed in `inputs/inputs.yaml` by exact version. "
        f"Training metrics are in `results/metrics.yaml`, as the trainer reported them.\n").encode()
    return parts


def publish(ctx: Context, pipeline: str, arch: str, brand: str, registry: str | None = None,
            profile: str | None = None, status: str = "candidate", progress=lambda f: None) -> dict:
    """Write a pipeline version to the registry as <arch>/<brand>/vN/. Returns where it went."""
    from .ops import ref

    _check_seg("architecture", arch)
    _check_seg("brand", brand)
    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    uri = (registry or MD.default_registry() or "").rstrip("/")
    if not uri:
        raise ValueError("name a model registry to publish to (--registry s3://<bucket>), or set CST_MODEL_REGISTRY")
    profile = _registry_profile(ctx, uri, profile)
    st = open_store(uri, profile)
    parts = _parts(ctx, pm)  # read everything before claiming, so a missing blob doesn't burn a number
    base = f"{arch}/{brand}/"
    claim = dump_yaml({"pipeline": ref(pm), "workspace": ctx.workspace.name, "by": whoami(), "at": _now()})
    n = _claim(st, base, claim)
    vdir = f"{base}v{n}/"
    sums = {".claim": hashlib.sha256(claim).hexdigest()}
    total = sum(len(b) for b in parts.values()) or 1
    done = 0
    for path, data in sorted(parts.items()):
        _put(st, vdir + path, data)
        sums[path] = hashlib.sha256(data).hexdigest()
        done += len(data)
        progress(0.9 * done / total)
    _put(st, vdir + "SHA256SUMS", "".join(f"{h}  {p}\n" for p, h in sorted(sums.items())).encode())
    manifest = {
        "status": status, "repro_status": "not-tested", "parent": _parent_label(st, base, pm),
        "pipeline": ref(pm), "workspace": ctx.workspace.name, "published_by": whoami(), "published_at": _now(),
        "training_commit": pm.training_commit, "registry_ref": pm.registry_ref, "attached_to_demo": False,
        "what_changed": pm.description, "known_issues": pm.known_issues,
        "results": [{"metric": k, "value": v, "kind": "offline, reported by the trainer"}
                    for k, v in (pm.train_metrics or {}).items()],
        "inputs": [i.get("ref") for i in pm.train_inputs] + ([pm.catalog] if pm.catalog else []),
    }
    _put(st, vdir + "MANIFEST.yaml", dump_yaml(manifest))
    progress(0.95)
    warnings = []
    try:
        _index_line(st, f"| {brand} | {arch} | v{n} | {status} | not-tested | {ref(pm)} | {_now()[:10]} |")
    except Exception as e:  # noqa: BLE001  the version is complete without it
        warnings.append(f"INDEX.md wasn't updated: {e}")
    snap = MD.import_registry(ctx, uri, profile)
    progress(1.0)
    return {"model": f"{brand}@v{n}", "architecture": arch, "brand": brand, "version": f"v{n}", "pipeline": ref(pm),
            "path": f"{uri}/{vdir}", "files": len(sums) + 2, "bytes": total, "repro_status": "not-tested",
            "snapshot": f"{snap.name}@v{snap.version}", "warnings": warnings}


def _index_line(st, line: str) -> None:
    head = "# Model registry\n\n| brand | architecture | version | status | repro | from | published |\n|---|---|---|---|---|---|---|\n"
    for _ in range(10):
        try:
            body, tag = st.get_with_tag("INDEX.md")
            text = body.decode()
        except NotFound:
            text, tag = head, None
        try:
            st.put_if("INDEX.md", (text.rstrip("\n") + "\n" + line + "\n").encode(), tag)
            return
        except Conflict:
            continue
    raise Conflict("INDEX.md kept changing")


# ---- import a model trained elsewhere ---------------------------------------------------------------------------


def _mlflow_run(ctx: Context, run_id: str) -> dict:
    from . import mlflow_bridge as MB

    run = MB._call(ctx, "GET", "runs/get", {"run_id": run_id}).get("run") or {}
    info, data = run.get("info", {}), run.get("data", {})
    tags = {t["key"]: t["value"] for t in data.get("tags", [])}
    return {"run_id": run_id, "metrics": {m["key"]: m["value"] for m in data.get("metrics", [])},
            "params": {p["key"]: p["value"] for p in data.get("params", [])},
            "commit": tags.get("mlflow.source.git.commit"), "artifacts": info.get("artifact_uri"),
            "name": info.get("run_name")}


def _from_mlflow(ctx: Context, model: str | None, run_id: str | None) -> dict:
    from . import mlflow_bridge as MB

    src: dict = {"kind": "mlflow", "tracking": MB.status(ctx).get("uri") or MB.status(ctx).get("databricks_connection")}
    location = None
    if model:
        name, sep, ver = model.rpartition("@")
        if not sep or not name or not ver:
            raise ValueError(f"an MLflow model version looks like name@version, got {model!r}")
        if MB._is_databricks(ctx) and name.count(".") == 2:  # catalog.schema.model: Unity Catalog
            mv = MB._uc(ctx, f"models/{name}/versions/{ver}")
            location = mv.get("storage_location") or mv.get("source")
        else:
            mv = MB._call(ctx, "GET", "model-versions/get", {"name": name, "version": ver}).get("model_version") or {}
            location = mv.get("source")
        run_id = run_id or mv.get("run_id")
        src.update(model=name, version=str(ver))
    run = _mlflow_run(ctx, run_id) if run_id else {"metrics": {}, "params": {}}
    src.update(run_id=run_id, artifacts=location or run.get("artifacts"))
    parents = ([f"mlflow-model:{src['model']}@{src['version']}"] if model else []) + ([f"mlflow-run:{run_id}"] if run_id else [])
    return {"source": src, "metrics": run["metrics"], "params": run["params"], "commit": run.get("commit"),
            "registry_ref": src["artifacts"], "parents": parents,
            "label": f"MLflow {src.get('model', 'run')}" + (f" v{src['version']}" if model else f" {run_id}")}


def _numbers(results) -> dict:
    """A registry manifest's results, as {metric: number} where they say so."""
    out = {}
    if isinstance(results, dict):
        return {k: v for k, v in results.items() if isinstance(v, (int, float))}
    for r in results or []:
        if isinstance(r, dict):
            k, v = r.get("metric") or r.get("name"), r.get("value")
            if k and isinstance(v, (int, float)):
                out[str(k)] = v
    return out


def _from_registry(ctx: Context, path: str) -> dict:
    bits = path.strip("/").split("/")
    if len(bits) != 3:
        raise ValueError(f"a registry path looks like <architecture>/<brand>/<version>, got {path!r}")
    arch, brand, ver = bits
    hit = next(((m, s) for s in MD.all_latest(ctx) for m in s.models
                if (m["architecture"], m["brand"], m["version"]) == (arch, brand, ver)), None)
    if not hit:
        raise NotFound(f"no {path} in any imported registry. Run `cst models import --uri <registry>` first.")
    m, snap = hit
    man = m.get("manifest") or {}
    metrics = {**_numbers(man.get("results")), **_numbers(man.get("metrics"))}
    return {"source": {"kind": "registry", "registry": snap.uri, "architecture": arch, "brand": brand, "version": ver,
                       "snapshot": f"{snap.name}@v{snap.version}", "repro_status": m["repro_status"]},
            "metrics": metrics, "params": man.get("params") if isinstance(man.get("params"), dict) else {},
            "commit": m.get("training_commit"), "registry_ref": m["path"],
            "parents": [f"registry:{m['id']}"] + [f"registry:{p}" for p in m.get("parent_ids", [])],
            "label": f"registry {path}"}


def import_version(ctx: Context, pipeline: str, mlflow_model: str | None = None, mlflow_run: str | None = None,
                   registry_path: str | None = None, base: str | None = None, description: str | None = None):
    """Register a model trained elsewhere as a new version of `pipeline`. Its engine and spec come from `base` (a
    pipeline ref), else from the pipeline's latest version: Captain needs to know how to query it."""
    from .model.entities import ReproStatus, new_id, utcnow
    from .ops import ref

    if sum(bool(x) for x in (mlflow_model or mlflow_run, registry_path)) != 1:
        raise ValueError("name one source: an MLflow model version (--mlflow name@version) or run (--mlflow-run), "
                         "or a registry path (--registry-path <architecture>/<brand>/<version>)")
    from .storage.registry import check_name

    check_name(pipeline)
    if base:
        bm, _ = ctx.registry.resolve("pipeline", base)
    elif ctx.registry.versions("pipeline", pipeline):
        bm = ctx.registry.load("pipeline", pipeline)
    else:
        raise ValueError(f"there is no pipeline {pipeline!r} yet. Name one to copy the engine and spec from with --from, "
                         "so Captain knows how to query the imported model.")
    got = _from_registry(ctx, registry_path) if registry_path else _from_mlflow(ctx, mlflow_model, mlflow_run)
    same = bm.name == pipeline
    new = bm.model_copy(update={
        "id": new_id(), "created_by": whoami(), "created_at": utcnow(), "name": pipeline, "version": 0,
        "parent": bm.version if same else None,
        "files": [f for f in bm.files if not f.path.startswith("artifact/")],
        "registry_ref": got["registry_ref"] or bm.registry_ref, "training_commit": got["commit"] or None,
        "repro_status": ReproStatus.not_tested, "train_inputs": [], "train_metrics": got["metrics"],
        "train_params": got["params"], "parents": got["parents"] + ([] if same else [ref(bm)]),
        "source": got["source"], "description": description or f"imported from {got['label']}",
        "tags": {**bm.tags, "imported_from": got["source"]["kind"]}})
    return ctx.registry.publish(type(bm).model_validate(new.model_dump()))
