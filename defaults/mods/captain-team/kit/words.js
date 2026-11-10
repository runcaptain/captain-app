// captain-ui: names from readouts and the backend, in words people read.

const MEASURES = [
  [/^(conv|conversion|cvr)$/, "Conversion"], [/^(revenue|rev|gmv|sales)$/, "Revenue"], [/^(aov)$/, "Average order value"],
  [/^(rpv|revenue_per_visit)$/, "Revenue per visit"], [/^(ctr|click|clicks)$/, "Click-through rate"],
  [/^(atc|add_to_cart|cart)$/, "Add to cart"], [/^(zero|zero_results)$/, "Searches with no results"], [/^mrr$/, "MRR"],
  [/^ndcg(@\d+)?$/, (m) => `nDCG${m[1] ?? ""}`], [/^p@(\d+)$/, (m) => `Precision@${m[1]}`], [/^r@(\d+)$/, (m) => `Recall@${m[1]}`],
  [/^judged@(\d+)$/, (m) => `Judged@${m[1]}`], [/^b_minus_a$/, "nDCG"], [/^lift$/, "Lift"],
];
const AUDIENCES = [
  [/^all$/, "all shoppers"], [/^new$/, "new shoppers"], [/^(returning|repeat)$/, "returning shoppers"], [/^mobile$/, "mobile"],
  [/^desktop$/, "desktop"], [/^search$/, "search"], [/^browse$/, "browse"], [/^(head|torso|tail)$/, (m) => `${m[0]} queries`],
];

/** A metric key in words: "arms.full~tuned+m6.all.conv_vs_ref" reads "Conversion vs the reference, all shoppers",
 *  "ndcg@10_tw" reads "nDCG@10, traffic-weighted". Unknown keys come back tidied, never invented. */
export function metricWords(key) {
  const raw = String(key ?? "");
  const vsRef = /_vs_ref|vs_ref/.test(raw);
  const weighted = /_tw\b|_tw$/.test(raw);
  const parts = raw.toLowerCase().replace(/_vs_ref/g, "").replace(/_tw\b/g, "").split(/[.~+\s]/).flatMap((p) => [p, ...p.split("_")]).filter(Boolean);
  let measure = null;
  let audience = null;
  for (const p of parts) {
    if (!measure) for (const [re, out] of MEASURES) { const m = re.exec(p); if (m) { measure = typeof out === "function" ? out(m) : out; break; } }
    if (!audience) for (const [re, out] of AUDIENCES) { const m = re.exec(p); if (m) { audience = typeof out === "function" ? out(m) : out; break; } }
  }
  if (!measure) return raw.replace(/[._~+]+/g, " ").trim();
  const rel = /\.rel$|\brel\b/.test(raw.toLowerCase()) ? " (relative)" : measure !== "Lift" && /\.lift$/.test(raw.toLowerCase()) ? " lift" : "";
  return `${measure}${rel}${vsRef ? " vs the reference" : ""}${weighted ? ", traffic-weighted" : ""}${audience ? `, ${audience}` : ""}`;
}

const WARN_ICONS = {
  "different-judgments": "git-compare", "different-metric": "git-compare", "different-mode": "git-compare",
  "different-queries": "git-compare", "different-shoppers": "git-compare", "exposure-bias": "filter",
  incomplete: "square-check", "uneven-coverage": "square-check", "not-holdout": "layers", "not-independent": "link",
  offline: "activity", "small-slices": "bar-chart-3", imported: "file-text",
};
/** The kit icon for a comparison warning's kind. */
export function warningIcon(code) {
  return WARN_ICONS[code] ?? "triangle-alert";
}
