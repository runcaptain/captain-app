"""A project's icon: a glyph in a palette color, a brand mark, or an organization's logo kept in the workspace store
and cached small for the app to draw."""
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from typer.testing import CliRunner

from captain_search import projects as PR
from captain_search import scopes as SC
from captain_search.storage.backend import NotFound
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    c = init_workspace("t", str(tmp_path / "b"))
    PR.create(c, "Outdoor")
    return c


def _icon(c, pid="outdoor"):
    return next(p for p in PR.listing(c) if p["id"] == pid).get("icon")


def test_glyph_and_brand_icons_are_checked_and_cleared(ctx):
    PR.set_icon(ctx, "outdoor", {"kind": "glyph", "name": "package", "color": "#059669"})
    assert _icon(ctx) == {"kind": "glyph", "name": "package", "color": "#059669"}
    with pytest.raises(ValueError, match="palette"):
        PR.set_icon(ctx, "outdoor", {"kind": "glyph", "name": "package", "color": "#123456"})
    with pytest.raises(ValueError, match="isn't an icon name"):
        PR.set_icon(ctx, "outdoor", {"kind": "glyph", "name": "../etc"})
    PR.set_icon(ctx, "outdoor", {"kind": "brand", "slug": "snowflake"})
    assert _icon(ctx) == {"kind": "brand", "slug": "snowflake"}
    with pytest.raises(ValueError, match="upload the logo first"):
        PR.set_icon(ctx, "outdoor", {"kind": "logo", "sha": "nope", "ext": "png"})
    PR.set_icon(ctx, "outdoor", None)
    assert _icon(ctx) is None
    # a new project can start with one, and the Explorer carries it
    PR.create(ctx, "Gifts", icon={"kind": "glyph", "name": "sparkles", "color": "#DB2777"})
    node = next(n for g in SC.explorer(ctx)["tree"] if g.get("type") == "project" for n in g["children"] if n["id"] == "gifts")
    assert node["icon"] == {"kind": "glyph", "name": "sparkles", "color": "#DB2777"}


def test_a_logo_goes_into_the_workspace_and_is_cached_small(ctx, tmp_path):
    big = tmp_path / "logo.png"
    Image.new("RGBA", (600, 300), (10, 120, 200, 255)).save(big)
    p = PR.upload_logo(ctx, "outdoor", str(big))
    icon = p["icon"]
    assert icon["kind"] == "logo" and icon["ext"] == "png"
    assert ctx.store.exists(PR.logo_key(icon["sha"], "png"))  # in the store, so teammates see it
    cache = tmp_path / "home" / "cache" / "project-logos"
    f = PR.logo_file(ctx, "outdoor", str(cache))["file"]
    im = Image.open(cache / f)
    assert im.size == (PR.LOGO_PX, PR.LOGO_PX)  # small and square, the wide logo centered
    assert PR.logo_file(ctx, "outdoor", str(cache))["file"] == f  # cached once
    with pytest.raises(ValueError, match="cache/project-logos"):
        PR.logo_file(ctx, "outdoor", str(tmp_path / "anywhere"))
    # an SVG stays vector, sized to draw at the cached resolution
    svg = tmp_path / "logo.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="2500" height="2500" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>')
    PR.upload_logo(ctx, "outdoor", str(svg))
    out = (cache / PR.logo_file(ctx, "outdoor", str(cache))["file"]).read_text()
    assert f'width="{PR.LOGO_PX // 2}"' in out and 'width="2500"' not in out and "viewBox" in out
    # refusals in words
    (tmp_path / "logo.gif").write_bytes(b"GIF89a")
    with pytest.raises(ValueError, match="PNG, JPEG or SVG"):
        PR.upload_logo(ctx, "outdoor", str(tmp_path / "logo.gif"))
    (tmp_path / "fake.png").write_text("not an image")
    with pytest.raises(ValueError, match="isn't an image"):
        PR.upload_logo(ctx, "outdoor", str(tmp_path / "fake.png"))
    with pytest.raises(NotFound):
        PR.upload_logo(ctx, "outdoor", str(tmp_path / "missing.png"))
    PR.set_icon(ctx, "outdoor", {"kind": "glyph", "name": "package", "color": "#0043CA"})
    assert PR.logo_file(ctx, "outdoor", str(cache)) == {"file": None}


def test_icon_routes_and_cli(ctx, tmp_path, monkeypatch):
    from captain_search import server
    from captain_search.cli.main import app

    monkeypatch.setattr(server, "_ctx", None)
    cl = TestClient(server.app)
    h = {"Authorization": f"Bearer {server.TOKEN}"}
    r = cl.put("/api/projects/outdoor/icon", headers=h, json={"icon": {"kind": "glyph", "name": "trophy", "color": "#D97706"}})
    assert r.status_code == 200 and r.json()["icon"]["name"] == "trophy"
    assert cl.put("/api/projects/outdoor/icon", headers=h, json={"icon": {"kind": "glyph", "name": "trophy", "color": "red"}}).status_code == 400
    png = tmp_path / "l.png"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(png)
    assert cl.post("/api/projects/outdoor/logo", headers=h, json={"path": str(png)}).json()["icon"]["kind"] == "logo"
    dest = str(tmp_path / "app" / "cache" / "project-logos")
    assert cl.post("/api/projects/outdoor/logo/file", headers=h, json={"dest": dest}).json()["file"].endswith(".png")
    assert cl.post("/api/projects", headers=h, json={"name": "Ayala", "icon": {"kind": "brand", "slug": "databricks"}}).json()["icon"]["slug"] == "databricks"
    run = CliRunner().invoke(app, ["projects", "set-icon", "outdoor", "--glyph", "package", "--color", "#059669"])
    assert run.exit_code == 0, run.output
    assert _icon(ctx) == {"kind": "glyph", "name": "package", "color": "#059669"}
    assert CliRunner().invoke(app, ["projects", "set-icon", "outdoor"]).exit_code != 0  # one choice needed
    assert CliRunner().invoke(app, ["projects", "set-icon", "outdoor", "--clear"]).exit_code == 0
    assert _icon(ctx) is None
