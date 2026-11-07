"""Compute providers: one adapter per place a Captain job can run.

A provider turns a job (run `cst <argv>` once in the runner image, with these environment variables) into calls on
one compute service, and then answers four questions about it: is it done (`status`), what did it print (`logs`),
stop it (`cancel`), and can this target run at all (`test`). Every provider has the same shape, so the app, the CLI
and the job records (jobs.py) never branch on the cloud.

Built-in kinds live in this package. A team adds its own with an entry point in the `captain_search.compute` group,
named by the kind and pointing at a ComputeProvider subclass:

    [project.entry-points."captain_search.compute"]
    slurm = "acme_captain.slurm:SlurmProvider"

The handle that `submit` returns is a JSON-able dict that only its provider reads. Store it, pass it back.
"""
from __future__ import annotations

import dataclasses
from importlib.metadata import entry_points

STATES = ("queued", "running", "succeeded", "failed", "stopped")
TERMINAL = frozenset({"succeeded", "failed", "stopped"})
GROUP = "captain_search.compute"
# The runner image (runner/Dockerfile). `cst compute setup aws-ecs` builds it into the team's own registry instead.
IMAGE_DEFAULT = "ghcr.io/captain/captain-search-runner:0.1.0"


@dataclasses.dataclass
class Job:
    """One run of `cst <argv>` in the runner image. argv already holds `-w <workspace>`."""
    id: str
    argv: list[str]
    label: str
    env: dict[str, str]
    backtest: str | None = None


class ProviderError(ValueError):
    """The compute service refused or failed a call. The message says what it said, in its words."""


def status(state: str, reason: str = "", **extra) -> dict:
    """A status answer. `reason` is a plain sentence a person reads, e.g. "the container exited with code 1"."""
    if state not in STATES:
        raise ValueError(f"unknown job state {state!r}: use {', '.join(STATES)}")
    return {"state": state, "reason": reason, **{k: v for k, v in extra.items() if v is not None}}


class ComputeProvider:
    """Subclass this for a new kind. Set the class attributes, implement the methods that raise NotImplementedError."""

    kind: str = ""
    provider: str = ""  # which cloud login it needs: aws, gcp, azure, or anything else for none
    label: str = ""
    purposes: list[str] = ["backtest"]
    fields: list[dict] = []
    # The last date this kind ran a real job on a real account, or None. Shown in the app, so be honest.
    live_tested: str | None = None
    can_logs = True
    can_cancel = True

    def __init__(self, target: dict, ctx=None):
        self.target = target
        self.config = target.get("config") or {}
        self.region = target.get("region")
        self.profile = target.get("profile")
        self.ctx = ctx

    # --- what to run ---
    def plan(self, job: Job) -> list[dict]:
        """The provider API calls `submit` would make, for --dry-run. No network."""
        raise NotImplementedError

    def submit(self, job: Job) -> dict:
        """Start the job. Returns the handle."""
        raise NotImplementedError

    # --- what happened ---
    def status(self, handle: dict) -> dict:
        """{state: queued|running|succeeded|failed|stopped, reason, started_at?, finished_at?, exit_code?}"""
        raise NotImplementedError

    def logs(self, handle: dict, since: str | None = None) -> dict:
        """{lines: [{ts, message}], next: cursor to pass as `since` for the lines after these, note?}"""
        raise NotImplementedError

    def cancel(self, handle: dict) -> dict:
        """Ask the provider to stop the job. Returns a status (often still running while it stops)."""
        raise NotImplementedError

    def test(self) -> dict:
        """{ok, detail}: can this target run a job right now? Calls the provider, read-only."""
        raise NotImplementedError

    @classmethod
    def describe(cls) -> dict:
        return {"kind": cls.kind, "provider": cls.provider, "label": cls.label, "purposes": list(cls.purposes),
                "fields": [dict(f) for f in cls.fields], "live_tested": cls.live_tested,
                "capabilities": {"logs": cls.can_logs, "cancel": cls.can_cancel}}


REQUIRED = ("submit", "status", "logs", "cancel", "test", "describe", "plan")


def _builtin() -> dict[str, type]:
    from .aws import BatchProvider, ECSProvider, SageMakerProvider
    from .azure import AzureMLProvider, ContainerAppsProvider
    from .gcp import CloudRunProvider, VertexProvider
    from .local import LocalProvider
    from .modal import ModalProvider

    return {c.kind: c for c in (LocalProvider, ModalProvider, BatchProvider, ECSProvider, SageMakerProvider,
                                CloudRunProvider, VertexProvider, ContainerAppsProvider, AzureMLProvider)}


_plugin_errors: dict[str, str] = {}


def classes() -> dict[str, type]:
    """Every provider kind: built in, then entry points. A broken plugin is skipped and named in plugin_errors()."""
    out = _builtin()
    _plugin_errors.clear()
    for ep in entry_points(group=GROUP):
        try:
            cls = ep.load()
            missing = [m for m in REQUIRED if not callable(getattr(cls, m, None))]
            if missing:
                raise TypeError(f"missing {', '.join(missing)}")
            if ep.name in out:
                raise TypeError(f"{ep.name} is a built-in kind")
            if getattr(cls, "kind", "") not in ("", ep.name):
                raise TypeError(f"its kind is {cls.kind!r} but the entry point is named {ep.name!r}")
            cls.kind = ep.name
            out[ep.name] = cls
        except Exception as e:  # noqa: BLE001
            _plugin_errors[ep.name] = f"{ep.value}: {e}"
    return out


def plugin_errors() -> dict[str, str]:
    return dict(_plugin_errors)


def get(target: dict, ctx=None) -> ComputeProvider:
    cls = classes().get(target["kind"])
    if cls is None:
        extra = f" The plugin failed to load: {_plugin_errors[target['kind']]}" if target["kind"] in _plugin_errors else ""
        raise ValueError(f"no compute provider for kind {target['kind']!r}.{extra}")
    return cls(target, ctx)


def env_list(env: dict) -> list[dict]:
    return [{"name": a, "value": b} for a, b in env.items()]


def split_csv(v) -> list[str]:
    return [s.strip() for s in str(v or "").split(",") if s.strip()]
