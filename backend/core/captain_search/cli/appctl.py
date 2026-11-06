"""cst app: open the desktop app, and drive the running one from a shell or an agent.

The app writes <home>/app/app.json ({port, token, pid}) while it runs and serves a small localhost endpoint.
These commands are the shell side of docs/customization.md: run any app command, list them, see what is on
screen, save a screenshot, and check a mod before calling it done.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import typer

from ..workspace import home
from .main import S, app, emit, fail

app_app = typer.Typer(invoke_without_command=True, help="Open the desktop app, or drive the running one")
app.add_typer(app_app, name="app")


def app_home() -> str:
    return os.path.join(home(), "app")


def _endpoint() -> dict:
    p = os.path.join(app_home(), "app.json")
    try:
        with open(p) as f:
            info = json.load(f)
    except (OSError, ValueError):
        fail("the desktop app is not running (no app.json in its folder). Open it with `cst app`.")
    try:
        os.kill(int(info["pid"]), 0)
    except (OSError, KeyError, ValueError):
        fail("the desktop app that wrote app.json is no longer running. Open it with `cst app`.")
    return info


def _call(method: str, path: str, body: dict | None = None, timeout: float = 40.0):
    import httpx

    info = _endpoint()
    try:
        r = httpx.request(method, f"http://127.0.0.1:{info['port']}{path}", json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {info['token']}"})
    except httpx.HTTPError as e:
        fail(f"could not reach the desktop app: {e}")
    try:
        out = r.json()
    except ValueError:
        out = {"error": r.text[:500]}
    if r.status_code >= 400 or (isinstance(out, dict) and out.get("error")):
        if S.json:
            typer.echo(json.dumps(out if isinstance(out, dict) else {"error": str(out)}))
            raise typer.Exit(1)
        fail(out.get("error") if isinstance(out, dict) else str(out))
    return out


@app_app.callback()
def app_main(c: typer.Context):
    """With no subcommand: open the desktop app."""
    if c.invoked_subcommand is None:
        from .more import _open_app

        _open_app()


def _parse_args(args: Optional[str], kv: list[str]) -> dict:
    out: dict = {}
    if args:
        try:
            out = json.loads(args)
        except ValueError:
            fail("--args must be a JSON object")
        if not isinstance(out, dict):
            fail("--args must be a JSON object")
    for item in kv:
        if "=" not in item:
            fail(f"-a takes key=value (got {item!r})")
        k, v = item.split("=", 1)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


@app_app.command("do")
def app_do(command: str, args: Optional[str] = typer.Option(None, "--args", help="JSON object of arguments"),
           arg: list[str] = typer.Option([], "-a", help="key=value, repeatable; values parse as JSON when they can")):
    """Run one app command in the running app, e.g. `cst app do layout.switch -a name=focus`."""
    body = {"command": command, "args": _parse_args(args, arg)}
    if S.dry_run:
        return emit({"would_run": body})
    out = _call("POST", "/do", body)
    emit(out, json.dumps(out, indent=2) if not isinstance(out, str) else out)


@app_app.command("commands")
def app_commands():
    """Every command the app runs (app, layout, pane, overlay, mod, window and cst ones), with arguments."""
    out = _call("GET", "/commands")
    rows = out if isinstance(out, list) else out.get("commands", [])
    emit(rows, "\n".join(f"{c.get('name'):34} {c.get('help', '')}" for c in rows))


@app_app.command("status")
def app_status():
    """What is on screen: layout, focus, open overlays, and errors from files and mods."""
    out = _call("GET", "/status")
    emit(out, json.dumps(out, indent=2))


@app_app.command("shot")
def app_shot(path: str = typer.Argument("captain-shot.png")):
    """Save the app's current window as a PNG and print its path."""
    out = _call("POST", "/shot", {"path": os.path.abspath(path)}, timeout=60)
    emit(out, out.get("path", ""))


@app_app.command("check")
def app_check(folder: str):
    """Load a mod once, render it, and exit nonzero with the exact error. Run after every change to a mod."""
    p = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isfile(os.path.join(p, "main.js")):
        fail(f"{folder} has no main.js")
    out = _call("POST", "/check", {"path": p}, timeout=60)
    emit(out, "ok" if out.get("ok") else f"failed: {out.get('error')}")
    if not out.get("ok"):
        raise typer.Exit(1)


@app_app.command("home")
def app_home_cmd():
    """Print the app's customization folder (layouts, keys, settings, themes, mods)."""
    emit({"home": app_home()}, app_home())
