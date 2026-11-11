from captain_search.storage.backend import LocalStore


def test_browse_reads_a_blob_from_a_store_duckdb_cannot_reach(tmp_path, monkeypatch):
    """A workspace on Azure Blob or Google Cloud Storage: the blob is copied to a local cache once and read there.
    It used to stop with "browsing blobs in GCS or Azure workspaces is not supported yet"."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    from captain_search import browse
    from captain_search.storage.registry import Registry

    monkeypatch.setenv("CST_HOME", str(tmp_path / "home"))
    inner = LocalStore(str(tmp_path / "bucket"))
    gets = []

    class Remote:  # neither a local root nor an S3 bucket, like AzureStore and GCSStore
        def get(self, key):
            gets.append(key)
            return inner.get(key)

    buf = io.BytesIO()
    pq.write_table(pa.table({"id": [1, 2, 3]}), buf)
    entry = Registry(inner).put_blob(buf.getvalue(), "data/part-0.parquet")
    ctx = type("Ctx", (), {"store": Remote(), "registry": Registry(inner)})()
    path = browse._blob_uri(ctx, entry.sha256)
    assert path.startswith(str(tmp_path / "home")) and pq.read_table(path).num_rows == 3
    assert browse._blob_uri(ctx, entry.sha256) == path and len(gets) == 1  # the second read is the cached file
