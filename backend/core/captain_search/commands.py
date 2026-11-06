"""Every cst command as data, so the app can build a form for any of them and run it.

GET /api/commands lists the command tree with each option's type, default and help. POST
/api/commands/run executes one in-process with --json, so the app, the CLI and a coding agent all
share one code path and the app can do everything the CLI can."""
from __future__ import annotations

import json
import shlex
import threading

import typer

from .cli.main import app as typer_app

HIDDEN = {("serve",), ("app",), ("code",), ("notebook", "open"), ("pipeline", "serve")}  # start long-lived processes or GUIs
HIDDEN_GROUPS = {("app",)}  # drives the desktop app from a shell; the app runs its own commands directly
_lock = threading.Lock()


def _is_group(c) -> bool:
    return hasattr(c, "commands")


def _is_arg(p) -> bool:
    return getattr(p, "param_type_name", "") == "argument"


def _root():
    return typer.main.get_command(typer_app)


def _param(p) -> dict:
    t = p.type
    tn = type(t).__name__
    choices = list(getattr(t, "choices", []) or []) or None
    kind = "flag" if getattr(p, "is_flag", False) else "choice" if choices else \
        "int" if tn == "IntParamType" else "float" if tn == "FloatParamType" else "string"
    return {"name": p.name, "kind": kind, "argument": _is_arg(p),
            "opts": list(getattr(p, "opts", [])), "required": bool(p.required),
            "default": None if callable(p.default) or p.default in (None, ()) else p.default,
            "help": getattr(p, "help", None) or "", "choices": [getattr(c, "value", c) for c in choices] if choices else None,
            "multiple": bool(getattr(p, "multiple", False)), "secret": _secret(p)}


SECRET_NAMES = {"value", "password", "token", "secret", "api_key", "client_secret", "secret_access_key"}


def _secret(p) -> bool:
    return bool(getattr(p, "hide_input", False)) or p.name in SECRET_NAMES


def tree() -> list[dict]:
    out = []

    def walk(cmd, path: list[str]):
        if _is_group(cmd):
            if tuple(path) in HIDDEN_GROUPS:
                return
            for name, sub in sorted(cmd.commands.items()):
                walk(sub, path + [name])
            return
        if tuple(path) in HIDDEN:
            return
        out.append({"path": path, "help": (cmd.help or "").strip().split("\n\n")[0],
                    "params": [_param(p) for p in cmd.params if p.name != "help"]})

    walk(_root(), [])
    return out


def argv(path: list[str], args: dict) -> list[str]:
    cmd = _root()
    for seg in path:
        if not _is_group(cmd) or seg not in cmd.commands:
            raise ValueError(f"unknown command: cst {' '.join(path)}")
        cmd = cmd.commands[seg]
    if tuple(path) in HIDDEN:
        raise ValueError(f"cst {' '.join(path)} can't run from here")
    params = {p.name: p for p in cmd.params}
    opt = []
    for k, v in (args or {}).items():
        if v is None or v == "" or k not in params:
            if k not in params:
                raise ValueError(f"cst {' '.join(path)} has no option {k!r}")
            continue
        p = params[k]
        if _is_arg(p):
            continue
        elif getattr(p, "is_flag", False):
            if v:
                opt.append(p.opts[0])
        else:
            for x in (v if isinstance(v, list) else [v]):
                opt += [p.opts[0], str(x)]
    # keep arguments in declared order
    order = [p.name for p in cmd.params if _is_arg(p)]
    pos = []
    for name in order:
        v = (args or {}).get(name)
        if v not in (None, ""):
            pos += [str(x) for x in (v if isinstance(v, list) else [v])]
    return path + opt + pos


def run(path: list[str], args: dict, workspace: str | None = None, dry_run: bool = False) -> dict:
    from typer.testing import CliRunner

    from .cli import main as M

    a = argv(path, args)
    pre = ["--json"] + (["-w", workspace] if workspace else []) + (["--dry-run"] if dry_run else [])
    with _lock:  # the CLI keeps its flags in module state
        r = CliRunner().invoke(typer_app, pre + a, catch_exceptions=True)
        M.S.workspace, M.S.json, M.S.dry_run = None, False, False
    text = r.stdout if hasattr(r, "stdout") else r.output
    try:
        output = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        output = text
    err = None
    if r.exit_code != 0:
        err = (output.get("error") if isinstance(output, dict) else None) or (str(r.exception) if r.exception else text.strip()[-500:])
    shown = _masked(path, args, a)
    return {"command": "cst " + " ".join(shlex.quote(x) for x in shown), "exit_code": r.exit_code, "output": output,
            "error": err, "dry_run": dry_run}


def _masked(path: list[str], args: dict, a: list[str]) -> list[str]:
    hidden = {str(v) for k, v in (args or {}).items() if k in SECRET_NAMES and v not in (None, "")}
    return ["****" if x in hidden else x for x in a]
