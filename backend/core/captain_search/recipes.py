"""Project kinds as recipes: what a project is building, the inputs it asks for, and how it is evaluated.

A recipe is a small YAML file:

  id: search-events
  label: Search ranking from events
  description: ...
  slots:                      # the inputs it asks for, copied into a new project and then editable there
    - {id: catalog, label: Catalog, role: catalog, accepts: [catalog], required: true, why: "..."}
  trainers:                   # suggested training commands
    - {id: your-command, label: Your training command, command: "python train.py", why: "..."}
  evaluation: {method: holdout | logged-clicks | trainer-reported | auto | none, holdout: true, text: "..."}

They come from three places, later ones replacing earlier ones with the same id (the extension points' order):
1. built in, `captain_search/recipes/*.yaml`;
2. the workspace, `recipes/<id>.yaml` in its storage, so a team shares them;
3. the team's repo, `captain/recipes/<id>.yaml` in the project folder (extensions.find_project).

`role` says what an input is for (evaluation reads roles, never slot ids). `accepts` lists the dataset kinds a slot
takes, or `any`.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .workspace import Context

ROLES = ("catalog", "events", "queries", "labels", "holdout", "users", "items", "interactions", "custom")
DATASET_KINDS = ("catalog", "events", "queries", "judgments", "logs", "generic", "any")
METHODS = ("auto", "holdout", "logged-clicks", "trainer-reported", "none")
SLOT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
BUILTIN = Path(__file__).parent / "recipes"


def check_slot(s: dict, origin: str = "recipe") -> dict:
    """A slot with every field filled and checked."""
    if not isinstance(s, dict):
        raise ValueError(f"a slot is a mapping, got {s!r}")
    sid = str(s.get("id") or re.sub(r"[^a-z0-9]+", "-", str(s.get("label") or "").lower()).strip("-")[:40])
    if not SLOT_ID.match(sid):
        raise ValueError(f"bad slot id {sid!r}: lowercase letters, digits, - and _")
    role = str(s.get("role") or "custom")
    if role not in ROLES:
        raise ValueError(f"slot {sid}: role is one of {', '.join(ROLES)}, not {role!r}")
    acc = s.get("accepts") or ["any"]
    acc = [acc] if isinstance(acc, str) else [str(a) for a in acc]
    bad = [a for a in acc if a not in DATASET_KINDS]
    if bad:
        raise ValueError(f"slot {sid}: accepts dataset kinds {', '.join(DATASET_KINDS)}, not {', '.join(bad)}")
    return {"id": sid, "label": str(s.get("label") or sid), "role": role, "accepts": acc,
            "required": bool(s.get("required", False)), "why": str(s.get("why") or ""),
            "origin": s.get("origin") or origin}


def check(doc: dict, source: str) -> dict:
    if not isinstance(doc, dict) or not doc.get("id"):
        raise ValueError("a recipe needs an id")
    rid = str(doc["id"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", rid):
        raise ValueError(f"bad recipe id {rid!r}")
    slots = [check_slot(s) for s in doc.get("slots") or []]
    ids = [s["id"] for s in slots]
    if len(set(ids)) != len(ids):
        raise ValueError(f"recipe {rid}: slot ids repeat")
    ev = dict(doc.get("evaluation") or {})
    method = str(ev.get("method") or "auto")
    if method not in METHODS:
        raise ValueError(f"recipe {rid}: evaluation method is one of {', '.join(METHODS)}")
    trainers = [{"id": str(t.get("id") or f"t{i}"), "label": str(t.get("label") or t.get("command") or ""),
                 "command": str(t.get("command") or ""), "why": str(t.get("why") or "")}
                for i, t in enumerate(doc.get("trainers") or [])]
    return {"id": rid, "label": str(doc.get("label") or rid), "description": str(doc.get("description") or ""),
            "source": source, "slots": slots, "trainers": trainers,
            "evaluation": {"method": method, "holdout": bool(ev.get("holdout", False)), "text": str(ev.get("text") or "")}}


def _load_dir(files, source: str, out: dict, errors: list) -> None:
    for name, text in files:
        try:
            r = check(yaml.safe_load(text) or {}, source)
            out[r["id"]] = r
        except (ValueError, yaml.YAMLError) as e:
            errors.append({"file": name, "source": source, "error": str(e)})


def load(ctx: Context | None = None) -> tuple[dict[str, dict], list[dict]]:
    """Every recipe by id, and the files that failed to load (shown, never fatal)."""
    out: dict[str, dict] = {}
    errors: list[dict] = []
    _load_dir(((p.name, p.read_text()) for p in sorted(BUILTIN.glob("*.yaml"))), "builtin", out, errors)
    if ctx is not None:
        keys = [k for k in ctx.store.list("recipes/") if k.endswith((".yaml", ".yml"))]
        _load_dir(((k, ctx.store.get(k).decode()) for k in keys), "workspace", out, errors)
        try:
            from .extensions import find_project

            proj = find_project(ctx.workspace.name)
        except Exception:  # noqa: BLE001  no project folder is the usual case
            proj = None
        if proj is not None:
            d = Path(proj) / "recipes"
            if d.is_dir():
                _load_dir(((str(p), p.read_text()) for p in sorted(d.glob("*.y*ml"))), "repo", out, errors)
    return out, errors


def listing(ctx: Context | None = None) -> list[dict]:
    rs, _ = load(ctx)
    order = ["search-events", "search-judgments", "recommendations", "byo-pipeline"]
    return sorted(rs.values(), key=lambda r: (order.index(r["id"]) if r["id"] in order else 99, r["label"]))


def get(ctx: Context | None, rid: str) -> dict:
    rs, errors = load(ctx)
    if rid not in rs:
        bad = next((e for e in errors if rid in e["file"]), None)
        hint = f" Its file failed to load: {bad['error']}" if bad else ""
        raise ValueError(f"no project kind {rid!r}. Kinds: {', '.join(r['id'] for r in listing(ctx))}.{hint}")
    return rs[rid]


def save(ctx: Context, doc: dict) -> dict:
    """Add a recipe to the workspace (write-once per id: a change is a new id, like every shared definition)."""
    r = check(doc, "workspace")
    ctx.store.put_new(f"recipes/{r['id']}.yaml", yaml.safe_dump(doc, sort_keys=False).encode())
    return r
