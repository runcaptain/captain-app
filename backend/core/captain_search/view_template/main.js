// {{title}}: a team view (captain/views/{{name}}/). Captain loads it from your project's captain/ folder, lists it
// under the Explorer's Views, and reloads it when you save. It's an ordinary mod: gpui-kit for drawing, "herdr" for
// the window, and the Captain kit as ./kit (Captain puts it there when it loads the view: don't commit a kit/ folder).
//
// This one shows a dataset's rows in the kit's result grid, filtered to whatever the Explorer has picked. Change it
// to show what your team needs. The view API is in backend/docs/extending.md, "Team views".
//   captain-app check captain/views/{{name}}      loads it once with the kit and says exactly what's wrong
import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { api, watchScope, watchWorkspace } from "./kit/cst.js";
import { Empty, Skeleton, Problem } from "./kit/ui.js";
import { ResultGrid } from "./kit/grid.js";

const LIMIT = 200;

export default class TeamView extends View {
  init(_props, cx) {
    this.datasets = null;
    this.picked = null;
    this.grid = null;
    this.load(cx);
    // The Explorer's pick and the space's workspace travel with every request; reload when either changes.
    watchScope(cx, (cx) => this.load(cx));
    watchWorkspace(cx, (cx) => { this.picked = null; this.load(cx); });
  }

  load(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/datasets");
      this.listed = r;
      this.datasets = r.ok ? r.data : null;
      const still = this.datasets?.some((d) => d.name === this.picked);
      if (this.datasets?.length) await this.open(cx, still ? this.picked : this.datasets[0].name);
      else this.grid = null;
      cx.notify();
    });
  }

  async open(cx, name) {
    this.picked = name;
    this.rows = await api(`/api/datasets/${encodeURIComponent(name)}/rows?limit=${LIMIT}`);
    const d = this.rows.ok ? this.rows.data : null;
    this.grid = d ? new ResultGrid({ id: "{{name}}-rows", name, table: { columns: d.columns, rows: d.rows, row_count: d.total ?? d.rows.length, truncated: d.has_more, limit: LIMIT } }) : null;
    cx.notify();
  }

  render() {
    const c = palette(isDark(theme()));
    const list = this.datasets;
    return v_flex().id("{{name}}").size_full().bg(c.surface).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(52).flex_none().px(20).items_center().border_b(1).border_color(c.rule)
        .child(div().text_size(15).font_semibold().child("{{title}}")))
      .child(this.listed && !this.listed.ok ? div().p(20).child(Problem(c, this.listed, (cx) => this.load(cx)))
        : !list ? div().p(20).child(Skeleton(c, 6))
        : !list.length ? Empty(c, "No datasets here", "Import one, or pick another scope in the Explorer.")
        : h_flex().flex_1().min_h(0).items_start()
          .child(v_flex().id("{{name}}-list").w(220).h_full().flex_none().overflow_y_scroll().border_r(1).border_color(c.rule)
            .children(list.map((d, i) => div().id(`{{name}}-ds-${i}`).px(14).py(8).text_size(13).cursor_pointer().truncate()
              .bg(d.name === this.picked ? c.blueLight : c.surface).text_color(d.name === this.picked ? c.blue : c.navy)
              .hover((s) => s.bg(c.surface2))
              .on_click((_e, cx) => { cx.spawn(async (cx) => this.open(cx, d.name)); })
              .child(d.name))))
          .child(div().flex_1().min_w(0).min_h(0)
            .child(this.rows && !this.rows.ok ? div().p(20).child(Problem(c, this.rows, (cx) => this.load(cx)))
              : this.grid ? this.grid.render(c, { height: 520 }) : Skeleton(c, 6))));
  }
}
