"""A model on disk: a manifest plus tensors, in `.safetensors` or `.npz`, read and written with numpy alone.

A `.safetensors` file is eight bytes giving the length of a JSON header, the header, then every tensor's raw bytes
one after another. The header maps each tensor's name to its type, shape and where its bytes start and end.

The manifest (manifest.json) says what the tensors are: the layers in order, each layer's activation, what the
inputs and outputs mean, and which file holds each tensor. See docs/weights.md.
"""
from __future__ import annotations

import io
import json
import os
import struct

import numpy as np

from .net import ACTIVATIONS, COSTS, Layer, Network, WeightsError, check

FORMAT = "captain-weights/1"
MANIFEST = "manifest.json"
_ST = {"F64": "<f8", "F32": "<f4", "F16": "<f2", "I64": "<i8", "I32": "<i4", "I16": "<i2", "I8": "i1", "U8": "u1", "BOOL": "?"}
_ST_BACK = {np.dtype(v).str: k for k, v in _ST.items()}
_HEADER_LIMIT = 100 << 20


# ---- safetensors ------------------------------------------------------------------------------------------------------

def safetensors_dumps(tensors: dict[str, np.ndarray], metadata: dict[str, str] | None = None) -> bytes:
    header: dict = {}
    if metadata:
        header["__metadata__"] = {str(k): str(v) for k, v in metadata.items()}
    parts, at = [], 0
    for name, t in tensors.items():
        a = np.ascontiguousarray(t)
        a = a.astype(a.dtype.newbyteorder("<")) if a.dtype.byteorder == ">" else a
        code = _ST_BACK.get(a.dtype.str) or _ST_BACK.get(np.dtype(a.dtype.str.replace("=", "<")).str)
        if code is None:
            raise WeightsError(f"The tensor {name} holds {a.dtype} numbers, which a safetensors file cannot store.")
        raw = a.tobytes()
        header[name] = {"dtype": code, "shape": list(a.shape), "data_offsets": [at, at + len(raw)]}
        parts.append(raw)
        at += len(raw)
    head = json.dumps(header, separators=(",", ":")).encode()
    head += b" " * (-len(head) % 8)
    return struct.pack("<Q", len(head)) + head + b"".join(parts)


def safetensors_loads(data: bytes) -> tuple[dict[str, np.ndarray], dict]:
    """Returns (tensors, metadata). Refuses a file whose header and bytes disagree."""
    if len(data) < 8:
        raise WeightsError("This is not a safetensors file: it is shorter than its own header length.")
    (n,) = struct.unpack("<Q", data[:8])
    if n > _HEADER_LIMIT or 8 + n > len(data):
        raise WeightsError("This is not a safetensors file: its header length does not fit the file.")
    try:
        header = json.loads(data[8:8 + n].decode())
    except (UnicodeDecodeError, ValueError):
        raise WeightsError("This is not a safetensors file: its header is not JSON.") from None
    if not isinstance(header, dict):
        raise WeightsError("This is not a safetensors file: its header is not a JSON object.")
    meta = header.pop("__metadata__", None) or {}
    body = memoryview(data)[8 + n:]
    out = {}
    for name, info in header.items():
        try:
            code, shape, (a, b) = info["dtype"], [int(s) for s in info["shape"]], info["data_offsets"]
        except (KeyError, TypeError, ValueError):
            raise WeightsError(f"The safetensors header entry for {name} is incomplete.") from None
        if code not in _ST:
            raise WeightsError(f"The tensor {name} has the type {code}, which is not supported here.")
        dt = np.dtype(_ST[code])
        count = int(np.prod(shape)) if shape else 1
        if a < 0 or b > len(body) or b - a != count * dt.itemsize:
            raise WeightsError(f"The tensor {name} does not fit the bytes the file holds for it.")
        out[name] = np.frombuffer(body[a:b], dtype=dt).reshape(shape).copy()
    return out, meta


# ---- npz ----------------------------------------------------------------------------------------------------------------

def npz_dumps(tensors: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, **{k: np.asarray(v) for k, v in tensors.items()})
    return buf.getvalue()


def npz_loads(data: bytes) -> dict[str, np.ndarray]:
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            return {k: z[k] for k in z.files}
    except (OSError, ValueError, EOFError):
        raise WeightsError("This is not an npz file numpy can read. Save it with numpy.savez and no pickled objects.") from None


def dumps(tensors: dict[str, np.ndarray], filename: str) -> bytes:
    return npz_dumps(tensors) if filename.endswith(".npz") else safetensors_dumps(tensors)


def loads(data: bytes, filename: str) -> dict[str, np.ndarray]:
    return npz_loads(data) if filename.endswith(".npz") else safetensors_loads(data)[0]


# ---- the manifest -------------------------------------------------------------------------------------------------------

def manifest_for(net: Network, name: str, *, inputs: dict | None = None, outputs: dict | None = None,
                 data: dict | None = None, file: str = "tensors.safetensors", dtype: str = "float32", **extra) -> dict:
    """The manifest that describes `net`. `inputs` and `outputs` say what the numbers mean."""
    check(net)
    m = {
        "format": FORMAT, "name": name, "kind": "dense", "sizes": net.sizes, "cost": net.cost,
        "layers": [{"name": ly.name, "inputs": ly.inputs, "outputs": ly.outputs, "activation": ly.activation,
                    "weight": f"{ly.name}.weight", "bias": f"{ly.name}.bias"} for ly in net.layers],
        "inputs": inputs or {"meaning": "not stated"}, "outputs": outputs or {"meaning": "not stated"},
        "tensors": {k: {"file": file, "shape": list(v.shape), "dtype": dtype} for k, v in net.tensors().items()},
        "data": data or {"synthetic": False, "note": "not stated"},
        "parameters": net.parameters,
    }
    m.update(extra)
    return m


def check_manifest(m) -> dict:
    """Refuse a manifest Captain cannot open, naming what is wrong in a sentence."""
    if not isinstance(m, dict):
        raise WeightsError("A model manifest is a JSON object.")
    if m.get("format") != FORMAT:
        raise WeightsError(f"The manifest's format should be {FORMAT!r}. It is {m.get('format')!r}.")
    if m.get("kind", "dense") != "dense":
        raise WeightsError(f"Only fully connected networks open here (kind: dense). This one is {m.get('kind')!r}.")
    layers = m.get("layers")
    if not isinstance(layers, list) or not layers:
        raise WeightsError("The manifest needs a list of layers, in order from the input.")
    tensors = m.get("tensors")
    if not isinstance(tensors, dict):
        raise WeightsError("The manifest needs `tensors`: for each tensor's name, the file that holds it and its shape.")
    for i, ly in enumerate(layers):
        if not isinstance(ly, dict):
            raise WeightsError(f"Layer {i} in the manifest is not an object.")
        for key in ("name", "activation", "weight", "bias"):
            if not isinstance(ly.get(key), str):
                raise WeightsError(f"Layer {i} in the manifest needs `{key}`.")
        if ly["activation"] not in ACTIVATIONS:
            raise WeightsError(f"Layer {ly['name']} has the activation {ly['activation']!r}. Use one of: {', '.join(ACTIVATIONS)}.")
        for key in ("weight", "bias"):
            t = tensors.get(ly[key])
            if not isinstance(t, dict) or not isinstance(t.get("file"), str):
                raise WeightsError(f"Layer {ly['name']} names the tensor {ly[key]!r}, and `tensors` does not say which file holds it.")
            if os.path.isabs(t["file"]) or ".." in t["file"].replace("\\", "/").split("/"):
                raise WeightsError(f"The tensor file {t['file']!r} must be a name inside the model's own folder.")
    if m.get("cost", "squared_error") not in COSTS:
        raise WeightsError(f"The cost {m.get('cost')!r} is not known. Use one of: {', '.join(COSTS)}.")
    return m


def network_from(m: dict, tensors: dict[str, np.ndarray]) -> Network:
    """Build the network a manifest describes from its tensors, checking every shape."""
    check_manifest(m)
    layers = []
    for ly in m["layers"]:
        for key in ("weight", "bias"):
            if ly[key] not in tensors:
                raise WeightsError(f"The tensor {ly[key]!r} is named by the manifest and is not in the tensor file.")
            want = m["tensors"][ly[key]].get("shape")
            if want is not None and list(tensors[ly[key]].shape) != list(want):
                raise WeightsError(f"The tensor {ly[key]!r} has shape {list(tensors[ly[key]].shape)} and the manifest says {list(want)}.")
        layers.append(Layer(ly["name"], np.asarray(tensors[ly["weight"]]), np.asarray(tensors[ly["bias"]]), ly["activation"]))
    net = Network(layers, m.get("cost", "squared_error"))
    check(net)
    return net


def tensor_files(m: dict) -> list[str]:
    return sorted({t["file"] for t in m["tensors"].values()})


def pack(m: dict, tensors: dict[str, np.ndarray]) -> dict[str, bytes]:
    """Every file of a model, by name: the manifest and one file per tensor file it names."""
    check_manifest(m)
    out = {}
    for fname in tensor_files(m):
        names = [k for k, t in m["tensors"].items() if t["file"] == fname]
        out[fname] = dumps({k: np.asarray(tensors[k], dtype=m["tensors"][k].get("dtype") or "float32") for k in names}, fname)
    out[MANIFEST] = json.dumps(m, indent=2).encode()
    return out


def unpack(read, m: dict | None = None) -> tuple[dict, dict[str, np.ndarray]]:
    """Read a model through `read(filename) -> bytes`. Returns (manifest, tensors)."""
    if m is None:
        try:
            m = json.loads(read(MANIFEST).decode())
        except (UnicodeDecodeError, ValueError):
            raise WeightsError("manifest.json is not valid JSON.") from None
    check_manifest(m)
    tensors: dict[str, np.ndarray] = {}
    for fname in tensor_files(m):
        got = loads(read(fname), fname)
        for k, t in m["tensors"].items():
            if t["file"] == fname:
                if k not in got:
                    raise WeightsError(f"The file {fname} does not hold the tensor {k!r} the manifest names.")
                tensors[k] = got[k]
    network_from(m, tensors)
    return m, tensors


def write_model(folder: str, net: Network, name: str, **about) -> dict:
    """Write a network into `folder` as manifest.json plus a tensor file. Returns the manifest.

    This is the call a training script makes: `write_model(os.path.join(os.environ["CST_OUT"], "network"), net, "mine")`."""
    m = manifest_for(net, name, **about)
    os.makedirs(folder, exist_ok=True)
    for fname, body in pack(m, net.tensors()).items():
        with open(os.path.join(folder, fname), "wb") as f:
            f.write(body)
    return m


def read_model(folder: str) -> tuple[dict, dict[str, np.ndarray]]:
    """Read a model folder written by `write_model` or by hand."""
    if not os.path.isfile(os.path.join(folder, MANIFEST)):
        raise WeightsError(f"There is no manifest.json in {folder}. A model folder holds a manifest and its tensor files.")

    def read(fname: str) -> bytes:
        try:
            with open(os.path.join(folder, fname), "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise WeightsError(f"The manifest names the file {fname}, and it is not in the folder.") from None

    return unpack(read)
