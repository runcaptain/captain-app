"""Which machine a deploy runs on: the smallest one the model fits on, on every host.

A first deploy used to take each host's default (ml.m5.large on AWS, $0.13 an hour in us-west-1), whatever the
model was. The demo store's ranker ran fine on ml.t2.medium at $0.07. So the plan now picks.

  the need      what the model takes to serve, from the fit check (fit.py): the same measured numbers the
                "this computer" card uses, with its own 30% headroom already in.
  the room      twice the need, and at least 1 GB more than the need, whichever is larger.
  the pick      the cheapest size on that host and region, by the price table, with that much memory.

The plan says which machine and why in one line ("ml.t2.medium: this model needs about 205 MB, and this has 4 GB").
A person can choose a bigger one (Settings, or `PUT /api/settings/serving`): that choice is used as long as the
model fits on it by the same rule, and refused with the reason when it doesn't.

A model too big for the largest size a host has is not deployable there, and the sentence says so.
"""
from __future__ import annotations

from .. import price_table as PT
from ..workspace import Context
from .fit import _gb

HEADROOM_TIMES = 2.0
HEADROOM_MB = 1024.0
# What a SageMaker hosting machine has, in MB. The price comes from the price table. Machines with a GPU are left
# out: a ranker Captain serves does not use one.
SAGEMAKER_MEMORY = {name: m["memory_mb"] for name, m in PT.SAGEMAKER_MACHINES.items()}
# One copy on Modal: CPU cores and memory. Modal takes any memory; these are the steps Captain offers.
MODAL_SIZES = [(1, 1024), (1, 2048), (1, 4096), (2, 8192), (4, 16384), (8, 32768)]
# Cloud Run: memory steps and the fewest vCPU Cloud Run allows with each.
CLOUD_RUN_SIZES = [(1, 512), (1, 1024), (1, 2048), (1, 4096), (2, 8192), (4, 16384), (8, 32768)]
# Container Apps, Consumption plan: the pairs it allows, up to 4 vCPU and 8 GB.
CONTAINER_APPS_SIZES = [(0.25, 512), (0.5, 1024), (0.75, 1536), (1, 2048), (1.25, 2560), (1.5, 3072), (1.75, 3584), (2, 4096),
                        (2.5, 5120), (3, 6144), (3.5, 7168), (4, 8192)]
WHERE = {"aws": "AWS", "modal": "Modal", "gcp": "Cloud Run", "azure": "Container Apps"}


class TooBig(ValueError):
    """The model needs more memory than the largest size this host has."""


def room_mb(need_mb: float) -> float:
    """How much memory a machine must have for a model that needs `need_mb`."""
    return max(need_mb * HEADROOM_TIMES, need_mb + HEADROOM_MB)


def _cpu(c: float) -> str:
    return f"{c:g} vCPU" if c != 1 else "1 vCPU"


def sizes(host: str, region: str | None) -> list[dict]:
    """Every size Captain offers on this host, cheapest first: [{name, cpu, memory_mb, hourly_usd, config}]. `config`
    is what the compute target is given."""
    out = []
    if host == "aws":
        table = PT.SAGEMAKER_HOSTING.get(region or "") or PT.SAGEMAKER_HOSTING["us-east-1"]
        for name, mem in SAGEMAKER_MEMORY.items():
            if name in table:
                out.append({"name": name, "cpu": None, "memory_mb": mem, "hourly_usd": float(table[name]),
                            "config": {"instance_type": name, "instance_count": "1"}})
    elif host == "modal":
        for cpu, mem in MODAL_SIZES:
            out.append({"name": f"{cpu:g} CPU, {_gb(mem)}", "cpu": cpu, "memory_mb": mem,
                        "hourly_usd": 3600 * (cpu * PT.MODAL["core_second"] + mem / 1024 * PT.MODAL["gib_second"]),
                        "config": {"cpu": f"{cpu:g}", "memory_mb": str(mem)}})
    elif host == "gcp":
        p = PT.CLOUD_RUN
        for cpu, mem in CLOUD_RUN_SIZES:
            out.append({"name": f"{_cpu(cpu)}, {_gb(mem)}", "cpu": cpu, "memory_mb": mem,
                        "hourly_usd": 3600 * (cpu * p["vcpu_second"] + mem / 1024 * p["gib_second"]),
                        "config": {"cpu": f"{cpu:g}", "memory_mb": str(mem)}})
    elif host == "azure":
        p = PT.CONTAINER_APPS.get(str(region or "").lower().replace(" ", ""), PT.CONTAINER_APPS["default"])
        for cpu, mem in CONTAINER_APPS_SIZES:
            out.append({"name": f"{_cpu(cpu)}, {_gb(mem)}", "cpu": cpu, "memory_mb": mem,
                        "hourly_usd": 3600 * (cpu * p["vcpu_second"] + mem / 1024 * p["gib_second"]),
                        "config": {"cpu": f"{cpu:g}", "memory_mb": str(mem)}})
    return sorted(out, key=lambda s: (s["hourly_usd"], s["memory_mb"]))


def chosen(ctx: Context, host: str) -> str | None:
    """The machine a person chose for this host in Settings, or None."""
    from .. import settings as ST

    return (((ST.get(ctx).get("serving") or {}).get("machine") or {}).get(host)) or None


def pick(ctx: Context, host: str, region: str | None, need_mb: float) -> dict:
    """The machine a deploy on this host uses: {name, memory_mb, cpu, hourly_usd, config, need_mb, room_mb, by, text}.
    Raises TooBig, in a sentence, when no size has the room."""
    if host == "local":
        return {"name": "This computer", "memory_mb": None, "cpu": None, "hourly_usd": 0.0, "config": {}, "need_mb": round(need_mb),
                "room_mb": round(need_mb), "by": "local", "text": f"This computer: this model needs about {_gb(need_mb)}."}
    room = room_mb(need_mb)
    all_sizes = sizes(host, region)
    fits = [s for s in all_sizes if s["memory_mb"] >= room]
    if not fits:
        biggest = max(all_sizes, key=lambda s: s["memory_mb"])
        raise TooBig(f"This model needs about {_gb(need_mb)} of memory to serve, so it wants a machine with {_gb(room)}. The largest "
                     f"Captain offers on {WHERE[host]} is {biggest['name']}" + ("" if host != "aws" else f", with {_gb(biggest['memory_mb'])}")
                     + ". Pick another place for it.")
    want = chosen(ctx, host)
    by = "smallest"
    s = fits[0]
    if want:
        mine = next((x for x in all_sizes if x["name"] == want or str(x["memory_mb"]) == str(want)), None)
        if mine is None:
            raise ValueError(f"The machine chosen in Settings for {WHERE[host]}, {want}, is not one Captain offers there. "
                             f"Choose one of: {', '.join(x['name'] for x in all_sizes)}.")
        if mine["memory_mb"] < room:
            raise ValueError(f"The machine chosen in Settings for {WHERE[host]}, {mine['name']}, has {_gb(mine['memory_mb'])}. This model needs "
                             f"about {_gb(need_mb)} and wants {_gb(room)} with room to spare. Choose a bigger one, or clear the choice.")
        s, by = mine, "setting"
    text = (f"{s['name']}: this model needs about {_gb(need_mb)}, and this has {_gb(s['memory_mb'])}."
            + (" The smallest that leaves room to spare." if by == "smallest" else " Your choice in Settings."))
    return {**s, "hourly_usd": round(s["hourly_usd"], 6), "need_mb": round(need_mb), "room_mb": round(room), "by": by, "text": text}


def machine_facts(host: str, config: dict | None, region: str | None = None, name: str | None = None) -> dict:
    """What a deployment's machine is, on any host: {name, cpus, memory_mb, hourly_usd, words}. `words` is how a row
    says it: "ml.t2.medium: 2 virtual CPUs, 4 GB memory", or "1 CPU, 2 GB memory" where the size is its own facts.
    From the price table for a named machine, from the size itself elsewhere. Unknown parts are None."""
    cfg = config or {}
    name = name or cfg.get("instance_type")
    if host == "aws" and name:
        m = PT.SAGEMAKER_MACHINES.get(name) or {}
        price = next((s["hourly_usd"] for s in sizes("aws", region) if s["name"] == name), None)
        cpus, mem = m.get("cpus"), m.get("memory_mb")
        words = f"{name}: {cpus} virtual CPUs, {_gb(mem)} memory" if cpus and mem else name
        return {"name": name, "cpus": cpus, "memory_mb": mem, "hourly_usd": price, "words": words}
    try:
        cpus, mem = float(cfg.get("cpu") or 0) or None, int(float(cfg.get("memory_mb") or 0)) or None
    except (TypeError, ValueError):
        cpus, mem = None, None
    if host == "local" or not (cpus and mem):
        return {"name": None, "cpus": cpus, "memory_mb": mem, "hourly_usd": 0.0 if host == "local" else None, "words": None}
    price = next((s["hourly_usd"] for s in sizes(host, region) if s["cpu"] == cpus and s["memory_mb"] == mem), None)
    unit = "CPU" if host == "modal" else "virtual CPU"
    return {"name": None, "cpus": cpus, "memory_mb": mem, "hourly_usd": price,
            "words": f"{cpus:g} {unit}{'s' if cpus != 1 else ''}, {_gb(mem)} memory"}


def offered(host: str, region: str | None) -> list[dict]:
    """The sizes a person can choose from, for the setting: [{name, memory_mb, hourly_usd, text}]."""
    return [{"name": s["name"], "memory_mb": s["memory_mb"], "hourly_usd": round(s["hourly_usd"], 4),
             "text": f"{s['name']}, {_gb(s['memory_mb'])}, about ${s['hourly_usd']:.2f} an hour"} for s in sizes(host, region)]
