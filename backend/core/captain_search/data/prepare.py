"""Turn a warehouse table, a query or a dataset into clean, training-ready data in one step.

Copying a table as it is leaves the work for later: columns named however the warehouse named them, duplicates, rows
missing their id, numbers stored as text. Here a person says what the data is, its role, and Captain:
1. maps the source's columns to the columns that role needs, guessing from common names, with every guess shown and
   editable;
2. cleans it: drops rows missing a required column, removes duplicates on the role's key, casts types, trims text;
3. shows a preview of the cleaned rows with a report of every change, and nothing is saved;
4. on import, does the same to the whole table and saves a versioned dataset of that kind in the role's folder. The
   mapping and cleaning steps are recorded in its provenance, so the import can be repeated or audited.

Roles and what they need:
  catalog    id, title (+ description, brand, category, price, image_url, url)
  queries    query (+ query_id, class, weight)
  judgments  query_id, product_id, grade 0 to 3 (+ query)
  events     timestamp, event (+ session_id, user_id, query, product_id, position, value)
  generic    nothing required (cleaning only)
"""
from __future__ import annotations

import re
import time

import pyarrow as pa

ROLES = {
    "catalog": {"label": "Catalog (products or documents)", "kind": "catalog", "folder": "catalogs", "key": ["id"],
                "required": ["id", "title"],
                "optional": ["description", "brand", "category", "price", "image_url", "url"],
                "types": {"id": "VARCHAR", "title": "VARCHAR", "description": "VARCHAR", "brand": "VARCHAR",
                          "category": "VARCHAR", "price": "DOUBLE", "image_url": "VARCHAR", "url": "VARCHAR"}},
    "queries": {"label": "Search queries", "kind": "queries", "folder": "queries", "key": ["query_id"],
                "required": ["query"], "optional": ["query_id", "class", "weight"],
                "types": {"query": "VARCHAR", "query_id": "VARCHAR", "class": "VARCHAR", "weight": "DOUBLE"}},
    "judgments": {"label": "Relevance labels", "kind": "judgments", "folder": "judgments",
                  "key": ["query_id", "product_id"], "required": ["query_id", "product_id", "grade"],
                  "optional": ["query"],
                  "types": {"query_id": "VARCHAR", "product_id": "VARCHAR", "grade": "INTEGER", "query": "VARCHAR"}},
    "events": {"label": "Events or search logs", "kind": "events", "folder": "logs", "key": [],
               "required": ["timestamp", "event"],
               "optional": ["session_id", "user_id", "query", "product_id", "position", "value"],
               "types": {"timestamp": "TIMESTAMP", "event": "VARCHAR", "session_id": "VARCHAR", "user_id": "VARCHAR",
                         "query": "VARCHAR", "product_id": "VARCHAR", "position": "INTEGER", "value": "DOUBLE"}},
    "generic": {"label": "Something else", "kind": "generic", "folder": "imports", "key": [], "required": [],
                "optional": [], "types": {}},
}

SYNONYMS = {
    "id": ["id", "product_id", "item_id", "sku", "objectid", "doc_id", "document_id", "listing_id", "supplierid", "productid"],
    "title": ["title", "name", "product_name", "item_name", "product_title", "headline"],
    "description": ["description", "desc", "body", "details", "summary"],
    "brand": ["brand", "brand_name", "manufacturer", "vendor", "supplier"],
    "category": ["category", "category_name", "department", "product_type", "type", "category_path"],
    "price": ["price", "unit_price", "sale_price", "amount", "list_price"],
    "image_url": ["image_url", "image", "img", "thumbnail", "image_link"],
    "url": ["url", "link", "product_url", "permalink"],
    "query": ["query", "search_term", "query_text", "q", "keyword", "searchterm", "search_query"],
    "query_id": ["query_id", "qid", "search_id"],
    "class": ["class", "segment", "query_class", "intent"],
    "weight": ["weight", "searches", "count", "frequency", "n"],
    "product_id": ["product_id", "item_id", "doc_id", "sku", "id", "productid"],
    "grade": ["grade", "label", "relevance", "rating", "score", "judgment"],
    "timestamp": ["timestamp", "ts", "event_time", "time", "created_at", "occurred_at", "datetime", "date", "event_timestamp"],
    "event": ["event", "event_name", "event_type", "type", "action"],
    "session_id": ["session_id", "session", "sid", "visit_id"],
    "user_id": ["user_id", "distinct_id", "customer_id", "uid", "anonymous_id"],
    "position": ["position", "rank", "pos"],
    "value": ["value", "revenue", "amount", "price"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def auto_map(columns: list[str], role: str) -> dict:
    """{target: source column or None}. Exact-name matches first, then synonyms, never the same source twice."""
    spec = ROLES[role]
    by_norm = {_norm(c): c for c in columns}
    used, out = set(), {}
    for target in spec["required"] + spec["optional"]:
        hit = None
        for cand in [target] + SYNONYMS.get(target, []):
            c = by_norm.get(_norm(cand))
            if c and c not in used:
                hit = c
                break
        out[target] = hit
        if hit:
            used.add(hit)
    return out


def guess_role(columns: list[str]) -> str:
    """The role whose required columns this table can fill best (ties go to the more specific role)."""
    best, score = "generic", 0.0
    for role in ("judgments", "events", "catalog", "queries"):
        m = auto_map(columns, role)
        req = ROLES[role]["required"]
        s = sum(1 for t in req if m[t]) / len(req)
        if s > score + 1e-9:
            best, score = role, s
    return best if score >= 1.0 else "generic"


def _q(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def _clean_sql(columns: list[str], role: str, mapping: dict, opts: dict) -> tuple[str, list[str]]:
    """DuckDB SQL over table `src` that maps, casts, trims and filters. Returns (sql, steps in words)."""
    spec = ROLES[role]
    steps, select, keep_rest = [], [], opts.get("keep_other_columns", True)
    mapped_sources = {v for v in mapping.values() if v}
    for target in spec["required"] + spec["optional"]:
        src = mapping.get(target)
        if not src:
            continue
        typ = spec["types"].get(target, "VARCHAR")
        expr = _q(src)
        if typ == "VARCHAR" and opts.get("trim", True):
            expr = f"nullif(trim(cast({expr} AS VARCHAR)), '')"
        elif typ == "VARCHAR":
            expr = f"cast({expr} AS VARCHAR)"
        else:
            expr = f"try_cast({expr} AS {typ})"
        select.append(f"{expr} AS {_q(target)}")
        if src != target:
            steps.append(f"{src} becomes {target}")
    if keep_rest:
        select += [_q(c) for c in columns if c not in mapped_sources and c not in spec["types"]]
    if not select:
        select = [_q(c) for c in columns]
    select.append("_ord")  # each row's place in the source, so cleaning keeps the order
    where = [f"{_q(t)} IS NOT NULL" for t in spec["required"] if mapping.get(t)]
    if role == "judgments" and mapping.get("grade"):
        where.append('"grade" BETWEEN 0 AND 3')
    sql = f"SELECT {', '.join(select)} FROM (SELECT *, row_number() OVER () AS _ord FROM src) s"
    inner = f"({sql})"
    out = f"SELECT * FROM {inner} t" + (f" WHERE {' AND '.join(where)}" if where else "")
    if where:
        steps.append("rows missing " + ", ".join(t for t in spec["required"] if mapping.get(t)) + " are dropped")
    key = [k for k in spec["key"] if mapping.get(k)]
    if opts.get("dedupe", True):
        part = ", ".join(_q(k) for k in key) if key else "*"
        if key:
            out = (f"SELECT * EXCLUDE (_rn) FROM (SELECT *, row_number() OVER (PARTITION BY {part} ORDER BY _ord) AS _rn "
                   f"FROM ({out}) d) WHERE _rn = 1")
            steps.append(f"duplicate {', '.join(key)} keep their first row")
        else:
            names = [t for t in spec["required"] + spec["optional"] if mapping.get(t)]
            names += [c for c in columns if keep_rest and c not in mapped_sources and c not in spec["types"]] or \
                     ([] if names else list(columns))
            part = ", ".join(_q(c) for c in names)
            out = (f"SELECT * EXCLUDE (_rn) FROM (SELECT *, row_number() OVER (PARTITION BY {part} ORDER BY _ord) AS _rn "
                   f"FROM ({out}) d) WHERE _rn = 1")
            steps.append("exact duplicate rows are removed")
    return f"SELECT * EXCLUDE (_ord) FROM ({out}) o ORDER BY _ord", steps


def run(table: pa.Table, role: str | None = None, mapping: dict | None = None, opts: dict | None = None) -> dict:
    """Map and clean an Arrow table. Returns {role, mapping, missing, table, report}."""
    import duckdb

    cols = table.column_names
    role = role or guess_role(cols)
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}. Roles: {', '.join(ROLES)}")
    auto = auto_map(cols, role)
    m = {**auto, **{k: v for k, v in (mapping or {}).items() if k in auto}}
    bad = [v for v in m.values() if v and v not in cols]
    if bad:
        raise ValueError(f"no column named {', '.join(bad)} in the source. Its columns: {', '.join(cols)}")
    missing = [t for t in ROLES[role]["required"] if not m.get(t)]
    con = duckdb.connect()
    con.register("src", table)
    sql, steps = _clean_sql(cols, role, m, opts or {})
    t0 = time.time()
    out = con.sql(sql).to_arrow_table()
    rows_in, rows_out = table.num_rows, out.num_rows
    report = {"rows_in": rows_in, "rows_out": rows_out, "dropped": rows_in - rows_out, "steps": steps,
              "ms": int((time.time() - t0) * 1000), "problems": []}
    if missing:
        report["problems"].append(f"A {ROLES[role]['label'].lower()} needs {', '.join(missing)}. Pick which column holds "
                                  f"{'it' if len(missing) == 1 else 'each'}.")
    if rows_in and rows_out == 0:
        report["problems"].append("Cleaning removed every row. Check the column choices.")
    if rows_in and (rows_in - rows_out) / rows_in > 0.2:
        report["warnings"] = [f"Cleaning removed {(rows_in - rows_out) / rows_in:.0%} of the rows."]
    return {"role": role, "role_label": ROLES[role]["label"], "mapping": m, "missing": missing, "table": out,
            "report": report, "sql": sql}


def _source(ctx, src: dict, limit: int | None) -> tuple[pa.Table, dict, str]:
    """(table, provenance params, default name) from {conn, table: "schema.name"} | {conn, sql} | {dataset}."""
    from ..connectors.sql import run_sql

    if src.get("dataset"):
        from . import datasets as DS

        m = ctx.registry.resolve("dataset", src["dataset"])[0]
        t = DS.load_table(ctx, m)
        return (t.slice(0, limit) if limit else t), {"dataset": f"{m.name}@v{m.version}"}, m.name.split("/")[-1]
    conn = ctx.load_connection(src["conn"])
    if src.get("table"):
        from .. import metastore as MS

        schema, _, name = src["table"].rpartition(".")
        page = MS.table_rows(ctx, src["conn"], schema, name, 0, 1, None, False, None)  # quotes the name per database
        sql = page["sql"]
        default = name
    elif src.get("sql"):
        sql, default = src["sql"], "query-result"
    else:
        raise ValueError("say what to prepare: a table, a query, or a dataset")
    r = run_sql(conn, sql, limit=limit) if limit else run_sql(conn, sql)
    return r.table, {"connection": src["conn"], "sql": sql}, default


def preview(ctx, src: dict, role: str | None = None, mapping: dict | None = None, opts: dict | None = None,
            sample: int = 1000) -> dict:
    """The cleaned first rows and a report of every change, from a sample. Nothing is saved."""
    from .cells import page_from_arrow

    t, prov, default = _source(ctx, src, sample)
    out = run(t, role, mapping, opts)
    page = page_from_arrow(out["table"].slice(0, 101), offset=0, limit=100, ms=out["report"]["ms"])
    return {"role": out["role"], "role_label": out["role_label"], "mapping": out["mapping"], "missing": out["missing"],
            "columns": t.column_names, "report": {**out["report"], "sampled": t.num_rows >= sample},
            "roles": [{"role": k, "label": v["label"], "required": v["required"], "optional": v["optional"]}
                      for k, v in ROLES.items()],
            "suggested_name": _slug(default), "folder": ROLES[out["role"]]["folder"], "page": page}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "data"


def import_prepared(ctx, src: dict, role: str | None = None, mapping: dict | None = None, opts: dict | None = None,
                    name: str | None = None, folder: str | None = None, project: str | None = None,
                    progress=lambda f: None) -> dict:
    """Prepare the whole source the same way as the preview and save it as a versioned dataset of the role's kind."""
    from .. import library as LIB
    from ..model.entities import DatasetKind, Provenance
    from ..storage.registry import check_name
    from . import datasets as DS

    progress(0.05)
    t, prov, default = _source(ctx, src, None)
    progress(0.5)
    out = run(t, role, mapping, opts)
    if out["missing"]:
        raise ValueError(" ".join(out["report"]["problems"]))
    if out["table"].num_rows == 0:
        raise ValueError("Cleaning left no rows, so nothing was imported. Check the column choices.")
    spec = ROLES[out["role"]]
    folder = folder if folder is not None else spec["folder"]
    name = check_name("/".join(x for x in (_slug(folder) if folder else "", _slug(name or default)) if x))
    try:
        prev = ctx.registry.load("dataset", name)
    except Exception:  # noqa: BLE001
        prev = None
    m = DS.snapshot(ctx, name, out["table"], kind=DatasetKind(spec["kind"]),
                    provenance=Provenance(source="sql" if prov.get("sql") else "import", connection=prov.get("connection"),
                                          sql=prov.get("sql"),
                                          params={**{k: v for k, v in prov.items() if k not in ("connection", "sql")},
                                                  "role": out["role"], "mapping": {k: v for k, v in out["mapping"].items() if v},
                                                  "cleaning": out["report"]["steps"], "rows_in": out["report"]["rows_in"]}),
                    description=f"{spec['label']}, prepared from {prov.get('dataset') or src.get('table') or 'a query'}: "
                                f"{out['report']['rows_out']:,} rows kept of {out['report']['rows_in']:,}")
    LIB.move(ctx, [name], folder or "")
    if project:
        from .. import projects as PR

        PR.assign(ctx, project, [{"kind": "dataset", "name": name}])
    progress(1.0)
    return {"ref": f"{m.name}@v{m.version}", "name": m.name, "version": m.version, "folder": folder or "",
            "kind": spec["kind"], "rows": m.row_count, "report": out["report"],
            "new_version_of_existing": prev is not None}
