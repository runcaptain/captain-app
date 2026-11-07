"""One way to send table data to the app, whatever it came from.

A warehouse table previewed before import, the same rows after import as a dataset, and a sample on the Tables page
must look identical: same column types, same cell values, same paging. Before this module, warehouse previews sent
Arrow type names ("decimal128(10, 2)") and Python reprs of lists ("['a', 'b']"), while datasets sent DuckDB type names
("DECIMAL(10,2)") and JSON ('["a","b"]').

Every route that returns rows uses page() and cell():
  {columns: [{name, type, kind}], rows: [[...]], offset, limit, has_more, total?, ms, filtered}
`type` is the source's own type name. `kind` is one of text, integer, number, decimal, bool, date, time, timestamp,
list, struct, binary, other, which the grid uses to align and format.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import uuid

KINDS = ("text", "integer", "number", "decimal", "bool", "date", "time", "timestamp", "list", "struct", "binary", "other")


def kind_of(type_name: str) -> str:
    t = str(type_name).lower().strip()
    if re.match(r"^(list|large_list|array|fixed_size_list)\b", t) or t.endswith("[]"):
        return "list"
    if re.match(r"^(struct|map|row|object|variant|json)\b", t):
        return "struct"
    if re.match(r"^(decimal\d*|numeric|number)\b", t):
        return "decimal"
    if re.match(r"^(u?int\d*|bigint|smallint|tinyint|integer|int|long|short|hugeint|ubigint|uinteger)\b", t):
        return "integer"
    if re.match(r"^(double|float\d*|real|halffloat)\b", t):
        return "number"
    if re.match(r"^(bool|boolean)\b", t):
        return "bool"
    if re.match(r"^(timestamp|datetime)", t):
        return "timestamp"
    if re.match(r"^date", t):
        return "date"
    if re.match(r"^time(32|64)?\b", t):
        return "time"
    if re.match(r"^(string|large_string|varchar|text|char|nvarchar|uuid|utf8|enum|string_view)", t):
        return "text"
    if re.match(r"^(binary|blob|bytea|large_binary|varbinary)", t):
        return "binary"
    return "other"


def cell(v):
    """A JSON-safe cell. Lists and structs are JSON text; decimals keep every digit as text; times are ISO 8601."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if v != v else v  # NaN has no JSON form
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple, dict, set)):
        return json.dumps(v if not isinstance(v, set) else sorted(v, key=str), default=_default, ensure_ascii=False)[:4000]
    return str(v)


def _default(o):
    c = cell(o)
    return c if not isinstance(o, (list, tuple, dict)) else o


def columns_from_arrow(schema) -> list[dict]:
    return [{"name": f.name, "type": str(f.type), "kind": kind_of(str(f.type))} for f in schema]


def page_from_arrow(table, *, offset: int, limit: int, ms: int, filtered: bool = False, total: int | None = None,
                    sql: str | None = None) -> dict:
    """A page from an Arrow table that holds up to limit+1 rows (the extra row says there's more)."""
    rows = table.to_pylist()
    has_more = len(rows) > limit
    out = {"columns": columns_from_arrow(table.schema),
           "rows": [[cell(v) for v in r.values()] for r in rows[:limit]],
           "offset": int(offset), "limit": int(limit), "has_more": has_more, "ms": ms, "filtered": filtered}
    if total is not None:
        out["total"] = total
    if sql:
        out["sql"] = sql
    return out
