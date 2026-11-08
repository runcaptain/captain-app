#!/usr/bin/env python3
"""Shot check: a catalog whose image column holds bare file names ("main.jpg"), with the pictures in
images/<pid>/main.jpg next to it, so the Sample data tab finds them, shows thumbnails, and offers to keep the address.
Run with cst's own Python and CST_HOME."""
import json
import os

from PIL import Image, ImageDraw

from captain_search import external
from captain_search.model.entities import DatasetKind, ExternalFile
from captain_search.workspace import open_context

ctx = open_context()
d = os.path.join(os.environ["CST_HOME"], "pictures")
colors = [(220, 60, 50), (40, 110, 210), (40, 160, 90), (230, 170, 30), (130, 70, 180), (30, 30, 40)]
rows = []
for i in range(40):
    pid = f"p{i:04d}"
    os.makedirs(os.path.join(d, "images", pid), exist_ok=True)
    im = Image.new("RGB", (600, 600), colors[i % len(colors)])
    ImageDraw.Draw(im).ellipse((150, 150, 450, 450), fill=(250, 250, 250))
    im.save(os.path.join(d, "images", pid, "main.jpg"))
    rows.append({"pid": pid, "title": f"Product {i}", "imgs": ["main.jpg"], "price": round(5 + i * 1.3, 2)})
with open(os.path.join(d, "items.jsonl"), "w") as f:
    f.write("\n".join(json.dumps(r) for r in rows))
p = os.path.join(d, "items.jsonl")
external.register(ctx, "shot-pictures", [], DatasetKind.catalog, extra_files=[ExternalFile(uri=p, bytes=os.path.getsize(p))],
                  description="Pictures by file name, for image previews.", row_count=len(rows))
print("shot-pictures: 40 rows")
