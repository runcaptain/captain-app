// captain-ui: Querybook's chart cell, natively. Nine kinds: line, stacked area, bar, horizontal bar, pie, doughnut,
// scatter, bubble and table, drawn from a result table through a spec:
//
//   spec = { mark, x, y: [col...], series?: col (pivot by its values), agg: none|sum|avg|count|min|max|median,
//            swap?: bool (series become categories), sort: none|x|value-desc|value-asc, stack?: bool,
//            scale: linear|log, min?, max?, format: none|$|%, z?: col (bubble size), legend: right|bottom|none,
//            labels?: bool (values on the marks), connect?: bool (lines over gaps), hidden: [series], colors: {} }
//
//   const data = shape(table, spec);          // {categories, series: [{name, values, color}], points?}
//   ChartView(c, table, spec, { width, height, onToggle: (name, cx) => ... })
//
// Charts draw at most MAX categories and points: total or sample in the query first.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { PathBuilder } from "gpui-kit";
import { MONO } from "./tokens.js";
import { num as decimals } from "./num.js";

export const MAX = 400;
export const MARKS = [["line", "Line"], ["area", "Stacked area"], ["bar", "Bar"], ["hbar", "Horizontal bar"], ["pie", "Pie"], ["doughnut", "Doughnut"], ["scatter", "Scatter"], ["bubble", "Bubble"], ["table", "Table"]];
export const AGGS = [["none", "As is"], ["sum", "Sum"], ["avg", "Average"], ["count", "Count"], ["min", "Min"], ["max", "Max"], ["median", "Median"]];
// Distinct in both themes; a series may be recolored from these.
export const COLORS = ["#2F6BFF", "#7C3AED", "#0E9F6E", "#D97706", "#DC2626", "#0891B2", "#DB2777", "#64748B"];

const num = (v) => (typeof v === "number" ? v : v === null || v === undefined || v === "" ? NaN : Number(v));
const key = (v) => (v === null || v === undefined ? "(empty)" : typeof v === "object" ? JSON.stringify(v) : String(v));

function aggregate(values, agg) {
  const ns = values.map(num).filter(Number.isFinite);
  if (agg === "count") return values.length;
  if (!ns.length) return NaN;
  if (agg === "sum") return ns.reduce((a, b) => a + b, 0);
  if (agg === "avg") return ns.reduce((a, b) => a + b, 0) / ns.length;
  if (agg === "min") return Math.min(...ns);
  if (agg === "max") return Math.max(...ns);
  if (agg === "median") { const s = [...ns].sort((a, b) => a - b), m = Math.floor(s.length / 2); return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
  return ns[ns.length - 1]; // "none": the last value for that category
}

/** Rows to categories and series, per the spec. Pure: tested without a window. */
export function shape(table, spec) {
  const cols = (table?.columns ?? []).map((c) => c.name);
  const idx = (name) => cols.indexOf(name);
  const rows = table?.rows ?? [];
  const xi = idx(spec.x ?? cols[0]);
  const ys = (spec.y?.length ? spec.y : cols.filter((c, i) => i !== xi).slice(0, 1)).filter((c) => idx(c) >= 0);
  const agg = spec.agg ?? "none";
  if (xi < 0 || (!ys.length && agg !== "count")) return { categories: [], series: [] };
  if (spec.mark === "scatter" || spec.mark === "bubble") {
    const yi = idx(ys[0]), zi = spec.z ? idx(spec.z) : -1, si = spec.series ? idx(spec.series) : -1;
    const points = rows.slice(0, MAX).map((r) => ({ x: num(r[xi]), y: num(r[yi]), z: zi >= 0 ? num(r[zi]) : 1, group: si >= 0 ? key(r[si]) : ys[0] }))
      .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
    const groups = [...new Set(points.map((p) => p.group))];
    return { categories: [], series: groups.map((g, i) => ({ name: g, values: [], color: spec.colors?.[g] ?? COLORS[i % COLORS.length] })), points };
  }
  // category -> series name -> values
  const cells = new Map();
  const names = [];
  const add = (cat, name, v) => {
    if (!cells.has(cat)) cells.set(cat, new Map());
    const m = cells.get(cat);
    if (!m.has(name)) m.set(name, []);
    m.get(name).push(v);
    if (!names.includes(name)) names.push(name);
  };
  const si = spec.series ? idx(spec.series) : -1;
  for (const r of rows) {
    const cat = key(r[xi]);
    if (si >= 0) add(cat, key(r[si]), agg === "count" ? 1 : r[idx(ys[0])]);
    else for (const y of ys.length ? ys : ["count"]) add(cat, agg === "count" && !ys.length ? "count" : y, agg === "count" ? 1 : r[idx(y)]);
  }
  let categories = [...cells.keys()];
  let series = names.map((n) => ({ name: n, values: categories.map((cat) => aggregate(cells.get(cat)?.get(n) ?? [], agg)) }));
  if (spec.swap) {
    const flipped = categories.map((cat, ci) => ({ name: cat, values: series.map((s) => s.values[ci]) }));
    categories = series.map((s) => s.name);
    series = flipped;
  }
  if (spec.sort && spec.sort !== "none") {
    const total = (ci) => series.reduce((a, s) => a + (Number.isFinite(s.values[ci]) ? s.values[ci] : 0), 0);
    const order = categories.map((_, i) => i);
    if (spec.sort === "x") order.sort((a, b) => String(categories[a]).localeCompare(String(categories[b]), undefined, { numeric: true }));
    else order.sort((a, b) => (spec.sort === "value-asc" ? total(a) - total(b) : total(b) - total(a)));
    categories = order.map((i) => categories[i]);
    series = series.map((s) => ({ ...s, values: order.map((i) => s.values[i]) }));
  }
  categories = categories.slice(0, MAX);
  series = series.map((s, i) => ({ ...s, values: s.values.slice(0, MAX), color: spec.colors?.[s.name] ?? COLORS[i % COLORS.length] }));
  return { categories, series };
}

/** A number as the spec says: plain, dollars or percent, shortened when large. */
export function fmt(v, format) {
  if (!Number.isFinite(v)) return "";
  const a = Math.abs(v);
  const short = a >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : a >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : a >= 1e4 ? `${(v / 1e3).toFixed(1)}k` : Number.isInteger(v) ? String(v) : v.toFixed(a < 1 ? 3 : 2);
  if (format === "$") return `$${short}`;
  if (format === "%") return `${decimals(v * 100, 1)}%`;
  return short;
}

/** The chart for a table and a spec. Pixel geometry: pass the width it has. */
export function ChartView(c, table, spec, { width = 600, height = 260, onToggle } = {}) {
  const data = shape(table, spec);
  const hidden = new Set(spec.hidden ?? []);
  const shown = data.series.filter((s) => !hidden.has(s.name));
  const legend = spec.legend ?? (data.series.length > 1 ? "right" : "none");
  const legendView = legend === "none" || !data.series.length ? null : (legend === "right" ? v_flex() : h_flex().flex_wrap()).gap(6).text_size(11.5)
    .children(data.series.map((s) => h_flex().id(`legend-${s.name}`).gap(5).items_center().cursor_pointer()
      .text_color(hidden.has(s.name) ? c.grey300 : c.grey900)
      .when(!!onToggle, (e) => e.on_click((_e, cx) => onToggle(s.name, cx)))
      .child(div().size(9).flex_none().bg(hidden.has(s.name) ? c.grey300 : s.color)).child(div().max_w(140).truncate().child(s.name))));
  const plotW = Math.max(160, width - (legend === "right" && legendView ? 160 : 0));
  let body;
  if (spec.mark === "table") body = tableView(c, data, spec);
  else if (!shown.length && !data.points?.length) body = div().h(height).flex().items_center().justify_center().text_size(12).text_color(c.grey500).child("Nothing to draw: pick a column with numbers, or show a hidden series.");
  else if (spec.mark === "pie" || spec.mark === "doughnut") body = pie(c, data, shown, spec, plotW, height);
  else body = cartesian(c, data, shown, spec, plotW, height);
  return v_flex().w_full().gap(6)
    .child(legend === "right" && legendView ? h_flex().gap(12).items_start().child(div().flex_none().w(plotW).child(body)).child(legendView) : body)
    .when(legend === "bottom" && !!legendView, (e) => e.child(legendView))
    .when(data.categories.length >= MAX || (data.points?.length ?? 0) >= MAX, (e) => e.child(div().text_size(11).text_color(c.grey500).child(`The first ${MAX} shown: total or sample in the query for the rest.`)));
}

function tableView(c, data, spec) {
  const series = data.series;
  return div().id("chart-table").w_full().overflow_x_scrollbar().child(v_flex().border_1().border_color(c.rule).text_size(12)
    .child(h_flex().h(26).bg(c.surface2).border_b(1).border_color(c.rule).font_medium()
      .child(div().w(160).flex_none().px(8).truncate().child(spec.x ?? "")).children(series.map((s) => div().w(110).flex_none().px(8).text_right().truncate().child(s.name))))
    .children(data.categories.slice(0, 200).map((cat, ci) => h_flex().id(`ct-${ci}`).h(24).border_b(1).border_color(c.rule)
      .child(div().w(160).flex_none().px(8).truncate().child(cat))
      .children(series.map((s) => div().w(110).flex_none().px(8).text_right().font_family(MONO).child(fmt(s.values[ci], spec.format)))))));
}

/** Axis bounds and a value-to-pixel map (top of the plot is 0). Log scale keeps positive values only. */
function scaleY(values, spec, top, bottom) {
  const log = spec.scale === "log";
  const vals = values.filter((v) => Number.isFinite(v) && (!log || v > 0));
  let lo = Number.isFinite(num(spec.min)) ? num(spec.min) : Math.min(log ? Infinity : 0, ...vals);
  let hi = Number.isFinite(num(spec.max)) ? num(spec.max) : Math.max(log ? 0 : 0, ...vals);
  if (!Number.isFinite(lo)) lo = log ? 1 : 0;
  if (!Number.isFinite(hi) || hi === lo) hi = lo + 1;
  const t = (v) => (log ? Math.log10(Math.max(v, 1e-12)) : v);
  const [a, b] = [t(lo), t(hi)];
  const y = (v) => bottom - ((t(v) - a) / (b - a || 1)) * (bottom - top);
  const ticks = log ? [lo, Math.sqrt(lo * hi), hi] : [lo, (lo + hi) / 2, hi];
  return { y, ticks, lo, hi };
}

function axes(c, s, spec, L, R, top, bottom) {
  return s.ticks.flatMap((t, i) => [
    div().absolute().left(L).w(R - L).top(s.y(t)).h(1).bg(i === 0 ? c.grey300 : c.rule),
    div().absolute().left(0).w(L - 6).top(s.y(t) - 7).text_right().whitespace_nowrap().text_size(10).font_family(MONO).text_color(c.grey500).child(fmt(t, spec.format)),
  ]);
}

function cartesian(c, data, shown, spec, width, height) {
  const paint = globalThis.window?.paint_path;
  const horizontal = spec.mark === "hbar";
  const L = 58, R = width - 10, top = 8, bottom = height - 22;
  const box = div().relative().w(width).h(height).bg(c.surface).border_1().border_color(c.rule);
  const kids = [];
  if (spec.mark === "scatter" || spec.mark === "bubble") {
    const pts = data.points ?? [];
    const sy = scaleY(pts.map((p) => p.y), spec, top, bottom);
    const xs = pts.map((p) => p.x), xlo = Math.min(...xs), xhi = Math.max(...xs);
    const X = (v) => L + ((v - xlo) / (xhi - xlo || 1)) * (R - L);
    const zs = pts.map((p) => p.z).filter(Number.isFinite), zhi = Math.max(1, ...zs);
    kids.push(...axes(c, sy, spec, L, R, top, bottom));
    for (const p of pts) {
      const s = shown.find((x) => x.name === p.group);
      if (!s) continue;
      const r = spec.mark === "bubble" ? 3 + 14 * Math.sqrt(Math.max(0, p.z) / zhi) : 3;
      kids.push(div().absolute().left(X(p.x) - r).top(sy.y(p.y) - r).size(r * 2).rounded_full().bg(s.color).opacity(spec.mark === "bubble" ? 0.6 : 0.9));
    }
    kids.push(div().absolute().left(L).bottom(2).text_size(10).font_family(MONO).text_color(c.grey500).child(fmt(xlo)), div().absolute().right(10).bottom(2).text_size(10).font_family(MONO).text_color(c.grey500).child(fmt(xhi)));
    return box.children(kids);
  }
  const n = data.categories.length;
  const stack = spec.stack || spec.mark === "area";
  const totals = data.categories.map((_, ci) => shown.reduce((a, s) => a + (Number.isFinite(s.values[ci]) ? Math.max(0, s.values[ci]) : 0), 0));
  const all = stack ? totals : shown.flatMap((s) => s.values);
  if (horizontal) {
    const sx = scaleY(all, spec, 0, 1); // reuse as a fraction: 1 at lo, 0 at hi
    const rowH = Math.max(6, (bottom - top) / Math.max(1, n));
    data.categories.forEach((cat, ci) => {
      const yTop = top + ci * rowH;
      kids.push(div().absolute().left(0).w(L - 6).top(yTop + rowH / 2 - 7).text_right().truncate().text_size(10).text_color(c.grey500).child(cat));
      let acc = 0;
      shown.forEach((s, si) => {
        const v = s.values[ci];
        if (!Number.isFinite(v)) return;
        const frac = (val) => 1 - sx.y(val);
        const x0 = L + (stack ? frac(acc) : frac(Math.max(sx.lo, 0))) * (R - L), x1 = L + frac(stack ? acc + v : v) * (R - L);
        const h = stack ? rowH * 0.7 : (rowH * 0.7) / shown.length;
        const y = yTop + rowH * 0.15 + (stack ? 0 : si * h);
        kids.push(div().absolute().left(Math.min(x0, x1)).w(Math.max(1, Math.abs(x1 - x0))).top(y).h(h).bg(s.color));
        if (spec.labels) kids.push(div().absolute().left(Math.max(x0, x1) + 3).top(y + h / 2 - 6).text_size(9.5).font_family(MONO).text_color(c.grey900).child(fmt(v, spec.format)));
        if (stack) acc += v;
      });
    });
    return box.children(kids);
  }
  const sy = scaleY(all, spec, top, bottom);
  kids.push(...axes(c, sy, spec, L, R, top, bottom));
  const step = (R - L) / Math.max(1, n);
  const cx = (ci) => L + step * (ci + 0.5);
  if (spec.mark === "bar") {
    data.categories.forEach((_, ci) => {
      let acc = 0;
      shown.forEach((s, si) => {
        const v = s.values[ci];
        if (!Number.isFinite(v)) return;
        const w = stack ? step * 0.7 : (step * 0.7) / shown.length;
        const x = cx(ci) - step * 0.35 + (stack ? 0 : si * w);
        const y0 = sy.y(stack ? acc : Math.max(sy.lo, 0)), y1 = sy.y(stack ? acc + v : v);
        kids.push(div().absolute().left(x).w(Math.max(1, w - 1)).top(Math.min(y0, y1)).h(Math.max(1, Math.abs(y1 - y0))).bg(s.color));
        if (spec.labels && step > 26) kids.push(div().absolute().left(x).w(w).top(Math.min(y0, y1) - 13).text_center().text_size(9.5).font_family(MONO).text_color(c.grey900).child(fmt(v, spec.format)));
        if (stack) acc += v;
      });
    });
  } else if (paint) {
    // line, or stacked area: one path per series, gaps skipped unless "connect missing points"
    const base = data.categories.map(() => 0);
    shown.forEach((s) => {
      const tops = s.values.map((v, ci) => (Number.isFinite(v) ? (stack ? base[ci] + v : v) : NaN));
      if (spec.mark === "area") {
        const pts = tops.map((v, ci) => [cx(ci), sy.y(Number.isFinite(v) ? v : base[ci])]);
        const under = base.map((b, ci) => [cx(ci), sy.y(b)]).reverse();
        kids.push(div().absolute().inset_0().child(paint(PathBuilder.fill().add_polygon([...pts, ...under], true).build(), s.color)));
        tops.forEach((v, ci) => { if (Number.isFinite(v)) base[ci] = v; });
      } else {
        let p = PathBuilder.stroke(2), pen = false;
        tops.forEach((v, ci) => {
          if (!Number.isFinite(v)) { if (!spec.connect) pen = false; return; }
          p = pen ? p.line_to(cx(ci), sy.y(v)) : p.move_to(cx(ci), sy.y(v));
          pen = true;
        });
        kids.push(div().absolute().inset_0().child(paint(p.build(), s.color)));
      }
      if (spec.labels && n <= 40) tops.forEach((v, ci) => { if (Number.isFinite(v)) kids.push(div().absolute().left(cx(ci) - 20).w(40).top(sy.y(v) - 14).text_center().text_size(9.5).font_family(MONO).text_color(c.grey900).child(fmt(s.values[ci], spec.format))); });
    });
  }
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor((R - L) / 70))));
  data.categories.forEach((cat, ci) => { if (ci % every === 0) kids.push(div().absolute().left(cx(ci) - 34).w(68).bottom(2).text_center().truncate().text_size(10).text_color(c.grey500).child(cat)); });
  return box.children(kids);
}

function pie(c, data, shown, spec, width, height) {
  const paint = globalThis.window?.paint_path;
  const s = shown[0];
  const vals = data.categories.map((cat, ci) => ({ cat, v: Math.max(0, num(s.values[ci])) })).filter((x) => x.v > 0);
  const total = vals.reduce((a, x) => a + x.v, 0) || 1;
  const size = Math.min(width, height) - 16, r = size / 2, cx0 = size / 2 + 8, cy0 = height / 2, hole = spec.mark === "doughnut" ? r * 0.55 : 0;
  const kids = [];
  let a0 = -Math.PI / 2;
  vals.forEach((x, i) => {
    const a1 = a0 + (x.v / total) * Math.PI * 2;
    const color = spec.colors?.[x.cat] ?? COLORS[i % COLORS.length];
    if (paint) {
      const P = (a, rr) => [cx0 + rr * Math.cos(a), cy0 + rr * Math.sin(a)];
      const large = a1 - a0 > Math.PI;
      let p = PathBuilder.fill();
      const [sx, sy] = P(a0, r), [ex, ey] = P(a1, r);
      if (hole) {
        const [ix, iy] = P(a1, hole), [jx, jy] = P(a0, hole);
        p = p.move_to(sx, sy).arc_to(r, r, 0, large, true, ex, ey).line_to(ix, iy).arc_to(hole, hole, 0, large, false, jx, jy).close();
      } else p = p.move_to(cx0, cy0).line_to(sx, sy).arc_to(r, r, 0, large, true, ex, ey).close();
      kids.push(div().absolute().inset_0().child(paint(p.build(), color)));
    }
    if (spec.labels && x.v / total > 0.04) {
      const mid = (a0 + a1) / 2, lr = hole ? (r + hole) / 2 : r * 0.62;
      kids.push(div().absolute().left(cx0 + lr * Math.cos(mid) - 24).w(48).top(cy0 + lr * Math.sin(mid) - 7).text_center().text_size(10).font_family(MONO).text_color("#ffffff").child(spec.format === "%" ? fmt(x.v / total, "%") : fmt(x.v, spec.format)));
    }
    a0 = a1;
  });
  const legend = v_flex().absolute().left(size + 28).top(8).gap(4).text_size(11).children(vals.slice(0, 14).map((x, i) => h_flex().gap(5).items_center()
    .child(div().size(9).flex_none().bg(spec.colors?.[x.cat] ?? COLORS[i % COLORS.length]))
    .child(div().max_w(Math.max(60, width - size - 90)).truncate().text_color(c.grey900).child(`${x.cat}  ${fmt(x.v / total, "%")}`))));
  return div().relative().w(width).h(height).bg(c.surface).border_1().border_color(c.rule).children(kids).child(legend);
}
