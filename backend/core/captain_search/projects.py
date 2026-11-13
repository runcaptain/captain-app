"""Projects: one per company (or any line of work) inside a workspace.

A search engineer who trains models for several companies needs everything for one company in one place: its
connections, datasets, pipelines and models, backtests, investigations, and the lineage of its models. A project is
that place.

Membership is mutable organization, kept in the workspace's storage (`projects.yaml`, compare-and-swap) and never
written into the write-once records. An item belongs to a project when:
1. it was put there (`assign`), for example by importing into the project; or
2. a model registry's brand maps to the project (a registry folder `<architecture>/<brand>/<version>`), which also
   claims the pipelines linked to those models; or
3. a dataset sits in a folder named like the project, or a pipeline, backtest or dataset name starts with
   `<project>/` or `<project>-`.

Anything unclaimed is "not in a project". Nothing is ever moved or copied to join a project.
"""
from __future__ import annotations

import datetime as dt
import re

import yaml

from .storage.backend import Conflict, NotFound
from .workspace import Context, whoami

KEY = "projects.yaml"
KINDS = ("connection", "dataset", "pipeline", "backtest", "investigation", "registry", "model", "signal", "grading",
         "notebook")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63]
    if not s:
        raise ValueError("name the project")
    return s


def _read(ctx: Context) -> tuple[dict, str | None]:
    try:
        body, tag = ctx.store.get_with_tag(KEY)
        doc = yaml.safe_load(body) or {}
    except NotFound:
        doc, tag = {}, None
    doc.setdefault("projects", [])
    doc.setdefault("members", {})  # "<kind>:<name>" -> project id
    doc.setdefault("brands", {})  # registry brand -> project id
    return doc, tag


def _update(ctx: Context, change) -> dict:
    for _ in range(10):
        doc, tag = _read(ctx)
        out = change(doc)
        try:
            ctx.store.put_if(KEY, yaml.safe_dump(doc, sort_keys=True).encode(), tag)
            return out if out is not None else doc
        except Conflict:
            continue
    raise Conflict("projects kept changing while saving. Try again.")


def _get(doc: dict, pid: str) -> dict:
    p = next((p for p in doc["projects"] if p["id"] == pid), None)
    if p is None:
        raise NotFound(f"no project {pid!r}")
    return p


def create(ctx: Context, name: str, description: str = "", brands: list[str] | None = None, icon: dict | None = None) -> dict:
    pid = slug(name)
    checked = _check_icon(icon) if icon and icon.get("kind") != "logo" else None  # a logo is uploaded once it exists

    def ch(doc):
        if any(p["id"] == pid for p in doc["projects"]):
            raise ValueError(f"{name} already exists. Pick another name.")
        p = {"id": pid, "name": name.strip(), "description": description, "created_by": whoami(),
             "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "archived": False,
             **({"icon": checked} if checked else {})}
        doc["projects"].append(p)
        for b in brands or [pid]:
            doc["brands"].setdefault(b.lower(), pid)
        return p

    return _update(ctx, ch)


def update(ctx: Context, pid: str, name: str | None = None, description: str | None = None,
           archived: bool | None = None, brands: list[str] | None = None) -> dict:
    def ch(doc):
        p = _get(doc, pid)
        if name is not None:
            p["name"] = name.strip()
        if description is not None:
            p["description"] = description
        if archived is not None:
            p["archived"] = bool(archived)
        if brands is not None:
            doc["brands"] = {b: q for b, q in doc["brands"].items() if q != pid}
            for b in brands:
                doc["brands"][b.lower()] = pid
        return p

    return _update(ctx, ch)


# ---- icons ------------------------------------------------------------------------------------
# A project's mark, shown wherever the project is (Edgar, 2026-11-09: "make it so I can change the icons of the
# projects"). One of:
#   {kind: "glyph", name, color}   an icon from the app's set, in one of the palette's colors
#   {kind: "brand", slug}          a vendor mark the app ships (Snowflake, AWS ...)
#   {kind: "logo", sha, ext}       an organization's own logo, uploaded into the workspace store, so teammates see it
# Uploaded logos are the user's: nothing is shipped in the app.
ICON_COLORS = ("#0043CA", "#7C3AED", "#059669", "#D97706", "#DC2626", "#0891B2", "#DB2777", "#64748B")
LOGO_TYPES = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".svg": "svg"}
LOGO_MAX = 2 * 1024 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def logo_key(sha: str, ext: str) -> str:
    return f"projects/logos/{sha}.{ext}"


def _check_icon(icon: dict | None) -> dict | None:
    if icon is None:
        return None
    if not isinstance(icon, dict):
        raise ValueError("an icon is a glyph, a brand mark or a logo")
    kind = icon.get("kind")
    if kind == "glyph":
        name, color = str(icon.get("name") or ""), str(icon.get("color") or ICON_COLORS[0]).upper()
        if not _NAME.match(name):
            raise ValueError(f"{name!r} isn't an icon name")
        if color not in ICON_COLORS:
            raise ValueError(f"pick one of the palette's colors: {', '.join(ICON_COLORS)}")
        return {"kind": "glyph", "name": name, "color": color}
    if kind == "brand":
        slug_ = str(icon.get("slug") or "")
        if not _NAME.match(slug_):
            raise ValueError(f"{slug_!r} isn't a brand mark")
        return {"kind": "brand", "slug": slug_}
    if kind == "logo":
        sha, ext = str(icon.get("sha") or ""), str(icon.get("ext") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or ext not in LOGO_TYPES.values():
            raise ValueError("upload the logo first")
        return {"kind": "logo", "sha": sha, "ext": ext}
    raise ValueError("an icon is a glyph, a brand mark or a logo")


def set_icon(ctx: Context, pid: str, icon: dict | None) -> dict:
    """Set (or with None, clear) a project's icon."""
    checked = _check_icon(icon)
    if checked and checked["kind"] == "logo" and not ctx.store.exists(logo_key(checked["sha"], checked["ext"])):
        raise NotFound("that logo isn't in the workspace. Upload it again.")

    def ch(doc):
        p = _get(doc, pid)
        if checked is None:
            p.pop("icon", None)
        else:
            p["icon"] = checked
        return p

    return _update(ctx, ch)


def upload_logo(ctx: Context, pid: str, path: str) -> dict:
    """Put an image file (png, jpg or svg, 2 MB at most) in the workspace store and make it the project's icon."""
    import hashlib
    import os

    from .storage.backend import AlreadyExists

    path = os.path.expanduser(path)
    ext = LOGO_TYPES.get(os.path.splitext(path)[1].lower())
    if not ext:
        raise ValueError("a logo is a PNG, JPEG or SVG file")
    if not os.path.isfile(path):
        raise NotFound(f"there's no file at {path}")
    if os.path.getsize(path) > LOGO_MAX:
        raise ValueError("the logo is over 2 MB. Use a smaller file.")
    with open(path, "rb") as f:
        data = f.read()
    if ext == "svg":
        if b"<svg" not in data[:4096].lower():
            raise ValueError("that file isn't an SVG image")
    else:
        from PIL import Image
        import io

        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:  # noqa: BLE001
            raise ValueError("that file isn't an image Captain can read") from None
    sha = hashlib.sha256(data).hexdigest()
    try:
        ctx.store.put_new(logo_key(sha, ext), data)
    except AlreadyExists:
        pass  # the same logo, already there
    return set_icon(ctx, pid, {"kind": "logo", "sha": sha, "ext": ext})


LOGO_PX = 48  # cached size: crisp at 14 to 24 px on a 2x screen


def logo_file(ctx: Context, pid: str, dest_dir: str) -> dict:
    """Write the project's logo, small, into the app's cache folder, so a screen can draw it. Returns {file} (its name
    in dest_dir) or {file: None} when the project has no logo. A raster is resized with a good filter to LOGO_PX; an SVG
    is kept as vector, sized so it is drawn at that resolution."""
    import io
    import os

    d = os.path.realpath(os.path.expanduser(dest_dir))
    if not os.path.isabs(dest_dir) or not d.replace("\\", "/").endswith("/cache/project-logos"):
        raise ValueError("logos are cached only in an app's cache/project-logos folder")
    doc, _ = _read(ctx)
    icon = (_get(doc, pid).get("icon") or {})
    if icon.get("kind") != "logo":
        return {"file": None}
    sha, ext = icon["sha"], icon["ext"]
    name = f"{sha[:24]}-{LOGO_PX}.{'svg' if ext == 'svg' else 'png'}"
    out = os.path.join(d, name)
    if not os.path.exists(out):
        os.makedirs(d, exist_ok=True)
        data = ctx.store.get(logo_key(sha, ext))
        tmp = f"{out}.{os.getpid()}.tmp"
        if ext == "svg":
            # image() rasterizes an SVG at its own width and height (times 2): give it the drawn size.
            text = data.decode("utf-8", "replace")
            half = LOGO_PX // 2
            text = re.sub(r"<svg\b([^>]*)>", lambda m: "<svg" + re.sub(r'\s(width|height)="[^"]*"', "", m.group(1))
                          + f' width="{half}" height="{half}">', text, count=1)
            with open(tmp, "w") as f:
                f.write(text)
        else:
            from PIL import Image

            im = Image.open(io.BytesIO(data)).convert("RGBA")
            im.thumbnail((LOGO_PX, LOGO_PX), Image.LANCZOS)
            canvas = Image.new("RGBA", (LOGO_PX, LOGO_PX), (0, 0, 0, 0))
            canvas.paste(im, ((LOGO_PX - im.width) // 2, (LOGO_PX - im.height) // 2))
            canvas.save(tmp, "PNG")
        os.replace(tmp, out)
    return {"file": name}


def pending_handoffs(ctx: Context, pid: str) -> list[dict]:
    """Live handoffs of this project's champion or experiments that no deployment has been linked to yet."""
    from . import handoff as HO
    from . import project_state as PS

    doc, _ = PS.read(ctx, pid)
    mine = {e["pipeline"] for e in doc.get("experiments") or []} | ({doc["champion"]} if doc.get("champion") else set())
    if not mine:
        return []
    out = []
    for b in HO.listing(ctx):
        if b.get("pipeline") in mine and not any(e.get("kind") == "deployment" for e in b.get("links") or []):
            out.append({"id": b["id"], "pipeline": b["pipeline"], "environment": b.get("environment")})
    return out


def delete(ctx: Context, pid: str, force: bool = False) -> dict:
    """Remove a project: its entry, its membership and brand claims, and its state. Never a dataset, pipeline, model or
    run: those stay in the workspace, unclaimed. Refused while a live handoff waits for its deployment, unless forced."""
    from . import project_state as PS

    c = contents(ctx, pid)
    waiting = pending_handoffs(ctx, pid)
    if waiting and not force:
        err = ValueError(f"{pid} has {len(waiting)} launch review(s) waiting for a deployment "
                         f"({', '.join(h['pipeline'] for h in waiting)}). Link or close them first, or delete with force.")
        err.handoffs = waiting
        raise err

    def ch(doc):
        _get(doc, pid)
        doc["projects"] = [p for p in doc["projects"] if p["id"] != pid]
        doc["members"] = {k: v for k, v in doc["members"].items() if v != pid}
        doc["brands"] = {b: q for b, q in doc["brands"].items() if q != pid}
        return None

    _update(ctx, ch)
    # Stores never delete, so the state becomes a tombstone that reads as empty (a new project of this name starts clean).
    for _ in range(10):
        try:
            _, tag = ctx.store.get_with_tag(PS.key(pid))
        except NotFound:
            tag = None
        try:
            ctx.store.put_if(PS.key(pid), yaml.safe_dump({"deleted": True, "deleted_by": whoami(),
                                                          "deleted_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}).encode(), tag)
            break
        except Conflict:
            continue
    kept = {"datasets": len(c["dataset"]), "pipelines": len(c["pipeline"]), "backtests": len(c["backtest"]),
            "models": len(c["models"]), "notebooks": len(c["notebook"]), "connections": len(c["connection"])}
    return {"deleted": pid, "kept": kept, "forced_past": waiting if force else []}


def assign(ctx: Context, pid: str | None, items: list[dict], clear: bool = False) -> dict:
    """Put items ({kind, name}) in a project, or take them out with pid None. clear=True forgets the choice instead,
    so the rules decide again (a model's brand, a name that starts with the project's id)."""
    for it in items:
        if it.get("kind") not in KINDS:
            raise ValueError(f"a project holds {', '.join(KINDS)}, not {it.get('kind')!r}")

    def ch(doc):
        if pid is not None:
            _get(doc, pid)
        for it in items:
            key = f"{it['kind']}:{it['name']}"
            if clear:
                doc["members"].pop(key, None)
            elif pid is None:
                doc["members"][key] = None  # explicit "not in a project", beats the name rules
            else:
                doc["members"][key] = pid
        return {"project": pid, "items": items}

    return _update(ctx, ch)


# ---- who belongs where ----------------------------------------------------------------------


class Resolver:
    """Answers "which project is this in?" for one read of projects.yaml."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.doc, _ = _read(ctx)
        self.ids = [p["id"] for p in self.doc["projects"] if not p.get("archived")]
        self._models = None

    def _by_name(self, name: str) -> str | None:
        low = name.lower()
        for pid in sorted(self.ids, key=len, reverse=True):
            if low == pid or low.startswith((pid + "/", pid + "-", pid + "_")) or f"/{pid}/" in f"/{low}/":
                return pid
        return None

    def _model_brands(self) -> dict[str, str]:
        """Pipeline name -> registry brand, for pipelines linked to a registry model."""
        if self._models is None:
            self._models = {}
            for n in self.ctx.registry.names("pipeline"):
                try:
                    pm = self.ctx.registry.load("pipeline", n)
                except Exception:  # noqa: BLE001
                    continue
                ref = getattr(pm, "registry_ref", None) or ""
                m = re.search(r"/([^/]+)/([^/]+)/?$", ref.rstrip("/"))
                if m:
                    self._models[n] = m.group(1).lower()
        return self._models

    def of(self, kind: str, name: str, brand: str | None = None) -> str | None:
        key = f"{kind}:{name}"
        if key in self.doc["members"]:
            return self.doc["members"][key]
        if brand and brand.lower() in self.doc["brands"]:
            return self.doc["brands"][brand.lower()]
        if kind == "pipeline":
            b = self._model_brands().get(name)
            if b and b in self.doc["brands"]:
                return self.doc["brands"][b]
        if kind == "dataset":
            from . import library as LIB

            ldoc, _ = LIB._read(self.ctx)
            folder = LIB.folder_of(ldoc, name)
            hit = self._by_name(folder) if folder else None
            if hit:
                return hit
        return self._by_name(name)


def listing(ctx: Context) -> list[dict]:
    r = Resolver(ctx)
    counts = {p["id"]: {k: 0 for k in KINDS} | {"models": 0} for p in r.doc["projects"]}
    for kind, names in _all(ctx).items():
        for n in names:
            pid = r.of(kind, n)
            if pid in counts:
                counts[pid][kind] += 1
    for m in _models(ctx):
        pid = r.of("model", m["id"], brand=m.get("brand"))
        if pid in counts:
            counts[pid]["models"] += 1
    brands = {}
    for b, pid in r.doc["brands"].items():
        brands.setdefault(pid, []).append(b)
    return [{**p, "brands": sorted(brands.get(p["id"], [])), "counts": counts[p["id"]]} for p in r.doc["projects"]]


def _all(ctx: Context) -> dict[str, list[str]]:
    from . import investigations as INV

    out = {"connection": ctx.connections(), "dataset": ctx.registry.names("dataset"),
           "pipeline": ctx.registry.names("pipeline"), "backtest": ctx.registry.names("backtest"),
           "notebook": ctx.registry.names("notebook")}
    try:
        out["investigation"] = [i["id"] for i in INV.listing(ctx)]
    except Exception:  # noqa: BLE001
        out["investigation"] = []
    return out


def _models(ctx: Context) -> list[dict]:
    from . import models as MD

    try:
        return MD.listing(ctx)
    except Exception:  # noqa: BLE001
        return []


def contents(ctx: Context, pid: str) -> dict:
    """Everything in one project, by kind."""
    r = Resolver(ctx)
    p = _get(r.doc, pid)
    out = {k: [] for k in KINDS}
    for kind, names in _all(ctx).items():
        out[kind] = [n for n in names if r.of(kind, n) == pid]
    out["models"] = [m for m in _models(ctx) if r.of("model", m["id"], brand=m.get("brand")) == pid]
    # The weights models the project holds (member `model:weights:<name>`), each version a thing that can be
    # deployed with its `deploy` name. They are not in a registry snapshot, so they are listed apart.
    try:
        from .staging import predict as PD

        out["networks"] = PD.networks_of(ctx, pid)
    except Exception:  # noqa: BLE001  a workspace where weights can't be read still has its project page
        out["networks"] = []
    return {"project": p, **out}


def home(ctx: Context, pid: str) -> dict:
    """A project's home screen: its datasets, models, notebooks and recent runs, each with what a row needs."""
    from . import mlflow_bridge as MF

    c = contents(ctx, pid)

    def latest(kind, n):
        try:
            m = ctx.registry.resolve(kind, n)[0]
            return m
        except Exception:  # noqa: BLE001
            return None

    datasets = []
    for n in c["dataset"]:
        m = latest("dataset", n)
        if m is not None:
            datasets.append({"name": n, "version": m.version, "kind": getattr(m.kind, "value", m.kind),
                             "rows": m.row_count, "created_at": m.created_at.isoformat(timespec="seconds"),
                             "created_by": m.created_by, "description": m.description})
    pipelines = []
    for n in c["pipeline"]:
        m = latest("pipeline", n)
        if m is not None:
            pipelines.append({"name": n, "version": m.version, "engine": m.engine,
                              "created_at": m.created_at.isoformat(timespec="seconds"), "created_by": m.created_by})
    notebooks = []
    for n in c["notebook"]:
        m = latest("notebook", n)
        if m is not None:
            notebooks.append({"name": n, "version": m.version, "created_at": m.created_at.isoformat(timespec="seconds"),
                              "created_by": m.created_by})
    runs = []
    for n in c["backtest"]:
        try:
            r = ctx.registry.resolve("backtest-run", n)[0]
        except Exception:  # noqa: BLE001
            continue
        runs.append({"backtest": n, "run": f"{r.name}@v{r.version}", "created_at": r.created_at.isoformat(timespec="seconds"),
                     "arms": [a.arm for a in r.arms]})
    runs.sort(key=lambda r: r["created_at"], reverse=True)
    try:
        links = MF.links(ctx)
    except Exception:  # noqa: BLE001
        links = {}
    mine = {p["name"] for p in pipelines}
    mlflow = [{"model": k.rpartition("@")[0], "version": k.rpartition("@")[2], "pipeline": v}
              for k, v in links.items() if v.split("@")[0] in mine]
    doc, _ = _read(ctx)
    brands = sorted(b for b, q in doc["brands"].items() if q == pid)
    return {"project": {**c["project"], "brands": brands}, "datasets": sorted(datasets, key=lambda d: d["created_at"], reverse=True),
            "pipelines": pipelines, "models": c["models"], "mlflow_models": mlflow, "networks": c["networks"], "notebooks": notebooks,
            "runs": runs[:20], "connections": c["connection"], "investigations": c["investigation"],
            "counts": {"datasets": len(datasets), "pipelines": len(pipelines), "notebooks": len(notebooks),
                       "runs": len(runs), "models": len(c["models"]) + len(mlflow) + len(c["networks"])},
            "empty": not (datasets or pipelines or notebooks or runs or c["models"] or c["networks"])}


def member_filter(ctx: Context, pid: str | None, kind: str):
    """A predicate name -> bool for list routes' ?project= filter. pid "none" keeps unclaimed items."""
    if not pid:
        return lambda name, **kw: True
    r = Resolver(ctx)
    want = None if pid == "none" else pid
    return lambda name, **kw: r.of(kind, name, **kw) == want


def lineage(ctx: Context, pid: str) -> dict:
    """The project's model lineage: its registry models, plus the pipelines linked to them and the backtests that
    compared those pipelines."""
    from . import models as MD

    full = MD.lineage(ctx)
    r = Resolver(ctx)
    keep = set()
    for n in full["nodes"]:
        if n["kind"] in ("model", "model-unregistered", "demo") and r.of("model", n["id"], brand=n.get("brand")) == pid:
            keep.add(n["id"])
    for e in full["edges"]:
        if e["relation"] == "built_on" and e["src"] in keep:
            keep.add(e["dst"])
    nodes = [n for n in full["nodes"] if n["id"] in keep]
    edges = [e for e in full["edges"] if e["src"] in keep and e["dst"] in keep]
    for name in ctx.registry.names("pipeline"):
        if r.of("pipeline", name) != pid:
            continue
        nid = f"pipeline:{name}"
        nodes.append({"id": nid, "kind": "pipeline", "label": name})
        b = r._model_brands().get(name)
        try:
            ref = ctx.registry.load("pipeline", name).registry_ref or ""
        except Exception:  # noqa: BLE001
            ref = ""
        m = re.search(r"/([^/]+)/([^/]+)/?$", ref.rstrip("/"))
        if b and m and f"model:{b}@{m.group(2)}" in keep:
            edges.append({"src": f"model:{b}@{m.group(2)}", "dst": nid, "relation": "serves"})
    for name in ctx.registry.names("backtest"):
        try:
            bt = ctx.registry.load("backtest", name)
        except Exception:  # noqa: BLE001
            continue
        arms = [a.split("@")[0] for a in bt.arms]
        if r.of("backtest", name) == pid or any(r.of("pipeline", a) == pid for a in arms):
            bid = f"backtest:{name}"
            nodes.append({"id": bid, "kind": "backtest", "label": name})
            edges += [{"src": f"pipeline:{a}", "dst": bid, "relation": "compared_in"} for a in arms
                      if any(n["id"] == f"pipeline:{a}" for n in nodes)]
    return {"project": pid, "nodes": nodes, "edges": edges}
