"""Snapshot tables into write-once DatasetVersions with provenance."""
from __future__ import annotations

import io
import os

import pyarrow as pa
import pyarrow.parquet as pq

from ..model.entities import DatasetKind, DatasetVersion, FileEntry, Provenance
from ..workspace import Context, whoami


def to_parquet(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def from_parquet(data: bytes) -> pa.Table:
    return pq.read_table(io.BytesIO(data))


def read_file(path: str) -> pa.Table:
    import duckdb

    ext = os.path.splitext(path)[1].lower()
    fn = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".parquet": "read_parquet",
          ".jsonl": "read_json_auto", ".ndjson": "read_json_auto", ".json": "read_json_auto"}.get(ext)
    if fn is None:
        raise ValueError(f"unsupported file type {ext}: use csv, tsv, parquet, json or jsonl")
    return duckdb.connect().sql(f"select * from {fn}($p)", params={"p": path}).to_arrow_table()


def snapshot(ctx: Context, name: str, table: pa.Table, *, kind: DatasetKind, provenance: Provenance,
             description: str = "", truncated: bool = False, dry_run: bool = False) -> DatasetVersion:
    data = to_parquet(table)
    entry_path = "data/part-0.parquet"
    m = DatasetVersion(
        org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(),
        name=name, version=0, dataset_kind=kind, description=description,
        schema_fields=[{"name": f.name, "type": str(f.type)} for f in table.schema],
        row_count=table.num_rows, truncated=truncated, provenance=provenance,
    )
    if dry_run:
        from ..storage.registry import sha256

        return m.model_copy(update={"files": [FileEntry(path=entry_path, sha256=sha256(data), bytes=len(data))]})
    entry = ctx.registry.put_blob(data, entry_path)
    m = m.model_copy(update={"files": [entry]})
    return ctx.registry.publish(m)


def load_table(ctx: Context, m: DatasetVersion) -> pa.Table:
    parts = [from_parquet(ctx.registry.get_blob(f)) for f in m.files if f.path.endswith(".parquet")]
    return pa.concat_tables(parts) if parts else pa.table({})
