"""Deploy: one call from "this project" and "this place" to a model that answers.

A person never sets up serving as a step of its own. They pick where the model runs, read one plan, say yes, and
watch it happen. The first time on a cloud, the plan includes what that cloud needs once (a role, a place for the
image, the image itself), and the run does that first. After that the plan is just the model.

  offers(ctx, pid)        the places this project can be deployed: this computer, and one entry for each cloud
                          login connected in Captain. A cloud with no login is not offered. Each says whether it
                          can deploy now, whether the first-time work is still to do, whether it has run on a real
                          account, and what it costs.
  plan(ctx, pid, host)    what a deploy there would create (first time only, and for this model), what exists
                          already, the cost by the hour and by the month, how long it takes, and the named steps.
  run(ctx, pid, host)     do it, as one piece of work with named steps. Safe to run twice: what exists is kept,
                          and a run that failed is picked up where it stopped.
  status(ctx, pid)        where the last deploy stands: each step, the one that failed and why, what exists now,
                          and what can be done about it (retry, remove).
  remove(ctx, pid)        stop the model, and take away the first-time work too when this project's deploy made it
                          and nothing else uses it.

The setup commands (`cst compute setup serving ...`) and `cst projects stage` are still there underneath, for
agents and scripts. This is the same work, in the order a person needs it.

Steps. Every host has the same five, with its own words for the second:

  check    Checking your account      what is there already
  access   Creating the role ...      the identity the model runs as and where its files are kept (first time)
  image    Building the image         (first time, or when Captain itself has changed)
  start    Starting the model
  ready    Checking it answers        it answered, and a few test requests went through it

More steps can follow these (`after_step`): a check that what is served matches training, saving the version to a
model registry. One of those failing does not undo the deploy.

The state of the last deploy is kept in the workspace, `staging/<project>/<env>/deploy.json`.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from .. import project_state as PS
from ..storage.backend import Conflict, NotFound
from ..workspace import Context, whoami
from . import ENV, NeedsConfirm, _pick, _save, _status_text, plan as stage_plan, public, servable, stage, state, unstage
from . import hosts as H

HOURS_A_MONTH = 730
STEPS = ("check", "access", "image", "start", "ready")
# What the second step is called on each host, and what its first-time work is in a few words.
WORDS = {
    "local": {"access": "Nothing to create", "short": "Nothing is created. It runs as a process on this computer.",
              "model": "A process on this computer. It stops when idle."},
    "aws": {"access": "Creating permissions", "short": "A role that can only read this model and write its log, a place for the model's image, and the image.",
            "model": "The endpoint itself. Made now, deleted when you stop it."},
    "modal": {"access": "Creating storage", "short": "A Modal volume for the model's files and its request log. No keys and no roles.",
              "model": "The endpoint and what it is told. Made now, removed when you stop it."},
    "gcp": {"access": "Creating permissions and storage",
            "short": "Two service accounts that can only use one bucket and one image repository, the bucket, the repository, and the image.",
            "model": "The Cloud Run service itself. Made now, deleted when you stop it."},
    "azure": {"access": "Creating permissions and storage",
              "short": "One resource group holding the model's storage, an identity that can only use it, a registry and the image.",
              "model": "The container app itself. Made now, deleted when you stop it."},
}
TITLES = {"check": "Checking your account", "image": "Building the model's image", "start": "Starting the model", "ready": "Checking it answers"}
# How long each step takes comes from one table of measured times (timings.py), which the lines a run prints
# read too.
DEFAULT_REGION = {"aws": "us-east-1", "gcp": "us-central1", "azure": "eastus"}
# A run that has said nothing for this long is taken to have died with its process, so another may start.
STALE_S = 180
BEAT_S = 30  # a run says it is alive this often, even when its step has nothing to say


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _key(pid: str, env: str) -> str:
    return f"staging/{pid}/{env}/deploy.json"


# Steps that run after the model answers, in order. Each is {id, title, run(ctx, pid, env, say), when(ctx, pid)?,
# soft}. `when` says whether the step applies to this project (always, when left out). A `soft` step that fails is
# marked failed with its reason and the deploy still ends done: the model is up, and the screen says what the step
# found. Add one with `after_step`.
AFTER: list[dict] = []
TEST_REQUESTS = 5


def after_step(id: str, title: str, run, when=None, soft: bool = True) -> None:
    """Register a step that runs after "Checking it answers". Registering an id again replaces it."""
    AFTER[:] = [a for a in AFTER if a["id"] != id] + [{"id": id, "title": title, "run": run, "when": when, "soft": soft}]


def _after(ctx: Context, pid: str) -> list[dict]:
    out = []
    for a in AFTER:
        try:
            if a["when"] is None or a["when"](ctx, pid):
                out.append(a)
        except Exception:  # noqa: BLE001  a step that can't tell whether it applies does not run
            continue
    return out


def _steps(cloud: str, ctx: Context | None = None, pid: str | None = None) -> list[dict]:
    base = [{"id": s, "title": WORDS[cloud]["access"] if s == "access" else TITLES[s]} for s in STEPS]
    return base + ([{"id": a["id"], "title": a["title"]} for a in _after(ctx, pid)] if ctx is not None else [])


def _typical(steps: list[dict], cloud: str, first_time: bool) -> list[dict]:
    """Each step with what it typically takes on this host, from the measured table: `typical_s` and `typical`
    ("about 2 minutes"), both None for a step that has not been timed or does not run this time."""
    from . import timings as TM

    return [{**s, "typical_s": TM.seconds(cloud, s["id"], first_time), "typical": TM.step_words(cloud, s["id"], first_time)} for s in steps]


def _money(x: float) -> str:
    from ..cost_estimates import money

    return money(x)


# ---- which places to offer -----------------------------------------------------------------------


def _logins() -> list[dict]:
    from .. import cloud

    return [p for p in cloud.profiles() if p.get("provider") in ("aws", "gcp", "azure", "modal")]


def _region(cloud_name: str, login: dict | None) -> str | None:
    return (login or {}).get("region") or DEFAULT_REGION.get(cloud_name)


def _setup(ctx: Context, host) -> dict:
    try:
        return host.status(ctx)
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "text": str(e)[:200]}


def _target(ctx: Context, host, login: dict | None) -> dict | None:
    """The compute target a deploy on this host uses: the one setup made, or None before the first time."""
    from .. import compute as C

    try:
        t = C.get(ctx, host.target)
    except NotFound:
        return None
    if t.get("deleted") or t.get("kind") != host.kind:
        return None
    return t


def _virtual(host, login: dict | None) -> dict:
    """The target setup would make, for pricing a place before its first time. Nothing is saved."""
    cfg = {"instance_type": "ml.m5.large", "instance_count": "1"} if host.cloud == "aws" else {"cpu": "1", "memory_mb": "2048", "min_copies": "0", "max_copies": "1"}
    return {"name": host.target, "kind": host.kind, "provider": host.login or host.cloud, "profile": (login or {}).get("name"),
            "region": _region(host.cloud, login), "config": cfg}


def _need_mb(ctx: Context, arms: dict) -> float:
    """What these versions need to serve, from the fit check: the numbers the "this computer" card uses."""
    from . import fit as FT

    return float(FT.need([FT._products(ctx, r) for r in arms.values()])["memory_mb"])


def _estimate(ctx: Context, host, login: dict | None, target: dict | None, machine: dict | None = None) -> dict:
    from .. import cost_estimates as CE

    t = target or _virtual(host, login)
    if machine:  # priced on the machine this model will run on, not on what the target last ran
        t = {**t, "config": {**(t.get("config") or {}), **machine["config"]}}
    size = {"instances": int((t.get("config") or {}).get("instance_count") or 1),
            "instance_type": (t.get("config") or {}).get("instance_type") or "ml.m5.large"} if host.cloud == "aws" else {}
    try:
        return CE.estimate(ctx, "staging", t, size)
    except Exception as e:  # noqa: BLE001  a price that can't be read never hides a place
        return {"available": False, "text": f"Captain couldn't work out an estimate just now ({str(e)[:120]}).", "basis": []}


def cost_words(hourly: float | None, sleeps: bool, fixed_month: float = 0.0, fallback: str = "") -> tuple[str, float | None]:
    """The cost of a place in one sentence, the same on its card and in the confirm, and the monthly figure."""
    if hourly is None:
        text = fallback or "Captain has no estimate for this place yet."
        if sleeps:
            text += " It bills only while a copy is awake, and nothing while it sleeps."
        if fixed_month:
            text += f" Its registry costs about {_money(fixed_month)} a month for as long as it exists."
        return text, None
    monthly = hourly * HOURS_A_MONTH + fixed_month
    # A month is an estimate of an estimate: whole dollars once it is over ten ("about $98 a month", not $97.82).
    month = f"${monthly:,.0f}" if monthly >= 10 else _money(monthly)
    if sleeps:
        return (f"About {_money(hourly)} an hour while a copy is awake, and nothing while it sleeps. Up to {month} a month, "
                "if a copy never slept."), monthly
    return f"About {_money(hourly)} an hour, about {month} a month if it runs all the time. It keeps billing until you stop it.", monthly


def _fixed_month(cloud: str) -> float:
    if cloud != "azure":
        return 0.0
    from .. import price_table as PT

    return PT.AZURE_EXTRAS["registry_basic_day"] * 30.4


def _priced(est: dict, cloud: str, sleeps: bool) -> dict:
    """The shared estimate, with its sentence in the words every deploy surface uses."""
    hourly = float(est.get("high") or 0.0) if est.get("available") else None
    text, monthly = cost_words(hourly, sleeps, _fixed_month(cloud), est.get("text") or "")
    return {**est, "text": text, "hourly_usd": None if hourly is None else round(hourly, 4), "monthly_usd": None if monthly is None else round(monthly, 2)}


TITLES_SHORT = {"access": "set up", "image": "build the image", "start": "start the model"}


def _minutes(cloud: str, first_time: bool) -> dict:
    if cloud == "local":
        return {"low": 0, "high": 1, "measured": None, "text": "A few seconds."}
    from . import timings as TM

    total = TM.total(cloud, first_time)
    if total is None:
        lo, hi = TM.UNMEASURED_MINUTES[bool(first_time)]
        return {"low": lo, "high": hi, "measured": None, "seconds": None,
                "text": f"About {lo} to {hi} minutes. This place has not run on a real account yet, so that is a cautious guess."}
    mins = max(1, int(total / 60 + 0.5))
    # The steps worth naming: the ones that take half a minute or more, in the order they run.
    slow = [(TITLES_SHORT[s], TM.seconds(cloud, s, first_time)) for s in ("access", "image", "start") if (TM.seconds(cloud, s, first_time) or 0) >= 30]
    parts = " and ".join(f"{TM.words(sec)} to {name}" for name, sec in slow)
    low = max(1, int(total / 60))  # the measured time lies between low and high, in whole minutes
    return {"low": low, "high": low + 1, "measured": TM.measured_on(cloud), "seconds": total,
            "text": ("Under a minute." if total < 50 else f"About {mins} minute{'s' if mins != 1 else ''}" + (f": {parts}." if len(slow) > 1 else "."))
                    + f" Measured on {TM.measured_on(cloud)}."}


def offers(ctx: Context, pid: str, candidate: str | None = None, baseline: bool = False, env: str = ENV) -> dict:
    """The places this project's model can be deployed. Read from the workspace and this computer's logins only:
    nothing here calls a cloud, so the page can ask on every load."""
    doc = PS.get(ctx, pid)
    from . import sizing as SZ

    try:
        arms, serve, _exp = _pick(ctx, doc, candidate, baseline)
        version, beside, nothing = arms[serve], arms.get("baseline") if serve != "baseline" else None, None
        need = _need_mb(ctx, arms)
    except ValueError as e:
        version, beside, nothing, need = None, None, str(e), 0.0
    last = (doc.get("staging") or {}).get(env) or {}
    out = []
    # This computer, always. Whether the model fits is the plan's own check.
    here = {"id": "local", "host": "local", "label": "This computer", "login": None, "region": None, "can_deploy": version is not None, "why_not": nothing,
            "first_time": False, "proven": None, "proven_text": None, "sleeps": False, "reachable": False,
            "reach_text": "Only you can reach it. A site can't send it traffic.", "last": last.get("kind") == "local",
            "minutes": _minutes("local", False), "fit": None, "why_not_action": None, "target": "local",
            "estimate": {"free": True, "available": True, "text": "Free. It runs on this computer.", "basis": [], "hourly_usd": 0.0, "monthly_usd": 0.0}}
    if version is not None:
        try:
            fit = (stage_plan(ctx, pid, candidate, baseline, env, "local") or {}).get("local_fit")
            here["fit"] = fit
            if (fit or {}).get("outcome") in ("wont-fit", "over-limit"):
                # Over the size limit for this computer: Settings is where that limit is changed.
                here.update(can_deploy=False, why_not=fit["text"], why_not_action="settings" if fit.get("outcome") == "over-limit" else None)
        except Exception as e:  # noqa: BLE001
            here.update(can_deploy=False, why_not=str(e)[:300])
    out.append(here)
    for login in _logins():
        host = H.get(login["provider"])
        st = _setup(ctx, host)
        target = _target(ctx, host, login)
        ready = st.get("status") == "ready" and target is not None
        owner = st.get("profile") if host.cloud != "modal" else None
        # One serving setup per cloud in a workspace. Another login of the same cloud deploys through it or not at all.
        other = ready and owner and owner != login["name"]
        failed = (login.get("last_test") or {}).get("ok") is False
        region = (target or {}).get("region") or _region(host.cloud, login)
        machine, too_big = None, None
        if version is not None:
            try:
                machine = SZ.pick(ctx, host.cloud, region, need)
            except ValueError as e:  # too big for this host, or a choice in Settings that doesn't fit
                too_big = str(e)
        why = (nothing or too_big or (f"Serving in this workspace runs under the login {owner}. Deploy with that one." if other else None)
               or (f"This login's last check failed: {(login['last_test'].get('error') or 'no reason given')[:200]}" if failed else None))
        out.append({
            "id": f"{host.cloud}:{login['name']}", "why_not_action": None,
            "host": host.cloud, "label": host.label, "login": login["name"], "login_label": login.get("label") or host.label,
            "region": region, "can_deploy": why is None, "why_not": why,
            "machine": ({k: machine[k] for k in ("name", "memory_mb", "hourly_usd", "need_mb", "room_mb", "by", "text")} if machine else None),
            "machines": SZ.offered(host.cloud, region),
            "first_time": not ready, "proven": host.proven, "proven_text": host.describe()["proven_text"], "sleeps": host.scale_to_zero,
            "reachable": True, "reach_text": "A site can reach it.", "last": bool(last) and last.get("kind") == host.kind,
            "minutes": _minutes(host.cloud, not ready), "estimate": _priced(_estimate(ctx, host, login, target, machine), host.cloud, host.scale_to_zero),
            "target": target["name"] if target else None, "setup_status": st.get("status")})
    return {"project": pid, "env": env, "model_version": version, "beside": beside, "hosts": out,
            "connected": len(out) - 1, "last_id": next((h["id"] for h in out if h["last"]), None),
            "text": nothing or f"{len(out) - 1} cloud login{'s' if len(out) != 2 else ''} connected, and this computer."}


def _offer(ctx: Context, pid: str, host: str, login: str | None, candidate, baseline, env) -> dict:
    got = offers(ctx, pid, candidate, baseline, env)["hosts"]
    mine = [o for o in got if o["host"] == host and (login is None or o["login"] == login)]
    if not mine:
        if host != "local" and not any(o["host"] == host for o in got):
            label = H.get(host).label
            raise ValueError(f"No {label} login is connected in Captain. Connect one first (the cloud chip at the bottom of the sidebar, "
                             f"or cst cloud add), then deploy.")
        raise ValueError(f"No login named {login} is connected for {H.get(host).label}.")
    if len(mine) > 1:
        usable = [o for o in mine if o["can_deploy"]]
        if len(usable) != 1:
            raise ValueError(f"More than one {mine[0]['label']} login is connected: {', '.join(o['login'] for o in mine)}. Say which with --login.")
        mine = usable
    return mine[0]


# ---- the plan ------------------------------------------------------------------------------------


def _group(rows: list[dict]) -> list[dict]:
    return [{"resource": r["resource"], "name": r["name"], "why": r.get("why", "")} for r in rows or []]


def plan(ctx: Context, pid: str, host: str = "local", login: str | None = None, candidate: str | None = None, baseline: bool = False,
         env: str = ENV, allow_large: bool = False) -> dict:
    """What deploying this project's model there would do, before anything is created. On a cloud this asks the
    account what is there (which APIs are on, say), so it can take a few seconds."""
    o = _offer(ctx, pid, host, login, candidate, baseline, env)
    if not o["can_deploy"]:
        raise ValueError(o["why_not"])
    doc = PS.get(ctx, pid)
    arms, serve, _exp = _pick(ctx, doc, candidate, baseline)
    for r in arms.values():
        s = servable(ctx, r)
        if not s["ok"]:
            from . import NotServable

            raise NotServable(s["text"])
    version, beside = arms[serve], arms.get("baseline") if serve != "baseline" else None
    base = {"project": pid, "env": env, "id": o["id"], "host": host, "login": o["login"], "label": o["label"], "region": o["region"], "model_version": version,
            "beside": beside, "first_time": o["first_time"], "steps": _typical(_steps(host, ctx, pid), host, o["first_time"]), "minutes": o["minutes"], "proven": o["proven"],
            "proven_text": o["proven_text"], "sleeps": o["sleeps"], "warnings": [], "login_policy": None, "login_text": None}
    what = f"Deploy {version}" + (f" beside the baseline {beside}" if beside else "")
    if host == "local":
        p = stage_plan(ctx, pid, candidate, baseline, env, "local", allow_large=allow_large)
        fit = p.get("local_fit") or {}
        return {**base, "needs_confirm": False, "fit": fit, "replaces": p.get("replaces"),
                "creates": {"short": WORDS["local"]["short"], "count": 0, "groups": []}, "exists": [],
                "cost": {"hourly_usd": 0.0, "monthly_usd": 0.0, "setup_usd": 0.0, "sleeps": False, "free": True, "estimate": p["cost"]["estimate"],
                         "text": "Free. It runs on this computer, and stops when idle."},
                "text": f"{what} on this computer. Free, and only you can reach it." + (f" {fit['text']}" if fit.get("text") else "")}
    from . import sizing as SZ

    hobj = H.get(host)
    st = _setup(ctx, hobj)
    machine = SZ.pick(ctx, host, o["region"], _need_mb(ctx, arms))
    base["machine"] = {k: machine[k] for k in ("name", "memory_mb", "hourly_usd", "need_mb", "room_mb", "by", "text", "config")}
    first_groups, exists, setup_usd, policy, policy_text, replaces = [], [], 0.0, None, None, None
    each = []
    if o["first_time"]:
        hp = hobj.plan(ctx, **_setup_options(ctx, host, o, machine))
        made = {(x["type"], x["name"]) for x in st.get("created") or []}
        first_groups = _group(hp["creates"])
        each = _group(hp.get("each_deploy") or [])
        exists = [{"resource": x["type"], "name": x["name"]} for x in st.get("created") or []] if made else []
        setup_usd = float((hp.get("cost") or {}).get("setup_usd") or 0.0)
        policy, policy_text = hp.get("login_policy"), hp.get("login_text")
        est = o["estimate"]
        setup_cost_text = (hp.get("cost") or {}).get("text") or ""
    else:
        p = stage_plan(ctx, pid, candidate, baseline, env, o["target"])
        est, replaces = o["estimate"], p.get("replaces")
        base["warnings"] = p.get("warnings") or []
        exists = [{"resource": x["type"], "name": x["name"]} for x in st.get("created") or []]
        setup_cost_text = ""
    if host == "aws" and not each:
        from .. import settings as ST

        if o["first_time"] or not o.get("target"):
            named = (ST.get(ctx).get("serving") or {}).get("name_prefix")
        else:  # what the place was set up with, which a later change of the setting does not rename
            from .. import compute as C

            named = (C.get(ctx, o["target"]).get("config") or {}).get("name_prefix")
        each = [{"resource": "SageMaker endpoint", "name": f"{named or 'captain'}-{{project}}",
                 "why": "The model, answering. Made now, deleted when you stop it. It bills by the hour while it is up."}]
    # Which machine, and why this one, said once where "Show everything" puts it.
    # It is a choice, not a thing made, so it comes last and is left out of the count.
    # Only beside things being made: a plan that makes nothing keeps an empty list, and `machine.text` still says it.
    if each:
        each = [x for x in each if x["resource"] != "Machine"] + [{"resource": "Machine", "name": machine["name"], "why": machine["text"]}]
    priced = est if "monthly_usd" in est else _priced(est, host, o["sleeps"])
    hourly, monthly, cost_text = priced["hourly_usd"], priced["monthly_usd"], priced["text"]
    if setup_usd:
        cost_text += f" The first time also costs about {_money(setup_usd)} once, to build the image."
    groups = (([{"title": "The first time only", "summary": WORDS[host]["short"], "items": first_groups}] if first_groups else [])
              + ([{"title": "For this model", "summary": WORDS[host]["model"], "items": each}] if each else []))
    count = sum(1 for g in groups for x in g["items"] if x.get("resource") != "Machine")
    where = {"aws": "your AWS account", "modal": "your Modal workspace", "gcp": "your Google Cloud project", "azure": "your Azure subscription"}[host]
    short = (WORDS[host]["short"] if o["first_time"] else "Only the model itself. Everything else is already there.")
    return {**base, "needs_confirm": True, "replaces": replaces,
            "creates": {"short": short, "count": count, "groups": groups}, "exists": exists,
            "cost": {"hourly_usd": hourly, "monthly_usd": monthly,
                     "setup_usd": round(setup_usd, 2), "sleeps": o["sleeps"], "free": False, "estimate": est, "text": cost_text,
                     "setup_text": setup_cost_text},
            "login_policy": policy, "login_text": policy_text,
            "text": f"{what} in {where}" + (f" ({o['region']})" if o["region"] else "") + f". {short} {cost_text} {o['minutes']['text']}"
                    + ("" if o["proven"] else " " + (o["proven_text"] or ""))}


def _setup_options(ctx: Context, host: str, o: dict, machine: dict) -> dict:
    """What a host's setup is told: the login and region, the machine this model was sized for, and the workspace's
    name prefix when one is set."""
    from .. import settings as ST

    prefix = (ST.get(ctx).get("serving") or {}).get("name_prefix")
    named = {"name_prefix": prefix} if prefix else {}
    if host == "modal":
        from .. import cloud

        env_name = (cloud.get_profile(o["login"]).get("fields") or {}).get("environment")
        return {**({"environment": env_name} if env_name else {}), **machine["config"], **named}
    size = {"instance_type": machine["config"]["instance_type"]} if host == "aws" else machine["config"]
    return {"profile": o["login"], "region": o["region"], **size, **named}


def _use_machine(ctx: Context, target_name: str, machine: dict, say) -> None:
    """Point the compute target at the machine this model was sized for, when it is on another one. The size is
    read at each deploy, so nothing running changes until the model is started."""
    from .. import compute as C

    t = C.get(ctx, target_name)
    cfg = dict(t.get("config") or {})
    if all(str(cfg.get(k)) == str(v) for k, v in machine["config"].items()):
        return
    secrets = {k: v for k, v in cfg.items() if k.endswith("_secret")}
    C.add(ctx, target_name, t["kind"], t.get("profile"), t.get("region"), {**cfg, **machine["config"], **secrets}, replace=True)
    say(f"Using {machine['text']}")


# ---- the state of the last deploy -----------------------------------------------------------------


def _read(ctx: Context, pid: str, env: str) -> tuple[dict | None, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(_key(pid, env))
        return json.loads(body), tag
    except NotFound:
        return None, None


def _write(ctx: Context, pid: str, env: str, doc: dict) -> None:
    doc["updated_at"] = _now()
    for _ in range(10):
        _, tag = _read(ctx, pid, env)
        try:
            ctx.store.put_if(_key(pid, env), json.dumps(doc, indent=1).encode(), tag)
            return
        except Conflict:
            time.sleep(0.02)


def _exists_now(ctx: Context, pid: str, host: str, env: str) -> list[dict]:
    out = []
    if host != "local":
        out += [{"resource": x["type"], "name": x["name"]} for x in (_setup(ctx, H.get(host)).get("created") or [])]
    rec = state(ctx, pid, env) or {}
    out += [{"resource": r["type"], "name": r["name"]} for r in rec.get("resources") or []]
    return out


def _stopped_before_the_model_was_up(doc: dict) -> bool:
    """Whether a failed or stalled deploy stopped in one of the steps that get the model running. A step added
    after those (publishing to a registry, say) can fail with the model up, and that failure is real: a running
    model does not make it go away."""
    step = doc.get("failed_step") or next((s for s in doc.get("steps") or [] if s.get("status") in ("running", "failed")), None)
    return step is None or step.get("id") in STEPS


def _superseded(doc: dict, rec: dict | None) -> bool:
    """A deploy that is marked failed, or stalled, for a project whose model is running now and was started at or
    after that deploy began. The model being up is the fact: the panel about the attempt is history."""
    if not rec or rec.get("status") != "running" or not _stopped_before_the_model_was_up(doc):
        return False
    if doc.get("status") == "failed":
        pass
    elif doc.get("status") == "running":
        try:
            if (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(doc["updated_at"])).total_seconds() <= STALE_S:
                return False
        except (KeyError, ValueError):
            return False
    else:
        return False
    return str(rec.get("started_at") or "") >= str(doc.get("started_at") or "")


def close_stale(ctx: Context, pid: str, env: str = ENV, cloud_says_running: bool = True) -> bool:
    """Close a deploy left failed or stalled, because the model is running. Called when the cloud itself reports
    the endpoint up (fleet.py), and by `status` when the record says so. Returns whether anything was closed."""
    doc, _ = _read(ctx, pid, env)
    if not doc or doc.get("status") not in ("failed", "running") or not _stopped_before_the_model_was_up(doc):
        return False
    if doc["status"] == "running":
        try:
            if (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(doc["updated_at"])).total_seconds() <= STALE_S:
                return False  # a deploy that is really going: leave it be
        except (KeyError, ValueError):
            return False
    if not cloud_says_running:
        return False
    was = doc.get("failed_step") or next(({"id": s["id"], "title": s["title"]} for s in doc.get("steps") or [] if s.get("status") == "running"), None)
    for s in doc.get("steps") or []:
        if s.get("status") in ("running", "failed", "pending"):
            s["status"] = "done"
    doc.update(status="done", ended_at=doc.get("ended_at") or _now(), failed_step=None, error=None,
               closed={"at": _now(), "why": "The model is running, so this deploy is closed as done.",
                       "stopped_at_step": was.get("title") if was else None})
    _write(ctx, pid, env, doc)
    return True


def status(ctx: Context, pid: str, env: str = ENV) -> dict:
    """Where the last deploy of this project stands. Reads the workspace only."""
    doc, _ = _read(ctx, pid, env)
    rec = state(ctx, pid, env)
    if doc and doc.get("host") != "local" and (doc.get("status") == "failed" or (rec or {}).get("status") in ("failed", "starting")):
        # Before saying a cloud deploy failed, ask the cloud. If the endpoint is up, the record and this deploy are
        # set right (fleet.py), and what comes back below is the truth. Kept for a few seconds, so a page that
        # polls does not ask each time. A cloud that can't be asked changes nothing.
        try:
            from . import fleet as FL

            FL.one(ctx, pid, env)
            doc, _ = _read(ctx, pid, env)
            rec = state(ctx, pid, env)
        except Exception:  # noqa: BLE001
            pass
    if doc and _superseded(doc, rec) and close_stale(ctx, pid, env):
        doc, _ = _read(ctx, pid, env)
    if not doc:
        return {"project": pid, "env": env, "status": "none", "steps": [], "failed_step": None, "error": None, "exists_now": [], "can": [],
                "staged": public(rec), "text": "Nothing has been deployed from here yet."}
    out = dict(doc)
    if out["status"] == "running":
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(out["updated_at"])).total_seconds()
        if age > STALE_S:
            # The process doing the work is gone (Captain was quit, say). What it made is still listed.
            step = next((s for s in out["steps"] if s["status"] == "running"), None)
            if step:
                step["status"] = "failed"
            out.update(status="failed", failed_step={"id": step["id"], "title": step["title"]} if step else None,
                       error="This deploy stopped before it finished: Captain was closed or lost its connection while it ran. "
                             "Nothing is lost. Retry picks up where it stopped.")
    out["fix_text"] = None
    if out["status"] == "failed":
        out["exists_now"] = _exists_now(ctx, pid, out["host"], env)
        # "Deploy again" is offered only when the same deploy can be planned again. When it can't (the login is
        # gone, the model no longer fits that place, there is nothing to stage), the reason is said instead, so the
        # panel shows what to do and not a button that fails. Read from the workspace alone: no cloud is asked.
        why = None
        try:
            o = next((h for h in offers(ctx, pid, env=env)["hosts"] if h["host"] == out["host"] and (h.get("login") == out.get("login") or out["host"] == "local")), None)
            why = ("That place is not connected in Captain any more. Connect its cloud login again, or deploy somewhere else." if o is None
                   else None if o["can_deploy"] else o.get("why_not"))
        except Exception as e:  # noqa: BLE001
            why = str(e)[:300]
        out["fix_text"] = why
        out["can"] = ([] if why else ["retry"]) + (["remove"] if out["exists_now"] else [])
    else:
        out.setdefault("exists_now", [])
        out["can"] = []
    out["staged"] = public(rec)
    f = out.get("failed_step")
    out["text"] = ("Deploying: " + next((s["title"] for s in out["steps"] if s["status"] == "running"), "working") + "." if out["status"] == "running"
                   else f"The deploy stopped at \"{f['title']}\": {out.get('error')}" if out["status"] == "failed" and f
                   else f"The deploy did not finish: {out.get('error')}" if out["status"] == "failed"
                   else _status_text(rec, None) if rec else "Deployed.")
    return out


# ---- the run -------------------------------------------------------------------------------------


IMAGE_WORDS = ("image", "build folder", "Uploading the build")
TARGET_WORDS = ("compute target",)


def run(ctx: Context, pid: str, host: str = "local", login: str | None = None, candidate: str | None = None, baseline: bool = False,
        env: str = ENV, confirm: bool = False, allow_large: bool = False, on_event=None, force: bool = False) -> dict:
    """Deploy, first-time work included, as one piece of work with named steps. `on_event(kind, **data)` hears
    each step start ("step"), each line under it ("line"), and the end ("done" or "failed")."""
    p = plan(ctx, pid, host, login, candidate, baseline, env, allow_large)
    if host == "local" and (p.get("fit") or {}).get("outcome") in ("wont-fit", "over-limit"):
        raise ValueError(p["fit"]["text"])
    if p["needs_confirm"] and not confirm:
        raise NeedsConfirm("Confirm first. " + p["text"], p)
    prev, _ = _read(ctx, pid, env)
    if prev and prev.get("status") == "running" and not force:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(prev["updated_at"])).total_seconds()
        if age <= STALE_S:
            raise ValueError("A deploy of this project is already running. Watch that one, or wait for it to end.")
    say = on_event or (lambda _kind, **_d: None)
    made_setup = bool(p["first_time"]) or bool((prev or {}).get("made_setup") and (prev or {}).get("host") == host)
    doc = {"project": pid, "env": env, "host": host, "login": p["login"], "label": p["label"], "model_version": p["model_version"], "status": "running",
           "started_at": _now(), "started_by": whoami(), "made_setup": made_setup, "failed_step": None, "error": None, "policy": None,
           "steps": [{**s, "status": "pending", "seconds": None, "lines": []} for s in p["steps"]]}
    by_id = {s["id"]: s for s in doc["steps"]}
    order = [s["id"] for s in doc["steps"]]
    cur = {"id": None, "t": time.time(), "wrote": 0.0}

    def begin(sid: str) -> None:
        if cur["id"] == sid:
            return
        end("done")
        s = by_id[sid]
        s["status"] = "running"
        cur.update(id=sid, t=time.time())
        _write(ctx, pid, env, doc)
        say("step", id=sid, title=s["title"], index=order.index(sid) + 1, of=len(order))

    def end(how: str, note: str | None = None) -> None:
        if cur["id"] is None:
            return
        s = by_id[cur["id"]]
        s.update(status=how, seconds=round(time.time() - cur["t"], 1))
        if note:
            s["lines"].append(note)
        cur["id"] = None

    def skip(sid: str, why: str) -> None:
        by_id[sid].update(status="skipped", seconds=0, lines=[why])

    def line(text) -> None:
        text = str(text)
        if cur["id"]:
            by_id[cur["id"]]["lines"].append(text)
            del by_id[cur["id"]]["lines"][:-40]
        say("line", text=text, step=cur["id"])
        if time.time() - cur["wrote"] > 2.0:  # a heartbeat, so a run that died is told from one that is working
            cur["wrote"] = time.time()
            _write(ctx, pid, env, doc)

    def setup_line(text) -> None:
        """Setup speaks in sentences. The one that mentions the image starts that step."""
        t = str(text)
        if any(w in t for w in IMAGE_WORDS) and cur["id"] == "access":
            begin("image")
        line(t)

    # A step can be silent for minutes (a cloud starting an endpoint prints nothing while it waits). `status` takes
    # a run that has said nothing for STALE_S to have died, so without this a slow start was reported as failed
    # while it was still going, and the page kept that red panel after the model came up.
    import threading

    alive = threading.Event()

    def beat() -> None:
        while not alive.wait(BEAT_S):
            if doc["status"] != "running":
                return
            try:
                _write(ctx, pid, env, doc)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=beat, daemon=True, name="deploy-heartbeat").start()
    try:
        begin("check")
        if host == "local":
            line("This computer needs nothing set up.")
            skip("access", "Nothing to create on this computer.")
            skip("image", "No image: it runs as a process here.")
            target = "local"
        else:
            hobj = H.get(host)
            line(f"Looked at what is in {p['label']} already.")
            if p["first_time"]:
                begin("access")
                opts = _setup_options(ctx, host, p, p["machine"])
                got = hobj.setup(ctx, confirm=True, progress=setup_line, **opts)
                if by_id["image"]["status"] == "pending":
                    end("done")
                    skip("image", "Modal builds the image itself when the model starts." if host == "modal" else "The image was already there.")
                target = got.get("target") or hobj.target
            else:
                skip("access", "Already there from the first time.")
                skip("image", "Already built.")
                target = hobj.target
        begin("start")
        if host != "local":
            _use_machine(ctx, target, p["machine"], line)
        rec = stage(ctx, pid, candidate, baseline, env, target, confirm=True, progress=line, allow_large=allow_large)
        _save(ctx, pid, env, host=host, login=p["login"])  # so every screen can say where it runs without guessing
        begin("ready")
        ep = (rec or {}).get("endpoint") or {}
        line(f"It answered at {ep.get('url') or ep.get('name')}.")
        sent = _test_requests(ctx, pid, env)
        if sent:
            line(f"Sent {sent} test request{'s' if sent != 1 else ''} through it. They are marked as test traffic, never as shoppers.")
        end("done")
        soft_failed = []
        for a in _after(ctx, pid):
            if a["id"] not in by_id:
                continue
            begin(a["id"])
            try:
                a["run"](ctx, pid, env, line)
                end("done")
            except Exception as e:  # noqa: BLE001
                if not a["soft"]:
                    raise
                # The model is up. What this step found is said on the step, and the deploy still ends done.
                end("failed", str(e)[:400])
                soft_failed.append({"id": a["id"], "title": a["title"], "error": str(e)[:400]})
                say("line", text=str(e)[:400], step=a["id"])
        alive.set()
        doc.update(status="done", ended_at=_now(), address=ep.get("url") or ep.get("name"), soft_failed=soft_failed)
        _write(ctx, pid, env, doc)
        say("done", address=doc["address"], model_version=p["model_version"])
        return {**status(ctx, pid, env), "plan": p}
    except BaseException as e:
        failed = cur["id"] or "check"
        policy = getattr(e, "policy", None)
        end("failed", str(e)[:400])
        s = by_id[failed]
        s["status"] = "failed"
        alive.set()
        doc.update(status="failed", ended_at=_now(), failed_step={"id": failed, "title": s["title"]}, error=str(e)[:600] or type(e).__name__,
                   policy=policy)
        _write(ctx, pid, env, doc)
        say("failed", step=failed, title=s["title"], error=doc["error"])
        raise


def _test_requests(ctx: Context, pid: str, env: str, n: int = TEST_REQUESTS) -> int:
    """Send a few of the project's own queries through the new endpoint, as test traffic, so what runs next (the
    served-features check, say) has something to look at. Best effort: a deploy never fails on this."""
    from . import call
    from .traffic import sample_queries

    try:
        rec = state(ctx, pid, env)
        if not rec or rec.get("status") != "running" or rec.get("how") == "image":
            return 0
        sent = 0
        for i, q in enumerate(sample_queries(ctx, pid, n)[:n]):
            call(ctx, rec, "rank", {"query": q, "k": 10, "traffic": "test", "session_id": f"deploy-check-{i}"})
            sent += 1
        if sent:
            call(ctx, rec, "flush", {})
        return sent
    except Exception:  # noqa: BLE001
        return 0


def remove(ctx: Context, pid: str, env: str = ENV, progress=None) -> dict:
    """Stop the model. When this project's deploy also did the first-time work on its cloud and no other project
    has a model there, take that away too, so a deploy that failed halfway leaves nothing behind."""
    from .. import projects as PR

    say = progress or (lambda _t: None)
    doc, _ = _read(ctx, pid, env)
    rec = state(ctx, pid, env)
    left: list = []
    if rec and (rec.get("status") in ("running", "starting", "failed") or rec.get("resources")):
        say("Stopping the model")
        out = unstage(ctx, pid, env)
        left += out.get("resources") or []
    removed_setup = False
    host = (doc or {}).get("host")
    if doc and doc.get("made_setup") and host and host != "local":
        hobj = H.get(host)
        st = _setup(ctx, hobj)
        used = [pr["name"] for pr in PR.listing(ctx) if pr["id"] != pid
                and any(s.get("status") in ("running", "starting") and s.get("target") == st.get("target") for s in (PS.read(ctx, pr["id"])[0].get("staging") or {}).values())]
        if st.get("status") not in ("none", "removed") and not used:
            say(f"Removing what the first deploy created in {hobj.label}")
            got = hobj.teardown(ctx, progress=say)
            left += got.get("created") or []
            removed_setup = got.get("status") == "removed"
        elif used:
            say(f"Left the first-time work in {hobj.label}: {used[0]} has a model there.")
    if doc:
        doc.update(status="removed" if not left else "failed", ended_at=_now(), made_setup=False if removed_setup else doc.get("made_setup"),
                   error=("Captain could not remove: " + ", ".join(f"{r.get('type')} {r.get('name')}" for r in left)) if left else None)
        _write(ctx, pid, env, doc)
    return {"project": pid, "status": "removed" if not left else "partly removed", "left": left, "removed_setup": removed_setup,
            "text": ("Removed. Nothing of this deploy is left." if not left else
                     "Removed, except: " + "; ".join(f"{r.get('type')} {r.get('name')} ({r.get('error', 'still there')})" for r in left))}


# The steps every deploy ends with (the trained-numbers check, saving to the registry) register themselves here.
from . import deploy_steps  # noqa: E402,F401
