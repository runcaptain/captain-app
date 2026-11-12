"""A Modal volume as a store: `modal://<volume>@<environment>/<folder>` (the environment may be left out).

Modal has no roles to grant a bucket to, and keys short enough to be safe would run out under a long-lived
endpoint. A volume needs neither: it lives in the Modal workspace, the endpoint mounts it, and it is deleted with
the serving setup. Only the serving bundle, the control document and the request log go there.

Two ways in, one class each:

  from this computer   through Modal's API (read_file, batch_upload, listdir). Create-only writes are real: Modal
                       refuses to overwrite. A compare-and-swap is read, compare, write: two writers racing on one
                       key can both win. The only document swapped here is the endpoint's control document, which
                       one person at a time changes.
  inside the endpoint  the volume is mounted (CST_MODAL_VOLUME_MOUNT), so it is a folder. A mounted volume shows
                       other writers' changes only after a reload, and keeps its own writes only after a commit:
                       a small thread reloads every couple of seconds, and every write commits.
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
import time

from .backend import AlreadyExists, Conflict, LocalStore, NotFound, StorageError, Store

MOUNT_ENV = "CST_MODAL_VOLUME_MOUNT"
RELOAD_S = 2.0


def parse(uri: str) -> tuple[str, str | None, str]:
    """(volume, environment or None, folder) of modal://<volume>@<environment>/<folder>."""
    rest = uri.split("://", 1)[1]
    where, _, folder = rest.partition("/")
    vol, _, env = where.partition("@")
    if not vol:
        raise StorageError(f"a Modal volume address is modal://<volume>@<environment>/<folder>, not {uri}")
    return vol, env or None, folder.strip("/")


def _missing(e: Exception) -> bool:
    return isinstance(e, FileNotFoundError) or type(e).__name__ == "NotFoundError" or "no such file" in str(e).lower() or "not found" in str(e).lower()


class ModalVolumeStore(Store):
    """From this computer, through Modal's API."""

    def __init__(self, volume: str, environment: str | None = None, prefix: str = "", vol=None):
        self.volume, self.environment, self.prefix = volume, environment, prefix.strip("/")
        self._vol = vol

    def _v(self):
        if self._vol is None:
            import modal  # type: ignore

            self._vol = modal.Volume.from_name(self.volume, environment_name=self.environment)
        return self._vol

    def _k(self, key: str) -> str:
        return "/" + "/".join(p for p in (self.prefix, key.lstrip("/")) if p)

    def _upload(self, key: str, data: bytes, force: bool) -> None:
        with self._v().batch_upload(force=force) as b:
            b.put_file(io.BytesIO(data), self._k(key))

    def put_new(self, key: str, data: bytes) -> None:
        try:
            self._upload(key, data, force=False)
        except FileExistsError:
            raise AlreadyExists(key) from None

    def get_with_tag(self, key: str) -> tuple[bytes, str]:
        try:
            data = b"".join(self._v().read_file(self._k(key)))
        except Exception as e:  # noqa: BLE001
            if _missing(e):
                raise NotFound(key) from None
            raise
        return data, hashlib.sha256(data).hexdigest()

    def put_if(self, key: str, data: bytes, tag: str | None) -> str:
        try:
            _, cur = self.get_with_tag(key)
        except NotFound:
            cur = None
        if cur != tag:
            raise Conflict(key)
        if tag is None:
            self.put_new(key, data)
        else:
            self._upload(key, data, force=True)
        return hashlib.sha256(data).hexdigest()

    def list(self, prefix: str = "") -> list[str]:
        # Listing is by key prefix, as on the bucket stores: list the folder the prefix sits in, then filter.
        folder = prefix[:prefix.rfind("/") + 1]
        try:
            entries = self._v().listdir(self._k(folder) if folder or self.prefix else "/", recursive=True)
        except Exception as e:  # noqa: BLE001
            if _missing(e):
                return []
            raise
        base = (self.prefix + "/") if self.prefix else ""
        out = []
        for e in entries:
            if getattr(getattr(e, "type", None), "name", "FILE") != "FILE":
                continue
            p = str(e.path).lstrip("/")
            if base and not p.startswith(base):
                continue
            p = p[len(base):]
            if p.startswith(prefix):
                out.append(p)
        return sorted(out)

    def remove(self, key: str, recursive: bool = False) -> None:
        """Not part of the store contract (stores are append-only). Teardown uses it to empty the serving folder."""
        try:
            self._v().remove_file(self._k(key), recursive=recursive)
        except Exception as e:  # noqa: BLE001
            if not _missing(e):
                raise


class MountedVolumeStore(LocalStore):
    """Inside the endpoint: the mounted folder, reloaded in the background and committed after every write."""

    def __init__(self, root: str, vol=None, reload_s: float = RELOAD_S):
        super().__init__(root)
        self._vol = vol
        self._reload_s = reload_s
        # One lock for reload, write and commit. A reload throws away what is written but not yet committed, so a
        # write that raced one lost its folder ("No such file or directory", seen on a real volume 2026-11-11).
        self._lock = threading.RLock()
        self._stop = threading.Event()
        if vol is not None:
            threading.Thread(target=self._loop, daemon=True, name="modal-volume-reload").start()

    def _loop(self) -> None:
        while not self._stop.wait(self._reload_s):
            self.reload()

    def reload(self) -> None:
        with self._lock:
            try:
                self._vol.reload()
            except Exception as e:  # noqa: BLE001  a file open for a moment: the next round gets it
                if os.environ.get("CST_DEBUG"):
                    print(f"staging: volume reload skipped: {e}", flush=True)

    def _commit(self) -> None:
        if self._vol is None:
            return
        with self._lock:
            for i in range(3):
                try:
                    self._vol.commit()
                    return
                except Exception:  # noqa: BLE001
                    if i == 2:
                        raise
                    time.sleep(0.2)

    # A Modal volume has no hard links and no file locks, which is how a folder store makes a write create-only.
    # Opening with O_EXCL is create-only too: it fails when the file is there.
    def put_new_chunks(self, key: str, chunks) -> int:
        dest = self._p(key)
        with self._lock:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                raise AlreadyExists(key) from None
            n = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    for c in chunks:
                        fh.write(c)
                        n += len(c)
            except BaseException:
                try:
                    os.unlink(dest)  # never leave half a file where a whole one is expected
                except OSError:
                    pass
                raise
            self._commit()
        return n

    def put_new(self, key: str, data: bytes) -> None:
        self.put_new_chunks(key, [data])

    def put_if(self, key: str, data: bytes, tag: str | None) -> str:
        with self._lock:
            try:
                _, cur = self.get_with_tag(key)
            except NotFound:
                cur = None
            if cur != tag:
                raise Conflict(key)
            dest = self._p(key)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
            self._commit()
        return hashlib.sha256(data).hexdigest()


def open_modal(uri: str) -> Store:
    vol, env, folder = parse(uri)
    mount = os.environ.get(MOUNT_ENV)
    if mount:
        try:
            import modal  # type: ignore

            v = modal.Volume.from_name(vol)
        except Exception:  # noqa: BLE001  no Modal client in here: the folder still reads and writes
            v = None
        return MountedVolumeStore(os.path.join(mount, folder) if folder else mount, v)
    return ModalVolumeStore(vol, env, folder)
