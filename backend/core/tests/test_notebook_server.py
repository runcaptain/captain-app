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
            # A cell's row limit (the app's picker) caps the result and says it was cut.
            ws.send_json({"type": "execute", "cell_id": "c", "kind": "sql", "conn": "demo-store", "var": "q", "limit": 3, "code": "select * from products"})
            evs = []
            while True:
                e = ws.receive_json()
                evs.append(e)
                if e["type"] == "done":
                    break
            t = next(e for e in evs if e["type"] == "table")
            assert t["row_count"] == 3 and t["limited"] is True and t["limit"] == 3, t
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


def test_sql_cell_code_carries_the_row_limit():
    cell = {"cell_type": "code", "source": "select 1", "metadata": {"captain": {"kind": "sql", "conn": "w", "var": "df_1", "limit": 1000}}}
    assert NB.cell_code(cell, 1).endswith("var='df_1', limit=1000)")
    cell["metadata"]["captain"].pop("limit")
    assert NB.cell_code(cell, 1).endswith("var='df_1')")


def test_a_missing_version_is_named_in_words(ctx):
    from captain_search.storage.backend import NotFound

    with pytest.raises(NotFound) as e:
        ctx.registry.load("backtest-run", "demos/nothing-here", 1)
    msg = str(e.value)
    assert msg == "no backtest run demos/nothing-here with version 1", msg
    assert "MANIFEST" not in msg and "/v1/" not in msg


def test_page_of_a_dataframe_sorts_and_filters_every_row():
    import pandas as pd

    from captain_search.connectors.guard import WriteRefused
    from captain_search.notebook.helpers import page_of

    df = pd.DataFrame({"id": range(2500), "price": [i % 97 for i in range(2500)], "tags": [["a", str(i)] for i in range(2500)]})
    p = page_of(df, 0, 100, count=True)
    assert p["total"] == 2500 and len(p["rows"]) == 100 and p["has_more"] and p["offset"] == 0
    assert [c["kind"] for c in p["columns"]] == ["integer", "integer", "list"]
    assert p["rows"][0][2] == '["a", "0"]'  # lists as JSON text, the same as every rows route
    last = page_of(df, 2400, 100)
    assert last["rows"][-1][0] == 2499 and not last["has_more"]
    top = page_of(df, 0, 5, order="price", desc=True, where="id >= 1000", count=True)
    assert top["rows"][0][1] == 96 and all(r[0] >= 1000 for r in top["rows"]) and top["total"] == 1500 and top["filtered"]
    with pytest.raises(ValueError, match="no column"):
        page_of(df, 0, 5, order="nope")
    with pytest.raises(WriteRefused):
        page_of(df, 0, 5, where="1=1; drop table t")


def test_kernel_pages_a_result_over_the_socket(ctx):
    from captain_search import server

    server._ctx = None
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    sid = c.post("/api/kernels", headers=h, json={"notebook": "x"}).json()["session"]

    def until_done(ws, cid):
        evs = []
        while True:
            e = ws.receive_json()
            evs.append(e)
            if e["type"] == "done" and e["cell_id"] == cid:
                return evs

    try:
        with c.websocket_connect(f"/api/kernels/{sid}?token={server.TOKEN}") as ws:
            ws.send_json({"type": "execute", "cell_id": "a", "kind": "python", "code": "import pandas as pd\nbig = pd.DataFrame({'n': range(3000)})"})
            until_done(ws, "a")
            ws.send_json({"type": "page", "req": "r1", "var": "big", "offset": 1000, "limit": 50, "order": "n", "desc": True, "count": True})
            evs = until_done(ws, "r1")
            p = next(e for e in evs if e["type"] == "page")
            assert p["cell_id"] == "r1" and p["total"] == 3000 and p["rows"][0] == [1999] and len(p["rows"]) == 50
            assert not any(e["type"] == "status" for e in evs), "a page doesn't show the notebook as running"
            # cst.dataset() reads a saved dataset in a Python cell.
            ws.send_json({"type": "execute", "cell_id": "d", "kind": "python", "code": "print(len(cst.dataset('demo-catalog')))"})
            evs = until_done(ws, "d")
            assert any(e["type"] == "stream" and e["text"].strip() == "480" for e in evs), evs
            ws.send_json({"type": "page", "req": "r2", "var": "gone"})
            evs = until_done(ws, "r2")
            err = next(e for e in evs if e["type"] == "error")
            assert "Run its cell again" in err["evalue"]
    finally:
        c.delete(f"/api/kernels/{sid}", headers=h)


def test_rename_takes_any_words_and_moves_the_draft(ctx):
    assert "demo-zero-results" in [n["name"] for n in NB.listing(ctx)]
    # A saved notebook with an unsaved draft on top.
    _, nb, _ = NB.load(ctx, "demo-zero-results")
    draft = json.loads(json.dumps(nb))
    draft["cells"].append({"cell_type": "markdown", "metadata": {}, "source": "not saved yet"})
    NB.save_draft(ctx, "demo-zero-results", draft)
    out = NB.rename(ctx, "demo-zero-results", "Zero results, by week")
    assert out == {"name": "zero-results-by-week", "from": "demo-zero-results", "version": 1, "changed": True}
    names = {n["name"]: n for n in NB.listing(ctx)}
    assert "demo-zero-results" not in names, "the old name leaves the list"
    assert names["zero-results-by-week"] == {"name": "zero-results-by-week", "latest": 1, "has_draft": True}
    v, got, from_draft = NB.load(ctx, "zero-results-by-week")
    assert v == 1 and from_draft and got["cells"][-1]["source"] == "not saved yet", "the unsaved work came with it"
    # The earlier versions are still in the registry under the old name: saved versions are never rewritten.
    assert ctx.registry.versions("notebook", "demo-zero-results")
    # The same name again changes nothing, and a name in use is refused in plain words.
    assert NB.rename(ctx, "zero-results-by-week", "zero results by week")["changed"] is False
    NB.save_revision(ctx, "other", NB.new_notebook(), "x")
    with pytest.raises(ValueError, match="You already have a notebook called other"):
        NB.rename(ctx, "zero-results-by-week", "Other")
    from captain_search.storage.backend import NotFound

    with pytest.raises(NotFound):
        NB.rename(ctx, "never-was", "x")
    # A notebook that is only a draft: the draft moves, and nothing is published.
    NB.save_draft(ctx, "untitled-notebook", NB.new_notebook())
    out = NB.rename(ctx, "untitled-notebook", "Weekly review")
    assert out["name"] == "weekly-review" and out["version"] is None
    names = {n["name"]: n for n in NB.listing(ctx)}
    assert "untitled-notebook" not in names and names["weekly-review"]["latest"] is None and names["weekly-review"]["has_draft"]
    # Someone starts a new notebook under a name that was renamed away from: it shows again.
    NB.save_draft(ctx, "demo-zero-results", NB.new_notebook())
    assert "demo-zero-results" in [n["name"] for n in NB.listing(ctx)]


def test_rename_route(ctx, monkeypatch):
    from captain_search import server

    server._ctx = None
    c = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    r = c.post("/api/notebooks/demo-zero-results/rename", headers=h, json={"to": "GPU cloud notes"})
    assert r.status_code == 200 and r.json()["name"] == "gpu-cloud-notes"
    assert c.post("/api/notebooks/gpu-cloud-notes/rename", headers=h, json={"to": "  "}).json()["error"] == "Give it a name."
    assert c.post("/api/notebooks/never-was/rename", headers=h, json={"to": "x"}).status_code == 404
    assert "gpu-cloud-notes" in [n["name"] for n in c.get("/api/notebooks", headers=h).json()]
