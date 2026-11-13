"""Routes for reading a trained session model (real.py) and its tree rankers (trees.py). Shapes are in
docs/weights.md. They are added to the one weights router, so they are served under /api/weights like the rest."""
from __future__ import annotations

from fastapi import Query

from . import real as R
from .routes import _ctx, router


@router.get("/sessions/{sid}/nearest")
def nearest(sid: str, tensor: str, row: int = 0, top: int = 10, by: str | None = None, watch: bool = False):
    return R.nearest(_ctx(), sid, tensor, row, top, by, watch)


@router.get("/sessions/{sid}/directions")
def directions(sid: str, tensor: str, top: int = 5, by: str | None = None, watch: bool = False):
    return R.directions(_ctx(), sid, tensor, top, by, watch)


@router.get("/sessions/{sid}/attention")
def attention(sid: str, layer: int = 0, head: int | None = None, heads: int | None = None, matrix: str | None = None,
              by: str | None = None, watch: bool = False):
    return R.attention(_ctx(), sid, layer, head, heads, matrix, by, watch)


@router.get("/models/{model}/trees")
def trees(model: str, ranker: str | None = None, trees: int = 3):
    return R.trees(_ctx(), model, ranker, trees)


@router.get("/sessions/{sid}/labels")
def labels(sid: str, tensor: str):
    return R.labels(_ctx(), sid, tensor)


@router.get("/models/{model}/rankers")
def rankers(model: str):
    return R.rankers(_ctx(), model)


@router.get("/registry")
def registry(where: str = Query(..., alias="from")):
    return R.registry(where)
