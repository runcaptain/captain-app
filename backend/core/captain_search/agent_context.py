"""The agent's context: things a person put there for their agent, beside what they are looking at.

`cst context add <ref>` (or "Send to agent" in the app) pins a dataset, a model version, an offline evaluation, an
experiment, a candidate, a connection's table, a project or a file. `cst context show` prints the current screen and
each pinned thing, expanded to a short summary an agent can act on. Pins are kept per workspace in the app's own
folder, <app home>/context/<workspace>.json, so the app's sidebar and `cst` read the same list:

  {"items": [{"ref": "dataset:demo-catalog@v1", "kind": "dataset", "label": "demo-catalog@v1",
              "added_by": "edgar", "agent": null, "added_at": "2026-11-10T20:00:00+00:00"}]}

Nothing here holds a secret: connections are named, never opened, and every value printed goes through scrub().
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from .storage.backend import NotFound
from .workspace import Context, whoami

KINDS = ("dataset", "model", "run", "experiment", "candidate", "table", "project", "staging", "file")
# What people call them, on screen and in the CLI: each maps to one kind.
ALIASES = {"data": "dataset", "pipeline": "model", "version": "model", "backtest": "run", "evaluation": "run",
           "comparison": "run", "investigation": "experiment", "connection": "table", "path": "file"}
MAX_ITEMS = 40
# Files that are secrets by their name: never read, never pinned.
_SECRET_NAME = re.compile(r"\.env(\..*)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_[a-z0-9]+|credentials(\.json)?|"
                          r".*secret.*|\.netrc|\.pgpass|.*token.*", re.I)


def secret_file(path: str) -> bool:
    """By the file's own name, not the folders above it."""
    return bool(_SECRET_NAME.fullmatch(os.path.basename(path.rstrip("/"))))


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def app_home() -> str:
    return os.environ.get("CAPTAIN_APP_HOME") or os.path.join(os.path.expanduser("~"), ".config", "captain-app")


def path(workspace: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", workspace or "default")
    return os.path.join(app_home(), "context", f"{safe}.json")


def load(workspace: str) -> list[dict]:
    try:
        with open(path(workspace)) as f:
            items = json.load(f).get("items")
    except (OSError, ValueError, AttributeError):
        return []
    return [i for i in items or [] if isinstance(i, dict) and i.get("ref")]


def save(workspace: str, items: list[dict]) -> None:
    p = path(workspace)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"items": items[-MAX_ITEMS:]}, f, indent=2)
    os.replace(tmp, p)


def resolve(ctx: Context, raw: str) -> dict:
    """A ref a person or an agent typed, as {ref, kind, label}. `kind:name` says the kind; a bare name is looked up:
    a file that exists, then a project, a dataset, a model version, an offline evaluation, an experiment."""
    from . import projects as PR

    text = raw.strip()
    if not text:
        raise ValueError("say what to add: a dataset, model version, evaluation, experiment, candidate, table, project or file")
    head, sep, rest = text.partition(":")
    kind = ALIASES.get(head.lower(), head.lower()) if sep and rest and not rest.startswith("/") else None
    if kind not in KINDS:
        kind, rest = None, text

    def versioned(reg: str, what: str):
        name, _, v = rest.partition("@v")
        name = name.split(":")[0] if reg == "dataset" else name
        try:
            m = ctx.registry.load(reg, name, int(v.split(":")[0]) if v else None)
        except (NotFound, ValueError):
            raise NotFound(f"no {what} named {rest!r} in workspace {ctx.workspace.name}") from None
        return f"{m.name}@v{m.version}"

    def project():
        p = next((p for p in PR.listing(ctx) if rest in (p["id"], p["name"])), None)
        if not p:
            raise NotFound(f"no project {rest!r} in workspace {ctx.workspace.name}")
        return p

    if kind == "file" or (kind is None and (os.path.exists(os.path.expanduser(rest)) and ("/" in rest or "." in rest))):
        p = os.path.abspath(os.path.expanduser(rest))
        if not os.path.exists(p):
            raise NotFound(f"no file at {p}")
        if secret_file(p):
            raise ValueError(f"{os.path.basename(p)} looks like a file of secrets, so it isn't added. Name the thing it connects to instead.")
        return {"ref": f"file:{p}", "kind": "file", "label": os.path.basename(p)}
    if kind == "project":
        p = project()
        return {"ref": f"project:{p['id']}", "kind": "project", "label": p["name"]}
    if kind == "staging":
        p = project()
        return {"ref": f"staging:{p['id']}", "kind": "staging", "label": f"what is deployed for {p['name']}"}
    if kind == "dataset":
        r = versioned("dataset", "dataset")
        return {"ref": f"dataset:{r}", "kind": "dataset", "label": r}
    if kind == "model":
        r = versioned("pipeline", "model version")
        return {"ref": f"model:{r}", "kind": "model", "label": r}
    if kind == "run":
        name = rest.split("#")[0].split("@v")[0]
        try:
            ctx.registry.load("backtest", name)
        except NotFound:
            raise NotFound(f"no offline evaluation named {name!r} in workspace {ctx.workspace.name}") from None
        return {"ref": f"run:{name}", "kind": "run", "label": name}
    if kind == "experiment":
        from . import investigations as INV

        d = INV.get(ctx, rest)
        return {"ref": f"experiment:{d['id']}", "kind": "experiment", "label": d.get("title") or d["id"]}
    if kind == "candidate":
        from . import project_state as PS

        pid, _, eid = rest.partition("/")
        e = next((e for e in PS.get(ctx, pid).get("experiments", []) if eid in (e.get("id"), e.get("pipeline"))), None)
        if not e:
            raise NotFound(f"project {pid!r} has no candidate {eid!r}. Write it as candidate:<project>/<candidate id>")
        return {"ref": f"candidate:{pid}/{e['id']}", "kind": "candidate", "label": f"{e.get('pipeline') or e['id']} in {pid}"}
    if kind == "table":
        conn, _, table = rest.partition(".")
        if conn not in ctx.connections():
            raise NotFound(f"no connection {conn!r}. Write it as table:<connection>.<table>")
        return {"ref": f"table:{conn}.{table}" if table else f"table:{conn}", "kind": "table", "label": f"{conn} {table}".strip()}
    # A bare name: the first thing it names.
    for k in ("project", "dataset", "model", "run", "experiment"):
        try:
            return resolve(ctx, f"{k}:{rest}")
        except (NotFound, ValueError, KeyError):
            continue
    raise NotFound(f"nothing named {rest!r} here. Say its kind, for example dataset:{rest}, model:{rest}@v1, run:{rest}, "
                   f"experiment:<id>, candidate:<project>/<id>, table:<connection>.<table>, project:{rest}, or a file path")


def add(ctx: Context, refs: list[str], agent: str | None = None) -> list[dict]:
    items = load(ctx.workspace.name)
    out = []
    for raw in refs:
        it = resolve(ctx, raw)
        items = [i for i in items if i["ref"] != it["ref"]]
        it = {**it, "added_by": whoami(), "agent": agent, "added_at": _now()}
        items.append(it)
        out.append(it)
    save(ctx.workspace.name, items)
    return out


def remove(ctx: Context, raw: str) -> list[dict]:
    """Remove by its full ref, its label, or what `add` would resolve it to. Returns what was removed."""
    items = load(ctx.workspace.name)
    hit = [i for i in items if raw in (i["ref"], i.get("label"), i["ref"].split(":", 1)[-1])]
    if not hit:
        try:
            ref = resolve(ctx, raw)["ref"]
            hit = [i for i in items if i["ref"] == ref]
        except (NotFound, ValueError, KeyError):
            hit = []
    if not hit:
        raise NotFound(f"{raw!r} is not in the agent's context. `cst context show` lists what is.")
    save(ctx.workspace.name, [i for i in items if i not in hit])
    return hit


def clear(ctx: Context) -> int:
    n = len(load(ctx.workspace.name))
    save(ctx.workspace.name, [])
    return n


def _cell(v):
    s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
    return s if not isinstance(s, str) or len(s) <= 80 else s[:80] + "..."


def expand(ctx: Context, item: dict) -> dict:
    """A pinned thing as a short summary. A thing that is gone, or can't be read, says so and the rest still print."""
    kind, _, rest = item["ref"].partition(":")
    try:
        return _expand(ctx, kind, rest)
    except Exception as e:  # noqa: BLE001
        return {"problem": str(e)[:300]}


def _expand(ctx: Context, kind: str, rest: str) -> dict:
    if kind == "dataset":
        from .data import datasets as ds

        name, _, v = rest.partition("@v")
        m = ctx.registry.load("dataset", name, int(v) if v else None)
        d = m.model_dump(mode="json")
        cols = d.get("schema_fields") or []
        out = {"kind": d.get("dataset_kind"), "rows": d.get("row_count"), "columns": [f"{f.get('name')} {f.get('type')}" for f in cols[:40]],
               "more_columns": max(0, len(cols) - 40)}
        try:
            if m.external and not m.files:
                from . import browse

                r = browse.rows(ctx, m, limit=3)
                names = [c["name"] for c in r["columns"]]
                out["sample"] = [dict(zip(names, map(_cell, row))) for row in r["rows"][:3]]
            else:
                out["sample"] = [{k: _cell(x) for k, x in row.items()} for row in ds.load_table(ctx, m).slice(0, 3).to_pylist()]
        except Exception as e:  # noqa: BLE001
            out["sample_problem"] = str(e)[:200]
        return out
    if kind == "model":
        name, _, v = rest.partition("@v")
        m = ctx.registry.load("pipeline", name, int(v) if v else None)
        return {"engine": m.engine, "catalog": m.catalog, "registry_ref": m.registry_ref, "trained": bool(m.train_inputs),
                "train_metrics": dict(list((m.train_metrics or {}).items())[:8]), "known_issues": m.known_issues[:5],
                "created": m.created_at.isoformat()}
    if kind == "run":
        m = ctx.registry.load("backtest", rest)
        runs = ctx.registry.versions("backtest-run", rest)
        out = {"arms": m.arms, "primary_metric": m.preregistration.primary_metric, "mode": m.mode,
               "status": "finished" if runs else "planned, not run yet", "runs": len(runs)}
        if runs:
            r = ctx.registry.load("backtest-run", rest, runs[-1])
            out |= {"last_run": r.created_at.isoformat(), "graded_enough": r.complete, "independent_labels": r.independent,
                    "metrics": [{"arm": a.arm, "evidence": str(getattr(a.evidence, "value", a.evidence)),
                                 **dict(list(a.offline.items())[:6])} for a in r.arms],
                    "comparisons": [{k: c.get(k) for k in ("metric", "a", "b", "delta", "ci_low", "ci_high", "p") if c.get(k) is not None}
                                    for c in r.comparisons[:4]]}
        return out
    if kind == "experiment":
        from . import investigations as INV

        d = INV.get(ctx, rest)
        plan = (d.get("revisions") or [{}])[-1]
        return {"title": d.get("title"), "state": d.get("state"), "hypothesis": plan.get("hypothesis"), "baseline": plan.get("baseline"),
                "candidates": plan.get("candidates"), "decision": (d.get("decision") or {}).get("outcome"), "runs": len(d.get("runs") or [])}
    if kind == "candidate":
        from . import project_state as PS

        pid, _, eid = rest.partition("/")
        doc = PS.get(ctx, pid)
        e = next(e for e in doc.get("experiments", []) if e.get("id") == eid)
        return {"project": pid, "model": e.get("pipeline"), "status": e.get("status"), "is_baseline": doc.get("champion") == e.get("pipeline"),
                "labels": e.get("labels"), "evaluation": e.get("evaluation"), "offline_evaluation": e.get("backtest")}
    if kind == "table":
        from . import metastore as MS

        conn, _, table = rest.partition(".")
        c = ctx.load_connection(conn)
        out = {"connection": conn, "engine": getattr(c, "engine", None), "table": table or None}
        cache = MS.cached(ctx, conn) or {}
        for t in cache.get("tables") or []:
            full = ".".join(x for x in (t.get("schema"), t.get("name")) if x)
            if table and table in (full, t.get("name")):
                out |= {"rows": t.get("rows"), "columns": [f"{c.get('name')} {c.get('type')}" for c in (t.get("columns") or [])[:40]]}
        if table and "columns" not in out:
            out["note"] = f"Not read yet. `cst tables pull {conn}` reads its tables."
        return out
    if kind == "project":
        from . import project_state as PS

        st = PS.stages(ctx, rest)
        return {"inputs": [{k: i.get(k) for k in ("slot", "label", "pinned", "required", "newest") if i.get(k) is not None} for i in st.get("inputs", [])],
                "evaluation": (st.get("evaluation") or {}).get("text"), "candidates": (st.get("experiments") or {}).get("count"),
                "baseline": PS.get(ctx, rest).get("champion"), "next": st.get("next")}
    if kind == "staging":
        from . import staging as SG

        return SG.agent_summary(ctx, rest)
    if kind == "file":
        st = os.stat(rest)
        out = {"path": rest, "bytes": st.st_size, "modified": dt.datetime.fromtimestamp(st.st_mtime, dt.UTC).isoformat(timespec="seconds"),
               "folder": os.path.isdir(rest)}
        if os.path.isdir(rest):
            out["holds"] = sorted(os.listdir(rest))[:30]
        elif st.st_size <= 512 * 1024 and not secret_file(rest):
            try:
                with open(rest, encoding="utf-8") as f:
                    from .cli.appctl import scrub_text

                    out["first_lines"] = [scrub_text(line.rstrip("\n")[:200]) for _, line in zip(range(8), f)]
            except (UnicodeDecodeError, OSError):
                out["note"] = "not text"
        return out
    raise ValueError(f"unknown kind {kind!r}")
