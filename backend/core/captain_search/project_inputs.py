"""Where a project's inputs come from: a connection, a local file or folder, or a dataset already in the workspace.

Each input is either a **snapshot** (the rows are copied into a new write-once dataset version, which the slot pins)
or **live** (a reference: Captain records where to read and a fingerprint of what is there now, and each run reads
it again). The person picks one every time, because both are right for different inputs.

  from        snapshot                                         live
  connection  runs the table, SQL, PostHog event or GA4 report  stores the query (connection, sql). Runs count its
              once and saves it (the `cst sql --save` path)     rows and fingerprint it at the time
  local       reads the file or folder into a dataset           registers the files by reference (external.register),
              (parquet, csv, jsonl, json, duckdb)                with sizes and modified times
  dataset     pins that exact version                           follows the newest version of that dataset

`run_inputs` turns the slots into the trainer contract's inputs for one run, and says exactly what each read: rows,
fingerprint, as_of, and whether a live input changed since it was pinned. A live folder that changed is registered
again as a new by-reference version, so a run's record always names a version that describes what it read.

Fingerprints: a dataset version hashes its files' checksums (or, by reference, uris, sizes and etags). A local
folder hashes each file's path, size and modified time. A live query hashes its text and its row count, so a change
that keeps the count the same isn't seen: snapshot when that matters.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

import pyarrow as pa

from . import project_state as PS
from .model.entities import DatasetKind, Provenance
from .workspace import Context, whoami

TABLE_EXTS = (".parquet", ".csv", ".tsv", ".jsonl", ".ndjson", ".json")
MODES = ("snapshot", "live")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")
_EVENT = re.compile(r"^[\w$ .:/-]{1,200}$")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _h(*parts) -> str:
    return "sha256:" + hashlib.sha256("\n".join(str(p) for p in parts).encode()).hexdigest()


# ---- the picker's listings ------------------------------------------------------------------------


def _suggested_folders() -> list[dict]:
    """Folders on this machine that hold table files, a level deep: the person's usual places and the repo's data/."""
    cands = [("~/Downloads", "Downloads"), ("~/Documents", "Documents"), ("~/Desktop", "Desktop"), ("./data", "data/ here")]
    from .extensions import find_project

    try:
        proj = find_project()
    except Exception:  # noqa: BLE001
        proj = None
    if proj is not None:
        cands.insert(0, (str(Path(proj) / "data"), "the project folder's data/"))
    out, seen = [], set()
    for raw, label in cands:
        p = Path(os.path.expanduser(raw)).resolve()
        if p in seen or not p.is_dir():
            continue
        seen.add(p)
        try:
            n = sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() in TABLE_EXTS + (".duckdb",))
            n += sum(1 for d in p.iterdir() if d.is_dir() and not d.name.startswith(".") and any(
                f.suffix.lower() in TABLE_EXTS for f in list(d.iterdir())[:200] if f.is_file()))
        except OSError:
            continue
        if n:
            out.append({"path": str(p), "label": label, "files": n})
    return out


def sources(ctx: Context) -> dict:
    """What the input picker offers: each connection and what it can give, the workspace's datasets, and local
    folders with table files. Reads only cached facts: nothing here calls a database."""
    import yaml

    from . import metastore as MS
    from .connectors.kinds import BY_KIND

    conns = []
    for n in ctx.connections():
        raw = yaml.safe_load(ctx.store.get(ctx.conn_key(n))) or {}
        if raw.get("deleted"):
            continue
        eng = raw.get("engine", "")
        offers = ["event", "sql"] if eng == "posthog" else ["report"] if eng == "ga4" else ["table", "sql"]
        try:
            doc = MS.cached(ctx, n)
        except Exception:  # noqa: BLE001
            doc = None
        tables = sum(len(s.get("tables") or []) for s in (doc or {}).get("schemas", []))
        conns.append({"name": n, "engine": eng, "label": (BY_KIND.get(eng) or {}).get("label", eng), "offers": offers,
                      "tables_cached": tables, "tables_route": f"/api/tables/{n}",
                      "events_route": f"/api/sources/{n}/events" if eng in ("posthog", "ga4") else None,
                      "suggestions_route": f"/api/connections/{n}/suggestions"})
    datasets = []
    for name in ctx.registry.names("dataset"):
        try:
            m = ctx.registry.load("dataset", name)
        except Exception:  # noqa: BLE001
            continue
        datasets.append({"name": name, "version": m.version, "kind": m.dataset_kind.value, "rows": m.row_count,
                         "created_at": m.created_at.isoformat(timespec="seconds"),
                         "by_reference": bool(m.external)})
    return {"connections": conns, "datasets": datasets, "folders": _suggested_folders()}


def event_types(ctx: Context, conn_name: str, days: int = 30) -> list[dict]:
    """PostHog event names, or GA4 event names, with their counts over the last `days` days."""
    from .connectors.sql import run_sql

    conn = ctx.load_connection(conn_name)
    days = max(1, min(int(days), 365))
    if conn.engine == "posthog":
        r = run_sql(conn, f"select event, count() as n from events where timestamp > now() - interval {days} day "
                          "group by event order by n desc limit 200")
        return [{"event": x["event"], "count": x["n"]} for x in r.table.to_pylist()]
    if conn.engine == "ga4":
        r = run_sql(conn, json.dumps({"dimensions": ["eventName"], "metrics": ["eventCount"], "start": f"{days}daysAgo",
                                      "limit": 200}))
        return [{"event": x["eventName"], "count": x["eventCount"]} for x in r.table.to_pylist()]
    raise ValueError(f"{conn_name} is a {conn.engine} connection: event types come from PostHog and Google Analytics")


# ---- reading a source ---------------------------------------------------------------------------


def query_of(ctx: Context, source: dict) -> str:
    """The query text a connection source runs: a table, SQL, a PostHog event or a GA4 report."""
    conn = ctx.load_connection(source["connection"])
    n = sum(1 for k in ("table", "sql", "event", "report") if source.get(k))
    if n != 1:
        raise ValueError("a connection source names one of table, sql, event (PostHog) or report (Google Analytics)")
    if source.get("table"):
        t = str(source["table"])
        if not _IDENT.match(t):
            raise ValueError(f"{t!r} isn't a table name like schema.table")
        return f"select * from {t}"
    if source.get("sql"):
        return str(source["sql"])
    if source.get("event"):
        if conn.engine != "posthog":
            raise ValueError("an event source needs a PostHog connection")
        e = source["event"] if isinstance(source["event"], dict) else {"name": source["event"]}
        name, days = str(e.get("name") or ""), max(1, min(int(e.get("days") or 30), 365))
        if not _EVENT.match(name):
            raise ValueError(f"{name!r} isn't an event name")
        return (f"select * from events where event = '{name}' and timestamp > now() - interval {days} day "
                "order by timestamp")
    if conn.engine != "ga4":
        raise ValueError("a report source needs a Google Analytics connection")
    from .connectors.sources import ga4_spec

    spec = source["report"] if isinstance(source["report"], dict) else ga4_spec(str(source["report"]))
    return json.dumps(ga4_spec(json.dumps(spec)))


def _run(ctx: Context, conn_name: str, text: str, limit: int | None = None):
    from .connectors.sql import run_sql
    from .metastore import record_query

    conn = ctx.load_connection(conn_name)
    try:
        r = run_sql(conn, text, limit=limit)
    except Exception:
        record_query(ctx, conn_name, text, source="project", ok=False)
        raise
    record_query(ctx, conn_name, text, rows=r.table.num_rows, ms=r.elapsed_ms, source="project")
    return r


def _count(ctx: Context, conn_name: str, text: str) -> int | None:
    """How many rows a live query returns now, without reading them (a GA4 report is run, since it is small)."""
    conn = ctx.load_connection(conn_name)
    if conn.engine == "ga4":
        return _run(ctx, conn_name, text).table.num_rows
    r = _run(ctx, conn_name, f"select count(*) as n from ({text.strip().rstrip(';')}) as cst_count")
    row = r.table.to_pylist()[0] if r.table.num_rows else {}
    return int(next(iter(row.values()), 0) or 0)


def _local_files(path: str) -> list[Path]:
    p = Path(os.path.expanduser(path)).resolve()
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in TABLE_EXTS
                      and not any(x.startswith(".") for x in f.relative_to(p).parts))
    raise ValueError(f"nothing at {p}")


def _read_local(path: str, table: str | None = None) -> pa.Table:
    from .data.datasets import read_file

    p = Path(os.path.expanduser(path)).resolve()
    if p.suffix.lower() == ".duckdb":
        import duckdb

        if not table or not _IDENT.match(table):
            raise ValueError("a .duckdb file holds many tables: name the one to read (table)")
        con = duckdb.connect(str(p), read_only=True)
        try:
            return con.sql(f"select * from {table}").to_arrow_table()
        finally:
            con.close()
    files = _local_files(path)
    if not files:
        raise ValueError(f"{p} holds no table files (parquet, csv, jsonl, json)")
    parts = [read_file(str(f)) for f in files]
    try:
        return pa.concat_tables(parts, promote_options="default")
    except pa.ArrowInvalid as e:
        raise ValueError(f"the files in {p} don't share columns, so they can't be one input: {e}") from None


def _local_fingerprint(path: str) -> tuple[str, list[Path]]:
    files = _local_files(path)
    st = [(str(f), f.stat().st_size, f.stat().st_mtime_ns) for f in files]
    return _h(*(f"{a}:{b}:{c}" for a, b, c in st)), files


def _local_rows(files: list[Path]) -> int | None:
    import duckdb

    fn = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".parquet": "read_parquet", ".jsonl": "read_json_auto",
          ".ndjson": "read_json_auto", ".json": "read_json_auto"}
    try:
        con = duckdb.connect()
        return sum(con.sql(f"select count(*) from {fn[f.suffix.lower()]}($p)", params={"p": str(f)}).fetchall()[0][0]
                   for f in files)
    except Exception:  # noqa: BLE001  a count is a convenience: leave it unknown rather than fail the pin
        return None


def _kind_for(sl: dict, columns: list[str], asked: str | None) -> DatasetKind:
    from .data.importer import guess_kind

    if asked:
        return DatasetKind(asked)
    acc = [a for a in sl["accepts"] if a != "any"]
    if len(acc) == 1:
        return DatasetKind(acc[0])
    g = guess_kind(columns)
    if acc and g.value not in acc:
        return DatasetKind(acc[0])
    return g


def _register_local(ctx: Context, name: str, files: list[Path], kind: DatasetKind, fp: str, path: str):
    from . import external as EX
    from .model.entities import ExternalFile

    ext = [ExternalFile(uri=f.as_uri(), bytes=f.stat().st_size) for f in files]
    return EX.register(ctx, name, [], kind, description=f"{len(files)} file(s) at {path}, by reference",
                       row_count=_local_rows(files), tags={"live": "true", "fingerprint": fp},
                       provenance=Provenance(source="import", params={"path": path, "fingerprint": fp}),
                       extra_files=ext)


def _name(pid: str, sid: str) -> str:
    return f"{pid}/inputs/{sid}"


def set_input(ctx: Context, pid: str, sid: str, source: dict, mode: str | None, as_of: str | None = None,
              kind: str | None = None, progress=lambda f: None) -> dict:
    """Point a slot at a source, as a snapshot or a live reference. Returns {slot, input}."""
    from .ops import ref

    if mode not in MODES:
        raise ValueError("choose snapshot (copy the rows now) or live (read the source at each run)")
    doc = PS.get(ctx, pid)
    sl = PS.slot(doc, sid)
    frm = (source or {}).get("from")
    rec = {"mode": mode, "from": frm, "source": dict(source), "as_of": as_of, "pinned_at": _now(), "pinned_by": whoami()}
    pin_ref = None
    if frm == "dataset":
        m, split = ctx.registry.resolve("dataset", str(source.get("ref") or ""))
        pin_ref = PS._exact(ctx, sl, f"{ref(m)}" + (f":{split}" if split else ""))
        if mode == "live":
            rec["source"] = {"from": "dataset", "ref": m.name}  # follow the name, not the version
        rec.update(ref=pin_ref, rows=m.row_count or None, fingerprint=PS.dataset_fingerprint(m),
                   as_of=as_of or (m.tags or {}).get("as_of"), **PS.simulated_mark(m))
    elif frm == "local":
        path = str(source.get("path") or "")
        if mode == "live":
            if Path(os.path.expanduser(path)).suffix.lower() == ".duckdb":
                raise ValueError("a .duckdb file can't be read by reference yet: take a snapshot")
            fp, files = _local_fingerprint(path)
            k = _kind_for(sl, [], kind)
            m = _register_local(ctx, _name(pid, sid), files, k, fp, path)
            pin_ref = PS._exact(ctx, sl, ref(m))
            rec.update(ref=pin_ref, rows=m.row_count or None, fingerprint=fp)
        else:
            from .data.datasets import snapshot

            progress(0.2)
            t = _read_local(path, source.get("table"))
            k = _kind_for(sl, t.column_names, kind)
            m = snapshot(ctx, _name(pid, sid), t, kind=k, description=f"Snapshot of {path} for {pid}",
                         provenance=Provenance(source="file", file=path, params={"table": source.get("table")}))
            pin_ref = PS._exact(ctx, sl, ref(m))
            rec.update(ref=pin_ref, rows=m.row_count, fingerprint=PS.dataset_fingerprint(m))
    elif frm == "connection":
        text = query_of(ctx, source)
        conn = source["connection"]
        if mode == "live":
            n = _count(ctx, conn, text)
            rec.update(ref=None, rows=n, fingerprint=_h(conn, text, n), query=text)
        else:
            from .data.datasets import snapshot

            progress(0.2)
            r = _run(ctx, conn, text)
            k = _kind_for(sl, r.table.column_names, kind)
            m = snapshot(ctx, _name(pid, sid), r.table, kind=k, truncated=r.truncated,
                         description=f"Snapshot of {conn} for {pid}",
                         provenance=Provenance(source="sql", connection=conn, sql=text))
            pin_ref = PS._exact(ctx, sl, ref(m))
            rec.update(ref=pin_ref, rows=m.row_count, fingerprint=PS.dataset_fingerprint(m), truncated=r.truncated)
    else:
        raise ValueError("a source is {from: connection | local | dataset, ...}")
    if pin_ref is not None:
        from . import projects as PR

        PR.assign(ctx, pid, [{"kind": "dataset", "name": pin_ref.split("@")[0]}])
    progress(0.9)
    PS.set_input(ctx, pid, sid, rec, pin_ref)
    return {"slot": sid, "input": rec}


def events_plan(ctx: Context, pid: str, sid: str) -> dict:
    """What generating synthetic events for this slot would read and write. Refuses a slot that doesn't take events,
    and a project with no catalog: the shoppers search the catalog's own products."""
    doc = PS.get(ctx, pid)
    sl = PS.slot(doc, sid)
    if sl["role"] != "events" and "events" not in sl["accepts"]:
        raise ValueError(f"the {sl['label']} input doesn't take events, so synthetic events can't go in it")
    catalog = PS.pinned(doc, "catalog")
    if not catalog:
        raise ValueError("choose a catalog first: synthetic shoppers search the catalog's own products")
    return {"project": pid, "slot": sid, "catalog": catalog, "name": f"{pid}/synthetic-events"}


def generate_events(ctx: Context, pid: str, sid: str, sessions: int = 20000, days: int = 30, seed: int = 7,
                    end: str | None = None, progress=lambda f: None) -> dict:
    """Generate synthetic shopper events from the project's catalog and set them as this slot's input, as a snapshot.
    Returns {slot, input, dataset, stats, simulated}."""
    from . import ops

    plan = events_plan(ctx, pid, sid)
    m = ops.sim_events(ctx, plan["catalog"], plan["name"], sessions, days, seed, end, progress=progress)
    r = set_input(ctx, pid, sid, {"from": "dataset", "ref": ops.ref(m)}, "snapshot")
    return {**r, "dataset": ops.ref(m), "stats": m.provenance.params.get("stats"), "simulated": True}


def needs_job(source: dict, mode: str | None) -> bool:
    """Whether setting this input reads a lot of data (so the app runs it as a job)."""
    return mode == "snapshot" and (source or {}).get("from") in ("connection", "local")


def clear_input(ctx: Context, pid: str, sid: str) -> dict:
    PS.set_input(ctx, pid, sid, None, None)
    return PS.get(ctx, pid)


# ---- what a run reads -----------------------------------------------------------------------------


def run_inputs(ctx: Context, pid: str, refresh: bool = True) -> list[dict]:
    """The project's inputs for one run, in the trainer contract's shape plus what was read. A live input is read
    again: a dataset by its newest version, a folder by a fresh listing (registered again if it changed), a query by
    its row count. Slots with nothing set are left out."""
    from .ops import ref

    doc = PS.get(ctx, pid)
    out = []
    for sl in PS.slots_of(doc):
        rec = doc["inputs"].get(sl["id"])
        pin = doc["pins"].get(sl["id"])
        if not rec and not pin:
            continue
        rec = rec or {"mode": "snapshot", "from": "dataset", "ref": pin, "fingerprint": None, "rows": None}
        item = {"slot": sl["id"], "role": sl["role"], "mode": rec.get("mode"), "as_of": rec.get("as_of"),
                "changed_since_pin": False, "read_at": _now()}
        src = rec.get("source") or {}
        if rec.get("mode") == "live" and refresh and rec.get("from") == "dataset":
            m = ctx.registry.load("dataset", src.get("ref") or pin.split("@")[0])
            r = ref(m) + (":holdout" if sl["role"] == "holdout" else "")
            item.update(ref=r, rows=m.row_count or None, fingerprint=PS.dataset_fingerprint(m))
        elif rec.get("mode") == "live" and refresh and rec.get("from") == "local":
            fp, files = _local_fingerprint(src["path"])
            r = pin
            if fp != rec.get("fingerprint"):
                m = _register_local(ctx, _name(pid, sl["id"]), files, ctx.registry.resolve("dataset", pin)[0].dataset_kind,
                                    fp, src["path"])
                r = ref(m)
            m, _ = ctx.registry.resolve("dataset", r)
            item.update(ref=r, rows=m.row_count or None, fingerprint=fp)
        elif rec.get("mode") == "live" and rec.get("from") == "connection":
            text = rec.get("query") or query_of(ctx, src)
            n = _count(ctx, src["connection"], text) if refresh else rec.get("rows")
            item.update(ref=None, rows=n, fingerprint=_h(src["connection"], text, n),
                        source={"connection": src["connection"], "query": text},
                        kind=(sl["accepts"][0] if sl["accepts"] and sl["accepts"][0] != "any" else "generic"))
        else:
            m, _ = ctx.registry.resolve("dataset", pin)
            item.update(ref=pin, rows=m.row_count or None, fingerprint=PS.dataset_fingerprint(m))
        item["changed_since_pin"] = bool(rec.get("fingerprint")) and item["fingerprint"] != rec.get("fingerprint")
        out.append(item)
    if refresh:
        PS.note_read(ctx, pid, out)
    return out
