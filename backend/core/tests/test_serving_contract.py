"""What cloud serving targets run: /search, plus SageMaker's /ping and /invocations, and the runner entrypoint."""
import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from captain_search import demo
from captain_search.serving import make_app
from captain_search.workspace import init_workspace

ENTRY = Path(__file__).resolve().parents[2] / "runner" / "entrypoint.sh"


def test_sagemaker_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("CST_HOME", str(tmp_path / "h"))
    c = init_workspace("s", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    api = TestClient(make_app(c, "demo-bm25"))
    assert api.get("/ping").status_code == 200
    a = api.post("/invocations", json={"query": "tent", "k": 5}).json()
    b = api.post("/search", json={"query": "tent", "k": 5}).json()
    assert a["ids"] == b["ids"] and len(a["ids"]) == 5 and a["pipeline"] == "demo-bm25@v1"


def run_entry(tmp_path, args, env):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "cst").write_text('#!/bin/sh\necho "$@"\n')
    (stub / "cst").chmod(0o755)
    e = {"PATH": f"{stub}:{os.environ['PATH']}", **env}
    return subprocess.run(["sh", str(ENTRY), *args], env=e, capture_output=True, text=True).stdout.strip()


def test_entrypoint_turns_sagemaker_serve_into_pipeline_serve(tmp_path):
    out = run_entry(tmp_path, ["serve"], {"CST_PIPELINE": "p@v2", "CST_WORKSPACE": "ws"})
    assert out == "-w ws pipeline serve p@v2 --host 0.0.0.0 --port 8080"
    assert run_entry(tmp_path, ["serve"], {}) == "serve"  # the daemon, when no pipeline is named
    assert run_entry(tmp_path, ["-w", "ws", "backtest", "run", "b"], {"CST_PIPELINE": "p@v2"}) == "-w ws backtest run b"
