"""Exit 0 when a Captain shot's section shows real content, 1 when it is blank or still a loading skeleton.

Looks at the section's detail column (x 35% to 63% of the shot, below the top 6%: right of the section's own list,
left of the terminals), and counts pixels in strong contrast with the column's most common color: text, icons,
borders. A loading skeleton is faint bars and scores 0%. Calibrated on real shots, 2026-11-06: skeleton 0.0%, an
empty state with one sentence and a button 3.5%, a dataset grid 6.1%, a query side by side 6.4%. Passing takes 1.5%.
"""
import sys
from collections import Counter

from PIL import Image

img = Image.open(sys.argv[1]).convert("RGB")
w, h = img.size
area = img.crop((int(w * 0.35), int(h * 0.06), int(w * 0.63), int(h * 0.98))).resize((300, 500))
px = list(area.get_flattened_data() if hasattr(area, "get_flattened_data") else area.getdata())
top = Counter(px).most_common(1)[0][0]
strong = sum(1 for p in px if sum(abs(p[i] - top[i]) for i in range(3)) > 150) / len(px)
print(f"{sys.argv[1]}: {strong:.1%} of the detail column is content")
sys.exit(0 if strong >= 0.015 else 1)
