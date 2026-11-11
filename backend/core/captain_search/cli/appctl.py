"""cst app: open the desktop app, and drive the running one from a shell or an agent.

The app writes <home>/app/app.json ({port, token, pid}) while it runs and serves a small localhost endpoint.
These commands are the shell side of docs/customization.md: run any app command, list them, see what is on
screen, save a screenshot, and check a mod before calling it done.
"""
from __future__ import annotations

import json
import os
import re
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


# --- The native window: what the person is looking at -----------------------------------------------------------
# Captain's window serves one JSON line each way on <app home>/captain-app.sock (src/control.rs). The app home is
# $CAPTAIN_APP_HOME, which Captain sets in every terminal it opens, else ~/.config/captain-app.

SOCKET = "captain-app.sock"
HIDDEN = "(hidden)"
# Section ids, in the words the screens use (defaults/AGENTS.md, "Words"; kit/view.js SCREENS).
SCREENS = {"project": "Home", "investigations": "Experiments", "data": "Data and signals", "runs": "Runs",
           "comparisons": "Compare", "handoff": "Launch review", "team": "Team"}
# A session id in a row of search events names a shopper's visit, not a login, so it is not on this list.
SECRET_KEY = re.compile(r"pass(word|phrase)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|"
                        r"authorization|bearer|cookie|\bdsn\b|\bpat\b", re.I)
# The same pattern is in the screens (kit/view.js SECRET_VALUE): change both.
SECRET_VALUE = re.compile(
    r"^(?:(?:sk|pk|rk)[-_][-_A-Za-z0-9]{16,}|gh[pous]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abprs]-[-A-Za-z0-9]{10,}|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[-_A-Za-z0-9]{30,}|ya29\.[-_A-Za-z0-9.]{20,}|"
    r"eyJ[-_A-Za-z0-9]{10,}\.[-_A-Za-z0-9.]{10,}|dapi[a-f0-9]{24,}|hf_[A-Za-z0-9]{20,}|glpat-[-_A-Za-z0-9]{16,}|"
    r"shpat_[a-f0-9]{20,})$|-----BEGIN [A-Z ]*PRIVATE KEY-----")
URL_LOGIN = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@]+@", re.I)
URL_PARAM = re.compile(r"([?&;]\s*[^=&;\s]*(?:pass(?:word)?|pwd|secret|token|key|sig(?:nature)?|credential)[^=&;\s]*=)[^&;\s]+", re.I)


def scrub(v, key: str = ""):
    """A value safe to show an agent. The screens already leave secrets out (kit/view.js); this is the second check,
    on everything `cst app context` prints: a field with a secret's name, a string shaped like a key, a URL's login."""
    from ..commands import SECRET_NAMES

    if isinstance(v, dict):
        return {k: scrub(x, str(k)) for k, x in v.items()}
    if isinstance(v, list):
        return [scrub(x, key) for x in v]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = str(v)
    if key and s and (key.lower() in SECRET_NAMES or SECRET_KEY.search(key)):
        return HIDDEN
    if SECRET_VALUE.search(s.strip()):
        return HIDDEN
    return URL_PARAM.sub(rf"\1{HIDDEN}", URL_LOGIN.sub(rf"\1{HIDDEN}@", s))


_ASSIGN = re.compile(r"(?i)([\w.\-\"']*(?:pass|pwd|secret|token|key|credential|auth|cookie|dsn)[\w.\-\"']*\s*[:=]\s*)(\S.*)$")


def scrub_text(line: str) -> str:
    """One line of someone's file, safe to show: `name: value` and `NAME=value` lose the value when the name says
    secret, a word shaped like a key is hidden wherever it stands, and a URL loses its login."""
    line = _ASSIGN.sub(lambda m: m.group(1) + HIDDEN, line)
    return " ".join(HIDDEN if SECRET_VALUE.search(w.strip("\"',;")) else scrub(w) for w in line.split(" "))


def window_home() -> str:
    return os.environ.get("CAPTAIN_APP_HOME") or os.path.join(os.path.expanduser("~"), ".config", "captain-app")


class NotRunning(Exception):
    pass


def _window(request: dict, timeout: float = 6.0, quiet: bool = False) -> dict:
    """Ask the running window one thing. Fails with a plain sentence when Captain isn't running (`quiet`: raises
    NotRunning instead, for a caller that has more to say without the window)."""
    import socket

    path = os.path.join(window_home(), SOCKET)
    if not hasattr(socket, "AF_UNIX"):
        fail("Captain's window can't be asked from this Python on this system yet.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        if quiet:
            raise NotRunning() from None
        fail(f"Captain isn't running, so there is no screen to read. Open Captain and try again. (Looked for its window at {path}.)")
    except (OSError, socket.timeout) as e:
        fail(f"Captain's window didn't answer ({e}). It may be busy or starting: try again in a moment.")
    try:
        out = json.loads(buf.decode() or "null")
    except ValueError:
        out = None
    if not isinstance(out, dict):
        fail("Captain's window gave an answer cst couldn't read. Is this Captain older than this cst?")
    if out.get("error"):
        if "expected {" in str(out["error"]):
            fail("This Captain is older than this cst and can't say what is on screen. Update Captain.")
        fail(str(out["error"]))
    return out


def _record(raw: dict) -> dict:
    """The window's answer, as one small record: where the person is, what is selected, the open form, the page's
    Next line. Parts a screen published for another workspace are dropped."""
    import datetime

    ws = raw.get("workspace")
    views = {k: v for k, v in (raw.get("views") or {}).items() if isinstance(v, dict) and v.get("workspace") in (None, ws)}
    section = raw.get("section")
    page = dict(views.get(section) or {})
    scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else None
    overlays = [o for o in raw.get("overlays") or [] if o and o != "toast"]
    dialog = views.get("dialog")
    # A form's part outlives a window that quit with it open: believe it only while its mod is over the page.
    if dialog and dialog.get("mod") not in overlays:
        dialog = None
    project = page.get("project") if isinstance(page.get("project"), dict) else None
    if not project and scope and scope.get("kind") == "project":
        project = {"id": scope.get("id"), "name": scope.get("label")}
    skip = {"project", "next", "workspace", "at", "tab"}
    at = raw.get("at")
    rec = {
        "at": datetime.datetime.fromtimestamp(at / 1000, datetime.timezone.utc).isoformat(timespec="seconds") if at else None,
        "workspace": ws,
        "dev_backend": raw.get("dev_backend") or None,
        "project": project,
        "explorer_pick": {k: scope.get(k) for k in ("kind", "id", "label")} if scope and scope.get("id") else None,
        "screen": SCREENS.get(section, section),
        "section": section,
        "tab": page.get("tab"),
        "selected": {k: v for k, v in page.items() if k not in skip and v not in (None, "", [], {})},
        "dialog": {k: v for k, v in dialog.items() if k not in ("workspace", "at")} if dialog else None,
        "open_over_the_page": [o for o in overlays if not dialog or o != dialog.get("mod")],
        "next": page.get("next") or None,
        "agent_tab": raw.get("agent"),
        "terminals_shown": raw.get("terminals_shown"),
        "published_at": page.get("at"),
    }
    return scrub(rec)


def _known(rec: dict) -> dict:
    """What the services know about the things on screen. Each lookup stands alone: one that fails says why and the
    rest still print. Reads the records only, never a dataset's rows."""
    from .. import workspace as wsp

    out: dict = {}
    try:
        c = wsp.open_context(rec.get("workspace") or S.workspace)
    except Exception as e:  # noqa: BLE001
        return {"problem": f"the workspace {rec.get('workspace')!r} could not be opened here: {e}"}
    sel = rec.get("selected") or {}

    def look(name, fn):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"problem": str(e)[:300]}

    pid = (rec.get("project") or {}).get("id")
    if pid:
        def project():
            from .. import project_state as PS

            st = PS.stages(c, pid)
            return {"id": pid, "inputs": [{k: i.get(k) for k in ("slot", "label", "pinned", "required", "newest", "next") if i.get(k) is not None}
                                           for i in st.get("inputs", [])],
                    "evaluation": st.get("evaluation"), "experiments": st.get("experiments"), "next": st.get("next")}
        look("project", project)
    if sel.get("dataset"):
        def dataset():
            m = c.registry.load("dataset", str(sel["dataset"]), sel.get("version") if isinstance(sel.get("version"), int) else None)
            d = m.model_dump(mode="json")
            cols = d.get("schema_fields") or []
            return {"name": m.name, "version": m.version, "kind": d.get("dataset_kind"), "rows": d.get("row_count"),
                    "columns": [f"{f.get('name')} {f.get('type')}" for f in cols[:40]], "more_columns": max(0, len(cols) - 40),
                    "external": bool(d.get("external")), "created": d.get("created_at")}
        look("dataset", dataset)
    if sel.get("backtest"):
        def backtest():
            name = str(sel["backtest"])
            m = c.registry.load("backtest", name)
            runs = c.registry.versions("backtest-run", name)
            last = c.registry.load("backtest-run", name, runs[-1]) if runs else None
            return {"name": name, "arms": m.arms, "primary_metric": m.preregistration.primary_metric, "mode": m.mode,
                    "status": "finished" if last else "planned, not run yet", "runs": len(runs),
                    "last_run": last.created_at.isoformat() if last else None,
                    "last_run_graded_enough": last.complete if last else None}
        look("offline_evaluation", backtest)
    if sel.get("experiment"):
        def experiment():
            from .. import investigations as INV

            d = INV.get(c, str(sel["experiment"]))
            plan = (d.get("revisions") or [{}])[-1]
            return {"id": d.get("id"), "title": d.get("title"), "state": d.get("state"), "hypothesis": plan.get("hypothesis"),
                    "decision": (d.get("decision") or {}).get("outcome"), "runs": len(d.get("runs") or [])}
        look("experiment", experiment)
    if sel.get("launch_review"):
        def review():
            from .. import handoff as HO

            d = HO.get(c, str(sel["launch_review"]))
            return {k: d.get(k) for k in ("id", "pipeline", "state", "status", "ready") if d.get(k) is not None}
        look("launch_review", review)
    return scrub(out)


def _say(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, default=str)


def _text(rec: dict) -> str:
    p = rec.get("project") or {}
    rows = [("Development backend", f"Captain's services run from {rec['dev_backend']}, not from the installed app") if rec.get("dev_backend") else ("", None),
            ("Workspace", rec.get("workspace")),
            ("Project", (f"{p.get('name') or p.get('id')}" + (f" (id {p['id']})" if p.get("id") and p.get("id") != p.get("name") else "")
                         + (f", {p['kind']}" if p.get("kind") else "")) if p else None),
            ("Explorer pick", f"{rec['explorer_pick']['kind']} {rec['explorer_pick'].get('label') or rec['explorer_pick']['id']}"
             if rec.get("explorer_pick") and not p else None),
            ("Screen", f"{rec.get('screen')}" + (f", tab {rec['tab']}" if rec.get("tab") else "") + (f"  [section id: {rec['section']}]" if rec.get("section") else "")
             if rec.get("screen") else None)]
    sel = dict(rec.get("selected") or {})
    steps = sel.pop("steps", None)
    rows += [(k[0].upper() + k[1:].replace("_", " "), _say(v)) for k, v in sel.items()]
    # A guided start on the page (a project with nothing trained yet): each step, whether it is done, and what does it.
    for st in steps if isinstance(steps, list) else []:
        if isinstance(st, dict):
            rows.append(("Step", f"{st.get('label')}: " + ("done" if st.get("done") else "to do")
                         + (f"  [action: {st['action']}]" if st.get("action") else "")
                         + (f"  ({st['waiting_on']})" if st.get("waiting_on") else "")))
    d = rec.get("dialog")
    if d:
        rows.append(("Open form", f"{d.get('title') or d.get('form')}  [form id: {d.get('form')}]"))
        rows += [(f"  {k}", _say(v)) for k, v in (d.get("fields") or {}).items()]
    if rec.get("open_over_the_page"):
        rows.append(("Open over the page", ", ".join(rec["open_over_the_page"])))
    n = rec.get("next")
    if n:
        rows.append(("Next", f"{n.get('text')}" + (f"  (button: {n['label']})" if n.get("label") else "")
                     + (f"  [action: {_say(n['action'])}]" if n.get("action") else "")))
    rows.append(("Agent tab", rec.get("agent_tab") or ("none open" if rec.get("terminals_shown") is not None else None)))
    rows = [(k, v) for k, v in rows if v not in (None, "")]
    w = max(len(k) for k, _ in rows) if rows else 0
    lines = [f"What the person is looking at in Captain (read {rec.get('at') or 'now'}):"] + [f"  {k:{w}}  {v}" for k, v in rows]
    if not rec.get("selected") and not rec.get("published_at") and rec.get("section"):
        lines.append("  (This screen hasn't said what is selected on it.)")
    kn = rec.get("known") or {}
    if kn:
        lines.append("What Captain's services know about it:")
        if kn.get("problem"):
            lines.append(f"  {kn['problem']}")
        pr = kn.get("project")
        if pr and pr.get("problem"):
            lines.append(f"  Project: {pr['problem']}")
        elif pr:
            for i in pr.get("inputs") or []:
                state = i.get("pinned") or ("missing, required" if i.get("required") else "not chosen")
                lines.append(f"  Input {i.get('label') or i.get('slot')}: {state}" + (f"  (newest {i['newest']})" if i.get("newest") and i.get("newest") != i.get("pinned") else ""))
            for k in ("evaluation", "experiments", "next"):
                if pr.get(k):
                    lines.append(f"  Project {k}: {_say(pr[k])}")
        for k in ("dataset", "offline_evaluation", "experiment", "launch_review"):
            if kn.get(k):
                lines.append(f"  {k[0].upper() + k[1:].replace('_', ' ')}: {_say(kn[k])}")
    lines.append("For a picture of the window: cst app shot")
    return "\n".join(lines)


@app_app.command("context")
def app_context(bare: bool = typer.Option(False, "--bare", help="Only what the window says, without asking the services")):
    """What the person is looking at in Captain right now: workspace, project, screen, the selected item, an open
    form and the page's Next line, with what the services know about them. Run it when they say "this" or "here"."""
    rec = _record(_window({"view": True}))
    if not bare:
        rec["known"] = _known(rec)
    emit(rec, _text(rec))


@app_app.command("shot")
def app_shot(path: Optional[str] = typer.Argument(None, help="Where to save the PNG. Default: Captain's own cache folder")):
    """Save a picture of Captain's window as a PNG and print its path, so an agent can look at the screen."""
    import time

    if path:
        out = os.path.abspath(os.path.expanduser(path))
        if not out.lower().endswith(".png"):
            fail("the picture's path must end in .png")
    else:
        folder = os.path.join(window_home(), "cache", "shots")
        os.makedirs(folder, exist_ok=True)
        # Keep the folder small: the newest 20 pictures.
        old = sorted(f for f in os.listdir(folder) if f.startswith("captain-") and f.endswith(".png"))
        for f in old[:-19]:
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass
        out = os.path.join(folder, time.strftime("captain-%Y%m%d-%H%M%S.png"))
    got = _window({"shot": out}, timeout=35)
    emit({"path": got.get("path", out)}, got.get("path", out))


# --- The agent's context: the screen, plus what the person pinned ------------------------------------------------

context_app = typer.Typer(no_args_is_help=True, help="What the person gave their agent: the screen they're on, and the things they pinned")
app.add_typer(context_app, name="context")


def _ctx_do(fn, *a, **kw):
    from ..storage.backend import NotFound

    try:
        return fn(*a, **kw)
    except (ValueError, NotFound, KeyError) as e:
        fail(str(e).strip("'\""))


def _pin_ctx(view: dict | None):
    """The workspace the pins belong to: the one the window shows, else -w, else the current one."""
    from .main import ctx

    if view and view.get("workspace") and not S.workspace:
        from .. import workspace as wsp

        try:
            return wsp.open_context(view["workspace"])
        except Exception:  # noqa: BLE001  the window shows a workspace this cst can't open: fall back
            pass
    return ctx()


def _live_view() -> dict | None:
    try:
        return _record(_window({"view": True}, quiet=True))
    except NotRunning:
        return None


def _pins_text(items: list[dict]) -> list[str]:
    if not items:
        return ["Pinned for the agent: nothing yet. Add with `cst context add <ref>`, or \"Send to agent\" in Captain."]
    lines = [f"Pinned for the agent ({len(items)}):"]
    for i in items:
        who = i.get("agent") or i.get("added_by") or "?"
        lines.append(f"  {i['ref']}   (added by {who}, {str(i.get('added_at') or '')[:16].replace('T', ' ')})")
        for k, v in (i.get("summary") or {}).items():
            if v in (None, "", [], {}):
                continue
            if k == "sample" and isinstance(v, list):
                lines.append("      sample rows:")
                lines += [f"        {_say(r)}" for r in v]
            elif k == "first_lines" and isinstance(v, list):
                lines.append("      first lines:")
                lines += [f"        {r}" for r in v]
            else:
                lines.append(f"      {k.replace('_', ' ')}: {_say(v)}")
    return lines


@context_app.command("show")
def context_show(bare: bool = typer.Option(False, "--bare", help="Names only: no summaries, nothing asked of the services")):
    """The current screen (when Captain is open) and every pinned thing, each with a short summary: a dataset's kind,
    rows, columns and sample rows, a run's status and metrics, a project's inputs and next step."""
    from .. import agent_context as AC

    view = _live_view()
    c = _pin_ctx(view)
    items = AC.load(c.workspace.name)
    if not bare:
        if view is not None:
            view["known"] = _known(view)
        items = [{**i, "summary": AC.expand(c, i)} for i in items]
    items = scrub(items)
    out = {"workspace": c.workspace.name, "view": view, "captain_running": view is not None, "pinned": items}
    text = ([_text(view)] if view is not None else ["Captain isn't open, so the current screen is unknown. Pinned things still are:"]) + [""] + _pins_text(items)
    emit(out, "\n".join(text))


@context_app.command("add")
def context_add(refs: list[str] = typer.Argument(..., help="dataset:<name>[@vN], model:<name>@vN, run:<evaluation>, experiment:<id>, candidate:<project>/<id>, table:<connection>.<table>, project:<id>, or a file path. A bare name is looked up")):
    """Pin things for the agent. They stay until removed, and `cst context show` expands each."""
    from .. import agent_context as AC
    from ..proposals import agent_name

    c = _pin_ctx(_live_view())
    if S.dry_run:
        return emit({"would_add": [_ctx_do(AC.resolve, c, r) for r in refs]})
    got = _ctx_do(AC.add, c, refs, agent_name())
    emit(got, "\n".join(f"Pinned {i['ref']}" for i in got))


@context_app.command("rm")
def context_rm(ref: str):
    """Remove one pinned thing, by its ref or its name."""
    from .. import agent_context as AC

    c = _pin_ctx(_live_view())
    gone = _ctx_do(AC.remove, c, ref)
    emit(gone, "\n".join(f"Removed {i['ref']}" for i in gone))


@context_app.command("clear")
def context_clear():
    """Remove every pinned thing. The current screen is always part of the context and isn't something to clear."""
    from .. import agent_context as AC

    c = _pin_ctx(_live_view())
    n = _ctx_do(AC.clear, c)
    emit({"removed": n}, f"Removed {n} pinned thing{'s' if n != 1 else ''}.")


# --- Showing the person something ---------------------------------------------------------------------------------

SECTIONS = {"home": "project", "project": "project", "experiments": "investigations", "investigations": "investigations",
            "data": "data", "data-and-signals": "data", "runs": "runs", "compare": "comparisons", "comparisons": "comparisons",
            "launch-review": "handoff", "handoff": "handoff", "team": "team"}


@app_app.command("open")
def app_open(target: str = typer.Argument(..., help="A screen (home, experiments, data, runs, compare, launch-review, team), project:<id>, dataset:<name>, run:<evaluation>, comparison:<evaluation>[@vN], experiment:<id>, form:<task>, settings[:<section>]"),
             value: list[str] = typer.Option([], "--value", "-v", help="For form:<task>: field=value to fill in, repeatable. The person confirms; nothing is submitted"),
             tab: Optional[str] = typer.Option(None, "--tab", help="A tab on the screen opened (a dataset's sample, a comparison's queries)")):
    """Show the person something in Captain: a screen, a project, a dataset, a run, a comparison, an experiment,
    Settings, or a form filled in for them to confirm. It never submits a form or changes a record."""
    from .. import agent_context as AC
    from ..commands import SECRET_NAMES

    head, sep, rest = target.strip().partition(":")
    head = head.lower()
    view = _live_view()
    if view is None:
        fail("Captain isn't running, so there is nothing to open it in. Open Captain and try again.")
    if value and head != "form":
        fail("--value fills a form: use it with form:<task>")
    what: dict
    if not sep and head in SECTIONS:
        what = {"section": SECTIONS[head]}
    elif head == "settings":
        what = {"settings": rest or None}
    elif head == "form":
        if not re.fullmatch(r"[a-z][a-z0-9-]*", rest or ""):
            fail("say which form: form:<task>, for example form:investigation, form:backtest, form:database, form:experiment")
        vals = {}
        for item in value:
            k, eq, v = item.partition("=")
            if not eq:
                fail(f"--value takes field=value (got {item!r})")
            if k.lower() in SECRET_NAMES or SECRET_KEY.search(k) or scrub(v) != v:
                fail(f"{k} looks like a secret. Leave it out: the person types secrets into the form themselves.")
            vals[k] = v
        what = {"form": rest, "values": vals}
    elif head in ("project", "dataset", "run", "comparison", "experiment", "investigation", "evaluation", "backtest") and rest:
        c = _pin_ctx(view)
        kind = {"comparison": "run"}.get(head, head)
        version = None
        if head == "comparison" and "@v" in rest:
            rest, _, v = rest.partition("@v")
            version = int(v) if v.isdigit() else None
        it = _ctx_do(AC.resolve, c, f"{kind}:{rest}")
        name = it["ref"].split(":", 1)[1]
        if it["kind"] == "project":
            what = {"project": name, "label": it["label"]}
        elif it["kind"] == "dataset":
            what = {"section": "data", "dataset": name.split("@v")[0]}
        elif it["kind"] == "run":
            what = {"section": "comparisons" if head == "comparison" else "runs", "backtest": name, **({"version": version} if version else {})}
        else:
            what = {"section": "investigations", "investigation": name}
    else:
        fail(f"don't know how to open {target!r}. Try a screen (home, experiments, data, runs, compare, launch-review, team), "
             "project:<id>, dataset:<name>, run:<evaluation>, comparison:<evaluation>, experiment:<id>, form:<task> or settings")
    if tab:
        what["tab"] = tab
    if S.dry_run:
        return emit({"would_open": what})
    _window({"open": what})
    emit({"opened": what}, f"Opened {target} in Captain." + (" The form is filled in and waiting for the person: nothing was submitted." if "form" in what else ""))


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
