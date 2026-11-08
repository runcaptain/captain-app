"""Images in data: finding image columns, where relative names live, and thumbnails for the app."""
import os

import pytest
from PIL import Image

from captain_search import media as MD


def test_detect_finds_image_columns_and_relative_names():
    cols = ["pid", "title", "imgs", "hero", "link"]
    rows = [["1", "a", ["main.jpg"], "https://cdn.x/p/1.png", "https://shop.x/p/1"],
            ["2", "b", '["main.jpg", "side.webp"]', "https://cdn.x/p/2.png", "https://shop.x/p/2"]]
    got = {d["column"]: d for d in MD.detect(cols, rows)}
    assert set(got) == {"imgs", "hero"}  # a product page link isn't an image
    assert got["imgs"]["relative"] and not got["hero"]["relative"]
    assert MD.fill("s3://b/images/{pid}/{value}", "main.jpg", {"pid": "9"}) == "s3://b/images/9/main.jpg"
    assert MD.fill("s3://b/{value}", "https://cdn.x/a.jpg", {}) == "https://cdn.x/a.jpg"  # absolute stays


def test_guess_and_thumbs(tmp_path):
    for pid in ("p1", "p2"):
        (tmp_path / "images" / pid).mkdir(parents=True)
        Image.new("RGB", (900, 600), (200, 30, 30)).save(tmp_path / "images" / pid / "main.jpg")
    file_uri = str(tmp_path / "catalog.jsonl")
    t = MD.guess(file_uri, ["pid", "imgs"], {"pid": "p1"}, "main.jpg")
    assert t == f"{tmp_path}/images/{{pid}}/{{value}}"
    dest = tmp_path / "home" / "cache" / "thumbs"
    srcs = [MD.fill(t, "main.jpg", {"pid": p}) for p in ("p1", "p2")] + [f"{tmp_path}/nope.jpg"]
    out = MD.thumbs(srcs, str(dest), 160)
    files = [out[s]["file"] for s in srcs[:2]]
    assert all(os.path.exists(dest / f) for f in files) and "error" in out[srcs[2]]
    assert Image.open(dest / files[0]).size == (160, 107)
    assert MD.thumbs(srcs[:1], str(dest), 160)[srcs[0]]["file"] == files[0]  # cached
    with pytest.raises(ValueError, match="cache/thumbs"):
        MD.thumbs(srcs, str(tmp_path / "elsewhere"), 160)


def test_templates_are_kept_per_dataset(tmp_path):
    from captain_search.workspace import init_workspace

    ctx = init_workspace("t", str(tmp_path / "store"))
    MD.set_template(ctx, "catalogs/x", "imgs", "s3://b/{pid}/{value}")
    assert MD.templates(ctx, "catalogs/x") == {"imgs": "s3://b/{pid}/{value}"}
    with pytest.raises(ValueError, match="value"):
        MD.set_template(ctx, "catalogs/x", "imgs", "s3://b/fixed.jpg")
    MD.set_template(ctx, "catalogs/x", "imgs", None)
    assert MD.templates(ctx, "catalogs/x") == {}
