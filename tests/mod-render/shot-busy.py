"""Exit 0 when a Captain shot's section area has content drawn in it, 1 when it is (nearly) one flat color.

The section area is between the sidebar and the terminals: x from 14% to 72% of the shot, below the top 6%.
"Drawn" means at least 6% of its pixels differ from the area's most common color, which a grey placeholder panel or
a lone "Loading" line never reaches, and a list or a grid always does.
"""
import sys
from collections import Counter

from PIL import Image

img = Image.open(sys.argv[1]).convert("RGB")
w, h = img.size
area = img.crop((int(w * 0.14), int(h * 0.06), int(w * 0.72), int(h * 0.98))).resize((400, 400))
px = list(area.get_flattened_data() if hasattr(area, 'get_flattened_data') else area.getdata())
top, n = Counter(px).most_common(1)[0]
busy = 1 - n / len(px)
print(f"{sys.argv[1]}: {busy:.1%} of the section differs from its background")
sys.exit(0 if busy >= 0.06 else 1)
