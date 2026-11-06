"""Notebooks: nbformat 4 files with `metadata.captain` on each cell, write-once revisions in the
bucket, local drafts for autosave, and a Jupyter kernel per open notebook.

SQL cells run inside the kernel (through captain_search.notebook.helpers), so their result is a
pandas DataFrame in the same namespace as the Python cells.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import time

from ..model.entities import FileEntry, NotebookRevision
from ..storage.backend import NotFound
from ..storage.registry import check_name, sha256
from ..workspace import Context, home, whoami

TABLE_MIME = "application/vnd.captain.table+json"
VEGA_MIME = "application/vnd.vegalite.v5+json"


def new_notebook() -> dict:
    return {"nbformat": 4, "nbformat_minor": 5, "cells": [],
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                         "captain": {"format": 1}}}


def _draft_path(ctx: Context, name: str) -> str:
    d = os.path.join(home(), "drafts", ctx.workspace.name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name.replace("/", "__") + ".ipynb")


def normalize(nb: dict) -> dict:
    """Accept any Jupyter notebook (nbformat 4). Cells without Captain metadata stay plain: code cells
    run as Python, markdown stays markdown."""
    if not isinstance(nb, dict) or nb.get("nbformat") != 4 or not isinstance(nb.get("cells"), list):
        raise ValueError("not a Jupyter notebook: expected nbformat 4 JSON with a cells list")
    nb.setdefault("metadata", {}).setdefault("captain", {"format": 1})
    nb.setdefault("nbformat_minor", 5)
    for i, c in enumerate(nb["cells"]):
        if c.get("cell_type") not in ("code", "markdown", "raw"):
            raise ValueError(f"cell {i + 1} has unknown type {c.get('cell_type')!r}")
        if isinstance(c.get("source"), list):
            c["source"] = "".join(c["source"])
        c.setdefault("metadata", {})
        if c["cell_type"] == "code":
            c.setdefault("outputs", [])
            c.setdefault("execution_count", None)
    return nb


def import_file(ctx: Context, path: str, name: str | None = None) -> NotebookRevision:
    """Any supported format: ipynb, jupytext, Databricks, marimo, Quarto, R Markdown, MyST, Zeppelin."""
    from .formats import to_ipynb

    with open(path) as fh:
        nb, fmt, warnings = to_ipynb(os.path.basename(path), fh.read())
    name = name or re.sub(r"[^a-z0-9._-]+", "-", os.path.splitext(os.path.basename(path))[0].lower()).strip("-.")
    msg = f"imported from {os.path.basename(path)} ({fmt})" + (f"; {len(warnings)} warning(s): " + " | ".join(warnings) if warnings else "")
    return save_revision(ctx, name, nb, msg)


def save_draft(ctx: Context, name: str, nb: dict) -> None:
    check_name(name)
    p = _draft_path(ctx, name)
    with open(p + ".tmp", "w") as fh:
        json.dump(nb, fh)
    os.replace(p + ".tmp", p)


def save_revision(ctx: Context, name: str, nb: dict, message: str = "") -> NotebookRevision:
    nb = normalize(nb)
    body = json.dumps(nb, indent=1, sort_keys=True).encode()
    entry = ctx.registry.put_blob(body, "notebook.ipynb")
    m = NotebookRevision(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(),
                         name=check_name(name), version=0, description=message, files=[entry])
    m = ctx.registry.publish(m)
    try:
        os.unlink(_draft_path(ctx, name))
    except FileNotFoundError:
        pass
    return m


def load(ctx: Context, name: str) -> tuple[int | None, dict, bool]:
    """Returns (latest revision or None, notebook, from_draft)."""
    p = _draft_path(ctx, name)
    try:
        latest = ctx.registry.versions("notebook", name)[-1]
    except IndexError:
        latest = None
    if os.path.exists(p):
        with open(p) as fh:
            return latest, json.load(fh), True
    if latest is None:
        raise NotFound(f"no notebook named {name}")
    m = ctx.registry.load("notebook", name, latest)
    return latest, json.loads(ctx.registry.get_blob(m.files[0])), False


def listing(ctx: Context) -> list[dict]:
    names = set(ctx.registry.names("notebook"))
    d = os.path.join(home(), "drafts", ctx.workspace.name)
    drafts = {f[:-6].replace("__", "/") for f in os.listdir(d)} if os.path.isdir(d) else set()
    out = []
    for n in sorted(names | drafts):
        vs = ctx.registry.versions("notebook", n) if n in names else []
        out.append({"name": n, "latest": vs[-1] if vs else None, "has_draft": n in drafts})
    return out


def cell_code(cell: dict, n: int) -> str:
    """Turn a cell into the Python the kernel runs."""
    meta = (cell.get("metadata") or {}).get("captain") or {}
    kind = meta.get("kind") or ("markdown" if cell.get("cell_type") == "markdown" else "python")
    src = cell.get("source")
    src = "".join(src) if isinstance(src, list) else (src or "")
    if kind == "sql":
        var = meta.get("var") or f"df_{n}"
        if not var.isidentifier():
            raise ValueError(f"result name {var!r} is not a Python identifier")
        return f"{var} = __cst.sql({meta.get('conn')!r}, {src!r}, var={var!r})"
    if kind == "chart":
        return f"__cst.chart({src.strip() or 'None'}, {json.dumps(meta.get('chart') or {})!r})"
    return src


BOOT = "import captain_search.notebook.helpers as __cst\n__cst.install()\n"


class Kernel:
    """One Jupyter kernel. execute() yields API events (see docs/api.md, kernel socket)."""

    def __init__(self, ctx: Context):
        from jupyter_client import KernelManager

        env = dict(os.environ)
        env["CST_WORKSPACE"] = ctx.workspace.name
        env["CST_HOME"] = home()
        self.km = KernelManager(kernel_name="python3")
        self.km.kernel_cmd = None
        self.km.start_kernel(env=env, extra_arguments=[])
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=60)
        for _ in self.execute("__boot__", BOOT):
            pass

    def execute(self, cell_id: str, code: str):
        t0 = time.time()
        msg_id = self.kc.execute(code, store_history=True, allow_stdin=False)
        yield {"type": "status", "state": "busy"}
        while True:
            try:
                msg = self.kc.get_iopub_msg(timeout=3600)
            except queue.Empty:
                yield {"type": "error", "cell_id": cell_id, "ename": "Timeout", "evalue": "no output for an hour", "traceback": []}
                break
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            t, c = msg["msg_type"], msg["content"]
            if t == "stream":
                yield {"type": "stream", "cell_id": cell_id, "name": c["name"], "text": c["text"]}
            elif t in ("display_data", "execute_result"):
                data = c["data"]
                if TABLE_MIME in data:
                    yield {"type": "table", "cell_id": cell_id, **data[TABLE_MIME]}
                    continue
                for mime in (VEGA_MIME, "image/png", "text/html", "text/plain"):
                    if mime in data:
                        yield {"type": "display", "cell_id": cell_id, "mime": mime, "data": data[mime]}
                        break
            elif t == "error":
                yield {"type": "error", "cell_id": cell_id, "ename": c["ename"], "evalue": c["evalue"],
                       "traceback": [_strip_ansi(x) for x in c["traceback"]]}
            elif t == "status" and c["execution_state"] == "idle":
                break
        yield {"type": "status", "state": "idle"}
        yield {"type": "done", "cell_id": cell_id, "ms": int((time.time() - t0) * 1000)}

    def interrupt(self):
        self.km.interrupt_kernel()

    def shutdown(self):
        try:
            self.kc.stop_channels()
        finally:
            self.km.shutdown_kernel(now=True)


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def to_outputs(events: list[dict]) -> list[dict]:
    """API events -> nbformat outputs, so a headless run can be saved."""
    outs = []
    for e in events:
        if e["type"] == "stream":
            outs.append({"output_type": "stream", "name": e["name"], "text": e["text"]})
        elif e["type"] == "table":
            body = {k: e[k] for k in ("var", "columns", "rows", "row_count", "truncated")}
            outs.append({"output_type": "display_data", "metadata": {}, "data": {TABLE_MIME: body,
                         "text/plain": f"{e['var']}: {e['row_count']} rows"}})
        elif e["type"] == "display":
            outs.append({"output_type": "display_data", "metadata": {}, "data": {e["mime"]: e["data"]}})
        elif e["type"] == "error":
            outs.append({"output_type": "error", "ename": e["ename"], "evalue": e["evalue"], "traceback": e["traceback"]})
    return outs


def run_all(ctx: Context, nb: dict, params: dict | None = None, progress=None) -> tuple[dict, list[str]]:
    """Execute every code cell in order (papermill-style). Returns (notebook with outputs, errors)."""
    k = Kernel(ctx)
    errors = []
    try:
        if params:
            list(k.execute("__params__", "\n".join(f"{n} = {v!r}" for n, v in params.items())))
        n = 0
        for cell in nb["cells"]:
            if cell.get("cell_type") != "code":
                continue
            n += 1
            evs = list(k.execute(cell.get("id") or str(n), cell_code(cell, n)))
            cell["outputs"] = to_outputs(evs)
            cell["execution_count"] = n
            errs = [e for e in evs if e["type"] == "error"]
            if progress:
                progress(f"cell {n}: {'error' if errs else 'ok'}")
            if errs:
                errors.append(f"cell {n}: {errs[0]['ename']}: {errs[0]['evalue']}")
                break
    finally:
        k.shutdown()
    return nb, errors
