"""The list of projects reads the workspace's model registries on every call. A registry's kept copy is written once
under its version number, so it is read and parsed once and stands until a newer version is listed. Made-up
registries only."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from captain_search import attach as AT
from captain_search import models as MD
from captain_search import projects as PR
from captain_search.workspace import init_workspace


def make_registry(root: Path, brands=("shop", "garden")) -> Path:
    for brand in brands:
        d = root / "arch-a" / brand / "v1"
        (d / "weights").mkdir(parents=True)
        (d / "weights" / "model.bin").write_text("x")
        (d / "MANIFEST.yaml").write_text("status: live\nwhat_changed: made up\n")
    (root / "INDEX.md").write_text("# a made-up registry\n")
    return root


@pytest.fixture
def ws():
    # Short folders: a registry's copy is named after its address, cut at 100 letters.
    base = Path(tempfile.mkdtemp(prefix="cst-kept-", dir="/tmp"))
    MD._SNAPS.clear()
    try:
        yield init_workspace("t", str(base / "store")), base
    finally:
        MD._SNAPS.clear()
        shutil.rmtree(base, ignore_errors=True)


def count_loads(ctx, monkeypatch) -> list:
    seen = []
    real = ctx.registry.load

    def load(kind, name, version=None):
        seen.append((kind, name, version))
        return real(kind, name, version)

    monkeypatch.setattr(ctx.registry, "load", load)
    return seen


def test_a_registrys_copy_is_parsed_once_however_often_the_projects_are_listed(ws, monkeypatch):
    ctx, base = ws
    AT.attach(ctx, str(make_registry(base / "reg")))
    loads = count_loads(ctx, monkeypatch)
    first = MD.listing(ctx)
    assert sorted(m["id"] for m in first) == ["garden@v1", "shop@v1"]
    assert len(loads) == 1, "one copy, read once"
    for _ in range(5):
        assert MD.listing(ctx) == first
        PR.listing(ctx)
        MD.latest(ctx)
        MD.latest_for(ctx, str(base / "reg"))
    assert len(loads) == 1, f"never parsed again while it has not changed: {loads}"


def test_a_registry_read_again_shows_at_once(ws, monkeypatch):
    ctx, base = ws
    reg = make_registry(base / "reg", brands=("shop",))
    AT.attach(ctx, str(reg))
    assert [m["id"] for m in MD.listing(ctx)] == ["shop@v1"]
    # A new model lands in the registry and Captain reads it again: a new version of its copy.
    make_registry(reg, brands=("trail",))
    assert [m["id"] for m in MD.listing(ctx)] == ["shop@v1"], "the registry itself is not read by a listing"
    MD.import_registry(ctx, str(reg))
    loads = count_loads(ctx, monkeypatch)
    assert sorted(m["id"] for m in MD.listing(ctx)) == ["shop@v1", "trail@v1"], "the newer copy is used at once, with no wait"
    assert len(loads) == 1
    MD.listing(ctx)
    assert len(loads) == 1


def test_two_workspaces_never_share_a_copy(ws):
    ctx, base = ws
    AT.attach(ctx, str(make_registry(base / "reg", brands=("shop",))))
    assert [m["id"] for m in MD.listing(ctx)] == ["shop@v1"]
    other = init_workspace("t", str(base / "other-store"))
    assert MD.listing(other) == [], "the same workspace name over other storage reads its own"
    AT.attach(other, str(make_registry(base / "reg2", brands=("garden",))))
    assert [m["id"] for m in MD.listing(other)] == ["garden@v1"] and [m["id"] for m in MD.listing(ctx)] == ["shop@v1"]
