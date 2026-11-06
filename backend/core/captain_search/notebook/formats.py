"""Notebook format converters.

Every format goes through an nbformat 4 dict with `metadata.captain` on each cell:
ipynb, jupytext (percent .py), databricks (.py source), marimo (.py), quarto (.qmd), rmarkdown (.Rmd),
myst (.md) and zeppelin (.zpln).

Cell kinds: markdown, python, sql (with conn / var), chart, and raw. Raw cells hold code in a language
Captain does not run (Scala, R, shell...); `metadata.captain.lang` remembers the language so an export to
the same format writes it back.

Formats with no slot for a SQL connection (Databricks, marimo) carry it in a first SQL comment line:
`-- captain: conn=demo-store var=zero`. It is valid SQL, so the notebook still runs where it came from.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import textwrap
import uuid

import yaml

from . import new_notebook, normalize

FORMATS: list[dict] = [
    {"id": "ipynb", "label": "Jupyter notebook", "extensions": [".ipynb"], "import": True, "export": True},
    {"id": "jupytext", "label": "Jupytext percent script", "extensions": [".py"], "import": True, "export": True},
    {"id": "databricks", "label": "Databricks notebook source", "extensions": [".py"], "import": True, "export": True},
    {"id": "marimo", "label": "marimo notebook", "extensions": [".py"], "import": True, "export": True},
    {"id": "quarto", "label": "Quarto document", "extensions": [".qmd"], "import": True, "export": True},
    {"id": "rmarkdown", "label": "R Markdown", "extensions": [".Rmd"], "import": True, "export": True},
    {"id": "myst", "label": "MyST Markdown notebook", "extensions": [".md"], "import": True, "export": True},
    {"id": "zeppelin", "label": "Zeppelin note", "extensions": [".zpln"], "import": True, "export": True},
]

_MIME = {
    "ipynb": "application/x-ipynb+json", "jupytext": "text/x-python", "databricks": "text/x-python",
    "marimo": "text/x-python", "quarto": "text/markdown", "rmarkdown": "text/markdown", "myst": "text/markdown",
    "zeppelin": "application/json",
}
_EXT = {".ipynb": "ipynb", ".qmd": "quarto", ".rmd": "rmarkdown", ".md": "myst", ".myst": "myst", ".zpln": "zeppelin"}
_OUT_EXT = {"ipynb": ".ipynb", "jupytext": ".py", "databricks": ".py", "marimo": ".py", "quarto": ".qmd",
            "rmarkdown": ".Rmd", "myst": ".md", "zeppelin": ".zpln"}

MARIMO_ORDER_WARNING = ("marimo runs cells in dependency order; Captain runs them top to bottom, "
                        "so the reactive execution order is not kept")


# ---------------------------------------------------------------------------------------------- public API

def detect(filename: str, text: str) -> str:
    """Format id from the extension, and from the content for .py files."""
    name = (filename or "").lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext in _EXT:
        return _EXT[ext]
    if ext == ".py":
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if first == "# Databricks notebook source":
            return "databricks"
        if re.search(r"^import marimo\b", text, re.M) or re.search(r"^app\s*=\s*marimo\.App\(", text, re.M):
            return "marimo"
        return "jupytext"
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and "cells" in data:
            return "ipynb"
        if isinstance(data, dict) and "paragraphs" in data:
            return "zeppelin"
    raise ValueError(f"cannot tell the notebook format of {filename!r}")


def to_ipynb(filename: str, text: str) -> tuple[dict, str, list[str]]:
    """Read any supported format. Returns (nbformat 4 notebook, format id, warnings)."""
    fmt = detect(filename, text)
    text = text.replace("\r\n", "\n")
    warnings: list[str] = []
    if fmt == "ipynb":
        nb = json.loads(text)
    else:
        nb = _READERS[fmt](text, warnings)
    return normalize(nb), fmt, warnings


def from_ipynb(nb: dict, fmt: str) -> tuple[str, str, str]:
    """Write a notebook in another format. Returns (file extension with the dot, text, mime type)."""
    if fmt not in _OUT_EXT:
        raise ValueError(f"unknown notebook format {fmt!r}")
    nb = normalize(copy.deepcopy(nb))
    if fmt == "ipynb":
        text = json.dumps(nb, indent=1, ensure_ascii=False) + "\n"
    else:
        text = _WRITERS[fmt](nb)
    return _OUT_EXT[fmt], text, _MIME[fmt]


# ---------------------------------------------------------------------------------------------- shared helpers

def _cell(cell_type: str, source: str, captain: dict | None = None) -> dict:
    c = {"id": uuid.uuid4().hex[:8], "cell_type": cell_type, "metadata": {}, "source": source}
    if captain:
        c["metadata"]["captain"] = captain
    if cell_type == "code":
        c["outputs"] = []
        c["execution_count"] = None
    return c


def _md(src: str) -> dict:
    return _cell("markdown", src)


def _py(src: str) -> dict:
    return _cell("code", src)


def _sql(src: str, conn: str | None = None, var: str | None = None) -> dict:
    src, c2, v2 = _sql_split(src)
    meta = {"kind": "sql"}
    if conn or c2:
        meta["conn"] = conn or c2
    if var or v2:
        meta["var"] = var or v2
    return _cell("code", src, meta)


def _raw(src: str, lang: str | None = None, **extra) -> dict:
    meta = {k: v for k, v in {"lang": lang, **extra}.items() if v}
    return _cell("raw", src, meta or None)


def _info(cell: dict) -> tuple[str, str, dict]:
    """(kind, source, captain metadata). Kind is markdown, python, sql, chart or raw."""
    meta = (cell.get("metadata") or {}).get("captain") or {}
    src = cell.get("source") or ""
    if isinstance(src, list):
        src = "".join(src)
    if cell.get("cell_type") == "raw":
        return "raw", src, meta
    if cell.get("cell_type") == "markdown":
        return "markdown", src, meta
    kind = meta.get("kind") or "python"
    return ("python" if kind == "markdown" else kind), src, meta


def _trim(s: str) -> str:
    """Drop blank lines at both ends, keep indentation."""
    lines = s.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(ln.rstrip() for ln in lines)


_OPT = re.compile(r"""([A-Za-z_][\w.\-]*)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^\s,]+)""")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        if v[0] == '"':
            try:
                return json.loads(v)
            except ValueError:
                pass
        return v[1:-1]
    return v


def _opts(s: str) -> dict:
    return {k: _unquote(v) for k, v in _OPT.findall(s or "")}


def _q(v: str) -> str:
    return v if re.fullmatch(r"[\w.\-/:@]+", v) else json.dumps(v)


def _optstr(**kw) -> str:
    return "".join(f" {k}={_q(str(v))}" for k, v in kw.items() if v)


_SQL_HEAD = re.compile(r"^\s*--\s*captain:(.*)$")


def _sql_split(src: str) -> tuple[str, str | None, str | None]:
    """Pull a `-- captain: conn=.. var=..` first line out of SQL text."""
    lines = src.split("\n")
    m = _SQL_HEAD.match(lines[0]) if lines else None
    if not m:
        return src, None, None
    o = _opts(m.group(1))
    return _trim("\n".join(lines[1:])), o.get("conn"), o.get("var")


def _sql_join(src: str, meta: dict) -> str:
    head = _optstr(conn=meta.get("conn"), var=meta.get("var"))
    return f"-- captain:{head}\n{src}" if head else src


def _front_matter(lines: list[str]) -> tuple[dict | None, list[str]]:
    if not lines or lines[0].strip() != "---":
        return None, lines
    for j in range(1, len(lines)):
        if lines[j].strip() in ("---", "..."):
            try:
                fm = yaml.safe_load("\n".join(lines[1:j])) or {}
            except yaml.YAMLError:
                return None, lines
            return (fm if isinstance(fm, dict) else {}), lines[j + 1:]
    return None, lines


def _notebook(cells: list[dict], front_matter: dict | None = None, kernelspec: dict | None = None) -> dict:
    nb = new_notebook()
    nb["cells"] = cells
    if front_matter:
        nb["metadata"]["captain"]["front_matter"] = front_matter
    if isinstance(kernelspec, dict) and kernelspec.get("name"):
        nb["metadata"]["kernelspec"] = kernelspec
    return nb


def _kernelspec(nb: dict) -> dict:
    ks = nb.get("metadata", {}).get("kernelspec") or {}
    return {"display_name": ks.get("display_name", "Python 3"), "language": ks.get("language", "python"),
            "name": ks.get("name", "python3")}


def _comment(src: str) -> str:
    return "\n".join(("# " + ln) if ln else "#" for ln in src.split("\n")) if src else ""


def _uncomment(lines: list[str]) -> str:
    return "\n".join(ln[2:] if ln.startswith("# ") else ln[1:] if ln.startswith("#") else ln for ln in lines)


def _unsupported(warnings: list[str], n: int, lang: str) -> None:
    warnings.append(f"cell {n}: {lang} is not supported, kept as a raw cell (not run)")


# ---------------------------------------------------------------------------------------------- jupytext percent

_PCT = re.compile(r"^# %%(?:[ \t]+(.*))?$")


def _read_jupytext(text: str, warnings: list[str]) -> dict:
    lines = text.split("\n")
    kernelspec = None
    if lines and lines[0].strip() == "# ---":
        end = next((k for k in range(1, len(lines)) if lines[k].strip() == "# ---"), None)
        if end:
            try:
                head = yaml.safe_load(_uncomment(lines[1:end])) or {}
                kernelspec = (head.get("jupyter") or {}).get("kernelspec") if isinstance(head, dict) else None
            except yaml.YAMLError:
                pass
            lines = lines[end + 1:]
    segments: list[tuple[str | None, list[str]]] = [(None, [])]
    for ln in lines:
        m = _PCT.match(ln)
        if m:
            segments.append((m.group(1) or "", []))
        else:
            segments[-1][1].append(ln)
    cells = []
    for marker, body in segments:
        if marker is None:
            if _trim("\n".join(body)):
                cells.append(_py(_trim("\n".join(body))))
            continue
        tm = re.search(r"\[([\w-]+)\]", marker)
        ctype = tm.group(1).lower() if tm else "code"
        opts = _opts(marker[tm.end():] if tm else marker)
        if ctype in ("markdown", "md"):
            cells.append(_md(_trim(_uncomment(body))))
        elif ctype == "raw":
            cells.append(_raw(_trim(_uncomment(body)), opts.get("lang")))
        elif ctype == "sql":
            cells.append(_sql(_trim("\n".join(body)), opts.get("conn"), opts.get("var")))
        else:
            src = _trim("\n".join(body))
            first, _, rest = src.partition("\n")
            mm = re.match(r"^(?:# )?%%sql\b(.*)$", first)
            if mm:
                o = _opts(mm.group(1))
                cells.append(_sql(_trim(rest), o.get("conn"), o.get("var")))
            else:
                cells.append(_py(src))
    return _notebook(cells, kernelspec=kernelspec)


def _write_jupytext(nb: dict) -> str:
    ks = _kernelspec(nb)
    head = ["# ---", "# jupyter:", "#   jupytext:", "#     text_representation:", "#       extension: .py",
            "#       format_name: percent", "#       format_version: '1.3'", "#   kernelspec:",
            f"#     display_name: {json.dumps(ks['display_name'])}", f"#     language: {ks['language']}",
            f"#     name: {ks['name']}", "# ---"]
    out = ["\n".join(head)]
    for c in nb["cells"]:
        kind, src, meta = _info(c)
        if kind == "markdown":
            out.append("# %% [markdown]\n" + _comment(src))
        elif kind == "sql":
            out.append("# %% [sql]" + _optstr(conn=meta.get("conn"), var=meta.get("var")) + "\n" + src)
        elif kind == "raw":
            out.append("# %% [raw]" + _optstr(lang=meta.get("lang")) + "\n" + _comment(src))
        else:
            out.append("# %%\n" + src)
    return "\n\n".join(s.rstrip("\n") for s in out) + "\n"


# ---------------------------------------------------------------------------------------------- databricks

_DB_HEAD = "# Databricks notebook source"
_DB_SEP = "# COMMAND ----------"


def _read_databricks(text: str, warnings: list[str]) -> dict:
    lines = text.split("\n")
    if lines and lines[0].strip() == _DB_HEAD:
        lines = lines[1:]
    chunks: list[list[str]] = [[]]
    for ln in lines:
        if ln.strip() == _DB_SEP:
            chunks.append([])
        else:
            chunks[-1].append(ln)
    cells = []
    for chunk in chunks:
        body = _trim("\n".join(ln for ln in chunk if not ln.startswith("# DBTITLE")))
        if not body:
            continue
        blines = body.split("\n")
        if not all(ln.startswith("# MAGIC") for ln in blines):
            cells.append(_py(body))
            continue
        un = [ln[8:] if ln.startswith("# MAGIC ") else ln[7:] for ln in blines]
        m = re.match(r"^%(\w+)[ \t]*(.*)$", un[0])
        if not m:
            cells.append(_py(_trim("\n".join(un))))
            continue
        magic, rest = m.group(1).lower(), m.group(2)
        inner = _trim("\n".join(([rest] if rest.strip() else []) + un[1:]))
        if magic in ("md", "markdown"):
            cells.append(_md(inner))
        elif magic == "sql":
            cells.append(_sql(inner))
        elif magic == "python":
            cells.append(_py(inner))
        else:
            _unsupported(warnings, len(cells) + 1, f"%{magic}")
            cells.append(_raw("\n".join(un), magic))
    return _notebook(cells)


def _magic(src: str) -> str:
    return "\n".join(("# MAGIC " + ln) if ln else "# MAGIC" for ln in src.split("\n"))


def _write_databricks(nb: dict) -> str:
    out = []
    for c in nb["cells"]:
        kind, src, meta = _info(c)
        if kind == "markdown":
            out.append(_magic("%md\n" + src))
        elif kind == "sql":
            out.append(_magic("%sql\n" + _sql_join(src, meta)))
        elif kind == "raw":
            lang = meta.get("lang")
            if src.startswith("%"):
                out.append(_magic(src))
            elif lang:
                out.append(_magic(f"%{lang}\n{src}"))
            else:
                out.append(_comment(src))
        else:
            out.append(src)
    return _DB_HEAD + "\n" + f"\n\n{_DB_SEP}\n\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------- marimo

def _is_app_attr(node: ast.AST, attrs: tuple[str, ...]) -> bool:
    if isinstance(node, ast.Call):
        node = node.func
    return (isinstance(node, ast.Attribute) and node.attr in attrs
            and isinstance(node.value, ast.Name) and node.value.id == "app")


def _mo_call(node: ast.AST, attr: str) -> ast.Call | None:
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "mo"):
        return node
    return None


def _string_body(text: str, node: ast.AST) -> str | None:
    seg = ast.get_source_segment(text, node)
    m = re.fullmatch(r'[rRfFuU]*("""|\'\'\'|"|\')(.*)\1', seg or "", re.S)
    return m.group(2) if m else None


def _stmts_source(lines: list[str], stmts: list[ast.stmt], floor: int) -> str:
    start = stmts[0].lineno - 1
    while start - 1 > floor and (not lines[start - 1].strip() or lines[start - 1].lstrip().startswith("#")):
        start -= 1
    return _trim(textwrap.dedent("\n".join(lines[start:stmts[-1].end_lineno])))


def _read_marimo(text: str, warnings: list[str]) -> dict:
    tree = ast.parse(text)
    lines = text.split("\n")
    cells = []
    for node in tree.body:
        if isinstance(node, ast.With) and any(_is_app_attr(i.context_expr, ("setup",)) for i in node.items):
            cells.append(_py(_stmts_source(lines, node.body, node.lineno - 1)))
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        decos = node.decorator_list
        if any(_is_app_attr(d, ("function", "class_definition")) for d in decos):
            src = "\n".join(lines[node.lineno - 1:node.end_lineno])
            cells.append(_py(_trim(textwrap.dedent(src))))
            continue
        if not any(_is_app_attr(d, ("cell",)) for d in decos):
            continue
        body = list(node.body)
        if body and isinstance(body[-1], ast.Return):
            body = body[:-1]
        if not body:
            cells.append(_py(""))
            continue
        src = _stmts_source(lines, body, node.lineno - 1)
        if src == "import marimo as mo":
            continue
        cells.append(_marimo_cell(text, body, src))
    warnings.append(MARIMO_ORDER_WARNING)
    return _notebook(cells)


def _marimo_cell(text: str, body: list[ast.stmt], src: str) -> dict:
    if len(body) == 1:
        st = body[0]
        call = _mo_call(st.value, "md") if isinstance(st, ast.Expr) else None
        if call and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            return _md(_trim(textwrap.dedent(call.args[0].value)))
        value = st.value if isinstance(st, (ast.Expr, ast.Assign)) else None
        call = _mo_call(value, "sql") if value is not None else None
        if call and call.args and isinstance(call.args[0], (ast.Constant, ast.JoinedStr)):
            sql = _string_body(text, call.args[0])
            if sql is not None:
                var = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    var = st.targets[0].id if not st.targets[0].id.startswith("_") else None
                conn = None
                for kw in call.keywords:
                    if kw.arg == "engine" and isinstance(kw.value, ast.Name):
                        conn = kw.value.id
                    elif kw.arg == "engine" and isinstance(kw.value, ast.Constant):
                        conn = str(kw.value.value)
                return _sql(_trim(textwrap.dedent(sql)), conn, var)
    return _py(src)


class _Names(ast.NodeVisitor):
    """Top-level names a cell defines, and every name it reads (marimo cell signatures)."""

    def __init__(self):
        self.defs: set[str] = set()
        self.refs: set[str] = set()
        self.depth = 0

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.refs.add(node.id)
        elif self.depth == 0:
            self.defs.add(node.id)

    def _scope(self, node, name=None):
        if name and self.depth == 0:
            self.defs.add(name)
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_FunctionDef(self, node):
        for d in node.decorator_list:
            self.visit(d)
        self._scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._scope(node, node.name)

    def visit_Lambda(self, node):
        self._scope(node)

    def visit_ListComp(self, node):
        self._scope(node)

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_Import(self, node):
        if self.depth == 0:
            for a in node.names:
                self.defs.add(a.asname or a.name.split(".")[0])

    visit_ImportFrom = visit_Import


def _names(src: str) -> tuple[set[str], set[str]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set(), set()
    v = _Names()
    v.visit(tree)
    return {d for d in v.defs if not d.startswith("_")}, v.refs


def _py_str(s: str, indent: str) -> str:
    """A triple-quoted raw string literal holding s, indented for a cell body (marimo dedents it)."""
    if '"""' in s or s.endswith("\\") or "\r" in s:
        return repr(s)
    return 'r"""\n' + textwrap.indent(s, indent) + "\n" + indent + '"""'


def _write_marimo(nb: dict) -> str:
    plan = []
    for c in nb["cells"]:
        kind, src, meta = _info(c)
        if kind == "markdown":
            plan.append(("mo.md(" + _py_str(src, "    ") + ")", {"mo"}, set()))
        elif kind == "sql":
            sql = _sql_join(src, {"conn": meta.get("conn")})
            var = meta.get("var") or "_df"
            body = sql if '"""' not in sql else sql.replace('"""', '\\"\\"\\"')
            lit = 'f"""\n' + textwrap.indent(body, "    ") + '\n    """'
            refs = {"mo"} | set(re.findall(r"(?<!\{)\{([A-Za-z_]\w*)\}", sql))
            plan.append((f"{var} = mo.sql({lit})", refs, {var} if not var.startswith("_") else set()))
        elif kind == "raw":
            plan.append((_comment(src) or "pass", set(), set()))
        else:
            defs, refs = _names(src)
            plan.append((src, refs, defs))
    owners: dict[str, int] = {"mo": -1}
    for i, (_, _, defs) in enumerate(plan):
        for d in defs:
            owners.setdefault(d, i)
    out = ['import marimo', '', '__generated_with = "0.9.0"', 'app = marimo.App()', '', '',
           '@app.cell', 'def _():', '    import marimo as mo', '    return (mo,)', '']
    for i, (src, refs, defs) in enumerate(plan):
        params = sorted(r for r in refs if r in owners and owners[r] != i)
        rets = sorted(d for d in defs if owners.get(d) == i)
        ret = "return" if not rets else "return (" + ", ".join(rets) + ("," if len(rets) == 1 else "") + ")"
        out += ['', '@app.cell', f'def _({", ".join(params)}):', textwrap.indent(src, "    ") if src.strip() else "",
                f'    {ret}', '']
    out += ['', 'if __name__ == "__main__":', '    app.run()', '']
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------- quarto / rmarkdown

_CHUNK = re.compile(r"^(`{3,})\s*\{([^}]*)\}\s*$")


def _read_knitr(text: str, warnings: list[str]) -> dict:
    fm, lines = _front_matter(text.split("\n"))
    cells, md = [], []

    def flush():
        s = _trim("\n".join(md))
        if s:
            cells.append(_md(s))
        md.clear()

    i = 0
    while i < len(lines):
        m = _CHUNK.match(lines[i])
        if not m:
            md.append(lines[i])
            i += 1
            continue
        flush()
        fence, header = m.group(1), m.group(2).strip()
        j = i + 1
        while j < len(lines) and not (lines[j].strip().startswith(fence) and set(lines[j].strip()) == {"`"}):
            j += 1
        body = lines[i + 1:j]
        i = j + 1
        hm = re.match(r"^([=\w.\-]+)\s*,?\s*(.*)$", header)
        lang = hm.group(1).lower() if hm else ""
        if lang == "python":
            cells.append(_py(_trim("\n".join(body))))
        elif lang == "sql":
            opts = _opts(hm.group(2))
            k = 0
            while k < len(body) and body[k].startswith("#|"):
                key, _, val = body[k][2:].partition(":")
                opts[key.strip()] = _unquote(val)
                k += 1
            cells.append(_sql(_trim("\n".join(body[k:])), opts.get("connection") or opts.get("conn"),
                              opts.get("output.var") or opts.get("var")))
        else:
            _unsupported(warnings, len(cells) + 1, f"{{{header}}} chunk")
            cells.append(_raw(_trim("\n".join(body)), lang or "unknown", header=header))
    flush()
    return _notebook(cells, front_matter=fm)


def _write_knitr(nb: dict, rmd: bool) -> str:
    out = []
    fm = nb["metadata"].get("captain", {}).get("front_matter")
    if fm:
        out.append("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n") + "\n---")
    for c in nb["cells"]:
        kind, src, meta = _info(c)
        if kind == "markdown":
            out.append(src)
        elif kind == "sql":
            conn, var = meta.get("conn"), meta.get("var")
            if rmd:
                o = "".join(f", {k}={json.dumps(v)}" for k, v in (("connection", conn), ("output.var", var)) if v)
                out.append("```{sql" + o + "}\n" + src + "\n```")
            else:
                o = "".join(f"#| {k}: {_q(v)}\n" for k, v in (("connection", conn), ("output.var", var)) if v)
                out.append("```{sql}\n" + o + src + "\n```")
        elif kind == "raw":
            header = meta.get("header") or meta.get("lang") or "=raw"
            out.append("```{" + header + "}\n" + src + "\n```")
        else:
            out.append("```{python}\n" + src + "\n```")
    return "\n\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------- myst

_CC = re.compile(r"^(`{3,}|~{3,})\{(code-cell|raw-cell)\}[ \t]*(\S*)\s*$")
_PY_LANGS = {"", "python", "python3", "ipython", "ipython3"}


def _read_myst(text: str, warnings: list[str]) -> dict:
    fm, lines = _front_matter(text.split("\n"))
    kernelspec = (fm or {}).pop("kernelspec", None)
    if fm:
        fm.pop("jupytext", None)
    cells, md = [], []

    def flush():
        s = _trim("\n".join(md))
        if s:
            cells.append(_md(s))
        md.clear()

    i = 0
    while i < len(lines):
        if re.match(r"^\+\+\+", lines[i]):
            flush()
            i += 1
            continue
        m = _CC.match(lines[i])
        if not m:
            md.append(lines[i])
            i += 1
            continue
        flush()
        fence, directive, lang = m.group(1), m.group(2), m.group(3).lower()
        j = i + 1
        while j < len(lines) and lines[j].rstrip() != fence:
            j += 1
        body = lines[i + 1:j]
        i = j + 1
        opts: dict = {}
        if body and body[0].strip() == "---":
            end = next((k for k in range(1, len(body)) if body[k].strip() == "---"), None)
            if end:
                try:
                    opts = yaml.safe_load("\n".join(body[1:end])) or {}
                except yaml.YAMLError:
                    opts = {}
                body = body[end + 1:]
        else:
            k = 0
            while k < len(body) and (om := re.match(r"^:([\w.\-]+):\s*(.*)$", body[k])):
                opts[om.group(1)] = _unquote(om.group(2))
                k += 1
            body = body[k:]
        src = _trim("\n".join(body))
        if directive == "raw-cell":
            cells.append(_raw(src))
        elif lang in _PY_LANGS:
            cells.append(_py(src))
        elif lang == "sql":
            cells.append(_sql(src, opts.get("conn"), opts.get("var")))
        else:
            _unsupported(warnings, len(cells) + 1, f"{lang} code-cell")
            cells.append(_raw(src, lang))
    flush()
    return _notebook(cells, front_matter=fm, kernelspec=kernelspec)


def _write_myst(nb: dict) -> str:
    head = {"jupytext": {"text_representation": {"extension": ".md", "format_name": "myst",
                                                 "format_version": 0.13}},
            "kernelspec": _kernelspec(nb)}
    head.update(nb["metadata"].get("captain", {}).get("front_matter") or {})
    out = ["---\n" + yaml.safe_dump(head, sort_keys=False, allow_unicode=True).rstrip("\n") + "\n---"]
    prev = None
    for c in nb["cells"]:
        kind, src, meta = _info(c)
        if kind == "markdown":
            if prev == "markdown":
                out.append("+++")
            out.append(src)
        elif kind == "sql":
            o = "".join(f":{k}: {v}\n" for k, v in (("conn", meta.get("conn")), ("var", meta.get("var"))) if v)
            out.append("```{code-cell} sql\n" + o + src + "\n```")
        elif kind == "raw":
            lang = meta.get("lang")
            out.append(("```{code-cell} " + lang if lang else "```{raw-cell}") + "\n" + src + "\n```")
        else:
            out.append("```{code-cell} ipython3\n" + src + "\n```")
        prev = kind
    return "\n\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------- zeppelin

_ZEP_HEAD = re.compile(r"^%([\w.\-]+)(?:\(([^)]*)\))?[ \t]*(.*)$")
_PY_INTERP = {"python", "pyspark", "ipython", "spark.pyspark", "python.ipython", "spark.ipyspark"}


def _read_zeppelin(text: str, warnings: list[str]) -> dict:
    data = json.loads(text)
    cells = []
    for p in data.get("paragraphs") or []:
        t = (p.get("text") or "").replace("\r\n", "\n")
        if not t.strip():
            continue
        cfg = ((p.get("config") or {}).get("captain")) or {}
        first, _, rest = t.partition("\n")
        m = _ZEP_HEAD.match(first)
        if not m:
            warnings.append(f"cell {len(cells) + 1}: no interpreter line, read as Python")
            cells.append(_py(_trim(t)))
            continue
        interp, arg = m.group(1).lower(), m.group(2)
        body = _trim("\n".join(([m.group(3)] if m.group(3).strip() else []) + rest.split("\n")))
        if interp in ("md", "markdown"):
            cells.append(_md(body))
        elif interp in ("sql", "jdbc") or interp.endswith(".sql") or interp.startswith("jdbc"):
            cells.append(_sql(body, arg or cfg.get("conn"), cfg.get("var")))
        elif interp in _PY_INTERP:
            cells.append(_py(body))
        else:
            _unsupported(warnings, len(cells) + 1, f"%{interp}")
            cells.append(_raw(_trim(t), interp))
    nb = _notebook(cells)
    if data.get("name"):
        nb["metadata"]["captain"]["name"] = data["name"]
    return nb


def _write_zeppelin(nb: dict) -> str:
    paras = []
    for i, c in enumerate(nb["cells"]):
        kind, src, meta = _info(c)
        config: dict = {"editorHide": False}
        if kind == "markdown":
            text, mode = "%md\n" + src, "ace/mode/markdown"
            config["editorHide"] = True
        elif kind == "sql":
            conn = meta.get("conn")
            text, mode = (f"%jdbc({conn})\n" if conn else "%sql\n") + src, "ace/mode/sql"
            config["captain"] = {k: meta[k] for k in ("conn", "var") if meta.get(k)}
        elif kind == "raw":
            lang = meta.get("lang")
            text = src if src.startswith("%") or not lang else f"%{lang}\n{src}"
            mode = "ace/mode/text"
        else:
            text, mode = "%python\n" + src, "ace/mode/python"
        config["editorMode"] = mode
        paras.append({"id": f"paragraph_{i + 1}_{c.get('id') or uuid.uuid4().hex[:8]}", "text": text,
                      "config": config, "settings": {"params": {}, "forms": {}}, "status": "READY"})
    note = {"paragraphs": paras, "name": nb["metadata"].get("captain", {}).get("name") or "notebook",
            "id": uuid.uuid4().hex[:9].upper(), "defaultInterpreterGroup": "python", "version": "0.10.1",
            "noteParams": {}, "noteForms": {}, "angularObjects": {}, "config": {"isZeppelinNotebookCronEnable": False},
            "info": {}}
    return json.dumps(note, indent=2, ensure_ascii=False) + "\n"


_READERS = {"jupytext": _read_jupytext, "databricks": _read_databricks, "marimo": _read_marimo,
            "quarto": _read_knitr, "rmarkdown": _read_knitr, "myst": _read_myst, "zeppelin": _read_zeppelin}
_WRITERS = {"jupytext": _write_jupytext, "databricks": _write_databricks, "marimo": _write_marimo,
            "quarto": lambda nb: _write_knitr(nb, rmd=False), "rmarkdown": lambda nb: _write_knitr(nb, rmd=True),
            "myst": _write_myst, "zeppelin": _write_zeppelin}
