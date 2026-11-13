// CAPTAIN: Data, Tables: every table of the workspace in one list (Edgar, 2026-11-12: "tables and datasets could
// effectively be merged together because of the same thing"). One row per table or dataset: its name, where it
// lives, how many rows, whether Captain keeps versions of it, and when it was last read. A dataset is a table
// Captain keeps versions of, and the Versions column says so; there is no separate screen for it.
//
// GET /api/data/tables answers the whole list (the datasets from the registry, each database's tables from what
// "Read its tables" cached, the last read from the read log). Searching asks the services again, so a column's
// name finds its table. Clicking a row opens that table's page in place.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { api } from "./kit/cst.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { int } from "./kit/num.js";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** When something was last read, in words: "today", "yesterday", "5 days ago", then the date. Takes a day
 *  ("2026-11-12") or a full time. */
export function readWords(when, now = Date.now()) {
  if (!when) return "Not read yet";
  const t = Date.parse(String(when).length === 10 ? `${when}T00:00:00Z` : when);
  if (!Number.isFinite(t)) return "Not read yet";
  const days = Math.floor((now - t) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 30) return `${days} days ago`;
  const d = new Date(t);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
}

/** The Versions column: a dataset says how many Captain keeps, a database table that it keeps none. */
export function versionWords(row) {
  if (row.kind !== "dataset") return "Read where it lives";
  const n = row.versions ?? 1;
  return n === 1 ? "Captain keeps 1 version" : `Captain keeps ${int(n)} versions`;
}

const FILTERS = [[null, "All"], ["dataset", "Captain keeps versions"], ["table", "In a database"]];

export class Catalog {
  constructor(host) {
    this.host = host;
    this.rows = null; // null until the services answer
    this.connections = [];
    this.error = null;
    this.kind = null;
    this.find = InputState.new({ placeholder: "Search tables and columns" });
    this.find.on("submit", (_e, cx) => this.load(cx));
    this.find.on("change", (_e, cx) => { this.typed = Date.now(); cx.notify(); });
  }

  load(cx) {
    const seq = (this.seq = (this.seq ?? 0) + 1);
    const q = this.find.value().trim();
    this.asked = q;
    cx.spawn(async (cx) => {
      const r = await api(`/api/data/tables${q ? `?q=${encodeURIComponent(q)}` : ""}`);
      if (seq !== this.seq) return; // a newer search was asked for
      if (r.ok) {
        this.rows = r.data?.rows ?? [];
        this.connections = r.data?.connections ?? [];
        this.error = null;
      } else if (!r.starting) this.error = r;
      this.starting = !r.ok && !!r.starting;
      cx.notify();
    });
  }

  /** From the screen's timer: search a moment after typing stops, and ask again while the services start. */
  tick(cx) {
    if (this.typed && Date.now() - this.typed > 350 && this.find.value().trim() !== (this.asked ?? "")) { this.typed = null; this.load(cx); }
  }

  shown() {
    return (this.rows ?? []).filter((r) => !this.kind || r.kind === this.kind);
  }

  open(cx, r) {
    if (r.kind === "dataset") this.host.openDataset(cx, r.name);
    else this.host.openTable(cx, r.connection, r.schema, r.name);
  }

  render(c, w) {
    const rows = this.shown();
    const all = this.rows ?? [];
    const q = this.find.value().trim();
    const unread = this.connections.filter((k) => !k.pulled_at);
    // Name and where it lives share what the three fixed columns leave.
    const narrow = w < 900;
    const col = (t, width, right) => div().w(width).flex_none().px(8).truncate().when(!!right, (e) => e.text_right()).child(t);
    return v_flex().id("catalog").flex_1().min_h(0)
      .on_drop((e, cx) => this.host.lib.importPaths(cx, e.paths ?? [], ""))
      .child(h_flex().flex_none().flex_wrap().gap(8).px(20).py(10).items_center().border_b(1).border_color(c.rule)
        .child(h_flex().id("cat-find").w(280).h(28).px(8).gap(6).items_center().border_1().border_color(q ? c.blue : c.grey300)
          .child(svg(icon("search")).size(12).flex_none().text_color(c.grey500))
          .child(div().flex_1().min_w(0).child(Input.new(this.find).w_full().text_size(12).text_color(c.navy))))
        .children(FILTERS.map(([k, label]) => div().id(`cat-kind-${k ?? "all"}`).h(28).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12)
          .border_1().border_color(this.kind === k ? c.blue : c.grey300).bg(this.kind === k ? c.blueLight : c.surface).text_color(this.kind === k ? c.blue : c.grey900)
          .on_click((_e, cx) => { this.kind = k; cx.notify(); }).child(label)))
        .child(div().flex_1())
        .child(div().id("cat-count").flex_none().text_size(12).text_color(c.grey500).child(this.rows === null ? "" : `${int(rows.length)} ${rows.length === 1 ? "table" : "tables"}`)))
      .when(!!this.host.lib.job, (e) => e.child(this.host.lib.progress(c)))
      .when(!!this.host.lib.summary, (e) => e.child(this.host.lib.summaryPanel(c)))
      // A database connected but never read lists nothing: say so, with the button that reads it.
      .when(unread.length > 0 && this.kind !== "dataset", (e) => e.child(h_flex().id("cat-unread").flex_none().px(20).py(8).gap(10).items_center().border_b(1).border_color(c.rule).text_size(12.5).text_color(c.grey900)
        .child(div().flex_1().min_w(0).child(`${unread.map((k) => k.connection).join(", ")} ${unread.length === 1 ? "is connected, but its tables haven't been read yet" : "are connected, but their tables haven't been read yet"}.`))
        .child(Button(c, "cat-read", unread.length === 1 ? "Read its tables" : "Read their tables", (_e, cx) => this.host.showSources(cx, unread[0].connection), { small: true }))))
      .child(h_flex().flex_none().h(30).px(12).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().text_color(c.grey900).items_center()
        .child(div().flex_1().min_w(160).px(8).child("Name"))
        .child(div().flex_1().min_w(160).px(8).child("Where it lives"))
        .child(col("Rows", 96, true))
        .when(!narrow, (e) => e.child(col("Versions", 190)))
        .child(col("Last read", 110)))
      .child(v_flex().id("cat-list").flex_1().min_h(0).overflow_y_scrollbar()
        .when(this.rows === null && !this.error, (e) => e.child(Skeleton(c, 8)))
        .when(!!this.error, (e) => e.child(div().p(20).child(this.host.troubleView(c, "tables-list", this.error, "Couldn't load your tables.", (cx) => this.load(cx)))))
        .when(this.rows !== null && !rows.length && !this.error, (e) => e.child(div().id("cat-none").p(20).text_size(12.5).text_color(c.grey500)
          .child(q ? `No table matches "${q}". Search looks at names, where a table lives and column names.` : all.length ? "No tables of this kind." : "No tables yet.")))
        .children(rows.map((r, i) => h_flex().id(`cat-row-${i}`).h(36).flex_none().px(12).items_center().cursor_pointer().border_b(1).border_color(c.rule).text_size(12.5)
          .hover((s) => s.bg(c.surface2)).tooltip(r.description ?? r.name)
          .on_click((_e, cx) => this.open(cx, r))
          .child(h_flex().flex_1().min_w(160).px(8).gap(8).items_center()
            .child(r.kind === "table" && r.engine ? Brand(c, r.engine, 14) : svg(icon("table")).size(14).flex_none().text_color(c.grey500))
            .child(div().min_w(0).truncate().text_color(c.navy).child(r.name))
            .when(!!r.matched_column, (e) => e.child(div().flex_none().text_size(11).text_color(c.grey500).child(`column ${r.matched_column}`))))
          .child(div().flex_1().min_w(160).px(8).truncate().text_color(c.grey900).child(r.where))
          .child(col(r.rows != null ? int(r.rows) : "", 96, true))
          .when(!narrow, (e) => e.child(col(versionWords(r), 190)))
          .child(col(readWords(r.last_read), 110).text_color(c.grey500)))));
  }

}
