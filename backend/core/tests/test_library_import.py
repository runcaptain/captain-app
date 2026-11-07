"""Onboarding without demo data: check storage, drop local files in, organize them in folders, attach a registry."""
import json

import pytest

from captain_search import attach as AT
from captain_search import library as LIB
from captain_search.data import importer as IM
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    return init_workspace("mine", str(tmp_path / "bucket"))


def _drop(tmp_path):
    d = tmp_path / "work"
    (d / "catalogs").mkdir(parents=True)
    (d / "catalogs" / "Spring Catalog.csv").write_text("id,title,price\n1,Red shoes,40\n2,Blue hat,12\n")
    (d / "queries.jsonl").write_text('{"query_id":"q1","query":"red shoes"}\n')
    (d / "grades.csv").write_text("query_id,product_id,grade\nq1,1,3\n")
    (d / "notes.txt").write_text("not a table")
    (d / ".DS_Store").write_text("x")
    return d


def test_drop_a_folder_keeps_structure_and_guesses_kinds(ctx, tmp_path):
    d = _drop(tmp_path)
    r = IM.import_paths(ctx, [str(d)], "my stuff")
    got = {x["ref"]: (x["kind"], x["folder"]) for x in r["imported"]}
    assert got == {
        "my-stuff/work/catalogs/spring-catalog@v1": ("catalog", "my stuff/work/catalogs"),
        "my-stuff/work/queries@v1": ("queries", "my stuff/work"),
        "my-stuff/work/grades@v1": ("judgments", "my stuff/work"),
    }
    assert [s["path"].endswith("notes.txt") for s in r["skipped"]] == [True]
    assert "isn't a table" in r["skipped"][0]["reason"]
    tree = LIB.tree(ctx, lambda n: {"name": n, "leaf": n.split("/")[-1]})
    top = tree["folders"][0]
    assert top["name"] == "my stuff" and top["folders"][0]["name"] == "work"


def test_same_file_again_is_skipped_and_a_changed_one_is_a_new_version(ctx, tmp_path):
    d = _drop(tmp_path)
    f = d / "catalogs" / "Spring Catalog.csv"
    IM.import_paths(ctx, [str(f)])
    again = IM.import_paths(ctx, [str(f)])
    assert again["unchanged"] == ["spring-catalog@v1"] and not again["imported"]
    f.write_text("id,title,price\n1,Red shoes,35\n2,Blue hat,12\n3,Green bag,20\n")
    r = IM.import_paths(ctx, [str(f)])
    assert r["updated"][0]["ref"] == "spring-catalog@v2" and r["updated"][0]["rows"] == 3


def test_folders_move_and_rename_without_touching_data(ctx, tmp_path):
    d = _drop(tmp_path)
    IM.import_paths(ctx, [str(d / "grades.csv"), str(d / "queries.jsonl")])
    LIB.make_folder(ctx, "eval/holdout")
    LIB.move(ctx, ["grades"], "eval/holdout")
    LIB.rename_folder(ctx, "eval", "evaluation")
    doc, _ = LIB._read(ctx)
    assert LIB.folder_of(doc, "grades") == "evaluation/holdout" and LIB.folder_of(doc, "queries") == ""
    assert ctx.registry.load("dataset", "grades").version == 1
    with pytest.raises(ValueError, match="isn't empty"):
        LIB.remove_folder(ctx, "evaluation")
    LIB.make_folder(ctx, "empty")
    assert LIB.remove_folder(ctx, "empty") == {"removed": "empty"}


def test_storage_check_says_what_works(tmp_path):
    ok = AT.check_storage(str(tmp_path / "fresh"))
    assert ok["ok"] and ok["can_read"] and ok["can_write"] and ok["existing_workspace"] is None
    init_workspace("there", str(tmp_path / "used"))
    assert AT.check_storage(str(tmp_path / "used"))["existing_workspace"] == "there"


def test_attach_registry_any_layout(ctx, tmp_path, monkeypatch):
    from captain_search import models as MD

    def fake_scan(uri, profile=None, s3=None):
        return {"index_md": "", "architectures": ["two-tower"], "architecture_docs": {}, "models": [
            {"id": "acme@v1", "brand": "acme", "architecture": "two-tower", "version": "v1", "status": "current",
             "status_kind": "current", "repro_status": "not-tested", "parents": [], "attached_to_demo": False,
             "files": [], "bytes": 0, "manifest": {}, "path": f"{uri}/two-tower/acme/v1/"}]}

    monkeypatch.setattr(MD, "scan", fake_scan)
    r = AT.attach(ctx, "s3://team-models/registry")
    assert r["models"] == 1
    assert [x["uri"] for x in AT.registries(ctx)] == ["s3://team-models/registry"]
    assert MD.listing(ctx)[0]["registry"] == "s3://team-models/registry"
    assert MD.lineage(ctx)["registries"] == ["s3://team-models/registry"]
    AT.detach(ctx, "s3://team-models/registry")
    assert AT.registries(ctx) == []


def test_registry_address_and_login_come_from_connected_clouds(ctx, monkeypatch):
    from captain_search.workspace import load_config, save_config

    cfg = load_config()
    cfg["profiles"] = {"team-aws": {"provider": "aws"}, "team-gcp": {"provider": "gcp", "last_test": {"ok": True}},
                       "old-gcp": {"provider": "gcp"}}
    save_config(cfg)
    assert AT.resolve(ctx, "s3://models/registry/") == ("s3://models/registry", "team-aws")
    assert AT.resolve(ctx, "gs://models") == ("gs://models", "team-gcp")
    assert AT.resolve(ctx, "s3://x", "mine") == ("s3://x", "mine")
    # no scheme: two clouds connected and a local workspace, so AWS
    assert AT.resolve(ctx, "models/registry")[0] == "s3://models/registry"
    cfg["profiles"] = {"only-gcp": {"provider": "gcp"}}
    save_config(cfg)
    assert AT.resolve(ctx, "models/registry") == ("gs://models/registry", "only-gcp")


def test_attach_a_registry_in_a_folder(ctx, tmp_path):
    from captain_search import models as MD

    reg = tmp_path / "registry"
    for brand, v, parent in (("acme", "v1", None), ("acme", "v2", "v1")):
        d = reg / "two-tower" / brand / v
        d.mkdir(parents=True)
        (d / "MANIFEST.yaml").write_text(f"status: current\nparent: {parent or ''}\n")
        (d / "weights.bin").write_text("x")
    r = AT.attach(ctx, str(reg))
    assert r["models"] == 2 and r["profile"] is None
    assert {m["id"] for m in MD.listing(ctx)} == {"acme@v1", "acme@v2"}
