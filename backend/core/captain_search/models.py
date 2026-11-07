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


def _scan_store(uri: str, profile: str | None) -> dict:
    """Any registry Captain's storage layer can open: gs://, az:// or a local folder (sizes aren't listed there)."""
    from .storage.backend import open_store

    st = open_store(uri, profile)
    keys = st.list("")
    try:
        index_md = st.get("INDEX.md").decode()
    except Exception:  # noqa: BLE001
        index_md = ""
    models = []
    for k in sorted(keys):
        m = re.match(r"^([^/]+)/([^/]+)/([^/]+)/MANIFEST\.yaml$", k)
        if not m:
            continue
        arch, brand, label = m.groups()
        man = yaml.safe_load(st.get(k)) or {}
        base = f"{arch}/{brand}/{label}/"
        files = [{"path": f[len(base):], "bytes": None} for f in keys if f.startswith(base)]
        models.append({
            "id": f"{brand}@{label}", "brand": brand, "architecture": arch, "version": label,
            "status": str(man.get("status", "")), "status_kind": status_kind(str(man.get("status", ""))),
            "repro_status": str(man.get("repro_status", "not-tested")), "parent_label": man.get("parent"),
            "parents": parse_parents(brand, man.get("parent")), "training_commit": man.get("training_commit"),
            "dvc_alias": man.get("dvc_alias"), "attached_to_demo": bool(man.get("attached_to_demo")),
            "results": man.get("results") or [], "known_issues": man.get("known_issues") or [],
            "what_changed": man.get("what_changed"), "path": f"{uri.rstrip('/')}/{base}",
            "files": sorted(files, key=lambda f: f["path"]), "bytes": None, "manifest": man,
        })
    archs = sorted({m["architecture"] for m in models})
    return {"index_md": index_md, "models": models, "architectures": archs, "architecture_docs": {}}


def scan(uri: str, profile: str | None = None, s3=None) -> dict:
    u = urlparse(uri)
    if u.scheme != "s3":
        return _scan_store(uri, profile)
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
    if not data["models"] and not data["index_md"]:
        # A new registry (Captain made it, or a team started one) has an INDEX.md and no models yet: that's fine.
        raise ValueError(f"no <architecture>/<brand>/<version>/MANIFEST.yaml found under {uri}, and no INDEX.md: "
                         "this doesn't look like a model registry")
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


def _vkey(label: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", label or ""))


def find(ctx: Context, ref: str) -> tuple[dict, RegistrySnapshot]:
    """A model by its id (brand@version), or by brand alone: the brand's current model, else its newest version."""
    from .storage.backend import NotFound

    pairs = [(m, s) for s in all_latest(ctx) for m in s.models]
    hit = next(((m, s) for m, s in pairs if m["id"] == ref), None)
    if hit:
        return hit
    if "@" not in ref:
        mine = [(m, s) for m, s in pairs if m["brand"] == ref]
        if mine:
            cur = [p for p in mine if p[0]["status_kind"] == "current"] or mine
            return max(cur, key=lambda p: _vkey(p[0]["version"]))
    raise NotFound(f"no model {ref!r} in any attached registry. Ids look like brand@v1, see `cst models list`.")


def detail(ctx: Context, model_id: str) -> dict:
    return find(ctx, model_id)[0]


PULL_PARTS = ("weights", "code", "env", "results", "inputs")


def _sha256(path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pull(ctx: Context, ref: str, dest: str | None = None, parts: list[str] | None = None, profile: str | None = None,
         progress=lambda f: None) -> dict:
    """Download a model version's files and check each against the version's SHA256SUMS.

    By default it takes everything but inputs/ (training data can be large and belongs in the cloud). The top-level
    files (MANIFEST.yaml, REPRODUCE.md, reproduce.sh, SHA256SUMS) always come. A file already on disk with the
    right checksum is kept, so a stopped pull picks up where it left off. A checksum mismatch fails the pull.
    """
    import os
    from pathlib import Path

    from .attach import _read

    m, snap = find(ctx, ref)
    parts = list(parts) if parts else [p for p in PULL_PARTS if p != "inputs"]
    bad = [p for p in parts if p not in PULL_PARTS]
    if bad:
        raise ValueError(f"unknown part {', '.join(bad)}: pick from {', '.join(PULL_PARTS)}")
    if profile is None:
        profile = next((r.get("profile") for r in _read(ctx)[0]["registries"]
                        if r["uri"].rstrip("/") == snap.uri.rstrip("/")), None)
    out = Path(dest or f"{m['brand']}-{m['version']}").expanduser().resolve()
    want = [f for f in m["files"] if "/" not in f["path"] or f["path"].split("/", 1)[0] in parts]
    if not want:
        raise ValueError(f"{m['id']} has no files under {', '.join(parts)}")

    u = urlparse(m["path"])
    if u.scheme == "s3":
        s3 = _s3(profile)
        bucket, base = u.netloc, u.path.lstrip("/")

        def fetch(rel, to):
            s3.download_file(bucket, base + rel, str(to))

        def read(rel):
            return s3.get_object(Bucket=bucket, Key=base + rel)["Body"].read()
    else:
        from .storage.backend import open_store

        st = open_store(snap.uri, profile)
        base = m["path"][len(snap.uri.rstrip("/")) + 1:]

        def read(rel):
            return st.get(base + rel)

        def fetch(rel, to):
            to.write_bytes(read(rel))

    try:
        sums_text = read("SHA256SUMS").decode()
    except Exception:  # noqa: BLE001  a version without SHA256SUMS still downloads, unchecked
        sums_text = ""
    sums = {}
    for line in sums_text.splitlines():
        h, _, path = line.strip().partition("  ")
        if len(h) == 64 and path:
            sums[path.lstrip("*")] = h

    total = sum(f["bytes"] or 0 for f in want) or len(want)
    done, fetched, kept, mismatched = 0, 0, 0, []
    for f in want:
        rel, to = f["path"], out / f["path"]
        to.parent.mkdir(parents=True, exist_ok=True)
        expect = sums.get(rel)
        if to.exists() and expect and _sha256(to) == expect:
            kept += 1
        else:
            tmp = to.with_name(to.name + ".part")
            fetch(rel, tmp)
            os.replace(tmp, to)
            fetched += 1
            if expect and _sha256(to) != expect:
                mismatched.append(rel)
        done += f["bytes"] or 1
        progress(done / total)
    unchecked = [f["path"] for f in want if f["path"] not in sums and f["path"] != "SHA256SUMS"]
    res = {"model": m["id"], "architecture": m["architecture"], "from": m["path"], "to": str(out), "parts": parts,
           "files": len(want), "downloaded": fetched, "already_there": kept,
           "bytes": sum(f["bytes"] or 0 for f in want) if all(f["bytes"] is not None for f in want) else None,
           "checked": len(want) - len(unchecked) - (1 if any(f["path"] == "SHA256SUMS" for f in want) else 0),
           "unchecked": unchecked, "mismatched": mismatched}
    if mismatched:
        raise ValueError(f"{len(mismatched)} file(s) don't match SHA256SUMS: {', '.join(mismatched[:5])}. "
                         "They may have been changed after the version was written. Don't use this download.")
    return res


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
