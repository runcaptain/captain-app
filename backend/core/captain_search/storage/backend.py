"""Object storage with the two guarantees the data model needs.

1. put_new: create-only. A second write to the same key fails, so versions are write-once.
2. put_if: compare-and-swap on a tag, for the few mutable pointers (aliases, current deploy).

Local paths (and file://) work for tests and solo use. S3 uses conditional PUT
(If-None-Match / If-Match). GCS uses generation preconditions. Azure uses ETag conditions.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from abc import ABC, abstractmethod
from urllib.parse import urlparse


class StorageError(Exception):
    pass


class AlreadyExists(StorageError):
    pass


class NotFound(StorageError):
    pass


class Conflict(StorageError):
    """The object changed since it was read."""


def _told(fn):
    """Wrap a store's write so the change feed is told after it succeeds (changes.py). The key is the first
    argument of every write."""
    import functools

    @functools.wraps(fn)
    def write(self, key, *a, **k):
        out = fn(self, key, *a, **k)
        try:
            from .. import changes

            changes.wrote(self, key)
        except Exception:  # noqa: BLE001  a feed that can't be told never fails a write
            pass
        return out

    write._told = True
    return write


class Store(ABC):
    uri: str
    WRITES = ("put_new", "put_if", "put_new_chunks", "delete", "remove")

    def __init_subclass__(cls, **kw):
        # Every store's writes tell the change feed, here, so no caller and no new store can forget.
        super().__init_subclass__(**kw)
        for name in cls.WRITES:
            fn = cls.__dict__.get(name)
            if callable(fn) and not getattr(fn, "_told", False):
                setattr(cls, name, _told(fn))

    @abstractmethod
    def put_new(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get_with_tag(self, key: str) -> tuple[bytes, str]: ...

    @abstractmethod
    def put_if(self, key: str, data: bytes, tag: str | None) -> str:
        """Write when the current tag equals `tag`. tag=None means the key must not exist."""

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]:
        """All keys under prefix, recursive, relative to the store root, sorted."""

    def list_tags(self, prefix: str = "", shallow: bool = False) -> dict[str, str]:
        """{key: tag} for what is under prefix, from one listing where the store's listing carries tags (the
        buckets' do), so the change feed can see what another machine wrote without reading any object. `shallow`
        lists one level only: the files there with their tags, and each folder as "name/" with an empty tag.
        This default knows no tags: it sees keys come and go, not a key replaced in place."""
        keys = self.list(prefix)
        if not shallow:
            return {k: "" for k in keys}
        base = prefix.lstrip("/")
        out: dict[str, str] = {}
        for k in keys:
            rest = k[len(base):]
            out[k if "/" not in rest else base + rest.split("/")[0] + "/"] = ""
        return out

    def get(self, key: str) -> bytes:
        return self.get_with_tag(key)[0]

    def put_new_chunks(self, key: str, chunks) -> int:
        """Create-only, like put_new, for data too large for one request. `chunks` is an iterable of bytes.
        Returns the bytes written. Stores without a multipart path join the chunks and call put_new."""
        data = b"".join(chunks)
        self.put_new(key, data)
        return len(data)

    def exists(self, key: str) -> bool:
        try:
            self.get_with_tag(key)
            return True
        except NotFound:
            return False


# ---- local -----------------------------------------------------------------------------


class LocalStore(Store):
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.uri = "file://" + self.root
        os.makedirs(self.root, exist_ok=True)

    def _p(self, key: str) -> str:
        key = key.lstrip("/")
        p = os.path.abspath(os.path.join(self.root, key))
        if not (p == self.root or p.startswith(self.root + os.sep)):
            raise StorageError(f"key escapes store root: {key}")
        return p

    def put_new(self, key: str, data: bytes) -> None:
        dest = self._p(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            try:
                os.link(tmp, dest)  # atomic, fails if dest exists
            except FileExistsError:
                raise AlreadyExists(key) from None
        finally:
            os.unlink(tmp)

    def put_new_chunks(self, key: str, chunks) -> int:
        dest = self._p(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".tmp-")
        n = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                for c in chunks:
                    fh.write(c)
                    n += len(c)
            try:
                os.link(tmp, dest)
            except FileExistsError:
                raise AlreadyExists(key) from None
        finally:
            os.unlink(tmp)
        return n

    def get_with_tag(self, key: str) -> tuple[bytes, str]:
        try:
            with open(self._p(key), "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            raise NotFound(key) from None
        return data, hashlib.sha256(data).hexdigest()

    def put_if(self, key: str, data: bytes, tag: str | None) -> str:
        dest = self._p(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                _, cur = self.get_with_tag(key)
            except NotFound:
                cur = None
            if cur != tag:
                raise Conflict(key)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".tmp-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
        return hashlib.sha256(data).hexdigest()

    def list(self, prefix: str = "") -> list[str]:
        base = self._p(prefix) if prefix else self.root
        out = []
        if os.path.isfile(base):
            return [prefix.lstrip("/")]
        for d, _, files in os.walk(base):
            for f in files:
                if f.startswith(".tmp-") or f.endswith(".lock"):
                    continue
                out.append(os.path.relpath(os.path.join(d, f), self.root))
        return sorted(out)


# ---- S3 --------------------------------------------------------------------------------


class S3Store(Store):
    def __init__(self, bucket: str, prefix: str = "", client=None):
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.s3 = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.uri = f"s3://{bucket}/{self.prefix}".rstrip("/")

    def _k(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    @staticmethod
    def _code(e) -> str:
        return e.response.get("Error", {}).get("Code", "")

    def put_new(self, key: str, data: bytes) -> None:
        from botocore.exceptions import ClientError

        try:
            self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, IfNoneMatch="*")
        except ClientError as e:
            if self._code(e) in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
                raise AlreadyExists(key) from None
            raise

    PART = 16 << 20  # S3 parts must be at least 5 MiB, except the last

    def put_new_chunks(self, key: str, chunks) -> int:
        """A multipart upload whose completion is create-only (If-None-Match on CompleteMultipartUpload), so
        a second writer fails the same way put_new does. A failed upload is aborted, leaving nothing behind."""
        from botocore.exceptions import ClientError

        k = self._k(key)
        up = self.s3.create_multipart_upload(Bucket=self.bucket, Key=k)["UploadId"]
        parts, buf, n = [], bytearray(), 0
        try:
            def send(data: bytes):
                r = self.s3.upload_part(Bucket=self.bucket, Key=k, UploadId=up, PartNumber=len(parts) + 1, Body=data)
                parts.append({"PartNumber": len(parts) + 1, "ETag": r["ETag"]})

            for c in chunks:
                buf += c
                n += len(c)
                while len(buf) >= self.PART:
                    send(bytes(buf[:self.PART]))
                    del buf[:self.PART]
            if buf or not parts:
                send(bytes(buf))
            try:
                self.s3.complete_multipart_upload(Bucket=self.bucket, Key=k, UploadId=up,
                                                  MultipartUpload={"Parts": parts}, IfNoneMatch="*")
            except ClientError as e:
                if self._code(e) in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
                    raise AlreadyExists(key) from None
                raise
        except BaseException:
            try:
                self.s3.abort_multipart_upload(Bucket=self.bucket, Key=k, UploadId=up)
            except Exception:  # noqa: BLE001  the upload may already be gone
                pass
            raise
        return n

    def get_with_tag(self, key: str) -> tuple[bytes, str]:
        from botocore.exceptions import ClientError

        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=self._k(key))
        except ClientError as e:
            if self._code(e) in ("NoSuchKey", "404"):
                raise NotFound(key) from None
            raise
        return r["Body"].read(), r["ETag"]

    def put_if(self, key: str, data: bytes, tag: str | None) -> str:
        from botocore.exceptions import ClientError

        kw = {"IfNoneMatch": "*"} if tag is None else {"IfMatch": tag}
        try:
            r = self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, **kw)
        except ClientError as e:
            if self._code(e) in ("PreconditionFailed", "ConditionalRequestConflict", "412", "NoSuchKey"):
                raise Conflict(key) from None
            raise
        return r["ETag"]

    def list(self, prefix: str = "") -> list[str]:
        out = []
        pfx = self._k(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        strip = len(self.prefix) + 1 if self.prefix else 0
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=pfx):
            out += [o["Key"][strip:] for o in page.get("Contents", [])]
        return sorted(out)

    def list_tags(self, prefix: str = "", shallow: bool = False) -> dict[str, str]:
        out: dict[str, str] = {}
        pfx = self._k(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        strip = len(self.prefix) + 1 if self.prefix else 0
        kw = {"Delimiter": "/"} if shallow else {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=pfx, **kw):
            for o in page.get("Contents", []):
                out[o["Key"][strip:]] = str(o.get("ETag") or "")
            for d in page.get("CommonPrefixes", []):
                out[d["Prefix"][strip:]] = ""
        return out


# ---- GCS ---------------------------------------------------------------------------------------
# Run against a real bucket on 2026-11-10 (docs/compute-live-testing.md): create-only writes and compare-and-swap
# held under 8 concurrent writers, and a workspace, the lineage index and a model registry prefix all worked.
# No emulator in CI yet, so nothing here runs in the unit tests.


class GCSStore(Store):
    def __init__(self, bucket: str, prefix: str = "", credentials=None, project=None):
        from google.cloud import storage  # type: ignore

        self.b = storage.Client(project=project, credentials=credentials).bucket(bucket)
        self.prefix = prefix.strip("/")
        self.uri = f"gs://{bucket}/{self.prefix}".rstrip("/")

    def _k(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_new(self, key: str, data: bytes) -> None:
        from google.api_core.exceptions import PreconditionFailed  # type: ignore

        try:
            self.b.blob(self._k(key)).upload_from_string(data, if_generation_match=0)
        except PreconditionFailed:
            raise AlreadyExists(key) from None

    def get_with_tag(self, key: str) -> tuple[bytes, str]:
        blob = self.b.get_blob(self._k(key))
        if blob is None:
            raise NotFound(key)
        return blob.download_as_bytes(if_generation_match=blob.generation), str(blob.generation)

    def put_if(self, key: str, data: bytes, tag: str | None) -> str:
        from google.api_core.exceptions import PreconditionFailed  # type: ignore

        blob = self.b.blob(self._k(key))
        try:
            blob.upload_from_string(data, if_generation_match=0 if tag is None else int(tag))
        except PreconditionFailed:
            raise Conflict(key) from None
        return str(blob.generation)

    def list(self, prefix: str = "") -> list[str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        pfx = self._k(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        return sorted(b.name[strip:] for b in self.b.client.list_blobs(self.b, prefix=pfx))

    def list_tags(self, prefix: str = "", shallow: bool = False) -> dict[str, str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        pfx = self._k(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        it = self.b.client.list_blobs(self.b, prefix=pfx, **({"delimiter": "/"} if shallow else {}))
        out = {b.name[strip:]: str(getattr(b, "etag", None) or getattr(b, "generation", "") or "") for b in it}
        for d in getattr(it, "prefixes", None) or []:   # filled once the listing has been read through
            out[d[strip:]] = ""
        return out


# ---- Azure Blob --------------------------------------------------------------------------
# Run against a real storage account on 2026-11-10 (docs/compute-live-testing.md). CI has no emulator: the unit
# test uses a fake with the SDK's shapes.


class AzureStore(Store):
    def __init__(self, account: str, container: str, prefix: str = "", credential=None, connection_string=None):
        from azure.storage.blob import ContainerClient  # type: ignore

        if connection_string:
            self.c = ContainerClient.from_connection_string(connection_string, container)
        else:
            if credential is None:
                from azure.identity import DefaultAzureCredential  # type: ignore

                credential = DefaultAzureCredential()
            self.c = ContainerClient(f"https://{account}.blob.core.windows.net", container, credential)
        self.prefix = prefix.strip("/")
        self.uri = f"az://{account}/{container}/{self.prefix}".rstrip("/")

    def _k(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_new(self, key: str, data: bytes) -> None:
        from azure.core.exceptions import ResourceExistsError  # type: ignore

        try:
            self.c.upload_blob(self._k(key), data, overwrite=False)
        except ResourceExistsError:
            raise AlreadyExists(key) from None

    def get_with_tag(self, key: str) -> tuple[bytes, str]:
        from azure.core.exceptions import ResourceNotFoundError  # type: ignore

        try:
            d = self.c.download_blob(self._k(key))
        except ResourceNotFoundError:
            raise NotFound(key) from None
        return d.readall(), d.properties.etag

    def put_if(self, key: str, data: bytes, tag: str | None) -> str:
        from azure.core import MatchConditions  # type: ignore
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError  # type: ignore

        # The blob client, not ContainerClient.upload_blob: that one returns a BlobClient, which has no ETag to
        # hand back. BlobClient.upload_blob returns the response headers, with the new ETag.
        blob = self.c.get_blob_client(self._k(key))
        try:
            if tag is None:
                r = blob.upload_blob(data, overwrite=False)
            else:
                r = blob.upload_blob(data, overwrite=True, etag=tag, match_condition=MatchConditions.IfNotModified)
        except (ResourceExistsError, ResourceModifiedError):
            raise Conflict(key) from None
        return r["etag"]

    def list(self, prefix: str = "") -> list[str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        pfx = self._k(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        return sorted(b.name[strip:] for b in self.c.list_blobs(name_starts_with=pfx))

    def list_tags(self, prefix: str = "", shallow: bool = False) -> dict[str, str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        pfx = self._k(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        walk = getattr(self.c, "walk_blobs", None)
        if not shallow or not callable(walk):
            return super().list_tags(prefix, shallow) if shallow else {
                b.name[strip:]: str(getattr(b, "etag", "") or "") for b in self.c.list_blobs(name_starts_with=pfx)}
        # walk_blobs gives the files of one level, and each folder as an item whose name ends in "/"
        return {b.name[strip:]: ("" if b.name.endswith("/") else str(getattr(b, "etag", "") or "")) for b in walk(name_starts_with=pfx, delimiter="/")}


def open_store(uri: str, profile: str | None = None) -> Store:
    """profile names a cloud profile (see cloud.py). None uses the provider's default credential chain."""
    u = urlparse(uri)
    if u.scheme in ("", "file"):
        return LocalStore(u.path if u.scheme == "file" else uri)
    if u.scheme == "s3":
        from ..cloud import aws_session

        return S3Store(u.netloc, u.path, client=aws_session(profile).client("s3"))
    if u.scheme in ("gs", "gcs"):
        if profile:
            from ..cloud import gcp_credentials

            creds, project = gcp_credentials(profile)
            return GCSStore(u.netloc, u.path, creds, project)
        return GCSStore(u.netloc, u.path)
    if u.scheme in ("az", "azure"):
        container, _, prefix = u.path.lstrip("/").partition("/")
        if profile:
            from ..cloud import azure_credential

            cred, conn, _ = azure_credential(profile)
            return AzureStore(u.netloc, container, prefix, credential=cred, connection_string=conn)
        return AzureStore(u.netloc, container, prefix)
    if u.scheme == "modal":
        from .modal_volume import open_modal

        return open_modal(uri)
    raise StorageError(f"unsupported storage URI: {uri}")
