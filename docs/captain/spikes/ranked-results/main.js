// CAPTAIN spike (not shipped): can a native gpui-kit mod hold 100,000 ranked
// results and a chart? See docs/captain/RISKS.md. Install by copying this
// folder to ~/.config/captain-app/mods/ranked-results, then
// `captain-app do mod.open '{"name":"ranked-results"}'`.
// It measures itself and shows the numbers in its header.
import { View, div } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar } from "gpui-base";
import { theme } from "herdr";

const N = 100000;
const ROW = 24;
const SLICES = ["head", "torso", "tail", "brand", "category", "typo", "long", "numeric", "color", "size", "gift", "sale"];

// Deterministic, so two runs are comparable.
function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

function generate() {
  const r = rng(7);
  const rows = new Array(N);
  for (let i = 0; i < N; i++) {
    const slice = SLICES[Math.floor(r() * SLICES.length)];
    rows[i] = { id: i, query: `${slice} query ${Math.floor(r() * 5000)}`, doc: `doc-${Math.floor(r() * 1e6)}`,
      score: r(), ndcg: r() * 0.6 + 0.3, slice };
  }
  return rows;
}

export default class RankedResults extends View {
  init(_props, cx) {
    let t0 = Date.now();
    this.rows = generate();
    this.genMs = Date.now() - t0;
    t0 = Date.now();
    this.chart = this.sliceMeans();
    this.chartMs = Date.now() - t0;
    this.sortKey = null;
    this.sortMs = null;
    this.stats = { calls: 0, totalMs: 0, maxMs: 0, rows: 0, lastAt: 0, gaps: [] };
    // Refresh the numbers twice a second (this redraw is part of what's measured).
    cx.timer.every(500, (cx) => cx.notify());
  }

  sliceMeans() {
    const sum = new Map(), count = new Map();
    for (const r of this.rows) {
      sum.set(r.slice, (sum.get(r.slice) ?? 0) + r.ndcg);
      count.set(r.slice, (count.get(r.slice) ?? 0) + 1);
    }
    return SLICES.map((s) => ({ slice: s, mean: sum.get(s) / count.get(s), n: count.get(s) }));
  }

  sort(key, cx) {
    const t0 = Date.now();
    const dir = this.sortKey === key ? -1 : 1;
    this.rows.sort((a, b) => (a[key] < b[key] ? -dir : a[key] > b[key] ? dir : 0));
    this.sortKey = this.sortKey === key ? `-${key}` : key;
    this.sortMs = Date.now() - t0;
    cx.notify();
  }

  // Called by the list during layout for the visible window only.
  window(range, t) {
    const t0 = Date.now();
    const out = this.rows.slice(range.start, range.end).map((r, i) =>
      h_flex().h(ROW).px(10).gap(12).items_center().text_size(12)
        .when((range.start + i) % 2 === 1, (e) => e.bg(t.raised))
        .child(div().w(70).text_color(t.dim).child(String(range.start + i + 1)))
        .child(div().w(90).child(r.slice))
        .child(div().flex_1().min_w(0).truncate().child(r.query))
        .child(div().w(110).text_color(t.muted).child(r.doc))
        .child(div().w(70).child(r.score.toFixed(4)))
        .child(div().w(70).child(r.ndcg.toFixed(4))));
    const ms = Date.now() - t0, s = this.stats;
    s.calls++; s.totalMs += ms; s.maxMs = Math.max(s.maxMs, ms); s.rows = range.end - range.start;
    if (s.lastAt) { s.gaps.push(t0 - s.lastAt); if (s.gaps.length > 60) s.gaps.shift(); }
    s.lastAt = t0;
    return out;
  }

  header(t, cx) {
    const col = (w, label, key) => div().w(w).when(w === 0, (e) => e.flex_1()).cursor_pointer().hover((s) => s.text_color(t.foreground))
      .on_click((_e, cx) => key && this.sort(key, cx)).child(label + (this.sortKey === key ? " (asc)" : this.sortKey === `-${key}` ? " (desc)" : ""));
    return h_flex().h(28).flex_none().px(10).gap(12).items_center().text_size(12).font_semibold().text_color(t.muted).border_b(1).border_color(t.border)
      .child(col(70, "Rank")).child(col(90, "Slice", "slice")).child(col(0, "Query", "query")).child(col(110, "Doc"))
      .child(col(70, "Score", "score")).child(col(70, "nDCG", "ndcg"));
  }

  render(cx) {
    const t = theme();
    const s = this.stats;
    const gaps = s.gaps.slice().sort((a, b) => a - b);
    const p50 = gaps.length ? gaps[Math.floor(gaps.length / 2)] : null;
    const max = Math.max(...this.chart.map((c) => c.mean));
    const line = `${N.toLocaleString()} rows made in ${this.genMs} ms, chart in ${this.chartMs} ms`
      + (this.sortMs != null ? `, last sort ${this.sortMs} ms` : "")
      + ` | row window: ${s.rows} rows, ${s.calls ? (s.totalMs / s.calls).toFixed(2) : "-"} ms avg, ${s.maxMs} ms max, ${s.calls} calls`
      + (p50 != null ? ` | frame gap p50 ${p50} ms` : "");
    return v_flex().size_full().bg(t.background).text_color(t.foreground)
      .child(div().flex_none().px(12).py(8).text_size(12).text_color(t.dim).border_b(1).border_color(t.border).child(line))
      .child(h_flex().flex_none().h(150).px(12).py(10).gap(6).items_end().border_b(1).border_color(t.border)
        .children(this.chart.map((c) => v_flex().flex_1().h_full().justify_end().items_center().gap(4)
          .child(div().text_size(10).text_color(t.dim).child(c.mean.toFixed(3)))
          .child(div().w_full().h(Math.max(2, (c.mean / max) * 100)).rounded(3).bg(t.accent))
          .child(div().text_size(10).text_color(t.muted).child(c.slice)))))
      .child(this.header(t, cx))
      .child(div().flex_1().min_h(0).relative()
        .child(v_virtual_list("ranked", this.rows.length, ROW, (i) => String(this.rows[i].id), (range) => this.window(range, t)).size_full())
        .child(Scrollbar.vertical("ranked").viewport_from_layout().absolute().inset_0()));
  }
}
