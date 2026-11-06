#!/usr/bin/env python3
"""CAPTAIN: is a captain-forms dialog drawn? Usage: forms-shot.py <shot.png> [--verbose]
Exit 0 when the dialog shows a form; 1 when it shows "This view could not be rendered" (the red banner of
gpui-kit's error view) or its body is mostly blank. Light appearance only."""
import sys
from PIL import Image

def analyze(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    # The dialog is the one large pure-white box: the dimmed window behind it has no pure white, and a toast is
    # small. Columns that are mostly white give its sides, then rows inside those columns give its top and bottom.
    def longest(flags):
        best, cur, bs, st = 0, 0, 0, 0
        for i, f in enumerate(flags):
            if f:
                if cur == 0: st = i
                cur += 1
                if cur > best: best, bs = cur, st
            else:
                cur = 0
        return bs, bs + best
    white = lambda x, y: min(px[x, y]) >= 250
    cols = [sum(white(x, y) for y in range(0, h, 4)) > (h / 4) * 0.3 for x in range(w)]
    left, right = longest(cols)
    if right - left < w * 0.2:
        return {"ok": False, "why": "no dialog found"}
    # Its side padding is white from top to bottom, broken only by the 1 px header and footer rules.
    scale = (right - left) / 560
    m = max(2, int(6 * scale))
    rws = [white(left + m, y) and white(right - 1 - m, y) for y in range(h)]
    for y in range(1, h - 3):
        if not rws[y] and rws[y - 1] and any(rws[y + 1:y + 4]):
            rws[y] = True
    top, bottom = longest(rws)
    dh = bottom - top
    # Body: skip the header (~56 px) and footer (~60 px), at the shot's scale (the dialog is 560 px wide at 1x).
    b0, b1 = int(top + 60 * scale), int(bottom - 64 * scale)
    red = dark = total = 0
    for y in range(max(b0, top), max(b1, b0 + 1), 2):
        for x in range(left, right, 2):
            r, g, b = px[x, y]
            total += 1
            if r > 150 and g < 90 and b < 90:
                red += 1
            if 0.3 * r + 0.59 * g + 0.11 * b < 150:
                dark += 1
    ink = dark / total if total else 0
    errored = red > 200
    blank = ink < 0.004
    return {"ok": not errored and not blank, "why": "error banner" if errored else "mostly blank" if blank else "drawn",
            "ink": round(ink, 4), "red": red, "box": [left, top, right, bottom]}

if __name__ == "__main__":
    r = analyze(sys.argv[1])
    if "--verbose" in sys.argv:
        print(sys.argv[1], r)
    sys.exit(0 if r["ok"] else 1)
