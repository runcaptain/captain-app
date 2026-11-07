"""Connecting storage during onboarding, and attaching model registries for lineage.

- check_storage: before a workspace is made on a bucket, prove Captain can read and write there with the chosen
  cloud login, and say in plain words what's wrong if it can't.
- registries: the model registries (s3://, gs://, az://, or a folder in the <architecture>/<brand>/<version>/
  layout) a workspace has attached. Each attach or sync reads the registry's manifests (never writes it) into a
  write-once snapshot, which feeds the lineage view.
"""
from __future__ import annotations

import datetime as dt
import uuid

import yaml

from .storage.backend import AlreadyExists, Conflict, NotFound, open_store
from .workspace import Context, whoami

REG_KEY = "registries.yaml"


def check_storage(uri: str, profile: str | None = None) -> dict:
    """{ok, can_read, can_write, existing_workspace, error}. Writes one small marker file, never deletes anything."""
    out = {"uri": uri, "ok": False, "can_read": False, "can_write": False, "existing_workspace": None, "error": None}
    try:
        store = open_store(uri, profile)
    except Exception as e:  # noqa: BLE001
        out["error"] = _plain(e, uri)
        return out
    try:
        body = store.get("workspace.yaml")
        out["existing_workspace"] = (yaml.safe_load(body) or {}).get("name")
        out["can_read"] = True
    except NotFound:
        out["can_read"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = _plain(e, uri)
        return out
    key = ".captain/access-check"
    try:
        try:
            _, tag = store.get_with_tag(key)
        except NotFound:
            tag = None
        store.put_if(key, f"written by {whoami()} at {dt.datetime.now(dt.UTC).isoformat()}\n".encode(), tag)
        out["can_write"] = True
    except (Conflict, AlreadyExists):
        out["can_write"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = _plain(e, uri, writing=True)
        return out
    out["ok"] = True
    return out


def _plain(e: Exception, uri: str, writing: bool = False) -> str:
    s = str(e)
    low = s.lower()
    if "nosuchbucket" in low or "does not exist" in low or "notfound" in low.replace(" ", ""):
        return f"{uri} doesn't exist, or this login can't see it."
    if "accessdenied" in low.replace(" ", "") or "403" in low or "forbidden" in low or "permission" in low:
        return f"This login isn't allowed to {'write to' if writing else 'read'} {uri}."
    if "credential" in low or "token" in low or "expired" in low:
        return "The cloud login didn't work. Check it in Cloud and compute."
    return s.splitlines()[0][:300] if s else type(e).__name__


# ---- registries ---------------------------------------------------------------------------


def _read(ctx: Context) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(REG_KEY)
        return yaml.safe_load(body) or {"registries": []}, tag
    except NotFound:
        return {"registries": []}, None


def registries(ctx: Context) -> list[dict]:
    from . import models as MD

    doc, _ = _read(ctx)
    out = []
    for r in doc["registries"]:
        snap = MD.latest_for(ctx, r["uri"]) if hasattr(MD, "latest_for") else None
        out.append({**r, "models": len(snap.models) if snap else None,
                    "snapshot": f"{snap.name}@v{snap.version}" if snap else None})
    return out


def resolve(ctx: Context, uri: str, profile: str | None = None) -> tuple[str, str | None]:
    """Fill in what the person left out, from the clouds they've connected:
    - no scheme ("my-bucket/models"): the cloud of the workspace's own storage if it's a cloud, else the only cloud
      with a login here, else AWS;
    - no login: the workspace's own login if it's for that cloud, else the one login for that cloud, else the first
      that passed its last test. None falls back to the provider's default credentials."""
    import os

    from . import cloud
    from .workspace import load_config

    uri = uri.strip().rstrip("/")
    profs = cloud.profiles()
    wcfg = (load_config().get("workspaces") or {}).get(ctx.workspace.name, {})
    if "://" not in uri and not os.path.isabs(os.path.expanduser(uri)):
        ws_provider = cloud.provider_of(ctx.workspace.storage_uri)
        providers = sorted({p.get("provider") for p in profs if p.get("provider")})
        provider = ws_provider if ws_provider != "local" else (providers[0] if len(providers) == 1 else "aws")
        scheme = {"aws": "s3", "gcp": "gs", "azure": "az"}.get(provider, "s3")
        uri = f"{scheme}://{uri.removeprefix('/')}"
    if profile is None:
        prov = cloud.provider_of(uri)
        if prov != "local":
            mine = [p for p in profs if p.get("provider") == prov]
            if wcfg.get("profile") and any(p["name"] == wcfg["profile"] for p in mine):
                profile = wcfg["profile"]
            elif len(mine) == 1:
                profile = mine[0]["name"]
            else:
                ok = [p for p in mine if (p.get("last_test") or {}).get("ok")]
                profile = (ok or mine or [{"name": None}])[0]["name"]
    return uri, profile


def attach(ctx: Context, uri: str, profile: str | None = None, label: str | None = None, progress=lambda f: None) -> dict:
    """Attach a registry and read it. Reading it first means a wrong address fails here, not later. The scheme and
    the cloud login are filled in from the connected clouds when left out (see resolve)."""
    from . import models as MD

    uri, profile = resolve(ctx, uri, profile)
    progress(0.1)
    snap = MD.import_registry(ctx, uri, profile)
    progress(0.9)
    for _ in range(10):
        doc, tag = _read(ctx)
        regs = [r for r in doc["registries"] if r["uri"] != uri]
        regs.append({"id": uuid.uuid5(uuid.NAMESPACE_URL, uri).hex[:12], "uri": uri, "profile": profile,
                     "label": label or uri.split("://", 1)[-1], "attached_by": whoami(),
                     "synced_at": dt.datetime.now(dt.UTC).isoformat()})
        try:
            ctx.store.put_if(REG_KEY, yaml.safe_dump({"registries": regs}, sort_keys=False).encode(), tag)
            break
        except Conflict:
            continue
    progress(1.0)
    return {"uri": uri, "profile": profile, "snapshot": f"{snap.name}@v{snap.version}", "models": len(snap.models)}


def detach(ctx: Context, uri: str) -> dict:
    """Stop listing a registry. Its snapshots stay (they're write-once), and the registry itself is never touched."""
    uri = uri.rstrip("/")
    for _ in range(10):
        doc, tag = _read(ctx)
        if not any(r["uri"] == uri for r in doc["registries"]):
            raise NotFound(f"{uri} isn't attached")
        try:
            ctx.store.put_if(REG_KEY, yaml.safe_dump({"registries": [r for r in doc["registries"] if r["uri"] != uri]},
                                                     sort_keys=False).encode(), tag)
            return {"detached": uri}
        except Conflict:
            continue
    raise Conflict("registries kept changing while saving. Try again.")
