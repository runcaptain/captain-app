"""`cst data training-set`: a versioned training set that doesn't let the model see its own labels.

One row per (query, product): the features from features/ (the same module that scores at search time), a grade
0 to 3 as the label, and a split (train or valid) decided by the query, so one query's rows never straddle it.

Where the labels come from:
- clicks and purchases in an events dataset, graded the way historic.py grades them (clicks over expected clicks,
  which takes out most of the position bias), or
- a judgments dataset, when there is one that grades queries outside the sealed test set.

Three rules, each a refusal with a plain sentence rather than a warning:
1. A cutoff in time. Features that count behaviour (popularity, click rates) read only events up to `as_of`.
   Labels come from events after it, up to `train_until`. Events after `train_until` are left for evaluation,
   and no training row reads them. By default `as_of` is the 60% point of the events in time and `train_until`
   the 80% point, so the last fifth of the log judges what the first four fifths trained.
2. No feature is counted from the label window. The popularity tables record the first and last timestamp of the
   rows they were actually built from, and `features.check_windows` refuses a table that reaches the labels.
3. No training query is in the sealed test set. Queries the test set holds are dropped and counted. If one is
   still there when the set is written, the build is refused.

Provenance records the label source, both windows, the cutoffs, and the fingerprint of every input. Its catalog,
logs and judgments refs are the lineage edges (lineage_index.derive reads them). A set built from simulated
events is tagged synthetic, so everything trained and judged on it reads SIMULATED.
"""
from __future__ import annotations

import json

import pyarrow as pa

from . import features as F
from .eval import queries as Q
from .model.entities import DatasetKind, FileEntry, Provenance
from .storage.registry import check_name, sha256

LABEL_COLUMNS = ["query_id", "query", "product_id", "grade", "split"]
MIN_QUERIES = 20


def holdout_queries(ctx, holdout: str | None) -> set[str]:
    """The sealed test set's queries, as normalized text."""
    if not holdout:
        return set()
    from .ops import rows_of

    ref_ = holdout if ":" in holdout else holdout + ":holdout"
    _, rows, _ = rows_of(ctx, ref_)
    return {F.norm_query(r["query"]) for r in rows}


def check_holdout(queries, sealed: set[str], holdout: str | None = None) -> None:
    hit = sorted({F.norm_query(q) for q in queries} & sealed)
    if hit:
        raise F.Leak(f"{len(hit)} training {'query is' if len(hit) == 1 else 'queries are'} in the sealed test set"
                     f"{' ' + holdout if holdout else ''} (for example \"{hit[0]}\"). A model that trains on its test "
                     "queries can't be judged on them. Leave those queries out of training.")


def cutoffs(rows: list[dict], as_of: str | None = None, train_until: str | None = None) -> dict:
    """Where the events are cut in time. Returns {column, as_of, train_until, first, last}. The defaults are the
    60% and 80% points by row, moved to real timestamps in the log, so "up to as_of" always holds whole rows."""
    col = F.ts_column(rows[0]) if rows else None
    if col is None:
        raise ValueError("The events have no timestamp column (ts, timestamp, event_time or time). Without one Captain "
                         "can't keep the searches a model trains on apart from the ones it is judged on. Add a "
                         "timestamp column to the events.")
    stamps = sorted(F.ts_key(r.get(col)) for r in rows)
    first, last = stamps[0], stamps[-1]

    def at(frac: float) -> str:
        return stamps[min(len(stamps) - 1, int(len(stamps) * frac))]

    def before(t: str) -> str:
        """The last timestamp strictly before the one at t, so the cut never swallows the final stamp."""
        lower = [s for s in stamps if s < t]
        return lower[-1] if lower else t

    until = F.ts_key(train_until) if train_until else at(0.8)
    if until >= last:
        until = before(last) if not train_until else until
    cut = F.ts_key(as_of) if as_of else at(0.6)
    if not as_of and cut >= until:
        cut = before(until)
    if cut >= until:
        raise ValueError(f"the feature cutoff ({cut}) must be earlier than the end of the label window ({until})")
    if cut < first or not any(cut < s <= until for s in stamps):
        raise ValueError(f"no events fall between the cutoff {cut} and {until}, so there is nothing to label. The "
                         f"events run from {first} to {last}.")
    return {"column": col, "as_of": cut, "train_until": until, "first": first, "last": last}


def _window(rows: list[dict], col: str) -> dict:
    stamps = sorted(F.ts_key(r.get(col)) for r in rows)
    return {"from": stamps[0] if stamps else None, "to": stamps[-1] if stamps else None, "rows": len(rows)}


def _fingerprint(m) -> dict:
    from .project_state import dataset_fingerprint

    return {"ref": f"{m.name}@v{m.version}", "rows": m.row_count or None, "fingerprint": dataset_fingerprint(m)}


def plan(ctx, catalog: str, events: str | None = None, judgments: str | None = None, queries: str | None = None,
         holdout: str | None = None, as_of: str | None = None, train_until: str | None = None,
         title: str | None = None, labels: str = "auto", popularity: bool | None = None,
         min_impressions: int = 3, valid: float = 0.2, salt: str = "", min_queries: int = MIN_QUERIES,
         tables: dict | None = None) -> dict:
    """Everything `build` will write, without writing it: the rows, the feature bundle and the provenance. Raises
    ValueError (features.Leak for a leak) with the sentence to show.

    `labels` is auto, clicks or judgments. `tables` replaces the popularity tables this would count itself: the gate
    still checks whatever window they were really counted from."""
    from . import historic as H
    from .ops import products_of, ref, rows_of

    cm, _ = ctx.registry.resolve("dataset", catalog)
    products = products_of(ctx, ref(cm))
    if not products:
        raise ValueError(f"{ref(cm)} has no rows")
    spec = F.detect_spec(list(products[0]), title)
    notes: list[str] = []
    sealed = holdout_queries(ctx, holdout)
    em = jm = None
    feat_rows: list[dict] = []
    label_window = None
    cut = None
    pairs: list[dict] = []
    source = None
    simulated = False

    erows = None
    if events:
        em, erows, _ = rows_of(ctx, events)
        if erows and not H.REQUIRED <= set(erows[0]):
            raise ValueError(f"{ref(em)} needs the columns {', '.join(sorted(H.REQUIRED))} to learn from clicks. It has "
                             f"{', '.join(sorted(erows[0]))}.")
        simulated = H.synthetic(em)

    def from_judgments() -> list[dict]:
        nonlocal jm
        jm, jrows, _ = rows_of(ctx, judgments)
        text = {}
        if queries:
            _, qrows, _ = rows_of(ctx, queries.split(":")[0])
            text = {r["query_id"]: r["query"] for r in qrows}
        out = []
        for r in jrows:
            q = r.get("query") or text.get(r["query_id"])
            if q is None:
                raise ValueError(f"{ref(jm)} has no query text, only query ids. Name the queries dataset those ids "
                                 "come from, so the features can read the words.")
            out.append({"query_id": str(r["query_id"]), "query": F.norm_query(q), "product_id": str(r["product_id"]),
                        "grade": int(r["grade"])})
        return out

    use = labels
    if use == "auto":
        use = "clicks" if events else "judgments"
        if judgments:
            jp = from_judgments()
            left = {p["query"] for p in jp} - sealed
            if len(left) >= min_queries:
                use, pairs = "judgments", jp
            elif events:
                notes.append(f"{ref(jm)} grades only {len(left)} "
                             f"{'query' if len(left) == 1 else 'queries'} outside the sealed test set, so the labels "
                             "come from clicks in the events instead.")
            else:
                use, pairs = "judgments", jp
    if use == "judgments":
        if not judgments:
            raise ValueError("labels from judgments need a judgments dataset")
        pairs = pairs or from_judgments()
        source = {"human": "human", "llm": "llm", "click": "clicks", "heuristic": "simulated"}.get(jm.judgment_source or "", None)
        if H.synthetic(jm):
            simulated = True
    elif use == "clicks":
        if not events:
            raise ValueError("labels from clicks need an events dataset with query, product_id, position and clicked")
        cut = cutoffs(erows, as_of, train_until)
        col = cut["column"]
        lab_rows = [r for r in erows if cut["as_of"] < F.ts_key(r.get(col)) <= cut["train_until"]]
        label_window = _window(lab_rows, col)
        graded, _ = H.grades(lab_rows, min_impressions)
        pairs = [{"query_id": g["query_id"], "query": g["query"], "product_id": g["product_id"], "grade": g["grade"]}
                 for g in graded]
        source = "clicks"
    else:
        raise ValueError("labels is auto, clicks or judgments")

    # Popularity: only from events before the cutoff, and never beside labels that were themselves made from clicks
    # in a window Captain can't see.
    want_pop = popularity if popularity is not None else bool(events)
    if tables is None and want_pop and events:
        if use == "judgments" and (jm.judgment_source == "click"):
            if popularity:
                raise F.Leak(f"{ref(jm)} was graded from clicks. Click rates counted from the events could be the same "
                             "clicks, which gives the model the answer. Leave popularity features out.")
            notes.append(f"{ref(jm)} was graded from clicks, so popularity features are left out: they could count "
                         "the same clicks.")
        else:
            try:
                cut = cut or cutoffs(erows, as_of, train_until)
                feat_rows = [r for r in erows if F.ts_key(r.get(cut["column"])) <= cut["as_of"]]
                tables = F.popularity_tables(feat_rows)
            except ValueError as e:
                if use == "clicks" or popularity:
                    raise
                notes.append(f"Popularity features are left out: {e}")
    if tables is not None:
        F.check_windows(tables.get("window"), label_window)

    # The sealed test set: its queries are dropped and counted, then the gate checks nothing slipped through.
    dropped = sorted({p["query"] for p in pairs} & sealed)
    pairs = [p for p in pairs if p["query"] not in sealed]
    check_holdout((p["query"] for p in pairs), sealed, holdout)
    if dropped:
        notes.append(f"{len(dropped)} {'query was' if len(dropped) == 1 else 'queries were'} left out because the "
                     "sealed test set holds them.")

    known = {str(p["id"]) for p in products}
    unknown = sum(1 for p in pairs if p["product_id"] not in known)
    pairs = [p for p in pairs if p["product_id"] in known]
    if unknown:
        notes.append(f"{unknown} labelled {'pair names a product' if unknown == 1 else 'pairs name products'} the "
                     "catalog doesn't hold, and were left out.")
    by_q: dict[str, list[dict]] = {}
    for p in pairs:
        by_q.setdefault(p["query_id"], []).append(p)
    # A query whose products all got the same grade teaches a ranker nothing about order.
    flat = [q for q, ps in by_q.items() if len({p["grade"] for p in ps}) < 2]
    for q in flat:
        del by_q[q]
    if len(by_q) < min_queries:
        raise ValueError(f"Only {len(by_q)} {'query has' if len(by_q) == 1 else 'queries have'} products with different "
                         f"grades to learn an order from, and a ranker needs at least {min_queries}. "
                         + ("More events, or a longer label window, would give more." if use == "clicks"
                            else "More judged queries outside the test set would give more."))

    fz = F.Featurizer(products, spec, tables)
    split_salt = salt or f"{ref(cm)}:{use}"
    rows = []
    for qid in sorted(by_q):
        ps = sorted(by_q[qid], key=lambda p: p["product_id"])
        sp = "valid" if Q.split_of(qid, valid, split_salt) == "holdout" else "train"
        for p, fv in zip(ps, fz.features(ps[0]["query"], [p["product_id"] for p in ps])):
            rows.append({"query_id": qid, "query": p["query"], "product_id": p["product_id"], "grade": p["grade"],
                         "split": sp, **dict(zip(fz.names, fv))})
    n_valid = len({r["query_id"] for r in rows if r["split"] == "valid"})
    n_train = len(by_q) - n_valid
    if not n_valid or not n_train:
        raise ValueError("the split left no queries on one side. Use a validation share between 0.05 and 0.5.")

    params = {
        "catalog": ref(cm), **({"logs": ref(em)} if em else {}), **({"judgments": ref(jm)} if use == "judgments" else {}),
        "method": "features and labels for learning to rank (cst data training-set)",
        "feature_version": F.VERSION, "features": fz.names, "feature_spec": spec,
        "label_source": source, "labels_from": "judgments" if use == "judgments" else "clicks",
        "label_method": ("grades as given" if use == "judgments"
                         else f"clicks over expected clicks (historic.grades), pairs with {min_impressions}+ impressions"),
        "as_of": cut["as_of"] if cut else None, "train_until": cut["train_until"] if cut and use == "clicks" else None,
        "feature_window": (tables or {}).get("window"), "label_window": label_window,
        "evaluation_window": ({"after": cut["train_until"], "to": cut["last"]} if cut and use == "clicks" else None),
        "holdout": holdout, "holdout_queries_dropped": len(dropped),
        "split": {"by": "query", "valid": valid, "salt": split_salt, "train_queries": n_train, "valid_queries": n_valid},
        "inputs": [_fingerprint(m) for m in (cm, em, jm) if m is not None],
        "simulated": simulated, "notes": notes,
    }
    return {"rows": rows, "names": fz.names, "bundle": F.bundle(spec, tables, {"label_source": source, "simulated": simulated,
                                                                              "as_of": params["as_of"],
                                                                              "train_until": params["train_until"]}),
            "params": params, "notes": notes, "simulated": simulated,
            "summary": {"rows": len(rows), "queries": len(by_q), "train_queries": n_train, "valid_queries": n_valid,
                        "features": len(fz.names), "labels_from": params["labels_from"], "label_source": source,
                        "as_of": params["as_of"], "train_until": params["train_until"],
                        "feature_window": params["feature_window"], "label_window": label_window,
                        "popularity": tables is not None, "holdout_queries_dropped": len(dropped),
                        "simulated": simulated, "catalog": ref(cm), "events": ref(em) if em else None,
                        "judgments": ref(jm) if use == "judgments" else None}}


def build(ctx, name: str, catalog: str, dry_run: bool = False, **kw):
    """Build and publish the training set. Returns (DatasetVersion, plan). The version holds the rows as parquet
    and `features.json`, the bundle a trainer copies into its artifact so scoring computes the same features."""
    from .data.datasets import to_parquet

    name = check_name(name)
    p = plan(ctx, catalog, **{"salt": name, **kw})
    cols = LABEL_COLUMNS + p["names"]
    table = pa.Table.from_pylist(p["rows"]).select(cols)
    data = to_parquet(table)
    fbytes = json.dumps(p["bundle"], sort_keys=True, separators=(",", ":")).encode()
    s = p["summary"]
    tags = {"label_source": s["label_source"]} if s["label_source"] else {}
    if s["train_until"] or s["as_of"]:
        tags["as_of"] = s["train_until"] or s["as_of"]  # what a trainer is told: nothing after this was read
    if p["simulated"]:
        tags["synthetic"] = "true"
    from .model.entities import DatasetVersion
    from .workspace import whoami

    m = DatasetVersion(
        org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name=name, version=0,
        dataset_kind=DatasetKind.training,
        description=(("SIMULATED. " if p["simulated"] else "")
                     + f"{s['rows']:,} (query, product) rows over {s['queries']:,} queries, {s['features']} features, "
                       f"labels from {s['labels_from']}"
                     + (f", features up to {s['as_of']}, labels to {s['train_until']}" if s["train_until"] else "")),
        schema_fields=[{"name": f.name, "type": str(f.type)} for f in table.schema], row_count=table.num_rows,
        provenance=Provenance(source="generated", params=p["params"]), tags=tags,
        files=[FileEntry(path="data/part-0.parquet", sha256=sha256(data), bytes=len(data)),
               FileEntry(path="features.json", sha256=sha256(fbytes), bytes=len(fbytes))])
    if dry_run:
        return m, p
    files = [ctx.registry.put_blob(data, "data/part-0.parquet"), ctx.registry.put_blob(fbytes, "features.json")]
    return ctx.registry.publish(m.model_copy(update={"files": files})), p
