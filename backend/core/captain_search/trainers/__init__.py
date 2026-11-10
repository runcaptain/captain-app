"""Built-in trainers, and the private environment they run in.

Captain's app bundle and base install carry no training library. The starter trainer needs LightGBM, so the
first time it runs Captain makes a small environment of its own under its home folder (`<CST_HOME>/envs/train-ltr`),
installs the pinned packages there, and from then on runs the trainer in it as a subprocess, through the same
trainer contract a team's own trainer uses. Nothing is added to the Python that runs Captain.

Where the trainer runs, in order:
1. this Python, when LightGBM is already importable here (the runner image installs the `train-ltr` extra, and so
   does a developer who ran `pip install 'captain-search[train-ltr]'`);
2. the private environment, made on first use with `uv` when it is on PATH, else `python -m venv` and pip.

macOS: LightGBM's wheel doesn't bundle the OpenMP library (libomp) it links to. When it can't be loaded, the
environment also gets scikit-learn, whose wheel does bundle one, and the trainer is started with the loader's
fallback path pointing at it. If that still fails, the person is told the one command that fixes it.
"""
from __future__ import annotations

import glob
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

# The same pin as the `train-ltr` extra in pyproject.toml (a test keeps the two equal).
LIGHTGBM = "lightgbm==4.7.0"
REQUIREMENTS = [LIGHTGBM, "numpy>=1.26", "pyarrow>=16"]
OPENMP_DONOR = "scikit-learn>=1.6"  # its macOS wheel bundles libomp.dylib
SETUP_STAGE = "Setting up the starter trainer, one time"
TRAINERS = {"lightgbm-ltr": {"script": "lightgbm_ltr.py", "env": "train-ltr",
                             "label": "Starter ranker (LightGBM lambdarank)"}}
COMMAND = '"$CST_PYTHON" -m captain_search.trainers {name}'


class SetupFailed(RuntimeError):
    """The environment couldn't be made. The message is the sentence shown to the person."""


def command(name: str = "lightgbm-ltr") -> str:
    """The training command a model version declares for a built-in trainer (spec.train.command)."""
    if name not in TRAINERS:
        raise ValueError(f"no built-in trainer {name!r}. There is: {', '.join(TRAINERS)}")
    return COMMAND.format(name=name)


def env_dir(name: str = "train-ltr") -> str:
    from ..workspace import home

    return os.path.join(home(), "envs", name)


def _python(d: str) -> str:
    return os.path.join(d, "Scripts", "python.exe") if os.name == "nt" else os.path.join(d, "bin", "python")


def _stamp() -> str:
    return hashlib.sha256(json.dumps([REQUIREMENTS, sys.version_info[:2]]).encode()).hexdigest()[:16]


def _run(argv: list[str], log, env: dict | None = None) -> tuple[int, str]:
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    tail: list[str] = []
    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip()
        if line:
            tail.append(line)
            del tail[:-30]
            log(line)
    return p.wait(), "\n".join(tail)


def _openmp_env(py: str) -> dict:
    """Extra environment that lets the loader find a bundled libomp (macOS only), or nothing."""
    if sys.platform != "darwin":
        return {}
    root = os.path.dirname(os.path.dirname(py))
    hits = glob.glob(os.path.join(root, "lib", "python*", "site-packages", "sklearn", ".dylibs"))
    if not hits:
        return {}
    have = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH")
    # Setting the variable replaces the loader's own default list, so the defaults are named again after ours.
    return {"DYLD_FALLBACK_LIBRARY_PATH": os.pathsep.join([hits[0]] + ([have] if have else ["/usr/local/lib", "/usr/lib"]))}


def _imports(py: str, extra: dict) -> tuple[bool, str]:
    r = subprocess.run([py, "-c", "import lightgbm, numpy, pyarrow"], capture_output=True, text=True,
                       env={**os.environ, **extra})
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def _install(py: str, d: str, packages: list[str], log) -> None:
    uv = shutil.which("uv")
    argv = [uv, "pip", "install", "--python", py, *packages] if uv else [py, "-m", "pip", "install", "--disable-pip-version-check", *packages]
    code, tail = _run(argv, log)
    if code != 0:
        last = tail.splitlines()[-1] if tail else "no output"
        net = any(w in tail.lower() for w in ("connection", "network", "resolve", "timed out", "dns", "offline", "ssl",
                                              "failed to fetch", "temporary failure"))
        shutil.rmtree(d, ignore_errors=True)  # a half-made environment would look ready next time
        raise SetupFailed(("Captain couldn't download the starter trainer. It needs the internet once, to fetch LightGBM "
                           "(about 150 MB). Check the connection and train again." if net else
                           "Captain couldn't install the starter trainer's packages.") + f" The installer said: {last}")


def ensure(log=print, stage=None) -> tuple[str, dict]:
    """(python, extra environment) for running the starter trainer. Makes the private environment on first use,
    reporting each step through `log` and announcing the one-time setup through `stage`."""
    if importlib.util.find_spec("lightgbm") is not None:
        return sys.executable, {}
    d = env_dir()
    py = _python(d)
    marker = os.path.join(d, ".captain-ready")
    try:
        with open(marker) as f:
            if f.read().strip() == _stamp() and os.path.exists(py):
                return py, _openmp_env(py)
    except FileNotFoundError:
        pass
    (stage or log)(SETUP_STAGE)
    log(f"Making a private environment for the trainer in {d}. Captain's own Python is left as it is.")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    uv = shutil.which("uv")
    code, tail = _run([uv, "venv", "--python", sys.executable, d] if uv else [sys.executable, "-m", "venv", d], log)
    if code != 0 or not os.path.exists(py):
        shutil.rmtree(d, ignore_errors=True)
        raise SetupFailed("Captain couldn't make an environment for the starter trainer. "
                          f"The tool said: {(tail.splitlines() or ['no output'])[-1]}")
    log("Installing " + ", ".join(REQUIREMENTS))
    _install(py, d, REQUIREMENTS, log)
    ok, err = _imports(py, {})
    extra: dict = {}
    if not ok and sys.platform == "darwin" and "libomp" in err:
        log("This Mac has no OpenMP library, which LightGBM needs. Adding one that ships inside scikit-learn.")
        _install(py, d, [OPENMP_DONOR], log)
        extra = _openmp_env(py)
        ok, err = _imports(py, extra)
        if not ok and "libomp" in err:
            shutil.rmtree(d, ignore_errors=True)
            raise SetupFailed("LightGBM needs the OpenMP library, and this Mac doesn't have it. Install it with "
                              "`brew install libomp`, then train again.")
    if not ok:
        shutil.rmtree(d, ignore_errors=True)
        raise SetupFailed(f"The starter trainer's packages installed but don't load: {(err.splitlines() or ['no reason given'])[-1]}")
    with open(marker, "w") as f:
        f.write(_stamp())
    log("The starter trainer is ready. This setup won't run again.")
    return py, extra


def status() -> dict:
    """Whether the starter trainer can run now without a download."""
    here = importlib.util.find_spec("lightgbm") is not None
    d = env_dir()
    ready = False
    try:
        with open(os.path.join(d, ".captain-ready")) as f:
            ready = f.read().strip() == _stamp() and os.path.exists(_python(d))
    except FileNotFoundError:
        pass
    return {"ready": here or ready, "where": "this Python" if here else d, "needs_setup": not (here or ready),
            "packages": REQUIREMENTS}


def run(name: str) -> int:
    """Run a built-in trainer under the trainer contract: stdout carries its log and its JSON event lines."""
    if name not in TRAINERS:
        print(f"No built-in trainer {name!r}. There is: {', '.join(TRAINERS)}", flush=True)
        return 2

    def log(text: str) -> None:
        print(text, flush=True)

    try:
        py, extra = ensure(log, stage=lambda s: print(json.dumps({"type": "stage", "name": s}), flush=True))
    except SetupFailed as e:
        print(str(e), flush=True)
        return 3
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), TRAINERS[name]["script"])
    return subprocess.call([py, script], env={**os.environ, **extra, "PYTHONUNBUFFERED": "1"})
