#!/usr/bin/env python3
"""Shot check (SHOT_PROJECT=1): a project, and a model registry on disk with two brands, one of them the project's, so
the project home shows models and the Choose models picker has something to pick. Prints the project's id. Run with
cst's own Python and CST_HOME."""
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
print(p["id"])
