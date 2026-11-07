"""Workspace settings: choices made once and kept in the bucket, so every teammate and every session gets them.

Today it holds the default compute: where backtests run and where pipelines are served, unless a backtest or an
environment names its own. Settings are shared and versioned by compare-and-swap like the other workspace files.
"""
from __future__ import annotations

import datetime as dt

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

KEY = "settings.yaml"
DEFAULTS = {"compute": {"backtest": "local", "serve": None}}


def get(ctx: Context) -> dict:
    try:
        doc = yaml.safe_load(ctx.store.get(KEY)) or {}
    except NotFound:
        doc = {}
    out = yaml.safe_load(yaml.safe_dump(DEFAULTS))
    for k, v in doc.items():
        out[k] = {**out.get(k, {}), **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def set_compute(ctx: Context, backtest: str | None = None, serve: str | None = None, *, clear_serve=False) -> dict:
    from . import compute as C

    for name, purpose in ((backtest, "backtest"), (serve, "serve")):
        if name:
            t = C.get(ctx, name)
            if purpose not in (t.get("purposes") or ["backtest"]):
                raise ValueError(f"{name} can't be used for {purpose}s. Pick a target that lists {purpose}.")
    for _ in range(10):
        try:
            body, tag = ctx.store.get_with_tag(KEY)
            doc = yaml.safe_load(body) or {}
        except NotFound:
            doc, tag = {}, None
        comp = doc.setdefault("compute", {})
        if backtest:
            comp["backtest"] = backtest
        if serve or clear_serve:
            comp["serve"] = serve
        doc["updated_by"], doc["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return get(ctx)
        except Conflict:
            continue
    raise Conflict("the settings kept changing while saving. Try again.")


def default_compute(ctx: Context, purpose: str = "backtest") -> str | None:
    return (get(ctx).get("compute") or {}).get(purpose)
