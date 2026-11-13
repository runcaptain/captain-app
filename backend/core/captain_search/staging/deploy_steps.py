"""Two steps every deploy ends with, after the model answers (registered through deploy.after_step).

  match     "Checking it matches training". Compares the numbers the deployed model computes with the ones training
            computed, on the test requests the step before just sent (consistency.check). It is a launch gate, so
            it runs on every deploy and its one-line result is the step's last line. A mismatch fails the step, not
            the deploy: the model stays up, and the deployed model's screen says so at the top.
  publish   "Saving this version to your model registry". Only when the project names a registry (architecture and
            brand). It writes the version there as a candidate. Saving is filing, not approval: nothing here marks
            a version reviewed, human graded or tested on real traffic, and no launch gate reads it. A version
            already saved is not saved twice.

Both are soft: a failure is recorded on the step and in `soft_failed`, and the deploy still ends done.
"""
from __future__ import annotations

from .. import project_state as PS
from ..workspace import Context
from . import ENV, state
from . import deploy as DP


def _match(ctx: Context, pid: str, env: str, say) -> None:
    from . import consistency as SC

    # For a network, which has no ranking features, the same call checks that the deployed copy gives the same
    # answers as the saved version, edits included, on the model's own practice inputs.
    r = SC.check(ctx, pid, env or ENV)
    say(r.get("text") or "The check ran.")
    if r.get("status") == "fail":
        raise ValueError(r.get("text") or "The deployed model doesn't compute the numbers it was trained on.")


def _registry(ctx: Context, pid: str) -> dict | None:
    reg = (PS.get(ctx, pid).get("registry") or {})
    return reg if reg.get("arch") and reg.get("brand") else None


def has_registry(ctx: Context, pid: str) -> bool:
    return _registry(ctx, pid) is not None


def _publish(ctx: Context, pid: str, env: str, say) -> None:
    from .. import model_versions as MV

    reg = _registry(ctx, pid)
    rec = state(ctx, pid, env or ENV) or {}
    version = rec.get("model_version")
    if not reg or not version:
        say("Nothing to save.")
        return
    if rec.get("serves") == "predict":
        say("This is a network kept under Weights, where every version is already saved. Nothing was added to the model registry.")
        return
    saved = (PS.get(ctx, pid).get("registry_saved") or {}).get(version)
    if saved:
        say(f"Already saved as {saved}.")
        return
    # status stays "candidate": saving a version files it, and approves nothing.
    out = MV.publish(ctx, version, reg["arch"], reg["brand"], reg.get("uri") or None, status="candidate")
    where = out.get("path") or out.get("uri") or f"{reg['arch']}/{reg['brand']}"

    def note(doc):
        doc.setdefault("registry_saved", {})[version] = where

    PS._update(ctx, pid, note)
    say(f"Saved as {where}, as a candidate.")


DP.after_step("match", "Checking it matches training", _match)
DP.after_step("publish", "Saving this version to your model registry", _publish, when=has_registry)
