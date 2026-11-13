"""Names a person types, and the ids Captain keeps.

A person names things in their own words: "GPU cloud", "Outdoor store search". Captain never refuses a name for
its spelling. It keeps the words as the label and makes its own id from them, with one rule used everywhere:

    lower case; spaces and underscores become hyphens; anything else that is not a letter, a digit or a hyphen is
    dropped; hyphens are collapsed and trimmed from the ends; at most 60 characters.

    "GPU cloud"           ->  gpu-cloud
    "  Über Größe #2!  "  ->  ber-gre-2      (letters outside a to z are dropped, never an error)
    "!!!"                 ->  the fallback for that kind of thing ("login", "project", ...)

When the id is taken, `free` gives the next free one: gpu-cloud-2, gpu-cloud-3. The routes that store things still
check ids themselves. This module is how a form never has to send one that fails.
"""
from __future__ import annotations

import re

MAX = 60
VALID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# What an unnamed thing of each kind is called, and the word for it in a sentence.
KINDS = {"cloud": ("login", "cloud account"), "project": ("project", "project"), "compute": ("place", "place to run work"),
         "input": ("input", "input"), "dataset": ("dataset", "dataset"), "folder": ("folder", "folder"), "model": ("model", "model"),
         "test": ("test", "test"), "notebook": ("notebook", "notebook"), "connection": ("connection", "connection")}


def make_id(label, fallback: str = "item") -> str:
    """The id Captain makes from what a person typed. Never raises, and always returns a valid id."""
    s = re.sub(r"[\s_]+", "-", str(label or "").strip().lower())
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")[:MAX].strip("-")
    return s or fallback


def is_id(s) -> bool:
    return bool(VALID.match(str(s or ""))) and len(str(s)) <= MAX


def free(wanted: str, taken) -> str:
    """`wanted` if nothing has it, else the next free one: name-2, name-3, ..."""
    have = {str(t) for t in taken or ()}
    if wanted not in have:
        return wanted
    base = re.sub(r"-\d+$", "", wanted) or wanted
    n = 2
    while f"{base[:MAX - 4]}-{n}" in have:
        n += 1
    return f"{base[:MAX - 4]}-{n}"


def propose(label, kind: str, taken) -> dict:
    """What a form shows under a name field: {label, id, taken, next, text}. `text` is the one line to show."""
    fallback, word = KINDS.get(kind, ("item", "item"))
    said = " ".join(str(label or "").split())
    wanted = make_id(said, fallback)
    nxt = free(wanted, taken)
    is_taken = nxt != wanted
    return {"label": said, "id": wanted, "taken": is_taken, "next": nxt,
            "text": (f"You already have a {word} called {wanted}. Captain will call this one {nxt}." if is_taken
                     else f"Captain will call it {wanted}.")}
