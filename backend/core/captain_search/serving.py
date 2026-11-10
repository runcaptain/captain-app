"""`cst pipeline serve`: one pipeline version behind HTTP. This is what the cloud serving targets run."""
from __future__ import annotations

from .engines import artifact_reader, make_engine, timed_search


def serve_pipeline(ctx, ref: str, host: str = "127.0.0.1", port: int = 8080):
    import uvicorn

    uvicorn.run(make_app(ctx, ref), host=host, port=port, log_level="warning")


def make_app(ctx, ref: str):
    from fastapi import FastAPI

    from . import ops

    pm, _ = ctx.registry.resolve("pipeline", ref)
    prods = ops.products_of(ctx, pm.catalog) if pm.catalog else None
    eng = make_engine(pm.engine, pm.spec, prods, artifact=artifact_reader(ctx, pm))
    fp = eng.fingerprint()
    if fp and pm.index_fingerprint and (fp.doc_count, fp.sample_hash) != (pm.index_fingerprint.doc_count, pm.index_fingerprint.sample_hash):
        raise SystemExit(f"the index behind {ops.ref(pm)} changed since it was published; refusing to serve it")
    api = FastAPI(title=f"Captain pipeline {ops.ref(pm)}")

    @api.get("/healthz")
    def health():
        return {"ok": True, "pipeline": ops.ref(pm)}

    @api.post("/search")
    def search(body: dict):
        ids, ms = timed_search(eng, str(body["query"]), int(body.get("k", 20)))
        return {"ids": ids, "ms": round(ms, 3), "pipeline": ops.ref(pm)}

    # SageMaker's contract for a serving container: GET /ping answers 200 when ready, POST /invocations does the work.
    @api.get("/ping")
    def ping():
        return {"ok": True}

    @api.post("/invocations")
    def invocations(body: dict):
        return search(body)

    return api
