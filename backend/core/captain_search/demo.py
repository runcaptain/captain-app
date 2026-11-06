"""`cst demo seed`: a small synthetic store so a new install has something real to click through.

Everything it makes is labeled. The catalog is generated (not a real store), the queries are
generated, and the judgments come from the heuristic judge, so every number is SIMULATED.
"""
from __future__ import annotations

import os
import random

import duckdb
import pyarrow as pa

from . import ops
from .data.datasets import snapshot
from .model.entities import Connection, DatasetKind, Provenance
from .workspace import Context, home, whoami

TYPES = {
    "running shoes": ("Footwear", ["trail", "road", "racing", "cushioned", "stability"], (60, 220)),
    "hiking boots": ("Footwear", ["waterproof", "leather", "lightweight", "insulated"], (80, 280)),
    "rain jacket": ("Outerwear", ["packable", "hooded", "breathable", "insulated"], (50, 300)),
    "fleece hoodie": ("Outerwear", ["full zip", "recycled", "midweight", "sherpa"], (35, 140)),
    "backpack": ("Bags", ["20l", "30l", "laptop", "hydration", "rolltop"], (40, 220)),
    "water bottle": ("Gear", ["insulated", "32 oz", "18 oz", "filter", "collapsible"], (12, 55)),
    "headlamp": ("Gear", ["rechargeable", "400 lumen", "red light", "waterproof"], (20, 90)),
    "tent": ("Camping", ["2 person", "4 person", "ultralight", "freestanding"], (120, 650)),
    "sleeping bag": ("Camping", ["20 degree", "35 degree", "down", "synthetic"], (70, 420)),
    "usb c cable": ("Electronics", ["6 ft", "braided", "100w", "fast charge"], (8, 35)),
    "wireless earbuds": ("Electronics", ["noise cancelling", "sport", "waterproof", "bluetooth 5.3"], (30, 280)),
    "yoga mat": ("Fitness", ["6mm", "non slip", "cork", "travel"], (20, 120)),
}
BRANDS = ["Northpeak", "Altura", "Ridgeline", "Kestrel", "Summitry", "Voltwave", "Tidepool", "Granite & Co"]
COLORS = ["black", "navy", "olive", "red", "grey", "blue", "orange"]


def catalog(n: int = 480, seed: int = 11) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        t, (cat, attrs, (lo, hi)) = rng.choice(list(TYPES.items()))
        brand = rng.choice(BRANDS)
        a = rng.sample(attrs, k=min(2, len(attrs)))
        color = rng.choice(COLORS)
        model = f"{brand[:2].upper()}-{rng.randint(100, 999)}"
        title = f"{brand} {' '.join(a)} {t} {model} {color}".title()
        out.append({"id": f"p{i:04d}", "title": title, "brand": brand, "category": cat, "color": color,
                    "price": round(rng.uniform(lo, hi), 2), "in_stock": rng.random() > 0.15,
                    "description": f"{' '.join(a).capitalize()} {t} from {brand} in {color}. Model {model}."})
    return out


def seed(ctx: Context, progress=print) -> dict:
    prods = catalog()
    db = os.path.join(home(), "demo", f"{ctx.workspace.name}-store.duckdb")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = duckdb.connect(db)
    con.register("t", pa.Table.from_pylist(prods))
    con.execute("create or replace table products as select * from t")
    rng = random.Random(5)
    logs = [{"ts": f"2026-10-{rng.randint(1, 31):02d}", "query": rng.choice(list(TYPES)) + rng.choice(["", " " + rng.choice(COLORS), " under 50"]),
             "results": rng.choice([0, 0, 3, 12, 40, 40, 40]), "clicked": rng.random() < 0.3} for _ in range(3000)]
    con.register("l", pa.Table.from_pylist(logs))
    con.execute("create or replace table search_log as select * from l")
    con.close()
    progress("made a demo store database with products and search_log tables")
    conn = Connection(org=ctx.workspace.org, workspace=ctx.workspace.name, created_by=whoami(), name="demo-store",
                      engine="duckdb", url=f"duckdb:///{db}")
    try:
        ctx.save_connection(conn)
    except Exception:
        ctx.save_connection(conn, replace=True)
    from .connectors.sql import run_sql

    r = run_sql(conn, "select * from products")
    cat = snapshot(ctx, "demo-catalog", r.table, kind=DatasetKind.catalog, description="synthetic outdoor store catalog (generated)",
                   provenance=Provenance(source="sql", connection="demo-store", sql="select * from products"))
    progress(f"saved {ops.ref(cat)}")
    q = ops.queries_gen(ctx, ops.ref(cat), "demo-queries", ["head", "brand", "multi-attr", "numeric", "model-code", "typo", "zero-result"], 300, 7)
    q = ops.queries_seal(ctx, ops.ref(q), 0.5)
    progress(f"generated and sealed {ops.ref(q)}")
    base = ops.pipeline_publish(ctx, {"name": "demo-bm25", "engine": "local-bm25", "catalog": ops.ref(cat),
                                      "description": "baseline: title-heavy BM25",
                                      "spec": {"fields": {"title": 1.0}, "k1": 1.2, "b": 0.75}})
    tuned = ops.pipeline_publish(ctx, {"name": "demo-tuned", "engine": "local-bm25", "catalog": ops.ref(cat),
                                       "description": "multi-field BM25 with price filter, prefix match and synonyms",
                                       "spec": {"fields": {"title": 3.0, "brand": 1.5, "category": 1.0, "description": 0.7},
                                                "k1": 1.2, "b": 0.75, "numeric_filter": True, "prefix_match": True,
                                                "synonyms": {"earbuds": ["earphones"], "hoodie": ["sweatshirt"]},
                                                "boosts": [{"field": "in_stock", "equals": True, "weight": 1.15}]}})
    progress(f"published {ops.ref(base)} and {ops.ref(tuned)}")
    j = ops.judge_run(ctx, ops.ref(q) + ":holdout", [ops.ref(base), ops.ref(tuned)], "heuristic", "demo-judgments")
    progress(f"graded {j.row_count} pairs into {ops.ref(j)} (heuristic judge, SIMULATED)")
    pop = ops.population_create(ctx, "demo-shoppers", "cascade-v1", 4000, 1)
    bt = ops.backtest_plan(ctx, "demo-tuned-vs-bm25", [ops.ref(base), ops.ref(tuned)], ops.ref(q) + ":holdout",
                           ops.ref(j), ops.ref(pop), "Multi-field BM25 with a price filter beats title-only BM25 on nDCG@10.")
    run = ops.backtest_run(ctx, bt.name)
    progress(f"ran {ops.ref(run)}")
    for name, tier in (("staging", "demo"), ("prod", "live")):
        try:
            ops.env_create(ctx, name, tier, {"type": "record"})
        except Exception:
            pass
    ops.promote(ctx, ops.ref(base), "staging")
    ops.promote(ctx, ops.ref(tuned), "staging")
    progress("created environments staging (demo tier) and prod (live tier), and promoted both pipelines to staging")
    _notebook(ctx)
    progress("saved notebook demo-zero-results")
    hist = historic_demo(ctx, cat, base, tuned, progress)
    extra = review_demo(ctx, q, base, tuned, hist["logs"], progress)
    return {"catalog": ops.ref(cat), "queries": ops.ref(q), "judgments": ops.ref(j), "run": ops.ref(run), **hist, **extra}


def review_demo(ctx, q, base, tuned, logs_ref: str, progress=print) -> dict:
    """A label definition, randomized-holdout logs with a replay sweep, a grading queue and a proposal."""
    from . import grading as GR
    from . import labels as LB
    from . import proposals as PR
    from . import replay as RP
    from .data.datasets import snapshot
    from .data.datasets import to_parquet as tp
    from .model.entities import DatasetKind, Provenance

    lab = LB.define(ctx, "deep-search", "max(case when clicked and position <= 3 then 1 else 0 end)", "search",
                    description="a search counts as deep when the shopper clicked a top-3 result")
    progress(f"defined label {ops.ref(lab)}")
    rows = RP.make_demo_logs()
    t = pa.Table.from_pylist(rows)
    m = snapshot(ctx, "logs/shopping-trigger-holdout", t, kind=DatasetKind.events, dry_run=True,
                 provenance=Provenance(source="generated", params={"method": "synthetic 50/50 trigger holdout"}),
                 description="synthetic randomized holdout: is the shopping source called, and did the request engage")
    e = ctx.registry.put_blob(tp(t), "data/part-0.parquet")
    hl = ctx.registry.publish(m.model_copy(update={"files": [e], "tags": {"synthetic": "true"}}))
    rp = RP.sweep(ctx, "shopping-trigger", ops.ref(hl), "score", "treated", ["engaged", "shopping_impressions"],
                  propensity_col="propensity", direct=["shopping_impressions"])
    progress(f"replayed {ops.ref(rp)} over {ops.ref(hl)} (synthetic, SIMULATED)")
    gq = GR.create(ctx, "demo-grading", ops.ref(q) + ":holdout", [ops.ref(base), ops.ref(tuned)], depth=5, sample=20)
    progress(f"queued {gq['pairs']} pairs for people to grade in demo-grading")
    pr = PR.create(ctx, "Tuned title boost on numeric queries", "Raising the title boost lifts nDCG@10 on numeric queries.",
                   [ops.ref(base), ops.ref(tuned)], ops.ref(q) + ":holdout", "demo-judgments@v1", agent="demo-agent",
                   rationale="Segments show the tuned pipeline's biggest gain is on numeric queries.")
    progress(f"filed agent proposal {pr['id']} for a person to review")
    return {"label": ops.ref(lab), "replay": ops.ref(rp), "grading": "demo-grading", "proposal": pr["id"]}


def historic_demo(ctx, cat, base, tuned, progress=print) -> dict:
    """Synthetic impression logs from the old ranker, then a historic-replay backtest on them.
    The logs are tagged synthetic, so every number they produce stays SIMULATED."""
    from . import historic as H
    from .engines import make_engine

    prods = ops.products_of(ctx, ops.ref(cat))
    eng = make_engine("local-bm25", base.spec, prods)
    qs = [t + suffix for t in TYPES for suffix in ("", " " + COLORS[0], " under 50", " waterproof")]
    rows = H.make_synthetic_logs(prods, lambda q: eng.search(q, 10), qs, 6000, 21)
    m = snapshot(ctx, "logs/demo-search-sessions", pa.Table.from_pylist(rows), kind=DatasetKind.events, dry_run=True,
                 provenance=Provenance(source="generated", params={"generator": "historic.make_synthetic_logs", "sessions": 6000,
                                                                     "ranker": ops.ref(base)}),
                 description="SYNTHETIC search sessions: impressions, clicks and purchases from the old ranker")
    from .data.datasets import to_parquet

    e = ctx.registry.put_blob(to_parquet(pa.Table.from_pylist(rows)), "data/part-0.parquet")
    logs = ctx.registry.publish(m.model_copy(update={"files": [e], "tags": {"synthetic": "true"}}))
    progress(f"saved {ops.ref(logs)} ({len(rows)} impressions, tagged synthetic)")
    bt = ops.backtest_plan(ctx, "demo-historic-replay", [ops.ref(base), ops.ref(tuned)], None, None, None,
                           "Replaying logged searches, the tuned pipeline beats the old ranker on traffic-weighted nDCG@10.",
                           primary="ndcg@10_tw", mode="historic", logs=ops.ref(logs))
    run = ops.backtest_run(ctx, bt.name)
    progress(f"ran historic replay {ops.ref(run)}")
    return {"logs": ops.ref(logs), "historic_run": ops.ref(run)}


def _notebook(ctx):
    from .notebook import new_notebook, save_revision

    nb = new_notebook()
    nb["cells"] = [
        {"cell_type": "markdown", "metadata": {"captain": {"kind": "markdown"}}, "source": "# Zero-result queries\nWhich searches return nothing, and how often?"},
        {"cell_type": "code", "metadata": {"captain": {"kind": "sql", "conn": "demo-store", "var": "zero"}}, "outputs": [], "execution_count": None,
         "source": "select query, count(*) as n\nfrom search_log\nwhere results = 0\ngroup by 1\norder by n desc\nlimit 20"},
        {"cell_type": "code", "metadata": {"captain": {"kind": "python"}}, "outputs": [], "execution_count": None,
         "source": "print(f\"{len(zero)} distinct zero-result queries, {zero.n.sum()} searches\")\nzero.head()"},
        {"cell_type": "code", "metadata": {"captain": {"kind": "chart", "chart": {"mark": "bar", "x": "query", "y": "n", "data": "zero"}}},
         "outputs": [], "execution_count": None, "source": "zero"},
    ]
    save_revision(ctx, "demo-zero-results", nb, "demo seed")
