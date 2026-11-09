#!/usr/bin/env python3
"""Shot check (SHOT_PROJECT=1): a project, and a model registry on disk with two brands, one of them the project's, so
the project home shows models and the Choose models picker has something to pick. Its inputs are pinned (queries at an
older version, so a card shows "Newer version available"), the baseline is the champion, and the tuned ranker is an
experiment trained on the holdout's own judgments and judged against it, so the page shows a filled Experiments table
with the "agrees with its own labeler" row. Prints the project's id. Run with cst's own Python and CST_HOME."""
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
p = PR.create(ctx, "Shot Project", brands=["shop"])
PR.assign(ctx, p["id"], [{"kind": "dataset", "name": "demo-catalog"}])

from captain_search import project_state as PS

PS.pin(ctx, p["id"], {"catalog": "demo-catalog", "queries": "demo-queries@v1", "holdout": "demo-queries", "labels": "demo-judgments"})
PS.experiment_add(ctx, p["id"], "demo-bm25")
e = PS.experiment_add(ctx, p["id"], "demo-tuned", labels="demo-judgments", trainer="python train.py")
PS.set_champion(ctx, p["id"], "demo-bm25@v1")
PS.experiment_backtest(ctx, p["id"], e["id"])
print(p["id"])
