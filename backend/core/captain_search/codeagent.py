"""Setup for the coding agent the desktop app hosts (Claude Code or Codex in a terminal).

The daemon gives the app a working folder per workspace with AGENTS.md / CLAUDE.md that explain
`cst`, plus the built-in skills as Claude Code skills. The agent then drives everything with
`cst ... --json`, the same code path as the app and the CLI.
"""
from __future__ import annotations

import os
import shutil

from . import skills as SK
from .workspace import Context, home

GUIDE = """# Captain Search Tools workspace: {ws}

You are a coding agent inside Captain Search Tools. Use the `cst` command line for everything; add `--json` to
read results. Storage: `{uri}`. Never write to the bucket any other way.

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
  propose with `cst projects propose <p> --file proposal.yaml`. A person accepts it in the app: never apply it yourself
- `cst skill list`; skills are also in .claude/skills/

Connections: {conns}
"""


def prepare(ctx: Context) -> dict:
    cwd = os.path.join(home(), "agent", ctx.workspace.name)
    os.makedirs(cwd, exist_ok=True)
    conns = ", ".join(ctx.connections()) or "none yet"
    text = GUIDE.format(ws=ctx.workspace.name, uri=ctx.workspace.storage_uri, conns=conns)
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
    cst_dir = os.path.dirname(shutil.which("cst") or os.path.expanduser("~/.local/bin/cst"))
    path = os.pathsep.join([cst_dir, os.path.expanduser("~/.local/bin"), os.environ.get("PATH", "")])
    # The app runs plain shells (Edgar, 2026-11-06: "we wanted a zsh, not claude code or codex").
    # People start any agent they like by typing it; AGENTS.md and CLAUDE.md are here for them.
    sh = os.environ.get("SHELL") or shutil.which("zsh") or "/bin/zsh"
    shell = {"id": "shell", "label": os.path.basename(sh), "command": [sh, "-l"], "available": True}
    return {"cwd": cwd, "env": {"CST_HOME": home(), "CST_WORKSPACE": ctx.workspace.name, "PATH": path},
            "shell": shell, "agents": [shell]}
