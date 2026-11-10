"""Setup for the coding agent the desktop app hosts (Claude Code or Codex in a terminal).

The daemon gives the app a working folder per workspace with AGENTS.md / CLAUDE.md that explain
`cst`, plus the built-in skills as Claude Code skills. The agent then drives everything with
`cst ... --json`, the same code path as the app and the CLI.
"""
from __future__ import annotations

import os
import shlex
import shutil
import sys

from . import skills as SK
from .workspace import Context, home

def cst_path() -> str:
    """The cst this process is, as an absolute path: what an agent it hands work to must run.

    A person can have an older cst first on PATH (an install.sh copy in ~/.local/bin), without the commands a prompt
    names. So a prompt never says just `cst`: it names this path. $CST_BIN wins (the app sets it in every terminal
    it opens, and for its own services)."""
    exe_dir = os.path.dirname(sys.executable or "")
    arg0 = sys.argv[0] if sys.argv else ""
    cands = [os.environ.get("CST_BIN"),
             os.path.abspath(arg0) if os.path.basename(arg0) == "cst" and os.sep in arg0 else None,
             # Captain.app: Resources/cst/python/bin/python3 is run by the launcher Resources/cst/bin/cst
             os.path.join(os.path.dirname(os.path.dirname(exe_dir)), "bin", "cst")
             if os.path.basename(os.path.dirname(exe_dir)) == "python" else None,
             os.path.join(exe_dir, "cst")]  # a venv or a pip install: the script beside its python
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which("cst") or "cst"


def cst_line() -> str:
    """The first lines of every prompt the services build for an agent."""
    c = cst_path()
    return (f"Use this cst: {c}\n"
            f"Run every `cst` command below with that exact path (it is also in $CST_BIN when Captain opened your "
            f"terminal). An older `cst` may come first on your PATH: if `{shlex.quote(c)} projects --help` fails, you "
            f"are on an old copy.")


GUIDE = """# Captain Search Tools workspace: {ws}

You are a coding agent inside Captain Search Tools. Use the `cst` command line for everything; add `--json` to
read results. Storage: `{uri}`. Never write to the bucket any other way.

Use this cst: `{cst}` (also `$CST_BIN`). An older `cst` may come first on your PATH: if `cst projects --help` fails,
you are on an old copy, so run the path above.

## Rules
- Versions are write-once. A change is a new version (`cst pipeline publish`, `cst data snapshot`, `--save`).
- SQL is read-only unless a connection is marked writable. Aggregate in SQL; laptop results cap at 1M rows.
- Every metric carries an evidence label: SIMULATED, LLM_GRADED, LOGGED_CLICKS, HUMAN_GRADED or REAL_TRAFFIC.
  Say the label whenever you report a number. Simulated numbers never prove revenue.
- nDCG comes only from judgments. Clicks, conversion and revenue come only from shopper logs.
- A gain under the pre-registered MDE (default 0.003 nDCG@10), or a CI that crosses zero, is noise.
- Pre-register before running: `cst backtest plan ... --hypothesis "..."`, then `cst backtest run`.
- Live deploys need --confirm and pass gates. Do not use --override-reason unless the person asks.

## Useful commands
- `cst data list --json`, `cst data head <ref>`, `cst sql <conn> -e "..." --save <folder/name> --kind <kind>`
- `cst queries gen --catalog <ref> --name <n>`, `cst queries seal <ref> --holdout 0.3`
- `cst judge run --queries <ref>:holdout --pipelines a@v1,b@v1 --judge llm --into <n>`
- `cst judge from-logs <logs ref> --into <n>` (grades from historic clicks)
- `cst pipeline new <n> --engine local-bm25 --catalog <ref>`, edit the YAML, `cst pipeline publish <file>`
- `cst backtest plan/run/report/verify`, `cst deploy promote <ref> --env <e>`
- `cst notebook import <file>` (ipynb, jupytext, Databricks, marimo, Quarto, R Markdown, MyST, Zeppelin)
- `cst models list --json` (the model registry), `cst lineage --view models`
- `cst projects kinds`, `cst projects state <p> --json`, `cst projects sources --json`. To help set up a project,
  propose with `cst projects propose <p> --file proposal.yaml`. A person accepts it in the app. You cannot apply it,
  even when asked in chat: tell the person to press Accept in Captain (Home, the proposal panel). While your proposal
  is open, `cst projects slot-add`, `input` and `pin` refuse an agent session
- `cst docs trainer-contract`: what a training command gets (CST_TRAIN_INPUTS, CST_PARAMS, CST_OUT) and writes
- `cst skill list`; skills are also in .claude/skills/

Connections: {conns}
"""


def prepare(ctx: Context) -> dict:
    cwd = os.path.join(home(), "agent", ctx.workspace.name)
    os.makedirs(cwd, exist_ok=True)
    conns = ", ".join(ctx.connections()) or "none yet"
    cst = cst_path()
    text = GUIDE.format(ws=ctx.workspace.name, uri=ctx.workspace.storage_uri, conns=conns, cst=cst)
    for f in ("AGENTS.md", "CLAUDE.md"):
        with open(os.path.join(cwd, f), "w") as fh:
            fh.write(text)
    sk = os.path.join(cwd, ".claude", "skills")
    os.makedirs(sk, exist_ok=True)
    for s in SK.listing(ctx):
        d = os.path.join(sk, s["name"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w") as fh:
            fh.write(SK.read(ctx, s["name"]))
    # This cst first, never whichever is first on PATH: that can be an older install without today's commands.
    path = os.pathsep.join([os.path.dirname(os.path.abspath(cst)), os.path.expanduser("~/.local/bin"), os.environ.get("PATH", "")])
    # The app runs plain shells (Edgar, 2026-11-06: "we wanted a zsh, not claude code or codex").
    # People start any agent they like by typing it; AGENTS.md and CLAUDE.md are here for them.
    sh = os.environ.get("SHELL") or shutil.which("zsh") or "/bin/zsh"
    shell = {"id": "shell", "label": os.path.basename(sh), "command": [sh, "-l"], "available": True}
    return {"cwd": cwd, "env": {"CST_HOME": home(), "CST_WORKSPACE": ctx.workspace.name, "PATH": path, "CST_BIN": cst},
            "shell": shell, "agents": [shell]}
