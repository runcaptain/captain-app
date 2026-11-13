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
NOT_YET = "This model can be read here, not changed or run yet."
FILES = "files"
SESSION_FILE = "sess.npz"
READ_CAP = 200_000       # the most numbers one read or one heatmap sends
FACT_CAP = 5000          # a fact longer than this keeps its shape and not its values
ROWS_CAP = 4096
CANNOT = ("template", "forward", "activations", "gradients", "trace", "cost_slice", "set_sets", "evaluate", "edit", "undo", "redo",
          "save", "export")
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


class _Folder:
    def __init__(self, where: str):
        self.where = os.path.abspath(where)
        inner = os.path.join(self.where, "weights")
        self.dir = inner if os.path.isfile(os.path.join(inner, SESSION_FILE)) else self.where
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
    return any(os.path.isfile(os.path.join(where, *p)) for p in ((SESSION_FILE,), ("weights", SESSION_FILE)))


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
    return a.dtype.kind == "f" and (name.endswith((".weight", ".bias", "in_proj_weight", "in_proj_bias")) or name == "logit_scale")


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
    sess_name = SESSION_FILE if SESSION_FILE in names else None
    if sess_name is None:
        raise WeightsError(f"There is no {SESSION_FILE} at {where}, or in a weights folder inside it. That file holds the session model.")
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


def load(ctx, m: dict) -> tuple[dict, dict[str, np.ndarray]]:
    """A version's manifest and its tensors, each in the number type it was saved with."""
    out: dict[str, np.ndarray] = {}
    for fname in sorted({t["file"] for t in m["tensors"].values()}):
        got, _ = _arrays(ctx.store.get(WS._key(m["name"], m["version"], fname)), fname)
        for k, t in m["tensors"].items():
            if t["file"] == fname:
                if k not in got:
                    raise WeightsError(f"The stored file {fname} no longer holds the tensor {k!r}.")
                out[k] = got[k].copy()
    if m.get("sha256") and WS.digest(out) != m["sha256"]:
        raise WeightsError(f"{WS.ref_of(m['name'], m['version'])} does not match the numbers recorded when it was brought in.")
    return m, out


# ---- what the model is, from its shapes -------------------------------------------------------------------------------------------

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
             "shape": list(sh[tensors[0]]) if tensors else [], "parameters": count(*tensors), "feeds_from": feeds, **more}
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
                         "layers_agree": None if declared_layers is None else declared_layers == len(ids)},
            "parts": parts, "wiring_says": WIRING, "facts": facts,
            "rankers": [{**r, "says": T.SAYS} for r in m.get("rankers") or []], "tables": m.get("tables") or [],
            "can": {"read": True, "edit": False, "run": False, "deploy": False, "says": NOT_YET}}


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
           "base": None, "cost": None, "sha256": m.get("sha256"),
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
            out["rows"] = [{"row": j, "norm": float(norm[j]), "mean": float(t[j].mean()), "min": float(t[j].min()), "max": float(t[j].max()),
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


def _labels(m: dict, tensor: str, rows: int) -> tuple[list[str] | None, str]:
    """Names for a table's rows, when a list saved beside the arrays has exactly one name per row."""
    stem = tensor.rsplit(".", 1)[0]
    key = (stem[2:] if stem.startswith("c_") else stem).split(".")[0] + "_vocab"
    f = (m.get("facts") or {}).get(key)
    if not f or f.get("kind") != "text" or not isinstance(f.get("value"), list):
        return None, ""
    if len(f["value"]) == rows:
        return f["value"], f"Rows are named from {key}, saved beside the arrays."
    return None, f"{key} lists {len(f['value']):,} names for {rows:,} rows, so which row has which name is not certain. Rows are numbered."


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
        names, says = _labels(st.manifest, tensor, t.shape[0])
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
        names, says = _labels(st.manifest, tensor, t.shape[0])
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
                names, _ = _labels(st.manifest, p["tensors"][0], e.shape[0])
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


OPS = {"layers": layers, "session_layers": _no_layers, "read": read, "stats": stats, "heatmap": heatmap}


# ---- the tree rankers ------------------------------------------------------------------------------------------------------------

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
    parsed = T.parse(ctx.store.get(WS._key(name, v, pick["file"])).decode(errors="replace"))
    named = T.names_from(ctx.store.get(WS._key(name, v, pick["cols"])), parsed["inputs"]) if pick.get("cols") else []
    out = T.summary(parsed, named or None, trees)
    return {"model": name, "version": v, "ranker": pick["name"], "rankers": names, "names_from": pick.get("names_from"),
            "file": pick["file"].split("/")[-1], "sha256": ((m.get("source") or {}).get("files") or {}).get(pick["file"].split("/")[-1], {}).get("sha256"),
            **out, "cannot": NOT_YET}
