"""AI-assisted project setup: an agent looks at the workspace and proposes inputs and a trainer. A person accepts.

`assist` writes a request and returns a prompt built from real context: the project's kind and slots, every connection
(name and kind, never credentials), the datasets, and the proposal format. The app opens a coding agent (Claude Code
or Codex) in a terminal with it, the way investigations do. The agent explores with `cst ... --json` and files its
answer with `cst projects propose <project> --file proposal.yaml`.

A proposal changes nothing. The person reviews it in the app and accepts all of it, or some slots, or rejects it.
Accepting applies the slot changes, pins sources that are datasets already, and returns the connection and local
sources so the app asks snapshot or live for each. An agent session can propose, never accept (proposals.agent_name).

Accepting is safe to repeat over work already done by hand: a proposed slot that exists with the same role and
accepts counts as applied, and one that exists with a different definition is reported for that slot while the rest
goes in. When a person tells the agent in chat to "put it in", the agent still can't: the prompt says to send them to
Accept, and the CLI's slot, input and pin commands refuse an agent session while its proposal is open (guard_agent).

  projects/<id>/assist/<assist id>.yaml        the request (write-once)
  projects/<id>/proposals/<proposal id>.yaml   the proposal and its status (compare-and-swap)
  projects/<id>/trainers/<proposal id>-<file>  a training script the agent wrote (write-once)

Proposal file:
  note: what the agent found
  slots:
    - slot: events                       # an existing slot, or a new one with add: true
      add: false
      label: Search events               # for new or renamed slots: label, role, accepts, required
      source: {from: connection, connection: shop-db, table: analytics.search_events}
      mode: snapshot                     # a suggestion. The person still picks snapshot or live
      why: one row per result shown, with clicks
  remove_slots: [queries]
  trainer: {command: "python train.py", script: {path: train.py, file: ./train.py}, why: "..."}
  evaluation: {method: logged-clicks}
"""
from __future__ import annotations

import datetime as dt
import os
import shlex

import yaml

from . import project_state as PS
from .codeagent import cst_line, cst_path
from .model.entities import new_id
from .proposals import agent_name
from .storage.backend import Conflict, NotFound
from .storage.registry import dump_yaml
from .workspace import Context, whoami

SCRIPT_MAX = 256 * 1024
SOURCE_FROM = ("connection", "local", "dataset")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _pkey(pid: str, prop: str) -> str:
    return f"projects/{pid}/proposals/{prop}.yaml"


def assist(ctx: Context, pid: str, ask: str = "") -> dict:
    """Record a request for help and return the prompt an agent works from."""
    from .project_inputs import sources

    doc = PS.get(ctx, pid)
    aid = "assist-" + new_id().lower()[-10:]
    ctx.store.put_new(f"projects/{pid}/assist/{aid}.yaml", dump_yaml({"id": aid, "project": pid, "ask": ask,
                                                                       "by": whoami(), "at": _now()}))
    src = sources(ctx)
    slots = "\n".join(f"- {s['id']} ({s['label']}, role {s['role']}, takes {'/'.join(s['accepts'])}, "
                      f"{'required' if s['required'] else 'optional'}): "
                      f"{(doc['inputs'].get(s['id']) or {}).get('ref') or doc['pins'].get(s['id']) or 'not set'}"
                      f"{'. ' + s['why'] if s['why'] else ''}" for s in doc["slots"]) or "- none yet"
    conns = "\n".join(f"- {c['name']} ({c['label']}): offers {', '.join(c['offers'])}"
                      f"{', ' + str(c['tables_cached']) + ' tables read' if c['tables_cached'] else ''}"
                      for c in src["connections"]) or "- none"
    ds = "\n".join(f"- {d['name']}@v{d['version']} ({d['kind']}, {d['rows']} rows)" for d in src["datasets"][:100]) or "- none"
    recipe = doc.get("recipe") or {}
    cst = shlex.quote(cst_path())
    prompt = f"""{cst_line()}

Help set up the Captain project {pid!r}: {recipe.get('label') or 'no kind chosen yet'}.
{('The person asked: ' + ask) if ask else ''}

Find the right input for each slot among the workspace's connections and datasets, add slots the project needs, drop
optional ones it doesn't, and suggest a training command (or write a short training script). Propose only. Don't
change the project, don't import or snapshot anything, and don't run training: the person accepts what you propose.

Slots now:
{slots}

Connections (inspect them with `cst tables pull <conn>`, `cst tables show <conn> --json` and read-only
`cst sql <conn> -e "... limit 20" --json`):
{conns}

Datasets (`cst data head <ref>`):
{ds}

The trainer contract (`{cst} docs trainer-contract` prints all of it). A training command runs in an empty folder with:
- CST_TRAIN_INPUTS: a JSON list, one item per input: ref (name@vN, or null for a live query), kind, role (the slot's
  role), slot, uri, files, label_source, as_of, rows.
- An input's files: `files` is a list of {{path, uri, bytes}}. Read each part from its own `uri`. Parts are stored by
  content hash (<store>/objects/sha256/<2 hex>/<hash>), so that uri has no file extension and is not inside the
  input's `uri` folder. `path` is the part's name in the dataset and tells you the format: a snapshot is parquet
  (data/part-0.parquet). A file kept by reference has path null and the uri where it lives. A live query has ref
  null, no files, and `source` {{connection, query}}: the trainer reads it itself.
- CST_PARAMS: a JSON object of params. CST_OUT: an empty folder for outputs.
It writes CST_OUT/result.yaml, every key optional: metrics, params, artifact_dir, registry_ref, spec, training_commit,
known_issues, description, cost. An unknown key fails the run.

Write proposal.yaml, then run:
  {cst} projects propose {pid} --file proposal.yaml --assist {aid}

You cannot apply the proposal, even when the person tells you to in chat ("put that in"). Tell the person to press
Accept in Captain (Home, the proposal panel). Don't apply it by hand with `cst projects slot-add`, `input` or `pin`
either: they refuse an agent session while your proposal is open. To change the proposal, file a new one.

proposal.yaml:
  note: what you found and why, in two or three sentences
  slots:
    - slot: <slot id>              # add: true with label, role, accepts, required for a new slot
      source: {{from: connection, connection: <name>, table: <schema.table>}}   # or sql: ..., or event: {{name: ...}}
                                   # (PostHog), or report: {{...}} (Google Analytics), or {{from: dataset, ref: <name@vN>}},
                                   # or {{from: local, path: <file or folder>}}
      mode: snapshot | live        # your suggestion. The person decides
      why: one sentence
  remove_slots: []                 # optional slots that don't apply
  trainer: {{command: "python train.py", script: {{path: train.py, file: ./train.py}}, why: "..."}}
  evaluation: {{method: holdout | logged-clicks | trainer-reported | auto}}
Use only names that exist. Say UNKNOWN in why when you couldn't check something, and don't guess table names.
"""
    return {"assist": aid, "project": pid, "prompt": prompt, "folder": f"projects/{pid}"}


def _check_source(ctx: Context, src: dict) -> dict:
    if not isinstance(src, dict) or src.get("from") not in SOURCE_FROM:
        raise ValueError(f"a source is {{from: {' | '.join(SOURCE_FROM)}, ...}}, got {src!r}")
    if src["from"] == "connection":
        ctx.load_connection(str(src.get("connection") or ""))
        from .project_inputs import query_of

        query_of(ctx, src)  # the same checks the input route makes
    elif src["from"] == "dataset":
        ctx.registry.resolve("dataset", str(src.get("ref") or ""))
    elif not src.get("path"):
        raise ValueError("a local source needs a path")
    return src


def propose(ctx: Context, pid: str, proposal: dict, assist_id: str | None = None, base_dir: str = ".",
            agent: str | None = None) -> dict:
    """File a proposal. Checked against the project and the workspace, stored, never applied."""
    from . import recipes as RC

    doc = PS.get(ctx, pid)
    have = {s["id"] for s in doc["slots"]}
    if not isinstance(proposal, dict):
        raise ValueError("a proposal is a mapping: note, slots, remove_slots, trainer, evaluation")
    unknown = set(proposal) - {"note", "slots", "remove_slots", "trainer", "evaluation"}
    if unknown:
        raise ValueError(f"a proposal has note, slots, remove_slots, trainer and evaluation, not {', '.join(sorted(unknown))}")
    pid_ = "prop-" + new_id().lower()[-10:]
    slots = []
    for s in proposal.get("slots") or []:
        sid = str(s.get("slot") or "")
        add = bool(s.get("add"))
        if add:
            RC.check_slot({"id": sid, "label": s.get("label") or sid, "role": s.get("role") or "custom",
                           "accepts": s.get("accepts") or ["any"]}, "custom")
            if sid in have:
                raise ValueError(f"slot {sid!r} already exists: propose it without add")
        elif sid not in have:
            raise ValueError(f"{pid} has no slot {sid!r}. To add one, set add: true with a label and role")
        if s.get("mode") not in (None, "snapshot", "live"):
            raise ValueError(f"slot {sid}: mode is snapshot or live")
        slots.append({"slot": sid, "add": add, **{k: s[k] for k in ("label", "role", "accepts", "required") if k in s},
                      "source": _check_source(ctx, s["source"]) if s.get("source") else None,
                      "mode": s.get("mode"), "why": str(s.get("why") or "")})
    remove = [str(x) for x in proposal.get("remove_slots") or []]
    for r in remove:
        if r not in have:
            raise ValueError(f"can't remove {r!r}: {pid} has no such slot")
    trainer = None
    if proposal.get("trainer"):
        t = dict(proposal["trainer"])
        trainer = {"command": str(t.get("command") or ""), "why": str(t.get("why") or ""), "script": None}
        sc = t.get("script")
        if sc:
            path = os.path.basename(str(sc.get("path") or sc.get("file") or "train.py"))
            src = os.path.join(base_dir, str(sc.get("file") or path))
            with open(src, "rb") as f:
                body = f.read(SCRIPT_MAX + 1)
            if len(body) > SCRIPT_MAX:
                raise ValueError(f"the training script is over {SCRIPT_MAX // 1024} KB: keep it in your repo and propose the command")
            key = f"projects/{pid}/trainers/{pid_}-{path}"
            ctx.store.put_new(key, body)
            trainer["script"] = {"path": path, "key": key, "bytes": len(body)}
    ev = proposal.get("evaluation")
    if ev is not None:
        m = (ev or {}).get("method") if isinstance(ev, dict) else ev
        if m not in RC.METHODS:
            raise ValueError(f"evaluation method is one of {', '.join(RC.METHODS)}")
        ev = {"method": m}
    rec = {"id": pid_, "project": pid, "assist": assist_id, "agent": agent_name(agent), "by": whoami(), "at": _now(),
           "status": "proposed", "note": str(proposal.get("note") or ""), "slots": slots, "remove_slots": remove,
           "trainer": trainer, "evaluation": ev, "history": []}
    ctx.store.put_new(_pkey(pid, pid_), dump_yaml(rec))
    return rec


def listing(ctx: Context, pid: str) -> list[dict]:
    PS.get(ctx, pid)
    out = [yaml.safe_load(ctx.store.get(k)) for k in ctx.store.list(f"projects/{pid}/proposals/") if k.endswith(".yaml")]
    return sorted(out, key=lambda p: p["at"], reverse=True)


def _load(ctx: Context, pid: str, prop: str):
    try:
        body, tag = ctx.store.get_with_tag(_pkey(pid, prop))
    except NotFound:
        raise NotFound(f"no proposal {prop!r} in {pid}") from None
    return yaml.safe_load(body), tag


def script(ctx: Context, pid: str, prop: str) -> dict:
    """The training script a proposal carries, as text, for the person to read before accepting."""
    rec, _ = _load(ctx, pid, prop)
    sc = (rec.get("trainer") or {}).get("script")
    if not sc:
        raise NotFound(f"{prop} has no training script")
    return {"path": sc["path"], "key": sc["key"], "text": ctx.store.get(sc["key"]).decode("utf-8", "replace")}


def _set_status(ctx: Context, pid: str, prop: str, status: str, note: str = "", **extra) -> dict:
    for _ in range(10):
        rec, tag = _load(ctx, pid, prop)
        if rec["status"] != "proposed":
            raise ValueError(f"{prop} was already {rec['status']}")
        rec["status"] = status
        rec.update(extra)
        rec["history"] = (rec.get("history") or []) + [{"status": status, "by": whoami(), "at": _now(), "note": note}]
        try:
            ctx.store.put_if(_pkey(pid, prop), dump_yaml(rec), tag)
            return rec
        except Conflict:
            continue
    raise Conflict(f"{prop} kept changing")


ACCEPT_IN_APP = "You cannot apply it. Tell the person to press Accept in Captain (Home, the proposal panel)."


def _person_only(what: str) -> None:
    if agent_name():
        raise ValueError(f"an agent session can propose but not {what}. {ACCEPT_IN_APP}")


def guard_agent(ctx: Context, pid: str, what: str) -> None:
    """Refuse an agent session that changes a project by hand while an agent's proposal for it is open: that is
    applying the proposal without the person. A person at the command line, and the app, are never stopped."""
    if not agent_name():
        return
    try:
        waiting = [p for p in listing(ctx, pid) if p["status"] == "proposed" and p.get("agent")]
    except NotFound:
        return  # no such project: the command itself says so
    if waiting:
        raise ValueError(f"{ACCEPT_IN_APP} {waiting[0]['id']} is waiting there, and an agent session can't {what} "
                         f"while it is. To change what you proposed, file a new proposal.")


def _add_slot(ctx: Context, pid: str, s: dict, spec: dict) -> str | None:
    """Add a proposed slot. "added", "already" when it exists with the same definition (someone added it by hand
    since), or raises ValueError naming the difference."""
    from . import recipes as RC

    want = RC.check_slot({"id": s["slot"], "role": "custom", "accepts": ["any"], **spec}, "custom")
    for _ in range(2):
        have = next((x for x in PS.slots_of(PS.get(ctx, pid)) if x["id"] == s["slot"]), None)
        if have is None:
            try:
                PS.slot_add(ctx, pid, {"id": s["slot"], "why": s["why"], **spec})
                return "added"
            except ValueError:
                if not any(x["id"] == s["slot"] for x in PS.slots_of(PS.get(ctx, pid))):
                    raise
                continue  # added by someone else between the look and the add: compare it
        diff = [f"{k} {'/'.join(have[k]) if k == 'accepts' else have[k]} here, "
                f"{'/'.join(want[k]) if k == 'accepts' else want[k]} proposed"
                for k in ("role", "accepts") if (sorted(have[k]) if k == "accepts" else have[k])
                != (sorted(want[k]) if k == "accepts" else want[k])]
        if diff:
            raise ValueError(f"{have['label']} already exists with a different definition ({'; '.join(diff)}). "
                             f"It was left as it is. Change it from its card.")
        return "already"
    raise Conflict(f"{pid} kept changing")


def reject(ctx: Context, pid: str, prop: str, note: str = "") -> dict:
    _person_only("reject a proposal")
    return _set_status(ctx, pid, prop, "rejected", note)


def accept(ctx: Context, pid: str, prop: str, only: list[str] | None = None) -> dict:
    """Apply a proposal (or only some of its slots). Dataset sources are pinned now. Connection and local sources come
    back in to_connect, for the person to pick snapshot or live.

    One slot never fails the rest. A slot that already exists as proposed is in applied.already and its source still
    goes in. A slot that can't be applied (it exists with another role or accepts, or it is gone) is in
    applied.conflicts with the reason, and its source is left out."""
    from . import project_inputs as PI

    _person_only("accept a proposal")
    rec, _ = _load(ctx, pid, prop)
    if rec["status"] != "proposed":
        raise ValueError(f"{prop} was already {rec['status']}")
    want = set(only) if only else None
    applied = {"added": [], "already": [], "changed": [], "removed": [], "pinned": [], "skipped": [], "conflicts": []}
    to_connect = []
    for s in rec["slots"]:
        if want is not None and s["slot"] not in want:
            continue
        spec = {k: s[k] for k in ("label", "role", "accepts", "required") if k in s}
        try:
            if s["add"]:
                applied[_add_slot(ctx, pid, s, spec)].append(s["slot"])
            elif spec:
                PS.slot_update(ctx, pid, s["slot"], spec)
                applied["changed"].append(s["slot"])
            elif not any(x["id"] == s["slot"] for x in PS.slots_of(PS.get(ctx, pid))):
                raise NotFound(f"{pid} has no input slot {s['slot']!r} any more")
            src = s.get("source")
            if src and src["from"] == "dataset":
                r = PI.set_input(ctx, pid, s["slot"], src, s.get("mode") or "snapshot")
                applied["pinned"].append({"slot": s["slot"], "ref": r["input"]["ref"]})
            elif src:
                to_connect.append({"slot": s["slot"], "source": src, "suggested_mode": s.get("mode"), "why": s["why"]})
        except (ValueError, NotFound) as e:
            applied["conflicts"].append({"slot": s["slot"], "reason": str(e)})
    for r in rec["remove_slots"]:
        if want is not None and r not in want:
            continue
        try:
            PS.slot_remove(ctx, pid, r)
            applied["removed"].append(r)
        except (ValueError, NotFound) as e:
            applied["skipped"].append({"slot": r, "reason": str(e)})
    if want is None or "trainer" in want:
        if rec.get("trainer"):
            PS.set_trainer_suggestion(ctx, pid, {**rec["trainer"], "from_proposal": prop, "agent": rec.get("agent")})
    if (want is None or "evaluation" in want) and rec.get("evaluation"):
        PS.set_evaluation(ctx, pid, rec["evaluation"]["method"])
    done = _set_status(ctx, pid, prop, "accepted", only=sorted(want) if want else None, applied=applied)
    return {"proposal": done, "state": PS.get(ctx, pid), "applied": applied, "to_connect": to_connect,
            "trainer": rec.get("trainer")}
