"""What is deployed. The cloud is the truth, and Captain's records are corrected from it.

One function, `deployed`, that every screen and command reads. It asks each connected cloud login what exists there
that Captain made (found by Captain's tag, or by the workspace's name prefix), and merges that with Captain's own
records, one row per deployed model:

  linked     a project's record and the thing in the cloud, matched. The state is the cloud's.
  unlinked   in the cloud, made by Captain, and no project record knows it: "found in your AWS account, not linked
             to a project". Another workspace made it, or its record was lost.
  gone       a record that says running, and the cloud says it is not there: "removed outside Captain".
  local      on this computer: there is no cloud to ask, so the process itself is checked.
  unknown    the cloud could not be asked just now. The record is shown as it stands and nothing is corrected.

Why it exists: on 2026-11-12 a project page showed a red "Deploying to AWS stopped: Starting the model: failed"
panel from an earlier attempt, while that project's endpoint was in service and answering. The page read Captain's
records, and the records were wrong. So on every read:

  - a record marked failed, stopped or starting whose endpoint the cloud reports running is set to running;
  - a record marked running whose endpoint the cloud does not have is set to stopped, "removed outside Captain";
  - a deploy job left failed or running for a project whose endpoint is running is closed as done.

A failed job panel can therefore never outlive an endpoint that is running.

Asking costs a few read-only calls to each cloud's own listing, never a request to a model: a host that sleeps when
idle is not woken. The answer is kept for a few seconds (`TTL_S`), so a screen that redraws does not ask again, and
never for minutes.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from ..storage.backend import NotFound
from ..workspace import Context

ENV = "staging"
TTL_S = 5.0
CLOUDS = {"aws": "AWS", "modal": "Modal", "gcp": "Google Cloud", "azure": "Azure", "local": "This computer"}
THING = {"aws": "SageMaker endpoint", "modal": "Modal app", "gcp": "Cloud Run service", "azure": "Container Apps app"}
STATE_WORDS = {"running": "Running", "starting": "Starting", "asleep": "Asleep", "stopped": "Stopped", "failed": "Failed",
               "gone": "Removed outside Captain", "unknown": "Not checked"}
_cache: dict = {}
_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def forget(ctx: Context | None = None) -> None:
    """Drop the kept answer: after anything that changes what is deployed, and between tests."""
    with _lock:
        if ctx is None:
            _cache.clear()
        else:
            _cache.pop(ctx.workspace.name, None)


# ---- where to ask --------------------------------------------------------------------------------


def sources(ctx: Context) -> list[dict]:
    """Every place to ask: each compute target that serves models, and each connected cloud login that has none
    yet (so something made there by another workspace is still found). [{cloud, login, region, target}], where
    `target` is the compute target, or one made up for the question and never saved."""
    from .. import cloud
    from .. import compute as C
    from . import hosts as H

    out, seen = [], set()
    kinds = {h.kind: c for c, h in ((c, H.get(c)) for c in ("aws", "modal", "gcp", "azure"))}
    try:
        targets = C.listing(ctx) if hasattr(C, "listing") else C.list_targets(ctx)
    except Exception:  # noqa: BLE001
        targets = []
    for t in targets:
        c = kinds.get(t.get("kind"))
        if not c or t.get("deleted"):
            continue
        key = (c, t.get("profile"), t.get("region"), (t.get("config") or {}).get("environment"), (t.get("config") or {}).get("resource_group"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"cloud": c, "login": t.get("profile"), "region": t.get("region"), "target": t})
    for p in cloud.profiles():
        c = p.get("provider")
        # A target with no login of its own (Modal signs in by itself) already covers that cloud's login.
        if c not in ("aws", "modal", "gcp") or any(s["cloud"] == c and s["login"] in (p.get("name"), None) for s in out):
            continue  # Azure needs the resource group its setup made: without a target there is nowhere to look
        h = H.get(c)
        cfg = {"environment": (p.get("fields") or {}).get("environment")} if c == "modal" else {}
        region = p.get("region") or {"aws": "us-east-1", "gcp": "us-central1"}.get(c)
        out.append({"cloud": c, "login": p.get("name"), "region": region,
                    "target": {"name": h.target, "kind": h.kind, "provider": c, "profile": p.get("name"), "region": region,
                               "config": {k: v for k, v in cfg.items() if v}, "virtual": True}})
    return out


def _prefix(ctx: Context, target: dict) -> str:
    from .. import settings as ST

    return str((target.get("config") or {}).get("name_prefix") or (ST.get(ctx).get("serving") or {}).get("name_prefix") or "captain")


def _ask(ctx: Context, src: dict) -> tuple[list[dict], str | None]:
    """Everything Captain made that this place has, or the reason it could not be asked."""
    from .. import compute_providers as P

    try:
        fn = getattr(P.get(src["target"], ctx), "found", None)
        if not callable(fn):
            return [], "Captain can't list what is deployed on this host yet."
        return list(fn(_prefix(ctx, src["target"]))), None
    except Exception as e:  # noqa: BLE001  one cloud that can't be asked never hides the others
        return [], str(e)[:300]


ask = _ask  # the public name: ask this one place now, with nothing kept (the cloud watcher's call)


# ---- the merge -----------------------------------------------------------------------------------


def _records(ctx: Context) -> list[tuple[dict, str, dict]]:
    from .. import project_state as PS
    from .. import projects as PR

    out = []
    for pr in PR.listing(ctx):
        try:
            doc = PS.read(ctx, pr["id"])[0]
        except Exception:  # noqa: BLE001
            continue
        for env, rec in (doc.get("staging") or {}).items():
            if rec and rec.get("id"):
                out.append((pr, env, rec))
    return out


def _where_of(ctx: Context, rec: dict) -> tuple[str, str | None, str | None, dict | None]:
    """(cloud, login, region, target) of a record."""
    from .. import compute as C
    from . import hosts as H

    if rec.get("kind") == "local":
        return "local", None, None, None
    try:
        t = C.get(ctx, rec.get("target"))
    except (NotFound, Exception):  # noqa: BLE001
        t = None
    h = H.of_kind(rec.get("kind") or (t or {}).get("kind") or "")
    c = rec.get("host") or (h.cloud if h else "aws")
    return c, rec.get("login") or (t or {}).get("profile"), (rec.get("endpoint") or {}).get("region") or (t or {}).get("region"), t


def _machine(ctx: Context, rec: dict, thing: dict | None, target: dict | None) -> str | None:
    if thing and thing.get("machine"):
        return thing["machine"]
    cfg = (target or {}).get("config") or {}
    if cfg.get("instance_type"):
        return cfg["instance_type"]
    if cfg.get("memory_mb"):
        from .fit import _gb

        return f"{float(cfg.get('cpu') or 1):g} CPU, {_gb(float(cfg['memory_mb']))}"
    return None


NO_FACTS = {"machine_words": None, "machine_hourly_usd": None, "account": None, "copies_words": None}


def _system(ctx: Context, cloud: str, target: dict | None, region: str | None, machine: str | None, thing: dict | None, rec: dict) -> dict:
    """The facts about the system a model runs on that a row says in one line: the machine in words (what it is, not
    only its name), the account by its alias or last four digits, and how many copies it may run."""
    from .. import compute_providers as P
    from . import sizing as SZ

    cfg = (target or {}).get("config") or {}
    m = SZ.machine_facts(cloud, cfg, region, name=machine if cloud == "aws" else None)
    account = None
    if cloud == "aws" and target is not None:
        try:
            hint = getattr(P.get(target, ctx), "account_hint", None)
            account = hint() if callable(hint) else None
        except Exception:  # noqa: BLE001
            account = None
    elif cloud == "gcp":
        account = f"project {cfg['project']}" if cfg.get("project") else None
    elif cloud == "modal":
        account = f"environment {cfg['environment']}" if cfg.get("environment") else None
    elif cloud == "azure":
        account = f"resource group {cfg['resource_group']}" if cfg.get("resource_group") else None
    lo, hi = cfg.get("min_copies") or (cfg.get("instance_count") if cloud == "aws" else None), cfg.get("max_copies")
    sc = rec.get("scale") or {}
    lo, hi = sc.get("min", lo), sc.get("max", hi)
    now = (thing or {}).get("copies_now")
    copies = None
    if now is not None or lo is not None:
        lo_i, hi_i = (int(float(lo)) if lo is not None else None), (int(float(hi)) if hi is not None else None)
        hi_i = hi_i if hi_i is not None else lo_i
        copies = ((f"{now} cop{'y' if now == 1 else 'ies'} up now" if now is not None else "") +
                  (f", may run {lo_i} to {hi_i}" if lo_i is not None and hi_i is not None and hi_i != lo_i else "")).lstrip(", ") or None
        copies = copies[0].upper() + copies[1:] if copies else None  # it is a sentence of its own in a row: "May run 0 to 2."
    return {"machine_words": m["words"], "machine_hourly_usd": m["hourly_usd"], "account": account, "copies_words": copies}


def _local_state(rec: dict) -> tuple[str, str | None]:
    import socket

    from . import _alive

    ep = rec.get("endpoint") or {}
    if rec.get("status") != "running":
        return ("failed" if rec.get("status") == "failed" else "stopped"), rec.get("stop_reason") or rec.get("error")
    if ep.get("host") and ep.get("host") != socket.gethostname():
        return "unknown", f"It runs on {ep.get('host')}. Only that computer can check it."
    return ("running", None) if _alive(ep.get("pid")) else ("stopped", "the process ended")


def _row(pr: dict | None, env: str | None, rec: dict | None, cloud: str, login: str | None, region: str | None, thing: dict | None,
         state: str, reason: str | None, link: str, machine: str | None, corrected: list[str]) -> dict:
    name = (thing or {}).get("name") or ((rec or {}).get("endpoint") or {}).get("name")
    where = CLOUDS.get(cloud, cloud) + (f", {region}" if region else "") + (f", {machine}" if machine and machine != CLOUDS.get(cloud) else "")
    copies_now = (thing or {}).get("copies_now")
    if link == "unlinked":
        text = f"Found in your {CLOUDS.get(cloud, cloud)} account, not linked to a project."
    elif link == "gone":
        text = "Removed outside Captain."
    elif state == "unknown":
        text = reason or "Captain couldn't ask the cloud just now."
    elif state == "failed":
        text = "Failed" + (f": {reason}" if reason else ".")
    elif state == "asleep":
        text = "Asleep. It wakes on the next request and bills nothing until then."
    elif state == "starting":
        text = _starting_words(cloud, reason)
    elif state == "stopped":
        text = _stopped_words(reason, (rec or {}).get("stopped_at"))
    else:
        text = "Running" + (f" on {copies_now} " + ("copy" if copies_now == 1 else "copies") if copies_now else "") + "."
    return {"id": f"{pr['id']}:{env}" if pr else f"cloud:{cloud}:{login or ''}:{region or ''}:{name}",
            "project": pr["id"] if pr else (thing or {}).get("project"), "project_name": (pr.get("name") or pr["id"]) if pr else None,
            "env": env or (thing or {}).get("env"), "model_version": (rec or {}).get("model_version") if link != "unlinked" else None,
            "model_label": model_words((rec or {}).get("model_version"), pr["id"] if pr else None) if link != "unlinked" else None,
            "cloud": cloud, "cloud_label": CLOUDS.get(cloud, cloud), "login": login, "region": region, "machine": machine, "where": where,
            "name": name, "thing": THING.get(cloud),
            "copies": {"now": copies_now, "min": (thing or {}).get("copies_min"), "max": (thing or {}).get("copies_max")},
            "state": state, "state_label": STATE_WORDS.get(state, state.capitalize()), "state_text": text, "reason": reason,
            "since": (thing or {}).get("since") or (rec or {}).get("started_at"),
            "stopped_at": (rec or {}).get("stopped_at") if state in ("stopped", "gone") else None,
            "by": (rec or {}).get("started_by") or (thing or {}).get("by"),
            "by_name": short_name((rec or {}).get("started_by") or (thing or {}).get("by")),
            "link": link, "record_status": (rec or {}).get("status"), "corrected": corrected,
            # Whether anything of it exists in a cloud (or runs on this computer) now. A model that was stopped does
            # not: Captain only remembers it, so it can be started again. It is listed apart, as "stopped earlier".
            "in_cloud": thing is not None or link in ("unlinked", "unknown") or (cloud == "local" and state == "running"),
            "remembered_text": (None if (thing is not None or link in ("unlinked", "unknown") or (cloud == "local" and state == "running")) else
                                f"Nothing of it is running {'on this computer' if cloud == 'local' else 'in ' + CLOUDS.get(cloud, cloud)} and nothing is billing. "
                                "Captain remembers it so you can start it again."),
            "resources": (thing or {}).get("resources") or [],
            "sleeps": bool((rec or {}).get("scale_to_zero")), "address": _address(cloud, region, name, rec),
            "version_n": (rec or {}).get("version_n"),
            "can": ([] if link == "unknown" else ["link", "remove"] if link == "unlinked" else
                    ["open", "stop", "rollback"] if state in ("running", "asleep", "starting") else
                    ["open", "remove"] if thing is not None else ["start", "forget"])}


def _address(cloud: str, region: str | None, name: str | None, rec: dict | None) -> str | None:
    """The address a site calls. On AWS a SageMaker endpoint has no address of its own: it is called through AWS, at
    the invoke address made from its region and name. Elsewhere it is the endpoint's own URL."""
    url = ((rec or {}).get("endpoint") or {}).get("url")
    if url:
        return url
    if cloud == "aws" and name and region:
        return f"https://runtime.sagemaker.{region}.amazonaws.com/endpoints/{name}/invocations"
    return None


def short_name(who) -> str | None:
    """A person as a row says them: the part of an address before the @. The whole address is in `by`."""
    w = str(who or "").strip()
    return (w.split("@", 1)[0] or w) if w else None


def model_words(version, project=None) -> str | None:
    """A model version as a person reads it beside its project's name: "shop/starter-ranker@v2" is "starter-ranker,
    version 2". The project is not said twice, and the version is a word."""
    v = str(version or "").strip()
    if not v:
        return None
    name, _, n = v.partition("@")
    if project and name.startswith(f"{project}/"):
        name = name[len(project) + 1:]
    n = n[1:] if n[:1] == "v" and n[1:].isdigit() else n
    return f"{name}, version {n}" if n else name


def ago_words(when) -> str | None:
    """How long ago, in words: "just now", "20 minutes ago", "3 hours ago", "2 days ago"."""
    try:
        s = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(when))).total_seconds()
    except (TypeError, ValueError):
        return None
    if s < 90:
        return "just now"
    for size, word in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if s >= size:
            n = int(s // size)
            return f"{n} {word}{'s' if n != 1 else ''} ago"
    return "just now"


# A cloud's own word for a thing that is coming up, as a person would say it.
STARTING = {"Creating": "the endpoint is being created", "Updating": "the endpoint is moving to the new version",
            "SystemUpdating": "the cloud is updating the machine it runs on", "RollingBack": "it is going back to the version before"}


def _starting_words(cloud: str, reason) -> str:
    """One sentence for a model that is coming up: where, and what the cloud is doing. "Starting on AWS: the endpoint
    is being created." The cloud's own state ("Creating") is not a sentence."""
    r = str(reason or "").strip().rstrip(".")
    where = "on this computer" if cloud == "local" else f"on {CLOUDS.get(cloud, cloud)}"
    what = STARTING.get(r) or (r[0].lower() + r[1:] if r else "")
    return f"Starting {where}" + (f": {what}." if what else ".")


def _stopped_words(reason, when) -> str:
    """One sentence for a stopped model: who or what stopped it, and when. The record's reason is a note
    ("stopped by dana@example.com", "stopped by itself: no requests for 240 minutes"), not a sentence."""
    r, ago = str(reason or "").strip().rstrip("."), ago_words(when)
    when_said = f", {ago}" if ago else ""
    if r.lower().startswith("stopped by itself"):
        why = r.partition(":")[2].strip()
        return f"Stopped by itself{when_said}" + (f": {why}." if why else ".")
    if r.lower().startswith("stopped by "):
        return f"Stopped by {short_name(r[len('stopped by '):])}{when_said}."
    if r:
        return f"Stopped{when_said}: {r[0].lower() + r[1:] if r[:2] != 'It' else r}."
    return f"Stopped{when_said}." if ago else "Stopped."


def _correct(ctx: Context, pr: dict, env: str, rec: dict, thing: dict | None, asked_ok: bool, region: str | None) -> tuple[dict, list[str]]:
    """Set Captain's record right from what the cloud says. Returns the record as it now stands and what changed, in
    sentences. Nothing is changed when the cloud could not be asked."""
    from . import _note, _save

    if not asked_ok:
        return rec, []
    said: list[str] = []
    if thing is None:
        if rec.get("status") in ("running", "starting"):
            rec = _save(ctx, pr["id"], env, status="stopped", stopped_at=_now(), stop_reason="It was removed outside Captain.", resources=[])
            said.append("Captain's record said it was running. The cloud does not have it, so the record now says stopped: removed outside Captain.")
    elif thing["state"] in ("running", "asleep") and rec.get("status") != "running":
        was = rec.get("status") or "nothing"
        ep = {**(rec.get("endpoint") or {}), "name": thing["name"], **({"region": region} if region else {})}
        res = rec.get("resources") or [{"type": thing.get("type") or "endpoint", "name": thing["name"], **({"region": region} if region else {})}]
        rec = _save(ctx, pr["id"], env, status="running", endpoint=ep, resources=res, error=None, stopped_at=None, stop_reason=None)
        said.append(f"Captain's record said {was}. The cloud says it is running, so the record now says running.")
    elif thing["state"] == "failed" and rec.get("status") in ("running", "starting"):
        rec = _save(ctx, pr["id"], env, status="failed", error=thing.get("reason") or "the cloud reports it failed")
        said.append("Captain's record said it was running. The cloud says it failed, so the record now says failed.")
    if said:
        try:
            _note(ctx, pr["id"], "corrected the deployment's record from the cloud: " + said[0])
        except Exception:  # noqa: BLE001
            pass
    # A deploy job left failed, or left running by a process that died, for an endpoint that is up.
    if thing is not None and thing["state"] in ("running", "asleep"):
        from . import deploy as DP

        if DP.close_stale(ctx, pr["id"], env):
            said.append("An earlier deploy was still marked as failed. The model is running, so that is closed.")
    return rec, said


def deployed(ctx: Context, fresh: bool = False) -> dict:
    """{rows, asked, text, read_at}: every deployed model across projects and clouds, as the clouds report them.
    `asked` says which places were asked and which could not be. `fresh` skips the few-second cache."""
    key = ctx.workspace.name
    # The kept answer is only good while Captain's own records are as they were: a deploy or a stop a second ago
    # must show at once, so the records are read every time (they are on this computer or in the workspace) and
    # only the clouds' answers are kept.
    records = _records(ctx)
    going = _deploying(ctx)  # deploys under way now, by (project, env)
    mark = tuple(sorted((pr["id"], env, str(rec.get("id")), str(rec.get("status")), str(rec.get("model_version"))) for pr, env, rec in records)) + \
        tuple(sorted((k[0], k[1], "deploying", str(v["step"]["id"])) for k, v in going.items()))
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < TTL_S and hit[2] == mark:
            return hit[1]
    srcs = sources(ctx)
    asked, found = [], {}
    from . import cloud_watch as WT

    slowed = {(w["cloud"], w["login"], w["region"]): w["text"] for w in WT.status(ctx) if w.get("text")}
    for s in srcs:
        # While a screen is looking, a watcher asks each place every second: its answer is used, and this read
        # makes no call of its own.
        things, err = WT.recent(ctx, s) or _ask(ctx, s)
        asked.append({"cloud": s["cloud"], "cloud_label": CLOUDS[s["cloud"]], "login": s["login"], "region": s["region"], "ok": err is None,
                      "error": err, "found": len(things), "watching": slowed.get((s["cloud"], s["login"], s["region"]))})
        found[(s["cloud"], s["login"], s["region"])] = (things, err)
    rows, matched = [], set()
    for pr, env, rec in records:
        if (pr["id"], env) in going and rec.get("status") != "running":
            continue  # being deployed now: its row below says so, in place of what Captain remembers of the last run
        cloud, login, region, target = _where_of(ctx, rec)
        if cloud == "local":
            state, reason = _local_state(rec)
            rows.append({**NO_FACTS, **_row(pr, env, rec, "local", None, None, None, state, reason, "local", "This computer", [])})
            continue
        things, err = _place(found, cloud, login, region)
        ok = things is not None and err is None
        name = (rec.get("endpoint") or {}).get("name")
        thing = None
        if ok:
            mine = lambda t: _same(t.get("project"), pr["id"]) and _same(t.get("env"), env)  # noqa: E731
            thing = next((t for t in things if name and t["name"] == name), None) or next(
                (t for t in things if _same(t.get("stage"), rec.get("id")) or (not name and mine(t))), None)
            if thing is None and rec.get("status") not in ("running", "starting"):
                # A record that is not up, and no endpoint by its name: one this project's tags name still counts.
                thing = next((t for t in things if mine(t) and id(t) not in matched), None)
        rec, said = _correct(ctx, pr, env, rec, thing, ok, region)
        if thing is not None:
            matched.add(id(thing))
        machine = _machine(ctx, rec, thing, target)
        facts = _system(ctx, cloud, target, region, machine, thing, rec)
        if not ok:
            rows.append({**facts, **_row(pr, env, rec, cloud, login, region, None, "unknown" if rec.get("status") in ("running", "starting") else
                             ("failed" if rec.get("status") == "failed" else "stopped"),
                             f"Captain couldn't ask {CLOUDS.get(cloud, cloud)} just now: {err}" if rec.get("status") in ("running", "starting") else
                             rec.get("stop_reason") or rec.get("error"), "unknown", machine, [])})
        elif thing is None:
            gone = any("removed outside Captain" in x for x in said) or rec.get("stop_reason") == "It was removed outside Captain."
            rows.append({**facts, **_row(pr, env, rec, cloud, login, region, None, "gone" if gone else ("failed" if rec.get("status") == "failed" else "stopped"),
                             rec.get("stop_reason") or rec.get("error"), "gone" if gone else "linked", machine, said)})
        else:
            rows.append({**facts, **_row(pr, env, rec, cloud, login, region, thing, thing["state"], thing.get("reason"), "linked", machine, said)})
    for (cloud, login, region), (things, _err) in found.items():
        for t in things or []:
            if id(t) not in matched:
                rows.append({**NO_FACTS, **_row(None, None, None, cloud, login, t.get("region") or region, t, t["state"], t.get("reason"), "unlinked", t.get("machine"), [])})
    # A deploy under way is a row from the moment it is accepted: there is nothing in the cloud to find yet, and
    # a list that says "No models running" while one is being built is wrong.
    for (pid, env), g in going.items():
        live = next((r for r in rows if r["project"] == pid and r["env"] == env and r["link"] != "unlinked"), None)
        if live is not None:  # a new version going onto a model that is running: the row stays, and says so
            live["deploying"] = g
            continue
        rows.append(_deploying_row(g))
    # Which version of the deployment each one is, from the project's own history of what was put on it.
    for r in rows:
        if r["link"] == "unlinked" or not r.get("project") or r["state"] == "deploying":
            continue
        try:
            from . import traffic as TF

            live = next((v for v in TF.versions(ctx, r["project"])["rows"] if v.get("live")), None)
            r["version_n"] = live["n"] if live else None
        except Exception:  # noqa: BLE001  a project with no history yet has no number, and says none
            r["version_n"] = None
    order = {"deploying": -1, "failed": 0, "starting": 1, "running": 2, "asleep": 3, "unknown": 4, "gone": 5, "stopped": 6}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -(dt.datetime.fromisoformat(r["since"]).timestamp() if _iso(r.get("since")) else 0)))
    mine = [r for r in rows if r["link"] != "unlinked"]
    up = sum(1 for r in mine if r["state"] in ("running", "asleep", "starting"))
    loose = len(rows) - len(mine)
    could_not = [a for a in asked if not a["ok"]]
    # What is in a cloud now is what is counted. A model stopped earlier is remembered, not deployed.
    text = _summary(up, [r["state"] for r in mine if r["in_cloud"] and r["state"] not in ("running", "asleep", "starting", "deploying")], loose,
                    sum(1 for r in mine if r["state"] == "deploying"))
    earlier = sum(1 for r in mine if not r["in_cloud"])
    if could_not:
        text += " Could not ask " + ", ".join(sorted({a["cloud_label"] for a in could_not})) + " just now."
    out = {"rows": rows, "asked": asked, "text": text, "read_at": _now(), "ttl_s": TTL_S, "stopped_earlier": earlier,
           "stopped_text": f"Stopped earlier: {earlier} model{'s' if earlier != 1 else ''}." if earlier else None}
    # Marked with the records as they stand after any correction, so the corrected answer is the one kept.
    after = tuple(sorted((pr["id"], env, str(rec.get("id")), str(rec.get("status")), str(rec.get("model_version"))) for pr, env, rec in _records(ctx))) + \
        tuple(sorted((k[0], k[1], "deploying", str(v["step"]["id"])) for k, v in going.items()))
    with _lock:
        _cache[key] = (time.monotonic(), out, after)
    return out


def _place(found: dict, cloud: str, login: str | None, region: str | None) -> tuple[list | None, str | None]:
    """What the place a record lives in answered. Matched on the cloud, login and region, then more loosely: a
    target with no login of its own stands for that cloud's login, and so does the only place asked in that cloud."""
    if (cloud, login, region) in found:
        return found[(cloud, login, region)]
    same = [(k, v) for k, v in found.items() if k[0] == cloud]
    for k, v in same:
        if (k[1] is None or login is None or k[1] == login) and (k[2] is None or region is None or k[2] == region):
            return v
    if len(same) == 1:
        return same[0][1]
    return None, "This cloud login is not connected in Captain any more."


def _same(tag, value) -> bool:
    """A cloud's tag against Captain's own id. Some clouds only take lower-case letters, digits and dashes in a
    label, so the tag is the id made to fit."""
    from ..compute_providers.serving import clean_name

    if not tag or not value:
        return False
    return str(tag) == str(value) or str(tag) == clean_name(str(value), 60)


def _summary(up: int, down: list[str], loose: int, deploying: int = 0) -> str:
    """The line beside the screen's title: what is running now. "2 models running.", "No models running.", with a
    word for one being deployed ("1 deploying") and for any that is in a cloud and not answering ("1 failed").
    Models stopped earlier are not in a cloud and are not counted here: they have their own line."""
    models = lambda k: f"{k} model{'s' if k != 1 else ''}"  # noqa: E731
    said = f"{models(up)} running" if up else "No models running"
    failed = sum(1 for s in down if s == "failed")
    other = len(down) - failed
    if deploying:
        said += f", {deploying} deploying"
    if failed:
        said += f", {failed} failed"
    if other:
        said += f", {other} not answering"
    return said + (f", {loose} found in your cloud with no project" if loose else "") + "."


def span_words(seconds: float) -> str:
    """A running time as a row says it: "48 s", "2 min 10 s"."""
    s = max(0, int(seconds))
    return f"{s} s" if s < 90 else f"{s // 60} min {s % 60} s" if s % 60 else f"{s // 60} min"


def _deploying(ctx: Context) -> dict:
    """Every deploy under way now: {(project, env): {project, project_name, env, host, label, login, model_version,
    started_at, started_by, step: {id, title, started_at, typical_s, typical}, steps, last_line}}. Read from each
    project's deploy record. One that has said nothing for longer than a run can be silent is not counted: its
    process is gone."""
    from .. import projects as PR
    from . import deploy as DP

    out = {}
    now = dt.datetime.now(dt.timezone.utc)
    for pr in PR.listing(ctx):
        try:
            doc, _ = DP._read(ctx, pr["id"], ENV)
        except Exception:  # noqa: BLE001
            continue
        if not doc or doc.get("status") != "running":
            continue
        try:
            if (now - dt.datetime.fromisoformat(doc["updated_at"])).total_seconds() > DP.STALE_S:
                continue
        except (KeyError, ValueError):
            continue
        steps = doc.get("steps") or []
        cur = next((x for x in steps if x.get("status") == "running"), None) or next((x for x in steps if x.get("status") == "pending"), None) or (steps[-1] if steps else {})
        out[(pr["id"], ENV)] = {
            "project": pr["id"], "project_name": pr.get("name") or pr["id"], "env": ENV, "host": doc.get("host"), "label": doc.get("label") or CLOUDS.get(doc.get("host"), "the cloud"),
            "login": doc.get("login"), "model_version": doc.get("model_version"), "started_at": doc.get("started_at"), "started_by": doc.get("started_by"),
            "step": {"id": cur.get("id"), "title": cur.get("title"), "started_at": cur.get("started_at") or doc.get("started_at"),
                     "typical_s": cur.get("typical_s"), "typical": cur.get("typical")},
            "steps": [{"id": x.get("id"), "title": x.get("title"), "status": x.get("status"), "seconds": x.get("seconds"), "typical_s": x.get("typical_s"),
                       "typical": x.get("typical"), "started_at": x.get("started_at")} for x in steps],
            "last_line": ((cur.get("lines") or [None])[-1])}
    return out


def deploying_words(g: dict, now: dt.datetime | None = None) -> str:
    """A deploy under way in one sentence: "Deploying to AWS: Building the model's image, 48 s so far (usually about
    a minute and a half)." A page that ticks draws the same sentence from the same fields on its own clock."""
    step = g["step"]
    try:
        ran = ((now or dt.datetime.now(dt.timezone.utc)) - dt.datetime.fromisoformat(str(step["started_at"]))).total_seconds()
    except (TypeError, ValueError):
        ran = None
    said = f"Deploying to {g['label']}: {step.get('title') or 'working'}"
    if ran is not None:
        said += f", {span_words(ran)} so far"
    return said + (f" (usually {step['typical']})." if step.get("typical") else ".")


def _deploying_row(g: dict) -> dict:
    cloud = g.get("host") or "local"
    return {**NO_FACTS, "id": f"{g['project']}:{g['env']}", "project": g["project"], "project_name": g["project_name"], "env": g["env"],
            "model_version": g.get("model_version"), "model_label": model_words(g.get("model_version"), g["project"]),
            "cloud": cloud, "cloud_label": CLOUDS.get(cloud, cloud), "login": g.get("login"), "region": None, "machine": None, "where": g["label"],
            "name": None, "thing": THING.get(cloud), "copies": {"now": None, "min": None, "max": None},
            "state": "deploying", "state_label": "Deploying", "state_text": deploying_words(g), "reason": None,
            "since": g.get("started_at"), "stopped_at": None, "by": g.get("started_by"), "by_name": short_name(g.get("started_by")),
            "link": "linked", "record_status": None, "corrected": [], "resources": [], "in_cloud": True, "remembered_text": None,
            "sleeps": False, "address": None, "version_n": None, "deploying": g,
            # Nothing is offered that would act on a model still being made. A deploy can't be cancelled safely yet.
            "can": []}


def _iso(s) -> bool:
    try:
        dt.datetime.fromisoformat(str(s))
        return True
    except (TypeError, ValueError):
        return False


def one(ctx: Context, pid: str, env: str = ENV, fresh: bool = False) -> dict | None:
    """The row for one project's deployment, or None when it has none."""
    return next((r for r in deployed(ctx, fresh)["rows"] if r["project"] == pid and r["env"] == env and r["link"] != "unlinked"), None)


def differences(ctx: Context) -> dict:
    """What differs between Captain's records and the cloud, for `cst deployments sync`: what was just corrected,
    what the cloud has that no project knows, and which places could not be asked."""
    d = deployed(ctx, fresh=True)
    fixed = [{"project": r["project"], "env": r["env"], "what": c} for r in d["rows"] for c in r["corrected"]]
    loose = [{"cloud": r["cloud_label"], "login": r["login"], "region": r["region"], "name": r["name"], "state": r["state_label"]}
             for r in d["rows"] if r["link"] == "unlinked"]
    could_not = [a for a in d["asked"] if not a["ok"]]
    lines = [f"{x['project']}: {x['what']}" for x in fixed]
    lines += [f"{x['name']}: found in your {x['cloud']} account ({x['region'] or x['login']}), not linked to a project. {x['state']}." for x in loose]
    lines += [f"Could not ask {a['cloud_label']} ({a['login'] or 'no login'}): {a['error']}" for a in could_not]
    return {"corrected": fixed, "unlinked": loose, "not_asked": could_not, "in_sync": not fixed and not loose and not could_not,
            "text": "\n".join(lines) if lines else "Captain's records match the cloud. Nothing to correct."}


# ---- things found in the cloud that no project knows ----------------------------------------------


def _found_row(ctx: Context, row_id: str) -> tuple[dict, dict]:
    """(the row, the place it was found in) for something in the cloud that no project knows."""
    d = deployed(ctx, fresh=True)
    row = next((r for r in d["rows"] if r["id"] == row_id and r["link"] == "unlinked"), None)
    if row is None:
        raise ValueError("That is not in the cloud any more, or a project already has it. Look at the list again.")
    src = next((s for s in sources(ctx) if s["cloud"] == row["cloud"] and s["login"] in (row["login"], None)
                and (s["region"] in (row["region"], None) or row["region"] is None)), None)
    if src is None:
        raise ValueError(f"Captain is not connected to that {CLOUDS.get(row['cloud'], row['cloud'])} login any more.")
    return row, src


def remove_found(ctx: Context, row_id: str, confirm: bool = False) -> dict:
    """Remove from the cloud something Captain made that no project knows. {removed, left, text}. Needs `confirm`:
    it cannot be undone. Only what the listing itself found is removed, by name."""
    from .. import compute as C

    row, src = _found_row(ctx, row_id)
    what = f"{row['thing'] or 'deployment'} {row['name']}"
    if not confirm:
        raise ValueError(f"Confirm first. This deletes the {what} from your {row['cloud_label']} account. It cannot be undone.")
    left = C.unstage(ctx, src["target"], row["resources"])
    forget(ctx)
    return {"removed": [r for r in row["resources"] if not any(x.get("name") == r["name"] and x.get("type") == r["type"] for x in left)],
            "left": left,
            "text": f"Removed the {what} from your {row['cloud_label']} account." if not left else
                    f"Could not remove everything: {'; '.join(str(x.get('name')) + ': ' + str(x.get('error') or 'still there') for x in left)}"}


def link(ctx: Context, row_id: str, pid: str, env: str = ENV) -> dict:
    """Give a project something found in the cloud, so it shows as that project's deployment and can be stopped,
    watched and removed from there. Captain does not know which model version it runs or its key: the record says
    so, and asking it for results needs the key the deployment was made with."""
    from .. import projects as PR
    from ..model.entities import new_id
    from ..workspace import whoami
    from . import _note, _save, state

    row, src = _found_row(ctx, row_id)
    if not any(p["id"] == pid for p in PR.listing(ctx)):
        raise ValueError(f"There is no project {pid}.")
    cur = state(ctx, pid, env)
    if cur and cur.get("status") in ("running", "starting"):
        raise ValueError("That project already has a model deployed. Stop it first, or pick another project.")
    if src["target"].get("virtual"):
        raise ValueError(f"This workspace has not set up {row['cloud_label']} yet, so it has nowhere to keep this. Deploy any model to "
                         f"{row['cloud_label']} once from a project's page, then link this.")
    rec = _save(ctx, pid, env, rec={
        "id": row.get("stage") or ("stg-" + new_id().lower()[-10:]), "env": env, "status": "running", "target": src["target"]["name"],
        "kind": src["target"]["kind"], "host": row["cloud"], "login": row["login"], "model_version": None, "arms": {}, "serve": None,
        "endpoint": {"name": row["name"], **({"region": row["region"]} if row["region"] else {})}, "resources": row["resources"],
        "started_at": row.get("since") or _now(), "started_by": row.get("by"), "linked": {"at": _now(), "by": whoami()}, "needs_key": True,
        "scale_to_zero": row["cloud"] != "aws"})
    try:
        _note(ctx, pid, f"linked {row['thing'] or 'a deployment'} {row['name']}, found in the {row['cloud_label']} account, to this project")
    except Exception:  # noqa: BLE001
        pass
    forget(ctx)
    return {"project": pid, "env": env, "record": {k: rec.get(k) for k in ("id", "status", "target", "endpoint", "host", "login")},
            "text": f"{row['name']} is now this project's deployment. Captain does not know which model version it runs, or its key: "
                    "to ask it for results, deploy from this project again, or use the key it was made with."}


# ---- what the first deploy created in each account -------------------------------------------------

# What each kind of thing is for, in a sentence a person who has never deployed a model can follow. Matched on the
# kind's name, most specific first.
WHAT_FOR = (
    ("s3-object", "The files the model's image is built from. Kept so the image can be built again without sending them."),
    ("s3-folder", "The files the model's image is built from. Kept so the image can be built again without sending them."),
    ("log-group", "What the image build printed while it ran. Kept so a build that failed can be read."),
    ("build", "Builds the model's image inside your account. It runs only when an image is built."),
    ("codebuild", "Builds the model's image inside your account. It runs only when an image is built."),
    ("repository", "Holds the model's image in your account."),
    ("registry", "Holds the model's image in your account."),
    ("role", "What a running model is allowed to do: read its own files and write its request log. Nothing else."),
    ("identity", "What a running model is allowed to do: read its own files and write its request log. Nothing else."),
    ("service-account", "What a running model is allowed to do: read its own files and write its request log. Nothing else."),
    ("bucket", "Holds the model's files and its request log."),
    ("storage", "Holds the model's files and its request log."),
    ("volume", "Holds the model's files and its request log."),
    ("environment", "The place in your account where a deployed model's copies run."),
    ("resource-group", "The folder in your account that holds everything else here, so it can be removed whole."),
    ("target", "Captain's own note of where to deploy. It is not in your cloud account."),
)


# The kind of thing, as a person says it. Two things can share a name (a role and a repository both called
# "captain-serving"), so the kind is shown beside the name.
KIND_WORDS = (("s3-object", "Build package"), ("s3-folder", "Build package"), ("log-group", "Build log"), ("codebuild", "Image build"), ("build", "Image build"), ("repository", "Image repository"), ("registry", "Image registry"),
              ("role", "Role"), ("identity", "Identity"), ("service-account", "Service account"), ("bucket", "Bucket"), ("storage", "Storage account"),
              ("volume", "Volume"), ("environment", "Environment"), ("resource-group", "Resource group"), ("target", "Captain's note"))


def _kind_word(kind: str, name: str = "") -> str:
    k = str(kind or "").lower()
    if "role" in k and "build" in str(name).lower():
        return "Role for the image build"
    return next((word for key, word in KIND_WORDS if key in k), "Made by the first deploy")


def _what_for(kind: str) -> str:
    k = str(kind or "").lower()
    return next((text for word, text in WHAT_FOR if word in k), "Made by the first deploy. Deployments here use it.")


def _same_word(login, cloud: str) -> bool:
    """Whether a login's name only repeats its cloud: "aws" for AWS, "google" or "gcp" for Google Cloud."""
    w = "".join(ch for ch in str(login or "").lower() if ch.isalnum())
    return not w or w in {cloud, CLOUDS.get(cloud, "").lower().replace(" ", ""), {"gcp": "google", "aws": "amazon", "azure": "microsoft"}.get(cloud, cloud)}


def created(ctx: Context) -> dict:
    """What Captain created in each cloud account the first time a model was deployed there: {places: [{cloud,
    cloud_label, login, region, status, things: [{kind, name, what_for}], in_use_by, can_remove, text}], text}.
    Read from the workspace alone. Removing a place's setup is refused while a model is deployed on it."""
    from . import hosts as H

    rows = {(r["cloud"], r["project_name"] or r["project"]) for r in deployed(ctx)["rows"]
            if r["link"] != "unlinked" and r["state"] in ("running", "asleep", "starting")}
    places = []
    for c in ("aws", "modal", "gcp", "azure"):
        try:
            st = H.get(c).status(ctx)
        except Exception as e:  # noqa: BLE001
            st = {"status": "unknown", "created": [], "text": str(e)[:200]}
        # Captain's own note of where to deploy is not in the cloud account, so it is not listed as something that
        # is. Removing a place's setup still clears it.
        made = [x for x in st.get("created") or [] if "target" not in str(x.get("type") or "").lower()]
        if st.get("status") in (None, "none", "removed") or not made:
            continue
        using = sorted(name for cloud, name in rows if cloud == c and name)
        # The login's name is said only when it tells a person something: "aws" beside "AWS" does not.
        who = st.get("profile")
        places.append({"cloud": c, "cloud_label": CLOUDS[c], "login": who, "login_shown": None if _same_word(who, c) else who,
                       "region": st.get("region"), "status": st.get("status"),
                       "created_at": st.get("created_at"), "created_by": st.get("created_by"),
                       "things": [{"kind": x.get("type"), "kind_label": _kind_word(x.get("type"), x.get("name")), "name": x.get("name"),
                                   # A long address is shown by its last part: the whole of it is in `name`.
                                   "short": (str(x.get("name") or "").rstrip("/").rsplit("/", 1)[-1] or x.get("name")) if "/" in str(x.get("name") or "") else x.get("name"),
                                   "what_for": ("Lets the image build read its files and save the image. Nothing else." if "role" in str(x.get("type")).lower()
                                                and "build" in str(x.get("name")).lower() else _what_for(x.get("type")))} for x in made],
                       "in_use_by": using, "can_remove": not using,
                       "text": (f"{len(made)} thing{'s' if len(made) != 1 else ''} in your {CLOUDS[c]} account."
                                + (f" In use by {', '.join(using)}: stop that first to remove these." if using else
                                   " Nothing is deployed here, so they can be removed."))})
    return {"places": places,
            "text": "Captain has created nothing in your cloud accounts." if not places else
                    "What the first deploy to each cloud created. A deployed model uses them, and they cost nothing or close to nothing while idle."}


# ---- a model that was stopped earlier ----------------------------------------------------------------


def forget_stopped(ctx: Context, pid: str, env: str = ENV, confirm: bool = False) -> dict:
    """Forget a model that was stopped: remove Captain's own record of the deployment. Nothing in any cloud is
    touched, because nothing of it is there. Refused while anything of it still exists in a cloud: stop or remove
    that first."""
    from .. import project_state as PS
    from . import _note

    row = one(ctx, pid, env, fresh=True)
    if row is None:
        raise ValueError(f"{pid} has no deployment to forget.")
    if row["in_cloud"]:
        raise ValueError(f"{row['project_name']}'s model is still in {row['cloud_label']}: {row['state_text']} Stop it or remove it from the cloud first.")
    if not confirm:
        raise ValueError(f"Confirm first. Captain will forget {row['project_name']}'s stopped model. Nothing in the cloud changes, and it can't be started again from here.")

    def ch(doc):
        st = dict(doc.get("staging") or {})
        st.pop(env, None)
        doc["staging"] = st
        return True

    PS._update(ctx, pid, ch)
    try:
        _note(ctx, pid, f"forgot the stopped deployment of {row.get('model_version') or 'a model'}")
    except Exception:  # noqa: BLE001
        pass
    forget(ctx)
    return {"project": pid, "env": env, "text": f"Forgot {row['project_name']}'s stopped model. Nothing in the cloud changed."}


# ---- where one model runs, in full -------------------------------------------------------------------


def where_it_runs(ctx: Context, pid: str, env: str = ENV) -> dict:
    """Where one deployed model lives, as a small table: {facts: [{id, label, value, copy, source}], text}. Each
    value comes from the cloud's own describe call where there is one (`source: "cloud"`), and from Captain's record
    otherwise (`source: "record"`), and says which. Read-only. A value nobody has is left out, never made up."""
    from .. import compute as C
    from .. import compute_providers as P
    from . import sizing as SZ
    from . import state
    from . import traffic as TF

    try:
        rec = state(ctx, pid, env)
    except NotFound:
        rec = None
    row = one(ctx, pid, env) if rec else None
    if not rec or row is None:
        raise ValueError(f"{pid} has no deployment.")
    cloud, login, region, target = _where_of(ctx, rec)
    label = CLOUDS.get(cloud, cloud)
    got, why = {}, None
    prov = None
    if cloud != "local" and target is not None and row["in_cloud"]:
        try:
            prov = P.get(target, ctx)
            fn = getattr(prov, "where", None)
            got = fn(rec) if callable(fn) else {}
        except Exception as e:  # noqa: BLE001
            why = str(e)[:200]
    facts: list[dict] = []

    def add(fid: str, name: str, value, *, cloud_value=None, copy: bool = False):
        v = cloud_value if cloud_value not in (None, "") else value
        if v in (None, ""):
            return
        facts.append({"id": fid, "label": name, "value": str(v), "copy": copy, "source": "cloud" if cloud_value not in (None, "") else "record",
                      "source_text": f"From {label}" if cloud_value not in (None, "") else "From Captain's record"})

    cfg = (target or {}).get("config") or {}
    add("address", "Address", row.get("address"), cloud_value=got.get("address"), copy=True)
    try:
        snip = TF.snippets(rec, ctx)
        call = snip.get("aws cli") or snip.get("curl")
        if rec.get("serves") == "predict":  # a model with named inputs: one line that sends its own example, marked as a test
            from . import predict as PD

            call = PD.try_info(ctx, pid, env)["request"]
        # A test request, so it is marked as one: it never counts as a shopper.
        add("call", "How to call it", call.replace('"traffic": "real"', '"traffic": "test"') if call else None, copy=True)
    except Exception:  # noqa: BLE001
        pass
    add("cloud", "Cloud and account", ", ".join(x for x in (label, row.get("account")) if x), cloud_value=(f"{label}, {got['account']}" if got.get("account") else None))
    add("region", "Region", region)
    # The machine as the cloud describes it: a named size on AWS, a CPU and memory size elsewhere. Else the target's.
    sized = {"cpu": got["cpu"], "memory_mb": got["memory_mb"]} if got.get("cpu") and got.get("memory_mb") else None
    m = SZ.machine_facts(cloud, sized or cfg, region, name=got.get("machine") or (row.get("machine") if cloud == "aws" else None))
    words = (m["words"] or row.get("machine")) if cloud != "local" else "This computer"
    price = f", about ${m['hourly_usd']:.2f} an hour for each copy" if m.get("hourly_usd") else ""
    add("machine", "Machine", (words + price) if words else None, cloud_value=((words + price) if (got.get("machine") or sized) and words else None))
    try:
        sc = prov.scale(rec) if prov is not None and callable(getattr(prov, "scale", None)) else {}
    except Exception:  # noqa: BLE001
        sc = {}
    now = got.get("copies_now", sc.get("copies_now"))
    lo = got.get("copies_min") if got.get("copies_min") is not None else sc.get("min_copies")
    hi = got.get("copies_max") if got.get("copies_max") is not None else sc.get("max_copies")
    if lo is not None and hi is not None:
        from_cloud = now is not None or got.get("copies_max") is not None
        add("copies", "Copies", f"Fewest {lo}, most {hi}",
            cloud_value=((f"{now} up now. " if now is not None else "") + f"Fewest {lo}, most {hi}") if from_cloud else None)
    built = ago_words(got.get("image_built"))
    add("image", "Image", rec.get("image") or cfg.get("image"), cloud_value=(got["image"] + (f", built {built}" if built else "")) if got.get("image") else None)
    files = None
    try:
        files = f"{C.serving_storage(ctx, target).rstrip('/')}/{rec['bundle']}" if target is not None and rec.get("bundle") else rec.get("bundle")
    except Exception:  # noqa: BLE001
        files = rec.get("bundle")
    add("files", "Model files", files)
    add("role", "Role it runs as", cfg.get("execution_role_arn") or cfg.get("service_account") or cfg.get("identity_id"), cloud_value=got.get("role"))
    add("logs", "Logs", None, cloud_value=got.get("logs"), copy=True)
    if rec.get("scale_to_zero"):
        add("stops", "Stops by itself", "It sleeps when no requests arrive and wakes on the next one. It bills nothing while asleep.")
    elif rec.get("idle_minutes"):
        add("stops", "Stops by itself after", f"{float(rec['idle_minutes']):g} minutes with no requests")
    when = ago_words(got.get("since") or rec.get("started_at"))
    add("by", "Deployed by", ", ".join(x for x in (short_name(rec.get("started_by")), when) if x))
    add("version", "Version", (f"Version {row['version_n']} of this deployment: " if row.get("version_n") else "") + f"{row.get('model_label') or rec.get('model_version')}"
        + (f" ({rec['model_version']})" if row.get("model_label") and rec.get("model_version") else ""))
    said = (f"Read from {label} just now." if got else f"{label} could not be asked just now ({why}). These are from Captain's record." if why else
            "On this computer." if cloud == "local" else
            f"Nothing of it is in {label} now. These are from Captain's record of when it ran." if not row["in_cloud"] else f"From Captain's record. Captain can't ask {label} for these yet.")
    return {"project": pid, "env": env, "facts": facts, "text": said, "from_cloud": bool(got)}
