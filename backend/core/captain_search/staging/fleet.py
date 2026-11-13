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
    where = CLOUDS.get(cloud, cloud) + (f", {region}" if region else "") + (f", {machine}" if machine else "")
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
        text = "Starting" + (f": {reason}" if reason else ".")
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
            "resources": (thing or {}).get("resources") or [],
            "sleeps": bool((rec or {}).get("scale_to_zero")), "address": ((rec or {}).get("endpoint") or {}).get("url"),
            "can": ([] if link == "unknown" else ["link", "remove"] if link == "unlinked" else
                    ["open", "stop", "rollback"] if state in ("running", "asleep", "starting") else ["open", "start", "remove"])}


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
    mark = tuple(sorted((pr["id"], env, str(rec.get("id")), str(rec.get("status")), str(rec.get("model_version"))) for pr, env, rec in records))
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < TTL_S and hit[2] == mark:
            return hit[1]
    srcs = sources(ctx)
    asked, found = [], {}
    for s in srcs:
        things, err = _ask(ctx, s)
        asked.append({"cloud": s["cloud"], "cloud_label": CLOUDS[s["cloud"]], "login": s["login"], "region": s["region"], "ok": err is None,
                      "error": err, "found": len(things)})
        found[(s["cloud"], s["login"], s["region"])] = (things, err)
    rows, matched = [], set()
    for pr, env, rec in records:
        cloud, login, region, target = _where_of(ctx, rec)
        if cloud == "local":
            state, reason = _local_state(rec)
            rows.append(_row(pr, env, rec, "local", None, None, None, state, reason, "local", "This computer", []))
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
        if not ok:
            rows.append(_row(pr, env, rec, cloud, login, region, None, "unknown" if rec.get("status") in ("running", "starting") else
                             ("failed" if rec.get("status") == "failed" else "stopped"),
                             f"Captain couldn't ask {CLOUDS.get(cloud, cloud)} just now: {err}" if rec.get("status") in ("running", "starting") else
                             rec.get("stop_reason") or rec.get("error"), "unknown", machine, []))
        elif thing is None:
            gone = any("removed outside Captain" in x for x in said) or rec.get("stop_reason") == "It was removed outside Captain."
            rows.append(_row(pr, env, rec, cloud, login, region, None, "gone" if gone else ("failed" if rec.get("status") == "failed" else "stopped"),
                             rec.get("stop_reason") or rec.get("error"), "gone" if gone else "linked", machine, said))
        else:
            rows.append(_row(pr, env, rec, cloud, login, region, thing, thing["state"], thing.get("reason"), "linked", machine, said))
    for (cloud, login, region), (things, _err) in found.items():
        for t in things or []:
            if id(t) not in matched:
                rows.append(_row(None, None, None, cloud, login, t.get("region") or region, t, t["state"], t.get("reason"), "unlinked", t.get("machine"), []))
    order = {"failed": 0, "starting": 1, "running": 2, "asleep": 3, "unknown": 4, "gone": 5, "stopped": 6}
    rows.sort(key=lambda r: (order.get(r["state"], 9), -(dt.datetime.fromisoformat(r["since"]).timestamp() if _iso(r.get("since")) else 0)))
    mine = [r for r in rows if r["link"] != "unlinked"]
    up = sum(1 for r in mine if r["state"] in ("running", "asleep", "starting"))
    loose = len(rows) - len(mine)
    could_not = [a for a in asked if not a["ok"]]
    text = _summary(up, [r["state"] for r in mine if r["state"] not in ("running", "asleep", "starting")], loose)
    if could_not:
        text += " Could not ask " + ", ".join(sorted({a["cloud_label"] for a in could_not})) + " just now."
    out = {"rows": rows, "asked": asked, "text": text, "read_at": _now(), "ttl_s": TTL_S}
    # Marked with the records as they stand after any correction, so the corrected answer is the one kept.
    after = tuple(sorted((pr["id"], env, str(rec.get("id")), str(rec.get("status")), str(rec.get("model_version"))) for pr, env, rec in _records(ctx)))
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


def _summary(up: int, down: list[str], loose: int) -> str:
    """The line beside the screen's title, counting models as a person would: "2 models running, 1 stopped",
    "1 model, not running", "No models are deployed yet."."""
    n = up + len(down)
    if not n and not loose:
        return "No models are deployed yet."
    models = lambda k: f"{k} model{'s' if k != 1 else ''}"  # noqa: E731
    # "stopped" when every one that is down was stopped. Otherwise some failed or could not be checked.
    word = "stopped" if all(s in ("stopped", "gone") for s in down) else "not running"
    if not n:
        said = "No models of yours are deployed"
    elif not down:
        said = f"{models(up)} running"
    elif not up:
        said = f"{models(n)}, {word if word == 'stopped' else 'not running'}" if n == 1 else f"{models(n)}, none running"
    else:
        said = f"{models(up)} running, {len(down)} {word}"
    return said + (f", {loose} found in your cloud with no project" if loose else "") + "."


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
