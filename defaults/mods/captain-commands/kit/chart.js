// captain-ui: a chart from a table that is already totalled: bars, a line, or points (scatter). Draws at most
// MAX points, so a big result never stalls a frame: total it in SQL or Python first.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { PathBuilder } from "gpui-kit";
import { MONO } from "./tokens.js";

export const MAX = 500;

const num = (v) => (typeof v === "number" ? v : v === null || v === undefined || v === "" ? NaN : Number(v));
const short = (v) => {
  const n = Math.abs(v);
  if (n >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
};

/** The points a chart draws: x labels or numbers, y numbers, from the named columns. */
export function series(table, x, y) {
  const cols = (table?.columns ?? []).map((c) => c.name);
  const xi = cols.indexOf(x), yi = cols.indexOf(y);
  if (xi < 0 || yi < 0) return [];
  return (table.rows ?? []).slice(0, MAX).map((r) => ({ x: r[xi], y: num(r[yi]) })).filter((p) => Number.isFinite(p.y));
}

/** Chart(c, {mark: "bar" | "line" | "point", points: [{x, y}], xLabel, yLabel}, {height}) */
export function Chart(c, { mark = "bar", points = [], xLabel = "", yLabel = "" }, { height = 220 } = {}) {
  const box = div().w_full().h(height).relative().bg(c.surface).border_1().border_color(c.rule);
  if (!points.length) return box.child(div().absolute().inset_0().flex().items_center().justify_center().text_size(12).text_color(c.grey500).child("Nothing to draw: pick columns with numbers."));
  const ys = points.map((p) => p.y);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const span = hi - lo || 1;
  const Y = (v) => (1 - (v - lo) / span) * 88 + 6; // percent from the top, with a margin
  const numericX = mark === "point" && points.every((p) => Number.isFinite(num(p.x)));
  const xs = numericX ? points.map((p) => num(p.x)) : null;
  const xlo = xs ? Math.min(...xs) : 0, xspan = xs ? Math.max(...xs) - xlo || 1 : 1;
  const X = (i) => (xs ? ((xs[i] - xlo) / xspan) * 92 + 4 : ((i + 0.5) / points.length) * 92 + 4);
  const plot = div().absolute().top(0).bottom(18).left(44).right(8);
  const paint = globalThis.window?.paint_path;
  if (mark === "bar") {
    const w = Math.max(0.4, (92 / points.length) * 0.7);
    plot.children(points.map((p, i) => div().absolute().left(`${X(i) - w / 2}%`).w(`${w}%`)
      .top(`${Math.min(Y(p.y), Y(0))}%`).h(`${Math.abs(Y(p.y) - Y(0))}%`).bg(p.y < 0 ? c.fail : c.blue)));
  } else if (paint && mark === "line" && points.length > 1) {
    let path = PathBuilder.stroke(2);
    points.forEach((p, i) => { path = i === 0 ? path.move_to(`${X(i)}%`, `${Y(p.y)}%`) : path.line_to(`${X(i)}%`, `${Y(p.y)}%`); });
    plot.child(div().absolute().inset_0().child(paint(path.build(), c.blue)));
  } else {
    plot.children(points.map((p, i) => div().absolute().left(`${X(i)}%`).top(`${Y(p.y)}%`).size(6).ml(-3).mt(-3).rounded_full().bg(c.blue)));
  }
  const ticks = [hi, (hi + lo) / 2, lo];
  const first = String(points[0].x ?? ""), last = String(points[points.length - 1].x ?? "");
  return v_flex().w_full().gap(4)
    .child(box
      .child(div().absolute().left(44).right(8).top(`${Y(0) * (1 - 18 / height)}%`).h(1).bg(c.rule))
      .children(ticks.map((t) => div().absolute().left(4).w(36).top(`${Y(t) * (1 - 18 / height)}%`).mt(-7).text_right().text_size(10).font_family(MONO).text_color(c.grey500).child(short(t))))
      .child(plot)
      .child(h_flex().absolute().left(44).right(8).bottom(2).text_size(10).font_family(MONO).text_color(c.grey500)
        .child(div().min_w(0).truncate().child(first)).child(div().flex_1()).child(div().min_w(0).truncate().child(points.length > 1 ? last : ""))))
    .child(h_flex().gap(10).text_size(11).text_color(c.grey500)
      .child(div().child(`${yLabel} by ${xLabel}`))
      .when(points.length >= MAX, (e) => e.child(div().child(`first ${MAX} rows`))));
}
