"""cst docs: the guides that ship inside the package, so they can be read wherever cst runs.

An agent in a workspace folder, or a person with only the app installed, has no repo checkout to open
docs/trainer-contract.md in. The text lives in captain_search/docs/ and this prints it."""
from __future__ import annotations

from importlib import resources
from typing import Optional

import typer

from .main import app, emit, fail


def _docs() -> dict[str, str]:
    """name -> first heading, for every guide in the package."""
    out = {}
    for f in resources.files("captain_search").joinpath("docs").iterdir():
        if f.name.endswith(".md"):
            head = next((ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), "# " + f.name)
            out[f.name[:-3]] = head[2:].strip()
    return dict(sorted(out.items()))


def read(name: str) -> str:
    return resources.files("captain_search").joinpath("docs", f"{name}.md").read_text(encoding="utf-8")


@app.command("docs")
def docs(name: Optional[str] = typer.Argument(None, help="which guide, such as trainer-contract. Without it: the list")):
    """Print a guide that ships with cst, such as `cst docs trainer-contract`."""
    have = _docs()
    if not name:
        return emit([{"name": k, "title": v} for k, v in have.items()], "\n".join(f"{k:20} {v}" for k, v in have.items()))
    if name not in have:
        fail(f"no guide {name!r}. There is: {', '.join(have)}")
    text = read(name)
    emit({"name": name, "title": have[name], "text": text}, text)
