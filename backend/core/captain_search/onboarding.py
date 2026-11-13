"""What a new person has done so far, so the app's onboarding can show each step as done or next.

Steps follow a search engineer's first day: connect a workspace and storage, add cloud credentials if the data is
in the cloud, bring data in (or seed the demo), open a first investigation, run a comparison, record a decision,
and point an agent at Captain. Every flag is read from real records. Nothing here changes anything.
"""
from __future__ import annotations

import shutil

from .workspace import Context, load_config


def _registries(ctx) -> list:
    from .attach import _read

    return _read(ctx)[0]["registries"]


def status(ctx: Context) -> dict:
    from . import cloud, investigations as INV

    reg = ctx.registry
    datasets = reg.names("dataset")
    pipelines = reg.names("pipeline")
    runs = reg.names("backtest-run")
    inv = INV.listing(ctx)
    storage = ctx.workspace.storage_uri
    kind = storage.split("://")[0] if "://" in storage else "local"
    steps = [
        {"id": "workspace", "title": "Connect a workspace", "done": True,
         "detail": f"{ctx.workspace.name} on {kind} storage", "command": "cst init <name> --storage s3://bucket/prefix"},
        {"id": "cloud", "title": "Add cloud credentials", "done": bool(cloud.profiles()) or kind == "local",
         "optional": kind == "local", "detail": f"{len(cloud.profiles())} profile(s)", "command": "cst cloud add"},
        {"id": "data", "title": "Bring in data", "done": len(datasets) > 0,
         "detail": f"{len(datasets)} dataset(s). Drag catalogs, query sets, judgments and logs into Data",
         "command": "cst data import <files or folders> --folder <folder>"},
        {"id": "registry", "title": "Attach your model registry", "done": bool(_registries(ctx)), "optional": True,
         "detail": "an S3, GCS or Azure folder of model versions, for lineage", "command": "POST /api/registries {uri}"},
        {"id": "pipeline", "title": "Register a baseline", "done": len(pipelines) > 0,
         "detail": f"{len(pipelines)} model version(s)", "command": "cst pipeline new  (or: cst pipeline link s3://<registry>/...)"},
        # Experiments left the screens (2026-11-12): the tour ends at an offline evaluation, which is what a team
        # opens Captain to run. `cst investigate` still works from a terminal.
        {"id": "run", "title": "Run an offline evaluation", "done": len(runs) > 0 or any(i["runs"] for i in inv),
         "detail": f"{len(runs)} run(s)", "command": "cst backtest run <name>"},
        {"id": "agent", "title": "Point an agent at Captain", "done": any((i.get("initiator") or {}).get("agent") for i in inv),
         "optional": True, "detail": "in a shell here, or Cursor, Devin or any terminal: set CST_AGENT",
         "command": "cst app context"},
    ]
    nxt = next((s["id"] for s in steps if not s["done"] and not s.get("optional")), None)
    return {"cst": shutil.which("cst"), "workspace": {"name": ctx.workspace.name, "storage": storage, "kind": kind},
            "workspaces": sorted((load_config().get("workspaces") or {}).keys()),
            "counts": {"datasets": len(datasets), "pipelines": len(pipelines), "runs": len(runs), "investigations": len(inv)},
            "steps": steps, "next": nxt}
