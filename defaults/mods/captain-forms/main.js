// CAPTAIN: a designed form for one task in plain words (tasks.js), or the list of
// tasks (the New menu). Opened as an overlay:
//   props {task: "dataset"}   that task's form
//   props {menu: true}        the New menu: every task, icon and one line each
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, TextareaState, Textarea } from "gpui-base";
import { theme, props, closeOverlay, openOverlay, notify } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { Button, Badge, ServiceError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { TASKS } from "./tasks.js";

export default class CaptainForms extends View {
  init(_props, cx) {
    this.task = TASKS.find((t) => t.id === props()?.task) ?? null;
    this.values = {};
    this.states = {};
    this.options = {};
    this.ctx = {};
    this.errors = {};
    if (this.task) this.start(cx);
  }

  close() { closeOverlay(props().overlay); }

  start(cx) {
    for (const f of this.task.fields) if (f.default !== undefined) this.values[f.key] = f.default;
    cx.spawn(async (cx) => {
      if (this.task.load) this.ctx = await this.task.load();
      await this.loadOptions(cx);
    });
  }

  async loadOptions(cx) {
    for (const f of this.task.fields) {
      if (f.kind !== "choice") continue;
      const o = typeof f.options === "function" ? await f.options(this.ctx, this.values) : f.options;
      this.options[f.key] = o ?? [];
      if (this.values[f.key] === undefined && this.options[f.key].length === 1 && f.required) this.values[f.key] = this.options[f.key][0].value;
    }
    cx.notify();
  }

  // Fields shown now, with the services' own fields (a sign-in's, a compute kind's) expanded.
  shown() {
    const out = [];
    for (const f of this.task.fields) {
      if (f.when && !f.when(this.values)) continue;
      if (f.kind === "dynamic") {
        for (const d of f.fields(this.ctx, this.values)) out.push({ key: `af:${d.name}`, label: d.label ?? d.name, kind: d.secret ? "secret" : "text", required: Boolean(d.required), default: d.default });
      } else out.push(f);
    }
    return out;
  }

  text(f) {
    if (!this.states[f.key]) {
      const long = f.kind === "long";
      const s = long ? TextareaState.new({ placeholder: f.placeholder ?? "", rows: 4 }) : InputState.new({ placeholder: f.placeholder ?? "" });
      if (f.kind === "secret") s.set_masked?.(true);
      const start = this.values[f.key] ?? f.default;
      if (start !== undefined) s.set_value(String(start));
      s.on("change", () => { this.values[f.key] = s.value(); delete this.errors[f.key]; });
      this.states[f.key] = s;
    }
    return this.states[f.key];
  }

  field(c, f) {
    const err = this.errors[f.key];
    const label = h_flex().gap(6).items_baseline()
      .child(div().text_size(13).font_medium().text_color(c.navy).child(f.label))
      .when(Boolean(f.optional), (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("optional")));
    let control;
    if (f.kind === "choice") {
      const opts = this.options[f.key];
      control = !opts ? div().h(30).w(160).bg(c.surface2)
        : !opts.length ? div().text_size(12.5).text_color(c.grey500).child(f.empty ?? "Nothing to pick yet.")
        : h_flex().flex_wrap().gap(6).children(opts.map((o) => {
          const on = this.values[f.key] === o.value;
          return h_flex().id(`f-${f.key}-${o.value}`).h(30).px(10).gap(6).items_center().cursor_pointer().border_1()
            .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5)
            .on_click((_e, cx) => { this.values[f.key] = o.value; delete this.errors[f.key]; this.loadOptions(cx); })
            .child(o.label).when(Boolean(o.chip), (e) => e.child(Badge(c, o.chip, on ? "blue" : "neutral")));
        }));
    } else if (f.kind === "toggle") {
      const on = Boolean(this.values[f.key]);
      control = h_flex().id(`f-${f.key}`).gap(8).items_center().cursor_pointer().on_click((_e, cx) => { this.values[f.key] = !on; cx.notify(); })
        .child(div().w(30).h(16).p(2).bg(on ? c.blue : c.grey300).child(div().size(12).bg(c.surface).when(on, (e) => e.ml(14))))
        .child(div().text_size(12.5).text_color(c.grey900).child(on ? "On" : "Off"));
    } else {
      const long = f.kind === "long";
      control = div().id(`f-${f.key}`).when(!long, (e) => e.h(32).flex().items_center()).when(long, (e) => e.min_h(84).py(6)).px(10)
        .bg(c.surface).border_1().border_color(err ? c.fail : c.grey300).cursor_text()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
        .child(long ? Textarea.new(this.text(f)).w_full().text_size(13).text_color(c.navy) : Input.new(this.text(f)).w_full().text_size(13).text_color(c.navy));
    }
    return v_flex().gap(5).child(label).child(control)
      .when(Boolean(err), (e) => e.child(div().text_size(12).text_color(c.fail).child(err)))
      .when(!err && Boolean(f.help), (e) => e.child(div().text_size(12).line_height("17px").text_color(c.grey500).child(f.help)));
  }

  submit(cx) {
    if (this.busy) return;
    this.errors = {};
    for (const f of this.shown()) {
      const v = this.values[f.key];
      if (f.required && (v === undefined || v === null || String(v).trim() === "")) this.errors[f.key] = `${f.label} is needed.`;
    }
    if (Object.keys(this.errors).length) { cx.notify(); return; }
    this.busy = true; this.result = null;
    cx.notify();
    cx.spawn(async (cx) => {
      let r;
      try { r = await this.task.submit(this.values, this.ctx, props()?.args ?? {}); } catch (e) { r = { ok: false, error: String(e) }; }
      this.busy = false;
      this.result = r;
      if (r.ok) {
        notify({ title: r.message ?? "Done", level: "info" });
        this.close();
      }
      cx.notify();
    });
  }

  menu(c) {
    return v_flex().size_full().bg(c.surface).font_family(FONT).py(6)
      .child(div().px(14).pt(4).pb(6).text_size(11).font_medium().text_color(c.grey500).child("NEW"))
      .children(TASKS.filter((t) => !t.hidden).map((t) => h_flex().id(`new-${t.id}`).px(14).py(8).gap(12).items_start().cursor_pointer().hover((s) => s.bg(c.surface2))
        .on_click(() => { this.close(); openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: t.id } }); })
        .child(div().size(28).flex_none().flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon(t.icon)).size(15)))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(13.5).font_medium().text_color(c.navy).child(t.label))
          .child(div().text_size(12).line_height("16px").text_color(c.grey500).child(t.about)))));
  }

  render() {
    const c = palette(isDark(theme()));
    if (!this.task) return this.menu(c);
    const t = this.task;
    const failed = this.result && !this.result.ok ? this.result.error : null;
    return v_flex().size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      .child(h_flex().h(56).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
        .child(div().size(30).flex_none().flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon(t.icon)).size(16)))
        .child(div().flex_1().text_size(16).font_semibold().child(t.label))
        .child(div().id("form-close").cursor_pointer().text_size(13).text_color(c.grey500).hover((s) => s.text_color(c.navy)).on_click(() => this.close()).child("Cancel")))
      .child(v_flex().id("form-body").flex_1().min_h(0).overflow_y_scroll().p(20).gap(16)
        .child(div().text_size(13).line_height("19px").text_color(c.grey900).child(t.about))
        .children(this.shown().map((f) => this.field(c, f)))
        .when(Boolean(failed), (e) => e.child(/couldn't start its services/.test(failed) ? ServiceError(c, failed) : div().p(10).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).line_height("18px").text_color(c.fail).child(failed))))
      .child(h_flex().h(60).flex_none().px(20).gap(8).items_center().justify_end().border_t(1).border_color(c.rule)
        .child(Button(c, "form-cancel", "Cancel", () => this.close()))
        .child(Button(c, "form-go", this.busy ? "Working..." : t.label, (_e, cx) => this.submit(cx), { kind: "primary", disabled: Boolean(this.busy) })));
  }
}
