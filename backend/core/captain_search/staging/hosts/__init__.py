"""Hosts: the places a staged model can run, each behind the same small shape.

A host has two halves. The setup half lives here: what has to exist in the person's account before a model can be
staged there, made once and removed whole. The endpoint half is the host's compute provider
(compute_providers/serving.py): deploy a bundle, ask how it is, call it, read its log, change its copies, remove it.

  plan(ctx, **options)      what setup would create, row by row with a reason each, what it costs (from the shared
                            estimator's prices), and what the login must be allowed to do. Changes nothing.
  setup(ctx, confirm, ...)  create it with the connected login. Safe to run again. A missing permission stops it
                            with the step, the action, and a policy or role to hand to an admin.
  status(ctx)               what setup made, read from the workspace.
  teardown(ctx)             remove exactly what setup made. What could not be removed is named.
  leftovers(ctx)            ask the cloud itself what still carries Captain's name or label there. Empty is the
                            proof that nothing is left.
  describe()                the card: label, the compute kind it makes, whether it sleeps when idle, what the
                            address and its key look like, where the bundle and log are kept, and when it last ran
                            on a real account.

Hosts: aws (SageMaker), modal, gcp (Cloud Run), azure (Container Apps), and local (this computer, nothing to set
up). Every host's record is `serving/setup-<cloud>.yaml` in the workspace. AWS keeps its older `serving/setup.yaml`.
"""
from __future__ import annotations

from .base import Host, MissingPermission  # noqa: F401

ORDER = ("local", "aws", "modal", "gcp", "azure")


def classes() -> dict[str, type]:
    from .aws import AwsHost
    from .azure import AzureHost
    from .gcp import GcpHost
    from .local import LocalHost
    from .modal import ModalHost

    return {c.cloud: c for c in (LocalHost, AwsHost, ModalHost, GcpHost, AzureHost)}


def get(cloud: str | None) -> Host:
    cls = classes().get(cloud or "aws")
    if cls is None:
        raise ValueError(f"Captain can't host on {cloud!r}. It hosts on: {', '.join(ORDER)}.")
    return cls()


def listing(ctx=None) -> list[dict]:
    """Every host as a card, with what is set up there when a workspace is given."""
    out = []
    cs = classes()
    for name in ORDER:
        h = cs[name]()
        d = h.describe()
        if ctx is not None:
            try:
                st = h.status(ctx)
                d["setup"] = {"status": st.get("status"), "target": st.get("target"), "text": st.get("text")}
            except Exception as e:  # noqa: BLE001
                d["setup"] = {"status": "unknown", "target": None, "text": str(e)[:200]}
        out.append(d)
    return out


def of_kind(kind: str) -> Host | None:
    """The host whose endpoints run on this compute kind."""
    for cls in classes().values():
        if cls.kind == kind:
            return cls()
    return None
