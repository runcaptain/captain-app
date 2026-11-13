"""Weights models in a workspace: where they are kept, how a version is made, and how one is opened.

    weights/<model>/v<N>/manifest.json         what the model is (files.py)
    weights/<model>/v<N>/tensors.safetensors   its numbers, for a version that carries its own
    weights/<model>/sessions/<id>/...          the sessions opened on it (session.py)

A version saved from a session carries no tensors. Its manifest names its base and holds the patches, so the
version is exactly "the base plus these edits" and opening it applies them. Versions are write-once.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

import numpy as np

from ..storage.backend import AlreadyExists, NotFound
from . import files as F
from . import starters as ST
from .net import Network, WeightsError

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ROOT = "weights"
WHOLE_LIMIT = 200_000    # the most numbers sent in one answer as a whole network


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.match(name) or name in ("sessions",):
        raise WeightsError(f"A model's name is lower-case letters, digits, dots and dashes, starting with a letter or digit. {name!r} is not.")
    return name


def split_ref(ref: str) -> tuple[str, int | None]:
    """'digits@v2' gives ('digits', 2), and 'digits' gives ('digits', None) for the newest."""
    name, _, v = str(ref).partition("@")
    check_name(name)
    if not v:
        return name, None
    if not re.fullmatch(r"v?\d+", v):
        raise WeightsError(f"A version is written like v2. {v!r} is not one.")
    return name, int(v.lstrip("v"))


def ref_of(name: str, version: int) -> str:
    return f"{name}@v{version}"


def _key(name: str, version: int, fname: str = F.MANIFEST) -> str:
    return f"{ROOT}/{name}/v{version}/{fname}"


def read_file(ctx, name: str, version: int, fname: str) -> bytes:
    """One file of a version as it is stored: its manifest.json, or a tensor file its manifest names."""
    check_name(name)
    return ctx.store.get(_key(name, int(version), fname))


def versions(ctx, name: str) -> list[int]:
    out = []
    for k in ctx.store.list(f"{ROOT}/{name}/"):
        m = re.fullmatch(rf"{ROOT}/{re.escape(name)}/v(\d+)/{re.escape(F.MANIFEST)}", k.replace("\\", "/"))
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def ensure_starters(ctx) -> None:
    """Make the two starter networks in this workspace if they are not there. They are trained here, by code."""
    for name, make in ST.STARTERS.items():
        if ctx.store.exists(_key(name, 1)):
            continue
        net, about = make()
        try:
            save_new(ctx, name, net, starter=True, **about)
        except AlreadyExists:
            pass   # another process made it a moment ago


def save_new(ctx, name: str, net: Network, **about) -> dict:
    """Store a network as the next version of `name`. Returns its manifest."""
    check_name(name)
    m = F.manifest_for(net, name, **about)
    return save_packed(ctx, name, m, net.tensors())


def save_packed(ctx, name: str, m: dict, tensors: dict | None) -> dict:
    """Write a manifest (and its tensors, if it carries any) as the next version. The manifest goes last, so a
    version that can be listed is whole."""
    from ..workspace import whoami

    check_name(name)
    for _ in range(50):
        have = versions(ctx, name)
        v = (have[-1] + 1) if have else 1
        m = {**m, "name": name, "version": v, "created_at": now(), "created_by": m.get("created_by") or whoami()}
        try:
            if tensors is not None:
                packed = F.pack(m, tensors)
                body = packed.pop(F.MANIFEST)
                m = {**m, "sha256": digest({k: np.asarray(t, dtype="float32") for k, t in tensors.items()})}
                body = json.dumps(m, indent=2).encode()
                for fname, data in packed.items():
                    ctx.store.put_new(_key(name, v, fname), data)
            else:
                body = json.dumps(m, indent=2).encode()
            ctx.store.put_new(_key(name, v), body)
            return m
        except AlreadyExists:
            continue   # someone took this number: try the next
    raise WeightsError(f"Could not find a free version number for {name}. Try again.")


def digest(tensors: dict[str, np.ndarray]) -> str:
    h = hashlib.sha256()
    for k in sorted(tensors):
        h.update(k.encode())
        h.update(np.ascontiguousarray(tensors[k]).tobytes())
    return h.hexdigest()


def manifest(ctx, name: str, version: int | None = None) -> dict:
    check_name(name)
    if version is None:
        have = versions(ctx, name)
        if not have:
            if name in ST.STARTERS:
                ensure_starters(ctx)
                return manifest(ctx, name, 1)
            raise NotFound(f"There is no weights model named {name}. See the list with `cst weights models`.")
        version = have[-1]
    try:
        return json.loads(ctx.store.get(_key(name, version)).decode())
    except NotFound:
        if name in ST.STARTERS and version == 1:
            ensure_starters(ctx)
            return json.loads(ctx.store.get(_key(name, 1)).decode())
        raise NotFound(f"There is no version v{version} of the weights model {name}.") from None


def apply_patches(tensors: dict[str, np.ndarray], patches: list[dict]) -> None:
    """Apply saved patches in place, checking each one starts from the numbers it recorded."""
    for p in patches:
        t = tensors.get(p["tensor"])
        if t is None:
            raise WeightsError(f"A saved edit names the tensor {p['tensor']!r}, which this network does not have.")
        flat = t.reshape(-1)
        idx = np.asarray(p["indices"], dtype=np.int64)
        if not np.array_equal(flat[idx], np.asarray(p["old"], dtype=flat.dtype)):
            raise WeightsError(f"A saved edit to {p['tensor']} does not start from the numbers its base holds. The version cannot be opened.")
        flat[idx] = np.asarray(p["new"], dtype=flat.dtype)


def load(ctx, name: str, version: int | None = None) -> tuple[dict, dict[str, np.ndarray]]:
    """Open a version: its manifest and its tensors as float32, with a patched version's edits applied."""
    m = manifest(ctx, name, version)
    if m.get("base"):
        bname, bver = split_ref(m["base"])
        bm, tensors = load(ctx, bname, bver)
        apply_patches(tensors, m.get("patches") or [])
        if m.get("sha256") and digest(tensors) != m["sha256"]:
            raise WeightsError(f"{ref_of(name, m['version'])} does not match the numbers recorded when it was saved.")
        return {**bm, **{k: v for k, v in m.items() if k not in ("tensors",)}, "tensors": bm["tensors"]}, tensors
    _, tensors = F.unpack(lambda fname: ctx.store.get(_key(name, m["version"], fname)), m)
    return m, {k: np.asarray(t, dtype="float32").copy() for k, t in tensors.items()}


def root_of(ctx, name: str, version: int) -> str:
    """The version a line of edits started from: the one that carries its own tensors."""
    m = manifest(ctx, name, version)
    seen = 0
    while m.get("base") and seen < 1000:
        bname, bver = split_ref(m["base"])
        name, version = bname, bver
        m = manifest(ctx, name, version)
        seen += 1
    return ref_of(name, m["version"])


def save_patched(ctx, name: str, base_version: int, patches: list[dict], tensors: dict[str, np.ndarray], record: dict,
                 note: str = "") -> dict:
    """Save "the base plus these patches" as the next version of `name`, with the session's record attached."""
    base = manifest(ctx, name, base_version)
    keep = {k: base[k] for k in ("format", "kind", "sizes", "cost", "layers", "inputs", "outputs", "data", "sets", "parameters", "says")
            if k in base}
    m = {**keep, "tensors": base["tensors"], "base": ref_of(name, base_version), "patches": patches, "record": record,
         "note": note, "sha256": digest(tensors)}
    return save_packed(ctx, name, m, None)


def sets(ctx, m: dict, which: str) -> tuple[np.ndarray, np.ndarray] | None:
    """A model's own named set of examples: (inputs, targets), or None when the model has no such set."""
    spec = (m.get("sets") or {}).get(which)
    if not spec:
        return None
    if spec.get("generator"):
        return ST.generate(spec)
    try:
        data = ctx.store.get(f"{ROOT}/{m['name']}/{spec['file']}")
    except (NotFound, KeyError):
        return None
    got = F.npz_loads(data)
    if "x" not in got or "y" not in got:
        raise WeightsError(f"The set {which} should hold `x` (inputs) and `y` (wanted outputs).")
    return np.asarray(got["x"], dtype=float), got["y"]


def summary(ctx, name: str, project_of=None) -> dict:
    have = versions(ctx, name)
    m = manifest(ctx, name, have[-1])
    out = {"id": name, "name": name, "says": m.get("says") or "", "sizes": m.get("sizes"), "parameters": m.get("parameters"),
           "versions": have, "latest": have[-1], "synthetic": bool((m.get("data") or {}).get("synthetic")),
           "data_note": (m.get("data") or {}).get("note") or "", "starter": bool(m.get("starter"))}
    if project_of:
        out["project"] = project_of(name)
    return out


def models(ctx, project: str | None = None) -> list[dict]:
    """Every weights model in the workspace, the starters first. `project` keeps the ones put in that project."""
    from .. import projects as PR

    ensure_starters(ctx)
    names = set()
    for k in ctx.store.list(f"{ROOT}/"):
        parts = k.replace("\\", "/").split("/")
        if len(parts) == 4 and parts[3] == F.MANIFEST and re.fullmatch(r"v\d+", parts[2]):
            names.add(parts[1])
    try:
        r = PR.Resolver(ctx)
        of = lambda n: r.doc["members"].get(f"model:weights:{n}")  # noqa: E731
    except Exception:  # noqa: BLE001  a workspace with no projects file yet
        of = lambda n: None  # noqa: E731
    rows = [summary(ctx, n, of) for n in sorted(names, key=lambda n: (list(ST.STARTERS).index(n) if n in ST.STARTERS else len(ST.STARTERS), n))]
    return [r for r in rows if not project or r.get("project") == project]


def assign(ctx, name: str, project: str) -> dict:
    """Put a weights model in a project. It is recorded as the project's member `model:weights:<name>`."""
    from .. import projects as PR

    manifest(ctx, name)
    PR.assign(ctx, project, [{"kind": "model", "name": f"weights:{name}"}])
    return {"model": name, "project": project}


def whole(m: dict, tensors: dict[str, np.ndarray], ctx=None, numbers: bool = True) -> dict:
    """A network in the shape a screen draws from. Its examples come along. Held-out ones never do.
    `numbers=False` leaves every weight and bias out: the description only."""
    if numbers and int(m.get("parameters") or 0) > WHOLE_LIMIT:
        raise WeightsError(f"This network has {m['parameters']:,} numbers. Only networks up to {WHOLE_LIMIT:,} are sent whole. "
                           "Read it a layer at a time.")
    layers = [{**({"W": np.asarray(tensors[ly["weight"]], dtype=float).tolist(), "b": np.asarray(tensors[ly["bias"]], dtype=float).tolist()}
                  if numbers else {}),
               "activation": ly["activation"], "name": ly["name"], "inputs": int(tensors[ly["weight"]].shape[1]),
               "outputs": int(tensors[ly["weight"]].shape[0])} for ly in m["layers"]]
    ins, outs = m.get("inputs") or {}, m.get("outputs") or {}
    out = {"id": m["name"], "name": m["name"], "version": m.get("version"), "sizes": m["sizes"],
           "squish": m["layers"][0]["activation"], "cost": m.get("cost"), "layers": layers,
           "inputMeaning": ins.get("meaning"), "outputMeaning": outs.get("meaning"),
           "numbers": numbers, "examples": [], "test": [], "synthetic": bool((m.get("data") or {}).get("synthetic")),
           "data_note": (m.get("data") or {}).get("note") or "", "parameters": m.get("parameters"), "base": m.get("base")}
    if ins.get("shape"):
        out["inputShape"] = ins["shape"]
    if ins.get("names"):
        out["inputNames"] = ins["names"]
    if outs.get("names"):
        out["outputNames"] = outs["names"]
    got = sets(ctx, m, "practice") if ctx is not None else None
    if got is not None:
        x, y = got
        n_out = m["sizes"][-1]
        for i in range(len(x)):
            yi = np.asarray(y[i])
            if yi.ndim == 0 and n_out > 1:
                want, label = np.eye(n_out)[int(yi)].tolist(), (outs.get("names") or [str(j) for j in range(n_out)])[int(yi)]
            else:
                want = np.atleast_1d(yi).astype(float).tolist()
                label = None
            out["examples"].append({"name": f"example {i + 1}" + (f": {label}" if label is not None else ""),
                                    "x": np.round(x[i], 4).tolist(), "y": want})
    return out


def import_folder(ctx, folder: str, name: str | None = None, **extra) -> dict:
    """Bring a model folder (a manifest and tensors, written by any training code) in as the next version."""
    m, tensors = F.read_model(folder)
    name = check_name(name or str(m.get("name") or ""))
    keep = {k: v for k, v in m.items() if k not in ("version", "created_at", "created_by", "sha256", "base", "patches", "record")}
    keep.setdefault("data", {"synthetic": False, "note": "not stated"})
    keep["parameters"] = int(sum(np.asarray(t).size for t in tensors.values()))
    keep["sizes"] = [int(np.asarray(tensors[m["layers"][0]["weight"]]).shape[1])] + [int(np.asarray(tensors[ly["weight"]]).shape[0]) for ly in m["layers"]]
    for ly in keep["layers"]:
        ly["outputs"], ly["inputs"] = (int(s) for s in np.asarray(tensors[ly["weight"]]).shape)
    for k, t in keep["tensors"].items():
        t["shape"], t["dtype"] = list(np.asarray(tensors[k]).shape), "float32"
    if "training" in extra and isinstance(keep.get("training"), dict):
        extra = {**extra, "training": {**keep["training"], **extra["training"]}}
    keep.update(extra)
    # A set that comes as a file (x and y in an .npz) is stored beside the versions, named by its content.
    sets_out = {}
    for which, spec in (keep.get("sets") or {}).items():
        if not isinstance(spec, dict):
            raise WeightsError(f"The set {which} is described by an object: a file with x and y, or a generator.")
        if spec.get("file"):
            rel = str(spec["file"]).replace("\\", "/")
            if os.path.isabs(rel) or ".." in rel.split("/"):
                raise WeightsError(f"The set file {spec['file']!r} must be a name inside the model's own folder.")
            try:
                with open(os.path.join(folder, rel), "rb") as fh:
                    data = fh.read()
            except OSError:
                raise WeightsError(f"The manifest names the set file {spec['file']}, and it is not in the folder.") from None
            got = F.npz_loads(data)
            if "x" not in got or "y" not in got or len(got["x"]) != len(got["y"]):
                raise WeightsError(f"The set {which} should hold `x` (inputs) and `y` (wanted outputs), one row each per example.")
            key = f"sets/{hashlib.sha256(data).hexdigest()[:12]}-{os.path.basename(rel)}"
            try:
                ctx.store.put_new(f"{ROOT}/{name}/{key}", data)
            except AlreadyExists:
                pass
            spec = {**spec, "file": key, "n": int(len(got["x"]))}
        sets_out[which] = spec
    if sets_out:
        keep["sets"] = sets_out
    return save_packed(ctx, name, keep, tensors)
