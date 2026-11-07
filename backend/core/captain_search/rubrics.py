"""Domain rubrics as code (plan 4): what "relevant" means for a team's domain, written once and used everywhere.

A rubric is a Markdown file in the team's captain/rubrics/ folder (or any path). The LLM judge is prompted with it,
and the grading queue shows it to human graders, so people and the model grade against the same words. Grades record
which rubric (name and content hash) they were made under, and grades from different rubrics are never mixed into one
dataset.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def resolve(name_or_path: str, workspace: str | None = None) -> dict:
    """{name, path, text, sha}. A path wins; otherwise captain/rubrics/<name>.md in the project folder."""
    p = Path(os.path.expanduser(name_or_path))
    if not p.is_file():
        from .extensions import find_project

        proj = find_project(workspace)
        cand = proj / "rubrics" / f"{name_or_path}.md" if proj else None
        if not cand or not cand.is_file():
            raise ValueError(f"no rubric {name_or_path!r}: give a file path, or add captain/rubrics/{name_or_path}.md "
                             "to your project folder")
        p = cand
    text = p.read_text().strip()
    if not text:
        raise ValueError(f"{p} is empty")
    return {"name": p.stem, "path": str(p), "text": text, "sha": hashlib.sha256(text.encode()).hexdigest()}


def listing(workspace: str | None = None) -> list[dict]:
    from .extensions import find_project

    proj = find_project(workspace)
    d = proj / "rubrics" if proj else None
    if not d or not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        r = resolve(str(f))
        out.append({k: r[k] for k in ("name", "path", "sha")} | {"preview": r["text"][:400]})
    return out
