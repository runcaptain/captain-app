"""Workspace settings: choices made once and kept in the bucket, so every teammate and every session gets them.

It holds the default compute (where backtests run and where pipelines are served, unless a backtest or an
environment names its own) and the cost above which a form asks again before starting work on a cloud. Settings are shared and versioned by compare-and-swap like the other workspace files.
"""
from __future__ import annotations

import datetime as dt

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

KEY = "settings.yaml"
# staging.local_limit_mb: the largest model staged on this computer. A bigger one is staged in the cloud, even when
# this computer has the memory free. 0 means no limit.
DEFAULTS = {"compute": {"backtest": "local", "serve": None}, "costs": {"confirm_above_usd": 5.0},
            "staging": {"local_limit_mb": 1024}}
LOCAL_LIMITS = (250, 500, 1024, 2048, 4096, 0)


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


def set_costs(ctx: Context, confirm_above_usd) -> dict:
    """The estimate above which a form asks again before it starts work on a cloud. 0 asks every time."""
    try:
        v = float(confirm_above_usd)
    except (TypeError, ValueError):
        raise ValueError("the amount to ask above is a number of dollars, like 5") from None
    if v < 0 or v > 100000:
        raise ValueError("the amount to ask above is between 0 and 100,000 dollars")
    for _ in range(10):
        try:
            body, tag = ctx.store.get_with_tag(KEY)
            doc = yaml.safe_load(body) or {}
        except NotFound:
            doc, tag = {}, None
        doc.setdefault("costs", {})["confirm_above_usd"] = v
        doc["updated_by"], doc["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return get(ctx)
        except Conflict:
            continue
    raise Conflict("the settings kept changing while saving. Try again.")


def default_compute(ctx: Context, purpose: str = "backtest") -> str | None:
    return (get(ctx).get("compute") or {}).get(purpose)


def set_serving_image(ctx: Context, public_image: str | None) -> dict:
    """The published Captain serving image to copy into a person's cloud account when serving is set up. None turns
    it off, which is how it ships: no image is published yet."""
    for _ in range(10):
        try:
            body, tag = ctx.store.get_with_tag(KEY)
            doc = yaml.safe_load(body) or {}
        except NotFound:
            doc, tag = {}, None
        doc.setdefault("serving", {})["public_image"] = (public_image or "").strip() or None
        doc["updated_by"], doc["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return get(ctx)
        except Conflict:
            continue
    raise Conflict("the settings kept changing while saving. Try again.")


def set_local_limit(ctx: Context, mb) -> dict:
    """The largest model to stage on this computer, in MB of memory it needs to serve: 250, 500, 1024, 2048, 4096,
    or 0 for no limit."""
    try:
        v = int(float(mb))
    except (TypeError, ValueError):
        raise ValueError("the limit is 250 MB, 500 MB, 1 GB, 2 GB, 4 GB or no limit") from None
    if v not in LOCAL_LIMITS:
        raise ValueError("the limit is 250 MB, 500 MB, 1 GB, 2 GB, 4 GB or no limit")
    for _ in range(10):
        try:
            body, tag = ctx.store.get_with_tag(KEY)
            doc = yaml.safe_load(body) or {}
        except NotFound:
            doc, tag = {}, None
        doc.setdefault("staging", {})["local_limit_mb"] = v
        doc["updated_by"], doc["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return get(ctx)
        except Conflict:
            continue
    raise Conflict("the settings kept changing while saving. Try again.")


def set_serving_machine(ctx: Context, host: str, machine) -> dict:
    """The machine a deploy on this host uses, in place of the smallest the model fits on: an instance type on AWS
    ("ml.m5.large"), a size's name or its memory in MB elsewhere. None or "" clears the choice. A choice the model
    doesn't fit on is refused when the deploy is planned, with the reason (staging/sizing.py)."""
    from .staging import sizing as SZ

    if host not in SZ.WHERE:
        raise ValueError("the host is one of: " + ", ".join(SZ.WHERE))
    value = str(machine or "").strip() or None
    if value is not None:
        names = [str(x) for s in SZ.sizes(host, None) for x in (s["name"], s["memory_mb"])]
        if value not in names:
            raise ValueError(f"{value} is not a size Captain offers on {SZ.WHERE[host]}. Choose one of: "
                             + ", ".join(s["name"] for s in SZ.sizes(host, None)) + ".")
    for _ in range(10):
        try:
            body, tag = ctx.store.get_with_tag(KEY)
            doc = yaml.safe_load(body) or {}
        except NotFound:
            doc, tag = {}, None
        m = doc.setdefault("serving", {}).setdefault("machine", {})
        if value is None:
            m.pop(host, None)
        else:
            m[host] = value
        doc["updated_by"], doc["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return get(ctx)
        except Conflict:
            continue
    raise Conflict("the settings kept changing while saving. Try again.")


def set_serving_prefix(ctx: Context, prefix) -> dict:
    """The start of the name of everything a first deploy makes in a cloud account ("captain" when not set). For
    two workspaces that share one account, or an account where names must start a certain way. It is read when a
    place is set up for the first time: changing it later does not rename what exists."""
    import re

    value = str(prefix or "").strip().lower() or None
    if value is not None and not re.fullmatch(r"[a-z][a-z0-9-]{1,24}[a-z0-9]", value):
        raise ValueError("the name prefix is 3 to 26 lower-case letters, digits and hyphens, starting with a letter")
    for _ in range(10):
        try:
            body, tag = ctx.store.get_with_tag(KEY)
            doc = yaml.safe_load(body) or {}
        except NotFound:
            doc, tag = {}, None
        if value is None:
            doc.setdefault("serving", {}).pop("name_prefix", None)
        else:
            doc.setdefault("serving", {})["name_prefix"] = value
        doc["updated_by"], doc["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return get(ctx)
        except Conflict:
            continue
    raise Conflict("the settings kept changing while saving. Try again.")


def set_alerts(ctx: Context, webhook_url) -> dict:
    """Where a message goes when a deployed model turns unhealthy or recovers: an http(s) address that takes a JSON
    body with `text` (a Slack incoming webhook, PagerDuty, your own). Pass "" to turn it off. The address can hold
    a secret, so it is never printed: `get` returns it, and the screens show only whether one is set."""
    url = str(webhook_url or "").strip()
    if url and not (url.startswith("https://") or url.startswith("http://")):
        raise ValueError("the webhook is an address that starts with https://")
    if len(url) > 2000:
        raise ValueError("that address is too long to be a webhook")
    for _ in range(10):
        try:
            body, tag = ctx.store.get_with_tag(KEY)
            doc = yaml.safe_load(body) or {}
        except NotFound:
            doc, tag = {}, None
        doc.setdefault("alerts", {})["webhook_url"] = url or None
        doc["updated_by"], doc["updated_at"] = whoami(), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return get(ctx)
        except Conflict:
            continue
    raise Conflict("the settings kept changing while saving. Try again.")
