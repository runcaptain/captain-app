"""A trained session model with its rankers, brought in from the folder its training wrote, and read as it is.

The folder holds a small transformer (`sess.npz`, in PyTorch's names), LightGBM rankers as text (`rank_*.txt`,
each with a `*.cols.json` naming its inputs), lookup tables (other `.npz` files) and a `model.json`. Captain keeps
every file exactly as it came, byte for byte, and writes a manifest beside them:

    weights/<model>/v<N>/manifest.json     kind "session-ranker": what was found, where it came from, its digests
    weights/<model>/v<N>/files/<name>      each file of the folder, unchanged

What the model is gets worked out from the shapes of its arrays and from nothing else: its width, how many layers,
each embedding table, each attention block with its query, key, value and output matrices, each feed-forward
block, the norms and the small heads at the end. The file holds numbers, not wiring, so the order of the parts is
PyTorch's usual one for these names and is said to be a reading, not a fact.

This kind can be read and not yet changed or run. Reading goes through a session like any other model: a slice of
any tensor, statistics, a heatmap, the rows of a table nearest to one row, a table's largest directions, and for
an attention block what each head looks for and what it copies. Those last ones compose the matrices. They never
run the model.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from urllib.parse import urlparse

import numpy as np

from .. import changes as CH
from ..storage.backend import AlreadyExists, NotFound
from . import files as F
from . import session as S
from . import store as WS
from . import trees as T
from .net import WeightsError

KIND = "session-ranker"
KIND_SAYS = "session model with rankers"
NOT_YET = "This model can be read and changed here, not run yet."
NOT_MEASURED = "Not measured: this model cannot be run here yet."
TREES_CANNOT = "A tree ranker can be read here, not changed or run."
FILES = "files"
SESSION_FILE = "sess.npz"
SESSION_FILES = (SESSION_FILE, "enc.npz")    # the session model's file. Older designs call it enc.npz, with the same array names
ITEMS = "cn"                                 # the item vectors worked out ahead of time, one row per catalog item
READ_CAP = 200_000       # the most numbers one read or one heatmap sends
FACT_CAP = 20_000         # a fact longer than this keeps its shape and not its values
ROWS_CAP = 4096
CANNOT = ("template", "forward", "activations", "gradients", "trace", "cost_slice", "set_sets", "evaluate", "export")
EDIT_OPS = ("set", "scale", "zero", "add")
WIRING = ("The order of the parts is PyTorch's usual one for these names: the tables and the text projection are added "
          "together, then each layer is attention, a norm, feed-forward, a norm. The file holds numbers, not wiring, so "
          "this order is a reading of the names and is not confirmed.")
FROM_NAME = "Guessed from the array's name. The file does not say."

# What a table most likely embeds, by the name PyTorch saved it under. A guess, and said to be one.
TABLES = {"act": ("action", "what kind of event it was"),
          "gap": ("time gap", "how long since the event before, in buckets"),
          "dur": ("duration", "how long the event lasted, in buckets"),
          "price": ("price", "the item's price, in buckets"),
          "rep": ("repeat", "how often the item came up before, in buckets"),
          "pos": ("position", "where the event sits in the session"),
          "cat": ("category", "the item's category")}
HEADS = {"q": ("query head", "turns the session's summary into the vector that candidates are compared with"),
         "rq": ("second query head", "a second head of the same shape, most likely for a second kind of comparison"),
         "intent": ("intent head", "one number about the whole session")}
ROLES = (("query", "what this event is looking for"), ("key", "what each event offers to be found by"),
         ("value", "what is copied from an event that is looked at"))


def is_real(m: dict) -> bool:
    return isinstance(m, dict) and m.get("kind") == KIND


# ---- where the files come from ------------------------------------------------------------------------------------------------

def _wanted(name: str) -> bool:
    return "/" not in name and name.lower().endswith((".npz", ".txt", ".json", ".safetensors"))


def _session_in(folder: str) -> bool:
    return any(os.path.isfile(os.path.join(folder, n)) for n in SESSION_FILES)


class _Folder:
    def __init__(self, where: str):
        self.where = os.path.abspath(where)
        inner = os.path.join(self.where, "weights")
        self.dir = inner if _session_in(inner) else self.where
        self.yaml = os.path.join(self.where, "MANIFEST.yaml") if self.dir == inner else (
            os.path.join(os.path.dirname(self.where), "MANIFEST.yaml") if os.path.basename(self.where) == "weights" else "")

    def names(self) -> list[str]:
        try:
            return sorted(n for n in os.listdir(self.dir) if _wanted(n) and os.path.isfile(os.path.join(self.dir, n)))
        except OSError:
            raise WeightsError(f"{self.where} is not a folder that can be read.") from None

    def get(self, name: str) -> bytes:
        with open(os.path.join(self.dir, name), "rb") as f:
            return f.read()

    def manifest_yaml(self) -> bytes | None:
        try:
            with open(self.yaml, "rb") as f:
                return f.read()
        except OSError:
            return None


def _cloud(fn):
    """Read from the cloud, and say what went wrong in a sentence."""
    try:
        return fn()
    except (WeightsError, NotFound):
        raise
    except Exception as e:  # noqa: BLE001  whatever the cloud library raises, a person reads a sentence
        code = ""
        try:
            code = str(e.response.get("Error", {}).get("Code", ""))
        except AttributeError:
            pass
        kind = type(e).__name__
        if "Credentials" in kind or code in ("ExpiredToken", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            why = "this computer has no working default AWS login"
        elif code in ("AccessDenied", "403", "AllAccessDisabled"):
            why = "this computer's default AWS login may not read it"
        elif code in ("NoSuchBucket", "404", "NoSuchKey"):
            why = "there is nothing at that address"
        elif "Endpoint" in kind or "Connect" in kind:
            why = "the cloud could not be reached"
        else:
            why = "the cloud answered with a failure" + (f" ({code})" if re.fullmatch(r"[A-Za-z0-9]{1,40}", code) else "")
        raise WeightsError(f"That address could not be read: {why}.") from None


class _S3:
    """A folder in S3, read with the default login. Only list and get are ever called. Nothing is written there."""

    def __init__(self, where: str):
        from ..storage.backend import S3Store

        u = urlparse(where)
        if not u.netloc:
            raise WeightsError("An S3 address is written s3://bucket/folder.")
        self.where = where.rstrip("/")
        self.bucket, prefix = u.netloc, u.path.strip("/")
        self.store = _cloud(lambda: S3Store(self.bucket, prefix))
        keys = _cloud(lambda: self.store.list(""))
        self.sub = "weights/" if any(k.startswith("weights/") for k in keys) else ""
        self.keys = [k[len(self.sub):] for k in keys if k.startswith(self.sub)]
        self.yaml = None
        if self.sub and "MANIFEST.yaml" in keys:
            self.yaml = (self.store, "MANIFEST.yaml")
        elif not self.sub and prefix.split("/")[-1] == "weights":
            self.yaml = (_cloud(lambda: S3Store(self.bucket, prefix.rpartition("/")[0])), "MANIFEST.yaml")

    def names(self) -> list[str]:
        return sorted(k for k in self.keys if _wanted(k))

    def get(self, name: str) -> bytes:
        return _cloud(lambda: self.store.get(self.sub + name))

    def manifest_yaml(self) -> bytes | None:
        if not self.yaml:
            return None
        try:
            return _cloud(lambda: self.yaml[0].get(self.yaml[1]))
        except (NotFound, WeightsError):
            return None


def is_address(where) -> bool:
    return isinstance(where, str) and where.startswith("s3://")


def looks_real(where) -> bool:
    """Is this a folder of trained weights in the layout above, and not a folder with a manifest.json?"""
    if is_address(where):
        return True
    if not isinstance(where, str) or os.path.isfile(os.path.join(where, F.MANIFEST)):
        return False
    return _session_in(where) or _session_in(os.path.join(where, "weights"))


# ---- reading the arrays ---------------------------------------------------------------------------------------------------------

def _arrays(data: bytes, fname: str) -> tuple[dict[str, np.ndarray], list[str]]:
    """Every array of an .npz that can be read without running code from the file, and the names of those that cannot."""
    if fname.endswith(".safetensors"):
        return F.safetensors_loads(data)[0], []
    out, skipped = {}, []
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            for k in z.files:
                try:
                    out[k] = z[k]
                except ValueError:
                    skipped.append(k)    # a pickled object: reading it would run code from the file
    except (OSError, ValueError, EOFError):
        raise WeightsError(f"{fname} is not an npz file numpy can read.") from None
    return out, skipped


def _is_tensor(name: str, a: np.ndarray) -> bool:
    return a.dtype.kind == "f" and (name.endswith((".weight", ".bias", "in_proj_weight", "in_proj_bias")) or name == "logit_scale" or a.ndim == 2)


def _fact(a: np.ndarray) -> dict:
    out = {"dtype": str(a.dtype), "shape": list(a.shape), "count": int(a.size)}
    if a.dtype.kind in "US":
        vals = [str(v) for v in a.reshape(-1)[:FACT_CAP]]
        out["kind"] = "text"
    elif a.dtype.kind in "fiub":
        vals = [v.item() for v in a.reshape(-1)[:FACT_CAP]]
        out["kind"] = "numbers"
    else:
        return {**out, "kind": "other", "kept": False}
    out["kept"] = a.size <= FACT_CAP
    if out["kept"]:
        out["value"] = vals[0] if a.ndim == 0 else vals
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ranker_name(fname: str) -> str:
    stem = fname.rsplit(".", 1)[0]
    return stem[len("rank_"):] if stem.startswith("rank_") else stem


# ---- import -------------------------------------------------------------------------------------------------------------------

def import_from(ctx, where: str, name: str | None) -> dict:
    """Bring a folder of trained weights in as the next version of `name`. Every file is kept unchanged and its
    digest recorded. Nothing is written back to where it came from."""
    if not name:
        raise WeightsError("Give the model a name here: `cst weights import <folder> --as <name>`.")
    name = WS.check_name(name)
    src = _S3(where) if is_address(where) else _Folder(where)
    names = src.names()
    sess_name = next((n for n in SESSION_FILES if n in names), None)
    if sess_name is None:
        raise WeightsError(f"There is no {' or '.join(SESSION_FILES)} at {where}, or in a weights folder inside it. That file holds the session model.")
    blobs = {n: src.get(n) for n in names}
    arrays, skipped = _arrays(blobs[sess_name], sess_name)
    tensors = {k: a for k, a in arrays.items() if _is_tensor(k, a)}
    if not any(re.fullmatch(r"L\d+\.self_attn\.in_proj_weight", k) for k in tensors):
        raise WeightsError(f"{sess_name} holds no attention block in PyTorch's names (L0.self_attn.in_proj_weight), so it is not a "
                           "session model Captain can describe.")
    facts = {k: _fact(a) for k, a in arrays.items() if k not in tensors}
    for k in skipped:
        facts[k] = {"kind": "other", "kept": False, "says": "Not read: reading it would run code stored in the file."}

    model_json = None
    if "model.json" in blobs:
        try:
            model_json = json.loads(blobs["model.json"].decode())
        except (ValueError, UnicodeDecodeError):
            model_json = None
    rankers, tables, others = [], [], []
    for n in names:
        if n == sess_name or n == "model.json":
            continue
        if n.endswith(".txt"):
            try:
                tm = T.parse(blobs[n].decode(errors="replace"))
            except WeightsError:
                others.append(n)
                continue
            cols = n[:-len(".txt")] + ".cols.json"
            named = T.names_from(blobs[cols], tm["inputs"]) if cols in blobs else []
            rankers.append({"name": _ranker_name(n), "file": f"{FILES}/{n}", "cols": f"{FILES}/{cols}" if cols in blobs else None,
                            "names_from": "the cols file" if named else ("the model file" if tm["names"] else "nowhere: the inputs are numbered"),
                            "objective": tm["objective"] or "not stated", "trees": len(tm["trees"]), "inputs": tm["inputs"]})
        elif n.endswith((".npz", ".safetensors")):
            got, skip = _arrays(blobs[n], n)
            tables.append({"name": n.rsplit(".", 1)[0], "file": f"{FILES}/{n}",
                           "arrays": [{"name": k, "shape": list(a.shape), "dtype": str(a.dtype)} for k, a in got.items()]
                           + [{"name": k, "shape": None, "dtype": "not read"} for k in skip],
                           "says": "A lookup table. It was counted from the data, not learned by gradient."})
        elif not n.endswith(".cols.json"):
            others.append(n)

    yaml_bytes = src.manifest_yaml()
    declared = None
    if yaml_bytes is not None:
        blobs["MANIFEST.yaml"] = yaml_bytes
        try:
            import yaml

            got = yaml.safe_load(yaml_bytes.decode())
            if isinstance(got, dict) and len(json.dumps(got, default=str)) < 20_000:
                declared = json.loads(json.dumps(got, default=str))
        except Exception:  # noqa: BLE001  a manifest that cannot be parsed is still kept as a file
            declared = None

    m = {"format": F.FORMAT, "name": name, "kind": KIND, "kind_says": KIND_SAYS, "sizes": [], "layers": [],
         "parameters": int(sum(a.size for a in tensors.values())),
         "tensors": {k: {"file": f"{FILES}/{sess_name}", "shape": list(a.shape), "dtype": str(a.dtype)} for k, a in tensors.items()},
         "facts": facts, "model_json": model_json, "rankers": rankers, "tables": tables, "other_files": others,
         "sha256": WS.digest(tensors),
         "source": {"from": src.where, "read_at": WS.now(),
                    "files": {n: {"sha256": _sha(b), "bytes": len(b)} for n, b in sorted(blobs.items())},
                    "manifest_yaml": yaml_bytes is not None, "declared": declared},
         "data": {"synthetic": False, "note": "Trained weights brought in from the folder their training wrote."}}
    d = describe(m)
    m["says"] = (f"A session model, width {d['width']}, {S._plural(d['depth'], 'layer')}, "
                 f"{'heads not stated' if d['heads'] is None else S._plural(d['heads'], 'head')}, with {S._plural(len(rankers), 'tree ranker')}.")
    m = _store(ctx, name, m, blobs)
    CH.bump("weights", f"weights:{name}")
    return {"model": name, "version": m["version"], "ref": WS.ref_of(name, m["version"]), "kind": KIND, "kind_says": KIND_SAYS,
            "parameters": m["parameters"], "sizes": [], "width": d["width"], "depth": d["depth"], "heads": d["heads"],
            "tensors": len(tensors), "facts": sorted(facts), "rankers": [r["name"] for r in rankers], "tables": [t["name"] for t in tables],
            "sha256": m["sha256"], "files": {n: f["sha256"] for n, f in m["source"]["files"].items()}, "from": m["source"]["from"],
            "says": m["says"], "cost_points": 0}


def _store(ctx, name: str, m: dict, blobs: dict[str, bytes]) -> dict:
    """Write the files, then the manifest, as the next version. Write-once: nothing that is there is replaced."""
    from ..workspace import whoami

    for _ in range(50):
        have = WS.versions(ctx, name)
        v = (have[-1] + 1) if have else 1
        m = {**m, "version": v, "created_at": WS.now(), "created_by": m.get("created_by") or whoami()}
        try:
            for fname, data in blobs.items():
                key = WS._key(name, v, f"{FILES}/{fname}")
                try:
                    ctx.store.put_new(key, data)
                except AlreadyExists:
                    if ctx.store.get(key) != data:    # left by an import that stopped part way, with other bytes
                        raise
            ctx.store.put_new(WS._key(name, v), json.dumps(m, indent=2).encode())
            return m
        except AlreadyExists:
            continue
    raise WeightsError(f"Could not find a free version number for {name}. Try again.")


def _file(ctx, m: dict, rel: str) -> bytes:
    """One of a version's kept files. A version saved from a session keeps none of its own: they are its root's."""
    name, version = WS.split_ref(m["files_at"]) if m.get("files_at") else (m["name"], m["version"])
    return ctx.store.get(WS._key(name, version, rel))


def load(ctx, m: dict) -> tuple[dict, dict[str, np.ndarray]]:
    """A version's manifest and its tensors, each in the number type it was saved with."""
    if m.get("base"):    # saved from a session: its base, then its edits, checked against the digest it was saved with
        bname, bver = WS.split_ref(m["base"])
        _, tensors = WS.load(ctx, bname, bver)
        WS.apply_patches(tensors, m.get("patches") or [])
        if m.get("sha256") and WS.digest(tensors) != m["sha256"]:
            raise WeightsError(f"{WS.ref_of(m['name'], m['version'])} does not match the numbers recorded when it was saved.")
        return m, tensors
    out: dict[str, np.ndarray] = {}
    for fname in sorted({t["file"] for t in m["tensors"].values()}):
        got, _ = _arrays(_file(ctx, m, fname), fname)
        for k, t in m["tensors"].items():
            if t["file"] == fname:
                if k not in got:
                    raise WeightsError(f"The stored file {fname} no longer holds the tensor {k!r}.")
                out[k] = got[k].copy()
    if m.get("sha256") and WS.digest(out) != m["sha256"]:
        raise WeightsError(f"{WS.ref_of(m['name'], m['version'])} does not match the numbers recorded when it was brought in.")
    return m, out


# ---- what the model is, from its shapes -------------------------------------------------------------------------------------------

def _stated(m: dict, name: str) -> int | None:
    """A whole number the file states about itself, saved as an array of one number: `heads`, `layers`."""
    f = (m.get("facts") or {}).get(name) or {}
    v = f.get("value")
    return v if f.get("kind") == "numbers" and isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _declared_heads(m: dict, width: int | None) -> tuple[int | None, str]:
    def from_obj(obj) -> int | None:
        if isinstance(obj, dict):
            for k in ("heads", "n_heads", "nhead", "num_heads", "n_head"):
                if isinstance(obj.get(k), int) and not isinstance(obj.get(k), bool):
                    return obj[k]
            for v in obj.values():
                got = from_obj(v) if isinstance(v, (dict, str)) else None
                if got:
                    return got
        if isinstance(obj, str):
            hit = re.search(r"(\d+)\s*heads?\b", obj) or re.search(r"\b(?:n_?heads?|nhead|num_heads)\W{0,4}(\d+)", obj)
            if hit:
                return int(hit.group(1))
        return None

    n = _stated(m, "heads")
    if n and width and width % n == 0:
        return n, "the file"
    for src, obj in (("model.json", m.get("model_json")), ("the cfg note saved with the arrays", ((m.get("facts") or {}).get("cfg") or {}).get("value"))):
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except ValueError:
                pass
        n = from_obj(obj)
        if n and width and width % n == 0:
            return n, src
    return None, ""


def _declared_layers(m: dict) -> int | None:
    if _stated(m, "layers"):
        return _stated(m, "layers")
    for v in (m.get("model_json") or {}).values() if isinstance(m.get("model_json"), dict) else ():
        if isinstance(v, str):
            hit = re.search(r"(\d+)\s*layers?\b", v)
            if hit:
                return int(hit.group(1))
    return None


def _table_words(stem: str) -> tuple[str, str, bool]:
    """(plain name, what it embeds, was it recognised) for an embedding table's saved name."""
    side = stem.startswith("c_")
    key = (stem[2:] if side else stem).split(".")[0]
    if key in TABLES:
        short, what = TABLES[key]
        return (f"candidate {short} table" if side else f"{short} table"), what, True
    return f"table {stem}", "What it stands for is not stated in the file.", False


def describe(m: dict) -> dict:
    """The model's parts, worked out from the shapes of its tensors. No number is read."""
    sh = {k: tuple(int(s) for s in t["shape"]) for k, t in m["tensors"].items()}
    ids = sorted({int(g.group(1)) for k in sh if (g := re.match(r"L(\d+)\.", k))})
    width = None
    for k in ids:
        got = sh.get(f"L{k}.self_attn.out_proj.weight")
        if got and len(got) == 2:
            width = got[0]
            break
    heads, heads_from = _declared_heads(m, width)
    count = lambda *names: int(sum(int(np.prod(sh[n])) if sh[n] else 1 for n in names if n in sh))  # noqa: E731
    have = lambda *names: [n for n in names if n in sh]  # noqa: E731
    used: set[str] = set()
    parts: list[dict] = []

    def part(pid: str, kind: str, name: str, says: str, tensors: list[str], feeds: list[str], **more) -> dict:
        used.update(tensors)
        p = {"id": pid, "kind": kind, "name": name, "says": says, "technical": pid, "tensors": tensors,
             "shape": list(sh[tensors[0]]) if tensors else [], "parameters": count(*tensors), "feeds_from": feeds,
             "editable": all(m["tensors"][t].get("dtype") == "float32" for t in tensors), **more}
        parts.append(p)
        return p

    # Everything outside the numbered layers, by its shape.
    stems = sorted({k.rsplit(".", 1)[0] for k in sh if k.endswith((".weight", ".bias")) and not re.match(r"L\d+\.", k)},
                   key=lambda s: list(sh).index(s + ".weight") if s + ".weight" in sh else len(sh))
    inputs: dict[str, list[str]] = {"session": [], "candidate": []}
    norms, small = [], []
    for stem in stems:
        w, b = sh.get(stem + ".weight"), sh.get(stem + ".bias")
        side = "candidate" if stem.startswith("c_") else "session"
        if w is None:
            continue
        if len(w) == 2 and b is None:
            name, what, known = _table_words(stem)
            part(stem, "embedding", name, f"embedding table: one row of {w[1]} numbers for each of {w[0]} values. {what[0].upper() + what[1:]}"
                 + ("" if what.endswith(".") else "."), [stem + ".weight"], [], side=side, rows=w[0], width=w[1],
                 embeds=what if known else None, meaning_from="name" if known else None, meaning_says=FROM_NAME if known else what)
            inputs[side].append(stem)
        elif len(w) == 2 and b is not None and width is not None and w[1] != width:
            words = "the item's text vector" if "text" in stem else "an outside vector"
            part(stem, "projection", ("candidate " if side == "candidate" else "") + ("text projection" if "text" in stem else f"projection {stem}"),
                 f"projection: turns {words} of {w[1]} numbers into {w[0]}, the model's width.", have(stem + ".weight", stem + ".bias"), [],
                 side=side, inputs=w[1], outputs=w[0])
            inputs[side].append(stem)
        elif len(w) == 1 and b is not None and len(b) == 1:
            norms.append(stem)
        elif len(w) == 2:
            small.append(stem)
    last = list(inputs["session"])
    for stem in norms:
        side = "candidate" if stem.startswith("c_") else "session"
        part(stem, "norm", f"norm {stem}", "norm: rescales each vector so its numbers stay in a steady range.",
             have(stem + ".weight", stem + ".bias"), list(inputs[side]), side=side, width=sh[stem + ".weight"][0])
        if side == "session":
            last = [stem]

    hidden = None
    for k in ids:
        p = f"L{k}"
        a = f"{p}.self_attn"
        ipw = sh.get(f"{a}.in_proj_weight")
        if ipw and len(ipw) == 2 and ipw[0] % 3 == 0:
            d = ipw[0] // 3
            mats = [{"role": role, "name": f"{role}: {what}", "tensor": f"{a}.in_proj_weight", "rows": [i * d, (i + 1) * d],
                     "shape": [d, ipw[1]], "bias": {"tensor": f"{a}.in_proj_bias", "rows": [i * d, (i + 1) * d]} if f"{a}.in_proj_bias" in sh else None}
                    for i, (role, what) in enumerate(ROLES)]
            if f"{a}.out_proj.weight" in sh:
                ow = sh[f"{a}.out_proj.weight"]
                mats.append({"role": "output", "name": "output: how what was copied is written back", "tensor": f"{a}.out_proj.weight",
                             "rows": [0, ow[0]], "shape": list(ow),
                             "bias": {"tensor": f"{a}.out_proj.bias", "rows": [0, ow[0]]} if f"{a}.out_proj.bias" in sh else None})
            part(a, "attention", f"attention, layer {k + 1}", "attention: which earlier events this one looks at, and what it copies from them.",
                 have(f"{a}.in_proj_weight", f"{a}.in_proj_bias", f"{a}.out_proj.weight", f"{a}.out_proj.bias"), last, side="session",
                 layer=k, width=d, heads=heads, head_width=(d // heads) if heads else None, matrices=mats,
                 packed="PyTorch stacks the query, key and value matrices in one tensor, in that order. Each is given here by its rows.")
            last = [a]
        if f"{p}.norm1.weight" in sh:
            part(f"{p}.norm1", "norm", f"norm after attention, layer {k + 1}", "norm: rescales each vector so its numbers stay in a steady range.",
                 have(f"{p}.norm1.weight", f"{p}.norm1.bias"), last, side="session", layer=k, width=sh[f"{p}.norm1.weight"][0])
            last = [f"{p}.norm1"]
        if f"{p}.linear1.weight" in sh:
            l1 = sh[f"{p}.linear1.weight"]
            hidden = hidden or l1[0]
            part(f"{p}.ff", "feed_forward", f"feed-forward, layer {k + 1}",
                 f"feed-forward: reworks each event's vector on its own, from {l1[1]} numbers up to {l1[0]} and back.",
                 have(f"{p}.linear1.weight", f"{p}.linear1.bias", f"{p}.linear2.weight", f"{p}.linear2.bias"), last, side="session", layer=k,
                 inputs=l1[1], hidden=l1[0], steps=[{"name": "up", "tensor": f"{p}.linear1.weight", "shape": list(l1)}]
                 + ([{"name": "back", "tensor": f"{p}.linear2.weight", "shape": list(sh[f"{p}.linear2.weight"])}] if f"{p}.linear2.weight" in sh else []))
            last = [f"{p}.ff"]
        if f"{p}.norm2.weight" in sh:
            part(f"{p}.norm2", "norm", f"norm after feed-forward, layer {k + 1}", "norm: rescales each vector so its numbers stay in a steady range.",
                 have(f"{p}.norm2.weight", f"{p}.norm2.bias"), last, side="session", layer=k, width=sh[f"{p}.norm2.weight"][0])
            last = [f"{p}.norm2"]
    for stem in small:
        w = sh[stem + ".weight"]
        side = "candidate" if stem.startswith("c_") else "session"
        short, what = HEADS.get(stem, (f"head {stem}", "What it is for is not stated in the file."))
        known = stem in HEADS
        part(stem, "head", short, f"small head at the end: {w[1]} numbers in, {w[0]} out. {what[0].upper() + what[1:]}" + ("" if what.endswith(".") else "."),
             have(stem + ".weight", stem + ".bias"), last if side == "session" else list(inputs["candidate"]), side=side,
             inputs=w[1], outputs=w[0], meaning_from="name" if known else None, meaning_says=FROM_NAME if known else what)
    if ITEMS in sh and len(sh[ITEMS]) == 2:
        part(ITEMS, "items", "item vectors", f"item vectors, worked out ahead of time: one row of {sh[ITEMS][1]} numbers per item, "
             f"{sh[ITEMS][0]:,} items. They are what a session's vector is compared with. They are not a learned table of the session side.",
             [ITEMS], [], side="candidate", rows=sh[ITEMS][0], width=sh[ITEMS][1])
    if "logit_scale" in sh:
        part("logit_scale", "scale", "score sharpness", "one number: how sharply the scores of candidates are told apart.", ["logit_scale"],
             [], side=None)
    for k in sh:
        if k not in used:
            part(k, "other", k, "A tensor Captain does not recognise by its name or shape. It can still be read.", [k], [], side=None)

    declared_layers = _declared_layers(m)
    mj = m.get("model_json") if isinstance(m.get("model_json"), dict) else {}
    facts = [{"name": k, "kind": f.get("kind"), "shape": f.get("shape"), "count": f.get("count"), "kept": bool(f.get("kept"))}
             for k, f in sorted((m.get("facts") or {}).items())]
    return {"kind": KIND, "kind_says": KIND_SAYS, "width": width, "depth": len(ids), "heads": heads,
            "heads_from": heads_from or None,
            "heads_says": (f"{S._plural(heads, 'head')}, as {heads_from} says. The shapes alone cannot tell." if heads
                           else "Unknown. The shapes do not tell how many heads there are, and no file beside them says."),
            "head_width": (width // heads) if heads and width else None, "feed_forward": hidden, "parameters": int(m.get("parameters") or 0),
            "declared": {"session_model": mj.get("session_model"), "layers": declared_layers,
                         "layers_from": None if declared_layers is None else ("the file" if _stated(m, "layers") else "model.json"),
                         "layers_agree": None if declared_layers is None else declared_layers == len(ids)},
            "parts": parts, "wiring_says": WIRING, "facts": facts, "session_file": next(iter(m["tensors"].values()))["file"].split("/")[-1],
            "other_files": [{"name": n, "bytes": (((m.get("source") or {}).get("files") or {}).get(n) or {}).get("bytes"),
                             "says": "Kept unchanged. Not read."} for n in m.get("other_files") or []],
            "rankers": [{**r, "says": T.SAYS} for r in m.get("rankers") or []], "tables": m.get("tables") or [],
            "can": {"read": True, "edit": True, "run": False, "deploy": False, "says": NOT_YET, "edits_say": NOT_MEASURED}}


def source_says(m: dict) -> str:
    """Where a model came from, in one sentence a list can show. No folder, bucket or address is in it."""
    src = m.get("source") or {}
    place = "cloud storage" if is_address(src.get("from")) else "a training folder on this computer"
    day = str(src.get("read_at") or "")[:10]
    n = len(src.get("files") or {})
    out = f"Brought in from {place}" + (f" on {day}" if day else "") + f", {S._plural(n, 'file')} kept unchanged."
    if m.get("base"):
        out += f" This version is {m['base'].replace('@v', ', version ')} with edits saved on {str(m.get('created_at') or '')[:10]}."
    return out


def whole(ctx, model: str, sid: str | None = None) -> dict | None:
    """This kind's answer to `GET /api/weights/models/{model}`: the description, with `parts` to draw. None for
    any other kind. No number is in it, with a session or without."""
    name, version = WS.split_ref(model)
    m = WS.manifest(ctx, name, version)
    if not is_real(m):
        return None
    d = describe(m)
    src = m.get("source") or {}
    out = {"id": m["name"], "name": m["name"], "version": m.get("version"), "says": m.get("says") or "", "numbers": False,
           "sizes": [], "layers": [], "examples": [], "test": [], "synthetic": False, "data_note": (m.get("data") or {}).get("note") or "",
           "base": m.get("base"), "note": m.get("note") or "", "cost": None, "sha256": m.get("sha256"), "source_says": source_says(m),
           "source": {"from": src.get("from"), "read_at": src.get("read_at"), "files": src.get("files") or {},
                      "manifest_yaml": bool(src.get("manifest_yaml"))}, **d}
    if sid:
        def fn(st):
            st.looked = 0
            return {**out, "session": st.sid, "edits": 0}
        return S._do(ctx, sid, fn, watch=True)
    return out


# ---- a session's operations on this kind ------------------------------------------------------------------------------------------

_KINDS: dict[tuple, str] = {}


def kind_of(ctx, sid: str) -> str:
    k = (ctx.store.uri, sid)
    if k not in _KINDS:
        model = S.find(ctx, sid)
        try:
            doc = json.loads(ctx.store.get(S._skey(model, sid)).decode())
        except NotFound:
            raise NotFound(f"There is no weights session {sid!r}. Open one with `cst weights open <model>`.") from None
        if len(_KINDS) > 512:
            _KINDS.clear()
        _KINDS[k] = str(WS.manifest(ctx, model, doc["version"]).get("kind") or "dense")
    return _KINDS[k]


def _cannot(*_a, **_k):
    raise WeightsError(NOT_YET)


def _no_layers(*_a, **_k):
    raise WeightsError("This model has parts, not numbered layers. Name a tensor instead. `cst weights layers` lists them.")


def handler(ctx, sid: str, op: str):
    """The function that answers `op` for the session's model when it is this kind, else None."""
    if op not in OPS and op not in CANNOT:
        return None
    if kind_of(ctx, sid) != KIND:
        return None
    return OPS.get(op) or _cannot


def _tensor(st, name) -> np.ndarray:
    if name in (None, ""):
        raise WeightsError("Name a tensor. `cst weights layers` lists them.")
    if name not in st.cur:
        raise WeightsError(f"This model has no tensor named {name!r}. `cst weights layers` lists its tensors.")
    return st.cur[name]


def _matrix(st, name) -> np.ndarray:
    t = _tensor(st, name)
    if t.ndim != 2:
        raise WeightsError(f"{name} is not a table of rows and columns. Its shape is {list(t.shape) or 'a single number'}.")
    return t


def _raw(st, what: str) -> None:
    if not st.doc["allowed"]["raw_weights"]:
        raise S.Refused(f"This session may not see raw weights, and {what}.")


def layers(ctx, sid: str, by: str | None = None, watch: bool = False) -> dict:
    def fn(st):
        d = describe(st.manifest)
        if not st.watch:
            st.event("read", f"{S._Who(st, by)} listed the {S._plural(len(d['parts']), 'part')} of the model.", by, op="layers")
        return {"sizes": [], "cost": None, "layers": [], **d}
    return S._do(ctx, sid, fn, watch)


def read(ctx, sid: str, tensor: str, rows=None, cols=None, by: str | None = None, watch: bool = False) -> dict:
    """A slice of any tensor: a table by rows and columns, a list by rows, or a single number."""
    def fn(st):
        t = _tensor(st, tensor)
        if t.ndim > 2:
            raise WeightsError(f"{tensor} has {t.ndim} dimensions. Only single numbers, lists and tables are read here.")
        if t.ndim == 2:
            r0, r1 = S._span(rows, t.shape[0], "Rows")
            c0, c1 = S._span(cols, t.shape[1], "Columns")
            vals, span = t[r0:r1, c0:c1], [c0, c1]
        else:
            if cols not in (None, ""):
                raise WeightsError(f"{tensor} is one list of numbers. It has rows and no columns.")
            flat = t.reshape(-1)
            r0, r1 = S._span(rows, flat.shape[0], "Rows")
            vals, span = flat[r0:r1], None
        if vals.size > READ_CAP:
            raise WeightsError(f"That is {vals.size:,} numbers. At most {READ_CAP:,} are sent at once: ask for fewer rows or columns.")
        st.looked = int(vals.size)
        if not st.watch:
            _raw(st, "this reads them")
            st.doc["used"]["weights_read"] += int(vals.size)
            st.event("read", f"{S._Who(st, by)} read {S._plural(int(vals.size), 'number')} of {tensor}.", by, op="read", tensor=tensor,
                     args={"rows": [r0, r1], "cols": span}, count=int(vals.size))
        return {"tensor": tensor, "shape": list(t.shape), "dtype": str(t.dtype), "rows": [r0, r1], "cols": span,
                "values": vals.astype(float).tolist(), "count": int(vals.size)}
    return S._do(ctx, sid, fn, watch)


def stats(ctx, sid: str, tensor: str | None = None, neurons: bool = False, by: str | None = None, watch: bool = False) -> dict:
    """Numbers about the numbers: per tensor, and per row of one table when `neurons` is asked for with a tensor."""
    def fn(st):
        if tensor:
            _tensor(st, tensor)
        names = [tensor] if tensor else list(st.cur)
        out = {"tensors": [S._tensor_stats(n, st.cur[n]) for n in names]}
        if neurons:
            if not tensor:
                raise WeightsError("Name one tensor to get a line per row.")
            t = _matrix(st, tensor).astype(float)
            norm = np.linalg.norm(t, axis=1)
            got = _labels(st.manifest, tensor, t, st.cur)
            out["labels_says"] = got["says"]
            out["rows"] = [{"row": j, "label": got["names"][j] if got["names"] else None, "norm": float(norm[j]), "mean": float(t[j].mean()), "min": float(t[j].min()), "max": float(t[j].max()),
                            "zero_share": float((t[j] == 0).mean()), "dead": bool((t[j] == 0).all())} for j in range(min(t.shape[0], ROWS_CAP))]
            out["rows_of"] = int(t.shape[0])
        if not st.watch:
            st.event("read", f"{S._Who(st, by)} looked at the statistics of {tensor or 'every tensor'}.", by, op="stats", tensor=tensor,
                     args={"rows": bool(neurons)})
        return out
    return S._do(ctx, sid, fn, watch)


def heatmap(ctx, sid: str, layer=None, neuron=None, by: str | None = None, watch: bool = False, tensor: str | None = None,
            rows=None, cols=None) -> dict:
    """Any matrix, or a part of it, as one grid of numbers to colour."""
    def fn(st):
        if not tensor:
            raise WeightsError("Name the matrix to draw, such as L0.linear1.weight. `cst weights layers` lists them.")
        t = _tensor(st, tensor)
        if t.ndim == 1:
            t = t.reshape(1, -1)
        if t.ndim != 2:
            raise WeightsError(f"{tensor} is not a table of rows and columns, so it cannot be drawn as a grid.")
        r0, r1 = S._span(rows, t.shape[0], "Rows")
        c0, c1 = S._span(cols, t.shape[1], "Columns")
        vals = t[r0:r1, c0:c1]
        if vals.size > READ_CAP:
            raise WeightsError(f"That is {vals.size:,} numbers. At most {READ_CAP:,} are drawn at once: ask for fewer rows or columns.")
        st.looked = int(vals.size)
        if not st.watch:
            _raw(st, "a heatmap is made of them")
            st.doc["used"]["weights_read"] += int(vals.size)
            st.event("read", f"{S._Who(st, by)} looked at {S._plural(int(vals.size), 'number')} of {tensor} as a grid.", by, op="heatmap",
                     tensor=tensor, args={"rows": [r0, r1], "cols": [c0, c1]}, count=int(vals.size))
        return {"tensor": tensor, "shape": list(st.cur[tensor].shape), "rows": [r0, r1], "cols": [c0, c1], "grid_shape": list(vals.shape),
                "scale": float(np.abs(vals).max()) if vals.size else 0.0, "min": float(vals.min()), "max": float(vals.max()),
                "grid": vals.astype(float).tolist(), "grids": []}
    return S._do(ctx, sid, fn, watch)


EXTRA = "none or unknown"


def _extra_row(t: np.ndarray) -> tuple[int, bool]:
    """Where a table's one extra row sits, and whether that is clear: (row, sure). A row whose numbers are all near
    zero beside the others is the odd one. When neither end stands out, row 0 is taken, as a guess."""
    size = np.linalg.norm(np.asarray(t, dtype=float), axis=1)
    mid = float(np.median(size[1:-1])) if len(size) > 2 else float(np.median(size))
    first, last = bool(size[0] <= 0.1 * mid), bool(size[-1] <= 0.1 * mid)
    if first != last:
        return (0 if first else len(size) - 1), True
    return 0, False


def _ranges(edges: list[float]) -> list[str]:
    """The ranges that a list of edges cuts the number line into: below the first, between each pair, above the last."""
    e = [f"{float(v):.2f}" for v in edges]
    return [f"below {e[0]}"] + [f"{a} to {b}" for a, b in zip(e, e[1:])] + [f"{e[-1]} and up"]


def _item_labels(m: dict, t: np.ndarray, cur: dict | None) -> dict:
    """Names for the rows of the item vectors: the item's place, with its price bucket and category when the file
    holds one of each per item (`priceb`, `catid`)."""
    rows = int(t.shape[0])
    facts = m.get("facts") or {}
    per = {}
    for key in ("priceb", "catid"):
        f = facts.get(key) or {}
        if f.get("kind") == "numbers" and isinstance(f.get("value"), list) and len(f["value"]) == rows:
            per[key] = f["value"]
    cats, cat_from = None, ""
    if "catid" in per and cur:
        table = next((k for k in cur if re.fullmatch(r"cat(\..+)?\.weight", k)), None)
        got = _labels(m, table, cur[table]) if table else None
        if got and got["names"] and got["sure"] and all(isinstance(c, int) and 0 <= c < len(got["names"]) for c in per["catid"]):
            cats, cat_from = got["names"], f" Categories are named from the rows of {table}."
    names = []
    for i in range(rows):
        bits = [f"item {i}"]
        if "priceb" in per:
            bits.append(f"price bucket {per['priceb'][i]}")
        if "catid" in per:
            bits.append(f"category {cats[per['catid'][i]] if cats else per['catid'][i]}")
        names.append(", ".join(bits))
    said = " and ".join({"priceb": "its price bucket from priceb", "catid": "its category from catid"}[k] for k in per)
    return {"names": names, "source": ", ".join(per) or None, "extra_row": None, "sure": True,
            "says": "Rows are items, in the order the file holds them" + (f", each with {said}, saved beside the arrays." if per else ".")
                    + cat_from + ("" if cats or "catid" not in per else " Categories are numbered: their names could not be matched for sure.")}


def _labels(m: dict, tensor: str, t: np.ndarray, cur: dict | None = None) -> dict:
    """Names for a table's rows, from a list saved beside the arrays: {names, says, source, extra_row, sure}.

    A list with one name per row names them. A list one short of the rows names them too, with the row left over
    called "none or unknown": at the end whose row is near zero, or at row 0 as a guess when neither is. A price
    table is named from `edges`, the cut points between its ranges."""
    if tensor == ITEMS:
        return _item_labels(m, t, cur)
    rows = int(t.shape[0])
    none = {"names": None, "says": "", "source": None, "extra_row": None, "sure": None}
    stem = tensor.rsplit(".", 1)[0]
    key = (stem[2:] if stem.startswith("c_") else stem).split(".")[0]
    facts = m.get("facts") or {}
    f, source, what = facts.get(key + "_vocab"), key + "_vocab", "names"
    have = f["value"] if f and f.get("kind") == "text" and isinstance(f.get("value"), list) else None
    if have is None and key == "price":
        f, source, what = facts.get("edges"), "edges", "ranges"
        if f and f.get("kind") == "numbers" and isinstance(f.get("value"), list) and len(f["value"]) >= 2:
            have = _ranges(f["value"])
            if rows not in (len(have), len(have) + 1):     # the edges may also be the two ends of the ranges
                inner = have[1:-1]
                have = inner if rows in (len(inner), len(inner) + 1) else have
    if have is None:
        return none
    made = f"{len(f['value']):,} edges, which make {len(have):,} ranges" if what == "ranges" else f"{len(have):,} names"
    if len(have) == rows:
        return {"names": list(have), "says": f"Rows are named from {source}, saved beside the arrays: {made}, one for each row.",
                "source": source, "extra_row": None, "sure": True}
    if len(have) == rows - 1:
        at, sure = _extra_row(t)
        names = [EXTRA] + list(have) if at == 0 else list(have) + [EXTRA]
        where = "row 0" if at == 0 else f"the last row, {rows - 1}"
        why = ("That row's numbers are all near zero beside the others, so it is the odd one." if sure
               else "Neither end stands out, so its place is a guess: every name may be one row off.")
        return {"names": names, "says": f"{source} holds {made} for {rows:,} rows, one row more. The extra row is taken to be {where}, "
                                        f"called \"{EXTRA}\". {why}", "source": source, "extra_row": at, "sure": sure}
    return {**none, "says": f"{source} holds {made} for {rows:,} rows, so which row is which is not known. Rows are numbered.", "source": source}


def labels(ctx, sid: str, tensor: str, by: str | None = None, watch: bool = False, rows=None) -> dict:
    """What each row of a table stands for, when a list saved beside the arrays says. Not an event: it shows no number.
    `rows` (3 or 0:50) gives the names of those rows only."""
    def fn(st):
        t = _matrix(st, tensor)
        got = _labels(st.manifest, tensor, t, st.cur)
        r0, r1 = S._span(rows, t.shape[0], "Rows")
        return {"tensor": tensor, "rows": int(t.shape[0]), "rows_shown": [r0, r1], "labels": got["names"][r0:r1] if got["names"] else None, "labels_from": got["source"], "labels_says": got["says"],
                "extra_row": got["extra_row"], "sure": got["sure"]}
    return S._do(ctx, sid, fn, watch="record")


def nearest(ctx, sid: str, tensor: str, row=0, top: int = 10, by: str | None = None, watch: bool = False) -> dict:
    """The rows of a table that point the same way as one chosen row, by cosine."""
    def fn(st):
        t = _matrix(st, tensor).astype(float)
        try:
            j, n = int(row), max(1, min(int(top), 100))
        except (TypeError, ValueError):
            raise WeightsError("A row and a count are whole numbers.") from None
        if not 0 <= j < t.shape[0]:
            raise WeightsError(f"{tensor} has {t.shape[0]:,} rows, counted from 0. Row {j} is not one of them.")
        size = np.linalg.norm(t, axis=1)
        safe = np.where(size == 0, 1.0, size)
        cos = (t @ t[j]) / (safe * (size[j] or 1.0))
        cos[size == 0] = 0.0
        order = [int(i) for i in np.argsort(-cos, kind="stable") if int(i) != j]
        got = _labels(st.manifest, tensor, t, st.cur)
        names, says = got["names"], got["says"]
        one = lambda i: {"row": i, "label": names[i] if names else None, "cosine": float(cos[i]), "norm": float(size[i])}  # noqa: E731
        if not st.watch:
            st.event("read", f"{S._Who(st, by)} looked for the rows of {tensor} nearest to row {j}.", by, op="nearest", tensor=tensor,
                     args={"row": j, "top": n})
        return {"tensor": tensor, "rows": int(t.shape[0]), "width": int(t.shape[1]), "row": j, "label": names[j] if names else None,
                "norm": float(size[j]), "nearest": [one(i) for i in order[:n]], "farthest": [one(i) for i in order[::-1][:min(3, len(order))]],
                "labels_says": says, "says": "Cosine is 1 for rows that point the same way, 0 for unrelated rows, and below 0 for opposite ones."}
    return S._do(ctx, sid, fn, watch)


def directions(ctx, sid: str, tensor: str, top: int = 5, by: str | None = None, watch: bool = False) -> dict:
    """A table's largest directions (its singular values), and the rows that lean on each the most."""
    def fn(st):
        t = _matrix(st, tensor).astype(float)
        try:
            n = max(1, min(int(top), 32))
        except (TypeError, ValueError):
            raise WeightsError("The number of directions is a whole number.") from None
        u, s, _ = np.linalg.svd(t, full_matrices=False)
        power = s ** 2
        total = float(power.sum()) or 1.0
        share = power / total
        got = _labels(st.manifest, tensor, t, st.cur)
        names, says = got["names"], got["says"]
        out = []
        for i in range(min(n, len(s))):
            amount = u[:, i] * s[i]
            strongest = np.argsort(-np.abs(amount), kind="stable")[:5]
            out.append({"direction": i, "size": float(s[i]), "share": float(share[i]),
                        "strongest_rows": [{"row": int(r), "label": names[int(r)] if names else None, "amount": float(amount[r])} for r in strongest]})
        if not st.watch:
            st.event("read", f"{S._Who(st, by)} looked at the largest directions of {tensor}.", by, op="directions", tensor=tensor, args={"top": n})
        return {"tensor": tensor, "shape": list(t.shape), "singular_values": [float(v) for v in s[:64]], "share": [float(v) for v in share[:64]],
                "needed_for_90": int(np.searchsorted(np.cumsum(share), 0.9) + 1), "of": int(len(s)), "directions": out, "labels_says": says,
                "says": "Each direction is one pattern the rows share. Its share is how much of the table's total size it carries."}
    return S._do(ctx, sid, fn, watch)


def _top(mat: np.ndarray, n: int = 5) -> list[float]:
    return [float(v) for v in np.linalg.svd(mat, compute_uv=False)[:n]]


def attention(ctx, sid: str, layer=0, head=None, heads=None, matrix: str | None = None, by: str | None = None, watch: bool = False) -> dict:
    """What each head of one attention block looks for and what it copies, composed from the matrices alone.

    For head h with query, key and value rows Q, K, V and output columns O:
      query_key     Q^T K: how much an event looking with vector x attends to an event offering vector y is x (Q^T K) y
      output_value  O V: what is written back when an event with vector y is looked at is (O V) y
    `reads` is how strongly each embedding table's rows come through each of those. The model is never run."""
    def fn(st):
        if not is_real(st.manifest):
            raise WeightsError("This network has no attention. It is fully connected.")
        d = describe(st.manifest)
        try:
            k = int(layer)
        except (TypeError, ValueError):
            raise WeightsError("A layer is a whole number, counted from 0 here.") from None
        blocks = [p for p in d["parts"] if p["kind"] == "attention"]
        block = next((p for p in blocks if p["layer"] == k), None)
        if block is None:
            raise WeightsError(f"This model has {S._plural(len(blocks), 'attention block')}. The layer asked for is not one of them.")
        width = block["width"]
        try:
            n_heads = int(heads) if heads not in (None, "") else d["heads"]
        except (TypeError, ValueError):
            raise WeightsError("The number of heads is a whole number.") from None
        if not n_heads:
            raise WeightsError("The files do not say how many heads this model has, and the shapes cannot tell. Give the number: --heads 2.")
        if n_heads < 1 or width % n_heads:
            raise WeightsError(f"The model's width is {width}, which does not split evenly into {n_heads} heads.")
        hw = width // n_heads
        a = block["id"]
        packed = st.cur[f"{a}.in_proj_weight"].astype(float)
        q, key, v = packed[:width], packed[width:2 * width], packed[2 * width:3 * width]
        o = st.cur[f"{a}.out_proj.weight"].astype(float) if f"{a}.out_proj.weight" in st.cur else np.eye(width)
        tables = [(p, st.cur[p["tensors"][0]].astype(float)) for p in d["parts"]
                  if p["kind"] == "embedding" and p["side"] == "session" and p["width"] == packed.shape[1]]
        try:
            only = None if head in (None, "") else int(head)
        except (TypeError, ValueError):
            raise WeightsError("A head is a whole number, counted from 0 here.") from None
        if only is not None and not 0 <= only < n_heads:
            raise WeightsError(f"This block has {n_heads} heads, counted from 0. Head {only} is not one of them.")
        which = None if matrix in (None, "") else str(matrix).replace("-", "_")
        which = {"qk": "query_key", "ov": "output_value"}.get(which, which)
        if which not in (None, "query_key", "output_value"):
            raise WeightsError("The matrix to send is query_key or output_value.")
        if which and only is None:
            raise WeightsError("Name one head to get its matrix.")
        rms = lambda m: float(np.sqrt((np.linalg.norm(m, axis=1) ** 2).mean())) if len(m) else 0.0  # noqa: E731
        out_heads = []
        for h in ([only] if only is not None else range(n_heads)):
            sl = slice(h * hw, (h + 1) * hw)
            qk, ov = q[sl].T @ key[sl], o[:, sl] @ v[sl]
            reads = []
            for p, e in tables:
                through = e @ ov.T
                per_row = np.linalg.norm(through, axis=1)
                names = _labels(st.manifest, p["tensors"][0], e)["names"]
                reads.append({"table": p["id"], "name": p["name"], "rows": int(e.shape[0]), "row_size": rms(e),
                              "query": rms(e @ q[sl].T), "key": rms(e @ key[sl].T), "value": rms(through),
                              "strongest": [{"row": int(r), "label": names[int(r)] if names else None, "value": float(per_row[r])}
                                            for r in np.argsort(-per_row, kind="stable")[:3]]})
            one = {"head": h, "head_width": hw,
                   "query_key": {"shape": list(qk.shape), "size": float(np.linalg.norm(qk)), "top": _top(qk), "rank_at_most": hw},
                   "output_value": {"shape": list(ov.shape), "size": float(np.linalg.norm(ov)), "top": _top(ov), "rank_at_most": hw},
                   "reads": reads}
            if which:
                one["matrix"] = {"which": which, "grid": (qk if which == "query_key" else ov).tolist(),
                                 "scale": float(np.abs(qk if which == "query_key" else ov).max())}
            out_heads.append(one)
        if not st.watch:
            st.event("read", f"{S._Who(st, by)} looked at what the {S._plural(n_heads, 'head')} of attention layer {k + 1} look for and copy.",
                     by, op="attention", tensor=f"{a}.in_proj_weight", args={"layer": k, "head": only, "heads": n_heads, "matrix": which})
        return {"layer": k, "part": a, "width": width, "heads": n_heads,
                "heads_from": "given with the request" if heads not in (None, "") else d["heads_from"], "head_width": hw, "of": out_heads,
                "says": "query_key says which pairs of events a head links. output_value says what it copies once linked. "
                        "`top` holds each matrix's largest singular values and `size` its overall size.",
                "reads_says": "For each table: the typical size of a row seen as a query, as a key, and after being copied (value). "
                              "Composed from the matrices. The model was not run, and the norms and biases between are left out"
                              + ("." if k == 0 else ", as are the layers before this one: this is the direct path only."),
                "run": False}
    return S._do(ctx, sid, fn, watch)


# ---- changing it: exact patches, never measured ---------------------------------------------------------------------------------

def _positions(req: dict, tensor: str, t: np.ndarray) -> np.ndarray:
    """The numbers an edit names, as positions in the flattened tensor: `indices`, whole rows (`neurons`), or
    `rows` and `cols` ranges. With none of them, the whole tensor."""
    if req.get("layer") is not None or req.get("neuron") is not None:
        raise WeightsError("This model has parts, not numbered layers. Name a tensor, with rows and columns or with positions.")
    if req.get("indices") is not None:
        try:
            idx = [[int(v) for v in (ix if isinstance(ix, (list, tuple)) else [ix])] for ix in req["indices"]]
        except (TypeError, ValueError):
            raise WeightsError("Positions are whole numbers, counted from 0.") from None
        if not idx or any(len(ix) != max(t.ndim, 1) or any(not 0 <= v < s for v, s in zip(ix, t.shape or (1,))) for ix in idx):
            raise WeightsError(f"{tensor} has the shape {list(t.shape) or 'of a single number'}. A position names one number inside it, counted from 0.")
        flat = np.ravel_multi_index(np.asarray(idx).T, t.shape) if t.ndim else np.zeros(len(idx), dtype=np.int64)
    elif t.ndim == 2:
        if req.get("neurons") is not None:
            try:
                rs = sorted({int(r) for r in req["neurons"]})
            except (TypeError, ValueError):
                raise WeightsError("Rows are whole numbers, counted from 0.") from None
            if not rs or not all(0 <= r < t.shape[0] for r in rs):
                raise WeightsError(f"{tensor} has {t.shape[0]:,} rows, counted from 0.")
        else:
            r0, r1 = S._span(req.get("rows"), t.shape[0], "Rows")
            rs = list(range(r0, r1))
        c0, c1 = S._span(req.get("cols"), t.shape[1], "Columns")
        flat = (np.asarray(rs)[:, None] * t.shape[1] + np.arange(c0, c1)[None, :]).reshape(-1)
    else:
        if req.get("cols") not in (None, ""):
            raise WeightsError(f"{tensor} is one list of numbers. It has rows and no columns.")
        r0, r1 = S._span(req.get("rows"), max(t.size, 1), "Rows")
        flat = np.arange(r0, r1)
    flat = np.asarray(flat, dtype=np.int64)
    if len(np.unique(flat)) != len(flat):
        raise WeightsError("An edit names each number once.")
    if len(flat) > READ_CAP:
        raise WeightsError(f"That is {len(flat):,} numbers. One edit changes at most {READ_CAP:,}.")
    return flat


def _amounts(req: dict, n: int, what: str) -> np.ndarray:
    vals = req.get("values") if req.get("values") is not None else req.get("value")
    if vals is None:
        raise WeightsError(f"Give {what}: `value` for all of them, or `values` with one number each.")
    try:
        v = np.asarray(vals, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        raise WeightsError("The numbers to write are numbers.") from None
    if len(v) not in (1, n):
        raise WeightsError(f"This edit names {n:,} numbers and gives {len(v):,} values. Give one for all, or one each.")
    return np.broadcast_to(v, (n,))


def _listed(t: np.ndarray, tensor: str, part: dict, frm: str, to: str) -> list[dict]:
    out = []
    for f, a, b in list(zip(part["indices"], part[frm], part[to]))[:S.LIST_CAP]:
        at = [int(v) for v in np.unravel_index(int(f), t.shape)] if t.ndim else []
        out.append({"tensor": tensor, "index": at, "old": a, "new": b})
    return out


def edit(ctx, sid: str, req: dict, by: str | None = None) -> dict:
    """Change numbers of one tensor: set, scale, zero or add. The edit is an exact patch, so it can be taken back to
    the bit. It is not measured: this kind cannot be run here yet."""
    if not isinstance(req, dict):
        raise WeightsError("An edit is an object with `op` and what to change.")
    by = by if by is not None else req.get("by")

    def fn(st):
        st.open_for_changes()
        why = str(req.get("why") or req.get("hypothesis") or "").strip()
        if S._by(by, st.who) == "agent" and not why:
            raise WeightsError("An agent's edit needs `why`: one sentence saying what it expects the edit to do, given before the edit.")
        op = str(req.get("op") or "").replace("-", "_")
        if op not in EDIT_OPS:
            raise WeightsError(f"The edits this model takes are: {', '.join(EDIT_OPS)}.")
        tensor = req.get("tensor")
        t = _tensor(st, tensor)
        if t.dtype != np.float32:
            raise WeightsError(f"{tensor} is stored as {t.dtype} numbers. Edits here are exact for float32 only, so it is left alone.")
        flat = _positions(req, tensor, t)
        cur = t.reshape(-1)[flat]
        with np.errstate(over="ignore", invalid="ignore"):    # a number too large for float32 becomes infinite, and is refused below
            if op == "zero":
                new = np.zeros(len(flat), dtype=np.float32)
            elif op == "scale":
                try:
                    new = (cur * np.float32(float(req["factor"]))).astype(np.float32)
                except (KeyError, TypeError, ValueError):
                    raise WeightsError("To scale, give `factor`: the number to multiply by.") from None
            elif op == "set":
                new = _amounts(req, len(flat), "the number to write").astype(np.float32)
            else:
                new = (cur + _amounts(req, len(flat), "the amount to add").astype(np.float32)).astype(np.float32)
        if not np.isfinite(new).all():
            raise WeightsError("This edit would write a number that is not finite.")
        moved = cur != new
        if not moved.any():
            raise WeightsError("This edit changes nothing: every number it names already has that value.")
        part = {"tensor": tensor, "indices": flat[moved].tolist(), "old": cur[moved].astype(float).tolist(), "new": new[moved].astype(float).tolist()}
        n = len(st.doc["edits"]) + 1
        key = f"patches/{n}-{S.secrets.token_hex(3)}.json"
        patch = {"n": n, "parts": [part]}
        st.new_patches[key] = patch
        st._put(patch, "new")
        changed = len(part["indices"])
        verb = {"set": "set", "scale": "scaled", "zero": "set to zero", "add": "added to"}[op]
        ev = st.event("edit", f"{S._Who(st, by)} {verb} {S._plural(changed, 'number')} in {tensor}. {NOT_MEASURED}", by, op=op, tensor=tensor,
                      tensors=[tensor], factor=float(req["factor"]) if op == "scale" else None, why=why,
                      args={k: v for k, v in req.items() if k not in ("why", "hypothesis", "by", "values", "indices")},
                      changes=_listed(t, tensor, part, "old", "new"), changed=changed, share=round(changed / st.total(), 6),
                      undone=False, measured=False, not_measured=NOT_MEASURED)
        st.doc["edits"].append({"n": n, "event": ev["id"], "op": op, "tensors": [tensor], "why": why, "by": ev["by"], "state": "applied",
                                "changed": changed, "patch": key})
        st.doc["actions"].append(["apply", n])
        ev["ledger"] = {"changed": st.changed(), "total": st.total()}
        return st.show(ev)
    return S._do(ctx, sid, fn)


def _flip(ctx, sid: str, edit_id, by, kind: str) -> dict:
    def fn(st):
        st.open_for_changes()
        e = S._pick(st, edit_id, "applied" if kind == "undo" else "undone")
        patch = st.patch(e["n"])
        if kind == "undo":
            later = [o for o in st.doc["edits"] if o["state"] == "applied" and o is not e
                     and S._last_apply(st, o["n"]) > S._last_apply(st, e["n"]) and S._overlap(st.patch(o["n"]), patch)]
            if later:
                raise WeightsError(f"Take back edit {later[0]['event']} first: it changed the same numbers after edit {e['event']}.")
        else:
            for part in patch["parts"]:
                now = st.cur[part["tensor"]].reshape(-1)[np.asarray(part["indices"], dtype=np.int64)]
                if not np.array_equal(now, np.asarray(part["old"], dtype=np.float32)):
                    raise WeightsError(f"Edit {e['event']} cannot be put back: the numbers it started from have changed since.")
        frm, to = ("new", "old") if kind == "undo" else ("old", "new")
        st._put(patch, to)
        e["state"] = "undone" if kind == "undo" else "applied"
        st.doc["actions"].append([kind, e["n"]])
        for ev in st.doc["events"]:
            if ev["id"] == e["event"]:
                ev["undone"] = kind == "undo"
        part = patch["parts"][0]
        words = "took back" if kind == "undo" else "put back"
        ev = st.event(kind, f"{S._Who(st, by)} {words} edit {e['event']}: {S._plural(e['changed'], 'number')} in {part['tensor']}. {NOT_MEASURED}",
                      by, op=e["op"], **{"undoes" if kind == "undo" else "redoes": e["event"]}, tensor=part["tensor"], tensors=e["tensors"],
                      changes=_listed(st.cur[part["tensor"]], part["tensor"], part, frm, to), changed=e["changed"], measured=False,
                      not_measured=NOT_MEASURED)
        ev["ledger"] = {"changed": st.changed(), "total": st.total()}
        return st.show(ev)
    return S._do(ctx, sid, fn)


def undo(ctx, sid: str, edit_id=None, by: str | None = None) -> dict:
    return _flip(ctx, sid, edit_id, by, "undo")


def redo(ctx, sid: str, edit_id=None, by: str | None = None) -> dict:
    return _flip(ctx, sid, edit_id, by, "redo")


def _diff(st, listed: int = S.LIST_CAP) -> dict:
    by_tensor, changes = [], []
    changed = total = 0
    for name, b in st.base.items():
        c = st.cur[name]
        moved = np.flatnonzero((b != c).reshape(-1))
        total += int(b.size)
        changed += len(moved)
        if not len(moved):
            continue
        by_tensor.append({"tensor": name, "changed": len(moved), "total": int(b.size), "share": round(len(moved) / b.size, 6),
                          "largest_change": float(np.abs(c.astype(float) - b.astype(float)).max())})
        for f in moved[:max(0, listed - len(changes))]:
            changes.append({"tensor": name, "index": [int(v) for v in np.unravel_index(int(f), b.shape)] if b.ndim else [],
                            "old": float(b.reshape(-1)[f]), "new": float(c.reshape(-1)[f])})
    return {"base": WS.ref_of(st.model, st.doc["version"]), "changed": changed, "total": total,
            "share": round(changed / total, 6) if total else 0.0, "by_tensor": by_tensor, "changes": changes}


def diff(ctx, sid: str, by: str | None = None, watch: bool = False) -> dict:
    def fn(st):
        out = _diff(st)
        if not st.watch:
            st.event("read", f"{S._Who(st, by)} compared the model with its base: {out['changed']:,} of {out['total']:,} numbers differ.", by, op="diff")
        return out
    return S._do(ctx, sid, fn, watch)


def report(ctx, sid: str) -> dict:
    def fn(st):
        out = S._session_out(st, events=0)
        out["events"] = [st.show(e) for e in st.doc["events"]]
        out["passes"] = st.doc["used"]["passes"]
        out["diff"] = _diff(st, listed=0)
        out["kept"] = [e for e in out["edits"] if e["state"] == "applied"]
        out["taken_back"] = [e for e in out["edits"] if e["state"] != "applied"]
        return out
    return S._do(ctx, sid, fn, watch="record")


def save(ctx, sid: str, note: str = "", by: str | None = None) -> dict:
    """Save the model as it is now as a new version: its base plus the changed numbers, with the session's record.
    The new version keeps no files of its own. Nothing is overwritten."""
    def fn(st):
        patches = []
        for name in st.base:
            moved = np.flatnonzero((st.base[name] != st.cur[name]).reshape(-1))
            if len(moved):
                patches.append({"tensor": name, "indices": moved.tolist(), "old": st.base[name].reshape(-1)[moved].astype(float).tolist(),
                                "new": st.cur[name].reshape(-1)[moved].astype(float).tolist()})
        if not patches:
            raise WeightsError("There is nothing to save: the model is the same as its base.")
        led = S.ledger(st)
        record = {"session": st.sid, "who": st.doc["who"], "opened_at": st.doc["opened_at"], "ledger": led, "edits": S._edits_out(st),
                  "passes": [], "measured": False, "not_measured": NOT_MEASURED,
                  "events": [{k: v for k, v in e.items() if k != "changes"} for e in st.doc["events"]]}
        base_ref = WS.ref_of(st.model, st.doc["version"])
        base = WS.manifest(ctx, st.model, st.doc["version"])
        keep = {k: v for k, v in base.items() if k not in ("version", "created_at", "created_by", "sha256", "patches", "record", "note", "base")}
        m = WS.save_packed(ctx, st.model, {**keep, "base": base_ref, "files_at": base.get("files_at") or base_ref, "patches": patches,
                                           "record": record, "note": str(note or ""), "sha256": WS.digest(st.cur)}, None)
        ref = WS.ref_of(st.model, m["version"])
        st.doc["saved"].append(ref)
        st.event("save", f"{S._Who(st, by)} saved the model as {ref}: {base_ref} plus {S._plural(led['changed'], 'changed number')}. {NOT_MEASURED}",
                 by, op="save", ref=ref, args={"note": note})
        return {"model": st.model, "version": m["version"], "ref": ref, "base": base_ref,
                "patches": sum(1 for e in st.doc["edits"] if e["state"] == "applied"), "changed": led["changed"], "share": led["share"],
                "label": led["label"], "weights_alone": led["weights_alone"], "note": str(note or ""), "scores_shown_now": False,
                "measured": False, "not_measured": NOT_MEASURED, "sha256": m["sha256"]}
    return S._do(ctx, sid, fn)


OPS = {"layers": layers, "session_layers": _no_layers, "read": read, "stats": stats, "heatmap": heatmap, "edit": edit, "undo": undo,
       "redo": redo, "diff": diff, "report": report, "save": save}


# ---- the tree rankers ------------------------------------------------------------------------------------------------------------

def _ranker(ctx, m: dict, pick: dict, shown: int) -> dict:
    parsed = T.parse(_file(ctx, m, pick["file"]).decode(errors="replace"))
    named = T.names_from(_file(ctx, m, pick["cols"]), parsed["inputs"]) if pick.get("cols") else []
    return T.summary(parsed, named or None, shown)


def rankers(ctx, model: str) -> dict:
    """A model's tree rankers side by side: which inputs each one asks about, and which none of them does."""
    name, version = WS.split_ref(model)
    m = WS.manifest(ctx, name, version)
    have = (m.get("rankers") or []) if is_real(m) else []
    if not have:
        raise WeightsError(f"{name} has no tree rankers. They come with a session model brought in from its training folder.")
    each = {r["name"]: _ranker(ctx, m, r, 0) for r in have}
    order: list[str] = []
    for s in each.values():
        order += [i["name"] for i in sorted(s["by_input"], key=lambda i: i["input"]) if i["name"] not in order]
    rows = []
    for nm in order:
        by = {}
        for rn, s in each.items():
            hit = next((i for i in s["by_input"] if i["name"] == nm), None)
            by[rn] = None if hit is None else {"splits": hit["splits"], "gain": hit["gain"], "share": hit["share"]}
        used = [rn for rn, v in by.items() if v and v["splits"]]
        rows.append({"name": nm, "group": T.group_of(nm), "by": by, "used_by": used, "unused_by": [rn for rn in by if rn not in used]})
    rows.sort(key=lambda r: -max((v["share"] for v in r["by"].values() if v), default=0.0))
    none = [r["name"] for r in rows if not r["used_by"]]
    every = [r["name"] for r in rows if len(r["used_by"]) == len(each)]
    names = list(each)
    said = ", ".join(none[:8]) + (f" and {len(none) - 8} more" if len(none) > 8 else "")
    return {"model": name, "version": m["version"], "says": T.SAYS,
            "rankers": [{"name": rn, "objective": s["objective"], "trees": s["trees"], "inputs": s["inputs"],
                         "used": s["inputs"] - len(s["unused"]), "unused": [u["name"] for u in s["unused"]]} for rn, s in each.items()],
            "inputs": rows, "used_by_all": every, "used_by_some": [r["name"] for r in rows if 0 < len(r["used_by"]) < len(each)],
            "unused_by_all": none,
            "unused_by_all_says": (f"{len(none)} of {len(rows)} inputs are asked about by none of the {S._plural(len(each), 'ranker')} "
                                   f"({', '.join(names)}), so they change no answer: {said}." if none
                                   else f"Every input is asked about by at least one of the {S._plural(len(each), 'ranker')}."),
            "same_inputs": all(s["inputs"] == len(rows) for s in each.values())}


# ---- what can be brought in from a folder of versions ----------------------------------------------------------------------------

VERSIONS_MOST = 500
DESCRIBED_MOST = 200
ENDS = tuple("weights/" + n for n in SESSION_FILES)


def _one_line(raw: bytes | None) -> str | None:
    """A version's own one-line description, from its MANIFEST.yaml."""
    if raw is None:
        return None
    try:
        import yaml

        got = yaml.safe_load(raw.decode())
    except Exception:  # noqa: BLE001  a manifest that cannot be parsed has no description
        return None
    return _line_of(got)


def _line_of(got) -> str | None:
    """One line about a version, from its manifest as a mapping."""
    if not isinstance(got, dict):
        return None
    for k in ("description", "summary", "says", "title", "notes", "what_changed"):
        if isinstance(got.get(k), str) and got[k].strip():
            return " ".join(got[k].split())[:200]
    scalars = [f"{k}: {v}" for k, v in got.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)][:4]
    return ", ".join(scalars)[:200] or None


NO_SESSION_FILE = "This version holds no session model file."


def _where(address) -> str:
    """An address or folder as it is compared: no trailing slash, and without a trailing `weights` folder."""
    a = str(address or "").rstrip("/")
    return a[: -len("/weights")] if a.endswith("/weights") else a


def _brought_in(ctx) -> dict[str, str]:
    """Where each model of this workspace was brought in from: {address: the model's name here}."""
    out: dict[str, str] = {}
    for m in WS.models(ctx):
        try:
            src = (WS.manifest(ctx, m["id"]).get("source") or {}).get("from")
        except Exception:  # noqa: BLE001  a model that cannot be read was not brought in from anywhere we can name
            continue
        if src:
            out.setdefault(_where(src), m["id"])
    return out


def _made(man: dict) -> str | None:
    """The day a version was made, when its manifest says: used to put the newest first."""
    for k in ("created_at", "created", "trained_at", "date", "updated"):
        v = man.get(k) if isinstance(man, dict) else None
        if v:
            return str(v)[:10]
    return None


def attached(ctx, only: str | None = None) -> dict:
    """What can be brought in from the model registries attached to this workspace, grouped by registry. It reads
    the copy of each registry's manifests and file listing that Captain already keeps (models.py), so nothing is
    listed in storage a second time. Each version says whether it can be opened here (it holds a session model
    file) and whether it is already brought in, and as what. A registry is named in words, never by its address."""
    from .. import attach as AT
    from .. import models as MD

    regs = AT.registries(ctx)
    if only not in (None, ""):
        regs = [r for r in regs if r.get("id") == only]
        if not regs:
            raise WeightsError("No model registry with that id is attached to this workspace.")
    here = _brought_in(ctx)
    out, total, openable = [], 0, 0
    for n, r in enumerate(regs):
        snap = MD.latest_for(ctx, r["uri"])
        # A label the person gave is the registry's name. The default label is its address, which is not shown.
        given = r.get("label") if r.get("label") and r["label"] != str(r["uri"]).split("://", 1)[-1] else None
        name = given or ("Model registry" if len(regs) == 1 else f"Model registry {n + 1}")
        rows = []
        for m in (snap.models if snap else []):
            files = {f.get("path") for f in m.get("files") or []}
            can = any(e in files for e in ENDS)
            man = m.get("manifest") or {}
            address = _where(m.get("path"))
            rows.append({"id": m.get("id"), "name": f"{m.get('brand')}, {m.get('version')}", "brand": m.get("brand"), "architecture": m.get("architecture"),
                         "version": m.get("version"), "path": f"{m.get('architecture')}/{m.get('brand')}/{m.get('version')}", "address": address,
                         "description": _line_of(man), "made": _made(man), "files": len(files), "can_open": can,
                         "why_not": None if can else NO_SESSION_FILE, "here": here.get(address)})
        # Newest first: by the day its manifest gives, then by its version label.
        rows.sort(key=lambda v: (v["made"] or "", MD._vkey(str(v["version"]))), reverse=True)
        total += len(rows)
        openable += sum(1 for v in rows if v["can_open"])
        out.append({"id": r.get("id"), "name": name, "read": snap is not None, "versions": rows, "count": len(rows),
                    "says": (f"{S._plural(len(rows), 'version')}, {sum(1 for v in rows if v['can_open'])} that can be opened here." if snap
                             else "Captain has not read this registry yet.")})
    return {"registries": out, "count": total, "can_open": openable, "read_only": True,
            "says": ("No model registry is attached to this workspace." if not out
                     else f"{S._plural(total, 'version')} in {len(out)} model {'registry' if len(out) == 1 else 'registries'}. "
                          f"{openable} can be opened here. Nothing was written there.")}


def registry(where: str) -> dict:
    """The version folders under a folder or an s3:// address that can be brought in: each one that holds
    weights/sess.npz, with the one-line description from its MANIFEST.yaml. Read only. Nothing is written there."""
    if not isinstance(where, str) or not where.strip():
        raise WeightsError("Name the folder or the s3:// address that holds the versions.")
    where = where.strip()
    if is_address(where):
        from ..storage.backend import S3Store

        u = urlparse(where)
        if not u.netloc:
            raise WeightsError("An S3 address is written s3://bucket/folder.")
        store = _cloud(lambda: S3Store(u.netloc, u.path.strip("/")))
        keys = _cloud(lambda: store.list(""))
        root = where.rstrip("/")
        paths = sorted({k[:-len(e)].rstrip("/") for k in keys for e in ENDS if k == e or k.endswith("/" + e)})
        have = set(keys)
        count = lambda p: sum(1 for k in keys if k.startswith((p + "/" if p else "") + "weights/"))  # noqa: E731
        read = lambda p: _cloud(lambda: store.get((p + "/" if p else "") + "MANIFEST.yaml")) if (p + "/" if p else "") + "MANIFEST.yaml" in have else None  # noqa: E731
    else:
        root = os.path.abspath(where)
        if not os.path.isdir(root):
            raise WeightsError(f"{where} is not a folder.")
        paths = []
        for d, subs, _files in os.walk(root):
            subs[:] = sorted(s for s in subs if not s.startswith("."))
            rel = os.path.relpath(d, root)
            if rel.count(os.sep) >= 6:
                subs[:] = []
            if _session_in(os.path.join(d, "weights")):
                paths.append("" if rel == "." else rel.replace(os.sep, "/"))
                subs[:] = []
        count = lambda p: len([f for f in os.listdir(os.path.join(root, p, "weights")) if _wanted(f)])  # noqa: E731

        def read(p):
            try:
                with open(os.path.join(root, p, "MANIFEST.yaml"), "rb") as f:
                    return f.read()
            except OSError:
                return None
    rows = []
    for i, p in enumerate(paths[:VERSIONS_MOST]):
        raw = read(p) if i < DESCRIBED_MOST else None
        rows.append({"path": p, "address": f"{root}/{p}" if p else root, "name": p.split("/")[-1] if p else root.split("/")[-1],
                     "description": _one_line(raw), "has_manifest": raw is not None, "files": count(p)})
    return {"from": root, "versions": rows, "count": len(paths), "listed": len(rows), "read_only": True,
            "says": (f"{S._plural(len(paths), 'version')} can be brought in from here." if paths
                     else "No version was found here. A version is a folder that holds weights/sess.npz or weights/enc.npz.")
                    + " Nothing was written there. Bring one in with `cst weights import <address> --as <name>`."}


def trees(ctx, model: str, ranker: str | None = None, trees: int = T.TREES_SHOWN) -> dict:
    """One of a model's tree rankers, read from its file: per input its questions and their gain, the inputs no tree
    asks about, and the first few trees to draw. A version of the model is named like `name@v2`."""
    name, version = WS.split_ref(model)
    m = WS.manifest(ctx, name, version)
    have = (m.get("rankers") or []) if is_real(m) else []
    if not have:
        raise WeightsError(f"{name} has no tree rankers. They come with a session model brought in from its training folder.")
    names = [r["name"] for r in have]
    pick = have[0] if ranker in (None, "") else next((r for r in have if r["name"] == ranker), None)
    if pick is None:
        raise WeightsError(f"{name} has no ranker named {ranker!r}. Its rankers are: {', '.join(names)}.")
    v = m["version"]
    out = _ranker(ctx, m, pick, trees)
    return {"model": name, "version": v, "ranker": pick["name"], "rankers": names, "names_from": pick.get("names_from"),
            "file": pick["file"].split("/")[-1], "sha256": ((m.get("source") or {}).get("files") or {}).get(pick["file"].split("/")[-1], {}).get("sha256"),
            **out, "cannot": TREES_CANNOT}
