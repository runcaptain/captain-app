"""Projects: one per company, holding its connections, data, pipelines, models, backtests and lineage."""
from fastapi.testclient import TestClient

from captain_search import demo
from captain_search import library as LIB
from captain_search import models as MD
from captain_search import projects as PR
from captain_search.data import importer as IM
from captain_search.workspace import init_workspace


def _registry(monkeypatch):
    def fake_scan(uri, profile=None, s3=None):
        mk = lambda brand, v, parents: {
            "id": f"{brand}@{v}", "brand": brand, "architecture": "two-tower", "version": v, "status": "current",
            "status_kind": "current", "repro_status": "not-tested", "parents": parents, "attached_to_demo": False,
            "files": [], "bytes": 0, "manifest": {}, "path": f"{uri}/two-tower/{brand}/{v}/"}
        return {"index_md": "", "architectures": ["two-tower"], "architecture_docs": {},
                "models": [mk("acme", "v1", []), mk("acme", "v2", ["v1"]), mk("globex", "v1", [])]}

    monkeypatch.setattr(MD, "scan", fake_scan)


def test_projects_group_a_companys_work(tmp_path, monkeypatch):
    from captain_search import attach as AT

    _registry(monkeypatch)
    c = init_workspace("t", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    AT.attach(c, "s3://models/registry")
    acme = PR.create(c, "Acme")
    PR.create(c, "Globex Corp", brands=["globex"])
    # a dropped file goes straight into the project
    f = tmp_path / "acme-catalog.csv"
    f.write_text("id,title\n1,Shoe\n")
    IM.import_paths(c, [str(f)], "catalogs", project="acme")
    # a dataset in a folder named after the project joins it too
    (tmp_path / "q.csv").write_text("query_id,query\nq1,shoes\n")
    IM.import_paths(c, [str(tmp_path / "q.csv")], "acme/eval")
    PR.assign(c, "acme", [{"kind": "pipeline", "name": "demo-bm25"}, {"kind": "backtest", "name": "demo-tuned-vs-bm25"}])
    got = PR.contents(c, "acme")
    assert got["project"]["name"] == "Acme" and acme["id"] == "acme"
    assert set(got["dataset"]) == {"catalogs/acme-catalog", "acme/eval/q"}
    assert [m["id"] for m in got["models"]] == ["acme@v1", "acme@v2"]
    assert got["pipeline"] == ["demo-bm25"] and got["backtest"] == ["demo-tuned-vs-bm25"]
    counts = {p["id"]: p["counts"] for p in PR.listing(c)}
    assert counts["globex-corp"]["models"] == 1 and counts["acme"]["models"] == 2
    lin = PR.lineage(c, "acme")
    ids = {n["id"] for n in lin["nodes"]}
    assert {"model:acme@v1", "model:acme@v2", "pipeline:demo-bm25", "backtest:demo-tuned-vs-bm25"} <= ids
    assert "model:globex@v1" not in ids
    assert {"src": "model:acme@v1", "dst": "model:acme@v2", "relation": "parent"} in lin["edges"]
    # taking an item out beats the name rules
    PR.assign(c, None, [{"kind": "dataset", "name": "acme/eval/q"}])
    assert "acme/eval/q" not in PR.contents(c, "acme")["dataset"]


def test_project_routes_filter_lists(tmp_path, monkeypatch):
    from captain_search import server

    c = init_workspace("t", str(tmp_path / "b"))
    demo.seed(c, progress=lambda s: None)
    monkeypatch.setattr(server, "_ctx", None)
    cl = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    assert cl.post("/api/projects", headers=h, json={"name": "Acme"}).json()["id"] == "acme"
    assert cl.post("/api/projects", headers=h, json={"name": "Acme"}).status_code == 400
    cl.post("/api/projects/assign", headers=h, json={"project": "acme", "items": [{"kind": "dataset", "name": "demo-catalog"}]})
    names = [d["name"] for d in cl.get("/api/datasets?project=acme", headers=h).json()]
    assert names == ["demo-catalog"]
    rest = [d["name"] for d in cl.get("/api/datasets?project=none", headers=h).json()]
    assert "demo-catalog" not in rest and rest
    tree = cl.get("/api/datasets/tree?project=acme", headers=h).json()
    assert [d["name"] for d in tree["datasets"]] == ["demo-catalog"]
    assert cl.get("/api/projects", headers=h).json()[0]["counts"]["dataset"] == 1
    assert cl.get("/api/backtests?project=acme", headers=h).json() == []
