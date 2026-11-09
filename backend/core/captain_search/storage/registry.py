"""Write-once versions, content-addressed blobs and compare-and-swap aliases on top of a Store.

Layout (relative to the workspace root):
  objects/sha256/<ab>/<hash>             blobs
  <kind-dir>/<name>/v<N>/MANIFEST.yaml    the version (claimed by create-only write)
  <kind-dir>/<name>/v<N>/SHA256SUMS       one line per file: "<sha>  <path>"
  aliases/<kind>/<name>/<alias>.yaml      mutable pointer, updated by CAS
  workspace.yaml                          the Workspace record
"""
from __future__ import annotations

import hashlib
import re
from typing import TypeVar

import yaml

from ..model.entities import KIND_DIRS, VERSION_KINDS, FileEntry, VersionBase
from .backend import AlreadyExists, Conflict, NotFound, Store

T = TypeVar("T", bound=VersionBase)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}(/[a-z0-9][a-z0-9._-]{0,99}){0,7}$")
REF_RE = re.compile(r"^(?P<name>[^@:]+)(@(?P<ver>v?\d+|[a-z][a-z0-9_-]*))?(:(?P<split>[a-z0-9_-]+))?$")


def check_name(name: str) -> str:
    if not NAME_RE.match(name) or any(re.fullmatch(r"v\d+", seg) for seg in name.split("/")):
        raise ValueError(f"bad name {name!r}: use lowercase letters, digits, . _ - and up to 8 folder levels split by /")
    return name


def dump_yaml(obj) -> bytes:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Registry:
    def __init__(self, store: Store, index_lineage: bool = True):
        self.store = store
        self.index_lineage = index_lineage  # append lineage edge records on publish (lineage_index.py)

    # -- blobs -------------------------------------------------------------------------

    @staticmethod
    def blob_key(digest: str) -> str:
        return f"objects/sha256/{digest[:2]}/{digest}"

    def put_blob(self, data: bytes, path: str) -> FileEntry:
        digest = sha256(data)
        try:
            self.store.put_new(self.blob_key(digest), data)
        except AlreadyExists:
            pass  # same content, same key: dedup
        return FileEntry(path=path, sha256=digest, bytes=len(data))

    def get_blob(self, entry: FileEntry) -> bytes:
        data = self.store.get(self.blob_key(entry.sha256))
        if sha256(data) != entry.sha256:
            raise ValueError(f"blob {entry.sha256} failed its checksum")
        return data

    # -- versions ----------------------------------------------------------------------

    @staticmethod
    def version_dir(kind: str, name: str, version: int) -> str:
        return f"{KIND_DIRS[kind]}/{name}/v{version}"

    def versions(self, kind: str, name: str) -> list[int]:
        pfx = f"{KIND_DIRS[kind]}/{name}/"
        out = set()
        for k in self.store.list(pfx):
            m = re.match(rf"^{re.escape(pfx)}v(\d+)/MANIFEST\.yaml$", k)
            if m:
                out.add(int(m.group(1)))
        return sorted(out)

    def names(self, kind: str) -> list[str]:
        d = KIND_DIRS[kind] + "/"
        out = set()
        for k in self.store.list(d):
            m = re.match(rf"^{re.escape(d)}(.+)/v\d+/MANIFEST\.yaml$", k)
            if m:
                out.add(m.group(1))
        return sorted(out)

    def publish(self, manifest: T, max_tries: int = 20) -> T:
        """Claim the next free version number with a create-only write. Two concurrent
        publishers get different numbers; neither overwrites the other."""
        check_name(manifest.name)
        kind = manifest.kind
        existing = self.versions(kind, manifest.name)
        n = (existing[-1] + 1) if existing else 1
        for _ in range(max_tries):
            m = manifest.model_copy(update={"version": n})
            if m.parent is None and n > 1:
                m = m.model_copy(update={"parent": n - 1})
            # model_copy skips validation, so validate before anything is written for good
            m = type(m).model_validate(m.model_dump(mode="json"))
            d = self.version_dir(kind, m.name, n)
            try:
                self.store.put_new(f"{d}/MANIFEST.yaml", dump_yaml(m.model_dump(mode="json")))
            except AlreadyExists:
                n += 1
                continue
            sums = "".join(f"{f.sha256}  {f.path}\n" for f in m.files)
            self.store.put_new(f"{d}/SHA256SUMS", sums.encode())
            if self.index_lineage:
                from ..lineage_index import record

                record(self, m)
            return m
        raise Conflict(f"could not claim a version for {kind}/{manifest.name} after {max_tries} tries")

    def load(self, kind: str, name: str, version: int | None = None):
        if version is None:
            vs = self.versions(kind, name)
            if not vs:
                raise NotFound(f"{kind} {name} has no versions")
            version = vs[-1]
        try:
            raw = yaml.safe_load(self.store.get(f"{self.version_dir(kind, name, version)}/MANIFEST.yaml"))
        except NotFound:
            # In words, not the storage key the store reports ("backtest-runs/x/v1/MANIFEST.yaml").
            raise NotFound(f"no {kind.replace('-', ' ')} {name} with version {version}") from None
        return VERSION_KINDS[kind].model_validate(raw)

    def resolve(self, kind: str, ref: str):
        """name, name@v3, name@3 or name@<alias>. Returns (manifest, split or None)."""
        m = REF_RE.match(ref)
        if not m:
            raise ValueError(f"bad reference {ref!r}")
        name, ver, split = m.group("name"), m.group("ver"), m.group("split")
        if ver is None:
            return self.load(kind, name), split
        if ver.lstrip("v").isdigit():
            return self.load(kind, name, int(ver.lstrip("v"))), split
        target, _ = self.get_alias(kind, name, ver)
        return self.load(kind, name, target), split

    def verify(self, kind: str, name: str, version: int) -> list[str]:
        """Check every file's blob against its sha256 and the SHA256SUMS file. Returns problems."""
        m = self.load(kind, name, version)
        problems = []
        sums = self.store.get(f"{self.version_dir(kind, name, version)}/SHA256SUMS").decode()
        listed = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in sums.splitlines() if line}
        for f in m.files:
            if listed.get(f.path) != f.sha256:
                problems.append(f"{f.path}: SHA256SUMS disagrees with MANIFEST")
            try:
                self.get_blob(f)
            except NotFound:
                problems.append(f"{f.path}: blob missing")
            except ValueError as e:
                problems.append(f"{f.path}: {e}")
        return problems

    # -- aliases (the only mutable state) ---------------------------------------------

    @staticmethod
    def alias_key(kind: str, name: str, alias: str) -> str:
        return f"aliases/{kind}/{name}/{alias}.yaml"

    def get_alias(self, kind: str, name: str, alias: str) -> tuple[int, str]:
        data, tag = self.store.get_with_tag(self.alias_key(kind, name, alias))
        return int(yaml.safe_load(data)["version"]), tag

    def set_alias(self, kind: str, name: str, alias: str, version: int, expect_tag: str | None, by: str) -> str:
        if version not in self.versions(kind, name):
            raise NotFound(f"{kind} {name}@v{version}")
        body = dump_yaml({"version": version, "set_by": by})
        return self.store.put_if(self.alias_key(kind, name, alias), body, expect_tag)
