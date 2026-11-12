"""Modal as a host: a web endpoint that sleeps when idle.

Setup makes two things: a Modal volume for the serving bundle and the request log, and the compute target. There
is no image repository and no role: Modal builds the image itself the first time a model is staged, and the
endpoint reaches nothing but its own volume. Each staged model then adds one app and one secret, removed on
unstage (compute_providers/modal_serving.py).

Why a volume and not the workspace's bucket: Modal has no identity a bucket can trust, so the endpoint would need
keys. Keys short enough to be safe (hours) run out under an endpoint that stays up for weeks, and long-lived keys
in a secret are what least privilege is meant to avoid. The volume needs no keys and goes away with the setup.

Modal signs in by itself (`modal token new`), so there is no `cst cloud` login and no policy to copy: a person who
can deploy in the Modal workspace can do all of this.
"""
from __future__ import annotations

from ... import price_table as PT
from ...workspace import Context
from .base import Host, MissingPermission, now

TARGET = "modal-serving"


def _clean(prefix: str) -> str:
    from ...compute_providers.serving import clean_name

    return clean_name(prefix or "captain", 40)


class ModalHost(Host):
    cloud, label, kind, target, login = "modal", "Modal", "modal-endpoint", TARGET, None
    scale_to_zero = True
    proven: str | None = "2026-11-11"
    storage = ("A Modal volume setup makes. Only the serving bundle and the request log go there. No keys: the "
               "endpoint mounts it.")
    address = ("https://<workspace>--<app>-endpoint-web.modal.run. Public, with the endpoint key as a bearer token on "
               "every route but /healthz and /ping.")
    options = [{"name": "environment", "label": "Modal environment (blank for your login's default)"},
               {"name": "name_prefix", "label": "Name prefix", "default": "captain"},
               {"name": "cpu", "label": "CPU cores for one copy", "default": "1"},
               {"name": "memory_mb", "label": "Memory for one copy, in MB", "default": "2048"},
               {"name": "max_copies", "label": "Most copies at once", "default": "1"},
               {"name": "idle_seconds", "label": "Seconds an idle copy stays up", "default": "300"}]
    order = {"compute-target": 0, "modal-volume": 5}

    def _opts(self, o: dict) -> dict:
        return {"environment": (o.get("environment") or "").strip() or None, "name_prefix": _clean(o.get("name_prefix") or "captain"),
                "cpu": float(o.get("cpu") or 1), "memory_mb": int(float(o.get("memory_mb") or 2048)),
                "max_copies": max(1, int(float(o.get("max_copies") or 1))), "min_copies": max(0, int(float(o.get("min_copies") or 0))),
                "idle_seconds": int(float(o.get("idle_seconds") or 300))}

    def names(self, ctx: Context, o: dict) -> dict:
        vol = f"{o['name_prefix']}-serving"
        return {"volume": vol, "target": TARGET, "apps": o["name_prefix"],
                "storage": f"modal://{vol}{'@' + o['environment'] if o['environment'] else ''}/{ctx.workspace.name}"}

    def hourly(self, cpu: float, memory_mb: int) -> float:
        return 3600 * (cpu * PT.MODAL["core_second"] + memory_mb / 1024 * PT.MODAL["gib_second"])

    def plan(self, ctx: Context, **options) -> dict:
        o = self._opts(options)
        n = self.names(ctx, o)
        hourly = self.hourly(o["cpu"], o["memory_mb"])
        where = f"the Modal environment {o['environment']}" if o["environment"] else "your Modal login's default environment"
        rows = [
            {"resource": "Modal volume", "name": n["volume"], "why": "Holds the serving bundle (the model and its catalog) and the request log. "
                                                                     "The endpoint mounts it, so no keys are needed."},
            {"resource": "Captain compute target", "name": n["target"],
             "why": f"Modal web endpoint, {o['cpu']:g} CPU and {o['memory_mb'] / 1024:g} GB a copy, at most {o['max_copies']}, in {where}. Staging uses it."}]
        later = [
            {"resource": "Modal app", "name": f"{n['apps']}-{{project}}-staging", "why": "The endpoint. Made when you stage a model, stopped when you unstage it."},
            {"resource": "Modal secret", "name": f"{n['apps']}-{{project}}-staging", "why": "What the endpoint is told: which bundle, and the SHA-256 of the endpoint key. "
                                                                                           "Never the key itself. Deleted with the app."}]
        return {"cloud": "modal", "environment": o["environment"], "names": n, "storage": n["storage"], "options": o, "creates": rows, "each_deploy": later,
                "cost": {"setup_usd": 0.0, "hourly_awake_usd": round(hourly, 4),
                         "text": "Setting up costs nothing. Modal builds the image the first time you stage, about two minutes of one CPU, under a cent. "
                                 f"A copy costs about ${hourly:.3f} an hour while it is awake and nothing while it sleeps. "
                                 f"It sleeps after {o['idle_seconds']} idle seconds. The volume is free under 1 TiB. "
                                 f"Prices as of {PT.SERVERLESS_PRICED_AS_OF}."},
                "login_policy": None,
                "login_text": "Modal has no policy to grant. Whoever runs this needs to be signed in to Modal on this computer "
                              "(`modal token new`) as a member who can deploy in that environment.",
                "text": f"Set up serving on Modal in {where}: " + ", ".join(f"{r['resource']} {r['name']}" for r in rows) + "."}

    def _modal(self):
        try:
            import modal  # type: ignore
        except ImportError:
            raise ValueError("The modal package is not installed here: pip install 'captain-search[modal]'") from None
        return modal

    def _denied(self, e: Exception, step: str):
        from ...compute_providers.modal_serving import signed_out

        s = signed_out(e)
        if s:
            return ValueError(s)
        if "permission" in str(e).lower() or "forbidden" in str(e).lower() or "not authorized" in str(e).lower():
            return MissingPermission(step, "the right to deploy in this Modal environment",
                                     ["Modal workspace member with deploy rights in the environment"], cloud="Modal")
        return None

    def setup(self, ctx: Context, confirm: bool = False, progress=None, **options) -> dict:
        from ... import compute as C
        from ...storage.backend import NotFound

        p = self.plan(ctx, **options)
        if not confirm:
            raise ValueError("Confirm first. " + p["text"] + " " + p["cost"]["text"])
        o, n = p["options"], p["names"]
        say = progress or (lambda _t: None)
        modal = self._modal()
        rec, note = self.begin(ctx, environment=o["environment"], names=n, storage=n["storage"])
        say(f"Creating the Modal volume {n['volume']}")
        try:
            try:
                modal.Volume.from_name(n["volume"], environment_name=o["environment"]).hydrate()
                existed = True
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ != "NotFoundError" and "not found" not in str(e).lower():
                    raise
                existed = False
            if existed and not any(x["type"] == "modal-volume" and x["name"] == n["volume"] for x in rec["created"]):
                rec["status"] = "name taken"
                self.write(ctx, rec)
                raise ValueError(f"A Modal volume named {n['volume']} already exists there and Captain did not make it. "
                                 "Pick another name prefix.")
            if not existed:
                modal.Volume.objects.create(n["volume"], environment_name=o["environment"])
                note("modal-volume", n["volume"], environment=o["environment"])
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            err = self._denied(e, f"create the volume {n['volume']}")
            if err:
                rec["status"] = "needs permission"
                self.write(ctx, rec)
                raise err from None
            raise
        cfg = {"environment": o["environment"], "name_prefix": n["apps"], "serving_storage": n["storage"], "cpu": str(o["cpu"]),
               "memory_mb": str(o["memory_mb"]), "min_copies": str(o["min_copies"]), "max_copies": str(o["max_copies"]),
               "idle_seconds": str(o["idle_seconds"])}
        say("Adding the compute target")
        try:
            C.get(ctx, n["target"])
            C.add(ctx, n["target"], self.kind, None, None, cfg, replace=True)
        except NotFound:
            C.add(ctx, n["target"], self.kind, None, None, cfg)
        note("compute-target", n["target"])
        rec.update(status="ready", target=n["target"], ready_at=now())
        self.write(ctx, rec)
        return {**rec, "text": f"Serving is set up on Modal. Staging can use the target {n['target']}."}

    def remove_one(self, ctx: Context, rec: dict, item: dict) -> None:
        if item["type"] == "modal-volume":
            self._modal().Volume.objects.delete(item["name"], allow_missing=True, environment_name=item.get("environment") or rec.get("environment"))

    def leftovers(self, ctx: Context) -> list[dict]:
        """Every app, secret and volume in the Modal environment whose name starts with this setup's prefix."""
        rec = self.read(ctx)
        if not rec:
            return []
        modal = self._modal()
        env = rec.get("environment") or ""
        prefix = (rec.get("names") or {}).get("apps") or "captain"
        out = []
        for kind, mgr in (("modal-secret", modal.Secret.objects), ("modal-volume", modal.Volume.objects)):
            for x in mgr.list(environment_name=env):
                name = getattr(x, "name", None) or ""
                if name.startswith(prefix):
                    out.append({"type": kind, "name": name})
        from ... import compute_providers as P

        try:
            prov = P.get({"kind": self.kind, "config": {"environment": env or None, "name_prefix": prefix, "serving_storage": rec.get("storage")}}, ctx)
            import json
            import os
            import sys

            r = prov._run([sys.executable, "-m", "modal", "app", "list", "--json"] + (["-e", env] if env else []), dict(os.environ))
            for a in json.loads(r.stdout or "[]"):
                name = a.get("Description") or a.get("description") or ""
                state = str(a.get("State") or a.get("state") or "").lower()
                if name.startswith(prefix) and state in ("deployed", "running"):
                    out.append({"type": "modal-app", "name": name})
        except Exception as e:  # noqa: BLE001
            out.append({"type": "modal-app", "name": "?", "error": f"could not list apps: {str(e)[:200]}"})
        return out
