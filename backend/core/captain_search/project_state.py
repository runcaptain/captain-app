"""A project's working state: what it is building, its inputs, its experiments, and its champion.

`projects/<id>/state.yaml`, written by compare-and-swap (Store.put_if), the same pattern as projects.yaml:

  kind:         the recipe it started from (recipes.py), or null for a project made before kinds
  slots:        its own input slots, copied from the recipe and then editable: [{id, label, role, accepts, required,
                why, origin}]. Missing on older projects, which read as the five original slots (LEGACY_SLOTS).
  inputs:       {slot: {mode: snapshot|live, from: connection|local|dataset, source, ref, rows, fingerprint, as_of}}
                (project_inputs.py writes these)
  pins:         {slot: name@vN or null}: the dataset ref each slot points at now (holdout: name@vN:holdout)
  evaluation:   {method: auto|holdout|logged-clicks|trainer-reported|none}
  experiments:  [{id, pipeline, training_set, labels, trainer, backtest, run, job, status, inputs_read, ...}]
  champion:     the pipeline ref the project ships, or null
  registry:     {arch, brand, uri}: where a published version goes (models publish reads it)

Nothing here names a particular slot: stages, evaluation and label independence read slot roles (catalog, events,
queries, labels, holdout, users, items, interactions, custom), so any slot set works.

Stages (`stages`) come from this doc plus the lineage index only. They never list the registry, so a project page
stays one listing (the edge prefix) however big the bucket is. A newer version of a pinned input is information
for the person, never an error: pins are exact on purpose.

Experiments are judged against the champion by the project's evaluation method (`evaluation_plan`): a sealed holdout
with judgments, a replay of logged searches graded from clicks, or the trainer's own reported metrics, each labelled
for what it is. With none of them there is no evaluation set yet, and nothing claims a winner. When the training
labels and the grades come from the same labeler, a win only shows the model learned that labeler, so the verdict
reads "agrees with its own labeler" (`label_independence`).
"""
from __future__ import annotations

import datetime as dt
import random
import re
import time

import yaml

from .model.entities import new_id
from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

SLOTS = {  # the five slots of projects made before kinds: slot -> (group on the page, dataset kinds it accepts)
    "catalog": ("data", ("catalog",)),
    "events": ("data", ("events", "logs")),
    "queries": ("data", ("queries",)),
    "labels": ("labels", ("judgments",)),
    "holdout": ("holdout", ("queries",)),
}
LEGACY_SLOTS = [{"id": s, "label": s.capitalize() if s != "holdout" else "Sealed test set", "role": s,
                 "accepts": list(k), "required": False, "why": "", "origin": "recipe"} for s, (_, k) in SLOTS.items()]
GROUP = {"labels": "labels", "holdout": "holdout"}  # role -> group on the page; everything else is data
VERDICTS = {"win": "beats the baseline", "loss": "loses to the baseline", "noise": "no clear difference from the baseline"}
OWN_LABELER = "agrees with its own labeler"
# Click grades only exist for products the old ranker showed. A ranker that surfaces other products has them counted
# as zero, so below this share of judged results in its top 10 a replay can't say which ranker is better.
THIN_COVERAGE = 0.5
TOO_FEW_JUDGED = "too few judged results to call"
NOT_ONLINE = "not yet confirmed online"
TRIES = 50


def key(pid: str) -> str:
    return f"projects/{pid}/state.yaml"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _empty(pid: str) -> dict:
    return {"project": pid, "kind": None, "recipe": None, "slots": None, "inputs": {}, "pins": {},
            "evaluation": {"method": "auto"}, "experiments": [], "champion": None, "champion_history": [],
            "registry": {}, "trainer_suggestion": None}


def slots_of(doc: dict) -> list[dict]:
    return doc["slots"] if doc.get("slots") is not None else [dict(s) for s in LEGACY_SLOTS]


def slot(doc: dict, sid: str) -> dict:
    s = next((s for s in slots_of(doc) if s["id"] == sid), None)
    if s is None:
        raise NotFound(f"{doc['project']} has no input slot {sid!r}. Slots: {', '.join(x['id'] for x in slots_of(doc)) or 'none'}")
    return s


def by_role(doc: dict, role: str) -> list[dict]:
    return [s for s in slots_of(doc) if s["role"] == role]


def pinned(doc: dict, role: str) -> str | None:
    """The first pinned dataset ref among the slots with this role."""
    return next((doc["pins"].get(s["id"]) for s in by_role(doc, role) if doc["pins"].get(s["id"])), None)


def _check_project(ctx: Context, pid: str) -> None:
    from . import projects as PR

    doc, _ = PR._read(ctx)
    PR._get(doc, pid)


def read(ctx: Context, pid: str) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(key(pid))
        doc = yaml.safe_load(body) or {}
    except NotFound:
        doc, tag = {}, None
    if doc.get("deleted"):  # a deleted project's state (stores never delete): a new project of that name starts clean
        doc = {}
    base = _empty(pid)
    for k, v in base.items():
        doc.setdefault(k, v)
    for s in slots_of(doc):
        doc["pins"].setdefault(s["id"], None)
    return doc, tag


def get(ctx: Context, pid: str) -> dict:
    _check_project(ctx, pid)
    doc = read(ctx, pid)[0]
    return {**doc, "slots": slots_of(doc)}


def _update(ctx: Context, pid: str, change, dry_run: bool = False):
    """Read, change, write if nobody wrote in between, else read again. Many writers converge; none is lost."""
    _check_project(ctx, pid)
    for i in range(TRIES):
        doc, tag = read(ctx, pid)
        out = change(doc)
        if dry_run:
            return out if out is not None else doc
        try:
            ctx.store.put_if(key(pid), yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return out if out is not None else doc
        except Conflict:
            time.sleep(random.uniform(0, 0.005 * (i + 1)))
    raise Conflict(f"project {pid} kept changing while saving. Try again.")


# ---- pins ---------------------------------------------------------------------------------------


def _exact(ctx: Context, sl: dict, raw: str) -> str:
    """The exact ref a slot pins for `raw`, checked against what the slot accepts."""
    from .ops import check_seal, ref

    m, split = ctx.registry.resolve("dataset", raw)
    kind = getattr(m.dataset_kind, "value", m.dataset_kind)
    ok = tuple(sl["accepts"])
    if "any" not in ok and kind not in ok:
        from . import classes as CL

        if CL.role_of(ctx, m.name) not in ok:
            raise ValueError(f"{ref(m)} holds {kind} rows, and the {sl['label']} input takes {' or '.join(ok)}")
    if sl["role"] == "holdout":
        if not m.holdout_seal:
            raise ValueError(f"{ref(m)} isn't sealed, so it has no test set yet. Seal it first: that keeps part of its queries back for judging.")
        s = check_seal(ctx, ref(m))
        if not s["ok"]:
            raise ValueError(f"{ref(m)} failed its seal check: {s['detail']}")
        return f"{ref(m)}:holdout"
    if split:
        raise ValueError(f"the {sl['label']} input takes a whole dataset, not the {split} split")
    return ref(m)


def dataset_fingerprint(m) -> str:
    """What a pinned dataset version is, as one hash: its files' checksums (or, by reference, their uris, sizes and
    etags). The same version always gives the same fingerprint."""
    import hashlib

    h = hashlib.sha256()
    for f in sorted(m.files, key=lambda f: f.path):
        h.update(f"{f.path}:{f.sha256}\n".encode())
    for e in sorted(m.external, key=lambda e: e.uri):
        h.update(f"{e.uri}:{e.bytes}:{e.etag or ''}:{e.sha256 or ''}\n".encode())
    return "sha256:" + h.hexdigest()


def simulated_mark(m) -> dict:
    """{"simulated": True} for a dataset version made by a generator (tagged synthetic), else nothing. An input's
    record carries it, so the project page can say so without loading the dataset."""
    from .historic import synthetic

    return {"simulated": True} if synthetic(m) else {}


def _dataset_input(ctx: Context, exact: str) -> dict:
    m, _ = ctx.registry.resolve("dataset", exact)
    return {"mode": "snapshot", "from": "dataset", "source": {"from": "dataset", "ref": exact}, "ref": exact,
            "rows": m.row_count or None, "fingerprint": dataset_fingerprint(m), "as_of": (m.tags or {}).get("as_of"),
            "pinned_at": _now(), "pinned_by": whoami(), **simulated_mark(m)}


def pin(ctx: Context, pid: str, refs: dict[str, str | None], dry_run: bool = False) -> dict:
    """Pin inputs to exact versions. A value of None or "" unpins that slot."""
    doc0, _ = read(ctx, pid)
    known = {s["id"]: s for s in slots_of(doc0)}
    bad = [s for s in refs if s not in known]
    if bad:
        raise ValueError(f"{pid}'s inputs are {', '.join(known) or 'none yet'}, not {', '.join(bad)}. Add a slot first.")
    exact = {s: (_exact(ctx, known[s], r) if r else None) for s, r in refs.items()}
    recs = {s: (_dataset_input(ctx, e) if e else None) for s, e in exact.items()}

    def ch(doc):
        before = dict(doc["pins"])
        doc["pins"].update(exact)
        for s, r in recs.items():
            if r is None:
                doc["inputs"].pop(s, None)
            else:
                doc["inputs"][s] = r
        doc["pins_updated"] = {"at": _now(), "by": whoami()}
        return {"project": pid, "pins": doc["pins"], "changed": {s: {"from": before.get(s), "to": v}
                                                                for s, v in exact.items() if before.get(s) != v},
                "dry_run": dry_run}

    return _update(ctx, pid, ch, dry_run)


def set_input(ctx: Context, pid: str, sid: str, rec: dict | None, pin_ref: str | None) -> dict:
    """Record what a slot reads (project_inputs builds `rec`), and the dataset ref it pins, if any."""
    def ch(doc):
        slot(doc, sid)
        if rec is None:
            doc["inputs"].pop(sid, None)
        else:
            doc["inputs"][sid] = rec
        doc["pins"][sid] = pin_ref
        doc["pins_updated"] = {"at": _now(), "by": whoami()}
        return {"project": pid, "slot": sid, "input": rec, "pins": doc["pins"]}

    return _update(ctx, pid, ch)


def note_read(ctx: Context, pid: str, items: list[dict]) -> None:
    """Remember, on each input, when a run last read it and what it found (shown as "last read")."""
    items = [i for i in items if i.get("slot")]
    if not items:
        return

    def ch(doc):
        for i in items:
            rec = doc["inputs"].get(i["slot"])
            if rec is not None:
                rec["last_read"] = {"at": i.get("read_at") or _now(), "rows": i.get("rows"),
                                    "fingerprint": i.get("fingerprint")}
        return None

    try:
        _update(ctx, pid, ch)
    except (Conflict, NotFound):  # a note, never a reason to fail the run
        pass


# ---- kind, slots and evaluation -----------------------------------------------------------------


def set_kind(ctx: Context, pid: str, kind: str) -> dict:
    """Start from a recipe: copy its slots and evaluation. Inputs whose slot id is still there are kept."""
    from . import recipes as RC

    r = RC.get(ctx, kind)

    def ch(doc):
        doc["kind"] = r["id"]
        doc["recipe"] = {"id": r["id"], "label": r["label"], "source": r["source"]}
        doc["slots"] = [dict(s) for s in r["slots"]]
        ids = {s["id"] for s in doc["slots"]}
        doc["pins"] = {k: v for k, v in doc["pins"].items() if k in ids}
        doc["inputs"] = {k: v for k, v in doc["inputs"].items() if k in ids}
        for i in ids:
            doc["pins"].setdefault(i, None)
        doc["evaluation"] = {"method": r["evaluation"]["method"]}
        doc["trainer_suggestion"] = (r["trainers"] or [None])[0]
        return None

    _update(ctx, pid, ch)
    return get(ctx, pid)


def _own_slots(doc: dict) -> list[dict]:
    if doc.get("slots") is None:  # an older project: its five slots become its own, editable list
        doc["slots"] = [dict(s) for s in LEGACY_SLOTS]
    return doc["slots"]


def slot_add(ctx: Context, pid: str, spec: dict) -> dict:
    from . import recipes as RC

    new = RC.check_slot({"role": "custom", "accepts": ["any"], "required": False, **spec}, origin="custom")

    def ch(doc):
        sl = _own_slots(doc)
        if any(s["id"] == new["id"] for s in sl):
            raise ValueError(f"{pid} already has an input {new['id']!r}. Pick another name.")
        sl.append(new)
        doc["pins"].setdefault(new["id"], None)
        return None

    _update(ctx, pid, ch)
    return get(ctx, pid)


def slot_update(ctx: Context, pid: str, sid: str, changes: dict) -> dict:
    from . import recipes as RC

    allowed = {"label", "role", "accepts", "required", "why"}
    if set(changes) - allowed:
        raise ValueError(f"a slot's {', '.join(sorted(allowed))} can change, not {', '.join(sorted(set(changes) - allowed))}")

    def ch(doc):
        sl = _own_slots(doc)
        i = next((i for i, s in enumerate(sl) if s["id"] == sid), None)
        if i is None:
            raise NotFound(f"{pid} has no input slot {sid!r}")
        sl[i] = RC.check_slot({**sl[i], **changes}, origin=sl[i].get("origin", "custom"))
        return None

    _update(ctx, pid, ch)
    return get(ctx, pid)


def slot_remove(ctx: Context, pid: str, sid: str) -> dict:
    """Remove an optional slot (and what it pointed at). A required one must be made optional first."""
    def ch(doc):
        sl = _own_slots(doc)
        s = next((s for s in sl if s["id"] == sid), None)
        if s is None:
            raise NotFound(f"{pid} has no input slot {sid!r}")
        if s["required"]:
            raise ValueError(f"{s['label']} is required for this kind of project. Make it optional first, then remove it.")
        doc["slots"] = [x for x in sl if x["id"] != sid]
        doc["pins"].pop(sid, None)
        doc["inputs"].pop(sid, None)
        return None

    _update(ctx, pid, ch)
    return get(ctx, pid)


def record_change(ctx: Context, pid: str, entry: dict) -> None:
    """Add one line to the project's history (the newest 200 are kept): who changed what, when, and, for a change an
    agent made, the person's instruction it acted on. What agents do to a project is always written down."""
    def ch(doc):
        doc["history"] = ((doc.get("history") or []) + [{"at": _now(), **entry}])[-200:]
        return None

    _update(ctx, pid, ch)


def set_evaluation(ctx: Context, pid: str, method: str) -> dict:
    from . import recipes as RC

    if method not in RC.METHODS:
        raise ValueError(f"the evaluation method is one of {', '.join(RC.METHODS)}")

    def ch(doc):
        doc["evaluation"] = {**(doc.get("evaluation") or {}), "method": method}
        return None

    _update(ctx, pid, ch)
    return get(ctx, pid)


def set_trainer_suggestion(ctx: Context, pid: str, trainer: dict | None) -> None:
    def ch(doc):
        doc["trainer_suggestion"] = trainer
        return None

    _update(ctx, pid, ch)


def set_registry(ctx: Context, pid: str, arch: str | None = None, brand: str | None = None, uri: str | None = None,
                 dry_run: bool = False) -> dict:
    """Where this project's versions go in a model registry: <uri>/<arch>/<brand>/<version>."""
    for label, v in (("architecture", arch), ("brand", brand)):
        if v is not None and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", v):
            raise ValueError(f"the {label} is lowercase letters, digits, . _ and -")

    def ch(doc):
        reg = doc.setdefault("registry", {}) or {}
        for k, v in (("arch", arch), ("brand", brand), ("uri", uri)):
            if v is not None:
                reg[k] = v
        doc["registry"] = reg
        return {"project": pid, "registry": reg, "dry_run": dry_run}

    return _update(ctx, pid, ch, dry_run)


# ---- experiments --------------------------------------------------------------------------------


def experiment_add(ctx: Context, pid: str, pipeline: str, training_set: str | None = None, labels: str | None = None,
                   trainer: str | None = None, note: str = "", dry_run: bool = False,
                   inputs_read: list[dict] | None = None) -> dict:
    """Record a trained pipeline version as an experiment. training_set and labels are what it trained on, when
    known: label independence reads them."""
    from .ops import ref

    pm, _ = ctx.registry.resolve("pipeline", pipeline)
    ts = ref(ctx.registry.resolve("dataset", training_set)[0]) if training_set else None
    lb = ref(ctx.registry.resolve("dataset", labels)[0]) if labels else None
    exp = {"id": "exp-" + new_id().lower()[-10:], "pipeline": ref(pm), "training_set": ts, "labels": lb,
           "trainer": trainer or (pm.tags or {}).get("trainer") or None, "backtest": None, "run": None, "job": None,
           "status": "trained", "note": note, "error": None, "created_at": _now(), "created_by": whoami(),
           "updated_at": None, "inputs_read": inputs_read if inputs_read is not None else _read_from(pm),
           "eval_inputs": None, "reported": None}

    def ch(doc):
        if any(e["pipeline"] == exp["pipeline"] for e in doc["experiments"]):
            raise ValueError(f"{exp['pipeline']} is already a candidate in {pid}")
        doc["experiments"].append(exp)
        return exp

    return _update(ctx, pid, ch, dry_run)


def _read_from(pm) -> list[dict]:
    """What a trained version says it read (the trainer contract's train_inputs), in the experiment's shape."""
    out = []
    for i in getattr(pm, "train_inputs", None) or []:
        out.append({k: i.get(k) for k in ("slot", "role", "ref", "mode", "rows", "fingerprint", "as_of",
                                          "changed_since_pin", "read_at")})
    return out


def _exp(doc: dict, eid: str) -> dict:
    e = next((e for e in doc["experiments"] if e["id"] == eid or e["pipeline"] == eid), None)
    if e is None:
        raise NotFound(f"no candidate {eid!r} in {doc['project']}")
    return e


def experiment_record(ctx: Context, pid: str, eid: str, **fields) -> dict:
    allowed = {"backtest", "run", "job", "status", "note", "error", "eval_inputs", "inputs_read", "reported",
               "cost", "train_job"}
    if set(fields) - allowed:
        raise ValueError(f"a candidate records {', '.join(sorted(allowed))}")

    def ch(doc):
        e = _exp(doc, eid)
        e.update(fields)
        e["updated_at"] = _now()
        return dict(e)

    return _update(ctx, pid, ch)


def set_champion(ctx: Context, pid: str, which: str | None, expect: str | None = "", dry_run: bool = False) -> dict:
    """Make an experiment (by id) or a pipeline ref the champion. `expect` is the champion the caller saw: if it
    changed since, nothing is written (pass "" to skip the check). None clears the champion."""
    from .ops import ref

    target = None
    if which:
        doc0, _ = read(ctx, pid)
        hit = next((e for e in doc0["experiments"] if e["id"] == which), None)
        target = hit["pipeline"] if hit else ref(ctx.registry.resolve("pipeline", which)[0])

    def ch(doc):
        if expect != "" and doc.get("champion") != expect:
            raise Conflict(f"the baseline changed to {doc.get('champion') or 'none'} since you looked. Check again.")
        prev = doc.get("champion")
        doc["champion"] = target
        doc["champion_history"] = (doc.get("champion_history") or []) + [{"from": prev, "to": target, "at": _now(),
                                                                           "by": whoami()}]
        exp = next((e for e in doc["experiments"] if e["pipeline"] == target), None)
        return {"project": pid, "champion": target, "previous": prev, "experiment": exp["id"] if exp else None,
                "dry_run": dry_run}

    return _update(ctx, pid, ch, dry_run)


# ---- label independence -------------------------------------------------------------------------


def training_labels(ctx: Context, exp: dict) -> str | None:
    """Where an experiment's training labels came from: a dataset ref, a bare source (human, llm, click,
    heuristic), or None when nothing says."""
    if exp.get("labels"):
        return exp["labels"]
    try:
        pm, _ = ctx.registry.resolve("pipeline", exp["pipeline"])
    except Exception:  # noqa: BLE001
        pm = None
    if pm is not None:
        lab = labels_from_inputs(getattr(pm, "train_inputs", None) or [])
        if lab:
            return lab
        if (pm.tags or {}).get("train_labels"):
            return pm.tags["train_labels"]
    if exp.get("training_set"):
        try:
            tm, _ = ctx.registry.resolve("dataset", exp["training_set"])
        except Exception:  # noqa: BLE001
            return None
        params = tm.provenance.params or {}
        for k in ("labels", "judgments"):
            if isinstance(params.get(k), str):
                return params[k]
        if params.get("label_source"):
            return str(params["label_source"])
        if tm.judgment_source:
            return f"{tm.name}@v{tm.version}"
    return None


def labels_from_inputs(inputs: list) -> str | None:
    """The labels in a version's train_inputs: the judgments item, preferring role "labels". Its exact ref when it
    has one, else its label_source (human, llm, clicks, simulated)."""
    items = [i if isinstance(i, dict) else (i.model_dump() if hasattr(i, "model_dump") else {}) for i in inputs]
    labs = [i for i in items if i.get("kind") == "judgments"]
    lab = next((i for i in labs if i.get("role") == "labels"), labs[0] if labs else None)
    if not lab:
        return None
    return lab.get("ref") or lab.get("label_source") or None


# The trainer contract says clicks and simulated, judgment datasets say click and heuristic: one name each.
_SOURCE = {"click": "clicks", "clicks": "clicks", "heuristic": "simulated", "simulated": "simulated", "llm": "llm",
           "human": "human"}


def label_independence(ctx: Context, train: str | None, holdout_judgments: str | None) -> dict:
    """Do the training labels and the holdout grades come from different labelers? status is independent,
    shared or unknown."""
    if not holdout_judgments:
        return {"status": "unknown", "train": train, "holdout": None,
                "text": "The test set has no judgments, so independence can't be checked."}
    try:
        hm, _ = ctx.registry.resolve("dataset", holdout_judgments)
    except Exception:  # noqa: BLE001
        return {"status": "unknown", "train": train, "holdout": holdout_judgments,
                "text": f"{holdout_judgments} can't be found, so independence can't be checked."}
    href = f"{hm.name}@v{hm.version}"
    hsrc = hm.judgment_source
    out = {"train": train, "holdout": href, "holdout_source": hsrc}
    if not train:
        return {**out, "status": "unknown", "train_source": None,
                "text": "Nothing records where the training labels came from, so independence can't be checked."}
    tm = None
    if "@" in train or ctx.registry.versions("dataset", train.split(":")[0]):
        try:
            tm, _ = ctx.registry.resolve("dataset", train.split(":")[0])
        except Exception:  # noqa: BLE001
            tm = None
    if tm is None:
        tsrc = _SOURCE.get(train.lower())
        if tsrc is None:
            return {**out, "status": "unknown", "train_source": None,
                    "text": f"The training labels ({train}) can't be found, so independence can't be checked."}
        if tsrc == _SOURCE.get(hsrc or "") and tsrc != "human":
            return {**out, "status": "shared", "train_source": tsrc,
                    "text": f"Training labels and test set grades both come from {tsrc} labeling."}
        return {**out, "status": "independent", "train_source": tsrc,
                "text": f"Training labels come from {tsrc} labeling and the test set grades from {hsrc or 'another source'}."}
    tref = f"{tm.name}@v{tm.version}"
    tsrc = tm.judgment_source
    out |= {"train": tref, "train_source": tsrc}
    if tm.name == hm.name:
        return {**out, "status": "shared",
                "text": f"The training labels and the test set grades are the same judgment set ({tm.name})."}
    tj, hj = (tm.judge or {}).get("model"), (hm.judge or {}).get("model")
    if tsrc and tsrc == hsrc and tsrc != "human" and (tsrc == "click" or not tj or not hj or tj == hj):
        who = f"the same {tsrc} judge" + (f" ({tj})" if tj and tsrc != "click" else "")
        return {**out, "status": "shared", "text": f"The training labels and the test set grades were made by {who}."}
    return {**out, "status": "independent",
            "text": f"Training labels come from {tref} ({tsrc or 'unknown source'}), test set grades from {href} "
                    f"({hsrc or 'unknown source'})."}


def verdict(comparison_verdict: str | None, independence: dict) -> str | None:
    if comparison_verdict is None:
        return None
    if independence.get("status") == "shared":
        return OWN_LABELER
    return VERDICTS.get(comparison_verdict, comparison_verdict)


# ---- experiments against the champion ------------------------------------------------------------


EVAL_LABELS = {"holdout": "Sealed test set, graded by judgments",
               "judged-queries": "Judged queries, not sealed",
               "logged-clicks": "Replay of logged searches, graded from clicks",
               "trainer-reported": "Reported by the trainer, not checked by Captain",
               "none": "No evaluation set yet"}
_EVIDENCE = {"human": "HUMAN_GRADED", "llm": "LLM_GRADED", "click": "LOGGED_CLICKS", "heuristic": "SIMULATED"}


def _has_columns(ctx: Context, ref_: str, cols: set[str]) -> bool:
    try:
        m, _ = ctx.registry.resolve("dataset", ref_.split(":")[0])
    except Exception:  # noqa: BLE001
        return False
    have = {f["name"] for f in m.schema_fields}
    return not have or cols <= have  # by-reference data has no schema recorded: let the replay say what's missing


def _simulated(ctx: Context, ref_: str) -> bool:
    from .historic import synthetic

    try:
        return synthetic(ctx.registry.resolve("dataset", ref_.split(":")[0])[0])
    except Exception:  # noqa: BLE001
        return False


def evaluation_plan(ctx: Context, doc: dict) -> dict:
    """How this project's experiments are judged, given the slots it has filled. Never pretends: a method that can't
    run says what it is missing, and with nothing to judge on the answer is "no evaluation set yet"."""
    from .historic import REQUIRED

    chosen = (doc.get("evaluation") or {}).get("method") or "auto"
    hold, queries, labels = pinned(doc, "holdout"), pinned(doc, "queries"), pinned(doc, "labels")
    logs = pinned(doc, "events")
    live_events = [s["id"] for s in by_role(doc, "events") if (doc["inputs"].get(s["id"]) or {}).get("from") == "connection"
                   and not doc["pins"].get(s["id"])]
    reported = any((e.get("reported") or _pipeline_metrics(ctx, e["pipeline"])) for e in doc["experiments"])

    def judged():
        if labels and (hold or queries):
            src = None
            try:
                src = ctx.registry.resolve("dataset", labels)[0].judgment_source
            except Exception:  # noqa: BLE001
                pass
            m = "holdout" if hold else "judged-queries"
            text = (f"Scores both rankers on {hold or queries} with {labels}." if hold else
                    f"Scores both rankers on {queries} with {labels}. These queries aren't sealed, so the model may "
                    "have trained on them: seal a test set for a fair score.")
            return {"method": m, "ready": True, "evidence": _EVIDENCE.get(src or ""), "text": text,
                    "uses": {"queries": hold or queries, "judgments": labels}, "missing": []}
        miss = [s["id"] for s in by_role(doc, "labels")][:1] if not labels else []
        if not (hold or queries):
            miss += [s["id"] for s in (by_role(doc, "holdout") or by_role(doc, "queries"))][:1]
        return {"method": "holdout", "ready": False, "evidence": None, "missing": miss,
                "text": "Scoring on judgments needs a judgments input and a queries or sealed test set input."}

    def clicks():
        if logs and _has_columns(ctx, logs, REQUIRED):
            sim = _simulated(ctx, logs)
            return {"method": "logged-clicks", "ready": True, "evidence": "SIMULATED" if sim else "LOGGED_CLICKS",
                    "simulated": sim, "missing": [], "uses": {"logs": logs},
                    "text": f"Replays the searches logged in {logs}, graded from clicks. Grades only exist for products "
                            "the old ranker showed. With an as_of cutoff on training, only later searches are replayed."
                            + (" These events are simulated: no real shopper made them, so the result is simulated too."
                               if sim else "")}
        if live_events:
            return {"method": "logged-clicks", "ready": False, "evidence": None, "missing": live_events,
                    "text": "The events input is a live query. A replay reads rows Captain holds: take a snapshot of it."}
        miss = [s["id"] for s in by_role(doc, "events")][:1]
        return {"method": "logged-clicks", "ready": False, "evidence": None, "missing": miss,
                "text": "A replay needs an events input with query, product_id, position and clicked columns."}

    def trainer():
        return {"method": "trainer-reported", "ready": reported, "evidence": "TRAINER_REPORTED" if reported else None,
                "missing": [], "text": "Compares the metrics each trainer reported on its own held-out data. Captain "
                                       "didn't compute them and can't check them, and they carry no interval."
                if reported else "No candidate reported metrics yet (result.yaml metrics)."}

    none = {"method": "none", "ready": False, "evidence": None, "missing": [],
            "text": "No evaluation set yet: add judgments with queries or a sealed test set, or an events input with "
                    "clicks, or have your trainer report metrics."}
    if chosen == "none":
        out = none
    elif chosen in ("holdout", "judged-queries"):
        out = judged()
    elif chosen == "logged-clicks":
        out = clicks()
    elif chosen == "trainer-reported":
        out = trainer()
    else:
        out = next((x for x in (judged(), clicks(), trainer()) if x["ready"]), none)
    return {"chosen": chosen, "label": EVAL_LABELS[out["method"]], "uses": {}, **out}


def _pipeline_metrics(ctx: Context, ref_: str) -> dict:
    try:
        return dict(getattr(ctx.registry.resolve("pipeline", ref_)[0], "train_metrics", None) or {})
    except Exception:  # noqa: BLE001
        return {}


def _as_of(ctx: Context, exp: dict, role: str) -> str | None:
    for i in exp.get("inputs_read") or []:
        if i.get("role") == role and i.get("as_of"):
            return str(i["as_of"])
    try:
        pm, _ = ctx.registry.resolve("pipeline", exp["pipeline"])
    except Exception:  # noqa: BLE001
        return None
    return next((str(i["as_of"]) for i in getattr(pm, "train_inputs", None) or []
                 if i.get("as_of") and i.get("kind") in ("events", "logs")), None)


def _after(ctx: Context, logs: str, as_of: str, into: str) -> str | None:
    """The logged rows after the cutoff, as their own dataset, so the replay never grades on what training saw.
    None when the logs have no timestamp to cut on."""
    from .data.datasets import snapshot
    from .model.entities import Provenance
    from .ops import ref, rows_of
    import pyarrow as pa

    m, rows, _ = rows_of(ctx, logs)
    col = next((c for c in ("ts", "timestamp", "event_time", "time") if rows and c in rows[0]), None)
    if col is None:
        return None
    keep = [r for r in rows if str(r.get(col) or "") > as_of]
    if not keep:
        raise ValueError(f"{logs} has no searches after {as_of}, so there is nothing later to replay")
    # A cut of synthetic logs is still synthetic: without the tag the replay would read as logged clicks.
    out = snapshot(ctx, into, pa.Table.from_pylist(keep), kind=m.dataset_kind,
                   tags={k: v for k, v in (m.tags or {}).items() if k in ("synthetic", "label_source")},
                   provenance=Provenance(source="generated", params={"from": ref(m), "after": as_of, "column": col}),
                   description=f"{len(keep)} of {len(rows)} logged rows after {as_of}, from {ref(m)}")
    return ref(out)


def _used(ctx: Context, doc: dict, refs: dict) -> list[dict]:
    """What an evaluation read: each dataset with its rows and fingerprint."""
    out = []
    for what, r in refs.items():
        if not r:
            continue
        try:
            m, _ = ctx.registry.resolve("dataset", r.split(":")[0])
        except Exception:  # noqa: BLE001
            continue
        sl = next((s for s in slots_of(doc) if (doc["pins"].get(s["id"]) or "").split(":")[0] == r.split(":")[0]), None)
        out.append({"slot": sl["id"] if sl else None, "role": sl["role"] if sl else what, "ref": r, "mode": "snapshot",
                    "rows": m.row_count or None, "fingerprint": dataset_fingerprint(m),
                    "as_of": (m.provenance.params or {}).get("after"), "changed_since_pin": False, "read_at": _now()})
    return out


def _reported(ctx: Context, exp: dict, champ: str) -> dict:
    mine, theirs = _pipeline_metrics(ctx, exp["pipeline"]), _pipeline_metrics(ctx, champ)
    common = [k for k in mine if k in theirs and isinstance(mine[k], (int, float)) and isinstance(theirs[k], (int, float))]
    metric = next((k for k in ("ndcg@10", "ndcg_at_10", "map@10", "recall@10", "hit_rate@10") if k in common),
                  common[0] if common else None)
    base = {"method": "trainer-reported", "evidence": "TRAINER_REPORTED", "against": champ,
            "text": "Reported by the trainers on their own held-out data, not checked by Captain. No interval, so this "
                    "can't say which is better, only which number is higher."}
    if metric is None:
        return {**base, "metric": None, "value": None, "champion_value": None, "delta": None,
                "verdict": "no shared metric reported",
                "text": "The two trainers reported no metric in common, so there is nothing to compare."}
    d = round(mine[metric] - theirs[metric], 6)
    return {**base, "metric": metric, "value": mine[metric], "champion_value": theirs[metric], "delta": d,
            "verdict": "reports a higher number" if d > 0 else "reports a lower number" if d < 0 else "reports the same number"}


def experiment_backtest(ctx: Context, pid: str, eid: str, compute: str | None = None, progress=None, live=None,
                        live_id: str | None = None, dry_run: bool = False) -> dict:
    """Judge an experiment against the champion by the project's evaluation method, and record what it read and the
    run (or the submitted job) on the experiment."""
    from . import ops

    doc = get(ctx, pid)
    exp = _exp(doc, eid)
    champ = doc.get("champion")
    if not champ:
        raise ValueError(f"{pid} has no baseline yet. Make one first: the first candidate or today's ranker.")
    if champ == exp["pipeline"]:
        raise ValueError(f"{exp['pipeline']} is the baseline. Pick another candidate to compare with it.")
    ep = evaluation_plan(ctx, doc)
    if not ep["ready"]:
        raise ValueError(ep["text"])
    base = {"project": pid, "experiment": exp["id"], "method": ep["method"], "evaluation": ep}
    if ep["method"] == "trainer-reported":
        rep = _reported(ctx, exp, champ)
        if not dry_run:
            experiment_record(ctx, pid, exp["id"], reported=rep, status="evaluated", eval_inputs=[], error=None)
        return {**base, "arms": [champ, exp["pipeline"]], "reported": rep, "label_independence": None,
                "backtest": None, "queries": None, "judgments": None, "dry_run": dry_run}
    name = f"{pid}/{exp['id']}"
    train = training_labels(ctx, exp)
    if ep["method"] == "logged-clicks":
        logs = ep["uses"]["logs"]
        cut = _as_of(ctx, exp, "events")
        note = None
        if dry_run:
            ind = {"status": "unknown", "train": train, "holdout": None,
                   "text": "Grades come from the logged clicks when the replay is built."}
            return {**base, "backtest": name, "arms": [champ, exp["pipeline"]], "queries": None, "judgments": None,
                    "logs": logs, "as_of": cut, "label_independence": ind, "dry_run": True}
        if cut:
            later = _after(ctx, logs, cut, f"{name}/logs-after")
            if later:
                logs = later
            else:
                note = f"The logs have no timestamp column, so the replay can't skip searches before {cut}."
        # The same parts a historic plan builds (queries, click grades, shoppers), so the plan can carry its tags
        # when it is published once.
        from . import historic as H

        lpin = ops.ref(ctx.registry.resolve("dataset", logs)[0])
        q = ops.ref(H.queries_from_logs(ctx, lpin, f"{name}/queries")) + ":holdout"
        j = ops.ref(H.judgments_from_logs(ctx, lpin, f"{name}/judgments"))
        pop = ops.ref(ops.population_create(ctx, f"{name}/shoppers", "cascade-v1", 4000, 1, from_logs=lpin))
        ind = label_independence(ctx, train, j)
        bt = ops.backtest_plan(ctx, name, [champ, exp["pipeline"]], q, j, pop, primary="ndcg@10_tw", dry_run=True,
                               hypothesis=f"{exp['pipeline']} ranks logged searches better than the baseline {champ}")
        bt = bt.model_copy(update={"mode": "historic", "logs": lpin, "tags": {
            **(bt.tags or {}), "project": pid, "experiment": exp["id"], "train_labels": ind.get("train") or "",
            "label_independence": ind["status"], "evaluation": ep["method"], **({"note": note} if note else {})}})
        bt = ctx.registry.publish(type(bt).model_validate(bt.model_dump(mode="json")))
        used = _used(ctx, doc, {"logs": logs})
    else:
        q, j = ep["uses"]["queries"], ep["uses"]["judgments"]
        ind = label_independence(ctx, train, j)
        bt = ops.backtest_plan(ctx, name, [champ, exp["pipeline"]], q, j, None,
                               hypothesis=f"{exp['pipeline']} ranks {q} better than the baseline {champ}", dry_run=True)
        bt = bt.model_copy(update={"tags": {**(bt.tags or {}), "project": pid, "experiment": exp["id"],
                                            "train_labels": ind.get("train") or "", "label_independence": ind["status"],
                                            "evaluation": ep["method"]}})
        plan = {**base, "backtest": name, "arms": bt.arms, "queries": bt.queries, "judgments": bt.judgments,
                "label_independence": ind}
        if dry_run:
            return {**plan, "dry_run": True}
        bt = ctx.registry.publish(type(bt).model_validate(bt.model_dump(mode="json")))
        used = _used(ctx, doc, {"queries": q, "judgments": j})
    plan = {**base, "backtest": ops.ref(bt), "arms": bt.arms, "queries": bt.queries, "judgments": bt.judgments,
            "label_independence": ind, "eval_inputs": used}
    experiment_record(ctx, pid, exp["id"], backtest=ops.ref(bt), status="backtesting", run=None, job=None, error=None,
                      eval_inputs=used)
    note_read(ctx, pid, used)
    try:
        r = ops.backtest_launch(ctx, bt.name, compute, progress=progress, live=live, live_id=live_id)
    except Exception as e:
        experiment_record(ctx, pid, exp["id"], status="failed", error=str(e)[:500])
        raise
    if r.get("run"):
        experiment_record(ctx, pid, exp["id"], run=r["run"], status="evaluated")
    else:
        experiment_record(ctx, pid, exp["id"], job=r.get("job"), status="backtesting")
    return {**plan, **{k: r.get(k) for k in ("run", "job", "compute", "live", "detail")}}


def _evaluation(ctx: Context, exp: dict, champion: str | None, index=None) -> dict | None:
    """The experiment's row numbers from its recorded run. Loads by exact version: no listing."""
    run_ref = exp.get("run")
    if not run_ref and exp.get("backtest") and index is not None:
        bname = exp["backtest"].split("@v")[0]
        vs = index.runs_of(bname)
        if vs:
            run_ref = f"{bname}@v{vs[-1]}"
    if not run_ref:
        return None
    name, _, v = run_ref.partition("@v")
    try:
        run = ctx.registry.load("backtest-run", name, int(v))
        bname, _, bv = run.backtest.partition("@v")
        bt = ctx.registry.load("backtest", bname, int(bv) if bv else None)
    except Exception as e:  # noqa: BLE001
        return {"run": run_ref, "error": f"the run can't be read: {e}"}
    by = {a.arm: a for a in run.arms}
    me, base = by.get(exp["pipeline"]), by.get(bt.arms[0])
    primary = bt.preregistration.primary_metric
    comp = next((c for c in run.comparisons if c.get("b") == exp["pipeline"] and c.get("metric") == primary), None)
    ind = label_independence(ctx, (bt.tags or {}).get("train_labels") or training_labels(ctx, exp), bt.judgments)
    rec = next((k for k in (me.offline if me else {}) if k.startswith("r@") or k.startswith("recall")), None)
    click_grades = bt.mode == "historic" or (ind.get("holdout_source") == "click")
    thin = bool(click_grades and me and me.judged_at_10 is not None and me.judged_at_10 < THIN_COVERAGE)
    coverage_note = (f"Only {me.judged_at_10:.0%} of its top 10 results have click grades, because clicks only grade "
                     "products the old ranker showed. The rest count as zero, so this score can be low because the "
                     "products are new to the log, not because they are worse. Judgments on a test set would settle it."
                     if thin else None)
    return {
        "run": f"{run.name}@v{run.version}", "against": bt.arms[0], "against_is_champion": bt.arms[0] == champion,
        "metric": primary,
        "value": (me.offline.get(primary) if me else None),
        "champion_value": (base.offline.get(primary) if base else None),
        "delta": comp.get("delta") if comp else None,
        "ci_low": comp.get("ci_low") if comp else None, "ci_high": comp.get("ci_high") if comp else None,
        "p": comp.get("p") if comp else None,
        "comparison": comp.get("verdict") if comp else None,
        "recall": {"metric": rec, "value": me.offline.get(rec), "champion_value": base.offline.get(rec) if base else None}
        if rec and me else None,
        "judged_at_10": me.judged_at_10 if me else None,
        "evidence": me.evidence.value if me else None,
        "label_independence": ind,
        "verdict": TOO_FEW_JUDGED if thin and comp else verdict(comp.get("verdict") if comp else None, ind),
        "coverage_note": coverage_note,
        "online": NOT_ONLINE,
        "click_grades": click_grades,
    }


def _feature_timing(ctx: Context, exp: dict) -> str | None:
    """One line on how the candidate's training features were timed, from its training set's provenance."""
    if not exp.get("training_set"):
        return None
    try:
        name, _, v = exp["training_set"].partition("@v")
        tm = ctx.registry.load("dataset", name, int(v))  # an exact version: no listing
    except Exception:  # noqa: BLE001
        return None
    return ((tm.provenance.params or {}).get("feature_timing") or {}).get("text")


def experiments(ctx: Context, pid: str) -> dict:
    from . import lineage_index as LI

    doc = get(ctx, pid)
    index = LI.Index(ctx.store)
    rows = []
    for e in doc["experiments"]:
        ev = e.get("reported") or _evaluation(ctx, e, doc.get("champion"), index)
        rows.append({**e, "is_champion": e["pipeline"] == doc.get("champion"), "evaluation": ev,
                     "feature_timing": _feature_timing(ctx, e)})
    return {"project": pid, "champion": doc.get("champion"), "pins": doc["pins"], "experiments": rows,
            "evaluation": evaluation_plan(ctx, doc)}


# ---- stages -------------------------------------------------------------------------------------


def stages(ctx: Context, pid: str) -> dict:
    """Where the project stands, from the pin doc and the lineage index only (one listing: the edge prefix)."""
    from . import lineage_index as LI

    doc = get(ctx, pid)
    idx = LI.Index(ctx.store)
    pins = doc["pins"]
    pinned_names = {}
    for s, r in pins.items():
        if r:
            n, _, v = r.split(":")[0].partition("@v")
            pinned_names.setdefault(n, []).append((s, int(v)))
    inputs = []
    for sl in slots_of(doc):
        slot = sl["id"]
        r = pins.get(slot)
        rec = doc["inputs"].get(slot) or {}
        live = rec.get("mode") == "live"
        row = {"slot": slot, "label": sl["label"], "role": sl["role"], "group": GROUP.get(sl["role"], "data"),
               "accepts": list(sl["accepts"]), "required": sl["required"], "why": sl["why"], "origin": sl.get("origin"),
               "pinned": r, "mode": rec.get("mode"), "from": rec.get("from"), "rows": rec.get("rows"),
               "fingerprint": rec.get("fingerprint"), "as_of": rec.get("as_of"), "last_read": rec.get("last_read"),
               "simulated": bool(rec.get("simulated")),
               "status": "live" if live else "pinned" if r else ("missing" if sl["required"] else "empty"),
               "newest": None, "info": [], "next": None}
        if not r:
            if not live:
                row["next"] = {"action": "pin", "slot": slot}
            inputs.append(row)
            continue
        name, _, v = r.split(":")[0].partition("@v")
        v = int(v)
        newest = idx.newest("dataset", name)
        row["newest"] = f"{name}@v{newest}" if newest else r.split(":")[0]
        if newest and newest > v:
            row["info"].append({"code": "newer-version", "ref": f"{name}@v{newest}",
                                "text": f"Newer version available: {name}@v{newest}."})
            if live:  # a live dataset input follows the newest version by itself
                row["info"][-1]["text"] += " Runs read it."
            else:
                row["next"] = {"action": "pin", "slot": slot,
                               "ref": f"{name}@v{newest}" + (":holdout" if sl["role"] == "holdout" else "")}
        for rec in idx.upstream(f"dataset:{name}@v{v}"):
            m = re.match(r"^dataset:(.+)@v(\d+)$", rec["from"] or "")
            if not m or m.group(1) == name:
                continue
            for other, ov in pinned_names.get(m.group(1), []):
                if ov != int(m.group(2)):
                    row["info"].append({"code": "made-from-other-version", "ref": f"{m.group(1)}@v{m.group(2)}",
                                        "text": f"Made from {m.group(1)}@v{m.group(2)}, but the {other} pin is "
                                                f"{m.group(1)}@v{ov}."})
        inputs.append(row)
    exps = doc["experiments"]
    champ = doc.get("champion")
    pending = [e for e in exps if e.get("status") in ("trained", "failed") and e["pipeline"] != champ]
    # A project that starts from nothing: keyword search over its catalog is a baseline that needs no training.
    has = {role: bool(pinned(doc, role)) for role in ("catalog", "events", "labels")}
    can_baseline = has["catalog"] and not champ
    start = {"baseline": {"done": bool(champ), "can": can_baseline, "ref": champ},
             "starter": {"done": any(e.get("trainer") == "lightgbm-ltr" for e in exps),
                         "can": has["catalog"] and (has["events"] or has["labels"]),
                         "needs": [r for r in ("catalog",) if not has[r]] + ([] if has["events"] or has["labels"] else ["events"])}}
    if not exps and can_baseline:
        nxt = {"action": "baseline-start"}
    elif not exps:
        nxt = {"action": "new-experiment"}
    elif not champ:
        nxt = {"action": "make-champion", "experiment": exps[0]["id"]}
    elif pending:
        nxt = {"action": "backtest", "experiment": pending[0]["id"]}
    else:
        nxt = {"action": "new-experiment"}
    by_status = {}
    for e in exps:
        by_status[e.get("status", "trained")] = by_status.get(e.get("status", "trained"), 0) + 1
    champ_exp = next((e for e in exps if e["pipeline"] == champ), None)
    newest_champ = None
    if champ:
        cn, _, cv = champ.partition("@v")
        nv = idx.newest("pipeline", cn)
        if nv and nv > int(cv):
            newest_champ = f"{cn}@v{nv}"
    reg = doc.get("registry") or {}
    notes = [] if idx.complete else [{"code": "index-incomplete",
                                      "text": "Lineage for older versions isn't indexed yet, so newer versions may be "
                                              "missing here. Run `cst lineage reindex` once."}]
    ev = evaluation_plan(ctx, doc)
    missing = [r["slot"] for r in inputs if r["status"] == "missing"]
    if missing:
        notes.append({"code": "inputs-missing", "slots": missing,
                      "text": "Required inputs aren't set yet: " + ", ".join(r["label"] for r in inputs if r["status"] == "missing") + "."})
    first_missing = next((r for r in inputs if r["status"] == "missing"), None)
    if first_missing:
        top = {"action": "set-input", "slot": first_missing["slot"], "text": f"Set the {first_missing['label']} input."}
    elif exps and not ev["ready"]:
        top = {"action": "add-evaluation", "text": ev["text"]}
    else:
        words = {"baseline-start": "Set a baseline to compare against.",
                 "new-experiment": "Train a starter ranker." if not exps and start["starter"]["can"] else "Train a new candidate.",
                 "make-champion": "Make a candidate the baseline.",
                 "backtest": "Compare the newest candidate with the baseline."}
        top = {**nxt, "text": words.get(nxt["action"], "")}
    return {
        "project": pid, "kind": doc.get("kind"), "recipe": doc.get("recipe"), "index_complete": idx.complete,
        "next": top, "notes": notes, "inputs": inputs, "evaluation": ev, "start": start,
        "experiments": {"count": len(exps), "by_status": by_status, "next": nxt},
        "champion": {"pinned": champ, "experiment": champ_exp["id"] if champ_exp else None,
                     "info": [{"code": "newer-version", "ref": newest_champ,
                               "text": f"Newer version available: {newest_champ}."}] if newest_champ else []},
        "ship": {"registry": {k: reg.get(k) for k in ("arch", "brand", "uri")},
                 "ready": bool(champ and reg.get("arch") and reg.get("brand")), "online": NOT_ONLINE},
    }
