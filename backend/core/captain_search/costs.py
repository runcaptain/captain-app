"""What a pipeline version costs, next to what it gains.

A platform team should score every model and signal on build, upkeep and inference cost against the
value it adds, and retire the ones that don't pay. This module keeps that ledger.

A cost record is mutable metadata on a write-once pipeline version (prices change, a retrain fills in
GPU hours later), so it lives beside the version with its own history, updated by compare-and-swap:
  costs/pipelines/<name>/v<N>.yaml   {train_usd, train_gpu_hours, gpu_type, serve_usd_per_1k, note, history}
  costs/settings.yaml                {queries_per_day}

ROI compares a version with the baseline arm of its latest backtest on the pre-registered primary metric:
  extra_monthly_usd = (serve_b - serve_a) per 1k queries x queries_per_day x 30 / 1000 + (train_b - train_a) / 12
  gain_per_1k_usd   = delta / (extra_monthly_usd / 1000)
Training cost is spread over a year so one retrain does not dominate a month. The flag reads:
  retire              costs more and the gain is not a measured win
  cheaper-and-better  costs the same or less and is a measured win
  worth-it            costs more and is a measured win
  unproven            no measured win, and no extra cost
"""
from __future__ import annotations

import datetime as dt

import yaml

from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami

FIELDS = {"train_usd": float, "train_gpu_hours": float, "gpu_type": str, "serve_usd_per_1k": float, "note": str}
SETTINGS_KEY = "costs/settings.yaml"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _key(name: str, version: int) -> str:
    return f"costs/pipelines/{name}/v{version}.yaml"


def _read(ctx: Context, key: str):
    try:
        data, tag = ctx.store.get_with_tag(key)
        return yaml.safe_load(data) or {}, tag
    except NotFound:
        return {}, None


def _write(ctx: Context, key: str, body: dict, tag) -> None:
    try:
        ctx.store.put_if(key, dump_yaml(body), tag)
    except Conflict:
        raise Conflict("someone else changed this cost at the same moment. Reload and try again.") from None


def _resolve(ctx: Context, pipeline: str):
    m, _ = ctx.registry.resolve("pipeline", pipeline)
    return m


def get(ctx: Context, pipeline: str) -> dict:
    m = _resolve(ctx, pipeline)
    doc, _ = _read(ctx, _key(m.name, m.version))
    return {"pipeline": f"{m.name}@v{m.version}", **{k: doc.get(k) for k in FIELDS},
            "updated_by": doc.get("updated_by"), "updated_at": doc.get("updated_at"), "history": doc.get("history", [])}


def set_cost(ctx: Context, pipeline: str, values: dict, dry_run: bool = False) -> dict:
    m = _resolve(ctx, pipeline)
    clean = {}
    for k, v in values.items():
        if k not in FIELDS:
            raise ValueError(f"unknown cost field {k}: use {', '.join(FIELDS)}")
        if v is None or v == "":
            continue
        try:
            clean[k] = FIELDS[k](v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number") from None
        if FIELDS[k] is float and clean[k] < 0:
            raise ValueError(f"{k} cannot be negative")
    if not clean:
        raise ValueError("give at least one of " + ", ".join(FIELDS))
    key = _key(m.name, m.version)
    doc, tag = _read(ctx, key)
    before = {k: doc.get(k) for k in clean}
    if dry_run:
        return {"pipeline": f"{m.name}@v{m.version}", "would_set": clean, "before": before}
    doc.update(clean)
    doc["updated_by"], doc["updated_at"] = whoami(), _now()
    doc.setdefault("history", []).append({"at": doc["updated_at"], "by": doc["updated_by"], "from": before, "to": clean})
    _write(ctx, key, doc, tag)
    return get(ctx, f"{m.name}@v{m.version}")


def settings(ctx: Context) -> dict:
    doc, _ = _read(ctx, SETTINGS_KEY)
    return {"queries_per_day": doc.get("queries_per_day")}


def set_settings(ctx: Context, queries_per_day: int | None) -> dict:
    if queries_per_day is not None and int(queries_per_day) <= 0:
        raise ValueError("queries per day must be a positive whole number")
    doc, tag = _read(ctx, SETTINGS_KEY)
    doc["queries_per_day"] = int(queries_per_day) if queries_per_day is not None else None
    doc["updated_by"], doc["updated_at"] = whoami(), _now()
    _write(ctx, SETTINGS_KEY, doc, tag)
    return settings(ctx)


def latency(ms: dict[str, float]) -> dict:
    """p50 and p99 of per-query latencies in milliseconds."""
    xs = sorted(v for v in ms.values() if v is not None)
    if not xs:
        return {"p50_ms": None, "p99_ms": None}
    pick = lambda q: xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]
    return {"p50_ms": round(pick(0.5), 3), "p99_ms": round(pick(0.99), 3)}


def arm_cost(ctx: Context, pipeline_ref: str, ms: dict[str, float], compute: str) -> dict:
    c = get(ctx, pipeline_ref)
    return {"train_usd": c["train_usd"], "train_gpu_hours": c["train_gpu_hours"], "serve_usd_per_1k": c["serve_usd_per_1k"],
            **latency(ms), "latency_source": f"measured per query on {compute}"}


def roi(a_cost: dict, b_cost: dict, delta: float, verdict: str, queries_per_day: int | None) -> dict:
    sa, sb = a_cost.get("serve_usd_per_1k"), b_cost.get("serve_usd_per_1k")
    ta, tb = a_cost.get("train_usd"), b_cost.get("train_usd")
    extra_serve = (sb - sa) if sa is not None and sb is not None else None
    extra_train = (tb or 0.0) - (ta or 0.0) if (ta is not None or tb is not None) else None
    known = extra_serve is not None or extra_train is not None
    monthly = None
    if known and (extra_serve is None or queries_per_day):
        monthly = (extra_serve or 0.0) * (queries_per_day or 0) * 30 / 1000 + (extra_train or 0.0) / 12
        monthly = round(monthly, 2)
    gain_per_1k = round(delta / (monthly / 1000), 6) if monthly else None
    win = verdict == "win"
    more = (monthly or 0) > 0 or (monthly is None and (extra_serve or 0) > 0)
    flag = None
    if known:
        flag = ("worth-it" if win else "retire") if more else ("cheaper-and-better" if win else "unproven")
    return {"extra_serve_usd_per_1k": None if extra_serve is None else round(extra_serve, 6),
            "extra_train_usd": None if extra_train is None else round(extra_train, 2),
            "extra_monthly_usd": monthly, "gain_per_1k_usd": gain_per_1k, "flag": flag,
            "needs": None if monthly is not None or not known else "set queries per day to price the serving cost per month"}


def ledger(ctx: Context) -> list[dict]:
    """Every pipeline version with its cost and its latest primary comparison against a baseline."""
    from .ops import _latest_run_for

    qpd = settings(ctx)["queries_per_day"]
    out = []
    for name in ctx.registry.names("pipeline"):
        for v in ctx.registry.versions("pipeline", name):
            ref = f"{name}@v{v}"
            try:
                c = get(ctx, ref)
            except NotFound:  # a name folder with no readable version (an interrupted import): skip it
                continue
            row = {k: c[k] for k in ("pipeline", *FIELDS, "updated_by", "updated_at")}
            row["p50_ms"] = row["p99_ms"] = row["latency_source"] = None
            row["roi"] = None
            run = _latest_run_for(ctx, ref)
            if run is not None:
                arm = next((a for a in run.arms if a.arm == ref), None)
                if arm and arm.cost:
                    row["p50_ms"], row["p99_ms"], row["latency_source"] = arm.cost.get("p50_ms"), arm.cost.get("p99_ms"), arm.cost.get("latency_source")
                comp = next((x for x in run.comparisons if x["b"] == ref and x.get("primary")), None)
                if comp:
                    base = next((a for a in run.arms if a.arm == comp["a"]), None)
                    try:  # imported runs can name a baseline that is not a pipeline here, such as "live policy"
                        base_cost = get(ctx, comp["a"])
                    except (NotFound, ValueError):
                        base_cost = {}
                    r = roi(base_cost, c, comp["delta"], comp["verdict"], qpd) if base else {}
                    row["roi"] = {"run": f"{run.name}@v{run.version}", "baseline": comp["a"], "metric": comp["metric"],
                                  "delta": comp["delta"], "verdict": comp["verdict"], **r}
            out.append(row)
    return out
