#!/usr/bin/env python3
"""Shot check (SHOT_PROJECT=1): a project, and a model registry on disk with two brands, one of them the project's, so
the project home shows models and the Choose models picker has something to pick. Its inputs are pinned (queries at an
older version, so a card shows "Newer version available"), the baseline is the champion, and the tuned ranker is an
experiment trained on the holdout's own judgments and judged against it, so the page shows a filled Experiments table
with the "agrees with its own labeler" row. SHOT_PROJECT_START=1 makes it a project with nothing trained yet instead. Prints the project's id. Run with cst's own Python and CST_HOME."""
import os

from captain_search import attach as AT
from captain_search import projects as PR
from captain_search.workspace import open_context

ctx = open_context()
reg = os.path.join(os.environ["CST_HOME"], "shot-registry")
for arch, brand, ver in [("arch-v1", "shop", "v1"), ("arch-v2", "shop", "v2"), ("arch-v2", "outlet", "v1")]:
    os.makedirs(os.path.join(reg, arch, brand, ver), exist_ok=True)
    with open(os.path.join(reg, arch, brand, ver, "MANIFEST.yaml"), "w") as f:
        f.write("status: candidate\n")
AT.attach(ctx, reg)
# SHOT_PROJECT_NAME: another name, e.g. a long one to check the header keeps it readable.
p = PR.create(ctx, os.environ.get("SHOT_PROJECT_NAME") or "Shot Project", brands=["shop"])
PR.assign(ctx, p["id"], [{"kind": "dataset", "name": "demo-catalog"}])

from captain_search import project_state as PS

PS.pin(ctx, p["id"], {"catalog": "demo-catalog", "queries": "demo-queries@v1", "holdout": "demo-queries", "labels": "demo-judgments"})
# What it builds (a recipe), its search events kept live from the demo store, and an optional input of its own.
from captain_search import project_inputs as PI

PS.set_kind(ctx, p["id"], "search-judgments")
try:
    PI.set_input(ctx, p["id"], "events", {"from": "connection", "connection": "demo-store", "table": "main.search_log"}, "live")
except Exception:  # noqa: BLE001  a recipe without an events slot: the page still shows the rest
    pass
PS.slot_add(ctx, p["id"], {"label": "Product embeddings", "why": "Item vectors for the second-stage ranker"})
if os.environ.get("SHOT_PROJECT_START"):
    # Nothing trained yet: a catalog and simulated events only, so Train and compare shows the guided start
    # (1 set a keyword baseline, 2 train a starter ranker) and the top line says to set a baseline.
    PS.set_kind(ctx, p["id"], "search-events")
    PI.generate_events(ctx, p["id"], "events", sessions=4000, days=30, seed=7, end="2026-10-31")
else:
    PS.experiment_add(ctx, p["id"], "demo-bm25")
    e = PS.experiment_add(ctx, p["id"], "demo-tuned", labels="demo-judgments", trainer="python train.py")
    PS.set_champion(ctx, p["id"], "demo-bm25@v1")
    PS.experiment_backtest(ctx, p["id"], e["id"])
PR.create(ctx, "Shot Gifts", description="A second project, just made")  # the card list shows a new one too
# Icons: a glyph on the shot project, and a made-up two-color logo (no real company's) on the other.
PR.set_icon(ctx, p["id"], {"kind": "glyph", "name": "store", "color": "#059669"})
from PIL import Image, ImageDraw

logo = os.path.join(os.environ["CST_HOME"], "shot-logo.png")
im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.ellipse((16, 16, 240, 240), fill=(219, 39, 119, 255))
d.rectangle((96, 70, 160, 186), fill=(255, 255, 255, 255))
im.save(logo)
PR.upload_logo(ctx, "shot-gifts", logo)
print(p["id"])
