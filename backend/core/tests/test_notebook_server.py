import json

import pytest
from fastapi.testclient import TestClient

from captain_search import demo
from captain_search import notebook as NB
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    c = init_workspace("t", str(tmp_path / "bucket"))
    demo.seed(c, progress=lambda s: None)
    return c


def test_headless_notebook_run_sql_then_python(ctx):
    _, nb, _ = NB.load(ctx, "demo-zero-results")
    out, errors = NB.run_all(ctx, nb)
    assert errors == []
    code = [c for c in out["cells"] if c["cell_type"] == "code"]
    assert NB.TABLE_MIME in code[0]["outputs"][0]["data"]
    assert any("distinct zero-result queries" in o.get("text", "") for o in code[1]["outputs"])
    assert any(NB.VEGA_MIME in o.get("data", {}) for o in code[2]["outputs"])


def test_api(ctx):
    from captain_search import server

    server._ctx = None
    c = TestClient(server.app)
    assert c.get("/api/health").status_code == 401
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert c.get("/api/workspace", headers=h).json()["name"] == "t"
    assert {d["name"] for d in c.get("/api/datasets", headers=h).json()} >= {"demo-catalog", "demo-queries"}
    assert c.get("/api/datasets/demo-catalog/head?n=3", headers=h).json()["rows"].__len__() == 3
    r = c.post("/api/sql", headers=h, json={"conn": "demo-store", "sql": "drop table products"})
    assert r.status_code == 400 and "not allowed" in r.json()["error"]
    run = c.get("/api/runs/demo-tuned-vs-bm25", headers=h).json()
    assert run["arms"][0]["evidence"] == "SIMULATED" and "report" in run
    r = c.post("/api/deploy/promote", headers=h, json={"pipeline": "demo-tuned@v1", "env": "prod", "confirm": True})
    assert r.status_code == 409 and any(g["gate"] == "evidence" for g in r.json()["gate_results"])
    j = c.post("/api/backtests/demo-tuned-vs-bm25/run", headers=h).json()["job"]
    import time
    for _ in range(100):
        s = c.get(f"/api/jobs/{j}", headers=h).json()
        if s["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert s["status"] == "done", s
    assert c.get("/api/lineage", headers=h).json()["edges"]
    assert len(c.get("/api/skills", headers=h).json()) >= 6


def test_kernel_websocket(ctx):
    from captain_search import server

    server._ctx = None
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    sid = c.post("/api/kernels", headers=h, json={"notebook": "x"}).json()["session"]
    try:
        with c.websocket_connect(f"/api/kernels/{sid}?token={server.TOKEN}") as ws:
            ws.send_json({"type": "execute", "cell_id": "a", "kind": "sql", "conn": "demo-store", "var": "p", "code": "select * from products limit 5"})
            evs = []
            while True:
                e = ws.receive_json()
                evs.append(e)
                if e["type"] == "done":
                    break
            t = next(e for e in evs if e["type"] == "table")
            assert t["row_count"] == 5 and t["var"] == "p"
            ws.send_json({"type": "execute", "cell_id": "b", "kind": "python", "code": "print(len(p))"})
            evs = []
            while True:
                e = ws.receive_json()
                evs.append(e)
                if e["type"] == "done":
                    break
            assert any(e["type"] == "stream" and e["text"].strip() == "5" for e in evs)
    finally:
        c.delete(f"/api/kernels/{sid}", headers=h)


def test_import_plain_jupyter_notebook(ctx, tmp_path):
    nb = {"nbformat": 4, "nbformat_minor": 4, "metadata": {"kernelspec": {"name": "python3"}},
          "cells": [{"cell_type": "markdown", "metadata": {}, "source": ["# Hi"]},
                    {"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": 1, "source": ["x = 2\n", "print(x * 21)"]}]}
    f = tmp_path / "My Analysis.ipynb"
    f.write_text(json.dumps(nb))
    m = NB.import_file(ctx, str(f))
    assert m.name == "my-analysis" and m.version == 1
    _, loaded, _ = NB.load(ctx, "my-analysis")
    out, errors = NB.run_all(ctx, loaded)
    assert errors == [] and any("42" in o.get("text", "") for o in out["cells"][1]["outputs"])
    bad = tmp_path / "bad.ipynb"
    bad.write_text(json.dumps({"nbformat": 3, "worksheets": []}))
    with pytest.raises(ValueError, match="nbformat 4"):
        NB.import_file(ctx, str(bad))
