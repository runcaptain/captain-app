// CAPTAIN: the compute picker (Edgar, 2026-11-12: "When clicking on the AWS thing, like the cloud and compute, I
// think it'd be useful if I also have the chance to select what compute environment I'm working in whenever I
// want"). The sidebar's cloud chip opens it: a small panel listing every place work can run (this computer, then
// each compute target of each connected cloud) with the current one checked. Picking one sets the workspace's
// default place for runs (PUT /api/settings/compute {backtest}); the chip and every "Runs on" row then show it
// (kit cost.js reads the same setting). A form can still pick another place for one run.
//
// It changes one setting and nothing else: it starts no work, and adds or removes no place. "Add a place to run
// work" and "Cloud logins" open the forms that do.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, props, openOverlay, closeOverlay } from "herdr";
import { api } from "./kit/cst.js";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { Skeleton } from "./kit/ui.js";
import { LOCAL, placesFrom, placeWords, announcePlace } from "./kit/cost.js";

export default class ComputePicker extends View {
  init(_props, cx) {
    this.places = null;
    this.current = LOCAL;
    this.error = null;
    this.saving = null;
    this.logins = null; // how many cloud logins are connected
    this.load(cx);
  }

  load(cx) {
    cx.spawn(async (cx) => {
      const [t, s, cl] = await Promise.all([api("/api/compute", { scoped: false }), api("/api/settings", { scoped: false }), api("/api/cloud/summary")]);
      this.logins = cl.ok ? (cl.data?.profiles ?? []).length : null;
      if (t.ok) { this.places = placesFrom(t.data); this.error = null; }
      else if (!t.starting) this.error = "Couldn't list the places work can run.";
      else cx.timer.after(1000, (cx) => this.load(cx));
      if (s.ok) this.current = s.data?.compute?.backtest ?? LOCAL;
      cx.notify();
    });
  }

  /** Make a place the workspace's default for runs. */
  pick(cx, p) {
    if (this.saving) return;
    if (p.value === this.current) { closeOverlay(props()?.overlay); return; }
    this.saving = p.value;
    this.error = null;
    cx.spawn(async (cx) => {
      const r = await api("/api/settings/compute", { method: "PUT", body: { backtest: p.value } });
      this.saving = null;
      if (r.ok) {
        this.current = r.data?.compute?.backtest ?? p.value;
        announcePlace(this.current); // the chip and every open "Runs on" row follow
        closeOverlay(props()?.overlay);
      } else this.error = String(r.data?.detail ?? r.data?.error ?? r.error ?? "Couldn't change where runs go.");
      cx.notify();
    });
    cx.notify();
  }

  render() {
    const c = palette(isDark(theme()));
    const places = this.places ?? [];
    const link = (id, label, onClick) => div().id(id).h(30).px(14).flex().items_center().cursor_pointer().text_size(12.5).text_color(c.blue)
      .hover((s) => s.bg(c.blueLight)).on_click(onClick).child(label);
    return v_flex().id("compute-picker").size_full().bg(c.surface).font_family(FONT).text_color(c.navy).border_1().border_color(c.grey300)
      .child(v_flex().flex_none().px(14).pt(12).pb(8).gap(2)
        .child(div().text_size(13).font_semibold().child("Where runs go"))
        .child(div().text_size(12).text_color(c.grey500).child("New runs start here unless a form picks another place.")))
      .child(v_flex().id("compute-places").flex_1().min_h(0).overflow_y_scrollbar()
        .when(this.places === null && !this.error, (e) => e.child(Skeleton(c, 3)))
        .children(places.map((p) => {
          const on = p.value === this.current;
          return h_flex().id(`compute-${p.value}`).min_h(40).px(14).py(4).gap(10).items_center().cursor_pointer()
            .bg(on ? c.blueLight : c.surface).hover((s) => (on ? s : s.bg(c.surface2)))
            .on_click((_e, cx) => this.pick(cx, p))
            .child(p.value === LOCAL || !p.brand ? svg(icon(p.value === LOCAL ? "monitor" : "cloud")).size(15).flex_none().text_color(on ? c.blue : c.grey500) : Brand(c, p.brand, 15))
            .child(v_flex().flex_1().min_w(0)
              .child(div().truncate().text_size(13).text_color(on ? c.blue : c.navy).child(p.label))
              .child(div().truncate().text_size(11.5).text_color(c.grey500).child(placeWords(p))))
            .when(on, (e) => e.child(svg(icon("check")).size(14).flex_none().text_color(c.blue)))
            .when(this.saving === p.value, (e) => e.child(div().flex_none().text_size(11.5).text_color(c.grey500).child("Saving")));
        })))
      .when(!!this.error, (e) => e.child(div().id("compute-error").flex_none().px(14).py(6).text_size(12).text_color(c.fail).child(this.error)))
      .child(v_flex().flex_none().py(4).border_t(1).border_color(c.rule)
        .child(link("compute-add", "Add a place to run work", () => { closeOverlay(props()?.overlay); openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "compute" } }); }))
        // With no cloud login yet, the form that connects one; else the list of them.
        .child(this.logins === 0
          ? link("compute-connect", "Connect a cloud account", () => { closeOverlay(props()?.overlay); openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "cloud" } }); })
          : link("compute-clouds", "Cloud logins", () => { closeOverlay(props()?.overlay); openOverlay({ mod: "captain-cloud", kind: "dialog", width: 760, height: 640 }); })));
  }
}
