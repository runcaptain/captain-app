"""Exit 0 when every vendor mark in the marks strip drew, 1 when one is blank.

shot-check.sh opens a helper dialog that draws six marks at 32 px, one per 64 px cell, on a pure magenta
background no real screen uses: AWS, Azure, Amazon Redshift, Amazon Athena and SQL Server (the five full-colour
ones, drawn through image()), then Snowflake (a tinted one, drawn through svg(), as the control). This finds the
magenta strip in the shot, cuts it into six cells, and counts the pixels in each that are not magenta. A drawn mark
covers 10% to 25% of its cell. A blank one covers nothing.

Why it exists: on 2026-11-12 the five full-colour marks were blank in every picture from the test window and
nobody could tell from a pass. A full-colour mark is loaded in the background and is blank the first time a screen
shows it. shot-check.sh opens this strip once after launch to load them, and again at the end for this check, so a
blank here means a mark that cannot be drawn at all: a missing file, a file the app can't read, a broken decoder.
"""
import sys

from PIL import Image

NAMES = ["AWS", "Azure", "Amazon Redshift", "Amazon Athena", "SQL Server", "Snowflake (the control)"]
img = Image.open(sys.argv[1]).convert("RGB")
w, h = img.size
px = img.load()
magenta = lambda p: p[0] > 235 and p[1] < 25 and p[2] > 235  # noqa: E731
xs, ys = [], []
for y in range(0, h, 2):
    for x in range(0, w, 2):
        if magenta(px[x, y]):
            xs.append(x)
            ys.append(y)
if len(xs) < 2000:
    print(f"{sys.argv[1]}: the marks strip is not in the picture, so the marks were not checked")
    sys.exit(1)
x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
cell = (x1 - x0) / len(NAMES)
blank = []
for i, name in enumerate(NAMES):
    a, b = int(x0 + i * cell), int(x0 + (i + 1) * cell)
    total = other = 0
    for y in range(y0, y1, 2):
        for x in range(a, b, 2):
            total += 1
            other += 0 if magenta(px[x, y]) else 1
    share = other / max(1, total)
    if share < 0.03:
        blank.append(name)
    print(f"  {name}: {share:.0%} of its cell is drawn")
if blank == [NAMES[-1]] or NAMES[-1] in blank and len(blank) == len(NAMES):
    print("the control mark is blank too: the strip itself did not draw, so this says nothing about the marks")
    sys.exit(1)
if blank:
    print(f"{sys.argv[1]}: drew blank: {', '.join(blank)}. They were loaded once before this picture, so this is not the "
          "first-show blank: the mark's file is missing, unreadable, or the app can't decode it.")
    sys.exit(1)
print(f"{sys.argv[1]}: all six marks drew")
