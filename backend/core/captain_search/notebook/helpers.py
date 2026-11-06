"""Runs inside the notebook kernel. `__cst.sql(...)` and `__cst.chart(...)` are what SQL and chart
cells compile to."""
from __future__ import annotations

import json
import math

TABLE_MIME = "application/vnd.captain.table+json"
VEGA_MIME = "application/vnd.vegalite.v5+json"
PREVIEW = 1000

_ctx = None


def _context():
    global _ctx
    if _ctx is None:
        from captain_search.workspace import open_context

        _ctx = open_context()
    return _ctx


def _cell(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def table_bundle(df, var: str | None = None) -> dict:
    head = df.head(PREVIEW)
    return {"var": var, "columns": [{"name": str(c), "type": str(t)} for c, t in zip(df.columns, df.dtypes)],
            "rows": [[_cell(v) for v in row] for row in head.itertuples(index=False, name=None)],
            "row_count": int(len(df)), "truncated": len(df) > PREVIEW}


def sql(conn_name: str, text: str, var: str | None = None, limit: int | None = None, local: bool = False):
    from IPython.display import display

    from captain_search.connectors.sql import run_sql

    ctx = _context()
    conn = ctx.load_connection(conn_name)
    r = run_sql(conn, text, limit=limit, allow_large=local)
    df = r.table.to_pandas()
    b = table_bundle(df, var)
    b["ms"] = r.elapsed_ms
    display({TABLE_MIME: b, "text/plain": f"{var}: {len(df)} rows"}, raw=True)
    return df


def chart(df, spec: dict):
    """spec: {mark: bar|line|point|area, x, y, color?} or a full Vega-Lite spec under `vega`."""
    from IPython.display import display

    if df is None:
        raise ValueError("a chart cell needs a DataFrame name, for example `zero`")
    if isinstance(spec, str):
        spec = json.loads(spec)
    data = [{k: _cell(v) for k, v in r.items()} for r in df.head(5000).to_dict(orient="records")]
    if spec.get("vega"):
        vl = dict(spec["vega"])
    else:
        x, y = spec.get("x") or df.columns[0], spec.get("y") or df.columns[-1]
        xt = "quantitative" if str(df[x].dtype).startswith(("int", "float")) and spec.get("mark") != "bar" else "nominal"
        enc = {"x": {"field": x, "type": xt, "sort": "-y" if spec.get("mark", "bar") == "bar" else None},
               "y": {"field": y, "type": "quantitative"}}
        if spec.get("color"):
            enc["color"] = {"field": spec["color"], "type": "nominal"}
        vl = {"mark": {"type": spec.get("mark", "bar")}, "encoding": enc}
    vl["$schema"] = "https://vega.github.io/schema/vega-lite/v5.json"
    vl["data"] = {"values": data}
    vl.setdefault("width", "container")
    display({VEGA_MIME: vl, "text/plain": "chart"}, raw=True)


def install():
    """Show DataFrames as Captain tables."""
    try:
        import pandas as pd
        from IPython import get_ipython

        ip = get_ipython()
        if ip is None:
            return
        ip.display_formatter.mimebundle_formatter.for_type(pd.DataFrame, lambda df, include=None, exclude=None: {TABLE_MIME: table_bundle(df)})
    except Exception:
        pass
