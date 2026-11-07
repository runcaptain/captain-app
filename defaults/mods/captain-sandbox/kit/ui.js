// captain-ui components. Each takes `c`, the palette from tokens.js, so a screen
// follows light and dark. They return gpui-kit elements; Table keeps state.
import { div, PathBuilder } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar } from "gpui-base";
import { FONT, MONO } from "./tokens.js";
import { retry, details } from "./cst.js";
import { int } from "./num.js";

/** A bordered panel with an optional title row and actions. */
export function Card(c, { title, actions, pad = 16 } = {}, ...children) {
  return v_flex().w_full().bg(c.surface).border_1().border_color(c.rule)
    .when(Boolean(title) || Boolean(actions), (e) => e.child(h_flex().h(40).flex_none().px(pad).gap(8).items_center().border_b(1).border_color(c.rule)
      .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().text_color(c.navy).child(title ?? ""))
      .children(actions ?? [])))
    .child(v_flex().p(pad).gap(10).children(children.flat()));
}

const TONES = { neutral: ["surface2", "grey900"], blue: ["blueLight", "blue"], good: ["goodLight", "good"], warn: ["warnLight", "warn"], bad: ["failLight", "fail"] };

/** A small square label. tone: neutral, blue, good, warn or bad. */
export function Badge(c, text, tone = "neutral") {
  const [bg, fg] = TONES[tone] ?? TONES.neutral;
  return div().flex_none().px(6).h(20).flex().items_center().bg(c[bg]).text_color(c[fg]).text_size(11).font_medium().child(String(text));
}

/** The tone a state word gets, the same everywhere. */
export function toneOf(state) {
  const s = String(state ?? "").toLowerCase();
  if (/(fail|error|reject|blocked|problem|refused)/.test(s)) return "bad";
  if (/(done|decided|ok|ready|passed|live|linked|approved|finished)/.test(s)) return "good";
  if (/(running|queued|working|review|pending|started)/.test(s)) return "blue";
  if (/(inconclusive|warn|stale|draft|missing|unchecked)/.test(s)) return "warn";
  return "neutral";
}

/** A versioned thing ("makro-catalog@v3") as its name and a version chip, never raw syntax. */
export function RefChip(c, ref) {
  const m = String(ref ?? "").match(/^(.*)@v?(\d+)$/);
  const name = m ? m[1] : String(ref ?? "");
  return h_flex().min_w(0).gap(6).items_center()
    .child(div().min_w(0).truncate().text_size(13).text_color(c.navy).child(name))
    .when(Boolean(m), (e) => e.child(Badge(c, `v${m[2]}`, "neutral")));
}

/** A button. kind: primary, secondary or quiet. */
export function Button(c, id, label, onClick, { kind = "secondary", disabled = false, small = false } = {}) {
  const h = small ? 26 : 32;
  return div().id(id).h(h).px(small ? 10 : 14).flex().flex_none().items_center().justify_center().gap(6).text_size(small ? 12 : 13).font_medium()
    .when(disabled, (e) => e.bg(c.surface2).text_color(c.grey500).border_1().border_color(c.rule))
    .when(!disabled && kind === "primary", (e) => e.bg(c.blue).text_color(c.onBlue).cursor_pointer())
    .when(!disabled && kind === "secondary", (e) => e.bg(c.surface).border_1().border_color(c.grey300).text_color(c.navy).cursor_pointer().hover((s) => s.border_color(c.blue).text_color(c.blue)))
    .when(!disabled && kind === "quiet", (e) => e.text_color(c.blue).cursor_pointer().hover((s) => s.bg(c.blueLight)))
    .when(!disabled, (e) => e.on_click(onClick))
    .child(label);
}

/** Tabs along a 1px rule; the active one has a 2px blue underline. */
export function Tabs(c, id, tabs, active, onSelect) {
  return h_flex().h(36).flex_none().gap(18).border_b(1).border_color(c.rule).children(tabs.map((t) => {
    const on = t.id === active;
    return h_flex().id(`${id}-${t.id}`).h_full().gap(6).items_center().cursor_pointer().border_b(2).border_color(on ? c.blue : "#00000000")
      .text_size(13).font_medium().text_color(on ? c.blue : c.grey900).hover((s) => s.text_color(c.navy))
      .on_click((_e, cx) => onSelect(t.id, cx))
      .child(t.label).when(t.count != null, (e) => e.child(div().text_size(11).text_color(c.grey500).child(String(t.count))));
  }));
}

/** Nothing to show yet: what this is and the one thing to do. */
export function Empty(c, title, body, action) {
  return v_flex().w_full().py(40).px(24).gap(8).items_center()
    .child(div().text_size(14).font_semibold().text_color(c.navy).child(title))
    .when(Boolean(body), (e) => e.child(div().max_w(460).text_size(13).line_height("19px").text_color(c.grey500).child(body)))
    .when(Boolean(action), (e) => e.child(div().pt(6).child(action)));
}

/** Something failed: what, in the daemon's words, and a retry. */
/** An error as people should read it: the backend's sentence, or `fallback` when what came back isn't one (a storage
 *  key or path like "backtest-runs/x/v1/MANIFEST.yaml", a bare "Not Found" or "HTTP 500", or a tool's or transport's
 *  output). The same rule desktop-app's screens use (captain-data say()). */
export function plainError(message, fallback = "Something went wrong.") {
  const e = String(message ?? "").trim();
  if (!e) return fallback;
  if (/^[\w.@-]+(\/[\w.@-]+)+$/.test(e) || /\.(ya?ml|json|parquet|csv)$/i.test(e)) return fallback;
  if (/^HTTP \d+$/.test(e) || /^(Not Found|Method Not Allowed|Internal Server Error|Bad Request)$/i.test(e)) return fallback;
  // "engine" is a product word now (a customer's search system), so only transport and backend talk is hidden.
  if (/curl|127\.0|localhost|\bport \d|error sending|traceback|daemon/i.test(e)) return fallback;
  if (/time ?out|timed out/i.test(e)) return "This took too long.";
  return e;
}

export function ErrorState(c, message, retry) {
  return h_flex().w_full().p(12).gap(10).items_start().bg(c.failLight).border_1().border_color(c.fail)
    .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.fail).child(plainError(message)))
    .when(Boolean(retry), (e) => e.child(retry));
}

/** Grey placeholder bars where content will be: no words while things load. */
export function Skeleton(c, rows = 6) {
  return v_flex().w_full().p(12).gap(10).children([...Array(rows)].map((_, i) => h_flex().gap(12)
    .child(div().h(12).w(`${38 + ((i * 17) % 40)}%`).bg(c.surface2))
    .child(div().h(12).w(`${12 + ((i * 7) % 14)}%`).bg(c.surface2))));
}

/** Waiting for data. While Captain's services start (starting), a quiet skeleton. */
export function Loading(c, label = "Loading...", starting = false) {
  if (starting) return Skeleton(c);
  return h_flex().w_full().py(24).gap(10).items_center().justify_center()
    .child(div().size(7).rounded_full().bg(c.blue))
    .child(div().text_size(13).text_color(c.grey500).child(label));
}

/** Captain's services didn't come up: one plain sentence, Retry, and Copy details. */
export function ServiceError(c, message, onRetry) {
  return v_flex().w_full().p(14).gap(10).bg(c.failLight).border_1().border_color(c.fail)
    .child(div().text_size(13).line_height("19px").text_color(c.fail).child(String(message)))
    .child(h_flex().gap(8)
      .child(Button(c, "svc-retry", "Retry", (_e, cx) => { retry(); onRetry?.(cx); }, { kind: "primary", small: true }))
      .child(Button(c, "svc-copy", "Copy details", (_e, cx) => cx.write_to_clipboard(details(message)), { small: true })));
}

/** What a failed api() call shows: ServiceError if the services are down, else the error. */
export function Problem(c, r, onRetry) {
  if (r?.starting) return Skeleton(c);
  if (r?.failed) return ServiceError(c, r.error, onRetry);
  return ErrorState(c, r?.error ?? "Something went wrong.");
}

/** Horizontal bars from a pre-totalled series [{label, value, tone?}]. */
export function Bars(c, series, { format = (v) => String(v), max } = {}) {
  const top = max ?? Math.max(1e-9, ...series.map((s) => Math.abs(Number(s.value) || 0)));
  return v_flex().w_full().gap(6).children(series.map((s, i) => h_flex().gap(10).items_center()
    .child(div().w(140).flex_none().truncate().text_size(12).text_color(c.grey900).child(String(s.label)))
    .child(div().flex_1().h(14).bg(c.surface2).child(div().h_full().w(`${Math.max(0.5, (Math.abs(Number(s.value) || 0) / top) * 100)}%`)
      .bg(s.tone === "bad" ? c.fail : s.tone === "good" ? c.good : c.blue)))
    .child(div().w(64).flex_none().text_size(12).font_family(MONO).text_color(c.navy).child(format(s.value)))));
}

/** A line from a pre-totalled series of numbers (evenly spaced), drawn natively. */
export function Line(c, values, { height = 120, min, max } = {}) {
  const nums = values.map(Number).filter((v) => Number.isFinite(v));
  const box = div().w_full().h(height).relative().bg(c.surface2).border_1().border_color(c.rule);
  if (nums.length < 2 || !globalThis.window?.paint_path) return box;
  const lo = min ?? Math.min(...nums), hi = max ?? Math.max(...nums);
  const span = hi - lo || 1;
  let p = PathBuilder.stroke(2);
  nums.forEach((v, i) => {
    const x = `${(i / (nums.length - 1)) * 100}%`, y = `${(1 - (v - lo) / span) * 92 + 4}%`;
    p = i === 0 ? p.move_to(x, y) : p.line_to(x, y);
  });
  return box.child(div().absolute().inset_0().child(globalThis.window.paint_path(p.build(), c.blue)));
}

/**
 * A table: a fixed list (`rows`) or server paging (`fetch({offset, limit, sort, desc})` resolving
 * `{rows, total}`). Rows are virtualized, so only what's visible is built. Keep the instance on the view.
 *
 *   this.table = new Table({ id: "inv", columns: [{key, label, width?, sortable?, render?(row, c)}],
 *                            rows?, fetch?, pageSize: 100, rowKey: (row) => row.id, onRow: (row, cx) => ... });
 *   this.table.load(cx);            // after a filter changes too
 *   this.table.render(c, cx)        // in render()
 */
export class Table {
  constructor({ id, columns, rows = null, fetch = null, pageSize = 100, rowKey = (r, i) => String(r.id ?? i), onRow = null, rowHeight = 34, empty = "Nothing here yet." }) {
    Object.assign(this, { id, columns, fetch, pageSize, rowKey, onRow, rowHeight, empty });
    this.rows = rows ?? [];
    this.total = rows ? rows.length : 0;
    this.offset = 0;
    this.sort = null;
    this.desc = false;
    this.state = rows ? "ready" : "idle";
    this.error = null;
  }

  setRows(rows) {
    this.rows = rows; this.total = rows.length; this.offset = 0; this.state = "ready"; this.error = null;
  }

  load(cx, offset = 0) {
    if (!this.fetch) return;
    this.offset = offset;
    this.state = "loading";
    const token = (this.request = (this.request ?? 0) + 1);
    cx.spawn(async (cx) => {
      let r;
      try { r = await this.fetch({ offset, limit: this.pageSize, sort: this.sort, desc: this.desc }); }
      catch (e) { r = { error: String(e) }; }
      if (token !== this.request) return;
      if (r?.error || r?.starting || r?.failed) { this.state = r.starting ? "starting" : r.failed ? "failed" : "error"; this.error = r.error; }
      else { this.rows = r.rows ?? []; this.total = r.total ?? this.rows.length; this.state = "ready"; this.error = null; }
      cx.notify();
    });
  }

  toggleSort(key, cx) {
    if (this.sort === key) this.desc = !this.desc; else { this.sort = key; this.desc = false; }
    if (this.fetch) this.load(cx, 0);
    else cx.notify();
  }

  header(c) {
    return h_flex().h(32).flex_none().px(12).gap(12).items_center().bg(c.surface2).border_b(1).border_color(c.rule)
      .text_size(11.5).font_medium().text_color(c.grey500)
      .children(this.columns.map((col) => {
        const cell = div().id(`${this.id}-h-${col.key}`).when(col.width != null, (e) => e.w(col.width).flex_none()).when(col.width == null, (e) => e.flex_1().min_w(0))
          .truncate().child(col.label + (this.sort === col.key ? (this.desc ? "  (desc)" : "  (asc)") : ""));
        return col.sortable && this.fetch ? cell.cursor_pointer().hover((s) => s.text_color(c.navy)).on_click((_e, cx) => this.toggleSort(col.key, cx)) : cell;
      }));
  }

  row(c, r, i) {
    return h_flex().h(this.rowHeight).px(12).gap(12).items_center().border_b(1).border_color(c.rule).text_size(13).text_color(c.navy)
      .when(Boolean(this.onRow), (e) => e.cursor_pointer().hover((s) => s.bg(c.surface2)))
      .children(this.columns.map((col) => {
        const v = col.render ? col.render(r, c) : String(r[col.key] ?? "");
        return div().when(col.width != null, (e) => e.w(col.width).flex_none()).when(col.width == null, (e) => e.flex_1().min_w(0)).truncate()
          .when(Boolean(col.mono), (e) => e.font_family(MONO).text_size(12)).child(v);
      }));
  }

  pager(c) {
    if (!this.fetch || this.total <= this.pageSize) return null;
    const from = this.total ? this.offset + 1 : 0, to = Math.min(this.offset + this.rows.length, this.total);
    return h_flex().h(36).flex_none().px(12).gap(8).items_center().border_t(1).border_color(c.rule)
      .child(div().flex_1().text_size(12).text_color(c.grey500).child(`${int(from)} to ${int(to)} of ${int(this.total)}`))
      .child(Button(c, `${this.id}-prev`, "Previous", (_e, cx) => this.load(cx, Math.max(0, this.offset - this.pageSize)), { small: true, disabled: this.offset === 0 }))
      .child(Button(c, `${this.id}-next`, "Next", (_e, cx) => this.load(cx, this.offset + this.pageSize), { small: true, disabled: to >= this.total }));
  }

  render(c) {
    const body = this.state === "error" ? div().p(12).child(ErrorState(c, this.error))
      : this.state === "failed" ? div().p(12).child(ServiceError(c, this.error))
      : this.state === "starting" ? Skeleton(c)
      : (this.state === "loading" || this.state === "idle") && !this.rows.length ? Loading(c)
      : !this.rows.length ? Empty(c, this.empty)
      : div().flex_1().min_h(0).relative()
        .child(v_virtual_list(this.id, this.rows.length, this.rowHeight, (i) => this.rowKey(this.rows[i], i),
          (range) => this.rows.slice(range.start, range.end).map((r, k) => this.row(c, r, range.start + k)))
          .size_full().when(Boolean(this.onRow), (e) => e.on_item_click((key, cx) => {
            const r = this.rows.find((x, i) => this.rowKey(x, i) === key);
            if (r) this.onRow(r, cx);
          })))
        .child(Scrollbar.vertical(this.id).viewport_from_layout().absolute().inset_0());
    return v_flex().size_full().min_h(0).bg(c.surface).border_1().border_color(c.rule).font_family(FONT)
      .child(this.header(c)).child(body).child(this.pager(c) ?? div());
  }
}
