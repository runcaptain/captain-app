"""A deployed model that is not a ranker: named inputs in, named outputs out, both described by its manifest.

Today that is a weights model (captain_search/weights): a small network kept as a manifest plus tensors, which a
person or an agent edits. It deploys through the same one-call deploy as a ranker and runs in the same runtime
(runtime.py), beside the ranking routes, which it does not touch.

  A version      `weights:<model>@v<N>`: how a project's deploy names a weights model version. With no version it
                 means the newest.
  The shape      `shape(manifest)`: what the model takes and what it gives back, read from the manifest alone.
  A request      POST /predict  {"inputs": {...}, request_id?, session_id?, traffic?}
                   {"inputs": {"input": [784 numbers]}}            every number, in order
                   {"inputs": {"input": [[28 numbers] x 28]}}      a grid, when the manifest gives the input a shape
                   {"inputs": {"x1": 0.9, "x2": 0.1}}              by name, when the manifest names each number
                   {"inputs": [0.9, 0.1]}                          short for the first form
  An answer      {"request_id", "outputs": {"scores": [...], "named": {name: score}, "chosen", "chosen_index"},
                  "arm", "model_version", "ms"}
                 `chosen` is the name of the largest score, when there is more than one output. A network with one
                 output between 0 and 1 answers `yes` (true above one half) instead.
  In the bundle  the version's whole line of edits: the manifest and tensors of the version it started from, and
                 every saved edit since, in order. The runtime applies them when it loads, with numpy alone, and
                 refuses to start if the result is not the numbers recorded when the version was saved.

A request that does not fit is refused with a sentence that says what was expected and what arrived.
"""
from __future__ import annotations

import json
import math

PREFIX = "weights:"
ENGINE = "weights"
FILES = "weights"          # the folder inside an arm of the bundle
LINE = "line.json"         # the version's line of edits, oldest first
SHOW = 8                   # how many numbers of a long input or output a log line carries
WHOLE = 32                 # an input or output up to this long is logged whole


class NotANetwork(ValueError):
    """The request was for a network and the deployment is a ranker, or the other way round."""


# ---- naming a version ----------------------------------------------------------------------------


def is_ref(ref) -> bool:
    return isinstance(ref, str) and ref.startswith(PREFIX)


def parse(ref: str) -> tuple[str, int | None]:
    """'weights:digits@v2' gives ('digits', 2). 'weights:digits' gives ('digits', None), the newest."""
    from ..weights import store as WS

    return WS.split_ref(str(ref)[len(PREFIX):])


def ref_of(name: str, version: int) -> str:
    return f"{PREFIX}{name}@v{int(version)}"


def resolve(ctx, ref: str) -> str:
    """The full name of a weights version, with its number. Refuses one that is not there, in a sentence."""
    from ..storage.backend import NotFound
    from ..weights import store as WS

    name, version = parse(ref)
    try:
        m = WS.manifest(ctx, name, version)
    except NotFound as e:
        raise ValueError(str(e)) from None
    if m.get("kind") not in (None, "dense"):
        from ..weights import real as R

        raise ValueError(R.NOT_YET)
    return ref_of(name, m["version"])


def short(ref: str | None) -> str:
    """A version as a person reads it: 'digits v2'."""
    if not is_ref(ref):
        return str(ref or "").split("/")[-1]
    return str(ref)[len(PREFIX):].replace("@", " ")


# ---- the shape: what goes in and what comes out ----------------------------------------------------


def shape(m: dict) -> dict:
    """What a model takes and gives back, from its manifest: {inputs: [...], outputs: [...], text}."""
    ins, outs = m.get("inputs") or {}, m.get("outputs") or {}
    n_in, n_out = int(m["sizes"][0]), int(m["sizes"][-1])
    grid = [int(s) for s in ins["shape"]] if ins.get("shape") and math.prod(int(s) for s in ins["shape"]) == n_in else None
    in_names = [str(n) for n in ins["names"]] if isinstance(ins.get("names"), list) and len(ins["names"]) == n_in else None
    out_names = [str(n) for n in outs["names"]] if isinstance(outs.get("names"), list) and len(outs["names"]) == n_out else None
    last = (m.get("layers") or [{}])[-1].get("activation")
    one = {"name": str(ins.get("name") or "input"), "type": "numbers", "count": n_in, "shape": grid, "names": in_names,
           "meaning": ins.get("meaning") or "not stated",
           "accepts": [f"a list of {n_in} numbers"] + ([f"a grid of {' by '.join(str(s) for s in grid)} numbers"] if grid else [])
                      + (["one number for each name: " + ", ".join(in_names[:6]) + (" and so on" if len(in_names) > 6 else "")] if in_names else [])}
    scores = {"name": "scores", "type": "numbers", "count": n_out, "names": out_names, "meaning": outs.get("meaning") or "not stated"}
    answers = [scores]
    if out_names:
        answers.append({"name": "named", "type": "numbers by name", "meaning": "the same scores, each under its output's name"})
    if n_out > 1:
        answers += [{"name": "chosen", "type": "name", "meaning": "the output with the largest score"},
                    {"name": "chosen_index", "type": "number", "meaning": "its position, counted from 0"}]
    elif last == "sigmoid":
        answers.append({"name": "yes", "type": "true or false", "meaning": "true when the score is above one half"})
    what = (f"a grid of {' by '.join(str(s) for s in grid)} numbers" if grid else f"{n_in} number{'s' if n_in != 1 else ''}"
            + (f" ({', '.join(in_names)})" if in_names and n_in <= 6 else ""))
    back = f"{n_out} score{'s' if n_out != 1 else ''}" + (" and the one chosen" if n_out > 1 else "")
    return {"inputs": [one], "outputs": answers, "text": f"Takes {what}. Gives back {back}."}


def _refuse(text: str):
    from .runtime import Refused

    return Refused(text)


def _numbers(value, what: str) -> list[float]:
    out = []
    for i, v in enumerate(value):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise _refuse(f"{what} must be numbers. Number {i + 1} is {json.dumps(v, default=str)[:40]}.")
        if not math.isfinite(v):
            raise _refuse(f"{what} must be ordinary numbers. Number {i + 1} is not one.")
        out.append(float(v))
    return out


def read_inputs(sh: dict, body) -> list[float]:
    """The numbers a predict request carries, in the order the model takes them. Refuses anything else in a
    sentence that says what was expected and what arrived."""
    one = sh["inputs"][0]
    name, n, grid, names = one["name"], one["count"], one["shape"], one["names"]
    expects = "This model takes " + ", or ".join(one["accepts"]) + f', under "inputs". For example: {json.dumps(example_body(sh, None), separators=(",", ":"))[:120]}'
    if not isinstance(body, dict) or "inputs" not in body:
        raise _refuse(f'A predict request needs "inputs". {expects}')
    given = body["inputs"]
    if isinstance(given, dict):
        if name in given:
            extra = [k for k in given if k != name]
            if extra:
                raise _refuse(f'This model has one input, "{name}". The request also sent {", ".join(repr(str(k)) for k in extra[:5])}.')
            given = given[name]
        elif names and any(k in names for k in given):
            unknown = [k for k in given if k not in names]
            if unknown:
                raise _refuse(f"This model has no input named {', '.join(repr(str(k)) for k in unknown[:5])}. Its inputs are: {', '.join(names[:12])}.")
            missing = [k for k in names if k not in given]
            if missing:
                raise _refuse(f"The request is missing {', '.join(missing[:8])}" + (f" and {len(missing) - 8} more" if len(missing) > 8 else "")
                              + f". This model needs all {n}: {', '.join(names[:12])}.")
            return _numbers([given[k] for k in names], "The inputs")
        else:
            raise _refuse(f'The request names {", ".join(repr(str(k)) for k in list(given)[:5]) or "no input"}. {expects}')
    if not isinstance(given, list):
        raise _refuse(f"The input must be a list of numbers. {expects}")
    if given and all(isinstance(r, list) for r in given):
        if not grid:
            raise _refuse(f"This model takes one list of {n} numbers, not rows of them.")
        if len(given) != grid[0] or any(len(r) != grid[1] for r in given) or len(grid) != 2:
            got = f"{len(given)} row{'s' if len(given) != 1 else ''}" + (f" of {len(given[0])}" if given and len({len(r) for r in given}) == 1 else " of uneven length")
            raise _refuse(f"This model takes a grid of {' by '.join(str(s) for s in grid)} numbers. The request sent {got}.")
        given = [v for r in given for v in r]
    if len(given) != n:
        raise _refuse(f"This model takes {n} number{'s' if n != 1 else ''}" + (f" (a grid of {' by '.join(str(s) for s in grid)})" if grid else "")
                      + f". The request sent {len(given)}.")
    return _numbers(given, "The inputs")


def outputs_of(sh: dict, scores: list[float]) -> dict:
    """The named outputs for one row of scores."""
    sc = sh["outputs"][0]
    out: dict = {"scores": [round(float(s), 6) for s in scores]}
    if sc["names"]:
        out["named"] = dict(zip(sc["names"], out["scores"]))
    kinds = {o["name"] for o in sh["outputs"]}
    if "chosen" in kinds:
        top = max(range(len(scores)), key=lambda i: scores[i])
        out["chosen"] = sc["names"][top] if sc["names"] else str(top)
        out["chosen_index"] = top
    elif "yes" in kinds:
        out["yes"] = bool(scores[0] > 0.5)
    return out


# ---- in a log line: short and readable -------------------------------------------------------------


def _g(x: float) -> str:
    return f"{x:.3g}"


def brief_inputs(sh: dict, xs: list[float]) -> dict:
    one = sh["inputs"][0]
    if len(xs) <= WHOLE:
        return {one["name"]: [round(x, 6) for x in xs], **({"names": one["names"]} if one["names"] else {})}
    return {one["name"]: {"count": len(xs), "shape": one["shape"], "min": round(min(xs), 6), "max": round(max(xs), 6),
                          "mean": round(sum(xs) / len(xs), 6), "not_zero": sum(1 for x in xs if x), "first": [round(x, 6) for x in xs[:SHOW]]}}


def brief_outputs(out: dict) -> dict:
    if len(out["scores"]) <= WHOLE:
        return out
    top = sorted(range(len(out["scores"])), key=lambda i: -out["scores"][i])[:SHOW]
    return {**{k: v for k, v in out.items() if k not in ("scores", "named")},
            "scores": {"count": len(out["scores"]), "top": [{"index": i, "score": out["scores"][i]} for i in top]}}


def sentence(sh: dict, xs: list[float], out: dict) -> str:
    """One request in a sentence: what went in and what came out."""
    one = sh["inputs"][0]
    if one["names"] and len(xs) <= 6:
        went = ", ".join(f"{n} {_g(x)}" for n, x in zip(one["names"], xs))
    elif one["shape"]:
        went = f"a {' by '.join(str(s) for s in one['shape'])} grid, {sum(1 for x in xs if x)} of {len(xs)} not zero"
    elif len(xs) <= 6:
        went = ", ".join(_g(x) for x in xs)
    else:
        went = f"{len(xs)} numbers from {_g(min(xs))} to {_g(max(xs))}"
    if "chosen" in out:
        came = f"chose {out['chosen']}, score {_g(out['scores'][out['chosen_index']])}"
    elif "yes" in out:
        name = (sh["outputs"][0]["names"] or ["the output"])[0]
        came = f"{name}: {'yes' if out['yes'] else 'no'}, score {_g(out['scores'][0])}"
    else:
        came = "scores " + ", ".join(_g(s) for s in out["scores"][:SHOW])
    return f"In: {went}. Out: {came}."


# ---- an example request ------------------------------------------------------------------------------


def example_values(ctx, m: dict) -> tuple[list[float], str]:
    """Numbers to try the model with: its first practice example when it has one, else every input at one half."""
    n = int(m["sizes"][0])
    if ctx is not None:
        try:
            from ..weights import store as WS

            got = WS.sets(ctx, m, "practice")
            if got is not None and len(got[0]):
                return [round(float(v), 4) for v in got[0][0].reshape(-1)], "the model's first practice example"
        except Exception:  # noqa: BLE001  a set that can't be read leaves the plain example
            pass
    return [0.5] * n, "every input at one half"


def example_body(sh: dict, xs: list[float] | None) -> dict:
    """A request body for these numbers, in the form that reads best: by name, as a grid, or as one list."""
    one = sh["inputs"][0]
    xs = xs if xs is not None else [0.5] * one["count"]
    if one["names"] and one["count"] <= 12:
        return {"inputs": dict(zip(one["names"], xs))}
    if one["shape"] and len(one["shape"]) == 2:
        rows, cols = one["shape"]
        return {"inputs": {one["name"]: [xs[r * cols:(r + 1) * cols] for r in range(rows)]}}
    return {"inputs": {one["name"]: xs}}


# ---- the bundle --------------------------------------------------------------------------------------


def line_of(ctx, ref: str) -> tuple[dict, list[dict]]:
    """A version's line of edits: (the manifest of the version that carries the tensors, [each version after it,
    oldest first, as {ref, patches, sha256}])."""
    from ..weights import store as WS

    name, version = parse(resolve(ctx, ref))
    steps = []
    m = WS.manifest(ctx, name, version)
    while m.get("base"):
        steps.append({"ref": WS.ref_of(name, m["version"]), "patches": m.get("patches") or [], "sha256": m.get("sha256")})
        name, version = WS.split_ref(m["base"])
        m = WS.manifest(ctx, name, version)
        if len(steps) > 1000:
            raise ValueError(f"{short(ref)} has a line of edits Captain cannot follow to its start.")
    return m, steps[::-1]


def carries(ctx, ref: str) -> dict:
    """What a deployment's record says a version is made of: the version it started from and the edits it carries."""
    from ..weights import store as WS

    name, version = parse(resolve(ctx, ref))
    m = WS.manifest(ctx, name, version)
    root, steps = line_of(ctx, ref)
    rec = m.get("record") or {}
    led = rec.get("ledger") or {}
    edits = [{k: e.get(k) for k in ("id", "op", "tensors", "why", "by", "changed") if e.get(k) is not None}
             for e in (rec.get("edits") or []) if e.get("state", "applied") == "applied"][:50]
    changed = sum(len(p.get("indices") or []) for s in steps for p in s["patches"])
    out = {"model": name, "version": int(m["version"]), "ref": ref_of(name, m["version"]),
           "base": m.get("base"), "started_from": WS.ref_of(root["name"], root["version"]),
           "line": [s["ref"] for s in steps], "numbers": int(m.get("parameters") or root.get("parameters") or 0),
           "changed": int(led.get("changed") if led.get("changed") is not None else changed) if m.get("base") else 0,
           "edits": edits, "note": m.get("note") or "", "label": led.get("label"),
           "session": rec.get("session"), "saved_by": m.get("created_by"), "saved_at": m.get("created_at"), "sha256": m.get("sha256")}
    out["text"] = (f"{short(out['ref'])} is {str(m['base']).replace('@', ' ')} plus {out['changed']:,} changed number{'s' if out['changed'] != 1 else ''}"
                   + (f" in {len(edits)} edit{'s' if len(edits) != 1 else ''}" if edits else "") + "."
                   if m.get("base") else f"{short(out['ref'])} is the network as it was trained or brought in. It carries no edits.")
    return out


def arm_spec(ctx, bundle_key: str, name: str, ref: str, store=None) -> dict:
    """Copy what one weights version needs into the bundle, and return its entry for bundle.json. It is loaded
    here first: a version that cannot be opened never reaches a host."""
    from ..weights import files as F
    from ..weights import store as WS

    ref = resolve(ctx, ref)
    mname, version = parse(ref)
    m, tensors = WS.load(ctx, mname, version)   # opens it, edits applied and checked
    F.network_from({**m, "tensors": m["tensors"]}, tensors)
    root, steps = line_of(ctx, ref)
    store = store or ctx.store

    def put(rel: str, data: bytes) -> None:
        from ..storage.backend import AlreadyExists

        try:
            store.put_new(f"{bundle_key}/{name}/{FILES}/{rel}", data)
        except AlreadyExists:
            pass

    for fname in F.tensor_files(root):
        put(fname, WS.read_file(ctx, root["name"], root["version"], fname))
    put(F.MANIFEST, json.dumps(root).encode())
    put(LINE, json.dumps({"ref": ref, "steps": steps, "sha256": WS.digest(tensors)}).encode())
    described = {k: m[k] for k in ("sizes", "layers", "inputs", "outputs", "cost", "parameters") if k in m}
    return {"pipeline": ref, "engine": ENGINE, "kind": ENGINE, "spec": {}, "catalog": None, "artifact": {},
            "files": f"{name}/{FILES}", "manifest": described, "shape": shape(m)}


class Net:
    """One arm of a bundle that answers /predict: a network, loaded from the bundle with numpy alone."""

    def __init__(self, name: str, spec: dict, net, sh: dict):
        self.name, self.pipeline, self.kind, self.net, self.shape = name, spec["pipeline"], ENGINE, net, sh
        self.names: list = []       # no ranking signals: what the request log's header reads
        self.importance: dict = {}
        self.engine = None

    def rank(self, query: str, k: int):
        raise NotANetwork(f"This deployment serves {short(self.pipeline)}, a network, not a search ranker. Send it POST /predict. {self.shape['text']}")

    def predict(self, xs: list[float]) -> list[float]:
        import numpy as np

        from ..weights import net as N

        return [float(v) for v in N.predict(self.net, np.asarray([xs], dtype=float))[0]]


def load(store, bundle_key: str, name: str, spec: dict) -> Net:
    """Open a weights arm from the bundle's own files: the tensors it started from, then every saved edit in order."""
    import numpy as np

    from ..weights import files as F
    from ..weights import store as WS

    base = f"{bundle_key}/{spec['files']}"
    root, tensors = F.unpack(lambda fname: store.get(f"{base}/{fname}"))
    tensors = {k: np.asarray(t, dtype="float32").copy() for k, t in tensors.items()}
    line = json.loads(store.get(f"{base}/{LINE}"))
    for step in line["steps"]:
        WS.apply_patches(tensors, step["patches"])
        if step.get("sha256") and WS.digest(tensors) != step["sha256"]:
            raise ValueError(f"{step['ref']} does not match the numbers recorded when it was saved. It was not loaded.")
    if line.get("sha256") and WS.digest(tensors) != line["sha256"]:
        raise ValueError(f"{short(spec['pipeline'])} does not match the numbers recorded when it was packaged. It was not loaded.")
    m = {**root, **(spec.get("manifest") or {})}
    return Net(name, spec, F.network_from({**m, "tensors": root["tensors"]}, tensors), spec.get("shape") or shape(m))


# ---- trying a deployed network ------------------------------------------------------------------------


def test_bodies(ctx, ref: str, n: int = 5) -> list[dict]:
    """Up to `n` requests to try a deployed network with: its own practice examples, or one plain example."""
    from ..weights import store as WS

    name, version = parse(resolve(ctx, ref))
    m = WS.manifest(ctx, name, version)
    if m.get("base"):
        m, _ = WS.load(ctx, name, version)
    sh = shape(m)
    try:
        got = WS.sets(ctx, m, "practice")
    except Exception:  # noqa: BLE001
        got = None
    if got is None or not len(got[0]):
        return [example_body(sh, example_values(None, m)[0])]
    return [example_body(sh, [round(float(v), 4) for v in row.reshape(-1)]) for row in got[0][:n]]


def check_served(ctx, pid: str, env: str, n: int = 5) -> dict:
    """Does the deployed copy answer as the saved version does? Sends the model's practice inputs through the
    deployment, as test traffic, and compares each score with the one this computer works out from the version
    in the workspace, edits applied. {status: pass | fail, compared, worst, text}."""
    import numpy as np

    from ..weights import files as F
    from ..weights import net as N
    from ..weights import store as WS
    from . import call, state

    rec = state(ctx, pid, env) or {}
    ref = rec.get("model_version")
    name, version = parse(ref)
    m, tensors = WS.load(ctx, name, version)
    net = F.network_from(m, tensors)
    sh = shape(m)
    worst, done = 0.0, 0
    for i, body in enumerate(test_bodies(ctx, ref, n)):
        xs = read_inputs(sh, body)
        want = N.predict(net, np.asarray([xs], dtype=float))[0]
        got = call(ctx, rec, "predict", {**body, "traffic": "test", "session_id": f"deploy-match-{i}"})["outputs"]["scores"]
        worst = max(worst, float(np.max(np.abs(np.asarray(got, dtype=float) - want))))
        done += 1
    ok = worst <= 1e-4
    return {"status": "pass" if ok else "fail", "compared": done, "worst": round(worst, 8),
            "text": (f"The deployed model gives the same answers as {short(ref)} on {done} practice input{'s' if done != 1 else ''}."
                     if ok else f"The deployed model does not answer as {short(ref)} does: on {done} practice input{'s' if done != 1 else ''} "
                                f"a score differs by up to {worst:.4g}. Deploy it again.")}


# ---- a project's networks: what it holds that can be deployed --------------------------------------------


def networks_of(ctx, pid: str) -> list[dict]:
    """The weights models a project holds, each with its versions as things that can be deployed. A project holds a
    weights model when the model was put in it (`model:weights:<name>`), which a deploy from the project does by
    itself for a model that is in no project. The model deployed from this project now is listed too."""
    from .. import project_state as PS
    from ..weights import store as WS

    try:
        rec = ((PS.get(ctx, pid).get("staging") or {}).get("staging")) or {}
    except Exception:  # noqa: BLE001
        rec = {}
    live = parse(rec["model_version"]) if rec.get("status") == "running" and is_ref(rec.get("model_version")) else (None, None)
    from .. import projects as PR

    members = PR.Resolver(ctx).doc["members"]
    mine = [k[len("model:" + PREFIX):] for k, v in members.items() if k.startswith("model:" + PREFIX) and v == pid]
    out = []
    # Read by name, never by listing every model: a project page must not make the starter networks as a side effect.
    for name in sorted(set(mine) | ({live[0]} if live[0] else set())):
        have = WS.versions(ctx, name)
        if not have:
            continue
        row = {**WS.summary(ctx, name), "project": members.get(f"model:{PREFIX}{name}")}
        versions = []
        for v in row["versions"]:
            m = WS.manifest(ctx, row["name"], v)
            led = ((m.get("record") or {}).get("ledger") or {})
            versions.append({"version": v, "ref": WS.ref_of(row["name"], v), "deploy": ref_of(row["name"], v), "base": m.get("base"),
                             "changed": led.get("changed") if m.get("base") else 0, "note": m.get("note") or "", "saved_at": m.get("created_at"),
                             "saved_by": m.get("created_by"), "deployed": row["name"] == live[0] and v == live[1]})
        sizes = "-".join(str(s) for s in row.get("sizes") or [])
        out.append({"id": f"{PREFIX}{row['name']}", "kind": "weights", "name": row["name"], "says": row.get("says") or "", "version": row["latest"],
                    "architecture": f"{sizes} network" if sizes else "network", "parameters": row.get("parameters"),
                    "status_kind": "running" if row["name"] == live[0] else "can be deployed", "synthetic": row.get("synthetic"),
                    "in_project": row.get("project") == pid, "deploy": ref_of(row["name"], row["latest"]), "versions": versions[::-1]})
    return out


def join_project(ctx, pid: str, ref: str) -> bool:
    """Put a weights model in the project that deploys it, when it is in no project yet. Returns whether it did."""
    from .. import projects as PR
    from ..weights import store as WS

    name, _ = parse(ref)
    try:
        if PR.Resolver(ctx).doc["members"].get(f"model:{PREFIX}{name}"):
            return False
        WS.assign(ctx, name, pid)
        return True
    except Exception:  # noqa: BLE001  a project that can't take it still gets its deploy
        return False


# ---- "Try it" for any deployed model -----------------------------------------------------------------------


def _manifest(ctx, ref: str) -> dict:
    from ..weights import store as WS

    name, version = parse(resolve(ctx, ref))
    m = WS.manifest(ctx, name, version)
    return WS.load(ctx, name, version)[0] if m.get("base") and "sizes" not in m else m


def copy_request(rec: dict, body: dict) -> str:
    """One command a person pastes to send a test request to a deployed network. The key is read from the
    environment, never written in the command."""
    from .traffic import _http

    ep = rec.get("endpoint") or {}
    payload = {**body, "traffic": "test"}
    if _http(rec):
        return (f"curl -s {ep.get('url')}/predict -H \"Authorization: Bearer $CAPTAIN_STAGING_KEY\" -H \"Content-Type: application/json\" "
                f"-d '{json.dumps(payload, separators=(',', ':'))}'")
    inner = json.dumps(payload, separators=(",", ":"))[1:]
    return (f"aws sagemaker-runtime invoke-endpoint --region {ep.get('region')} --endpoint-name {ep.get('name')} --content-type application/json "
            f"--cli-binary-format raw-in-base64-out --body '{{\"op\":\"predict\",\"key\":\"'\"$CAPTAIN_STAGING_KEY\"'\",{inner}' /dev/stdout")


def try_info(ctx, pid: str, env: str = "staging") -> dict:
    """What a screen needs to offer "Try it" for the model a project has deployed, whatever it is: what the model
    takes and gives back, a ready example request, and the command that sends it. Reads the workspace only: the
    model is not called."""
    from . import addresses_of, serving
    from .traffic import sample_queries

    rec = serving(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Nothing is deployed. Deploy a model first.")
    ep = rec.get("endpoint") or {}
    base = {"project": pid, "env": env, "model_version": rec.get("model_version"), "address": addresses_of(rec),
            "key_name": "CAPTAIN_STAGING_KEY", "send": f"/api/projects/{pid}/deployed/try"}
    if rec.get("serves") == "predict":
        m = _manifest(ctx, rec["model_version"])
        sh = shape(m)
        xs, source = example_values(ctx, m)
        body = example_body(sh, [round(x, 2) for x in xs])
        data = m.get("data") or {}
        return {**base, "serves": "predict", "route": "/predict", "op": "predict", **sh, "example": body, "example_from": source,
                # Said wherever a score is shown: a model trained on made-up data says nothing about real data.
                "synthetic": bool(data.get("synthetic")), "data_note": data.get("note") or "",
                "request": copy_request(rec, body),
                "form": {"kind": "grid" if sh["inputs"][0]["shape"] else "named" if sh["inputs"][0]["names"] else "list",
                         "input": sh["inputs"][0]["name"], "count": sh["inputs"][0]["count"], "shape": sh["inputs"][0]["shape"],
                         "names": sh["inputs"][0]["names"], "low": 0, "high": 1}}
    q = (sample_queries(ctx, pid, 1) or ["shoes"])[0]
    from .traffic import _http

    body = {"query": q, "k": 5}
    payload = json.dumps({**body, "traffic": "test"}, separators=(",", ":"))
    request = (f"curl -s {ep.get('url')}/rank -H \"Authorization: Bearer $CAPTAIN_STAGING_KEY\" -H \"Content-Type: application/json\" -d '{payload}'"
               if _http(rec) else
               f"aws sagemaker-runtime invoke-endpoint --region {ep.get('region')} --endpoint-name {ep.get('name')} --content-type application/json "
               f"--cli-binary-format raw-in-base64-out --body '{{\"op\":\"rank\",\"key\":\"'\"$CAPTAIN_STAGING_KEY\"'\",{payload[1:]}' /dev/stdout")
    return {**base, "serves": "rank", "route": "/rank", "op": "rank",
            "inputs": [{"name": "query", "type": "text", "meaning": "what the shopper searched"},
                       {"name": "k", "type": "number", "meaning": "how many results, 1 to 200"}],
            "outputs": [{"name": "ids", "type": "names", "meaning": "product ids, best first"},
                        {"name": "scores", "type": "numbers", "meaning": "one score for each id"}],
            "text": "Takes a search query. Gives back product ids, best first.", "example": body, "example_from": "a query this project's shoppers search",
            "request": request, "form": {"kind": "query"}}


def try_send(ctx, pid: str, body: dict | None = None, env: str = "staging") -> dict:
    """Send one request to the deployed model, as test traffic, and say what came back in a sentence. With no body
    the example from `try_info` is sent. For a ranker this is the same test request the deployment page sends."""
    from ..model.entities import new_id
    from . import _save, call, serving

    rec = serving(ctx, pid, env)
    if not rec or rec.get("status") != "running":
        raise ValueError("Nothing is deployed. Deploy a model first.")
    if rec.get("serves") != "predict":
        from .traffic import test_request

        return {"serves": "rank", **test_request(ctx, pid, (body or {}).get("query") or None, env)}
    m = _manifest(ctx, rec["model_version"])
    sh = shape(m)
    sent = {"inputs": (body or {})["inputs"]} if (body or {}).get("inputs") is not None else example_body(sh, example_values(ctx, m)[0])
    xs = read_inputs(sh, sent)  # refused here in the same sentence the model would use, before a cloud is called
    r = call(ctx, rec, "predict", {**sent, "traffic": "test", "session_id": "test-" + new_id().lower()[-6:]})
    if not rec.get("tried"):
        _save(ctx, pid, env, tried=True)
    said = sentence(sh, xs, r["outputs"])
    synthetic = bool((m.get("data") or {}).get("synthetic"))
    if synthetic:
        said += " This model was trained on synthetic data, so the score says nothing about real data."
    return {"ok": True, "serves": "predict", "synthetic": synthetic, "request_id": r["request_id"], "model_version": r["model_version"], "ms": r["ms"], "arm": r["arm"],
            "inputs": sent["inputs"], "outputs": r["outputs"], "summary": said,
            "text": f"The model answered in {r['ms']:.0f} ms. {said} This was test traffic: it isn't counted as a real request."}


# ---- from a Weights session to a deployed model, in one call --------------------------------------------------


def session_base(ctx, sid: str) -> tuple[str, int]:
    """(model, the version the session was opened on)."""
    from ..storage.backend import NotFound
    from ..weights import session as WSN

    try:
        s = WSN.session(ctx, sid)
    except NotFound as e:
        raise ValueError(str(e).strip("'\"")) from None
    return s["model"], int(s["version"])


def save_session(ctx, sid: str, note: str = "", by: str | None = None) -> dict:
    """Save a session as a version to deploy: {ref, version, base, saved (False when an earlier save of this same
    session already holds these exact numbers, and that version is used again), ...}. A session with no edits has
    nothing to save: its own base is what there is to deploy, and the answer says so."""
    from ..weights import session as WSN
    from ..weights import store as WS

    cur = WSN.current_digest(ctx, sid)
    if cur["matches"]:
        name, version = WS.split_ref(cur["matches"])
        return {"model": name, "version": int(version), "ref": cur["matches"], "base": WS.manifest(ctx, name, version).get("base"), "saved": False,
                "text": f"{cur['matches'].replace('@', ' ')} already holds these numbers, so it was not saved again."}
    if not cur["changed"]:
        name, version = WS.split_ref(cur["base"])
        return {"model": name, "version": int(version), "ref": cur["base"], "base": None, "saved": False,
                "text": f"This session has no edits, so there is no new version. {cur['base'].replace('@', ' ')} itself is deployed."}
    out = WSN.save(ctx, sid, note=note, by=by)
    return {**out, "saved": True, "text": f"Saved as {out['ref'].replace('@', ' ')}: {str(out['base']).replace('@', ' ')} plus "
                                          f"{out['changed']:,} changed number{'s' if out['changed'] != 1 else ''}."}


def deploy_session(ctx, pid: str, sid: str, host: str = "local", login: str | None = None, note: str = "", by: str | None = None,
                   env: str = "staging", confirm: bool = False, allow_large: bool = False, on_event=None) -> dict:
    """Save a Weights session as a new version and deploy that version to `host`, as one piece of work. A cloud
    that has not been confirmed is refused before anything is saved, with the plan. The deployment's record says
    which version it started from and which edits it carries (`carries`), and roll back returns to the version
    that was running before."""
    from . import NeedsConfirm
    from . import deploy as DP

    model, version = session_base(ctx, sid)
    p = DP.plan(ctx, pid, host, login, candidate=ref_of(model, version), env=env, allow_large=allow_large)
    if p["needs_confirm"] and not confirm:
        raise NeedsConfirm("Confirm first. Nothing was saved. " + p["text"].replace(ref_of(model, version), f"this session's edits to {model} v{version}"), p)
    saved = save_session(ctx, sid, note, by)
    if on_event:
        on_event("line", text=saved["text"], step=None)
    out = DP.run(ctx, pid, host, login, candidate=f"{PREFIX}{saved['ref']}", env=env, confirm=True, allow_large=allow_large, on_event=on_event)
    return {**out, "saved": saved, "session": sid}
