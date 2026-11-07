#!/usr/bin/env python3
"""CAPTAIN: are the gallery's charts drawn? Usage: charts-shot.py <shot.png> [--verbose]
tests/mod-render/charts-gallery frames each chart in magenta. A chart passes when its coloured ink (saturated pixels:
series, bars, slices, points) spans at least a quarter of its frame's height and half its width; a line drawn flat
along the top fails. The one cyan frame holds the flat-line bug on purpose and must fail, or the check itself is
broken. Exit 0 when every magenta frame passes and the cyan one is caught."""
import sys
from PIL import Image

def frames(px, w, h, colour):
    near = lambda p: abs(p[0] - colour[0]) < 40 and abs(p[1] - colour[1]) < 40 and abs(p[2] - colour[2]) < 40
    seen, out = set(), []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if (x, y) in seen or not near(px[x, y]):
                continue
            stack, xs, ys = [(x, y)], [], []
            seen.add((x, y))
            while stack:
                a, b = stack.pop()
                xs.append(a); ys.append(b)
                for da, db in ((2, 0), (-2, 0), (0, 2), (0, -2)):
                    n = (a + da, b + db)
                    if 0 <= n[0] < w and 0 <= n[1] < h and n not in seen and near(px[n]):
                        seen.add(n); stack.append(n)
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            if x1 - x0 > 60 and y1 - y0 > 40:
                out.append((x0, y0, x1, y1))
    return out

def ink(px, box):
    x0, y0, x1, y1 = box
    pad = 8
    xs, ys = [], []
    for y in range(y0 + pad, y1 - pad):
        for x in range(x0 + pad, x1 - pad):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) > 70 and not (r > 200 and b > 200 and g < 80) and not (g > 200 and b > 200 and r < 80):
                xs.append(x); ys.append(y)
    if not xs:
        return 0.0, 0.0
    return (max(ys) - min(ys)) / max(1, (y1 - y0 - 2 * pad)), (max(xs) - min(xs)) / max(1, (x1 - x0 - 2 * pad))

def main():
    path = sys.argv[1]
    verbose = "--verbose" in sys.argv
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    ok = True
    good = frames(px, w, h, (255, 0, 255))
    test = frames(px, w, h, (0, 255, 255))
    if len(good) < 14:
        print(f"charts: found {len(good)} chart frames, expected 14"); ok = False
    for i, box in enumerate(sorted(good, key=lambda b: (b[1] // 50, b[0]))):
        v, hz = ink(px, box)
        drawn = v >= 0.25 and hz >= 0.5
        if verbose or not drawn:
            print(f"chart {i + 1} at {box[0]},{box[1]}: ink spans {v:.0%} of the height, {hz:.0%} of the width{'' if drawn else ': NOT DRAWN'}")
        ok &= drawn
    if len(test) != 1:
        print(f"charts: SELF-TEST FAILED, found {len(test)} cyan frames"); ok = False
    else:
        v, hz = ink(px, test[0])
        if v >= 0.25:
            print(f"charts: SELF-TEST FAILED, the flat-line bug drew {v:.0%} tall and wasn't caught"); ok = False
        elif verbose:
            print(f"self-test: the flat line is caught ({v:.0%} tall)")
    print("charts: all drawn" if ok else f"charts: NOT ALL DRAWN ({path})")
    return 0 if ok else 1

sys.exit(main())
