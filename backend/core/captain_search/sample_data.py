"""An opt-in, isolated Sample North Pole workspace. No traffic or performance is invented."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import pyarrow as pa

from .data.datasets import snapshot, to_parquet
from .model.entities import DatasetKind, Provenance, Workspace
from .storage.backend import LocalStore
from .storage.registry import Registry, dump_yaml
from .workspace import Context, home, init_workspace, load_config, save_config

NAME = "sample-north-pole"
OWNER = "captain-sample-north-pole-v1"
MARKER = ".captain-sample.json"
NOTICE = "Sample data: synthetic prices, stock and specifications; not commercial offers. Illustrative manual judgments, not measured search results."


def bundle() -> dict:
    data = json.loads(files("captain_search").joinpath("samples/north_pole.json").read_text())
    validate(data)
    return data


def validate(data: dict) -> None:
    def require(condition, message):
        if not condition:
            raise ValueError(f"Invalid North Pole sample: {message}")

    require(set(data["catalogs"]) == {"gift", "workshop", "polar"}, "catalogs")
    products = [p for rows in data["catalogs"].values() for p in rows]
    product_ids = {p["id"] for p in products}
    require(len(product_ids) == len(products) == 150, "150 unique product IDs required")
    for key, rows in data["catalogs"].items():
        require(len(rows) == 50, f"{key} must have 50 products")
        for p in rows:
            require(p["id"].startswith(f"np-{key}-"), "stable catalog-specific ID")
            require(all(p.get(f) for f in ("catalog", "name", "title", "description", "category", "attributes", "tags")), "product fields")
            require(p["demo_data"] is True and p["currency"] == "USD", "demo/currency labels")
            require(isinstance(p["stock_quantity"], int) and p["stock_quantity"] >= 0 and p["price"] > 0, "price/stock")
            require(p["in_stock"] == (p["stock_quantity"] > 0), "stock flag")
    require(len(data["lists"]) == 6 and len({x["id"] for x in data["lists"]}) == 6, "six unique lists")
    for item in data["lists"]:
        require(item["name"].startswith("Sample ") and bool(item["product_ids"]), "sample list label/members")
        require(set(item["product_ids"]) <= product_ids and len(set(item["product_ids"])) == len(item["product_ids"]), "list references")
    qs = data["queries"]
    qids = {q["query_id"] for q in qs}
    require(len(qs) == len(qids) == 30, "30 unique queries")
    require({q["intent"] for q in qs} == {"exact", "discovery", "constraints"}, "query coverage")
    pairs = set()
    for row in data["judgments"]:
        require(row["query_id"] in qids and row["product_id"] in product_ids, "judgment references")
        require(row["grade"] in (1, 2, 3) and bool(row["explanation"].strip()), "judgment grade/explanation")
        require(row["source"] == "human" and row["evidence"] == "ILLUSTRATIVE" and row["demo_data"], "judgment labeling")
        pair = (row["query_id"], row["product_id"])
        require(pair not in pairs, "duplicate judgment")
        pairs.add(pair)
    require(len({r["query_id"] for r in data["judgments"]}) == 10, "ten judged queries")


def _root() -> Path:
    return Path(home()).absolute() / "samples" / NAME


@contextmanager
def _lock(root):
    root.parent.mkdir(parents=True, exist_ok=True)
    # Kernel locks release even if a command crashes, so reruns cannot inherit a stale lock.
    with root.with_name(root.name + ".lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0, 2)
                if not lock.tell():
                    lock.write(b"0"); lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Sample data is busy. Retry after the other sample command finishes.") from None
        yield


def _inventory(root):
    out = {}
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Refusing a sample directory reached through a symbolic link")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Refusing sample data containing a symbolic link")
        if path.is_file() and path.name != MARKER:
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _owned(root):
    try:
        marker = json.loads((root / MARKER).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raise ValueError("This directory is not an owned Sample North Pole workspace") from None
    if not isinstance(marker, dict) or marker.get("owner") != OWNER or not isinstance(marker.get("files"), dict):
        raise ValueError("This directory is not an owned Sample North Pole workspace")
    actual = _inventory(root)
    expected = marker.get("files", {})
    # Reading a dataset through the app/CLI records real access history. Those narrowly
    # validated records are disposable with the sample; ordinary added files are not.
    for key in set(actual) - set(expected):
        if _sample_read(root, key, expected):
            del actual[key]
    if marker.get("owner") != OWNER or expected != actual:
        raise ValueError("Sample workspace has changed or contains added files. Move your work out before removing it.")
    return marker


def _sample_read(root, key, owned):
    parts = key.split("/")
    if len(parts) != 5 or parts[:2] != ["metastore", "reads"] or not re.fullmatch(r"[0-9a-f]{32}\.json", parts[4]):
        return False
    name = parts[2]
    if f"datasets/{name}/v1/MANIFEST.yaml" not in owned:
        return False
    try:
        record = json.loads((root / key).read_text())
        return (set(record) == {"dataset", "version", "user", "agent", "via", "at"}
                and record["dataset"] == name and record["version"] in (None, 1)
                and record["via"] in ("app", "notebook", "agent", "cli")
                and isinstance(record["user"], str) and (record["agent"] is None or isinstance(record["agent"], str))
                and dt.datetime.fromisoformat(record["at"]).date().isoformat() == parts[3])
    except (ValueError, TypeError, KeyError, OSError):
        return False


def seed(*, dry_run: bool = False) -> dict:
    data = bundle()
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    root = _root()
    result = {"workspace": NAME, "label": "Sample North Pole", "products": 150, "catalogs": 3,
              "lists": 6, "queries": 30, "judged_queries": 10, "judgments": len(data["judgments"]), "notice": NOTICE}
    if dry_run:
        return {**result, "dry_run": True}
    with _lock(root):
        cfg = load_config()
        binding = cfg.get("workspaces", {}).get(NAME)
        if binding and binding.get("storage") not in (str(root), root.as_uri()):
            raise ValueError("The sample workspace name is already in use at another location")
        if root.exists() or root.is_symlink():
            marker = _owned(root)
            if marker["bundle_sha256"] != fingerprint:
                raise ValueError("An older sample is installed. Remove it before loading the new sample.")
        else:
            # Build in a temporary directory: a failure never leaves a partial sample or a config binding.
            with tempfile.TemporaryDirectory(prefix=".north-pole-build-", dir=root.parent) as tmp:
                store = LocalStore(tmp)
                ws = Workspace(org="sample", workspace=NAME, name=NAME, storage_uri=str(root),
                               created_by="sample@captain.local", members=["sample@captain.local"])
                store.put_new("workspace.yaml", dump_yaml(ws.model_dump(mode="json")))
                ctx = Context(ws, store, Registry(store))
                def save(name, rows, kind, label):
                    table = pa.Table.from_pylist(rows)
                    m = snapshot(ctx, name, table, kind=kind, description=label + ". " + NOTICE, dry_run=True,
                             provenance=Provenance(source="generated", params={"sample": OWNER, "bundle_sha256": fingerprint,
                                                     "evidence": "ILLUSTRATIVE", "manually_curated": kind == DatasetKind.judgments}))
                    updates = {"files": [ctx.registry.put_blob(to_parquet(table), "data/part-0.parquet")],
                               "tags": {"sample": "true", "evidence": "ILLUSTRATIVE"}}
                    if kind == DatasetKind.judgments:
                        updates["judgment_source"] = "human"
                    ctx.registry.publish(m.model_copy(update=updates))
                for key, rows in data["catalogs"].items():
                    save("sample-" + key + "-catalog", rows, DatasetKind.catalog, "Sample " + rows[0]["catalog"])
                for item in data["lists"]:
                    rows = [{"list_id": item["id"], "list_name": item["name"], "position": i, "product_id": pid,
                             "demo_data": True} for i, pid in enumerate(item["product_ids"], 1)]
                    save("sample-" + item["id"].removeprefix("np-list-"), rows, DatasetKind.generic, item["name"])
                save("sample-search-queries", data["queries"], DatasetKind.queries, "Sample search queries")
                save("sample-illustrative-judgments", data["judgments"], DatasetKind.judgments,
                     "Sample illustrative judgments (positive examples only; all unlisted pairs are unjudged)")
                temp = Path(tmp)
                marker = {"owner": OWNER, "bundle_sha256": fingerprint, "previous_workspace": cfg.get("current"),
                          "files": _inventory(temp)}
                (temp / MARKER).write_text(json.dumps(marker, sort_keys=True, indent=2) + "\n")
                temp.rename(root)
        init_workspace(NAME, str(root), org="sample")
    return result


def remove(*, dry_run: bool = False) -> dict:
    root = _root()
    with _lock(root):
        cfg = load_config()
        if not root.exists() and not root.is_symlink():
            return {"removed": False, "current": cfg.get("current")}
        marker = _owned(root)
        binding = cfg.get("workspaces", {}).get(NAME)
        if binding and binding.get("storage") not in (str(root), root.as_uri()):
            raise ValueError("Sample workspace binding points at another location; nothing removed")
        if dry_run:
            return {"removed": False, "dry_run": True, "current": cfg.get("current")}
        # No user-created or modified files are permitted by _owned, including additional dataset versions.
        shutil.rmtree(root)
        cfg.get("workspaces", {}).pop(NAME, None)
        if cfg.get("current") == NAME:
            previous = marker.get("previous_workspace")
            cfg["current"] = previous if previous in cfg.get("workspaces", {}) else next(iter(cfg.get("workspaces", {})), None)
        save_config(cfg)
        return {"removed": True, "current": cfg.get("current")}
