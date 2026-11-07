"""cst ext and cst project: the extensions Captain loaded, and the team's captain/ folder (plan 0)."""
from __future__ import annotations

import os
from typing import Optional

import typer

from .. import extensions as X
from .main import S, app, emit, fail

ext_app = typer.Typer(no_args_is_help=True, help="Engines, simulators, judges and metrics: built in, from packages, "
                                                  "or from your captain/ folder")
project_app = typer.Typer(no_args_is_help=True, help="Your organization's captain/ folder: search code versioned in your repo")
view_app = typer.Typer(no_args_is_help=True, help="Your team's own Captain screens, kept as code in captain/views/")
app.add_typer(ext_app, name="ext")
app.add_typer(project_app, name="project")
app.add_typer(view_app, name="view")


def _ws() -> str | None:
    try:
        from .. import workspace as wsp

        return S.workspace or os.environ.get("CST_WORKSPACE") or wsp.load_config().get("current")
    except Exception:  # noqa: BLE001
        return None


@ext_app.command("list")
def ext_list(kind: Optional[str] = typer.Option(None, "--kind", help="engine, simulator, judge or metric")):
    """Every extension Captain can use, and where it comes from."""
    reg = X.load(_ws(), fresh=True)
    items = [e.public() for e in reg.items if not kind or e.kind == kind]
    lines = [f"{e['kind']:<10} {e['name']:<24} {e['version']:<8} {e['source']}" for e in items]
    if reg.errors:
        lines.append(f"\n{len(reg.errors)} could not load. Run `cst ext check` for details.")
    emit({"extensions": items, "errors": reg.errors, "project": reg.project}, "\n".join(lines))


@ext_app.command("check")
def ext_check(path: Optional[str] = typer.Argument(None, help="a captain/ folder. Default: the one cst finds")):
    """Load and test every extension, then exit 1 if any fails. Run it in CI."""
    if path:
        os.environ["CST_PROJECT"] = path
    reg = X.load(_ws(), fresh=True)
    if path and not reg.project:
        fail(f"no captain.toml in {path}")
    custom = [e.public() for e in reg.items if e.source != "builtin"]
    lines = [f"ok    {e['kind']:<10} {e['name']:<24} {e['source']}" for e in custom]
    lines += [f"FAIL  {e['kind']:<10} {e['name']:<24} {e['source']}: {e['error']}" for e in reg.errors]
    if reg.project:
        p = reg.project
        lines.insert(0, f"project {p['path']} at {(p.get('commit') or 'no git commit')[:12]}"
                        f"{' with uncommitted changes' if p.get('dirty') else ''}")
    out = {"ok": not reg.errors, "checked": custom, "errors": reg.errors, "project": reg.project}
    emit(out, "\n".join(lines) or "no custom extensions found")
    if reg.errors:
        raise typer.Exit(1)


@project_app.command("init")
def project_init(where: str = typer.Argument(".", help="your repo. captain/ is created inside it"),
                 name: Optional[str] = typer.Option(None, "--name")):
    """Create a captain/ folder with an example metric and judge."""
    try:
        path = X.scaffold(where, name or os.path.basename(os.path.abspath(where)))
    except FileExistsError as e:
        fail(str(e))
    emit({"path": str(path)}, f"created {path}. Edit it, then run `cst ext check`.")


@project_app.command("use")
def project_use(path: Optional[str] = typer.Argument(None, help="a captain/ folder, or none to unbind"),
                clear: bool = typer.Option(False, "--clear")):
    """Bind a captain/ folder to the current workspace, so the app and every run use it."""
    ws = _ws()
    if not ws:
        fail("no workspace. Run `cst init` first.")
    if clear or not path:
        X.bind(ws, None)
        return emit({"workspace": ws, "project": None}, f"{ws} uses no project folder")
    found = X.find_project(start=path)
    if not found or not str(found).startswith(os.path.abspath(os.path.expanduser(path))):
        fail(f"no captain.toml in {path}. Create one with `cst project init {path}`.")
    X.bind(ws, str(found))
    emit({"workspace": ws, "project": str(found)}, f"{ws} now uses {found}")


@project_app.command("show")
def project_show():
    """Which captain/ folder is in use, at which commit."""
    reg = X.load(_ws(), fresh=True)
    if not reg.project:
        return emit({"project": None}, "no captain/ folder found. Create one with `cst project init <your repo>`.")
    p = reg.project
    emit(p, f"{p['path']}\ncommit {p.get('commit') or 'not in git'}{' (uncommitted changes)' if p.get('dirty') else ''}\n"
            f"{p['files']} files, sha256 {p['sha256'][:16]}")


VIEW_NAME = __import__("re").compile(r"^[a-z][a-z0-9-]{0,40}$")


@view_app.command("new")
def view_new(name: str = typer.Argument(..., help="folder name: lowercase letters, digits and dashes"),
             title: Optional[str] = typer.Option(None, "--title", help="what the Explorer shows. Default: from the name"),
             icon: str = typer.Option("table", "--icon", help="an icon name from Captain's set, e.g. table, chart, search")):
    """Start a team view: captain/views/<name>/ with a screen that reads real Captain data. Captain loads it
    beside its own screens and reloads it when the files change. Check it with `captain-app check`."""
    import importlib.resources as R
    import json

    if not VIEW_NAME.match(name):
        fail(f"{name!r} can't be a view name: lowercase letters, digits and dashes, starting with a letter")
    proj = X.find_project(_ws())
    if proj is None:
        fail("no captain/ folder here. Create one with `cst project init <your repo>`, or bind one with `cst project use`.")
    dest = proj / "views" / name
    if dest.exists():
        fail(f"{dest} already exists. Pick another name, or edit that view.")
    title = title or name.replace("-", " ").capitalize()
    src = R.files("captain_search") / "view_template"
    dest.mkdir(parents=True)
    for f in ("mod.json", "main.js", "README.md", ".gitignore"):
        text = (src / f).read_text().replace("{{name}}", name).replace("{{title}}", title)
        if f == "mod.json":
            doc = json.loads(text)
            doc["icon"] = icon
            text = json.dumps(doc, indent=2) + "\n"
        (dest / f).write_text(text)
    emit({"path": str(dest), "title": title, "icon": icon},
         f"created {dest}\nCaptain picks it up under Views. Check it with: captain-app check {dest}")
