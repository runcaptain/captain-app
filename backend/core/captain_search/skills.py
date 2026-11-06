"""Skills: built-in ones ship in the package, workspace ones are write-once versions in the bucket."""
from __future__ import annotations

import os

import yaml

from .model.entities import Skill
from .storage.registry import check_name, sha256
from .workspace import whoami

BUILTIN = os.path.join(os.path.dirname(__file__), "skills_builtin")


def _front(text: str) -> dict:
    if text.startswith("---"):
        try:
            return yaml.safe_load(text.split("---", 2)[1]) or {}
        except Exception:
            return {}
    return {}


def listing(ctx) -> list[dict]:
    out = []
    for n in sorted(os.listdir(BUILTIN)):
        p = os.path.join(BUILTIN, n, "SKILL.md")
        if os.path.exists(p):
            f = _front(open(p).read())
            out.append({"name": n, "version": "builtin", "description": f.get("description", ""), "tags": f.get("tags", [])})
    for n in ctx.registry.names("skill"):
        m = ctx.registry.load("skill", n)
        out.append({"name": n, "version": f"v{m.version}", "description": m.description, "tags": list(m.tags)})
    return out


def read(ctx, name: str) -> str:
    if name in ctx.registry.names("skill"):
        m = ctx.registry.load("skill", name)
        e = next(f for f in m.files if f.path == "SKILL.md")
        return ctx.registry.get_blob(e).decode()
    p = os.path.join(BUILTIN, name, "SKILL.md")
    if not os.path.exists(p):
        raise ValueError(f"no skill named {name}")
    return open(p).read()


def add(ctx, folder: str, dry_run=False) -> Skill:
    p = os.path.join(folder, "SKILL.md")
    text = open(p).read()
    f = _front(text)
    name = check_name(f.get("name") or os.path.basename(os.path.abspath(folder)))
    files = []
    blobs = []
    for root, _, fs in os.walk(folder):
        for fn in sorted(fs):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, folder)
            data = open(full, "rb").read()
            blobs.append((data, rel))
    m = Skill(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0,
              description=f.get("description", ""), tags={t: "1" for t in f.get("tags", [])},
              files=[{"path": rel, "sha256": sha256(d), "bytes": len(d)} for d, rel in blobs])
    if dry_run:
        return m
    for d, rel in blobs:
        ctx.registry.put_blob(d, rel)
    return ctx.registry.publish(m)
