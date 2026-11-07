// CAPTAIN: a designed form for one task in plain words (tasks.js), or the list of
// tasks (the New menu). Opened as an overlay:
//   props {task: "dataset"}   that task's form
//   props {menu: true}        the New menu: every task, icon and one line each
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, TextareaState, Textarea } from "gpui-base";
import { theme, props, closeOverlay, openOverlay, notify, shared, setShared } from "herdr";
import { palette, isDark, FONT, RADIUS } from "./kit/tokens.js";
import { Button, Badge, ServiceError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { TASKS } from "./tasks.js";

const IMPORT = { mod: "captain-import", kind: "dialog", width: 900, height: 640, props: {} };

export default class CaptainForms extends View {
  init(_props, cx) {
    // The New menu; the test hook's entry "menu" opens it too.
    const previewMenu = !props()?.task && !props()?.menu && (() => { const p = shared("captain.forms.preview"); return Array.isArray(p) && p[(shared("captain.forms.preview.i") ?? 0) % p.length] === "menu"; })();
    if (previewMenu) setShared("captain.forms.preview.i", (shared("captain.forms.preview.i") ?? 0) + 1);
    this.isMenu = Boolean(props()?.menu) || previewMenu;
    if (this.isMenu) { this.sel = 0; this.menuFocus = cx.focus_handle(); this.menuFocus.focus(); }
    this.values = {};
    // Test hook: opened bare (`captain-app do mod.open`), shared "captain.forms.preview" picks a form,
    // so a test copy can show any form on screen. People never set it.
    // A list steps through its entries, one per open; an entry may preset values.
    let want = props()?.task ?? null;
    if (!want && !this.isMenu) {
      let p = shared("captain.forms.preview");
      if (Array.isArray(p)) {
        const i = shared("captain.forms.preview.i") ?? 0;
        setShared("captain.forms.preview.i", i + 1);
        p = p[i % p.length];
      }
      want = typeof p === "string" ? p : p?.task;
      Object.assign(this.values, p?.values ?? {});
    }
    // A caller may preset values too: openOverlay({mod: "captain-forms", props: {task: "database", values: {kind: "posthog"}}}).
    Object.assign(this.values, props()?.values ?? {});
    this.task = TASKS.find((t) => t.id === want) ?? null;
    this.states = {};
    this.options = {};
    this.ctx = {};
    this.errors = {};
    if (this.task) this.start(cx);
  }

  close() { closeOverlay(props().overlay); }

  start(cx) {
    for (const f of this.task.fields) if (f.default !== undefined) this.values[f.key] ??= f.default;
    // Text inputs keep state, which gpui-kit only lets a view create outside render.
    this.ensureStates();
    cx.spawn(async (cx) => {
      if (this.task.load) this.ctx = await this.task.load();
      await this.loadOptions(cx);
    });
  }

  /** A choice clicked: show it now, then reload what depends on it. The click's cx is good only during the click, so
   *  the reload runs on its own task. Awaiting with the click's cx left the pick unshown: Edgar clicked Compare and
   *  With in "Run a backtest" and nothing changed. */
  pick(cx, f, o) {
    this.values[f.key] = o.value;
    delete this.errors[f.key];
    cx.notify();
    cx.spawn(async (cx) => this.loadOptions(cx));
  }

  async loadOptions(cx) {
    // The services' own fields can be choices too (a database kind's), so walk what's shown, twice: a choice
    // can bring more fields with it.
    for (let pass = 0; pass < 2; pass++) {
      for (const f of this.shown()) {
        if (f.kind !== "choice") continue;
        const o = typeof f.options === "function" ? await f.options(this.ctx, this.values) : f.options;
        this.options[f.key] = o ?? [];
        if (this.values[f.key] === undefined && f.default !== undefined) this.values[f.key] = f.default;
        if (this.values[f.key] === undefined && this.options[f.key].length === 1 && f.required) this.values[f.key] = this.options[f.key][0].value;
      }
    }
    // Fields the services describe (a sign-in's, a compute kind's) appear with the choice.
    this.ensureStates();
    cx.notify();
  }

  /** Make the input state for every text field that's shown now. Never call it from render. */
  ensureStates() {
    for (const f of this.shown()) {
      if (f.kind === "choice" || f.kind === "toggle" || this.states[f.key]) continue;
      const long = f.kind === "long";
      // "e.g." so an example never reads as a value that's already filled in.
      const hint = f.placeholder ? `e.g. ${f.placeholder}` : "";
      const st = long ? TextareaState.new({ placeholder: hint, rows: 4 }) : InputState.new({ placeholder: hint });
      if (f.kind === "secret") st.set_masked?.(true);
      const start = this.values[f.key] ?? f.default;
      if (start !== undefined) st.set_value(String(start));
      st.on("change", () => { this.values[f.key] = st.value(); delete this.errors[f.key]; });
      this.states[f.key] = st;
    }
  }

  // Fields shown now, with the services' own fields (a sign-in's, a compute kind's) expanded.
  shown() {
    const out = [];
    for (const f of this.task.fields) {
      if (f.when && !f.when(this.values, this.ctx)) continue;
      if (f.kind === "dynamic") {
        for (const d of f.fields(this.ctx, this.values)) {
          const type = d.secret ? "secret" : d.type === "secret" ? "secret" : d.type === "choice" ? "choice" : "text";
          const choices = (d.choices ?? []).map((ch) => (typeof ch === "object" ? { value: ch.value, label: ch.label ?? String(ch.value) } : { value: ch, label: String(ch) }));
          out.push({ key: `af:${d.key ?? d.name}`, label: d.label ?? d.name, kind: type, required: Boolean(d.required), optional: Boolean(d.optional),
            default: d.default ?? undefined, placeholder: d.placeholder ?? undefined, help: d.help ?? undefined, ...(type === "choice" ? { options: choices } : {}) });
        }
      } else out.push(f);
    }
    return out;
  }

  // Many options (pipelines, environments, compute kinds): a short scrolling list, not a wall of chips.
  longChoice(c, f, opts) {
    return v_flex().id(`f-${f.key}-list`).max_h(184).overflow_y_scroll().border_1().border_color(c.grey300)
      .children(opts.map((o) => {
        const on = this.values[f.key] === o.value;
        return h_flex().id(`f-${f.key}-${o.value}`).h(30).flex_none().px(10).gap(8).items_center().cursor_pointer()
          .bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5).hover((s) => (on ? s : s.bg(c.surface2)))
          .on_click((_e, cx) => this.pick(cx, f, o))
          .when(Boolean(o.brand), (e) => e.child(Brand(c, o.brand, 14)))
          .child(div().flex_1().min_w(0).truncate().child(o.label))
          .when(Boolean(o.chip), (e) => e.child(Badge(c, o.chip, on ? "blue" : "neutral")));
      }));
  }

  // A picker of products (databases, places to run work): a grid of tiles, each with its mark, `columns` to a row.
  gridChoice(c, f, opts) {
    const n = f.columns ?? 3;
    const rows = [];
    for (let i = 0; i < opts.length; i += n) rows.push(opts.slice(i, i + n));
    return v_flex().gap(6).children(rows.map((row, r) => h_flex().gap(6).children([...row.map((o) => {
      const on = this.values[f.key] === o.value;
      return h_flex().id(`f-${f.key}-${o.value}`).flex_1().min_w(0).h(40).px(10).gap(8).items_center().cursor_pointer().border_1()
        .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5)
        .hover((s) => (on ? s : s.border_color(c.blue)))
        .on_click((_e, cx) => this.pick(cx, f, o))
        .child(Brand(c, o.brand ?? o.value, 18))
        .child(div().flex_1().min_w(0).truncate().child(o.label))
        .when(Boolean(o.chip), (e) => e.child(div().text_size(10.5).text_color(c.grey500).child(o.chip)));
    }), ...Array(n - row.length).fill(0).map((_, k) => div().id(`f-${f.key}-pad-${r}-${k}`).flex_1())])));
  }

  field(c, f) {
    const err = this.errors[f.key];
    // Help can depend on what's picked (a database kind's note: "Signs in with your AWS login").
    const help = typeof f.help === "function" ? f.help(this.ctx, this.values) : f.help;
    const label = h_flex().gap(6).items_baseline()
      .child(div().text_size(13).font_medium().text_color(c.navy).child(f.label))
      .when(Boolean(f.optional), (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("optional")));
    let control;
    if (f.kind === "choice") {
      const opts = this.options[f.key];
      control = !opts ? div().h(30).w(160).bg(c.surface2)
        : !opts.length ? div().text_size(12.5).text_color(c.grey500).child(f.empty ?? "Nothing to pick yet.")
        : f.grid ? this.gridChoice(c, f, opts)
        : opts.length > 8 ? this.longChoice(c, f, opts)
        : h_flex().flex_wrap().gap(6).children(opts.map((o) => {
          const on = this.values[f.key] === o.value;
          return h_flex().id(`f-${f.key}-${o.value}`).h(30).px(10).gap(6).items_center().cursor_pointer().border_1()
            .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5)
            .on_click((_e, cx) => this.pick(cx, f, o))
            .when(Boolean(o.brand), (e) => e.child(Brand(c, o.brand, 14)))
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
        .child(!this.states[f.key] ? div().h(14).w(120).bg(c.surface2)
          : long ? Textarea.new(this.states[f.key]).w_full().text_size(13).text_color(c.navy) : Input.new(this.states[f.key]).w_full().text_size(13).text_color(c.navy));
    }
    return v_flex().gap(5).child(label).child(control)
      .when(Boolean(err), (e) => e.child(div().text_size(12).text_color(c.fail).child(err)))
      .when(!err && Boolean(help), (e) => e.child(div().text_size(12).line_height("17px").text_color(c.grey500).child(help)));
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

  // The New menu: every task with its icon, keyboard up and down and Enter, scrolling inside its popup with a fade
  // at the bottom so nothing is cut off.
  menuItems() { return TASKS.filter((t) => !t.hidden); }

  openTask(t) {
    this.close();
    // Import a dataset is desktop-app's wizard (sources, tables, a preview); the form stays for agents.
    openOverlay(t.opens ?? (t.id === "dataset" ? IMPORT : { mod: "captain-forms", kind: "dialog", width: 560, props: { task: t.id } }));
  }

  menu(c) {
    const items = this.menuItems();
    const sel = Math.min(this.sel ?? 0, items.length - 1);
    return div().id("new-menu").size_full().relative().bg(c.surface).rounded(RADIUS).font_family(FONT).when(Boolean(this.menuFocus), (e) => e.track_focus(this.menuFocus))
      .on_key_down((e, cx) => {
        if (e.key === "down") { this.sel = (sel + 1) % items.length; cx.notify(); }
        else if (e.key === "up") { this.sel = (sel - 1 + items.length) % items.length; cx.notify(); }
        else if (e.key === "enter") this.openTask(items[sel]);
        else if (e.key === "escape") this.close();
      })
      .child(div().id("new-menu-list").size_full().overflow_y_scroll().child(v_flex().py(4).pb(18)
        .children(items.map((t, i) => {
          const on = i === sel;
          return h_flex().id(`new-${t.id}`).mx(4).px(10).py(6).gap(10).items_center().cursor_pointer()
            .bg(on ? c.blueLight : c.surface)
            .on_hover((hovered, cx) => { if (hovered && this.sel !== i) { this.sel = i; cx.notify(); } })
            .on_click(() => this.openTask(t))
            .child(div().size(26).flex_none().flex().items_center().justify_center().bg(on ? c.blue : c.blueLight).text_color(on ? c.onBlue : c.blue)
              .child(svg(icon(t.icon)).size(14)))
            .child(v_flex().flex_1().min_w(0).gap(1)
              .child(div().truncate().text_size(13).font_medium().text_color(on ? c.blue : c.navy).child(t.label))
              .child(div().truncate().text_size(11.5).text_color(c.grey500).child(t.about)));
        }))))
      // The fade: the list scrolls under it, so a row past the bottom shows it continues.
      .child(v_flex().absolute().bottom(0).left(0).right(0).children([0.35, 0.65, 0.9].map((o, i) => div().id(`new-fade-${i}`).h(6).w_full().bg(c.surface).opacity(o))));
  }


  render() {
    const c = palette(isDark(theme()));
    if (!this.task || this.isMenu) return this.menu(c);
    const t = this.task;
    const failed = this.result && !this.result.ok ? this.result.error : null;
    return v_flex().size_full().bg(c.surface).rounded(RADIUS).font_family(FONT).text_color(c.navy)
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
