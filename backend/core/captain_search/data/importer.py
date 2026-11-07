"""Bring local files into the workspace: what happens when someone drags files or folders into Captain.

Each supported file (csv, tsv, parquet, json, jsonl, ndjson) becomes a versioned dataset in the chosen folder. A folder
dragged in keeps its structure. Dropping a file whose name already exists in that folder makes a new version of that
dataset, unless the file is unchanged, which is skipped. Captain guesses each file's kind (catalog, queries, judgments,
events) from its columns, and the person can change it later.

Files over the laptop limit are refused with a reason: big data belongs in cloud storage, registered by reference.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .. import library as LIB
from ..model.entities import DatasetKind, Provenance
from ..storage.backend import NotFound
from ..storage.registry import check_name
from ..workspace import Context
from . import datasets as DS

EXTS = {".csv", ".tsv", ".parquet", ".json", ".jsonl", ".ndjson"}
MAX_BYTES = 2 * 1024**3


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-._")
    if re.fullmatch(r"v\d+", s):
        s = f"{s}-data"
    return (s or "data")[:100]


def guess_kind(columns: list[str]) -> DatasetKind:
    c = {x.lower() for x in columns}
    has = lambda *names: any(n in c for n in names)
    if has("query", "query_id", "q", "search_term") and has("product_id", "item_id", "doc_id", "sku") and has(
            "grade", "label", "relevance", "rating", "score"):
        return DatasetKind.judgments
    if has("timestamp", "ts", "event_time", "time", "event", "event_type", "session_id", "session"):
        return DatasetKind.events
    if has("query", "q", "search_term", "query_text") and len(c) <= 6 and not has("title", "name", "price"):
        return DatasetKind.queries
    if has("id", "product_id", "sku", "item_id", "objectid") and has("title", "name", "product_name", "description"):
        return DatasetKind.catalog
    return DatasetKind.generic


def _files(paths: list[str]) -> list[tuple[Path, str]]:
    """(file, relative folder under the drop) for every file in the dropped paths."""
    out = []
    for p in paths:
        p = Path(os.path.expanduser(p)).resolve()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not any(part.startswith(".") for part in f.relative_to(p).parts):
                    rel = f.parent.relative_to(p)
                    out.append((f, "/".join((p.name, *rel.parts))))
        elif p.is_file():
            out.append((p, ""))
        else:
            out.append((p, None))
    return out


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_paths(ctx: Context, paths: list[str], folder: str = "", progress=lambda f: None,
                 project: str | None = None) -> dict:
    """Returns {imported: [...], updated: [...], unchanged: [...], skipped: [{path, reason}]}."""
    folder = LIB._clean(folder)
    out = {"imported": [], "updated": [], "unchanged": [], "skipped": []}
    files = _files(paths)
    for i, (f, rel) in enumerate(files):
        progress(i / max(1, len(files)))
        if rel is None:
            out["skipped"].append({"path": str(f), "reason": "not found"})
            continue
        if f.suffix.lower() not in EXTS:
            out["skipped"].append({"path": str(f), "reason": f"{f.suffix or 'this kind of file'} isn't a table. Captain takes csv, tsv, parquet, json and jsonl"})
            continue
        size = f.stat().st_size
        if size > MAX_BYTES:
            out["skipped"].append({"path": str(f), "reason": f"{size / 1024**3:.1f} GB is too big to copy from a laptop. Put it in cloud storage and register it by reference"})
            continue
        dest = "/".join(x for x in (folder, rel) if x)
        name = check_name("/".join([*(slug(p) for p in dest.split("/") if p), slug(f.stem)])[:800])
        digest = _sha(f)
        try:
            prev = ctx.registry.load("dataset", name)
        except NotFound:
            prev = None
        if prev is not None and (prev.provenance.params or {}).get("source_sha256") == digest:
            out["unchanged"].append(f"{name}@v{prev.version}")
            LIB.move(ctx, [name], dest)
            continue
        try:
            table = DS.read_file(str(f))
        except Exception as e:  # noqa: BLE001
            out["skipped"].append({"path": str(f), "reason": f"couldn't read it as a table: {str(e).splitlines()[0][:200]}"})
            continue
        kind = prev.dataset_kind if prev is not None else guess_kind(table.column_names)
        m = DS.snapshot(ctx, name, table, kind=kind,
                        provenance=Provenance(source="file", file=str(f), params={"source_sha256": digest, "bytes": size}),
                        description=prev.description if prev is not None else f"Imported from {f.name}")
        LIB.move(ctx, [name], dest)
        (out["updated"] if prev is not None else out["imported"]).append(
            {"ref": f"{m.name}@v{m.version}", "folder": dest, "kind": kind.value, "rows": m.row_count, "file": f.name})
    if project:
        from .. import projects as PR

        names = [x["ref"].split("@")[0] for x in out["imported"] + out["updated"]] + [u.split("@")[0] for u in out["unchanged"]]
        if names:
            PR.assign(ctx, project, [{"kind": "dataset", "name": n} for n in names])
    progress(1.0)
    return out
