// CAPTAIN: the small words and marks a deployed model's card and page are written with: how long ago, money,
// milliseconds, counts, a row of thin bars. Shared, so a model reads the same on every screen that shows it.
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

/** "Updated just now", "Updated 12 s ago", "Updated 3 min ago": when what is on screen was read. */
export function updatedWords(readAt) {
  if (!readAt) return "";
  const s = Math.max(0, Math.round((Date.now() - readAt) / 1000));
  return s < 5 ? "Updated just now" : s < 60 ? `Updated ${s} s ago` : `Updated ${Math.floor(s / 60)} min ago`;
}

/** The small control at the right of a title: when the screen was last read, or that a cloud could not be reached,
 *  and a refresh icon. The screen keeps itself current, so this is quiet. The icon says what it is to a screen
 *  reader and on hover. */
export function Updated(c, id, { readAt, problem, onRefresh, icon: mark, live = false }) {
  return h_flex().id(id).flex_none().gap(8).items_center()
    .child(div().id(`${id}-text`).text_size(12).text_color(problem ? c.fail : c.grey500).child(problem ?? (live ? "Live" : updatedWords(readAt))))
    .child(div().id(`${id}-refresh`).size(24).flex().flex_none().items_center().justify_center().cursor_pointer().text_color(c.grey500)
      .hover((x) => x.text_color(c.navy)).accessibility_label("Refresh").tooltip("Refresh").on_click(onRefresh).child(mark));
}

/** A running time as a row says it: "48 s", "2 min 10 s". The same words the services use. */
export function spanWords(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return s < 90 ? `${s} s` : s % 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 60)} min`;
}

/** A deploy under way, in one sentence, on this page's own clock so it ticks every second without a read:
 *  "Deploying to AWS: Building the model's image, 48 s so far (usually about a minute and a half)." */
export function deployingWords(g, now = Date.now()) {
  const step = g?.step ?? {};
  const began = step.started_at ? new Date(step.started_at).getTime() : NaN;
  return `Deploying to ${g?.label ?? "the cloud"}: ${step.title ?? "working"}`
    + (isFinite(began) ? `, ${spanWords((now - began) / 1000)} so far` : "")
    + (step.typical ? ` (usually ${step.typical}).` : ".");
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
export function BarChart(c, id, values, { height = 44, color, format = (v) => String(v) } = {}) {
  const nums = values.map((v) => (v == null ? null : Number(v)));
  const known = nums.filter((v) => v != null && isFinite(v));
  const top = Math.max(1e-9, ...known);
  // At most 60 bars: a chart is half a column wide, and more than that leaves each under a pixel.
  const step = Math.max(1, Math.ceil(nums.length / 60));
  const bars = [];
  for (let i = 0; i < nums.length; i += step) {
    const part = nums.slice(i, i + step).filter((v) => v != null);
    bars.push(part.length ? Math.max(...part) : null);
  }
  // The highest value is said in the chart's own line of words, so the chart takes the full width it is given.
  return h_flex().id(id).w_full().h(height).gap(1).items_end().border_b(1).border_color(c.rule)
    .children(bars.map((v) => div().flex_1().h(v == null ? 0 : Math.max(1, Math.round((v / top) * (height - 2)))).bg(color ?? c.blue)));
}
