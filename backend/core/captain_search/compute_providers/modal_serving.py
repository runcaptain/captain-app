"""Modal web endpoint: host a staged model on Modal, asleep while nobody calls.

One deployment is one Modal app (the endpoint, staging/modal_endpoint.py) and one Modal secret (what the runtime is
told, and the SHA-256 of the endpoint key). Both are named `<prefix>-<project>-<env>`, the app is tagged
app=captain, and both are removed on unstage. The image is built by Modal from the build folder Captain makes, so
there is no registry and no role. The bundle and the request log live in a Modal volume the serving setup makes.

Captain never deploys over, stops or deletes an app or a secret it did not make: an app of that name that does not
carry Captain's tag, or a secret of that name Captain has no record of, stops the deploy with a sentence.

Modal's login is its own (`modal token new`), not a `cst cloud` profile.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

from . import ComputeProvider, Job, ProviderError
from ..staging import timings as TM
from .serving import HttpServing, clean_name

APP_TAGS = {"app": "captain", "captain-purpose": "serving"}
CLASS = "Endpoint"
MAX_IDLE_S = 1200  # Modal keeps an idle copy at most 20 minutes


def _modal():
    try:
        import modal  # type: ignore
    except ImportError:
        raise ProviderError("the modal package is not installed: pip install 'captain-search[modal]'") from None
    return modal


def _gone(e: Exception) -> bool:
    return type(e).__name__ == "NotFoundError" or "not found" in str(e).lower()


def signed_out(e: Exception) -> str | None:
    """A sentence when Modal refused because nobody is signed in on this computer."""
    s = f"{type(e).__name__} {e}".lower()
    if "autherror" in s or "token" in s and ("missing" in s or "invalid" in s or "not found" in s) or "unauthenticated" in s:
        return "This computer isn't signed in to Modal. Run `modal token new` in a terminal, then try again."
    return None


class ModalServingProvider(HttpServing, ComputeProvider):
    kind, provider, label = "modal-endpoint", "modal", "Modal web endpoint"
    purposes = ["serve"]
    live_tested = "2026-11-11"  # it runs no jobs: this is the date it hosted a model on a real workspace (below)
    # A team Modal workspace, in its own environment, 1 CPU and 2 GB: the guided setup, the demo store's starter
    # ranker staged beside its baseline (ready in 82 s with the image build, 20 s without), queried, checked,
    # shadowed, split with simulated shoppers, ramped, rolled back, slept and woken (4.5 s), unstaged, removed.
    serving_live_tested: str | None = "2026-11-11"
    serving_storage_own = True  # its volume, whatever the workspace is kept on
    can_logs = can_cancel = False
    fields = [
        {"name": "environment", "label": "Modal environment (blank for your login's default)", "required": False},
        {"name": "name_prefix", "label": "Name prefix for deployed apps", "required": False, "default": "captain"},
        {"name": "serving_storage", "label": "Modal volume for the serving bundle and request log", "required": True},
        {"name": "cpu", "label": "CPU cores for one copy", "required": False, "default": "1"},
        {"name": "memory_mb", "label": "Memory for one copy, in MB", "required": False, "default": "2048"},
        {"name": "min_copies", "label": "Copies kept up while idle (0 sleeps)", "required": False, "default": "0"},
        {"name": "max_copies", "label": "Most copies at once", "required": False, "default": "1"},
        {"name": "idle_seconds", "label": "Seconds an idle copy stays up", "required": False, "default": "300"}]

    # ---- it runs no jobs --------------------------------------------------------------------

    def plan(self, job: Job) -> list[dict]:
        raise ValueError("a Modal web endpoint serves models and cannot run jobs. Use a Modal function target.")

    submit = plan

    def status(self, h):
        raise ValueError("Modal web endpoint targets run no jobs")

    def logs(self, h, since=None):
        return self.status(h)

    def cancel(self, h):
        return self.status(h)

    def test(self) -> dict:
        try:
            modal = _modal()
            modal.Volume.from_name(self._volume()[0], environment_name=self._env()).hydrate()
            return {"ok": True, "detail": f"signed in to Modal. The serving volume {self._volume()[0]} exists."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": signed_out(e) or str(e)[:400]}

    # ---- names ------------------------------------------------------------------------------

    def _env(self) -> str | None:
        return self.config.get("environment") or None

    def _volume(self) -> tuple[str, str]:
        from ..storage.modal_volume import parse

        vol, _, folder = parse(self.config["serving_storage"])
        return vol, folder

    def _names(self, st: dict) -> dict:
        prefix = (self.config.get("name_prefix") or "captain").strip("-")
        name = clean_name(f"{prefix}-{st['project']}-{st['env']}", 60)
        return {"app": name, "secret": name}

    def _size(self) -> dict:
        c = self.config

        def num(k, d):
            try:
                return float(c.get(k) or d)
            except (TypeError, ValueError):
                return float(d)

        return {"cpu": num("cpu", 1), "memory_mb": int(num("memory_mb", 2048)), "min": int(num("min_copies", 0)),
                "max": max(1, int(num("max_copies", 1))), "idle_s": int(min(MAX_IDLE_S, max(2, num("idle_seconds", 300))))}

    def _fn(self, app: str):
        return getattr(_modal().Cls.from_name(app, CLASS, environment_name=self._env())(), "web")

    def _app_tags(self, name: str) -> dict | None:
        """The tags of a deployed app of this name, or None when there is none."""
        modal = _modal()
        try:
            a = modal.App.lookup(name, environment_name=self._env())
        except Exception as e:  # noqa: BLE001
            if _gone(e):
                return None
            raise
        try:
            return dict(a.get_tags())
        except Exception:  # noqa: BLE001
            return {}

    # ---- stage ------------------------------------------------------------------------------

    def stage_plan(self, st: dict) -> list[dict]:
        n, size = self._names(st), self._size()
        env = dict(st.get("container_env") or {})
        return [
            {"service": "modal", "call": "Secret.objects.create", "args": {"name": n["secret"], "environment": self._env(),
                                                                            "keys": sorted(env)}},
            {"service": "modal", "call": "deploy captain_search.staging.modal_endpoint", "args": {
                "app": n["app"], "environment": self._env(), "volume": self._volume()[0], **size,
                "tags": {**APP_TAGS, "project": st["project"], "env": st["env"], "captain-stage": st["id"]}}}]

    def _deploy(self, n: dict, st: dict, say) -> None:
        from ..staging.serving_setup import build_folder_dir

        size = self._size()
        tags = {"project": st["project"], "env": st["env"], "captain-stage": st["id"]}
        with tempfile.TemporaryDirectory(prefix="captain-modal-") as d:
            build_folder_dir(d, flavor="modal")
            env = {**os.environ, "CST_MODAL_SERVE_APP": n["app"], "CST_MODAL_SERVE_SECRET": n["secret"],
                   "CST_MODAL_SERVE_VOLUME": self._volume()[0], "CST_MODAL_SERVE_BUILD": d,
                   "CST_MODAL_SERVE_CPU": str(size["cpu"]), "CST_MODAL_SERVE_MEMORY_MB": str(size["memory_mb"]),
                   "CST_MODAL_SERVE_MIN": str(size["min"]), "CST_MODAL_SERVE_MAX": str(size["max"]),
                   "CST_MODAL_SERVE_IDLE_S": str(size["idle_s"]),
                   "CST_MODAL_SERVE_TAGS": ",".join(f"{k}={clean_name(str(v), 60)}" for k, v in tags.items())}
            argv = [sys.executable, "-m", "modal", "deploy", "-m", "captain_search.staging.modal_endpoint"]
            if self._env():
                argv += ["-e", self._env()]
            r = self._run(argv, env)
        if r.returncode != 0:
            tail = " ".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-6:])[-500:]
            raise ProviderError(signed_out(Exception(tail)) or f"Modal could not deploy the endpoint: {tail}")

    def _run(self, argv: list[str], env: dict):
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=1800)

    def stage(self, st: dict, progress=None) -> dict:
        say = progress or (lambda _t: None)
        if st.get("image"):
            raise ProviderError("A container image of your own can't be deployed on Modal from Captain yet. Use an endpoint you already run.")
        modal = _modal()
        n = self._names(st)
        mine = {(r["type"], r["name"]) for r in st.get("existing") or []}
        made: list[dict] = []
        app_res = {"type": "modal-app", "name": n["app"], "environment": self._env()}
        sec_res = {"type": "modal-secret", "name": n["secret"], "environment": self._env()}
        try:
            tags = self._app_tags(n["app"])
            if tags is not None and tags.get("app") != "captain":
                raise ProviderError(f"A Modal app named {n['app']} already exists here and Captain did not make it. "
                                    "Give this target another name prefix.")
            say("Giving Modal what the endpoint is told")
            env = {k: str(v) for k, v in (st.get("container_env") or {}).items()}
            if ("modal-secret", n["secret"]) in mine:
                modal.Secret.objects.delete(n["secret"], allow_missing=True, environment_name=self._env())
            try:
                modal.Secret.objects.create(n["secret"], env, environment_name=self._env())
            except Exception as e:  # noqa: BLE001
                if "already exists" in str(e).lower():
                    raise ProviderError(f"A Modal secret named {n['secret']} already exists here and Captain did not make it. "
                                        "Give this target another name prefix.") from None
                raise
            made.append(sec_res)
            say("Deploying the endpoint on Modal (the first time Modal builds the image: " + (TM.step_words("modal", "start", True) or "a few minutes")
                + ". After that: " + (TM.step_words("modal", "start", False) or "less") + ")")
            made.append(app_res)  # from here an app may exist, whatever the deploy says
            self._deploy(n, st, say)
            url = self._fn(n["app"]).get_web_url()
            if not url:
                raise ProviderError("Modal deployed the app but gave it no address")
            say("Waking the endpoint and waiting for it to answer")
            self.wait_ready(url, st["id"])
        except BaseException as e:
            say("Removing what was created, because the endpoint did not start")
            # A failed move leaves nothing useful: Modal replaced the old copy's secret already, so both go.
            left = self.unstage(made)
            err = e if isinstance(e, ProviderError) or not isinstance(e, Exception) else ProviderError(signed_out(e) or f"{type(e).__name__}: {str(e)[:400]}")
            err.left_behind = left
            if err is e:
                raise
            raise err from None
        return {"endpoint": {"name": n["app"], "url": url, "environment": self._env()}, "resources": [app_res, sec_res]}

    def unstage(self, resources: list[dict]) -> list[dict]:
        """Stop these apps and delete these secrets. Only names in the list are touched."""
        modal = _modal()
        left = []
        for r in sorted(resources, key=lambda r: 0 if r["type"] == "modal-app" else 1):
            env = r.get("environment") or self._env()
            try:
                if r["type"] == "modal-app":
                    try:
                        a = modal.App.lookup(r["name"], environment_name=env)
                    except Exception as e:  # noqa: BLE001
                        if _gone(e):
                            continue
                        raise
                    tags = {}
                    try:
                        tags = dict(a.get_tags())
                    except Exception:  # noqa: BLE001
                        pass
                    if tags.get("app") != "captain":
                        left.append({**r, "error": "it does not carry Captain's tag, so Captain left it alone"})
                        continue
                    a.stop()
                    for _ in range(30):  # stopped means stopped: wait until Modal no longer finds it deployed
                        try:
                            modal.App.lookup(r["name"], environment_name=env)
                        except Exception as e:  # noqa: BLE001
                            if _gone(e):
                                break
                        time.sleep(1.0)
                    else:
                        left.append({**r, "error": "Modal is still stopping it"})
                elif r["type"] == "modal-secret":
                    modal.Secret.objects.delete(r["name"], allow_missing=True, environment_name=env)
            except Exception as e:  # noqa: BLE001
                left.append({**r, "error": (signed_out(e) or str(e))[:300]})
        return left

    # ---- what is it doing -------------------------------------------------------------------

    def staged_status(self, rec: dict) -> dict:
        name = (rec.get("endpoint") or {}).get("name")
        tags = self._app_tags(name)
        if tags is None:
            return {"state": "gone", "reason": "the Modal app no longer exists"}
        try:
            s = self._fn(name).get_current_stats()
            copies = int(getattr(s, "num_total_runners", 0) or 0)
        except Exception as e:  # noqa: BLE001
            if _gone(e):
                return {"state": "gone", "reason": "the Modal app no longer exists"}
            copies = None
        return {"state": "running", "copies": copies}

    def scale(self, rec: dict) -> dict:
        s = dict(self._size())
        s.update({k: v for k, v in ((rec.get("scale") or {}).items()) if k in ("min", "max", "idle_s") and v is not None})
        return {"min_copies": s["min"], "max_copies": s["max"], "idle_seconds": s["idle_s"], "can_set": ["min_copies", "max_copies", "idle_seconds"],
                "text": (f"Sleeps when idle: an idle copy stays up {s['idle_s'] // 60} min. " if s["min"] == 0 and s["idle_s"] >= 120 and s["idle_s"] % 60 == 0
                         else f"Sleeps when idle: an idle copy stays up {s['idle_s']} seconds. " if s["min"] == 0 else f"Keeps {s['min']} up at all times. ")
                        + f"At most {s['max']} at once."}

    def set_scale(self, rec: dict, min_copies: int | None = None, max_copies: int | None = None, idle_seconds: int | None = None) -> dict:
        if idle_seconds is not None and not 2 <= int(idle_seconds) <= MAX_IDLE_S:
            raise ValueError(f"On Modal an idle copy stays up between 2 and {MAX_IDLE_S} seconds.")
        if min_copies is not None and max_copies is not None and int(min_copies) > int(max_copies):
            raise ValueError("The fewest copies can't be more than the most.")
        self._fn((rec.get("endpoint") or {}).get("name")).update_autoscaler(
            min_containers=None if min_copies is None else int(min_copies), max_containers=None if max_copies is None else int(max_copies),
            scaledown_window=None if idle_seconds is None else int(idle_seconds))
        cur = dict(rec.get("scale") or {})
        cur.update({k: v for k, v in (("min", min_copies), ("max", max_copies), ("idle_s", idle_seconds)) if v is not None})
        return {**self.scale({**rec, "scale": cur}), "scale": cur}

    def staged_logs(self, rec: dict, since: str | None = None) -> dict:
        name = (rec.get("endpoint") or {}).get("name")
        env = self._env()
        return {"lines": [], "next": since,
                "note": f"Modal keeps the app's own lines. From a terminal: modal app logs {name}" + (f" -e {env}" if env else "")}

    def found(self, prefix: str = "captain") -> list[dict]:
        """Every deployed app in this Modal environment that Captain made, whatever project made it: by Captain's
        tag, or by the name prefix. Asks Modal for the list and for how many copies are up. It never calls an app,
        so a sleeping one stays asleep."""
        import json

        argv = [sys.executable, "-m", "modal", "app", "list", "--json"] + (["-e", self._env()] if self._env() else [])
        r = self._run(argv, dict(os.environ))
        if r.returncode != 0:
            raise ProviderError(signed_out(Exception(r.stderr)) or f"Modal could not list apps: {r.stderr.strip()[-300:]}")
        start = prefix.strip("-") + "-"
        out = []
        for a in json.loads(r.stdout or "[]"):
            name = a.get("Description") or a.get("description") or a.get("name")
            if not name or str(a.get("State") or a.get("state") or "").lower() not in ("deployed", "running"):
                continue
            tags = self._app_tags(name) or {}
            if tags.get("app") != "captain" and not str(name).startswith(start):
                continue
            try:
                copies = int(getattr(self._fn(name).get_current_stats(), "num_total_runners", 0) or 0)
            except Exception:  # noqa: BLE001
                copies = None
            size = self._size()
            out.append({"type": "modal-app", "name": name, "region": None, "environment": self._env(),
                        "resources": [{"type": "modal-app", "name": name, "environment": self._env()}],
                        "state": "asleep" if copies == 0 else "running", "reason": None,
                        "machine": None, "copies_now": copies, "copies_min": size.get("min"), "copies_max": size.get("max"),
                        "since": a.get("Created at") or a.get("created_at"), "changed": None,
                        "project": tags.get("project"), "env": tags.get("env"), "stage": tags.get("stage") or tags.get("captain-stage"),
                        "by": tags.get("by"), "by_tag": tags.get("app") == "captain", "by_prefix": str(name).startswith(start)})
        return out

    def tagged(self, project: str, env: str) -> list[dict]:
        """Every deployed app in this Modal environment that carries Captain's tags for this project's staging."""
        import json

        argv = [sys.executable, "-m", "modal", "app", "list", "--json"] + (["-e", self._env()] if self._env() else [])
        r = self._run(argv, dict(os.environ))
        if r.returncode != 0:
            raise ProviderError(signed_out(Exception(r.stderr)) or f"Modal could not list apps: {r.stderr.strip()[-300:]}")
        out = []
        for a in json.loads(r.stdout or "[]"):
            name = a.get("Description") or a.get("description") or a.get("name")
            if not name or str(a.get("State") or a.get("state") or "").lower() not in ("deployed", "running"):
                continue
            tags = self._app_tags(name) or {}
            if tags.get("app") == "captain" and tags.get("project") == clean_name(project, 60) and tags.get("env") == clean_name(env, 60):
                out.append({"type": "modal-app", "name": name, "environment": self._env()})
        return out
