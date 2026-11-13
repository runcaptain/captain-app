"""The shape every host's setup has, and the parts they share: the record in the workspace, the list of what was
made, removal in a safe order, and the refusal while a model is still staged there."""
from __future__ import annotations

import datetime as dt

import yaml

from ...storage.backend import NotFound
from ...storage.registry import dump_yaml
from ...workspace import Context, whoami


class MissingPermission(ValueError):
    """The connected login may not do one step. `.policy` is what an admin can grant: a policy document, or a
    list of role names, as the cloud takes them."""

    def __init__(self, step: str, action: str, policy, cloud: str = "cloud"):
        super().__init__(f"Your {cloud} login can't {step}: it lacks {action}. Ask an admin to grant what is below, then try again.")
        self.step, self.action, self.policy = step, action, policy


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Host:
    cloud = ""            # aws, modal, gcp, azure, local
    label = ""            # as a person reads it
    kind = ""             # the compute kind its staging target is
    target = ""           # the compute target setup makes
    login = None          # which `cst cloud` login it needs, or None (Modal signs in by itself, this computer needs none)
    scale_to_zero = False
    proven: str | None = None      # the date a model was staged here on a real account, start to finish
    storage = ""          # where the bundle and the request log are kept, in a sentence
    address = ""          # what the endpoint's address and key look like, in a sentence
    options: list[dict] = []       # what setup asks: [{name, label, default?, required?}]

    @property
    def key(self) -> str:
        return f"serving/setup-{self.cloud}.yaml"

    # ---- the record ---------------------------------------------------------------------------

    def read(self, ctx: Context) -> dict | None:
        try:
            return yaml.safe_load(ctx.store.get(self.key))
        except NotFound:
            return None

    def write(self, ctx: Context, rec: dict) -> None:
        try:
            _, tag = ctx.store.get_with_tag(self.key)
        except NotFound:
            tag = None
        ctx.store.put_if(self.key, dump_yaml(rec), tag)

    def begin(self, ctx: Context, **fields) -> tuple[dict, callable]:
        """The record to fill while setting up, and `note(type, name, **more)`: write down one thing that was made,
        at once, so a setup that stops halfway still lists it for teardown."""
        rec = self.read(ctx) or {}
        if rec.get("status") in ("removed",):
            rec = {}
        rec.setdefault("created", [])
        rec.setdefault("created_at", now())
        rec.setdefault("created_by", whoami())
        rec.update(cloud=self.cloud, status="setting up", **fields)

        def note(kind: str, name: str, **more) -> None:
            if not any(x["type"] == kind and x["name"] == name for x in rec["created"]):
                rec["created"].append({"type": kind, "name": name, **more})
            self.write(ctx, rec)

        self.write(ctx, rec)
        return rec, note

    # ---- the shape ----------------------------------------------------------------------------

    def describe(self) -> dict:
        return {"cloud": self.cloud, "label": self.label, "kind": self.kind, "target": self.target, "login": self.login,
                "scale_to_zero": self.scale_to_zero, "proven": self.proven, "storage": self.storage, "address": self.address,
                "options": [dict(o) for o in self.options],
                "proven_text": (f"Ran on a real account on {self.proven}." if self.proven else
                                "Not run on a real account yet. Tested against a stand-in only.") if self.cloud != "local" else None}

    def plan(self, ctx: Context, **options) -> dict:
        raise NotImplementedError

    def setup(self, ctx: Context, confirm: bool = False, progress=None, **options) -> dict:
        raise NotImplementedError

    def status(self, ctx: Context) -> dict:
        rec = self.read(ctx)
        if not rec or rec.get("status") == "removed":
            return {"cloud": self.cloud, "status": "none", "created": [], "text": f"Serving isn't set up in your {self.label} account yet."}
        return {**rec, "text": ("Serving is set up: " if rec.get("status") == "ready" else f"Serving setup is {rec.get('status')}: ")
                               + ", ".join(f"{x['type']} {x['name']}" for x in rec.get("created") or []) + "."}

    def leftovers(self, ctx: Context) -> list[dict]:
        raise NotImplementedError

    # ---- removal ------------------------------------------------------------------------------

    def refuse_if_staged(self, ctx: Context, rec: dict) -> None:
        from ... import project_state as PS
        from ... import projects as PR

        for pr in PR.rows(ctx):
            for _env, st in ((PS.read(ctx, pr["id"])[0].get("staging") or {}).items()):
                if st.get("status") in ("running", "starting") and st.get("target") == rec.get("target"):
                    raise ValueError(f"{pr['name']} has a model deployed here. Stop it first, then remove what the first deploy created.")

    order: dict[str, int] = {}

    def remove_one(self, ctx: Context, rec: dict, item: dict) -> None:
        """Delete one thing setup made. Raise with the cloud's words if it won't go. Missing counts as gone."""
        raise NotImplementedError

    def is_missing(self, e: Exception) -> bool:
        return any(w in str(e).lower() for w in ("not found", "does not exist", "cannot be found", "nosuch", "404", "notfound"))

    def teardown(self, ctx: Context, progress=None, **_options) -> dict:
        rec = self.read(ctx)
        if not rec or rec.get("status") == "removed":
            raise ValueError(f"Serving was never set up in {self.label} for this workspace.")
        self.refuse_if_staged(ctx, rec)
        say = progress or (lambda _t: None)
        left = []
        for x in sorted(rec.get("created") or [], key=lambda x: self.order.get(x["type"], 50)):
            say(f"Removing {x['type']} {x['name']}")
            try:
                if x["type"] == "compute-target":
                    body, tag = ctx.store.get_with_tag(f"compute/{x['name']}.yaml")
                    ctx.store.put_if(f"compute/{x['name']}.yaml", dump_yaml({**yaml.safe_load(body), "deleted": True, "purposes": []}), tag)
                else:
                    self.remove_one(ctx, rec, x)
            except NotFound:
                continue
            except Exception as e:  # noqa: BLE001
                if self.is_missing(e):
                    continue
                left.append({**{k: v for k, v in x.items() if k != "error"}, "error": str(e)[:300]})
        rec.update(created=left, status="removed" if not left else "partly removed", removed_at=now())
        self.write(ctx, rec)
        return {**rec, "text": f"Serving setup is removed. Nothing of Captain's is left in your {self.label} account." if not left else
                "Removed, except: " + "; ".join(f"{x['type']} {x['name']} ({x['error']})" for x in left)}
