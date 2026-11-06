"""Import a model registry (s3://<bucket> in the <architecture>/<brand>/<version>/ layout) and build its lineage.

The registry is read, never written: INDEX.md, every <architecture>/<brand>/<version>/MANIFEST.yaml
and the file listing. The result is a write-once RegistrySnapshot in the workspace; re-importing
makes a new snapshot version.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import yaml

from .model.entities import RegistrySnapshot
from .workspace import Context, whoami



def default_registry() -> str:
    """The registry to import when none is named: $CST_MODEL_REGISTRY, else the one in the demos file."""
    import os

    if os.environ.get("CST_MODEL_REGISTRY"):
        return os.environ["CST_MODEL_REGISTRY"]
    try:
        from .brands import load_demos

        return load_demos()[0]
    except (FileNotFoundError, OSError):
        return ""


def status_kind(status: str) -> str:
    s = (status or "").lower()
    for k, v in (("current", "current"), ("candidate", "candidate"), ("rolled back", "rolled-back"),
                 ("rolled-back", "rolled-back"), ("unattached", "unattached")):
        if s.startswith(k):
            return v
    return "other"


def parse_parents(brand: str, text: str | None) -> list[str]:
    """'acme v3 (rankers copied unchanged) and v2 (event model)' -> ['v3', 'v2'].
    'none, first Acme model' -> []."""
    t = (text or "").strip().lower()
    if not t or t.startswith("none"):
        return []
    t = re.sub(r"\([^)]*\)", " ", t)
    labels = re.findall(r"\b((?:recipe-)?v\d+[a-z0-9-]*)\b", t)
    out = []
    for l in labels:
        if l not in out:
            out.append(l)
    return out


def _s3(profile):
    from .cloud import aws_session

    return aws_session(profile, "us-west-1").client("s3")


def scan(uri: str, profile: str | None = None, s3=None) -> dict:
    u = urlparse(uri)
    if u.scheme != "s3":
        raise ValueError("registry import reads an s3:// registry")
    s3 = s3 or _s3(profile)
    bucket, prefix = u.netloc, u.path.strip("/")
    pfx = f"{prefix}/" if prefix else ""
    keys: dict[str, int] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=pfx):
        for o in page.get("Contents", []):
            keys[o["Key"][len(pfx):]] = o["Size"]
    try:
        index_md = s3.get_object(Bucket=bucket, Key=pfx + "INDEX.md")["Body"].read().decode()
    except Exception:
        index_md = ""
    models = []
    for k in sorted(keys):
        m = re.match(r"^([^/]+)/([^/]+)/([^/]+)/MANIFEST\.yaml$", k)
        if not m:
            continue
        arch, brand, label = m.groups()
        man = yaml.safe_load(s3.get_object(Bucket=bucket, Key=pfx + k)["Body"].read()) or {}
        base = f"{arch}/{brand}/{label}/"
        files = [{"path": f[len(base):], "bytes": b} for f, b in keys.items() if f.startswith(base)]
        models.append({
            "id": f"{brand}@{label}", "brand": brand, "architecture": arch, "version": label,
            "status": str(man.get("status", "")), "status_kind": status_kind(str(man.get("status", ""))),
            "repro_status": str(man.get("repro_status", "not-tested")), "parent_label": man.get("parent"),
            "parents": parse_parents(brand, man.get("parent")), "training_commit": man.get("training_commit"),
            "dvc_alias": man.get("dvc_alias"), "attached_to_demo": bool(man.get("attached_to_demo")),
            "results": man.get("results") or [], "known_issues": man.get("known_issues") or [],
            "what_changed": man.get("what_changed"), "path": f"s3://{bucket}/{pfx}{base}",
            "files": sorted(files, key=lambda f: f["path"]), "bytes": sum(f["bytes"] for f in files), "manifest": man,
        })
    archs = sorted({m["architecture"] for m in models})
    arch_docs = {}
    for a in archs:
        if f"{a}/ARCHITECTURE.md" in keys:
            arch_docs[a] = s3.get_object(Bucket=bucket, Key=f"{pfx}{a}/ARCHITECTURE.md")["Body"].read().decode()[:4000]
    return {"index_md": index_md, "models": models, "architectures": archs, "architecture_docs": arch_docs}


def import_registry(ctx: Context, uri: str | None = None, profile: str | None = None, s3=None) -> RegistrySnapshot:
    uri = uri or default_registry()
    if not uri:
        raise ValueError("name a model registry (s3://<bucket>), or set CST_MODEL_REGISTRY")
    data = scan(uri, profile, s3)
    if not data["models"]:
        raise ValueError(f"no <architecture>/<brand>/<version>/MANIFEST.yaml found under {uri}")
    for m in data["models"]:
        ids = {x["id"] for x in data["models"]}
        m["parent_ids"] = [f"{m['brand']}@{p}" for p in m["parents"]]
        m["parent_id"] = m["parent_ids"][0] if m["parent_ids"] else None
        m["parent_registered"] = [pid in ids for pid in m["parent_ids"]]
    u = urlparse(uri)
    name = re.sub(r"[^a-z0-9._-]+", "-", f"{u.netloc}-{u.path.strip('/')}".strip("-").lower())[:100].strip("-.")
    snap = RegistrySnapshot(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name,
                            version=0, uri=uri, index_md=data["index_md"], models=data["models"],
                            tags={"architectures": ",".join(data["architectures"])},
                            description=f"{len(data['models'])} models across {len(data['architectures'])} architectures")
    return ctx.registry.publish(snap)


def latest(ctx: Context) -> RegistrySnapshot | None:
    names = ctx.registry.names("registry")
    snaps = [ctx.registry.load("registry", n) for n in names]
    return max(snaps, key=lambda s: s.created_at) if snaps else None


def latest_for(ctx: Context, uri: str) -> RegistrySnapshot | None:
    snaps = [s for s in (ctx.registry.load("registry", n) for n in ctx.registry.names("registry")) if s.uri.rstrip("/") == uri.rstrip("/")]
    return max(snaps, key=lambda s: s.created_at) if snaps else None


def all_latest(ctx: Context) -> list[RegistrySnapshot]:
    """The newest snapshot of every attached registry (or of every registry ever read, if none are attached)."""
    from .attach import _read

    uris = [r["uri"] for r in _read(ctx)[0]["registries"]]
    if not uris:
        snaps = [ctx.registry.load("registry", n) for n in ctx.registry.names("registry")]
        uris = sorted({s.uri for s in snaps})
    return [s for s in (latest_for(ctx, u) for u in uris) if s]


def listing(ctx: Context) -> list[dict]:
    out = []
    for s in all_latest(ctx):
        out += [{**{k: v for k, v in m.items() if k not in ("manifest", "files")}, "registry": s.uri} for m in s.models]
    return out


def detail(ctx: Context, model_id: str) -> dict:
    m = next((m for s in all_latest(ctx) for m in s.models if m["id"] == model_id), None)
    if m is None:
        from .storage.backend import NotFound

        raise NotFound(f"no model {model_id!r} in any attached registry")
    return m


def lineage(ctx: Context) -> dict:
    snaps = all_latest(ctx)
    if not snaps:
        return {"nodes": [], "edges": []}

    class _All:  # every attached registry's models as one graph
        models = [m for x in snaps for m in x.models]

    s = _All
    nodes, edges = {}, []
    order = sorted({m["architecture"] for m in s.models}, key=lambda a: int(re.search(r"arch-v(\d+)", a).group(1)) if re.search(r"arch-v(\d+)", a) else 99)
    for i, a in enumerate(order):
        nodes[f"architecture:{a}"] = {"id": f"architecture:{a}", "kind": "architecture", "label": a, "group": i}
    for m in s.models:
        nid = f"model:{m['id']}"
        nodes[nid] = {"id": nid, "kind": "model", "label": f"{m['brand']} {m['version']}", "status": m["status_kind"],
                      "group": order.index(m["architecture"]), "repro_status": m["repro_status"], "brand": m["brand"]}
        edges.append({"src": nid, "dst": f"architecture:{m['architecture']}", "relation": "built_on"})
        for pid, registered in zip(m.get("parent_ids", []), m.get("parent_registered", [])):
            src = f"model:{pid}"
            if not registered and src not in nodes:
                nodes[src] = {"id": src, "kind": "model-unregistered", "label": pid.replace("@", " "),
                              "status": "unregistered", "brand": m["brand"], "group": None}
            edges.append({"src": src, "dst": nid, "relation": "parent"})
        if m["attached_to_demo"]:
            did = f"demo:{m['brand']}"
            nodes.setdefault(did, {"id": did, "kind": "demo", "label": f"{m['brand']} demo", "brand": m["brand"]})
            edges.append({"src": nid, "dst": did, "relation": "attached"})
    return {"nodes": list(nodes.values()), "edges": edges, "snapshots": [f"{x.name}@v{x.version}" for x in snaps],
            "registries": [x.uri for x in snaps]}
