"""What a piece of work will cost on a cloud before it runs, and what it did cost after.

One estimator for every place Captain can run work on someone's cloud: training, offline evaluation, synthetic
events, snapshots from a connection, LLM judging and staging endpoints.

    estimate(ctx, kind, where, size) -> {
        low, high, currency, unit: "run" | "hour", basis: [plain sentences], priced_as_of,
        confidence: "rough" | "measured", free: bool, text, ...}

Rules this module keeps:
- "This computer" is free and says so.
- A number never appears without its basis. `basis` lists, in plain sentences, the machine, its price, where the
  price came from and how the time was worked out.
- Prices are read live from the provider's price list when the credentials allow (AWS Price List API). Otherwise
  they come from the bundled table (price_table.py), which carries the date it was read on.
- A provider Captain has never run a real job on gets "No estimate for <provider> yet", not a guess.
- Time comes from a simple model (seconds per row, per query, per session) and, once this workspace has run the
  same kind of work, from how long those runs took. Then confidence is "measured".

Every estimate and every finished run is kept in the workspace (`costs/runs/<id>.yaml`), so later estimates use
them and a project can show what it has cost so far.

Refreshing the bundled AWS table: read FARGATE and SAGEMAKER_HOSTING from the Price List API
(`pricing.get_products`, ServiceCode AmazonECS / AmazonSageMaker, filter regionCode) and set AWS_PRICED_AS_OF.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
import time

import yaml
from ulid import ULID

from . import price_table as PT
from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami

KINDS = ("training", "evaluation", "synthetic-events", "snapshot", "judging", "staging", "workflow")
CONFIRM_ABOVE_USD = 5.0
LOCAL_TEXT = "Runs on this computer. No cloud cost."
WAREHOUSE_TEXT = "Billed by your warehouse. Captain can't estimate it."

# Seconds a cloud container takes to pull the image and start. Fargate bills from the start of the pull.
CLOUD_START_S = 50.0
FARGATE_MIN_S = 60.0  # Fargate bills at least one minute
# The rough model, before this workspace has run the kind of work: (fixed seconds, seconds per unit, unit name).
# Only the starter ranker was timed (the demo store on a laptop, 2026-11-10: 60,000 and 300,000 event rows both
# trained in under 5 s, so start-up dominates). The other two are first guesses and say "rough" until this workspace
# has run them. The low and high ends are ROUGH_SPREAD times the middle.
ROUGH = {
    ("training", "starter"): (15.0, 0.00002, "event rows"),
    ("evaluation", None): (10.0, 0.06, "query and version pairs"),
    ("synthetic-events", None): (5.0, 0.0015, "shopper sessions"),
}
ROUGH_SPREAD = (0.6, 2.0)
ECS_ASSUMED = (4.0, 16.0)  # vCPU and GB, when the task definition can't be read

# LLM judging (eval/judge.py): one call grades one query's products, up to PER_CALL of them.
JUDGE_SYSTEM_TOKENS = 260
JUDGE_TOKENS_PER_PRODUCT = (60, 260)  # a short title, or a title with a long description
JUDGE_OUT_TOKENS_PER_PRODUCT = 8
JUDGE_OUT_FIXED = 20

_price_cache: dict[tuple, tuple[float, object]] = {}
_PRICE_TTL_S = 6 * 3600


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


# ---- words ---------------------------------------------------------------------------------------------------


def money(x: float) -> str:
    if x < 0.01:
        return "less than $0.01"
    return f"${x:,.2f}"


def money_range(low: float, high: float) -> str:
    if high < 0.01:
        return "Less than $0.01"
    if low < 0.01:
        return f"Up to {money(high)}"
    a, b = money(low), money(high)
    return f"About {a}" if a == b else f"About {a} to {b}"


def _mins(s: float) -> int:
    return max(1, int(round(s / 60)))


def minutes_range(low_s: float, high_s: float) -> str:
    if high_s < 60:
        return "under a minute"
    a, b = _mins(low_s), _mins(high_s)
    if b >= 120:
        ha, hb = round(low_s / 3600, 1), round(high_s / 3600, 1)
        return f"about {hb:g} hours" if ha == hb else f"{ha:g} to {hb:g} hours"
    return f"about {a} min" if a == b else f"{a} to {b} min"


def duration_words(seconds: float) -> str:
    if seconds < 60:
        return f"{max(1, int(round(seconds)))} s"
    if seconds < 7200:
        return f"{_mins(seconds)} min"
    return f"{seconds / 3600:.1f} hours"


# ---- settings ------------------------------------------------------------------------------------------------


def threshold(ctx: Context) -> float:
    """Above this estimate a form asks again before it starts. Set in Settings (settings.yaml, costs)."""
    from . import settings as ST

    try:
        v = float((ST.get(ctx).get("costs") or {}).get("confirm_above_usd"))
        return v if v >= 0 else CONFIRM_ABOVE_USD
    except (TypeError, ValueError):
        return CONFIRM_ABOVE_USD


# ---- prices --------------------------------------------------------------------------------------------------


def _pricing_client(target: dict):
    """The AWS Price List API, with the target's own login. It lives in us-east-1 whatever the target's region."""
    from . import cloud

    return cloud.aws_session(target.get("profile"), "us-east-1").client("pricing")


def _products(client, code: str, filters: dict) -> list[dict]:
    import json

    out, token = [], None
    for _ in range(20):
        kw = {"ServiceCode": code, "Filters": [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]}
        if token:
            kw["NextToken"] = token
        r = client.get_products(**kw)
        out += [json.loads(p) if isinstance(p, str) else p for p in r.get("PriceList", [])]
        token = r.get("NextToken")
        if not token:
            break
    return out


def _usd(product: dict) -> float | None:
    for term in (product.get("terms") or {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            try:
                return float(dim["pricePerUnit"]["USD"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _cached(key: tuple, read):
    hit = _price_cache.get(key)
    if hit and time.time() - hit[0] < _PRICE_TTL_S:
        return hit[1]
    val = read()
    _price_cache[key] = (time.time(), val)
    return val


def _live_fargate(target: dict) -> dict | None:
    def read():
        out = {}
        for p in _products(_pricing_client(target), "AmazonECS", {"regionCode": target["region"]}):
            u = (p.get("product") or {}).get("attributes", {}).get("usagetype", "")
            if "ARM" in u or "Windows" in u:
                continue
            if u.endswith("Fargate-vCPU-Hours:perCPU"):
                out["vcpu_hour"] = _usd(p)
            elif u.endswith("Fargate-GB-Hours"):
                out["gb_hour"] = _usd(p)
        return out if out.get("vcpu_hour") and out.get("gb_hour") else None

    return _cached(("fargate", target.get("profile"), target["region"]), read)


def _live_sagemaker(target: dict, instance: str) -> float | None:
    def read():
        for p in _products(_pricing_client(target), "AmazonSageMaker", {"regionCode": target["region"], "instanceName": instance}):
            if (p.get("product") or {}).get("attributes", {}).get("component") == "Hosting":
                v = _usd(p)
                if v:
                    return v
        return None

    return _cached(("sagemaker", target.get("profile"), target["region"], instance), read)


def fargate_price(target: dict) -> dict | None:
    """{vcpu_hour, gb_hour, priced_as_of, source: live|bundled} for the target's region, or None when neither the
    live price list nor the bundled table has it."""
    region = target.get("region")
    if not region:
        return None
    try:
        live = _live_fargate(target)
    except Exception:  # noqa: BLE001  no pricing permission, no network: the bundled table answers
        live = None
    if live:
        return {**live, "priced_as_of": _today(), "source": "live"}
    t = PT.FARGATE.get(region)
    return {**t, "priced_as_of": PT.AWS_PRICED_AS_OF, "source": "bundled"} if t else None


def sagemaker_price(target: dict, instance: str) -> dict | None:
    region = target.get("region")
    if not region:
        return None
    try:
        live = _live_sagemaker(target, instance)
    except Exception:  # noqa: BLE001
        live = None
    if live:
        return {"hour": live, "priced_as_of": _today(), "source": "live"}
    t = (PT.SAGEMAKER_HOSTING.get(region) or {}).get(instance)
    return {"hour": t, "priced_as_of": PT.AWS_PRICED_AS_OF, "source": "bundled"} if t else None


def _price_words(p: dict) -> str:
    if p["source"] == "live":
        return f"Prices read from AWS's own price list today ({p['priced_as_of']})."
    return f"Prices from Captain's bundled AWS table, as of {p['priced_as_of']}. AWS's live price list couldn't be read with this login."


# ---- the machine a target runs on ----------------------------------------------------------------------------


def _ecs_size(ctx: Context, target: dict) -> tuple[float, float] | None:
    """vCPU and GB of the target's task definition, asked from ECS. None when it can't be read."""
    def read():
        from . import cloud

        ecs = cloud.aws_session(target.get("profile"), target.get("region")).client("ecs")
        td = ecs.describe_task_definition(taskDefinition=target["config"]["task_definition"])["taskDefinition"]
        return float(td["cpu"]) / 1024, float(td["memory"]) / 1024

    try:
        return _cached(("ecs-size", target.get("profile"), target.get("region"), (target.get("config") or {}).get("task_definition")), read)
    except Exception:  # noqa: BLE001
        return None


def machine(ctx: Context, target: dict, size: dict | None = None) -> dict:
    """What one hour on this target costs: {hourly, label, basis, priced_as_of, price_source}, or {none: sentence}
    when Captain can't price it."""
    from . import compute as C

    kind, cfg, region = target.get("kind"), target.get("config") or {}, target.get("region")
    spec = C.kind_map().get(kind) or {}
    label = spec.get("label") or kind
    # A host that sleeps when idle is priced once a model has been staged on it on a real account, and not before.
    if kind in SLEEPERS and (size or {}).get("_staging") and (spec.get("serving") or {}).get("live_tested"):
        return _sleeper(target, spec)
    if target.get("provider") != "aws" or not spec.get("live_tested"):
        return {"none": f"No estimate for {label} yet."}
    if kind in ("aws-ecs", "aws-batch"):
        basis = []
        if kind == "aws-batch":
            vcpu, gb = float(cfg.get("vcpus") or 4), float(cfg.get("memory_mb") or 16384) / 1024
            basis.append("Priced as Fargate. A Batch queue on EC2 instances bills those instances instead.")
        else:
            got = _ecs_size(ctx, target)
            vcpu, gb = got or ECS_ASSUMED
            if not got:
                basis.append(f"Captain couldn't read the task definition's size, so this assumes {vcpu:g} vCPU and {gb:g} GB.")
        p = fargate_price(target)
        if not p:
            return {"none": f"No estimate for {label} in {region or 'this region'} yet: Captain has no price for it."}
        hourly = vcpu * p["vcpu_hour"] + gb * p["gb_hour"]
        name = f"{vcpu:g} vCPU, {gb:g} GB on AWS Fargate"
        return {"hourly": hourly, "label": name, "priced_as_of": p["priced_as_of"], "price_source": p["source"],
                "min_seconds": FARGATE_MIN_S,
                "basis": [f"{name} in {region}: ${hourly:.4f} an hour (${p['vcpu_hour']:.5f} per vCPU hour, "
                          f"${p['gb_hour']:.5f} per GB hour).", *basis, _price_words(p)]}
    if kind == "aws-sagemaker":
        inst = str((size or {}).get("instance_type") or cfg.get("instance_type") or "ml.m5.large")
        n = int((size or {}).get("instances") or cfg.get("instance_count") or 1)
        p = sagemaker_price(target, inst)
        if not p:
            return {"none": f"No estimate for {inst} in {region or 'this region'} yet: Captain has no price for it."}
        hourly = n * p["hour"]
        name = f"{n} x {inst} on SageMaker" if n > 1 else f"{inst} on SageMaker"
        return {"hourly": hourly, "label": name, "priced_as_of": p["priced_as_of"], "price_source": p["source"],
                "min_seconds": 0.0,
                "basis": [f"{name} in {region}: ${hourly:.4f} an hour (${p['hour']:.4f} per instance).", _price_words(p)]}
    return {"none": f"No estimate for {label} yet."}


SLEEPERS = ("modal-endpoint", "gcp-cloud-run", "azure-container-apps")


def _sleeper(target: dict, spec: dict) -> dict:
    """A host that bills by the second a copy is awake. `hourly` is one copy awake for an hour; `kept_hourly` is
    what the copies kept up while idle cost (0 when it sleeps). From list prices (price_table.py)."""
    from . import price_table as PT

    kind, cfg, region = target.get("kind"), target.get("config") or {}, target.get("region")

    def num(k, d):
        try:
            return float(cfg.get(k) or d)
        except (TypeError, ValueError):
            return float(d)

    cpu, gb, kept = num("cpu", 1), num("memory_mb", 2048) / 1024, int(num("min_copies", 0))
    if kind == "modal-endpoint":
        hourly = 3600 * (cpu * PT.MODAL["core_second"] + gb * PT.MODAL["gib_second"])
        idle = hourly  # a copy kept up on Modal bills as an awake one
        name, where = f"{cpu:g} CPU, {gb:g} GB on Modal", "Modal"
    elif kind == "gcp-cloud-run":
        p = PT.CLOUD_RUN
        hourly = 3600 * (cpu * p["vcpu_second"] + gb * p["gib_second"])
        idle = 3600 * (cpu * p["idle_vcpu_second"] + gb * p["idle_gib_second"])
        name, where = f"{cpu:g} vCPU, {gb:g} GB on Cloud Run", f"Cloud Run in {region}"
    else:
        p = PT.CONTAINER_APPS.get(str(region or "").lower().replace(" ", ""), PT.CONTAINER_APPS["default"])
        hourly = 3600 * (cpu * p["vcpu_second"] + gb * p["gib_second"])
        idle = 3600 * (cpu * p["idle_vcpu_second"] + gb * p["idle_gib_second"])
        name, where = f"{cpu:g} vCPU, {gb:g} GB on Container Apps", f"Container Apps in {region}"
    proven = (spec.get("serving") or {}).get("live_tested")
    return {"hourly": hourly, "kept_hourly": kept * idle, "kept": kept, "sleeps": True, "label": name,
            "priced_as_of": PT.SERVERLESS_PRICED_AS_OF, "price_source": "bundled", "min_seconds": 0.0,
            "basis": [f"{name}: ${hourly:.4f} an hour for each copy while it is awake, on {where}.",
                      f"List prices as of {PT.SERVERLESS_PRICED_AS_OF}. Free monthly allowances are not counted.",
                      *([] if proven else ["Captain has not run a model on this host on a real account yet, so this is the list price only."])]}


# ---- how long ------------------------------------------------------------------------------------------------


def units(kind: str, size: dict) -> tuple[float | None, str | None]:
    """The one number that drives how long this kind of work takes, and what it counts."""
    s = size or {}

    def num(*keys):
        for k in keys:
            try:
                v = float(s.get(k))
                if v > 0:
                    return v
            except (TypeError, ValueError):
                continue
        return None

    if kind == "training":
        if (s.get("how") or "starter") != "starter":
            return num("rows"), "rows"
        rows = num("rows")
        if rows is None and num("sessions"):
            rows = num("sessions") * 5  # a simulated session is about five impression rows
        if rows is None and num("queries"):
            rows = num("queries") * 50  # judgments only: the top 50 of each query
        return rows, "event rows"
    if kind == "evaluation":
        q = num("queries")
        return (q * (num("arms") or 2), "query and version pairs") if q else (None, None)
    if kind == "synthetic-events":
        return num("sessions"), "shopper sessions"
    return None, None


def _how(kind: str, size: dict) -> str | None:
    return ((size or {}).get("how") or "starter") if kind == "training" else None


def _fit(ctx: Context, kind: str, how: str | None, cloud: bool) -> dict | None:
    """Seconds per unit from this workspace's earlier runs of the same kind. Cloud runs are used for a cloud
    estimate; runs on this computer only when there are no cloud ones (and then the estimate stays rough)."""
    rows = [r for r in ledger(ctx) if r.get("kind") == kind and r.get("how") == how
            and (r.get("actual") or {}).get("seconds") and (r.get("units") or 0) > 0 and (r.get("actual") or {}).get("state", "succeeded") == "succeeded"]
    on_cloud = [r for r in rows if r.get("target_kind") not in (None, "local")]
    use, measured = (on_cloud, True) if (on_cloud and cloud) else (rows, not cloud)
    if not use:
        return None
    use = use[:20]
    fixed = ROUGH.get((kind, how), (0.0, 0.0, ""))[0]
    start = CLOUD_START_S if use is on_cloud or use == on_cloud else 0.0
    # The part of a run that doesn't grow with its size (starting the machine, loading the workspace): never more
    # than the shortest run seen took in all.
    over = min(start + fixed, min(r["actual"]["seconds"] for r in use))
    rates = [max(0.0, r["actual"]["seconds"] - over) / r["units"] for r in use]
    mid = statistics.median(rates)
    lo, hi = (min(rates), max(rates)) if len(rates) >= 3 else (mid * 0.75, mid * 1.35)
    return {"low": lo, "mid": mid, "high": hi, "n": len(rates), "measured": measured, "overhead": over,
            "on_cloud": bool(start)}


def duration(ctx: Context, kind: str, size: dict, cloud: bool) -> dict | None:
    """{low_s, high_s, confidence, basis} for this work, or None when nothing says how long it takes."""
    how = _how(kind, size)
    n, what = units(kind, size)
    start = CLOUD_START_S if cloud else 0.0
    fit = _fit(ctx, kind, how, cloud) if n else None
    if fit:
        # Runs timed on this computer still need a machine started for them on a cloud.
        over = fit["overhead"] + (start if not fit["on_cloud"] else 0.0)
        low, high = over + n * fit["low"], over + n * fit["high"]
        runs = f"{fit['n']} earlier run{'s' if fit['n'] != 1 else ''} of this kind in this workspace"
        if fit["measured"]:
            return {"low_s": low, "high_s": high, "confidence": "measured",
                    "basis": [f"Time from {runs}, scaled to {n:,.0f} {what}."]}
        return {"low_s": low * ROUGH_SPREAD[0] / 0.75, "high_s": high * ROUGH_SPREAD[1] / 1.35, "confidence": "rough",
                "basis": [f"Time from {runs} on this computer, scaled to {n:,.0f} {what}. A cloud machine can be "
                          "faster or slower, so the range is wide."]}
    model = ROUGH.get((kind, how))
    if not model or not n:
        return None
    fixed, per, _ = model
    mid = fixed + n * per
    return {"low_s": start + mid * ROUGH_SPREAD[0], "high_s": start + mid * ROUGH_SPREAD[1], "confidence": "rough",
            "basis": [f"Time from Captain's own timings of this kind of work, scaled to {n:,.0f} {what}. It gets "
                      "closer once this workspace has run it."]}


# ---- the estimate --------------------------------------------------------------------------------------------


def _base(kind: str, where: str, label: str) -> dict:
    return {"kind": kind, "where": where, "where_label": label, "free": False, "available": False, "low": None,
            "high": None, "currency": "USD", "unit": "run", "basis": [], "priced_as_of": None, "price_source": None,
            "confidence": "rough", "minutes_low": None, "minutes_high": None, "machine": None, "hourly": None,
            "text": "", "confirm": False, "threshold": None}


def _target(ctx: Context, where) -> dict:
    from . import compute as C

    if isinstance(where, dict):
        return where
    return C.get(ctx, where or "local")


def estimate(ctx: Context, kind: str, where=None, size: dict | None = None) -> dict:
    """What `kind` of work of this `size` costs on `where`: a compute target's name (or its record), "local" for
    this computer, a connection's name for a snapshot, a model id for LLM judging."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: use {', '.join(KINDS)}")
    size = dict(size or {})
    if kind == "snapshot":
        return _snapshot(ctx, where, size)
    if kind == "judging":
        return _judging(ctx, where, size)
    target = _target(ctx, where)
    name = target.get("name") or "local"
    out = _base(kind, name, "This computer" if target.get("kind") == "local" else name)
    out["threshold"] = threshold(ctx)
    if target.get("kind") == "local":
        d = duration(ctx, kind, size, cloud=False)
        return {**out, "free": True, "available": True, "low": 0.0, "high": 0.0, "confidence": d["confidence"] if d else "rough",
                "unit": "hour" if kind == "staging" else "run",
                "minutes_low": round(d["low_s"] / 60, 1) if d else None, "minutes_high": round(d["high_s"] / 60, 1) if d else None,
                "basis": [LOCAL_TEXT, *(d["basis"] if d else [])], "text": LOCAL_TEXT}
    if target.get("kind") != "local" and ctx.workspace.storage_uri.split("://")[0] not in ("s3", "gs", "gcs", "az", "azure") and kind != "staging":
        out["basis"].append("This workspace is on this computer's disk. Work on a cloud needs a cloud workspace, "
                            "so it can't run there yet.")
    m = machine(ctx, target, {**size, "_staging": True} if kind == "staging" else size)
    if m.get("sleeps") and kind == "staging":
        out.update(machine=m["label"], hourly=round(m["hourly"], 6), priced_as_of=m["priced_as_of"], price_source=m["price_source"])
        awake, kept = m["hourly"], m["kept_hourly"]
        asleep = (f"about {money(kept * 24)} a day for the {m['kept']} kept up while idle" if kept else "nothing while it sleeps")
        return {**out, "available": True, "unit": "hour", "low": round(kept, 4), "high": round(awake, 4), "confidence": "rough",
                "sleeps": True, "basis": [*m["basis"], "It bills only for the seconds a copy is awake."
                                          + ("" if kept else " With no requests it costs nothing.")],
                "confirm": max(awake, kept) * 24 >= out["threshold"],
                "text": f"About {money(awake)} an hour while a copy is awake on {name}, {asleep}. Estimate, prices as of {m['priced_as_of']}."}
    if m.get("none"):
        return {**out, "text": m["none"], "basis": [*out["basis"], m["none"],
                                                    "Captain only estimates where it has run real jobs. Today that is AWS."]}
    out.update(machine=m["label"], hourly=round(m["hourly"], 6), priced_as_of=m["priced_as_of"], price_source=m["price_source"])
    stamp = f"Estimate, prices as of {m['priced_as_of']}."
    if kind == "staging":
        hourly = m["hourly"]
        day = hourly * 24
        return {**out, "available": True, "unit": "hour", "low": round(hourly, 4), "high": round(hourly, 4),
                "confidence": "measured" if m["price_source"] == "live" else "rough",
                "basis": [*m["basis"], f"It bills for every hour it is up, about {money(day)} a day, until you take it down."],
                "confirm": day >= out["threshold"],
                "text": f"About {money(hourly)} an hour on {name}, about {money(day)} a day while it is up. {stamp}"}
    d = duration(ctx, kind, size, cloud=True)
    if d is None:
        why = ("Captain can't tell how long your own training command runs until it has run once here."
               if kind == "training" else "Captain can't tell how long this runs until it has run once here.")
        # No length to price, so the question is asked by the day: above the threshold, a form asks again.
        return {**out, "available": True, "unit": "hour", "low": round(m["hourly"], 4), "high": round(m["hourly"], 4),
                "confirm": m["hourly"] * 24 >= out["threshold"],
                "basis": [*out["basis"], *m["basis"], why],
                "text": f"About {money(m['hourly'])} an hour on {name}. {why} {stamp}"}
    lo_s, hi_s = max(d["low_s"], m["min_seconds"]), max(d["high_s"], m["min_seconds"])
    low, high = m["hourly"] * lo_s / 3600, m["hourly"] * hi_s / 3600
    return {**out, "available": True, "low": round(low, 4), "high": round(high, 4), "confidence": d["confidence"],
            "minutes_low": round(lo_s / 60, 1), "minutes_high": round(hi_s / 60, 1),
            "basis": [*out["basis"], *m["basis"], *d["basis"],
                      f"That is {minutes_range(lo_s, hi_s)} including the time to start the machine, which is billed "
                      "too." + (" Fargate bills at least one minute." if m["min_seconds"] else "")],
            "confirm": high >= out["threshold"],
            "text": f"{money_range(low, high)}, {minutes_range(lo_s, hi_s)} on {name}. {stamp}"}


def _snapshot(ctx: Context, where, size: dict) -> dict:
    """A snapshot reads a connection. The warehouse bills the query, not Captain's cloud. BigQuery can say how many
    bytes a query scans without running it. Snowflake and Databricks can't do that cheaply."""
    out = _base("snapshot", str(where or ""), str(where or "your warehouse"))
    if not where:
        return {**out, "free": True, "available": True, "low": 0.0, "high": 0.0, "basis": [LOCAL_TEXT], "text": LOCAL_TEXT}
    engine = None
    try:
        conn = ctx.load_connection(str(where))
        engine = conn.engine
    except Exception:  # noqa: BLE001
        conn = None
    sql = str(size.get("sql") or "").strip()
    if conn is not None and engine == "bigquery" and sql:
        try:
            scanned = _bigquery_dry_run(conn, sql)
        except Exception as e:  # noqa: BLE001
            scanned = None
            out["basis"].append(f"BigQuery wouldn't price the query without running it: {str(e)[:200]}")
        if scanned is not None:
            gb = scanned / 1e9
            words = f"{gb:,.2f} GB" if gb >= 0.01 else f"{scanned / 1e6:,.1f} MB"
            text = f"BigQuery says this query scans {words}. It bills by bytes scanned, at your own rate."
            return {**out, "available": True, "bytes_scanned": int(scanned), "confidence": "measured",
                    "basis": [*out["basis"], text, "The number is BigQuery's own dry run of the query. Captain adds no price to it, "
                              "because on-demand and reserved rates differ."], "text": text}
    return {**out, "basis": [*out["basis"], WAREHOUSE_TEXT,
                             "Snowflake and Databricks can't price a query without running it."
                             if engine in ("snowflake", "databricks") else "The copy itself is made by Captain on this computer, at no cloud cost."],
            "text": WAREHOUSE_TEXT}


def _bigquery_dry_run(conn, sql: str) -> int:
    from google.cloud import bigquery  # the BigQuery driver brings it

    from .connectors.sql import open_engine

    eng = open_engine(conn)
    raw = eng.raw_connection()
    try:
        client = getattr(getattr(raw, "driver_connection", None) or raw.connection, "_client")
        job = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
        return int(job.total_bytes_processed)
    finally:
        raw.close()


def _model_id(where) -> str:
    import os

    from . import llm

    where = None if where in ("local", "") else where  # a form's place picker says "local"; the judge is a model
    m = str(where or os.environ.get("CST_MODEL") or llm.DEFAULT_MODEL)
    m = m.split(":", 1)[1] if m.startswith("llm:") else m
    return m.rsplit("/", 1)[-1].replace(".", "-")


def judge_tokens(pairs: int, per_call: int = 20) -> dict:
    """Tokens an LLM judging run sends and gets back, low and high, from the judge's own prompt shape
    (eval/judge.py): one call holds one query and up to `per_call` products."""
    calls = max(1, math.ceil(pairs / per_call))
    lo = calls * JUDGE_SYSTEM_TOKENS + pairs * JUDGE_TOKENS_PER_PRODUCT[0]
    hi = calls * JUDGE_SYSTEM_TOKENS + pairs * JUDGE_TOKENS_PER_PRODUCT[1]
    out = calls * JUDGE_OUT_FIXED + pairs * JUDGE_OUT_TOKENS_PER_PRODUCT
    return {"calls": calls, "input_low": lo, "input_high": hi, "output": out}


def _judging(ctx: Context, where, size: dict) -> dict:
    model = _model_id(where)
    out = _base("judging", model, model)
    out["threshold"] = threshold(ctx)
    try:
        pairs = int(size.get("pairs") or 0) or int(float(size.get("queries") or 0) * float(size.get("depth") or 10) * float(size.get("arms") or 1))
    except (TypeError, ValueError):
        pairs = 0
    price = PT.LLM.get(model)
    if not price:
        return {**out, "text": f"No estimate for {model} yet.",
                "basis": [f"Captain has no price for {model}. It has prices for {', '.join(PT.LLM)}."]}
    if pairs <= 0:
        return {**out, "text": "Billed by your model provider, by tokens. Choose the queries to see an estimate.",
                "basis": [f"{model}: ${price[0]:g} per million input tokens, ${price[1]:g} per million output tokens, as of {PT.LLM_PRICED_AS_OF}."]}
    t = judge_tokens(pairs)
    low = (t["input_low"] * price[0] + t["output"] * price[1]) / 1e6
    high = (t["input_high"] * price[0] + t["output"] * price[1]) / 1e6
    stamp = f"Estimate, prices as of {PT.LLM_PRICED_AS_OF}."
    return {**out, "available": True, "low": round(low, 4), "high": round(high, 4), "priced_as_of": PT.LLM_PRICED_AS_OF,
            "price_source": "bundled", "tokens": t, "confirm": high >= out["threshold"],
            "basis": [f"{pairs:,} query and product pairs in {t['calls']:,} calls to {model}.",
                      f"About {t['input_low']:,} to {t['input_high']:,} tokens sent and {t['output']:,} back. Short product "
                      "text is the low end, long descriptions the high end.",
                      f"{model}: ${price[0]:g} per million input tokens, ${price[1]:g} per million output tokens, from "
                      f"Captain's bundled table as of {PT.LLM_PRICED_AS_OF}.",
                      "Billed by your model provider on your own key. Pairs already graded are reused and cost nothing."],
            "text": f"{money_range(low, high)} in model tokens for {pairs:,} pairs on {model}. {stamp}"}


# ---- the ledger: every estimate, and what the run then cost ---------------------------------------------------


def _key(run_id: str) -> str:
    return f"costs/runs/{run_id}.yaml"


def new_run_id() -> str:
    return "run-" + str(ULID()).lower()


def _read(ctx: Context, run_id: str) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(_key(run_id))
        return yaml.safe_load(body) or {}, tag
    except NotFound:
        return {}, None


def _change(ctx: Context, run_id: str, change) -> dict:
    for _ in range(5):
        doc, tag = _read(ctx, run_id)
        change(doc)
        try:
            ctx.store.put_if(_key(run_id), dump_yaml(doc), tag)
            return doc
        except Conflict:
            continue
    raise Conflict(f"the cost record {run_id} kept changing while saving")


def note_estimate(ctx: Context, run_id: str, est: dict | None, *, kind: str, size: dict | None = None,
                  project: str | None = None, title: str | None = None, target: dict | None = None) -> dict:
    """Keep what was estimated when a run starts. `run_id` is the job's id for work on a compute target."""
    size = dict(size or {})
    n, what = units(kind, size)

    def ch(doc):
        doc.update(id=run_id, kind=kind, how=_how(kind, size), project=project or doc.get("project"), title=title,
                   where=(target or {}).get("name") or (est or {}).get("where") or "local",
                   target_kind=(target or {}).get("kind") or ("local" if (est or {}).get("free") else None),
                   region=(target or {}).get("region"), size=size, units=n, unit_name=what,
                   created_at=doc.get("created_at") or _now(), by=whoami())
        if est:
            doc["estimate"] = {k: est.get(k) for k in ("low", "high", "unit", "minutes_low", "minutes_high", "confidence",
                                                       "priced_as_of", "price_source", "machine", "text")}

    return _change(ctx, run_id, ch)


def cost_of(ctx: Context, target: dict, seconds: float, staging: bool = False) -> dict:
    """What `seconds` on this target cost: {usd, hourly_usd, machine, priced_as_of, price_source, text}. usd is None
    when Captain can't price the target; on this computer it is 0."""
    where = target.get("name") or "local"
    if target.get("kind") == "local":
        return {"usd": 0.0, "free": True, "seconds": round(seconds, 1), "where": where,
                "text": f"No cloud cost ({duration_words(seconds)} on this computer)"}
    m = machine(ctx, target, {"_staging": True} if staging else None)
    if m.get("none"):
        return {"usd": None, "free": False, "seconds": round(seconds, 1), "where": where, "note": m["none"],
                "text": f"{duration_words(seconds)} on {where}. {m['none']}"}
    if m.get("sleeps"):
        # Captain knows how long it was staged, not how long a copy was awake: the most it can have cost is every
        # second awake. The provider's bill has the real figure.
        most = m["hourly"] * seconds / 3600
        return {"usd": round(most, 4), "free": False, "seconds": round(seconds, 1), "where": where, "upper_bound": True,
                "hourly_usd": round(m["hourly"], 6), "machine": m["label"], "priced_as_of": m["priced_as_of"], "price_source": m["price_source"],
                "text": f"Cost at most {'$0.01' if most < 0.01 else money(most)} (deployed for {duration_words(seconds)} on {where}, billed only while a copy was awake)",
                "detail": f"{m['label']}, ${m['hourly']:.4f} an hour awake, prices as of {m['priced_as_of']}. This is the ceiling: "
                          "the whole time it was deployed, as if one copy never slept."}
    billed = max(seconds, m["min_seconds"])
    usd = m["hourly"] * billed / 3600
    return {"usd": round(usd, 4), "free": False, "seconds": round(seconds, 1), "billed_seconds": round(billed, 1),
            "where": where, "hourly_usd": round(m["hourly"], 6), "machine": m["label"], "priced_as_of": m["priced_as_of"],
            "price_source": m["price_source"],
            "text": f"Cost {'under $0.01' if usd < 0.01 else 'about ' + money(usd)} ({duration_words(billed)} on {where})",
            "detail": f"{m['label']}, ${m['hourly']:.4f} an hour, prices as of {m['priced_as_of']}. Computed from the run "
                      "time. AWS reports no cost for a single task, so check Cost Explorer for the billed figure."}


def note_actual(ctx: Context, run_id: str, target: dict, seconds: float, *, state: str = "succeeded",
                provider_usd: float | None = None, kind: str | None = None, size: dict | None = None,
                project: str | None = None, title: str | None = None) -> dict:
    """Record what a finished run took and cost, beside its estimate. Returns the cost ({usd, text, ...})."""
    cost = cost_of(ctx, target, seconds, staging=kind == "staging")
    cost.update(state=state, provider_usd=provider_usd, at=_now())

    def ch(doc):
        if not doc.get("id"):
            sz = dict(size or {})
            n, what = units(kind or "workflow", sz)
            doc.update(id=run_id, kind=kind or "workflow", how=_how(kind or "workflow", sz), size=sz, units=n,
                       unit_name=what, created_at=_now(), by=whoami(), title=title)
        elif size:  # the run knows its real size better than the estimate did (rows actually built)
            doc["size"] = {**(doc.get("size") or {}), **size}
            doc["units"], doc["unit_name"] = units(doc["kind"], doc["size"])
        doc.update(where=target.get("name") or "local", target_kind=target.get("kind"), region=target.get("region"),
                   project=project or doc.get("project"), actual=cost)

    _change(ctx, run_id, ch)
    return cost


JOB_KINDS = {"retrain": "training", "starter-train": "training", "backtest": "evaluation", "job": "workflow"}


def job_cost(ctx: Context, rec: dict) -> dict | None:
    """The cost of a finished compute job (jobs.py calls this once, when the job reaches a final state). Uses the
    provider's own start and stop times. Returns None for a job that never ran."""
    from . import compute as C
    from . import jobs as J

    a, b = rec.get("billed_from") or rec.get("started_at"), rec.get("finished_at")
    if not a or not b:
        return None
    try:
        seconds = max(0.0, (dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds())
        target = C.get(ctx, rec["target"])
    except (ValueError, NotFound):
        return None
    meta = rec.get("meta") or {}
    size = dict(meta.get("size") or {})
    try:
        res = J.result(ctx, rec["id"]) or {}
        size.update(res.get("size") or {})
    except Exception:  # noqa: BLE001
        pass
    return note_actual(ctx, rec["id"], target, seconds, state=rec.get("state") or "succeeded",
                       kind=meta.get("cost_kind") or JOB_KINDS.get(rec.get("kind"), "workflow"), size=size or None,
                       project=meta.get("project"), title=rec.get("label"))


def ledger(ctx: Context, project: str | None = None, limit: int = 400) -> list[dict]:
    """Recorded runs, newest first."""
    keys = [k for k in ctx.store.list("costs/runs/") if k.endswith(".yaml")]
    out = []
    for k in keys:
        try:
            d = yaml.safe_load(ctx.store.get(k)) or {}
        except NotFound:
            continue
        if project and d.get("project") != project:
            continue
        out.append(d)
    out.sort(key=lambda d: str((d.get("actual") or {}).get("at") or d.get("created_at") or ""), reverse=True)
    return out[:limit]


def get(ctx: Context, run_id: str) -> dict | None:
    doc, _ = _read(ctx, run_id)
    return doc or None


def project_total(ctx: Context, pid: str) -> dict:
    """What a project's runs on the cloud have cost so far, from the recorded actuals."""
    rows = [r for r in ledger(ctx, project=pid) if r.get("actual")]
    cloud = [r for r in rows if r.get("target_kind") not in (None, "local")]
    priced = [r for r in cloud if r["actual"].get("usd") is not None]
    usd = round(sum(r["actual"]["usd"] for r in priced), 4)
    unpriced = len(cloud) - len(priced)
    if not cloud:
        text = "Nothing in this project has run on your cloud yet."
    elif not priced:
        text = (f"{unpriced} run{'s' if unpriced != 1 else ''} in this project ran on your cloud, where Captain has "
                "no price.")
    else:
        text = f"This project has cost {'under $0.01' if usd < 0.01 else 'about ' + money(usd)} on your cloud so far"
        text += f" ({len(priced)} run{'s' if len(priced) != 1 else ''})."
        if unpriced:
            text += f" {unpriced} more ran where Captain has no price."
    return {"project": pid, "usd": usd, "runs": len(priced), "unpriced_runs": unpriced, "local_runs": len(rows) - len(cloud),
            "text": text, "recent": [{"id": r["id"], "kind": r.get("kind"), "title": r.get("title"), "where": r.get("where"),
                                      "text": r["actual"].get("text"), "usd": r["actual"].get("usd"), "at": r["actual"].get("at"),
                                      "estimate": (r.get("estimate") or {}).get("text")} for r in rows[:10]]}
