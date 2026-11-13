// CAPTAIN: Notebooks, its own view (Edgar, 2026-11-12: "Notebooks, we should keep that but not inside of data and
// signals. I think it should be a separate view tab."). SQL, Python, notes and charts, saved as versions. The
// notebook itself is notebooks.js, unchanged from when it was a tab of Data; this file is the page around it.
//
// Other screens open one through shared "captain.open" {section: "notebooks", notebook?, seedSql?: {conn, sql, name}}:
// a notebook by name, or a new one whose first cell is that query (a table's example query in Data).
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, shared, props as layoutProps } from "herdr";
import { watchWorkspace, watchConnections, keepFresh } from "./kit/cst.js";
import { watchView } from "./kit/view.js";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { icon } from "./kit/icons.js";
import { Notebooks, labelOf } from "./notebooks.js";

export default class CaptainNotebooks extends View {
  init(_props, cx) {
    this.width = 1000;
    this.nb = new Notebooks();
    // Layout props (a layout, or the real-window check): {notebook, runAll, sqlTools}.
    const p = layoutProps?.() ?? {};
    this.nb.want = p.notebook ?? null;
    this.nb.runOnOpen = !!p.runAll;
    this.nb.toolsOnOpen = p.sqlTools ?? null; // {panel}: the first SQL cell's tools, open on that panel
    this.nb.load(cx);
    // Autosave the draft, and keep a running cell's clock moving.
    cx.timer.every(500, (cx) => this.nb.tick(cx));
    // Until the list loads (the services are still starting), ask again.
    cx.timer.every(3000, (cx) => { if (this.nb.list === null) this.nb.load(cx); });
    // The list stays current by itself. The open notebook is not reloaded: its text is the person's.
    keepFresh(cx, "notebooks", (cx) => this.nb.refreshList(cx), ["notebooks"]);
    // Shared values outlive the app: an open from before this screen started is done.
    const stale = shared("captain.open");
    if (stale?.at && Date.now() - stale.at > 10000) this.opened = stale.at;
    cx.timer.every(500, (cx) => {
      const o = shared("captain.open");
      if (o?.section !== "notebooks" || o.at === this.opened) return;
      this.opened = o.at;
      const nb = this.nb;
      // The sidebar's entry: Notebooks' home, the list. An open notebook's draft is saved first.
      if (o.home) { nb.home(cx); return; }
      if (o.seedSql) {
        nb.seedSql = { conn: o.seedSql.conn, sql: o.seedSql.sql };
        nb.newName.set_value(String(o.seedSql.name ?? "query"));
        if (nb.list === null) nb.load(cx);
        nb.newNotebook(cx);
      } else if (o.notebook) {
        if (nb.list === null) { nb.want = o.notebook; nb.load(cx); } else nb.openNotebook(cx, o.notebook);
      }
      cx.notify();
    });
    // What this screen shows, for agents (cst app context): the notebook open.
    watchView(cx, "notebooks", () => ({ notebook: this.nb?.open?.name ?? null, name: this.nb?.open?.name ? labelOf(this.nb.row(this.nb.open.name)) : null }));
    // A database connected or removed anywhere in the app: relist it for the SQL cells.
    watchConnections(cx, (cx) => { this.nb.loadConns(cx); cx.notify(); });
    // Another workspace: its notebooks, on a fresh Python.
    watchWorkspace(cx, (cx) => {
      this.nb.kernel?.close();
      this.nb = new Notebooks();
      this.nb.load(cx);
      cx.notify();
    });
  }

  render() {
    const c = palette(isDark(theme()));
    return v_flex().id("root").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      .on_size_change((size, cx) => { this.width = size.width; cx.notify(); })
      .child(h_flex().h(52).flex_none().px(20).gap(14).items_center().border_b(1).border_color(c.rule)
        .child(div().flex_none().text_size(18).font_semibold().text_color(c.navy).child("Notebooks"))
        .child(div().flex_1())
        .child(div().id("nb-refresh").size(28).flex().flex_none().items_center().justify_center().cursor_pointer()
          .border_1().border_color(c.grey300).text_color(c.grey900).hover((s) => s.text_color(c.blue).border_color(c.blue))
          .tooltip("Refresh").on_click((_e, cx) => this.nb.load(cx))
          .child(svg(icon("refresh-cw")).size(14))))
      .child(this.nb.render(c, this.width));
  }
}
