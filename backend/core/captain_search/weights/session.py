"""A weights session: every read, run and change to a network, with an exact record of what was allowed and used.

A session is opened on one version of a model, its base. The session never changes the base. Every edit is a
patch (tensor, indices, old numbers, new numbers), so the numbers now are "the base, then these patches in this
order", undo and redo put back exact numbers, and a saved version is the base plus patches.

The record is written here, by the operations themselves. Nothing a caller passes can set it:
  - who is working, and what they were allowed: raw weights in context, a sandbox, how many forward passes
  - what they used: how many raw numbers were shown, each forward pass and what it held, whether gradients were
    computed, how many evaluations the tool ran
  - every operation with its arguments, every edit with its patch, and the share of numbers that now differ
  - whether a score was shown to the caller, and which operation showed the first one
  - `weights_alone`, worked out from the above each time it is asked for

A session opened blind (`feedback` off) measures and stores every score and shows none until it is saved or
closed. Only such a session can make edits and still read "from the weights alone".

In every sentence a person reads, layers and neurons are counted from 1. In every field they are counted from 0.

    weights/<model>/sessions/<id>/session.json      the record, replaced on every operation
    weights/<model>/sessions/<id>/patches/<n>.json  one edit's patch, written once

Each operation reads the record, does its work, writes the record back only if nobody else wrote in between (and
starts over if someone did), then tells the change feed: `weights:<model>` and `weights-session:<id>`.
"""
from __future__ import annotations

import functools
import json
import os
import secrets

import numpy as np

from .. import changes as CH
from ..storage.backend import AlreadyExists, Conflict, NotFound
from . import files as F
from . import net as N
from . import store as WS
from .net import WeightsError

LIST_CAP = 200         # the most changes or touched weights listed in one event
GRAD_CAP = 4096        # a gradient table larger than this is left out, and its largest entries listed instead
EVENTS_SHOWN = 100     # how many events a session answer carries (the record keeps all of them)
DEFAULTS = {"you": {"raw_weights": True, "sandbox": False, "forward_passes": 50},
            "agent": {"raw_weights": True, "sandbox": False, "forward_passes": 0}}
SCORE_KEYS = ("cost", "cost_before", "cost_after", "predicted", "result", "score_sentence")
BLIND = "This session is blind: the scores are measured and kept, and shown when it is saved or closed."
PASS_INPUTS = 32


class Refused(WeightsError):
    """An operation the session's allowances do not cover. It is written into the record, then raised."""


def _by_kind(fn):
    """A session on a model that is not a fully connected network (real.py) answers this operation in its own way,
    or says in a sentence that it cannot yet."""
    @functools.wraps(fn)
    def run(ctx, sid, *args, **kw):
        from . import real as R

        other = R.handler(ctx, sid, fn.__name__)
        return other(ctx, sid, *args, **kw) if other else fn(ctx, sid, *args, **kw)
    return run


# ---- loading and saving ---------------------------------------------------------------------------------------------

_BASES: dict[tuple, tuple[dict, dict]] = {}    # versions are write-once, so a loaded one can be kept
_PATCHES: dict[tuple, dict] = {}
_WHERE: dict[tuple, str] = {}                  # session id -> model


def _skey(model: str, sid: str, rest: str = "session.json") -> str:
    return f"{WS.ROOT}/{model}/sessions/{sid}/{rest}"


def _base(ctx, model: str, version: int):
    k = (ctx.store.uri, model, version)
    if k not in _BASES:
        if len(_BASES) > 16:
            _BASES.clear()
        _BASES[k] = WS.load(ctx, model, version)
    m, t = _BASES[k]
    return m, t


def _patch(ctx, model: str, sid: str, key: str) -> dict:
    k = (ctx.store.uri, model, sid, key)
    if k not in _PATCHES:
        if len(_PATCHES) > 512:
            _PATCHES.clear()
        _PATCHES[k] = json.loads(ctx.store.get(_skey(model, sid, key)).decode())
    return _PATCHES[k]


def find(ctx, sid: str) -> str:
    """The model a session is on."""
    k = (ctx.store.uri, sid)
    if k in _WHERE:
        return _WHERE[k]
    if not isinstance(sid, str) or not sid.startswith("ws-") or "/" in sid:
        raise NotFound(f"There is no weights session {sid!r}. Open one with `cst weights open <model>`.")
    for key in ctx.store.list(f"{WS.ROOT}/"):
        parts = key.replace("\\", "/").split("/")
        if len(parts) == 5 and parts[2] == "sessions" and parts[3] == sid and parts[4] == "session.json":
            _WHERE[k] = parts[1]
            return parts[1]
    raise NotFound(f"There is no weights session {sid!r}. Open one with `cst weights open <model>`.")


class State:
    """One operation's view of a session: the record, the base's numbers and the numbers now."""

    def __init__(self, ctx, doc: dict, tag: str | None):
        self.ctx, self.doc, self.tag = ctx, doc, tag
        self.model, self.sid = doc["model"], doc["id"]
        self.manifest, base = _base(ctx, self.model, doc["version"])
        self.base = base
        self.cur = {k: t.copy() for k, t in base.items()}
        self.new_patches: dict[str, dict] = {}
        for kind, n in doc["actions"]:
            self._put(self.patch(n), "old" if kind == "undo" else "new")

    # the numbers
    def patch(self, n: int) -> dict:
        key = self.doc["edits"][n - 1]["patch"]
        return self.new_patches.get(key) or _patch(self.ctx, self.model, self.sid, key)

    def _put(self, patch: dict, which: str) -> None:
        for part in patch["parts"]:
            flat = self.cur[part["tensor"]].reshape(-1)
            flat[np.asarray(part["indices"], dtype=np.int64)] = np.asarray(part[which], dtype=np.float32)

    def net(self, base: bool = False) -> N.Network:
        return F.network_from(self.manifest, self.base if base else self.cur)

    def layer(self, k) -> dict:
        try:
            k = int(k)
        except (TypeError, ValueError):
            raise WeightsError(f"A layer is a whole number. {k!r} is not one.") from None
        layers = self.manifest["layers"]
        if not 0 <= k < len(layers):
            raise WeightsError(f"This network has {len(layers)} layers. The layer asked for is not one of them.")
        return layers[k]

    def where(self, tensor: str) -> tuple[int, str]:
        """(layer number, 'weight' or 'bias') for a tensor's name."""
        for k, ly in enumerate(self.manifest["layers"]):
            for kind in ("weight", "bias"):
                if ly[kind] == tensor:
                    return k, kind
        names = ", ".join(t for ly in self.manifest["layers"] for t in (ly["weight"], ly["bias"]))
        raise WeightsError(f"This network has no tensor named {tensor!r}. Its tensors are: {names}.")

    # the record
    @property
    def who(self) -> str:
        # An agent that Captain started on a person's session says so through its environment (program.py).
        if os.environ.get("CST_WEIGHTS_BY") == "agent":
            return "agent"
        return "agent" if self.doc["who"]["kind"] == "agent" else "you"

    @property
    def program(self) -> dict | None:
        """The agent run Captain started on this session and has not ended, if there is one."""
        p = self.doc.get("program")
        return p if p and p.get("active") else None

    def changed(self) -> int:
        return int(sum(int((self.cur[k] != self.base[k]).sum()) for k in self.base))

    def total(self) -> int:
        return int(sum(t.size for t in self.base.values()))

    def event(self, kind: str, sentence: str, by: str | None = None, score_sentence: str = "", **fields) -> dict:
        """Add an event to the record. `score_sentence` and the SCORE_KEYS fields are scores: `show` leaves them
        out for a blind session until it is saved or closed."""
        ev = {"id": f"e{len(self.doc['events']) + 1}", "seq": len(self.doc["events"]) + 1, "at": WS.now(),
              "by": _by(by, self.who), "kind": kind, "touched": {"neurons": [], "weights": []}, **fields}
        ev["sentence"] = sentence
        if score_sentence:
            ev["score_sentence"] = score_sentence
        self.doc["events"].append(ev)
        return ev

    @property
    def scores_visible(self) -> bool:
        return bool(self.doc["allowed"].get("feedback", True) or self.doc.get("revealed"))

    def show(self, ev: dict) -> dict:
        """An event as a caller sees it: with its scores, or without them while the session is blind."""
        out = {k: v for k, v in ev.items() if k != "score_sentence"}
        if self.scores_visible:
            if ev.get("score_sentence"):
                out["sentence"] = ev["sentence"] + " " + ev["score_sentence"]
            return out
        hidden = [k for k in SCORE_KEYS if k in out]
        for k in hidden:
            out.pop(k)
        if hidden:
            out["scores"] = "hidden"
        return out

    def saw_score(self, ev: dict, op: str) -> None:
        """A score was shown to the caller. The first time is kept: it is what ends "from the weights alone"."""
        if self.doc["used"].get("score_seen") is None:
            self.doc["used"]["score_seen"] = {"event": ev["id"], "seq": ev["seq"], "op": op}

    def open_for_changes(self) -> None:
        if self.doc.get("closed"):
            raise WeightsError("This session is closed. Open a new one to go on.")


def _by(by, default: str) -> str:
    if by in (None, ""):
        return default
    if by in ("agent", "you", "captain"):
        return by
    if by == "person":
        return "you"
    raise WeightsError("`by` is \"agent\" or \"you\".")


def _load(ctx, sid: str) -> State:
    model = find(ctx, sid)
    data, tag = ctx.store.get_with_tag(_skey(model, sid))
    return State(ctx, json.loads(data.decode()), tag)


def _save(st: State) -> None:
    for key, patch in st.new_patches.items():
        try:
            st.ctx.store.put_new(_skey(st.model, st.sid, key), json.dumps(patch).encode())
        except AlreadyExists:
            pass
        _PATCHES[(st.ctx.store.uri, st.model, st.sid, key)] = patch
    st.tag = st.ctx.store.put_if(_skey(st.model, st.sid), json.dumps(st.doc).encode(), st.tag)
    st.new_patches = {}


def _tell(st: State) -> None:
    CH.bump("weights", f"weights:{st.model}", f"weights-session:{st.sid}")


def _looks_key(sid: str) -> str:
    return f"weights-looks/{sid}.json"    # outside weights/, so counting a look tells no screen to read again


def _looks(ctx, sid: str) -> dict:
    try:
        return json.loads(ctx.store.get(_looks_key(sid)).decode())
    except (NotFound, ValueError):
        return {"count": 0, "numbers": 0, "last": None}


def _note_look(st: "State") -> None:
    """A screen looked at this session's weights. Counted beside the record, so no look is traceless."""
    for _ in range(4):
        try:
            data, tag = st.ctx.store.get_with_tag(_looks_key(st.sid))
            d = json.loads(data.decode())
        except (NotFound, ValueError):
            d, tag = {"count": 0, "numbers": 0, "last": None}, None
        d = {"count": d["count"] + 1, "numbers": d["numbers"] + int(getattr(st, "looked", 0)), "last": WS.now()}
        try:
            st.ctx.store.put_if(_looks_key(st.sid), json.dumps(d).encode(), tag)
            return
        except Conflict:
            continue


def _do(ctx, sid: str, fn, watch=False):
    """Run one operation on a session and write its record.

    `watch=True` is a screen showing a session's weights to a person looking on: the look is not an event and is
    not held to the session's allowances, and it is counted beside the record ("looked at by the screen").
    `watch="record"` reads the record itself and counts nothing. Nothing that runs the network takes `watch`."""
    for _ in range(8):
        st = _load(ctx, sid)
        st.watch = bool(watch)
        st.looked = 0
        try:
            out = fn(st)
        except Refused as e:
            if not watch:
                st.event("refused", f"Refused: {e}")
                try:
                    _save(st)
                    _tell(st)
                except Conflict:
                    pass
            raise
        if watch:
            if watch is True:
                _note_look(st)
            return out
        try:
            _save(st)
        except Conflict:
            continue    # another process wrote the record in between: do the operation again on what is there now
        _tell(st)
        return out
    raise WeightsError("This session is being written by something else right now. Try again.")


# ---- what a session says about itself -----------------------------------------------------------------------------------

def ledger(st: State) -> dict:
    """What was allowed and what was used. `weights_alone` and `label` are worked out here, never stored."""
    a, u = st.doc["allowed"], st.doc["used"]
    changed, total = st.changed(), st.total()
    feedback = bool(a.get("feedback", True))
    seen = u.get("score_seen")
    # A score shown to the caller is feedback from running the network. In a session with feedback, the first one
    # shown ends "from the weights alone". A blind session shows none until it is saved or closed, and loses the
    # label only if it goes on editing after that.
    edited_after = bool(seen) and any(e["kind"] in ("edit", "undo", "redo") and e["seq"] > seen["seq"] for e in st.doc["events"])
    scored = bool(seen) and (feedback or edited_after)
    alone = (not a["sandbox"]) and u["forward_used"] == 0 and not scored
    if u["forward_used"]:
        label = f"with {u['forward_used']} of {a['forward_passes']} forward passes"
    elif a["sandbox"]:
        label = "from the weights, with code, network not run"
    elif scored:
        label = "from the weights, with scores shown along the way"
    else:
        label = "from the weights alone"
    if u["training_steps"]:
        label += f", and {u['training_steps']} training step{'s' if u['training_steps'] != 1 else ''}"
    return {"raw_weights": a["raw_weights"], "weights_read": u["weights_read"], "sandbox": a["sandbox"],
            "sandbox_used": u["sandbox_used"], "forward_allowed": a["forward_passes"], "forward_used": u["forward_used"],
            "pass_inputs": a["pass_inputs"], "gradients_computed": u["gradients_computed"], "evaluations": u["evaluations"],
            "training_steps": u["training_steps"], "changed": changed, "total": total,
            "share": round(changed / total, 6) if total else 0.0, "feedback": feedback, "scores_shown": bool(seen),
            "first_score": {"event": seen["event"], "op": seen["op"]} if seen else None,
            "screen_looks": _looks(st.ctx, st.sid)["count"], "screen_numbers": _looks(st.ctx, st.sid)["numbers"],
            "weights_alone": alone, "label": label}


def _edits_out(st: State) -> list[dict]:
    return [{"id": e["event"], "n": e["n"], "op": e["op"], "tensors": e["tensors"], "why": e.get("why") or "",
             "state": e["state"], "changed": e["changed"], "by": e["by"]} for e in st.doc["edits"]]


def _session_out(st: State, events: int = EVENTS_SHOWN) -> dict:
    d = st.doc
    return {"id": d["id"], "model": d["model"], "version": d["version"], "base": WS.ref_of(d["model"], d["version"]),
            "who": d["who"], "opened_at": d["opened_at"], "ledger": ledger(st), "sets": _sets_out(st),
            "edits": _edits_out(st), "events": [st.show(e) for e in d["events"][-events:]] if events else [],
            "event_count": len(d["events"]), "saved": d.get("saved") or [], "closed": bool(d.get("closed"))}


# ---- opening ------------------------------------------------------------------------------------------------------------

def open_session(ctx, model: str, *, by: str = "you", name: str | None = None, raw_weights: bool | None = None,
                 sandbox: bool | None = None, forward_passes: int | None = None, pass_inputs: int | None = None,
                 feedback: bool | None = None) -> dict:
    """Open a session on a model (`digits`, or `digits@v2` for one version). The allowances are fixed here, at the
    start, and cannot be changed later. `feedback=False` opens it blind: scores are measured and kept, and shown
    only when the session is saved or closed."""
    from ..workspace import whoami

    mname, version = WS.split_ref(model)
    if mname in WS.ST.STARTERS:
        WS.ensure_starters(ctx)
    m = WS.manifest(ctx, mname, version)
    kind = _by(by, "you")
    d = DEFAULTS[kind]
    passes = d["forward_passes"] if forward_passes is None else int(forward_passes)
    per = PASS_INPUTS if pass_inputs is None else int(pass_inputs)
    if passes < 0 or per < 1:
        raise WeightsError("The number of forward passes cannot be negative, and one pass holds at least one input.")
    sid = "ws-" + secrets.token_hex(4)
    doc = {
        "id": sid, "model": mname, "version": m["version"], "opened_at": WS.now(),
        "who": {"kind": "agent" if kind == "agent" else "person", "name": name or (whoami() if kind == "you" else "agent")},
        "allowed": {"raw_weights": d["raw_weights"] if raw_weights is None else bool(raw_weights),
                    "sandbox": d["sandbox"] if sandbox is None else bool(sandbox), "forward_passes": passes, "pass_inputs": per,
                    "feedback": True if feedback is None else bool(feedback)},
        "used": {"weights_read": 0, "sandbox_used": False, "forward_used": 0, "passes": [], "gradients_computed": False,
                 "evaluations": 0, "training_steps": 0, "score_seen": None},
        "revealed": False, "closed": False,
        "sets": {"target": None, "should_not_change": None},
        "edits": [], "actions": [], "events": [], "saved": [],
    }
    st = State(ctx, doc, None)
    a = doc["allowed"]
    st.event("open", f"{_Who(st)} opened {WS.ref_of(mname, m['version'])}. Allowed: raw weights {'yes' if a['raw_weights'] else 'no'}, "
                     f"a sandbox {'yes' if a['sandbox'] else 'no'}, {a['forward_passes']} forward passes, scores "
                     f"{'shown as it goes' if a['feedback'] else 'hidden until it is saved or closed'}.", allowed=dict(a))
    try:
        ctx.store.put_new(_skey(mname, sid), json.dumps(doc).encode())
    except AlreadyExists:
        raise WeightsError("Could not start a session. Try again.") from None
    _WHERE[(ctx.store.uri, sid)] = mname
    _tell(st)
    return _session_out(st)


def sessions(ctx, model: str) -> list[dict]:
    """The sessions opened on a model, newest first."""
    mname, _ = WS.split_ref(model)
    out = []
    for key in ctx.store.list(f"{WS.ROOT}/{mname}/sessions/"):
        if key.replace("\\", "/").endswith("/session.json"):
            d = json.loads(ctx.store.get(key).decode())
            out.append({"id": d["id"], "model": d["model"], "version": d["version"], "who": d["who"], "opened_at": d["opened_at"],
                        "events": len(d["events"]), "edits": len(d["edits"])})
    return sorted(out, key=lambda r: (r["opened_at"], r["id"]), reverse=True)


def session_for(ctx, model: str) -> dict:
    """The newest session on a model, opened for a person if there is none. What a screen asks for first."""
    mname, _ = WS.split_ref(model)
    WS.manifest(ctx, mname)
    have = sessions(ctx, mname)
    return session(ctx, have[0]["id"]) if have else open_session(ctx, model)


def current_digest(ctx, sid: str) -> dict:
    """A checksum of the session's numbers as they are now, and what it has to do with saved versions:
    {digest, base, changed, saved: [refs], matches: the saved ref with these exact numbers, or None}.
    For code that must not save the same numbers twice. It reads no number out, so it is not an event."""
    def fn(st):
        digest = WS.digest(st.cur)
        match = None
        for ref in reversed(st.doc.get("saved") or []):
            name, version = WS.split_ref(ref)
            if WS.manifest(ctx, name, version).get("sha256") == digest:
                match = ref
                break
        return {"digest": digest, "base": WS.ref_of(st.model, st.doc["version"]), "changed": st.changed(),
                "saved": list(st.doc.get("saved") or []), "matches": match}
    return _do(ctx, sid, fn, watch="record")


def session(ctx, sid: str) -> dict:
    return _do(ctx, sid, _session_out, watch="record")


def events(ctx, sid: str, since: int = 0) -> dict:
    """The session's events after number `since`, oldest first."""
    def fn(st):
        return {"session": st.sid, "model": st.model, "events": [st.show(e) for e in st.doc["events"] if e["seq"] > int(since)],
                "last": len(st.doc["events"])}
    return _do(ctx, sid, fn, watch="record")


@_by_kind
def report(ctx, sid: str) -> dict:
    """The whole record: the allowances, what was used, every operation, every edit kept or taken back, and the label."""
    def fn(st):
        out = _session_out(st, events=0)
        out["events"] = [st.show(e) for e in st.doc["events"]]
        out["passes"] = st.doc["used"]["passes"]
        out["diff"] = _diff(st, listed=0)
        out["kept"] = [e for e in out["edits"] if e["state"] == "applied"]
        out["taken_back"] = [e for e in out["edits"] if e["state"] != "applied"]
        return out
    return _do(ctx, sid, fn, watch="record")


# ---- words --------------------------------------------------------------------------------------------------------------

def _Who(st: State, by: str | None = None) -> str:
    who = _by(by, st.who)
    return "The agent" if who == "agent" else "Captain" if who == "captain" else "You"


def _g(x) -> str:
    return "none" if x is None else f"{float(x):.4g}"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _number_words(st: State, tensor: str, ix) -> str:
    """One number by where it sits, counted from 1 as a person counts."""
    k, kind = st.where(tensor)
    if kind == "bias":
        return f"the bias of layer {k + 1}, neuron {ix[0] + 1}"
    return f"the weight into layer {k + 1}, neuron {ix[0] + 1} from neuron {ix[1] + 1}"


# ---- reading ------------------------------------------------------------------------------------------------------------

@_by_kind
def layers(ctx, sid: str, by: str | None = None, watch: bool = False) -> dict:
    def fn(st):
        m = st.manifest
        rows = []
        for k, ly in enumerate(m["layers"]):
            w, b = st.cur[ly["weight"]], st.cur[ly["bias"]]
            rows.append({"layer": k, "column": k + 1, "name": ly["name"], "inputs": int(w.shape[1]), "outputs": int(w.shape[0]),
                         "activation": ly["activation"], "weight": {"tensor": ly["weight"], "shape": list(w.shape)},
                         "bias": {"tensor": ly["bias"], "shape": list(b.shape)}, "parameters": int(w.size + b.size)})
        if not st.watch:
            st.event("read", f"{_Who(st, by)} listed the {_plural(len(rows), 'layer')}.", by, op="layers")
        return {"sizes": m["sizes"], "cost": m.get("cost"), "parameters": st.total(), "layers": rows,
                "inputs": m.get("inputs"), "outputs": m.get("outputs"), "data": m.get("data")}
    return _do(ctx, sid, fn, watch)


@_by_kind
def session_layers(ctx, sid: str) -> list[dict]:
    """The session's layers and their tensor names, for turning "layer 2" into a tensor. Shapes only: no numbers,
    so it is not an event."""
    def fn(st):
        return [{"layer": k, "weight": {"tensor": ly["weight"]}, "bias": {"tensor": ly["bias"]}} for k, ly in enumerate(st.manifest["layers"])]
    return _do(ctx, sid, fn, watch="record")


def _span(text, size: int, what: str) -> tuple[int, int]:
    """'2', '0:4' or nothing (the whole range) as a start and an end."""
    if text in (None, ""):
        return 0, size
    try:
        if isinstance(text, (list, tuple)):
            a, b = int(text[0]), int(text[1])
        elif ":" in str(text):
            x, y = str(text).split(":", 1)
            a, b = int(x or 0), int(y or size)
        else:
            a = int(text)
            b = a + 1
    except (ValueError, IndexError):
        raise WeightsError(f"{what} are written like 3 or 0:4. {text!r} is neither.") from None
    if not (0 <= a < b <= size):
        raise WeightsError(f"{what} {a}:{b} are outside this tensor, which has {size}.")
    return a, b


def _touch(st: State, k: int, kind: str, rows: range, cols: range | None) -> dict:
    """What a read or an edit touched, in the shape a screen lights up."""
    neurons = [[k + 1, int(j)] for j in list(rows)[:LIST_CAP]]
    weights = []
    if kind == "weight" and cols is not None:
        for j in rows:
            for i in cols:
                if len(weights) >= LIST_CAP:
                    break
                weights.append([k, int(j), int(i)])
    return {"neurons": neurons, "weights": weights}


@_by_kind
def read(ctx, sid: str, tensor: str, rows=None, cols=None, by: str | None = None, watch: bool = False) -> dict:
    """A slice of raw numbers. Refused when the session may not see raw weights. Every number returned is counted."""
    def fn(st):
        k, kind = st.where(tensor)
        t = st.cur[tensor]
        r0, r1 = _span(rows, t.shape[0], "Rows")
        if kind == "weight":
            c0, c1 = _span(cols, t.shape[1], "Columns")
            vals = t[r0:r1, c0:c1]
        else:
            if cols not in (None, ""):
                raise WeightsError("A bias is one list of numbers. It has rows and no columns.")
            c0, c1 = 0, 1
            vals = t[r0:r1]
        st.looked = int(vals.size)
        if not st.watch:
            if not st.doc["allowed"]["raw_weights"]:
                raise Refused("This session may not see raw weights. It was opened without them, and that cannot be changed.")
            st.doc["used"]["weights_read"] += int(vals.size)
            st.event("read", f"{_Who(st, by)} read {_plural(int(vals.size), 'number')} of {tensor}.", by, op="read", tensor=tensor,
                     layer=k + 1, args={"rows": [r0, r1], "cols": [c0, c1] if kind == "weight" else None}, count=int(vals.size),
                     touched=_touch(st, k, kind, range(r0, r1), range(c0, c1) if kind == "weight" else None))
        return {"tensor": tensor, "shape": list(t.shape), "rows": [r0, r1], "cols": [c0, c1] if kind == "weight" else None,
                "values": vals.astype(float).tolist(), "count": int(vals.size)}
    return _do(ctx, sid, fn, watch)


def _tensor_stats(name: str, t: np.ndarray) -> dict:
    f = t.astype(float).reshape(-1)
    q = np.percentile(f, [0, 25, 50, 75, 100])
    return {"tensor": name, "shape": list(t.shape), "count": int(f.size), "mean": float(f.mean()), "variance": float(f.var()),
            "min": float(q[0]), "p25": float(q[1]), "median": float(q[2]), "p75": float(q[3]), "max": float(q[4]),
            "norm": float(np.linalg.norm(f)), "zero_share": float((f == 0).mean()), "distinct": int(np.unique(f).size)}


@_by_kind
def stats(ctx, sid: str, tensor: str | None = None, neurons: bool = False, by: str | None = None, watch: bool = False) -> dict:
    """Numbers about the numbers: per tensor (weights and biases apart), and per neuron when asked."""
    def fn(st):
        if tensor:
            st.where(tensor)
        m = st.manifest
        names = [tensor] if tensor else [t for ly in m["layers"] for t in (ly["weight"], ly["bias"])]
        out = {"tensors": [_tensor_stats(n, st.cur[n]) for n in names]}
        if neurons:
            rows = []
            for k, ly in enumerate(m["layers"]):
                if tensor and tensor not in (ly["weight"], ly["bias"]):
                    continue
                w, b = st.cur[ly["weight"]].astype(float), st.cur[ly["bias"]].astype(float)
                for j in range(w.shape[0]):
                    rows.append({"layer": k, "column": k + 1, "neuron": j, "bias": float(b[j]), "weight_norm": float(np.linalg.norm(w[j])),
                                 "weight_mean": float(w[j].mean()), "weight_min": float(w[j].min()), "weight_max": float(w[j].max()),
                                 "zero_share": float((w[j] == 0).mean()), "dead": bool((w[j] == 0).all())})
            out["neurons"] = rows
        first, last = st.cur[m["layers"][0]["bias"]].astype(float), st.cur[m["layers"][-1]["bias"]].astype(float)
        out["bias_range"] = {"first": float(first.max() - first.min()), "last": float(last.max() - last.min())}
        if not st.watch:
            st.event("read", f"{_Who(st, by)} looked at the statistics of {tensor or 'every tensor'}.", by, op="stats",
                     tensor=tensor, args={"neurons": bool(neurons)})
        return out
    return _do(ctx, sid, fn, watch)


def _grid_shape(st: State, k: int, n: int) -> list[int]:
    """The shape to draw a neuron's incoming weights in: the picture's for a picture input, a square when the
    count is one, else a single row."""
    shape = (st.manifest.get("inputs") or {}).get("shape") if k == 0 else None
    if shape and int(np.prod(shape)) == n:
        return [int(s) for s in shape]
    side = int(round(n ** 0.5))
    return [side, side] if side * side == n and n > 3 else [1, n]


@_by_kind
def heatmap(ctx, sid: str, layer=0, neuron=None, by: str | None = None, watch: bool = False, tensor: str | None = None,
            rows=None, cols=None) -> dict:
    """A layer's weights per input as grids, one per neuron: what each neuron looks for. `layer` counts from 0.
    A layer can be named by its weight tensor instead. (`rows` and `cols` are for models read by tensor: real.py.)"""
    def fn(st):
        k = st.where(tensor)[0] if tensor else layer
        ly = st.layer(k)
        k = int(k)
        w, b = st.cur[ly["weight"]], st.cur[ly["bias"]]
        if neuron in (None, ""):
            js = list(range(w.shape[0]))
        else:
            js = [int(x) for x in (neuron if isinstance(neuron, (list, tuple)) else str(neuron).split(","))]
            for j in js:
                if not 0 <= j < w.shape[0]:
                    raise WeightsError(f"Layer {k + 1} has {w.shape[0]} neurons. The neuron asked for is not one of them.")
        shape = _grid_shape(st, k, w.shape[1])
        grids = [{"neuron": j, "bias": float(b[j]), "min": float(w[j].min()), "max": float(w[j].max()),
                  "grid": w[j].astype(float).reshape(shape).tolist()} for j in js]
        count = len(js) * (w.shape[1] + 1)
        st.looked = count
        if not st.watch:
            if not st.doc["allowed"]["raw_weights"]:
                raise Refused("This session may not see raw weights, and a heatmap is made of them.")
            st.doc["used"]["weights_read"] += count
            st.event("read", f"{_Who(st, by)} looked at the incoming weights of {_plural(len(js), 'neuron')} in layer {k + 1} as a grid.",
                     by, op="heatmap", tensor=ly["weight"], layer=k + 1, args={"neuron": js if neuron not in (None, "") else None},
                     count=count, touched={"neurons": [[k + 1, j] for j in js[:LIST_CAP]], "weights": []})
        return {"layer": k, "column": k + 1, "tensor": ly["weight"], "grid_shape": shape,
                "scale": float(np.abs(w[js]).max()) if js else 0.0, "grids": grids}
    return _do(ctx, sid, fn, watch)


@_by_kind
def template(ctx, sid: str, output=0, by: str | None = None, watch: bool = False) -> dict:
    """One output neuron's incoming weights: which neurons of the column before it vote for this output."""
    def fn(st):
        k = len(st.manifest["layers"]) - 1
        ly = st.manifest["layers"][k]
        w, b = st.cur[ly["weight"]], st.cur[ly["bias"]]
        try:
            j = int(output)
        except (TypeError, ValueError):
            raise WeightsError(f"An output is a whole number. {output!r} is not one.") from None
        if not 0 <= j < w.shape[0]:
            raise WeightsError(f"This network has {w.shape[0]} outputs. The output asked for is not one of them.")
        shape = _grid_shape(st, k, w.shape[1])
        names = (st.manifest.get("outputs") or {}).get("names") or []
        st.looked = int(w.shape[1] + 1)
        if not st.watch:
            if not st.doc["allowed"]["raw_weights"]:
                raise Refused("This session may not see raw weights, and an output's template is made of them.")
            st.doc["used"]["weights_read"] += int(w.shape[1] + 1)
            st.event("read", f"{_Who(st, by)} looked at the weights coming into output {j + 1}.", by, op="template", tensor=ly["weight"],
                     layer=k + 1, neuron=j, count=int(w.shape[1] + 1), touched=_touch(st, k, "weight", range(j, j + 1), range(w.shape[1])))
        return {"layer": k, "column": k + 1, "output": j, "name": names[j] if j < len(names) else str(j), "bias": float(b[j]),
                "weights": w[j].astype(float).tolist(), "grid_shape": shape, "grid": w[j].astype(float).reshape(shape).tolist(),
                "scale": float(np.abs(w).max())}
    return _do(ctx, sid, fn, watch)


# ---- running ------------------------------------------------------------------------------------------------------------

def _examples(st: State, which: str = "practice"):
    got = WS.sets(st.ctx, st.manifest, which)
    if got is None:
        raise WeightsError(f"This model has no {which} examples. Give the inputs themselves.")
    return got


def _inputs(st: State, inputs=None, targets=None, example=None):
    """The inputs a run is on, their wanted outputs if known, and a short name for the record."""
    if example not in (None, ""):
        idx = [int(e) for e in (example if isinstance(example, (list, tuple)) else str(example).split(","))]
        x, y = _examples(st)
        for i in idx:
            if not 0 <= i < len(x):
                raise WeightsError(f"There are {len(x)} examples. The example asked for is not one of them.")
        return x[idx], np.asarray(y)[idx], "example" + ("s " if len(idx) != 1 else " ") + ", ".join(str(i + 1) for i in idx)
    if inputs is None:
        raise WeightsError("Give the inputs to run, or a practice example by number.")
    try:
        x = np.asarray(inputs, dtype=float)
    except (TypeError, ValueError):
        raise WeightsError("Inputs are numbers: one list per input.") from None
    if x.ndim == 1:
        x = x[None, :]
    y = None
    if targets is not None:
        y = np.asarray(targets)
        if y.ndim == 0:
            y = y.reshape(1)
    return x, y, _plural(len(x), "given input")


def _spend(st: State, n_inputs: int, op: str) -> None:
    """Count one forward pass, or refuse it past the allowance."""
    st.open_for_changes()
    a, u = st.doc["allowed"], st.doc["used"]
    if n_inputs > a["pass_inputs"]:
        raise Refused(f"One forward pass holds at most {a['pass_inputs']} inputs in this session, and this one has {n_inputs}.")
    if u["forward_used"] >= a["forward_passes"]:
        if a["forward_passes"] == 0:
            raise Refused("This session was opened with no forward passes, so the network cannot be run in it.")
        raise Refused(f"All {a['forward_passes']} forward passes of this session are used. The network cannot be run again in it.")
    u["forward_used"] += 1
    u["passes"].append({"n": u["forward_used"], "inputs": int(n_inputs), "op": op})


def _left(st: State) -> dict:
    a, u = st.doc["allowed"], st.doc["used"]
    return {"forward_used": u["forward_used"], "forward_left": max(0, a["forward_passes"] - u["forward_used"])}


def _answer(row: np.ndarray) -> int:
    return int(row[0] > 0.5) if row.size == 1 else int(row.argmax())


def _cost_terms(net: N.Network, out: np.ndarray, want: np.ndarray) -> list[dict]:
    """What each output adds to one example's cost. The terms add up to the cost."""
    one = N.Network(net.layers, net.cost)
    rows = []
    for j in range(out.size):
        if net.cost == "squared_error":
            term = float((out[j] - want[j]) ** 2)
        else:
            p = float(np.clip(out[j], 1e-12, 1 - 1e-12))
            term = float(-want[j] * np.log(p)) if one.layers[-1].activation == "softmax" else float(-(want[j] * np.log(p) + (1 - want[j]) * np.log(1 - p)))
        rows.append({"output": j, "a": float(out[j]), "wanted": float(want[j]), "term": term})
    return rows


@_by_kind
def forward(ctx, sid: str, inputs=None, targets=None, example=None, by: str | None = None) -> dict:
    """Run the network on inputs. Costs one forward pass."""
    def fn(st):
        x, y, name = _inputs(st, inputs, targets, example)
        net = st.net()
        xb = N._batch(net, x)
        _spend(st, len(xb), "forward")
        out = N.predict(net, xb)
        res = {"outputs": np.round(out, 6).tolist(), "predicted": [_answer(r) for r in out]}
        if y is not None:
            res["cost"] = N.cost(net, xb, y)
        st.event("run", f"{_Who(st, by)} ran the network on {name}. That is forward pass {st.doc['used']['forward_used']} of "
                        f"{st.doc['allowed']['forward_passes']}.", by, op="forward", input=name, inputs=len(xb), run_cost=res.get("cost"))
        return {**res, **_left(st)}
    return _do(ctx, sid, fn)


@_by_kind
def activations(ctx, sid: str, inputs=None, targets=None, example=None, by: str | None = None) -> dict:
    """Every neuron's weighted sum and activation for one input. Costs one forward pass."""
    def fn(st):
        x, y, name = _inputs(st, inputs, targets, example)
        net = st.net()
        xb = N._batch(net, x)
        if len(xb) != 1:
            raise WeightsError("Activations are shown for one input at a time.")
        _spend(st, 1, "activations")
        zs, acts = N.forward(net, xb)
        res = {"input": np.round(acts[0][0], 6).tolist(),
               "columns": [{"column": k + 1, "layer": k, "z": np.round(zs[k][0], 6).tolist(), "a": np.round(acts[k + 1][0], 6).tolist()}
                           for k in range(len(zs))],
               "output": np.round(acts[-1][0], 6).tolist(), "predicted": _answer(acts[-1][0])}
        if y is not None:
            yb = N._targets(net, y, 1)
            res["wanted"] = yb[0].tolist()
            res["cost"] = float(N.cost_each(net, acts[-1], yb)[0])
            res["cost_terms"] = _cost_terms(net, acts[-1][0], yb[0])
        st.event("run", f"{_Who(st, by)} looked at every neuron's activation on {name}. That is forward pass {st.doc['used']['forward_used']} of "
                        f"{st.doc['allowed']['forward_passes']}.", by, op="activations", input=name, inputs=1, run_cost=res.get("cost"))
        return {**res, **_left(st)}
    return _do(ctx, sid, fn)


@_by_kind
def gradients(ctx, sid: str, inputs=None, targets=None, example=None, layer=None, neuron=None, by: str | None = None) -> dict:
    """The cost and its gradient for every weight and bias, and what each neuron wants. Costs one forward pass and
    marks the session as having computed gradients."""
    def fn(st):
        x, y, name = _inputs(st, inputs, targets, example)
        if y is None:
            raise WeightsError("Gradients need the wanted outputs as well as the inputs.")
        net = st.net()
        xb = N._batch(net, x)
        _spend(st, len(xb), "gradients")
        g = N.backprop(net, xb, y)
        rows, top = [], []
        for k, (ly, gl) in enumerate(zip(st.manifest["layers"], g["layers"])):
            w = gl["weight"]
            rows.append({"layer": k, "column": k + 1, "name": ly["name"], "weight": w.tolist() if w.size <= GRAD_CAP else None,
                         "bias": gl["bias"].tolist(), "dcost_da": gl["dcost_da"].tolist(), "wanted_nudge": gl["wanted_nudge"].tolist(),
                         "dcost_dz": gl["dcost_dz"].tolist(), "da_dz": gl["da_dz"].tolist()})
            flat = np.abs(w).reshape(-1)
            for f in np.argsort(flat)[::-1][:20]:
                j, i = divmod(int(f), w.shape[1])
                top.append({"tensor": ly["weight"], "layer": k, "index": [j, i], "value": float(st.cur[ly["weight"]][j, i]),
                            "gradient": float(w[j, i])})
            for j in np.argsort(np.abs(gl["bias"]))[::-1][:5]:
                top.append({"tensor": ly["bias"], "layer": k, "index": [int(j)], "value": float(st.cur[ly["bias"]][j]),
                            "gradient": float(gl["bias"][j])})
        top = sorted(top, key=lambda r: -abs(r["gradient"]))[:20]
        res = {"cost": g["cost"], "examples": g["examples"], "layers": rows, "top": top,
               "input_dcost_da": g["input_dcost_da"].tolist() if g["input_dcost_da"].size <= GRAD_CAP else None}
        if layer not in (None, "") and neuron not in (None, ""):
            ly = st.layer(layer)
            gw = g["layers"][int(layer)]["weight"]
            if not 0 <= int(neuron) < gw.shape[0]:
                raise WeightsError(f"Layer {int(layer) + 1} has {gw.shape[0]} neurons. The neuron asked for is not one of them.")
            res["row"] = {"layer": int(layer), "neuron": int(neuron), "tensor": ly["weight"], "gradient": gw[int(neuron)].tolist(),
                          "bias_gradient": float(g["layers"][int(layer)]["bias"][int(neuron)])}
        if layer not in (None, "") and int(layer) >= 1:
            # How the neurons of the column before come to want what they want: each neuron after them asks, through
            # its weight, and the asks add up. cells[i][j] is what neuron j of this layer asks of neuron i before it.
            st.layer(layer)
            k = int(layer)
            w = np.asarray(net.layers[k].weight, dtype=float)
            cells = w.T * g["layers"][k]["dcost_dz"][None, :]
            if cells.size <= GRAD_CAP:
                res["desires"] = {"layer": k, "of_column": k, "asked_by_column": k + 1, "cells": cells.tolist(),
                                  "sums": cells.sum(axis=1).tolist(), "exact": g["examples"] == 1}
        st.doc["used"]["gradients_computed"] = True
        st.event("gradients", f"{_Who(st, by)} computed the gradient of the cost on {name}. The cost is {_g(g['cost'])}.", by,
                 op="gradients", input=name, inputs=len(xb), run_cost=g["cost"],
                 touched={"neurons": [], "weights": [[t["layer"], *t["index"]] for t in top if len(t["index"]) == 2]})
        return {**res, **_left(st)}
    return _do(ctx, sid, fn)


@_by_kind
def trace(ctx, sid: str, inputs=None, targets=None, example=None, path=None, by: str | None = None) -> dict:
    """The chain rule along one path through the network for one input: three factors per layer. Costs one forward
    pass and marks the session as having computed gradients."""
    def fn(st):
        x, y, name = _inputs(st, inputs, targets, example)
        if y is None:
            raise WeightsError("A trace needs the wanted output as well as the input.")
        net = st.net()
        p = None
        if path not in (None, ""):
            try:
                p = [int(v) for v in (path if isinstance(path, (list, tuple)) else str(path).split(","))]
            except ValueError:
                raise WeightsError("A path is one neuron number per column, like 0,2,0.") from None
        xb = N._batch(net, x)
        if len(xb) != 1:
            raise WeightsError("A trace follows one example. Give one input.")
        t = N.trace(net, xb, y, p)
        _spend(st, 1, "trace")
        st.doc["used"]["gradients_computed"] = True
        st.event("gradients", f"{_Who(st, by)} traced the cost back along the path through neurons "
                              f"{', '.join(str(v + 1) for v in t['path'])} (input first) on {name}.",
                 by, op="trace", input=name, inputs=1, run_cost=t["cost"],
                 touched={"neurons": [[c, j] for c, j in enumerate(t["path"])],
                          "weights": [[s["layer"], *s["weight"]["index"]] for s in t["steps"]]})
        return {**t, **_left(st)}
    return _do(ctx, sid, fn)


@_by_kind
def cost_slice(ctx, sid: str, tensor: str, index, inputs=None, targets=None, example=None, span: float | None = None,
               points: int = 21, by: str | None = None) -> dict:
    """The cost as one number is moved and every other stays put: a slice through one of the network's dimensions.

    The network is run once per point, so this costs `points` forward passes. Nothing is changed."""
    def fn(st):
        k, kind = st.where(tensor)
        t = st.cur[tensor]
        try:
            ix = tuple(int(v) for v in (index if isinstance(index, (list, tuple)) else str(index).split(",")))
        except ValueError:
            raise WeightsError("A position is written like 2,0 for a weight or 2 for a bias.") from None
        if len(ix) != t.ndim or any(not 0 <= v < n for v, n in zip(ix, t.shape)):
            raise WeightsError(f"The position {list(ix)} is outside {tensor}, whose shape is {list(t.shape)}.")
        n_pts = int(points)
        if not 3 <= n_pts <= 201:
            raise WeightsError("A slice has between 3 and 201 points.")
        x, y, name = _inputs(st, inputs, targets, example)
        if y is None:
            raise WeightsError("A slice of the cost needs the wanted outputs as well as the inputs.")
        net = st.net().copy()
        xb = N._batch(net, x)
        now = float(t[ix])
        half = float(span) / 2 if span not in (None, "") else max(1.0, abs(now))
        if not half > 0:
            raise WeightsError("The span of a slice is a number above zero.")
        a, u = st.doc["allowed"], st.doc["used"]
        if len(xb) > a["pass_inputs"]:
            raise Refused(f"One forward pass holds at most {a['pass_inputs']} inputs in this session, and this one has {len(xb)}.")
        if u["forward_used"] + n_pts > a["forward_passes"]:
            raise Refused(f"A slice of {n_pts} points runs the network {n_pts} times, and of this session's forward passes there are "
                          f"{max(0, a['forward_passes'] - u['forward_used'])} left.")
        for _ in range(n_pts):
            _spend(st, len(xb), "slice")
        slope = N.backprop(net, xb, y)["layers"][k][kind][ix]
        values = np.linspace(now - half, now + half, n_pts)
        target = net.layers[k].weight if kind == "weight" else net.layers[k].bias
        target = np.asarray(target, dtype=float).copy()
        setattr(net.layers[k], kind, target)
        costs = []
        for v in values:
            target[ix] = v
            costs.append(N.cost(net, xb, y))
        target[ix] = now
        cost_now = N.cost(net, xb, y)
        st.doc["used"]["gradients_computed"] = True
        st.event("run", f"{_Who(st, by)} measured the cost at {n_pts} values of {_number_words(st, tensor, ix)} on {name}. "
                        f"That used {n_pts} forward passes.", by, op="slice", tensor=tensor, input=name, inputs=len(xb),
                 args={"index": list(ix), "points": n_pts, "span": 2 * half},
                 result={"cost": cost_now, "slope": float(slope), "values": values.tolist(), "costs": costs},
                 touched=_touch(st, k, kind, range(ix[0], ix[0] + 1), range(ix[1], ix[1] + 1) if kind == "weight" else None))
        if not st.scores_visible:   # a blind session: measured and kept, shown when it is saved or closed
            return {"tensor": tensor, "index": list(ix), "value": now, "measured": True, "scores": "hidden", "says": BLIND,
                    "examples": len(xb), "dimensions": st.total(), **_left(st)}
        return {"tensor": tensor, "index": list(ix), "value": now, "cost": cost_now, "slope": float(slope), "values": values.tolist(),
                "costs": costs, "examples": len(xb), "dimensions": st.total(), **_left(st)}
    return _do(ctx, sid, fn)


# ---- the three sets -------------------------------------------------------------------------------------------------------

SET_NAMES = {"target": "target", "should_not_change": "should not change", "held_out": "held out"}


def _labels(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y)
    if y.ndim == 2 and y.shape[1] > 1:
        return y.argmax(axis=1)
    return y.reshape(-1).astype(float).round().astype(int)


def _resolve(st: State, which: str):
    """(inputs, targets, a sentence) for one of the three sets, or None when it is not defined."""
    if which == "held_out":
        got = WS.sets(st.ctx, st.manifest, "held_out")
        return None if got is None else (got[0], got[1], "the model's held-out set")
    spec = st.doc["sets"].get(which)
    if not spec:
        return None
    if "inputs" in spec:
        n = len(spec["inputs"])
        return np.asarray(spec["inputs"], dtype=float), np.asarray(spec["targets"]), f"{_plural(n, 'input')} given when the set was named"
    if "classes" in spec:
        got = WS.sets(st.ctx, st.manifest, "evaluation")
        if got is None:
            raise WeightsError("This model has no evaluation pool to draw classes from. Give the set's inputs and targets instead.")
        x, y = got
        keep = np.isin(_labels(y), np.asarray(spec["classes"], dtype=int))
        return x[keep], np.asarray(y)[keep], "classes " + ", ".join(str(c) for c in spec["classes"]) + " from the evaluation pool"
    return None


def _sets_out(st: State) -> dict:
    out = {}
    for which in SET_NAMES:
        got = _resolve(st, which)
        out[which] = {"defined": False} if got is None or len(got[0]) == 0 else {"defined": True, "of": int(len(got[0])), "says": got[2]}
        if which == "held_out" and out[which]["defined"]:
            # A fact about the version the session was opened on, before any edit: not a score of the caller's work.
            m = _measure(st.net(base=True), got[0], got[1])
            out[which]["base"] = {"model": WS.ref_of(st.model, st.doc["version"]), "cost": m["cost"], "right": m["right"], "of": int(len(got[0]))}
    return out


def _check_spec(st: State, spec, which: str):
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise WeightsError(f"The {SET_NAMES[which]} set is given as classes, or as inputs with targets.")
    n_out = st.manifest["sizes"][-1]
    if "classes" in spec:
        cs = [int(c) for c in spec["classes"]]
        top = max(n_out, 2)
        if not cs or any(not 0 <= c < top for c in cs):
            raise WeightsError(f"Classes are numbers from 0 to {top - 1}.")
        return {"classes": cs}
    if "example" in spec:
        # "Fix this one": one of the model's examples, copied in as its inputs and wanted output. Only a target can
        # be named this way: the examples are for looking at and are no measure of what must not change.
        if which != "target":
            raise WeightsError("Only the target can be one of the model's examples. The examples are never a measure of what must not change.")
        x, y = _examples(st)
        i = int(spec["example"])
        if not 0 <= i < len(x):
            raise WeightsError(f"There are {len(x)} examples. The example asked for is not one of them.")
        yi = np.asarray(y[i])
        spec = {"inputs": [x[i].tolist()], "targets": [int(yi)] if yi.ndim == 0 else [yi.tolist()]}
    if "inputs" in spec and "targets" in spec:
        x = np.asarray(spec["inputs"], dtype=float)
        net = st.net()
        xb = N._batch(net, x)
        N._targets(net, np.asarray(spec["targets"]), len(xb))
        return {"inputs": xb.tolist(), "targets": np.asarray(spec["targets"]).tolist()}
    raise WeightsError(f"The {SET_NAMES[which]} set is given as classes, or as inputs with targets. The examples shown with a "
                       "network are for looking at and are never scored.")


@_by_kind
def set_sets(ctx, sid: str, target=None, should_not_change=None, by: str | None = None) -> dict:
    """Name what the edits should fix (target) and what they must leave alone (should not change). The held-out set
    is the model's own and cannot be changed."""
    def fn(st):
        if target is not None:
            st.doc["sets"]["target"] = _check_spec(st, target, "target")
        if should_not_change is not None:
            st.doc["sets"]["should_not_change"] = _check_spec(st, should_not_change, "should_not_change")
        out = _sets_out(st)
        for which in ("target", "should_not_change"):
            if (target if which == "target" else should_not_change) is not None and not out[which]["defined"]:
                raise WeightsError(f"The {SET_NAMES[which]} set would be empty. Nothing in the pool matches it.")
        st.open_for_changes()
        if st.program and _by(by, st.who) == "agent":
            raise Refused("The sets for this run were named by Captain before the agent started, and the agent may not change them.")
        st.event("sets", f"{_Who(st, by)} named the sets: target is {out['target'].get('says', 'not defined')}, should not change is "
                         f"{out['should_not_change'].get('says', 'not defined')}.", by, op="sets",
                 args={"target": st.doc["sets"]["target"], "should_not_change": st.doc["sets"]["should_not_change"]})
        return out
    return _do(ctx, sid, fn)


def _measure(net: N.Network, x, y) -> dict:
    xb = N._batch(net, x)
    yb = N._targets(net, y, len(xb))
    out = N.predict(net, xb)
    answers = np.array([_answer(r) for r in out])
    wanted = np.array([_answer(r) for r in yb])
    return {"cost": float(N.cost_each(net, out, yb).mean()), "right": int((answers == wanted).sum()), "answers": answers, "wanted": wanted}


def _both(a: dict, b: dict, name: str, of: int, says: str | None = None) -> dict:
    out = {"name": name, "of": of, "before": a["cost"], "after": b["cost"], "right_before": a["right"], "right_after": b["right"],
           "answers_changed": int((a["answers"] != b["answers"]).sum()),
           "broke": int(((a["answers"] == a["wanted"]) & (b["answers"] != b["wanted"])).sum())}
    if says:
        out["says"] = says
    return out


def _three(st: State, before: N.Network, after: N.Network) -> dict:
    """The three sets measured on two networks, as {target, keep, test}. A set that is not defined is left out."""
    out = {}
    for key, which in (("target", "target"), ("keep", "should_not_change"), ("test", "held_out")):
        got = _resolve(st, which)
        if got is None or len(got[0]) == 0:
            continue
        x, y, _ = got
        out[key] = _both(_measure(before, x, y), _measure(after, x, y), SET_NAMES[which], int(len(x)))
    return out


def _drift(st: State, x) -> list[dict]:
    """How far each column's activations moved from the base on the same inputs."""
    base, cur = st.net(base=True), st.net()
    xb = N._batch(base, x)
    _, a0 = N.forward(base, xb)
    _, a1 = N.forward(cur, xb)
    rows = []
    for k in range(len(base.layers)):
        b, c = a0[k + 1], a1[k + 1]
        d = c - b
        nb, nc = np.linalg.norm(b, axis=1), np.linalg.norm(c, axis=1)
        sim = np.where((nb > 0) & (nc > 0), (b * c).sum(axis=1) / np.maximum(nb * nc, 1e-300), 1.0)
        rows.append({"layer": k, "column": k + 1, "name": base.layers[k].name, "mean_change": float(np.abs(d).mean()),
                     "relative": float(np.linalg.norm(d) / max(np.linalg.norm(b), 1e-300)), "similarity": float(sim.mean())})
    return rows


@_by_kind
def evaluate(ctx, sid: str, by: str | None = None) -> dict:
    """The base and the network as it is now, on the same three sets with the same settings, and how far each
    column's activations moved. Run by the tool: the examples are never returned."""
    def fn(st):
        base, cur = st.net(base=True), st.net()
        sets = {}
        drift_x, drift_on = None, None
        for which, name in SET_NAMES.items():
            got = _resolve(st, which)
            if got is None or len(got[0]) == 0:
                sets[which] = {"name": name, "defined": False}
                continue
            x, y, says = got
            sets[which] = {"defined": True, **_both(_measure(base, x, y), _measure(cur, x, y), name, int(len(x)), says)}
            if which == "held_out" or drift_x is None:
                drift_x, drift_on = x, name
        st.doc["used"]["evaluations"] += 1
        ho, tg = sets["held_out"], sets["target"]
        words = []
        if tg["defined"]:
            words.append(f"cost on the target is {_g(tg['after'])} against {_g(tg['before'])} for the base")
        if ho["defined"]:
            words.append(f"the held-out set has {ho['right_after']} of {ho['of']} right against {ho['right_before']}")
        res = {"sets": sets, "drift": _drift(st, drift_x) if drift_x is not None else [], "drift_on": drift_on,
               "before_is": WS.ref_of(st.model, st.doc["version"]), "evaluations": st.doc["used"]["evaluations"],
               "changed": st.changed(), "total": st.total()}
        ev = st.event("evaluate", f"{_Who(st, by)} measured the network against its base.", by,
                      score_sentence=("The " + ", and ".join(words) + ".").replace("The cost", "Cost") if words else "No set is defined.",
                      op="evaluate", cost_before=tg.get("before") if tg["defined"] else ho.get("before"),
                      cost_after=tg.get("after") if tg["defined"] else ho.get("after"),
                      cost={k: {x: v for x, v in sets[w].items() if x not in ("defined", "says")}
                            for k, w in (("target", "target"), ("keep", "should_not_change"), ("test", "held_out")) if sets[w]["defined"]},
                      result={"sets": sets, "drift": res["drift"], "drift_on": drift_on})
        if not st.scores_visible:
            return {"measured": True, "scores": "hidden", "says": BLIND, "before_is": res["before_is"],
                    "evaluations": res["evaluations"], "changed": res["changed"], "total": res["total"]}
        st.saw_score(ev, "evaluate")
        return res
    return _do(ctx, sid, fn)


# ---- edits ----------------------------------------------------------------------------------------------------------------

EDIT_OPS = ("set", "scale", "zero", "add", "add_direction", "step")


def _rows(spec, size: int, what: str) -> list[int]:
    if spec in (None, ""):
        return list(range(size))
    js = [int(v) for v in (spec if isinstance(spec, (list, tuple)) else str(spec).split(","))]
    for j in js:
        if not 0 <= j < size:
            raise WeightsError(f"There are {size} {what}. One that was asked for is not among them.")
    return js


def _plan(st: State, req: dict) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """Turn an edit request into (tensor, flat indices, new numbers) parts. Nothing is changed here."""
    op = str(req.get("op") or "").replace("-", "_")
    if op not in EDIT_OPS:
        raise WeightsError(f"An edit is one of: {', '.join(o.replace('_', '-') for o in EDIT_OPS)}. {req.get('op')!r} is not.")

    def num(key):
        try:
            v = float(req[key])
        except (KeyError, TypeError, ValueError):
            raise WeightsError(f"This edit needs `{key}`, a number.") from None
        if not np.isfinite(v):
            raise WeightsError(f"`{key}` must be a finite number.")
        return v

    if op == "step":
        x, y, _ = _inputs(st, req.get("inputs"), req.get("targets"), req.get("example"))
        if y is None:
            raise WeightsError("A training step needs the wanted outputs as well as the inputs.")
        net = st.net()
        xb = N._batch(net, x)
        _spend(st, len(xb), "step")
        g = N.backprop(net, xb, y)
        rate = num("rate") if "rate" in req else 0.5
        parts = []
        for ly, gl in zip(st.manifest["layers"], g["layers"]):
            for kind in ("weight", "bias"):
                t = st.cur[ly[kind]]
                parts.append((ly[kind], np.arange(t.size), (t.astype(float) - rate * gl[kind]).reshape(-1)))
        return parts

    # The neuron form: a column and a neuron in it.
    if req.get("tensor") in (None, ""):
        if req.get("layer") in (None, "") or req.get("neuron") in (None, ""):
            raise WeightsError("An edit names a tensor, or a neuron by its `layer` (1 is the first after the input) and its `neuron`.")
        col = int(req["layer"])
        if not 1 <= col <= len(st.manifest["layers"]):
            raise WeightsError(f"A neuron that can be edited is in layer 1 to {len(st.manifest['layers'])}, and `layer` was {col}.")
        ly = st.manifest["layers"][col - 1]
        w, b = st.cur[ly["weight"]], st.cur[ly["bias"]]
        j = int(req["neuron"])
        if not 0 <= j < w.shape[0]:
            raise WeightsError(f"Layer {col} has {w.shape[0]} neurons. The neuron asked for is not one of them.")
        n_in = w.shape[1]
        row = j * n_in + np.arange(n_in)
        frm = req.get("from")
        if frm not in (None, "") and not 0 <= int(frm) < n_in:
            raise WeightsError(f"A neuron in layer {col} has {n_in} incoming weights. The one asked for is not one of them.")
        if op in ("set", "add"):
            v = num("value")
            if frm in (None, ""):
                return [(ly["bias"], np.array([j]), np.array([v if op == "set" else float(b[j]) + v]))]
            i = int(frm)
            return [(ly["weight"], np.array([j * n_in + i]), np.array([v if op == "set" else float(w[j, i]) + v]))]
        if op == "scale":
            f = num("factor")
            parts = [(ly["weight"], row, w[j].astype(float) * f)]
            if req.get("bias"):
                parts.append((ly["bias"], np.array([j]), np.array([float(b[j]) * f])))
            return parts
        if op == "zero":
            return [(ly["weight"], row, np.zeros(n_in)), (ly["bias"], np.array([j]), np.zeros(1))]
        raise WeightsError("Adding a direction names the tensor: `tensor`, `direction`, `scale`, and `neurons` if not all of them.")

    # The tensor form.
    tensor = str(req["tensor"])
    k, kind = st.where(tensor)
    t = st.cur[tensor]
    if op == "add_direction":
        if kind != "weight":
            raise WeightsError("A direction is added to a weight table, not to a bias.")
        try:
            d = np.asarray(req.get("direction"), dtype=float).reshape(-1)
        except (TypeError, ValueError):
            raise WeightsError("`direction` is a list of numbers.") from None
        s = num("scale") if "scale" in req else 1.0
        along = str(req.get("along") or "inputs")
        new = t.astype(float).copy()
        if along == "inputs":
            if d.size != t.shape[1] or not np.isfinite(d).all():
                raise WeightsError(f"The direction needs {t.shape[1]} finite numbers, one per incoming weight. It has {d.size}.")
            js = _rows(req.get("neurons"), t.shape[0], "neurons in this layer")
            new[js, :] += s * d[None, :]
        elif along == "outputs":
            if d.size != t.shape[0] or not np.isfinite(d).all():
                raise WeightsError(f"The direction needs {t.shape[0]} finite numbers, one per neuron of the layer. It has {d.size}.")
            cols = _rows(req.get("from_neurons"), t.shape[1], "neurons in the column before")
            new[:, cols] += s * d[:, None]
        else:
            raise WeightsError("`along` is \"inputs\" or \"outputs\".")
        return [(tensor, np.arange(t.size), new.reshape(-1))]

    if req.get("indices") not in (None, ""):
        try:
            idx = [tuple(int(v) for v in (ix if isinstance(ix, (list, tuple)) else [ix])) for ix in req["indices"]]
        except (TypeError, ValueError):
            raise WeightsError("`indices` is a list of positions, like [[2, 0], [2, 1]] for a weight table or [[0]] for a bias.") from None
        for ix in idx:
            if len(ix) != t.ndim or any(not 0 <= v < s for v, s in zip(ix, t.shape)):
                raise WeightsError(f"The position {list(ix)} is outside {tensor}, whose shape is {list(t.shape)}.")
        flat = np.array([int(np.ravel_multi_index(ix, t.shape)) for ix in idx], dtype=np.int64)
        if len(set(flat.tolist())) != len(flat):
            raise WeightsError("The same position is named twice in this edit.")
    elif req.get("neurons") not in (None, "") or req.get("neuron") not in (None, ""):
        js = _rows(req.get("neurons") if req.get("neurons") not in (None, "") else [req["neuron"]], t.shape[0], "neurons in this layer")
        flat = (np.array(js)[:, None] * t.shape[1] + np.arange(t.shape[1])[None, :]).reshape(-1) if kind == "weight" else np.array(js)
    else:
        flat = np.arange(t.size)
    old = t.reshape(-1)[flat].astype(float)
    if op == "zero":
        return [(tensor, flat, np.zeros(len(flat)))]
    if op == "scale":
        return [(tensor, flat, old * num("factor"))]
    vals = req.get("values") if req.get("values") is not None else req.get("value")
    try:
        v = np.asarray(vals, dtype=float).reshape(-1)
    except (TypeError, ValueError):
        raise WeightsError("`values` is a list of numbers, one per position.") from None
    if vals is None or v.size not in (1, len(flat)) or not np.isfinite(v).all():
        raise WeightsError(f"This edit names {_plural(len(flat), 'position')} and needs that many finite values, or one value for all of them.")
    v = np.broadcast_to(v, (len(flat),)).astype(float)
    return [(tensor, flat, v if op == "set" else old + v)]


def _changes(st: State, patch: dict, frm: str, to: str, cap: int = LIST_CAP) -> tuple[list[dict], dict]:
    """A patch as the list a screen shows (layer, neuron, incoming weight or None for the bias, old, new), and what
    it touched."""
    changes, neurons, weights = [], [], []
    seen = set()
    for part in patch["parts"]:
        k, kind = st.where(part["tensor"])
        n_in = st.base[part["tensor"]].shape[1] if kind == "weight" else 1
        for f, a, b in zip(part["indices"], part[frm], part[to]):
            j, i = (divmod(int(f), n_in) if kind == "weight" else (int(f), None))
            if (k, j) not in seen and len(neurons) < LIST_CAP:
                seen.add((k, j))
                neurons.append([k + 1, j])
            if i is not None and len(weights) < LIST_CAP:
                weights.append([k, j, i])
            if len(changes) < cap:
                changes.append({"tensor": part["tensor"], "k": k, "j": j, "i": i, "old": a, "new": b})
    return changes, {"neurons": neurons, "weights": weights}


def _overlap(a: dict, b: dict) -> bool:
    for pa in a["parts"]:
        for pb in b["parts"]:
            if pa["tensor"] == pb["tensor"] and not set(pa["indices"]).isdisjoint(pb["indices"]):
                return True
    return False


def _where_words(st: State, touched: dict, tensors: list[str]) -> str:
    ns = touched["neurons"]
    if len(ns) == 1:
        return f"{' and '.join(tensors)} (layer {ns[0][0]}, neuron {ns[0][1] + 1})"
    return " and ".join(tensors)


def _cost_words(cost: dict) -> str:
    if "target" in cost:
        return f"Cost on the target went from {_g(cost['target']['before'])} to {_g(cost['target']['after'])}."
    if "test" in cost:
        return f"Cost on the held-out set went from {_g(cost['test']['before'])} to {_g(cost['test']['after'])}."
    return ""


def _first_cost(cost: dict, key: str):
    for k in ("target", "test", "keep"):
        if k in cost:
            return cost[k][key]
    return None


def _predicted(st: State, before: N.Network, parts: list[dict], cost: dict) -> dict | None:
    """What the gradient said this edit would do to the cost on the target, beside what it did. Only for a session
    that has already computed gradients: this never computes them behind the record's back. A large gap means the
    edit was not small."""
    if not st.doc["used"]["gradients_computed"] or "target" not in cost:
        return None
    got = _resolve(st, "target")
    if got is None or len(got[0]) == 0:
        return None
    g = N.backprop(before, got[0], got[1])
    by_tensor = {}
    for ly, gl in zip(st.manifest["layers"], g["layers"]):
        by_tensor[ly["weight"]], by_tensor[ly["bias"]] = gl["weight"].reshape(-1), gl["bias"].reshape(-1)
    change = 0.0
    for p in parts:
        idx = np.asarray(p["indices"], dtype=np.int64)
        change += float((by_tensor[p["tensor"]][idx] * (np.asarray(p["new"]) - np.asarray(p["old"]))).sum())
    actual = cost["target"]["after"] - cost["target"]["before"]
    return {"on": "target", "change": change, "after": cost["target"]["before"] + change, "actual_change": actual,
            "gap": actual - change}


@_by_kind
def edit(ctx, sid: str, req: dict, by: str | None = None) -> dict:
    """Change numbers. The edit is stored as a patch and measured on the three sets just before and just after.

    `req` is {op, ...}: see docs/weights.md. Returns the event that records the edit."""
    if not isinstance(req, dict):
        raise WeightsError("An edit is an object with `op` and what to change.")
    by = by if by is not None else req.get("by")

    def fn(st):
        st.open_for_changes()
        who = _by(by, st.who)
        why = str(req.get("why") or req.get("hypothesis") or "").strip()
        if who == "agent" and not why:
            raise WeightsError("An agent's edit needs `why`: one sentence saying what it expects the edit to do, given before the edit.")
        op = str(req.get("op") or "").replace("-", "_")
        run = st.program
        if run and who == "agent" and len(st.doc["edits"]) - run["edits_at_start"] >= run["max_edits"]:
            raise Refused(f"This run may make {_plural(run['max_edits'], 'edit')} and has made them. Take one back, or stop and report.")
        named = None
        if isinstance(req.get("target"), dict):
            # "Fix this one": the inputs and the wanted output, given whole. It becomes the session's target set.
            named = _check_spec(st, req["target"], "target")
            if "inputs" not in named:
                raise WeightsError("A target given with an edit is its inputs and its wanted output: {\"inputs\": [...], \"targets\": [...]}.")
        plan = _plan(st, req)
        if named is not None:
            st.doc["sets"]["target"] = named
        parts = []
        for tensor, flat, new in plan:
            cur = st.cur[tensor].reshape(-1)
            new32 = np.asarray(new, dtype=np.float32)
            if not np.isfinite(new32).all():
                raise WeightsError("This edit would write a number that is not finite.")
            flat = np.asarray(flat, dtype=np.int64)
            moved = cur[flat] != new32
            if moved.any():
                parts.append({"tensor": tensor, "indices": flat[moved].tolist(), "old": cur[flat][moved].astype(float).tolist(),
                              "new": new32[moved].astype(float).tolist()})
        if not parts:
            raise WeightsError("This edit changes nothing: every number it names already has that value.")
        before = st.net().copy()
        n = len(st.doc["edits"]) + 1
        key = f"patches/{n}-{secrets.token_hex(3)}.json"
        patch = {"n": n, "parts": parts}
        st.new_patches[key] = patch
        st._put(patch, "new")
        cost = _three(st, before, st.net())
        predicted = _predicted(st, before, parts, cost)
        changed = int(sum(len(p["indices"]) for p in parts))
        changes, touched = _changes(st, patch, "old", "new")
        tensors = [p["tensor"] for p in parts]
        if op == "step":
            st.doc["used"]["training_steps"] += 1
        verb = {"set": "set", "scale": "scaled", "zero": "set to zero", "add": "added to", "add_direction": "added a direction to",
                "step": "took one training step, which moved"}[op]
        ev = st.event("edit", f"{_Who(st, by)} {verb} {_plural(changed, 'number')} in {_where_words(st, touched, tensors)}.",
                      by, score_sentence=_cost_words(cost), op=op.replace("_", "-"), tensor=tensors[0], tensors=tensors,
                      layer=touched["neurons"][0][0] if touched["neurons"] else None,
                      neuron=touched["neurons"][0][1] if len(touched["neurons"]) == 1 else None,
                      factor=float(req["factor"]) if op == "scale" else None, why=why,
                      args={k: v for k, v in req.items() if k not in ("why", "hypothesis", "by", "direction", "values", "inputs", "targets", "target")},
                      **({"target_named": {"of": len(named["inputs"])}} if named is not None else {}),
                      changes=changes, changed=changed, share=round(changed / st.total(), 6), touched=touched, cost=cost,
                      cost_before=_first_cost(cost, "before"), cost_after=_first_cost(cost, "after"), undone=False,
                      **({"predicted": predicted} if predicted else {}))
        st.doc["edits"].append({"n": n, "event": ev["id"], "op": op.replace("_", "-"), "tensors": tensors, "why": why, "by": ev["by"],
                                "state": "applied", "changed": changed, "patch": key})
        st.doc["actions"].append(["apply", n])
        ev["ledger"] = {"changed": st.changed(), "total": st.total()}
        if cost and st.scores_visible:
            st.saw_score(ev, "edit")
        return st.show(ev)
    return _do(ctx, sid, fn)


def _pick(st: State, edit_id, want: str) -> dict:
    eds = st.doc["edits"]
    if edit_id in (None, ""):
        if want == "applied":
            for kind, n in reversed(st.doc["actions"]):
                if eds[n - 1]["state"] == "applied":
                    return eds[n - 1]
            raise WeightsError("There is no edit to take back.")
        for kind, n in reversed(st.doc["actions"]):
            if kind == "undo" and eds[n - 1]["state"] == "undone":
                return eds[n - 1]
        raise WeightsError("There is no edit to put back.")
    for e in eds:
        if edit_id in (e["event"], e["n"], str(e["n"])):
            if e["state"] != want:
                raise WeightsError(f"Edit {e['event']} is already taken back." if want == "applied" else f"Edit {e['event']} is not taken back, so there is nothing to put back.")
            return e
    raise WeightsError(f"This session has no edit {edit_id!r}.")


def _flip(ctx, sid: str, edit_id, by, kind: str) -> dict:
    def fn(st):
        st.open_for_changes()
        e = _pick(st, edit_id, "applied" if kind == "undo" else "undone")
        patch = st.patch(e["n"])
        if kind == "undo":
            later = [o for o in st.doc["edits"] if o["state"] == "applied" and o is not e
                     and _last_apply(st, o["n"]) > _last_apply(st, e["n"]) and _overlap(st.patch(o["n"]), patch)]
            if later:
                raise WeightsError(f"Take back edit {later[0]['event']} first: it changed the same numbers after edit {e['event']}.")
        else:
            for part in patch["parts"]:
                now = st.cur[part["tensor"]].reshape(-1)[np.asarray(part["indices"], dtype=np.int64)]
                if not np.array_equal(now, np.asarray(part["old"], dtype=np.float32)):
                    raise WeightsError(f"Edit {e['event']} cannot be put back: the numbers it started from have changed since.")
        before = st.net().copy()
        frm, to = ("new", "old") if kind == "undo" else ("old", "new")
        st._put(patch, to)
        e["state"] = "undone" if kind == "undo" else "applied"
        st.doc["actions"].append([kind, e["n"]])
        for ev in st.doc["events"]:
            if ev["id"] == e["event"]:
                ev["undone"] = kind == "undo"
        cost = _three(st, before, st.net())
        changes, touched = _changes(st, patch, frm, to)
        words = "took back" if kind == "undo" else "put back"
        ev = st.event(kind, f"{_Who(st, by)} {words} edit {e['event']}: {_plural(e['changed'], 'number')} in "
                            f"{_where_words(st, touched, e['tensors'])}.", by, score_sentence=_cost_words(cost), op=e["op"],
                      **{"undoes" if kind == "undo" else "redoes": e["event"]}, tensor=e["tensors"][0], tensors=e["tensors"],
                      layer=touched["neurons"][0][0] if touched["neurons"] else None,
                      neuron=touched["neurons"][0][1] if len(touched["neurons"]) == 1 else None,
                      changes=changes, changed=e["changed"], touched=touched, cost=cost,
                      cost_before=_first_cost(cost, "before"), cost_after=_first_cost(cost, "after"))
        ev["ledger"] = {"changed": st.changed(), "total": st.total()}
        if cost and st.scores_visible:
            st.saw_score(ev, kind)
        return st.show(ev)
    return _do(ctx, sid, fn)


def _last_apply(st: State, n: int) -> int:
    return max(i for i, (kind, m) in enumerate(st.doc["actions"]) if m == n and kind in ("apply", "redo"))


@_by_kind
def undo(ctx, sid: str, edit_id=None, by: str | None = None) -> dict:
    """Take an edit back: the last one still applied, or the one named. The exact old numbers return. The edit stays
    in the record, marked as taken back."""
    return _flip(ctx, sid, edit_id, by, "undo")


@_by_kind
def redo(ctx, sid: str, edit_id=None, by: str | None = None) -> dict:
    """Put back an edit that was taken back: the one taken back last, or the one named."""
    return _flip(ctx, sid, edit_id, by, "redo")


# ---- notes, and a run Captain starts ----------------------------------------------------------------------------------------

def note(ctx, sid: str, text: str, by: str | None = None) -> dict:
    """Put a thought in the record: a hypothesis before an edit, a conclusion after. It shows on the live feed."""
    text = " ".join(str(text or "").split())
    if not text:
        raise WeightsError("A note needs some words.")

    def fn(st):
        who = _by(by, st.who)
        lead = "The agent thinks" if who == "agent" else "Captain notes" if who == "captain" else "You noted"
        return st.show(st.event("note", f"{lead}: {text[:1000]}", by, op="note", text=text[:1000]))
    return _do(ctx, sid, fn)


def program_begin(ctx, sid: str, info: dict) -> dict:
    """Captain is starting an agent on this session. The record says which agent, its goal, its edit cap and whether
    the agent's own command line could be held to `cst weights` commands."""
    def fn(st):
        st.open_for_changes()
        if st.program:
            raise WeightsError("An agent is already working in this session. Stop it first.")
        st.doc["program"] = {**info, "active": True, "edits_at_start": len(st.doc["edits"]), "actions_at_start": len(st.doc["actions"]),
                             "started_at": WS.now()}
        limits = "held to cst weights commands" if info.get("restricted") else "not held to cst weights commands by its own settings"
        ev = st.event("program", f"Captain started the agent {info['agent']} with the goal: {info['goal']} It may make "
                                 f"{_plural(info['max_edits'], 'edit')} and is {limits}.", "captain", op="start",
                      program={k: info.get(k) for k in ("agent", "goal", "max_edits", "restricted", "restriction", "time_limit_s")})
        st.doc["program"]["seq"] = ev["seq"]
        return st.show(ev)
    return _do(ctx, sid, fn)


def _start_net(st: State) -> N.Network:
    """The network as it was when the run began: the base plus the edits that were already there."""
    run = st.doc.get("program") or {}
    t = {k: v.copy() for k, v in st.base.items()}
    for kind, n in st.doc["actions"][:run.get("actions_at_start", 0)]:
        for part in st.patch(n)["parts"]:
            t[part["tensor"]].reshape(-1)[np.asarray(part["indices"], dtype=np.int64)] = np.asarray(part["old" if kind == "undo" else "new"], dtype=np.float32)
    return F.network_from(st.manifest, t)


def program_scores(ctx, sid: str) -> dict:
    """For Captain's verdict: the three sets, the network when the run began against the network now, and what the
    run changed. Not an event and not shown to the agent: Captain reads it after the agent has stopped."""
    def fn(st):
        run = st.doc.get("program")
        if not run:
            raise WeightsError("No agent run was started in this session.")
        start, now = _start_net(st), st.net()
        mine = [e for e in st.doc["edits"] if e["n"] > run["edits_at_start"]]
        moved = int(sum(int((a != b).sum()) for a, b in zip(start.tensors().values(), now.tensors().values())))
        return {"scores": _three(st, start, now), "edits": [{"id": e["event"], "n": e["n"], "op": e["op"], "why": e.get("why") or "",
                                                           "state": e["state"], "changed": e["changed"]} for e in mine],
                "changed": moved, "total": st.total(), "share": round(moved / st.total(), 6), "ledger": ledger(st),
                "notes": [e["text"] for e in st.doc["events"] if e["kind"] == "note" and e["seq"] > run.get("seq", 0)],
                "sentences": {e["id"]: e["sentence"] for e in st.doc["events"] if e["kind"] == "edit" and e["seq"] > run.get("seq", 0)},
                "refused": [e["sentence"] for e in st.doc["events"] if e["kind"] == "refused" and e["seq"] > run.get("seq", 0)]}
    return _do(ctx, sid, fn, watch="record")


def program_undo(ctx, sid: str) -> int:
    """Take back, whole, every edit the run made that is still applied. Returns how many were taken back."""
    done = 0
    while True:
        st = _load(ctx, sid)
        run = st.doc.get("program") or {}
        left = [e for e in st.doc["edits"] if e["n"] > run.get("edits_at_start", 0) and e["state"] == "applied"]
        if not left:
            return done
        last = max(left, key=lambda e: _last_apply(st, e["n"]))
        undo(ctx, sid, last["event"], by="captain")
        done += 1


def program_end(ctx, sid: str, verdict: dict) -> dict:
    """The run is over. Captain's verdict goes in the record: kept or taken back, and why."""
    def fn(st):
        run = st.doc.get("program")
        if not run:
            raise WeightsError("No agent run was started in this session.")
        run["active"] = False
        run["ended_at"] = WS.now()
        run["verdict"] = {k: verdict.get(k) for k in ("kept", "reason", "stopped", "timed_out", "agent_exit")}
        scores = verdict.get("scores") or {}
        return st.show(st.event("program", f"Captain's verdict: {verdict['reason']}", "captain", score_sentence=_cost_words(scores),
                                op="end", kept=bool(verdict.get("kept")), cost=scores or None,
                                cost_before=_first_cost(scores, "before"), cost_after=_first_cost(scores, "after")))
    return _do(ctx, sid, fn)


# ---- diff, save, export -----------------------------------------------------------------------------------------------------

def _diff_tensors(st_where, base: dict, cur: dict, listed: int = LIST_CAP) -> dict:
    by_tensor, changes = [], []
    changed = total = 0
    for name in base:
        b, c = base[name], cur[name]
        moved = b != c
        n = int(moved.sum())
        total += int(b.size)
        changed += n
        if not n:
            continue
        by_tensor.append({"tensor": name, "changed": n, "total": int(b.size), "share": round(n / b.size, 6),
                          "largest_change": float(np.abs(c.astype(float) - b.astype(float)).max())})
        k, kind = st_where(name)
        for f in np.flatnonzero(moved.reshape(-1)):
            if len(changes) >= listed:
                break
            j, i = (divmod(int(f), b.shape[1]) if kind == "weight" else (int(f), None))
            changes.append({"tensor": name, "k": k, "j": j, "i": i, "old": float(b.reshape(-1)[f]), "new": float(c.reshape(-1)[f])})
    return {"changed": changed, "total": total, "share": round(changed / total, 6) if total else 0.0, "by_tensor": by_tensor,
            "changes": changes}


def _diff(st: State, listed: int = LIST_CAP) -> dict:
    return {"base": WS.ref_of(st.model, st.doc["version"]), **_diff_tensors(st.where, st.base, st.cur, listed)}


@_by_kind
def diff(ctx, sid: str, by: str | None = None, watch: bool = False) -> dict:
    """What differs between the network now and its base, counted from the tensors themselves."""
    def fn(st):
        out = _diff(st)
        if not st.watch:
            st.event("read", f"{_Who(st, by)} compared the network with its base: {out['changed']:,} of {out['total']:,} numbers differ.", by, op="diff")
        return out
    return _do(ctx, sid, fn, watch)


SEPARATE = ("These two networks were trained separately, so a weight-by-weight difference between them means little: the same "
            "network can be written with its neurons in another order. Diff is offered only between a network and its own edits.")


def compare(ctx, a: str, b: str) -> dict:
    """What differs between two versions of one line of edits. Two networks trained separately are refused."""
    an, av = WS.split_ref(a)
    bn, bv = WS.split_ref(b)
    ma, mb = WS.manifest(ctx, an, av), WS.manifest(ctx, bn, bv)
    if WS.root_of(ctx, an, ma["version"]) != WS.root_of(ctx, bn, mb["version"]):
        raise WeightsError(SEPARATE)
    m, ta = WS.load(ctx, an, ma["version"])
    _, tb = WS.load(ctx, bn, mb["version"])

    def where(tensor):
        for k, ly in enumerate(m["layers"]):
            for kind in ("weight", "bias"):
                if ly[kind] == tensor:
                    return k, kind
        return 0, "bias"
    return {"a": WS.ref_of(an, ma["version"]), "b": WS.ref_of(bn, mb["version"]), **_diff_tensors(where, ta, tb)}


@_by_kind
def save(ctx, sid: str, note: str = "", by: str | None = None) -> dict:
    """Save the network as it is now as a new version: the base plus patches, with this session's record attached.
    Nothing is overwritten."""
    def fn(st):
        patches = []
        for name in st.base:
            moved = np.flatnonzero((st.base[name] != st.cur[name]).reshape(-1))
            if len(moved):
                patches.append({"tensor": name, "indices": moved.tolist(), "old": st.base[name].reshape(-1)[moved].astype(float).tolist(),
                                "new": st.cur[name].reshape(-1)[moved].astype(float).tolist()})
        if not patches:
            raise WeightsError("There is nothing to save: the network is the same as its base.")
        was_blind = not st.scores_visible
        led = ledger(st)     # the label as it stood when the work ended, before any score is shown
        record = {"session": st.sid, "who": st.doc["who"], "opened_at": st.doc["opened_at"], "ledger": led, "edits": _edits_out(st),
                  "passes": st.doc["used"]["passes"], "sets": _sets_out(st),
                  "events": [{k: v for k, v in e.items() if k not in ("changes", "result")} for e in st.doc["events"]]}
        m = WS.save_patched(ctx, st.model, st.doc["version"], patches, st.cur, record, note=str(note or ""))
        ref = WS.ref_of(st.model, m["version"])
        st.doc["saved"].append(ref)
        applied = sum(1 for e in st.doc["edits"] if e["state"] == "applied")
        ev = st.event("save", f"{_Who(st, by)} saved the network as {ref}: {WS.ref_of(st.model, st.doc['version'])} plus "
                              f"{_plural(led['changed'], 'changed number')}.", by, op="save", ref=ref, args={"note": note})
        if was_blind:
            st.doc["revealed"] = True    # the work is saved: its scores can be shown now
            st.saw_score(ev, "save")
        return {"model": st.model, "version": m["version"], "ref": ref, "base": WS.ref_of(st.model, st.doc["version"]),
                "patches": applied, "changed": led["changed"], "share": led["share"], "label": led["label"],
                "weights_alone": led["weights_alone"], "note": str(note or ""), "scores_shown_now": was_blind}
    return _do(ctx, sid, fn)


def close(ctx, sid: str, by: str | None = None) -> dict:
    """End a session. Nothing more can be run or changed in it. A blind session's scores are shown from here on."""
    def fn(st):
        st.open_for_changes()
        was_blind = not st.scores_visible
        ev = st.event("close", f"{_Who(st, by)} closed the session.", by, op="close")
        st.doc["closed"] = True
        if was_blind:
            st.doc["revealed"] = True
            st.saw_score(ev, "close")
        return _session_out(st)
    return _do(ctx, sid, fn)


@_by_kind
def export(ctx, sid: str, folder: str, file: str = "tensors.safetensors", by: str | None = None) -> dict:
    """Write the network as it is now into a folder, for code to work on. Only a session that was allowed a sandbox
    may do this, and doing it is recorded."""
    def fn(st):
        st.open_for_changes()
        if not st.doc["allowed"]["sandbox"]:
            raise Refused("This session was opened without a sandbox, so its tensors cannot be written out for code to work on.")
        m = {k: v for k, v in st.manifest.items() if k not in ("record", "patches", "base", "sha256")}
        m["tensors"] = {k: {"file": file, "shape": list(t.shape), "dtype": "float32"} for k, t in st.cur.items()}
        os.makedirs(folder, exist_ok=True)
        for fname, body in F.pack(m, st.cur).items():
            with open(os.path.join(folder, fname), "wb") as fh:
                fh.write(body)
        st.doc["used"]["sandbox_used"] = True
        st.event("export", f"{_Who(st, by)} wrote the tensors out for code to work on.", by, op="export")
        return {"folder": folder, "files": sorted(F.pack(m, st.cur)), "sandbox_used": True}
    return _do(ctx, sid, fn)


def whole(ctx, model: str, sid: str | None = None) -> dict:
    """A network for a screen. With no session: its description only (sizes, what inputs and outputs mean, its
    examples) and no numbers. With a session: every number as that session has edited them, counted in the
    session's record as a look by the screen."""
    from . import real as R

    other = R.whole(ctx, model, sid)    # a trained session model answers with its parts, and no numbers
    if other is not None:
        return other
    if sid:
        def fn(st):
            out = WS.whole(st.manifest, st.cur, ctx)
            out["session"] = st.sid
            out["edits"] = len(st.doc["actions"])
            st.looked = st.total()
            return out
        return _do(ctx, sid, fn, watch=True)
    name, version = WS.split_ref(model)
    m, tensors = WS.load(ctx, name, version)
    return WS.whole(m, tensors, ctx, numbers=False)
