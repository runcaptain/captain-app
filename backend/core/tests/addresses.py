"""Shared by the address tests: every route the app serves, and every address the screens' code names.

Two failures these catch. A screen calls an address nothing serves (or serves with another method): the screen
breaks while the function behind it passes its own tests. Two routes share a method and an address: the first
registered wins and the second can never be reached."""
from __future__ import annotations

import re
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[3] / "defaults"


def routes(app) -> list[tuple[str, str]]:
    """(method, address) for every route, including those of routers the app includes. Newer FastAPI keeps an
    included router as one wrapper object in app.routes, so its routes are read from the router itself."""
    out = []

    def walk(items, prefix=""):
        for r in items:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                walk(inner.routes, prefix + (getattr(getattr(r, "include_context", None), "prefix", "") or ""))
            elif getattr(r, "methods", None):
                out.extend((m, prefix + r.path) for m in r.methods if m != "HEAD")

    walk(app.routes)
    return out


def _template(src: str, i: int) -> tuple[str, int]:
    """The text of a JavaScript template literal starting at src[i] == "`", with each ${...} (nested braces and
    all) replaced by one \\x00, and the index after its closing backtick."""
    out, i = [], i + 1
    while i < len(src) and src[i] != "`":
        if src.startswith("${", i):
            depth, i = 1, i + 2
            while i < len(src) and depth:
                if src[i] == "`":  # a template inside the value
                    _, i = _template(src, i)
                    continue
                depth += {"{": 1, "}": -1}.get(src[i], 0)
                i += 1
            out.append("\x00")
        else:
            out.append(src[i])
            i += 1
    return "".join(out), i + 1


def named(files) -> tuple[dict[tuple[str, str], list[str]], int]:
    """Every address given to api(...) as text in these files: {(method, address): [file:line]}, and how many
    calls pass a variable, which can't be read here. In an address, \\x00 stands for a value filled in at run
    time. The method is the call's `method:`, else POST when it sends a `body`, else GET."""
    found: dict[tuple[str, str], list[str]] = {}
    variable = 0
    for f in files:
        src = f.read_text()
        for m in re.finditer(r"(?<![\w.$])api\(\s*", src):
            i = m.end()
            if i >= len(src):
                continue
            if src[i] == "`":
                path, j = _template(src, i)
            elif src[i] in "\"'":
                j = src.index(src[i], i + 1) + 1
                path = src[i + 1:j - 1]
            else:
                variable += 1
                continue
            if not path.startswith("/api/"):
                continue
            depth, k = 0, j  # the rest of this call: its options
            while k < len(src) and k < j + 2000:
                if src[k] in "({[":
                    depth += 1
                elif src[k] in ")}]":
                    if depth == 0:
                        break
                    depth -= 1
                k += 1
            opts = src[j:k]
            mm = re.search(r"\bmethod:\s*[\"'`]([A-Z]+)", opts)
            if not mm and re.search(r"\bmethod\b", opts):
                continue  # the method is worked out at run time
            method = mm.group(1) if mm else "POST" if re.search(r"(^|[{,\s])body\b", opts) else "GET"
            line = src[:m.start()].count("\n") + 1
            if src[src.rfind("\n", 0, m.start()) + 1:m.start()].lstrip().startswith("//"):
                continue  # an example in a comment
            found.setdefault((method, path.split("?")[0]), []).append(f"{f.relative_to(DEFAULTS)}:{line}")
    return found, variable


def served(all_routes: list[tuple[str, str]], method: str, path: str) -> bool:
    """Is there a route with this method whose address this one can be? A value at the very end may be a query
    string (`/api/datasets${query(...)}`), so it may also stand for nothing."""
    body, tail = (path[:-1], True) if path.endswith("\x00") else (path, False)
    pat = "".join("[^/]+" if ch == "\x00" else re.escape(ch) for ch in body)
    # a value in the middle may also be more than one part of the address: `/api/projects/${id}${rest}`
    pat = pat.replace("[^/]+[^/]+", ".+")
    rx = re.compile("^" + pat + ("(/?[^/]*)?" if tail else "") + "/?$")
    examples = [(m, re.sub(r"\{[^}]+\}", "x", p)) for m, p in all_routes]
    return any(m == method and rx.match(p) for m, p in examples)


def screen_files() -> list[Path]:
    """Every screen's own code and the shared kit. Each mod carries a copy of the kit: the kit is read once, from
    where it is kept."""
    mods = [f for f in DEFAULTS.glob("mods/**/*.js") if "/kit/" not in f.as_posix()]
    return sorted(mods + list(DEFAULTS.glob("captain/kit/*.js")) + list(DEFAULTS.glob("frame/**/*.js")))
