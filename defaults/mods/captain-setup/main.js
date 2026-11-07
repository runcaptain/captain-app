// CAPTAIN: New workspace. Where it keeps its work (a folder on this Mac or cloud storage), then a drop zone for your
// files, then an optional model registry. The flow is kit/setup.js, shared with the welcome tour's first page.
// Opened from the sidebar's space switcher and the palette: openOverlay({ mod: "captain-setup", kind: "dialog" }).
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, Scrollbar } from "gpui-base";
import { theme, props, closeOverlay } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { icon } from "./kit/icons.js";
import { StorageSetup } from "./kit/setup.js";

export default class Setup extends View {
  init(_props, cx) {
    this.flow = new StorageSetup({ first: false, done: () => closeOverlay(props()?.overlay) });
    this.flow.start(cx);
  }

  render() {
    const c = palette(isDark(theme()));
    return v_flex().id("setup").size_full().bg(c.surface).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(52).flex_none().px(20).gap(10).items_center().border_b(1).border_color(c.rule)
        .child(div().size(28).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("folder-open")).size(15)))
        .child(div().flex_1().text_size(15).font_semibold().child("New workspace"))
        .child(div().id("setup-cancel").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => closeOverlay(props()?.overlay)).child(this.flow.created ? "Close" : "Cancel")))
      .child(div().flex_1().min_h(0).relative()
        .child(div().id("setup-body").size_full().overflow_y_scroll().child(div().px(24).pt(20).pb(20).child(this.flow.body(c))))
        .child(Scrollbar.vertical("setup-body").absolute().inset_0()))
      .child(h_flex().h(60).flex_none().px(20).items_center().justify_end().border_t(1).border_color(c.rule).child(this.flow.footer(c)));
  }
}
