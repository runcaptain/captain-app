"""The weights operations as routes under /api/weights. Shapes are in docs/weights.md.

Every route is a thin call into session.py or store.py. `watch=1` on a read of the weights is a screen showing a
session to a person looking on: it is not an event and not held to the session's allowances, and it is counted
beside the record as a look by the screen. Nothing that runs the network takes it.
"""
from __future__ import annotations

from fastapi import APIRouter

from . import session as S
from . import store as WS

router = APIRouter(prefix="/api/weights")


def _ctx():
    from .. import server

    return server.ctx()


def _body(body) -> dict:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise S.WeightsError("The request body is a JSON object.")
    return body


# ---- models -----------------------------------------------------------------------------------------------------------

@router.get("/models")
def models(project: str | None = None):
    return WS.models(_ctx(), project)


@router.get("/models/{model}")
def model(model: str, session: str | None = None):
    return S.whole(_ctx(), model, session)


@router.get("/models/{model}/session")
def model_session(model: str):
    return S.session_for(_ctx(), model)


@router.get("/models/{model}/sessions")
def model_sessions(model: str):
    return S.sessions(_ctx(), model)


@router.post("/models/{model}/project")
def model_project(model: str, body: dict):
    b = _body(body)
    if not b.get("project"):
        raise S.WeightsError("Name the project to put this model in.")
    return WS.assign(_ctx(), WS.split_ref(model)[0], str(b["project"]))


@router.post("/import")
def import_model(body: dict):
    b = _body(body)
    if not b.get("folder"):
        raise S.WeightsError("Name the folder that holds the model's manifest and tensors.")
    import os

    from . import pipeline as WP

    folder = os.path.abspath(str(b["folder"]))
    return WP.collect(_ctx(), folder, folder, b.get("name"))


@router.get("/compare")
def compare(a: str, b: str):
    return S.compare(_ctx(), a, b)


# ---- sessions ---------------------------------------------------------------------------------------------------------

@router.post("/sessions")
def open_session(body: dict):
    b = _body(body)
    if not b.get("model"):
        raise S.WeightsError("Name the model to open.")
    return S.open_session(_ctx(), str(b["model"]), by=b.get("by") or "you", name=b.get("name"), raw_weights=b.get("raw_weights"),
                          sandbox=b.get("sandbox"), forward_passes=b.get("forward_passes"), pass_inputs=b.get("pass_inputs"),
                          feedback=b.get("feedback"))


@router.get("/sessions/{sid}")
def session(sid: str):
    return S.session(_ctx(), sid)


@router.get("/sessions/{sid}/report")
def report(sid: str):
    return S.report(_ctx(), sid)


@router.get("/sessions/{sid}/events")
def events(sid: str, since: int = 0):
    return S.events(_ctx(), sid, since)


@router.get("/sessions/{sid}/layers")
def layers(sid: str, by: str | None = None, watch: bool = False):
    return S.layers(_ctx(), sid, by, watch)


@router.get("/sessions/{sid}/read")
def read(sid: str, tensor: str, rows: str | None = None, cols: str | None = None, by: str | None = None, watch: bool = False):
    return S.read(_ctx(), sid, tensor, rows, cols, by, watch)


@router.get("/sessions/{sid}/stats")
def stats(sid: str, tensor: str | None = None, neurons: bool = False, by: str | None = None, watch: bool = False):
    return S.stats(_ctx(), sid, tensor, neurons, by, watch)


@router.get("/sessions/{sid}/heatmap")
def heatmap(sid: str, layer: int = 0, neuron: str | None = None, by: str | None = None, watch: bool = False):
    return S.heatmap(_ctx(), sid, layer, neuron, by, watch)


@router.get("/sessions/{sid}/template")
def template(sid: str, output: int = 0, by: str | None = None, watch: bool = False):
    return S.template(_ctx(), sid, output, by, watch)


@router.get("/sessions/{sid}/diff")
def diff(sid: str, by: str | None = None, watch: bool = False):
    return S.diff(_ctx(), sid, by, watch)


def _run(fn, sid: str, body, **more):
    b = _body(body)
    return fn(_ctx(), sid, inputs=b.get("inputs"), targets=b.get("targets"), example=b.get("example"), by=b.get("by"),
              **{k: b.get(k) for k in more})


@router.post("/sessions/{sid}/forward")
def forward(sid: str, body: dict):
    return _run(S.forward, sid, body)


@router.post("/sessions/{sid}/activations")
def activations(sid: str, body: dict):
    return _run(S.activations, sid, body)


@router.post("/sessions/{sid}/gradients")
def gradients(sid: str, body: dict):
    return _run(S.gradients, sid, body, layer=None, neuron=None)


@router.post("/sessions/{sid}/trace")
def trace(sid: str, body: dict):
    return _run(S.trace, sid, body, path=None)


@router.post("/sessions/{sid}/slice")
def cost_slice(sid: str, body: dict):
    b = _body(body)
    if not b.get("tensor") or b.get("index") is None:
        raise S.WeightsError("A slice names one number: `tensor` and `index`.")
    return S.cost_slice(_ctx(), sid, str(b["tensor"]), b["index"], inputs=b.get("inputs"), targets=b.get("targets"),
                        example=b.get("example"), span=b.get("span"), points=int(b.get("points") or 21), by=b.get("by"))


@router.post("/sessions/{sid}/sets")
def sets(sid: str, body: dict):
    b = _body(body)
    return S.set_sets(_ctx(), sid, b.get("target"), b.get("should_not_change"), b.get("by"))


@router.post("/sessions/{sid}/edits")
def edits(sid: str, body: dict):
    return S.edit(_ctx(), sid, _body(body))


@router.post("/sessions/{sid}/undo")
def undo(sid: str, body: dict | None = None):
    b = _body(body)
    return S.undo(_ctx(), sid, b.get("edit"), b.get("by"))


@router.post("/sessions/{sid}/redo")
def redo(sid: str, body: dict | None = None):
    b = _body(body)
    return S.redo(_ctx(), sid, b.get("edit"), b.get("by"))


@router.post("/sessions/{sid}/evaluate")
def evaluate(sid: str, body: dict | None = None):
    return S.evaluate(_ctx(), sid, _body(body).get("by"))


@router.post("/sessions/{sid}/save")
def save(sid: str, body: dict | None = None):
    b = _body(body)
    return S.save(_ctx(), sid, str(b.get("note") or ""), b.get("by"))


@router.post("/sessions/{sid}/note")
def note(sid: str, body: dict):
    b = _body(body)
    return S.note(_ctx(), sid, str(b.get("text") or ""), b.get("by"))


@router.post("/sessions/{sid}/done")
def done(sid: str, body: dict):
    b = _body(body)
    return S.done(_ctx(), sid, str(b.get("text") or ""), b.get("by"))


@router.post("/sessions/{sid}/program")
def program(sid: str, body: dict):
    """Start an agent on this session with a goal. Returns a job: {job, session, agent}. The job's result is the
    report. The run tells `jobs`, `job:<id>` and `weights-session:<id>` as it goes.

    A result Captain keeps stays in the session and is NOT saved as a new version, unless the body says
    `"save": true`: from a page, nothing is saved until the person saves it."""
    from .. import server
    from . import program as WPG

    b = _body(body)
    c = _ctx()
    plan = WPG.prepare(c, None, str(b.get("goal") or ""), session=sid, agent=b.get("agent"), max_edits=int(b.get("max_edits") or 5),
                       target_example=b.get("target_example"), target_input=b.get("target_input"), target_output=b.get("target_output"),
                       should_not_change=b.get("should_not_change"), time_limit_s=int(b.get("time_limit_s") or WPG.TIME_LIMIT_S),
                       dry_run=bool(b.get("dry_run")))
    if b.get("dry_run"):
        return plan
    keep = b.get("keep")

    box: dict = {}

    def say(text: str) -> None:
        job = server.JOBS.get(box.get("jid"))
        if job is not None:
            job["log"].append(text)
            job["message"] = text

    def work(progress):
        return WPG.run(c, plan, keep=keep if isinstance(keep, bool) else None, save=b.get("save") is True, say=say)

    jid = server._job(work, f"{plan['agent']} is working on {S.session(c, sid)['base']}: {plan['goal']}", topics=(f"weights-session:{sid}",))
    box["jid"] = jid
    return {"job": jid, "session": sid, "agent": plan["agent"], "goal": plan["goal"]}


@router.post("/sessions/{sid}/program/stop")
def program_stop(sid: str):
    """Stop the agent working in this session. Captain then gives its verdict on what was done so far."""
    from . import program as WPG

    if not WPG.stop(sid):
        raise S.WeightsError("No agent is working in this session.")
    return {"session": sid, "stopping": True}


@router.post("/sessions/{sid}/close")
def close(sid: str, body: dict | None = None):
    return S.close(_ctx(), sid, _body(body).get("by"))


@router.post("/sessions/{sid}/export")
def export(sid: str, body: dict):
    b = _body(body)
    if not b.get("folder"):
        raise S.WeightsError("Name the folder to write the tensors into.")
    return S.export(_ctx(), sid, str(b["folder"]), str(b.get("file") or "tensors.safetensors"), b.get("by"))
