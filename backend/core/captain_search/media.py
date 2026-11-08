"""Images in data: find the columns that hold them, say where relative ones live, and make thumbnails to show.

A catalog often stores images as full addresses (https://..., s3://...), and often as bare file names ("main.jpg")
whose folder lives elsewhere, keyed by the product. So for each dataset:

- **detect:** a column is an image column when its values (or a list's items) are image addresses or image file
  names. A column of bare names is "relative" until someone says where they live.
- **where:** a template per column, kept in the workspace (media.yaml): `s3://bucket/images/{pid}/{value}` where
  `{value}` is the cell (each item of a list) and `{<column>}` any column of the same row. Captain tries a few likely
  places first, next to the dataset's own file, and keeps the first that holds the first row's image.
- **thumbs:** fetch each image (S3 through the workspace's login, or the web), shrink it, and save it as a JPEG in the
  folder the app asks for, under its own home, since the app draws images only from there. Cached by address.

Nothing here changes the data.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import io
import os
import re
from urllib.parse import urlparse

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context

KEY = "media.yaml"
IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif|bmp|tiff?)(\?.*)?$", re.I)
ID_HINTS = ("pid", "product_id", "productid", "sku", "id", "item_id", "itm", "asin", "gtin", "ean", "upc")
MAX_BYTES = 15 * 1024 * 1024
SIZES = (64, 160, 480)


def _items(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [x if isinstance(x, str) else (x.get("url") or x.get("src") or x.get("name")) if isinstance(x, dict) else str(x)
                for x in v if x is not None]
    if isinstance(v, str) and v.startswith("[") and v.endswith("]"):
        try:
            import json

            return _items(json.loads(v))
        except ValueError:
            return [v]
    return [str(v)] if isinstance(v, str) else []


def absolute(s: str) -> bool:
    """A full address: on the web, in a bucket, or a file path on this computer."""
    return bool(re.match(r"^(https?|s3|gs)://", s or "")) or (s or "").startswith("/")


def _imagey(s: str) -> bool:
    if not s or len(s) > 2000:
        return False
    if IMAGE_EXT.search(urlparse(s).path if absolute(s) else s):
        return True
    return absolute(s) and bool(re.search(r"/(images?|img|media|photos?|thumbs?)/", s, re.I))


def detect(columns: list[str], rows: list[list]) -> list[dict]:
    """The image columns in a sample: name, whether its values are relative, and a sample value."""
    out = []
    for j, name in enumerate(columns):
        vals = [x for r in rows for x in _items(r[j] if j < len(r) else None)]
        if not vals:
            continue
        hits = [v for v in vals if _imagey(v)]
        if len(hits) >= max(1, 0.6 * len(vals)):
            out.append({"column": name, "relative": not all(absolute(v) for v in hits), "sample": hits[0],
                        "list": any(isinstance(r[j], (list, tuple)) for r in rows if j < len(r))})
    return out


def fill(template: str | None, value: str, row: dict) -> str:
    """The address of one image: the cell itself when it's absolute, else the template filled from the row."""
    if absolute(value) or not template:
        return value
    out = template.replace("{value}", value)
    return re.sub(r"\{([A-Za-z0-9_.$-]+)\}", lambda m: str(row.get(m.group(1), m.group(0))), out)


# ---- where relative images live --------------------------------------------------------------------

def _read(ctx: Context) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(KEY)
        return yaml.safe_load(body) or {}, tag
    except NotFound:
        return {}, None


def templates(ctx: Context, dataset: str) -> dict:
    return ((_read(ctx)[0].get("datasets") or {}).get(dataset) or {})


def set_template(ctx: Context, dataset: str, column: str, template: str | None) -> dict:
    if template and "{value}" not in template and not re.search(r"\{[^}]+\}", template):
        raise ValueError("the address needs {value} (the cell) or a {column} from the row, e.g. s3://bucket/images/{pid}/{value}")
    for _ in range(10):
        doc, tag = _read(ctx)
        d = doc.setdefault("datasets", {}).setdefault(dataset, {})
        if template:
            d[column] = template
        else:
            d.pop(column, None)
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=False).encode(), tag)
            return d
        except Conflict:
            continue
    raise Conflict("the image settings kept changing while saving. Try again.")


def guesses(file_uri: str | None, columns: list[str]) -> list[str]:
    """Likely homes for relative image names, next to the dataset's file."""
    if not file_uri or "/" not in file_uri:
        return []
    base = file_uri.rsplit("/", 1)[0]
    ids = [c for c in columns if c.lower() in ID_HINTS]
    out = [f"{base}/{{value}}", f"{base}/images/{{value}}"]
    for up in (base, base.rsplit("/", 1)[0] if base.count("/") > 3 else None):
        if not up:
            continue
        for c in ids:
            out += [f"{up}/images/{{{c}}}/{{value}}", f"{up}/{{{c}}}/{{value}}"]
    return list(dict.fromkeys(out))


def _exists(src: str, profile: str | None) -> bool:
    u = urlparse(src)
    try:
        if u.scheme == "s3":
            _s3(profile).head_object(Bucket=u.netloc, Key=u.path.lstrip("/"))
            return True
        if u.scheme in ("http", "https"):
            import httpx

            return httpx.head(src, timeout=10, follow_redirects=True).status_code < 400
        if not u.scheme:
            return os.path.exists(src)
    except Exception:  # noqa: BLE001  a guess that can't be read is just not the answer
        return False
    return False


def guess(file_uri: str | None, columns: list[str], row: dict, value: str, profile: str | None = None) -> str | None:
    for t in guesses(file_uri, columns):
        if _exists(fill(t, value, row), profile):
            return t
    return None


# ---- thumbnails ------------------------------------------------------------------------------------

def _s3(profile: str | None):
    from .cloud import aws_session

    return aws_session(profile, "us-west-1").client("s3")


def _fetch(src: str, profile: str | None) -> bytes:
    u = urlparse(src)
    if u.scheme == "s3":
        o = _s3(profile).get_object(Bucket=u.netloc, Key=u.path.lstrip("/"))
        if (o.get("ContentLength") or 0) > MAX_BYTES:
            raise ValueError("the image is over 15 MB")
        return o["Body"].read(MAX_BYTES + 1)
    if u.scheme in ("http", "https"):
        import httpx

        r = httpx.get(src, timeout=20, follow_redirects=True, headers={"User-Agent": "Captain"})
        if r.status_code >= 400:
            raise ValueError(f"the address answered {r.status_code}")
        return r.content[:MAX_BYTES + 1]
    if not u.scheme and src.startswith("/"):
        if os.path.getsize(src) > MAX_BYTES:
            raise ValueError("the image is over 15 MB")
        with open(src, "rb") as f:
            return f.read()
    if u.scheme == "gs":
        from google.cloud import storage

        return storage.Client().bucket(u.netloc).blob(u.path.lstrip("/")).download_as_bytes()
    raise ValueError("Captain can't read this address. Use https://, s3:// or gs://.")


def _name(src: str, size: int) -> str:
    return hashlib.sha1(f"{size}:{src}".encode()).hexdigest()[:24] + ".jpg"


def thumb(src: str, dest_dir: str, size: int = 160, profile: str | None = None) -> str:
    """Make (or reuse) a thumbnail of `src` in `dest_dir`. Returns its file name."""
    from PIL import Image, ImageOps

    name = _name(src, size)
    path = os.path.join(dest_dir, name)
    if os.path.exists(path):
        return name
    data = _fetch(src, profile)
    if len(data) > MAX_BYTES:
        raise ValueError("the image is over 15 MB")
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    im.thumbnail((size, size))
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    tmp = f"{path}.{os.getpid()}.tmp"
    im.save(tmp, "JPEG", quality=82)
    os.replace(tmp, path)
    return name


def check_dest(dest_dir: str) -> str:
    """Thumbnails go only into an app home's thumbnail folder, never anywhere a caller names."""
    d = os.path.realpath(os.path.expanduser(dest_dir))
    if not os.path.isabs(dest_dir) or not d.replace("\\", "/").endswith("/cache/thumbs"):
        raise ValueError("thumbnails go in the app's cache/thumbs folder")
    os.makedirs(d, exist_ok=True)
    return d


def thumbs(srcs: list[str], dest_dir: str, size: int = 160, profile: str | None = None) -> dict:
    """Thumbnails for many images at once (a page of rows), 8 at a time. {src: {file} or {error}}."""
    size = min(SIZES, key=lambda s: abs(s - size))
    d = check_dest(dest_dir)
    out: dict[str, dict] = {}

    def one(s):
        try:
            return s, {"file": thumb(s, d, size, profile)}
        except Exception as e:  # noqa: BLE001  one bad image doesn't stop the page
            return s, {"error": str(e)[:200] or type(e).__name__}

    with cf.ThreadPoolExecutor(8) as pool:
        for s, r in pool.map(one, list(dict.fromkeys(x for x in srcs if x))[:400]):
            out[s] = r
    return out
