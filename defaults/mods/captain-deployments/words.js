// CAPTAIN: the words and the small chart the Deployments screens share.
import { div } from "gpui-kit";
import { h_flex } from "gpui-base";

/** "3 min ago", from a time. Null when there is none. */
export function ago(t) {
  if (!t) return null;
  const s = (Date.now() - new Date(t).getTime()) / 1000;
  if (!isFinite(s)) return null;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const days = Math.floor(s / 86400);
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

export function money(x) {
  const v = Number(x);
  if (!isFinite(v)) return "";
  return v === 0 ? "$0.00" : v < 0.01 ? "under $0.01" : `$${v.toFixed(2)}`;
}

export function ms(x) {
  const v = Number(x);
  if (!isFinite(v)) return "";
  return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : v >= 10 ? `${Math.round(v)} ms` : `${v.toFixed(1)} ms`;
}

/** 4876 as "4,876". Written out, because the window's own number formatting adds no separators. */
export function count(x) {
  const n = Math.round(Number(x ?? 0));
  if (!isFinite(n)) return "0";
  return String(Math.abs(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",").replace(/^/, n < 0 ? "-" : "");
}

/** A row of thin bars: counts over time, oldest first. Drawn with plain boxes, so it shows in every window. */
export function Spark(c, id, values, { width = 96, height = 22, color } = {}) {
  const top = Math.max(1, ...values.map((v) => Number(v) || 0));
  return h_flex().id(id).w(width).h(height).flex_none().gap(3).items_end().justify_end()
    .children(values.map((v) => div().w(4).flex_none().h(Math.max(2, Math.round(((Number(v) || 0) / top) * height))).bg((Number(v) || 0) > 0 ? (color ?? c.blue) : c.rule)));
}

/** The same, large, with the highest value said beside it. `values` may hold nulls: a gap, not a zero. */
export function BarChart(c, id, values, { height = 80, color, format = (v) => String(v) } = {}) {
  const nums = values.map((v) => (v == null ? null : Number(v)));
  const known = nums.filter((v) => v != null && isFinite(v));
  const top = Math.max(1e-9, ...known);
  // At most 96 bars: more than that and each is under a pixel wide.
  const step = Math.max(1, Math.ceil(nums.length / 96));
  const bars = [];
  for (let i = 0; i < nums.length; i += step) {
    const part = nums.slice(i, i + step).filter((v) => v != null);
    bars.push(part.length ? Math.max(...part) : null);
  }
  return h_flex().id(id).w_full().gap(8).items_end()
    .child(h_flex().flex_1().h(height).gap(1).items_end().border_b(1).border_color(c.rule)
      .children(bars.map((v) => div().flex_1().h(v == null ? 0 : Math.max(1, Math.round((v / top) * (height - 2)))).bg(color ?? c.blue))))
    .child(div().w(120).flex_none().text_size(12).text_color(c.grey500).child(known.length ? `up to ${format(top)}` : "no numbers"));
}
