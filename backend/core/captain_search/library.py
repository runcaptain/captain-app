"""The library: how a person organizes their workspace's datasets into folders, like a file browser.

Datasets are write-once and keep their names. Where each one shows in the folder tree is separate, mutable
organization stored in the workspace's own storage (`library.yaml`, updated by compare-and-swap, so two people
moving things at once never lose each other's change). Moving a dataset or renaming a folder copies no data and
touches no versions.

A dataset nobody has placed shows under the folder its name implies (`catalogs/acme` under `catalogs/`), so
workspaces made before the library existed look the same as before.
"""
from __future__ import annotations

import re

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context

KEY = "library.yaml"
SEG = re.compile(r"^[^/\x00]{1,120}$")


def _clean(folder: str | None) -> str:
    f = "/".join(p.strip() for p in str(folder or "").strip("/").split("/") if p.strip())
    for p in f.split("/") if f else []:
        if not SEG.match(p) or p in (".", ".."):
            raise ValueError(f"{p!r} can't be a folder name")
    return f


def _read(ctx: Context) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(KEY)
        doc = yaml.safe_load(body) or {}
    except NotFound:
        doc, tag = {}, None
    doc.setdefault("folders", [])
    doc.setdefault("placed", {})  # dataset name -> folder path ("" = top level)
    return doc, tag


def _update(ctx: Context, change) -> dict:
    for _ in range(10):
        doc, tag = _read(ctx)
        change(doc)
        doc["folders"] = sorted(set(doc["folders"]))
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=True).encode(), tag)
            return doc
        except Conflict:
            continue
    raise Conflict("the library kept changing while saving. Try again.")


def folder_of(doc: dict, name: str) -> str:
    if name in doc["placed"]:
        return doc["placed"][name]
    return name.rsplit("/", 1)[0] if "/" in name else ""


def make_folder(ctx: Context, path: str) -> str:
    path = _clean(path)
    if not path:
        raise ValueError("name the folder")

    def ch(doc):
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            doc["folders"].append("/".join(parts[:i]))

    _update(ctx, ch)
    return path


def move(ctx: Context, names: list[str], folder: str) -> dict:
    folder = _clean(folder)
    known = set(ctx.registry.names("dataset"))
    missing = [n for n in names if n not in known]
    if missing:
        raise NotFound(f"no dataset named {', '.join(missing)}")

    def ch(doc):
        for n in names:
            doc["placed"][n] = folder
        if folder:
            parts = folder.split("/")
            for i in range(1, len(parts) + 1):
                doc["folders"].append("/".join(parts[:i]))

    _update(ctx, ch)
    return {"moved": names, "folder": folder}


def rename_folder(ctx: Context, old: str, new: str) -> dict:
    old, new = _clean(old), _clean(new)
    if not old or not new:
        raise ValueError("name both folders")
    if new == old or new.startswith(old + "/"):
        raise ValueError("a folder can't move inside itself")
    names = ctx.registry.names("dataset")

    def ch(doc):
        sub = lambda f: new + f[len(old):] if f == old or f.startswith(old + "/") else f
        doc["folders"] = [sub(f) for f in doc["folders"]] + [new]
        for n in names:
            f = folder_of(doc, n)
            if f == old or f.startswith(old + "/"):
                doc["placed"][n] = sub(f)

    _update(ctx, ch)
    return {"from": old, "to": new}


def remove_folder(ctx: Context, path: str) -> dict:
    """Only an empty folder can be removed. Datasets are never deleted from here."""
    path = _clean(path)
    doc, _ = _read(ctx)
    inside = [n for n in ctx.registry.names("dataset") if (lambda f: f == path or f.startswith(path + "/"))(folder_of(doc, n))]
    if inside:
        raise ValueError(f"{path} isn't empty: it holds {len(inside)} dataset(s). Move them out first.")
    _update(ctx, lambda d: d.__setitem__("folders", [f for f in d["folders"] if f != path and not f.startswith(path + "/")]))
    return {"removed": path}


def tree(ctx: Context, entry) -> dict:
    """The folder tree. entry(name) -> the dataset's row, or None to leave it out."""
    doc, _ = _read(ctx)
    root = {"name": "", "path": "", "folders": [], "datasets": []}
    index = {"": root}

    def node(path: str) -> dict:
        if path in index:
            return index[path]
        parent, _, leaf = path.rpartition("/")
        n = {"name": leaf, "path": path, "folders": [], "datasets": []}
        node(parent)["folders"].append(n)
        index[path] = n
        return n

    for f in doc["folders"]:
        node(f)
    for name in ctx.registry.names("dataset"):
        row = entry(name)
        if row is not None:
            node(folder_of(doc, name))["datasets"].append(row)

    def sort(n):
        n["folders"].sort(key=lambda x: x["name"].lower())
        n["datasets"].sort(key=lambda x: x["leaf"].lower())
        for c in n["folders"]:
            sort(c)

    sort(root)
    return root
