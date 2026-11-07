"""Extension points: how an organization extends Captain in its own code (plan 0).

An extension comes from one of three places:
- built in (shipped with captain-search);
- an installed package, through an entry point group `captain_search.<kind>s` (engines, simulators, judges, metrics);
- the team's project folder, `captain/`, whose Python files register extensions with the decorators below.

A project folder is a `captain/` directory holding `captain.toml`. It is found, in order, from `$CST_PROJECT`, the
folder bound to the workspace with `cst project use`, or by walking up from the current directory.

Every run records the folder's git commit, whether it had uncommitted changes, a hash of its files, and which
extensions the run used, so a result always names the exact code that produced it.

Extensions are trusted code, like dbt models or Herdr mods: loading a project folder runs its Python.

Interfaces (one each):
- metric:    fn(ranked: list[str], grades: dict[str, int]) -> float, plus higher_is_better and a definition
- judge:     fn(query: str, product: dict) -> int | (int, str)    grade 0 to 3, optional rationale
- simulator: fn(population, queries, search, relevance, products, seed=...) -> list[log dict], same as cascade-v1
- engine:    class with __init__(spec, products) and search(query, k) -> list[str]; explain(query, k) optional
- compute:   a compute_providers.ComputeProvider subclass: plan, submit, status, logs, cancel, test, describe
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

KINDS = ("engine", "simulator", "judge", "metric", "compute")
FOLDERS = {"engine": "stages", "simulator": "simulators", "judge": "judges", "metric": "metrics", "compute": "compute"}
GROUPS = {k: f"captain_search.{k}s" for k in KINDS} | {"compute": "captain_search.compute"}


@dataclass
class Ext:
    kind: str
    name: str
    obj: Any
    version: str = "0"
    source: str = "builtin"  # builtin, package:<dist>, project:<relative file>
    description: str = ""
    deterministic: bool = True
    meta: dict = field(default_factory=dict)  # metric: higher_is_better, definition

    def public(self) -> dict:
        return {"kind": self.kind, "name": self.name, "version": self.version, "source": self.source,
                "description": self.description, "deterministic": self.deterministic, **self.meta}


# ---- decorators a project file uses ---------------------------------------------------------

_collect: list[Ext] | None = None


def _register(kind: str, name: str, obj, **kw):
    if _collect is None:
        raise RuntimeError("Captain extensions register only while a project folder loads (cst ext check, or a run)")
    _collect.append(Ext(kind=kind, name=name, obj=obj, **kw))
    return obj


def metric(name: str, *, higher_is_better: bool = True, definition: str = "", version: str = "1",
           deterministic: bool = True):
    """@metric("revenue@10", definition="Sum of price over the top 10 with grade >= 2")"""
    return lambda fn: _register("metric", name, fn, version=version, description=definition,
                                deterministic=deterministic,
                                meta={"higher_is_better": higher_is_better, "definition": definition})


def judge(name: str, *, version: str = "1", description: str = "", deterministic: bool = True):
    return lambda fn: _register("judge", name, fn, version=version, description=description, deterministic=deterministic)


def simulator(name: str, *, version: str = "1", description: str = "", deterministic: bool = True):
    return lambda fn: _register("simulator", name, fn, version=version, description=description,
                                deterministic=deterministic)


def engine(name: str, *, version: str = "1", description: str = "", deterministic: bool = True):
    return lambda cls: _register("engine", name, cls, version=version, description=description,
                                 deterministic=deterministic)


def compute(name: str, *, version: str = "1", description: str = ""):
    """@compute("slurm") on a ComputeProvider subclass: a place your organization runs Captain's jobs."""
    def reg(cls):
        cls.kind = name
        return _register("compute", name, cls, version=version, description=description, deterministic=False)
    return reg


# ---- finding the project folder ---------------------------------------------------------------


def _bindings_file() -> Path:
    from .workspace import home

    return Path(home()) / "projects.json"


def bind(workspace: str, path: str | None) -> None:
    f = _bindings_file()
    data = json.loads(f.read_text()) if f.exists() else {}
    if path is None:
        data.pop(workspace, None)
    else:
        data[workspace] = str(Path(path).expanduser().resolve())
    f.write_text(json.dumps(data, indent=2))


def find_project(workspace: str | None = None, start: str | None = None) -> Path | None:
    """The captain/ folder (the directory holding captain.toml), or None."""
    cands = []
    if workspace is None:
        try:
            from .workspace import load_config

            workspace = os.environ.get("CST_WORKSPACE") or load_config().get("current")
        except Exception:  # noqa: BLE001
            workspace = None
    if os.environ.get("CST_PROJECT"):
        cands.append(Path(os.environ["CST_PROJECT"]).expanduser())
    if workspace:
        f = _bindings_file()
        try:
            bound = json.loads(f.read_text()).get(workspace) if f.exists() else None
        except (OSError, ValueError):
            bound = None
        if bound:
            cands.append(Path(bound))
    d = Path(start or os.getcwd()).resolve()
    for p in [d, *d.parents]:
        cands.append(p / "captain")
    for c in cands:
        for p in (c, c / "captain"):
            if (p / "captain.toml").is_file():
                return p.resolve()
    return None


def project_config(path: Path) -> dict:
    with open(path / "captain.toml", "rb") as f:
        return tomllib.load(f)


def fingerprint(path: Path) -> dict:
    """The code identity of a project folder: git commit, uncommitted changes, and a hash of its files."""
    h = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    for p in files:
        h.update(str(p.relative_to(path)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    out = {"path": str(path), "files": len(files), "sha256": h.hexdigest(), "commit": None, "dirty": None}
    try:
        g = lambda *a: subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True, timeout=5)
        r = g("rev-parse", "HEAD")
        if r.returncode == 0:
            out["commit"] = r.stdout.strip()
            out["dirty"] = bool(g("status", "--porcelain", "--", ".").stdout.strip())
            top = g("rev-parse", "--show-toplevel").stdout.strip()
            out["repo_path"] = str(path.relative_to(top)) if top else None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return out


# ---- loading ----------------------------------------------------------------------------------


def _builtins() -> list[Ext]:
    from .engines import BUILTIN, LocalBM25
    from .eval import metrics as M
    from .sim import SIMULATORS, run_cascade

    out = [Ext("engine", "local-bm25", LocalBM25, description="BM25 over the pipeline's catalog dataset")]
    out += [Ext("engine", n, c, description=(c.__doc__ or "").strip().split("\n")[0]) for n, c in BUILTIN.items()]
    out += [Ext("simulator", n, run_cascade, version=v, deterministic=True,
                description="Cascade click model with personas and devices") for n, v in SIMULATORS.items()]
    out += [Ext("judge", "heuristic", None, version="h1", description="Token overlap. Labeled SIMULATED"),
            Ext("judge", "llm", None, version="s1", deterministic=False, description="An LLM grades 0 to 3. Labeled LLM_GRADED")]
    out += [Ext("metric", n, None, description=M.DEFINITIONS.get(n, ""), meta={"higher_is_better": n != "zero",
                                                                             "definition": M.DEFINITIONS.get(n, "")})
            for n in M.NAMES]
    try:
        from .compute_providers import _builtin

        out += [Ext("compute", k, c, description=c.describe().get("label", ""), deterministic=False)
                for k, c in _builtin().items()]
    except Exception:  # noqa: BLE001  a missing cloud SDK shouldn't hide the other extensions
        pass
    return out


def _packages() -> tuple[list[Ext], list[dict]]:
    out, errors = [], []
    for kind in KINDS:
        for ep in entry_points(group=GROUPS[kind]):
            dist = getattr(getattr(ep, "dist", None), "name", None) or ep.value.split(":")[0]
            ver = getattr(getattr(ep, "dist", None), "version", None) or "0"
            try:
                obj = ep.load()
            except Exception as e:  # noqa: BLE001
                errors.append({"source": f"package:{dist}", "kind": kind, "name": ep.name, "error": f"{type(e).__name__}: {e}"})
                continue
            meta = {}
            if kind == "metric":
                meta = {"higher_is_better": getattr(obj, "higher_is_better", True),
                        "definition": getattr(obj, "definition", (obj.__doc__ or "").strip())}
            out.append(Ext(kind, ep.name, obj, version=str(ver), source=f"package:{dist}",
                           description=(obj.__doc__ or "").strip().split("\n")[0], meta=meta))
    return out, errors


def _project(path: Path) -> tuple[list[Ext], list[dict]]:
    global _collect
    out, errors = [], []
    cfg = project_config(path)
    disabled = set((cfg.get("extensions") or {}).get("disable") or [])
    for kind, folder in FOLDERS.items():
        d = path / folder
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            rel = str(f.relative_to(path))
            modname = f"captain_project_{hashlib.sha1(str(f).encode()).hexdigest()[:8]}_{f.stem}"
            _collect = []
            try:
                spec = importlib.util.spec_from_file_location(modname, f)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[modname] = mod
                spec.loader.exec_module(mod)
                got = _collect
            except Exception as e:  # noqa: BLE001
                errors.append({"source": f"project:{rel}", "kind": kind, "name": f.stem, "error": f"{type(e).__name__}: {e}"})
                continue
            finally:
                _collect = None
            if not got:
                errors.append({"source": f"project:{rel}", "kind": kind, "name": f.stem,
                               "error": f"registers nothing. Decorate a function with @{kind}(\"name\") from captain_search.extensions"})
            for e in got:
                e.source = f"project:{rel}"
                if e.name in disabled:
                    continue
                if e.kind != kind:
                    errors.append({"source": e.source, "kind": e.kind, "name": e.name,
                                   "error": f"a {e.kind} in the {folder}/ folder. Move it to {FOLDERS[e.kind]}/"})
                    continue
                out.append(e)
    return out, errors


def _validate(e: Ext) -> str | None:
    """A reason this extension can't be used, or None. Checks the interface, then runs metrics and judges on a tiny case."""
    o = e.obj
    try:
        if e.kind == "metric":
            n = len(inspect.signature(o).parameters)
            if n < 2:
                return "a metric takes (ranked, grades)"
            v = o(["a", "b", "c"], {"a": 3, "b": 0})
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
                return f"a metric returns a number. It returned {v!r} on a small test case"
        elif e.kind == "judge":
            if len(inspect.signature(o).parameters) < 2:
                return "a judge takes (query, product)"
            g = o("red shoes", {"id": "1", "title": "Red running shoes"})
            g = g[0] if isinstance(g, tuple) else g
            if not isinstance(g, int) or not 0 <= g <= 3:
                return f"a judge returns a grade from 0 to 3. It returned {g!r} on a small test case"
        elif e.kind == "simulator":
            if not callable(o) or len(inspect.signature(o).parameters) < 5:
                return "a simulator takes (population, queries, search, relevance, products, ...) like cascade-v1"
        elif e.kind == "engine":
            if not (inspect.isclass(o) and callable(getattr(o, "search", None))):
                return "an engine is a class with search(query, k)"
        elif e.kind == "compute":
            from .compute_providers import REQUIRED, ComputeProvider

            if not (inspect.isclass(o) and issubclass(o, ComputeProvider)):
                return "a compute provider is a ComputeProvider subclass (captain_search.compute_providers)"
            missing = [m for m in REQUIRED if not callable(getattr(o, m, None))]
            if missing:
                return f"a compute provider needs {', '.join(missing)}"
    except Exception as ex:  # noqa: BLE001
        return f"failed on a small test case: {type(ex).__name__}: {ex}"
    return None


@dataclass
class Registry:
    items: list[Ext]
    errors: list[dict]
    project: dict | None

    def get(self, kind: str, name: str) -> Ext | None:
        # The project folder wins over packages, which win over built-ins: a team can override a built-in on purpose.
        for src in ("project:", "package:", "builtin"):
            for e in self.items:
                if e.kind == kind and e.name == name and e.source.startswith(src):
                    return e
        return None

    def of(self, kind: str) -> list[Ext]:
        return [e for e in self.items if e.kind == kind]

    def custom(self, kind: str) -> list[Ext]:
        names = {e.name for e in self.of(kind)}
        return [self.get(kind, n) for n in sorted(names) if self.get(kind, n).source != "builtin"]

    def used(self, pairs: list[tuple[str, str]]) -> list[dict]:
        return [self.get(k, n).public() for k, n in pairs if self.get(k, n)]


_cache: dict[tuple, Registry] = {}


def load(workspace: str | None = None, start: str | None = None, fresh: bool = False) -> Registry:
    path = find_project(workspace, start)
    fp = fingerprint(path) if path else None
    key = (str(path), fp and fp["sha256"])
    if not fresh and key in _cache:
        return _cache[key]
    items, errors = _builtins(), []
    pk, pe = _packages()
    items += pk
    errors += pe
    if path:
        pr, prerr = _project(path)
        items += pr
        errors += prerr
    good = []
    for e in items:
        why = None if e.source == "builtin" else _validate(e)
        if why:
            errors.append({"source": e.source, "kind": e.kind, "name": e.name, "error": why})
        else:
            good.append(e)
    reg = Registry(good, errors, fp)
    _cache.clear()
    _cache[key] = reg
    return reg


def current(ctx=None) -> Registry:
    ws = getattr(getattr(ctx, "workspace", None), "name", None)
    return load(ws)


SCAFFOLD = {
    "captain.toml": '''# Captain project: this folder is your organization's search code, versioned with your repo.
# cst finds it from $CST_PROJECT, `cst project use <path>`, or by walking up from where you run cst.
[project]
name = "{name}"

[extensions]
disable = []   # names of extensions in this folder to skip
''',
    "metrics/example_metric.py": '''"""Your own metrics. Each one scores one query's ranking against its grades (0 to 3)."""
from captain_search.extensions import metric


@metric("relevant@5", definition="Share of the top 5 with grade 2 or more", higher_is_better=True)
def relevant_at_5(ranked, grades):
    top = ranked[:5]
    return sum(1 for p in top if grades.get(str(p), 0) >= 2) / 5
''',
    "judges/example_judge.py": '''"""Your own relevance judges. Grade 0 (wrong) to 3 (exactly what was asked). Optionally return a reason."""
from captain_search.extensions import judge


@judge("title-match", description="Every query word appears in the title")
def title_match(query, product):
    words = query.lower().split()
    title = str(product.get("title", "")).lower()
    hits = sum(1 for w in words if w in title)
    if not words:
        return 0, "empty query"
    return (3 if hits == len(words) else 2 if hits * 2 >= len(words) else 1 if hits else 0), f"{hits}/{len(words)} words in the title"
''',
    "simulators/README.md": "Shopper simulators go here. Copy cascade-v1's signature: "
                            "(population, queries, search, relevance, products, seed=...) -> logs.\n",
    "stages/README.md": "Search engines and ranking stages go here: a class with __init__(spec, products) and "
                        "search(query, k). Decorate it with @engine(\"name\").\n",
}


def scaffold(where: str, name: str) -> Path:
    root = Path(where).expanduser().resolve()
    path = root if root.name == "captain" else root / "captain"
    if (path / "captain.toml").exists():
        raise FileExistsError(f"{path} already has a captain.toml")
    for rel, text in SCAFFOLD.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text.replace("{name}", name))
    return path


def run_manifest(ctx=None, used: list[tuple[str, str]] | None = None) -> dict:
    """What a run records about the code that produced it."""
    import platform

    from importlib.metadata import version

    reg = current(ctx)
    try:
        cst_version = version("captain-search")
    except Exception:  # noqa: BLE001
        cst_version = "unknown"
    cst_commit = None
    try:
        r = subprocess.run(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=5)
        cst_commit = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {"cst": {"version": cst_version, "commit": cst_commit}, "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}", "project": reg.project,
            "extensions": reg.used(used or [])}


Callable  # re-exported for type hints in project files
