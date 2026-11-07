"""The first pages of a JSONL or CSV file on S3 come from a ranged read of its start (a piece that grows until it
holds the rows asked for), read by DuckDB on this computer, with the same columns, types and cells as reading the
file itself. AWS is faked; the piece is read locally, so nothing here goes through httpfs."""
import json

import boto3
import pytest
from moto import mock_aws

from captain_search import browse as B
from captain_search.data.cells import cell
from captain_search.model.entities import DatasetKind, DatasetVersion, ExternalFile, Provenance
from captain_search.workspace import init_workspace


@pytest.fixture
def ctx(tmp_path):
    return init_workspace("t", str(tmp_path / "store"))


def _ds(ctx, uri, size):
    return DatasetVersion(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by="t", name="cat", version=1,
                          dataset_kind=DatasetKind.catalog, external=[ExternalFile(uri=uri, bytes=size)],
                          provenance=Provenance(source="import"))


@mock_aws
@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
def test_first_pages_from_the_start_of_the_file(ctx, monkeypatch, tmp_path, fmt):
    rows = [{"pid": f"p{i:04d}", "title": f"Product {i}", "price": round(1 + i * 0.5, 2), "tags": ["a", str(i)]}
            for i in range(600)]
    if fmt == "jsonl":
        body = "\n".join(json.dumps(r) for r in rows).encode()
    else:
        body = ("pid,title,price\n" + "\n".join(f"{r['pid']},{r['title']},{r['price']}" for r in rows) + "\n").encode()
    s3 = boto3.client("s3", region_name="us-west-1")
    s3.create_bucket(Bucket="cat-bucket", CreateBucketConfiguration={"LocationConstraint": "us-west-1"})
    s3.put_object(Bucket="cat-bucket", Key=f"items.{fmt}", Body=body)
    monkeypatch.setattr(B, "HEAD_START", 512)  # a small first piece, so it has to grow
    B._schemas.clear()
    m = _ds(ctx, f"s3://cat-bucket/items.{fmt}", len(body))
    first = B.rows(ctx, m, None, 0, 100)
    assert [r[0] for r in first["rows"]] == [f"p{i:04d}" for i in range(100)] and first["has_more"]
    # The same as DuckDB reading the whole file.
    local = tmp_path / f"all.{fmt}"
    local.write_bytes(body)
    rel = B._con(None).sql(f"select * from {B._reader(str(local), fmt)} limit 100")
    assert [c["name"] for c in first["columns"]] == rel.columns
    assert [c["type"] for c in first["columns"]] == [str(t) for t in rel.types]
    assert first["rows"] == [[cell(v) for v in r] for r in rel.fetchall()]
    later = B.rows(ctx, m, None, 500, 100)
    assert [r[0] for r in later["rows"]] == [f"p{i:04d}" for i in range(500, 600)] and not later["has_more"]
