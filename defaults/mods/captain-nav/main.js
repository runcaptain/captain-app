// CAPTAIN: the Captain side nav. Five sections (docs/product/design-from-user-stories.md
// in captain-ide); each is a child of the "sections" stack in layouts/captain.json
// (a webview block today, a native mod later). Clicking one runs layout.select,
// like any key or agent could. Terminals toggles the shells column: hiding it
// keeps every session running.
import { View, div, image, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, run, window, layout, props } from "herdr";

const STACK = "sections";
const SECTIONS = [
  { id: "investigations", label: "Investigations", icon: "search" },
  { id: "data", label: "Data and signals", icon: "database" },
  { id: "runs", label: "Runs", icon: "play" },
  { id: "comparisons", label: "Comparisons", icon: "git-compare" },
  { id: "handoff", label: "Handoff", icon: "send" },
];

function find(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = find(child, id);
    if (found) return found;
  }
  return null;
}

export default class CaptainNav extends View {
  row(t, id, icon, label, active, action) {
    return h_flex().id(id).h(30).px(10).gap(10).items_center().rounded(6).cursor_pointer()
      .text_color(active ? t.foreground : t.muted)
      .when(active, el => el.bg(t.hover).font_semibold())
      .hover(s => s.bg(t.hover).text_color(t.foreground))
      .on_click(action)
      .child(svg(`mods/captain-nav/${icon}.svg`).size(15))
      .child(label);
  }

  render() {
    const t = theme();
    const stack = find(layout(), STACK);
    const active = stack?.active ?? stack?.children?.[0]?.id;
    const mac = window()?.platform === "macos" && !window()?.fullscreen;
    // The brand row is its own block, so the layout can make it drag the window.
    if (props()?.part === "brand") {
      return h_flex().size_full().bg(t.chrome).pl(mac ? 80 : 14).pr(10).gap(8).items_center().text_size(13.5)
        .child(image("frame/welcome/captain.png").size(20).rounded(5))
        .child(div().font_semibold().text_color(t.foreground).child("Captain"));
    }
    return v_flex().size_full().bg(t.chrome).text_size(13.5)
      .child(v_flex().flex_1().px(8).pt(6).gap(2)
        .children(SECTIONS.map(s => this.row(t, `nav-${s.id}`, s.icon, s.label, s.id === active,
          () => run("layout.select", { stack: STACK, child: s.id })))))
      .child(v_flex().flex_none().px(8).pt(4).pb(8).border_t(1).border_color(t.border)
        .child(this.row(t, "nav-terminals", "terminal", "Terminals", !find(layout(), "work")?.hidden, () => run("layout.toggle", { id: "work" })))
        .child(this.row(t, "nav-settings", "settings", "Settings", false, () => run("app.settings"))));
  }
}
