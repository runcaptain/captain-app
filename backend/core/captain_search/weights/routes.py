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


@router.post("/sessions/{sid}/close")
def close(sid: str, body: dict | None = None):
    return S.close(_ctx(), sid, _body(body).get("by"))


@router.post("/sessions/{sid}/export")
def export(sid: str, body: dict):
    b = _body(body)
    if not b.get("folder"):
        raise S.WeightsError("Name the folder to write the tensors into.")
    return S.export(_ctx(), sid, str(b["folder"]), str(b.get("file") or "tensors.safetensors"), b.get("by"))
