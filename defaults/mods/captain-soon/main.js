// CAPTAIN: a section whose native screen isn't built yet. It says what will be
// here and lists the section's cst commands, each a native form. Never a web view.
//   props {title, about, prefixes: ["data", "queries"]}
import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, props, openOverlay } from "herdr";
import { api } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Card, Problem } from "./kit/ui.js";

export default class CaptainSoon extends View {
  init(_props, cx) {
    this.commands = null;
    this.error = null;
    this.load(cx);
    cx.timer.every(15000, (cx) => { if (!this.commands) this.load(cx); });
  }

  load(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/commands");
      const prefixes = props()?.prefixes ?? [];
      if (r.ok && Array.isArray(r.data)) {
        this.commands = r.data.filter((c) => prefixes.includes(c.path[0]));
        this.error = null;
      } else {
        this.problem = r;
      }
      cx.notify();
    });
  }

  render() {
    const c = palette(isDark(theme()));
    const p = props() ?? {};
    const list = this.commands ?? [];
    return v_flex().id("soon").size_full().bg(c.canvas).font_family(FONT).text_color(c.navy).overflow_y_scroll().p(24).gap(14)
      .child(div().text_size(20).font_semibold().child(p.title ?? "Coming next"))
      .child(div().max_w(640).text_size(13.5).line_height("20px").text_color(c.grey900).child(p.about ?? ""))
      .child(div().text_size(12.5).text_color(c.grey500).child("This screen is being built natively. Until then, everything it will do runs here as commands."))
      .child(this.commands ? Card(c, { title: `Commands (${list.length})`, pad: 0 },
        ...list.map((cmd) => h_flex().id(`soon-${cmd.path.join("-")}`).h(38).px(16).gap(12).items_center().cursor_pointer()
          .border_b(1).border_color(c.rule).hover((s) => s.bg(c.surface2))
          .on_click(() => openOverlay({ mod: "captain-commands", kind: "dialog", width: 560, props: { path: cmd.path } }))
          .child(div().w(220).flex_none().font_family(MONO).text_size(12.5).child(`cst ${cmd.path.join(" ")}`))
          .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.grey500).child(cmd.help ?? ""))))
        : Problem(c, this.problem ?? { starting: true }, (cx) => this.load(cx)));
  }
}
