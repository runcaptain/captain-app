"""Register data by reference: a DatasetVersion whose files stay where they are (S3, GCS, Azure or a
Modal volume). Only the listing, sizes and checksums are recorded. `head` reads the first bytes of a
JSONL/CSV object with a range request, so a preview never downloads the whole file."""
from __future__ import annotations

import io
import json
from urllib.parse import urlparse

from .model.entities import DatasetKind, DatasetVersion, ExternalFile, Provenance
from .storage.registry import check_name
from .workspace import Context, whoami


def list_s3(uri: str, profile: str | None = None, s3=None) -> list[ExternalFile]:
    from .cloud import aws_session

    u = urlparse(uri)
    s3 = s3 or aws_session(profile, "us-west-1").client("s3")
    pfx = u.path.lstrip("/")
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=u.netloc, Prefix=pfx):
        for o in page.get("Contents", []):
            out.append(ExternalFile(uri=f"s3://{u.netloc}/{o['Key']}", bytes=o["Size"], etag=o["ETag"].strip('"')))
    return out


def register(ctx: Context, name: str, uris: list[str], kind: DatasetKind, description: str = "",
             row_count: int | None = None, schema: list[dict] | None = None, sha256s: dict[str, str] | None = None,
             tags: dict | None = None, provenance: Provenance | None = None, profile: str | None = None, s3=None,
             extra_files: list[ExternalFile] | None = None) -> DatasetVersion:
    files: list[ExternalFile] = list(extra_files or [])
    for uri in uris:
        if uri.startswith("s3://"):
            got = list_s3(uri, profile, s3)
            if not got:
                raise ValueError(f"nothing found at {uri}")
            files += got
        elif uri.startswith("modal://"):
            files.append(ExternalFile(uri=uri, mutable=True))
        else:
            files.append(ExternalFile(uri=uri))
    for f in files:
        key = f.uri.rsplit("/", 1)[-1]
        if sha256s and (f.uri in sha256s or key in sha256s):
            f.sha256 = sha256s.get(f.uri) or sha256s.get(key)
    m = DatasetVersion(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=check_name(name),
                       version=0, dataset_kind=kind, description=description, external=files,
                       row_count=row_count or 0, schema_fields=schema or [],
                       tags={"storage": "external", **(tags or {})},
                       provenance=provenance or Provenance(source="import", params={"uris": uris}))
    return ctx.registry.publish(m)


def head(m: DatasetVersion, n: int = 20, profile: str | None = None, s3=None) -> list[dict]:
    """Preview the first rows of the first JSONL/CSV object, reading at most 2 MB."""
    from .cloud import aws_session

    s3files = [f for f in m.external if f.uri.startswith("s3://")]
    f = next((f for ext in (".jsonl", ".ndjson", ".csv", ".json") for f in s3files if f.uri.endswith(ext)), None)
    if f is None:
        raise ValueError("this dataset is stored by reference in a format that can't be previewed without downloading it")
    u = urlparse(f.uri)
    s3 = s3 or aws_session(profile, "us-west-1").client("s3")
    body = s3.get_object(Bucket=u.netloc, Key=u.path.lstrip("/"), Range="bytes=0-2097151")["Body"].read().decode("utf-8", "replace")
    lines = body.splitlines()[:-1] if len(body) >= 2097152 else body.splitlines()
    if f.uri.endswith(".csv"):
        import csv

        return list(csv.DictReader(io.StringIO("\n".join(lines))))[:n]
    if f.uri.endswith(".json"):
        try:
            j = json.loads(body)
            return (j if isinstance(j, list) else [j])[:n]
        except json.JSONDecodeError:
            raise ValueError("the JSON file is larger than the 2 MB preview window") from None
    return [json.loads(x) for x in lines[:n] if x.strip()]
