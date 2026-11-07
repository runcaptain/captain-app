// CAPTAIN: Settings > Scope model (Edgar's spec, 2026-11-06), on GET/PUT /api/scope-model (kit/scopes.js):
//   Scope types      Brand and Site, plus your own (Region, Team, ...). Keys are stable; words are free to change.
//   Terminology      each type's singular, plural and description, which write labels like "Add brand".
//   Hierarchy        which types may hold which; any type may also sit at the top level. A loop is refused here,
//                    and the services check everything again.
//   Explorer         the order types group in, and which levels show.
//   Metadata fields  facts each type's scopes carry, asked for when one is added.
// Saving sends expect_revision; a refusal comes back as plain reasons, shown as they are.
// Inputs are made when the model loads or a type is added, never in render.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, setShared, shared } from "herdr";
import { palette, isDark, FONT, RADIUS } from "./kit/tokens.js";
import { Button, Tabs } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { api } from "./kit/cst.js";
import { DEFAULT_MODEL, makesCycle } from "./kit/scopes.js";

const TABS = [["types", "Scope types"], ["terms", "Terminology"], ["hierarchy", "Hierarchy"], ["explorer", "Explorer"], ["fields", "Metadata fields"]];
const clone = (x) => JSON.parse(JSON.stringify(x));
const keyOf = (s) => String(s).trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^(\d)/, "t$1").slice(0, 32);

export default class ScopeModel extends View {
  init(_props, cx) {
    this.tab = TABS.some(([id]) => id === props()?.tab) ? props().tab : "types";
    this.model = null; this.live = false; this.dirty = false; this.busy = false; this.error = null; this.saved = false;
    this.states = {};
    this.newType = InputState.new({ placeholder: "e.g. Region" });
    this.newField = {};
    cx.spawn(async (cx) => {
      const r = shared("captain.explorer.sample") ? null : await api("/api/scope-model", { scoped: false });
      this.live = Boolean(r?.ok && r.data);
      this.model = clone(this.live ? r.data : DEFAULT_MODEL);
      this.model.explorer ??= { order: this.model.types.map((t) => t.key), hidden: [] };
      this.model.explorer.hidden ??= [];
      for (const t of this.model.types) t.fields ??= [];
      this.makeStates();
      cx.notify();
    });
  }

  /** Inputs for every type's words, and one "new field" input per type. */
  makeStates() {
    for (const t of this.model.types) {
      for (const k of ["singular", "plural", "description"]) {
        const key = `${t.key}.${k}`;
        if (this.states[key]) continue;
        const st = InputState.new({});
        st.set_value(String(t[k] ?? ""));
        st.on("change", () => { const target = this.model.types.find((x) => x.key === t.key); if (target) target[k] = st.value(); this.touch(); });
        this.states[key] = st;
      }
      this.newField[t.key] ??= InputState.new({ placeholder: "e.g. Owner" });
    }
  }

  touch() { this.dirty = true; this.saved = false; this.error = null; }
  label(key) { return this.model.types.find((t) => t.key === key)?.singular ?? key; }

  addType(cx) {
    const singular = this.newType.value().trim();
    const key = keyOf(singular);
    if (!key) { this.error = "Name the new scope type."; cx.notify(); return; }
    if (key === "engine" || key === "project" || this.model.types.some((t) => t.key === key)) { this.error = `There's already a type called ${singular}.`; cx.notify(); return; }
    this.model.types.push({ key, singular, plural: `${singular}s`, description: "", parents: [], fields: [] });
    this.model.explorer.order.push(key);
    this.newType.set_value("");
    this.makeStates(); this.touch(); cx.notify();
  }

  removeType(cx, key) {
    this.model.types = this.model.types.filter((t) => t.key !== key);
    for (const t of this.model.types) t.parents = (t.parents ?? []).filter((p) => p !== key);
    this.model.explorer.order = this.model.explorer.order.filter((k) => k !== key);
    this.model.explorer.hidden = this.model.explorer.hidden.filter((k) => k !== key);
    this.touch(); cx.notify();
  }

  toggleParent(cx, key, parent) {
    const t = this.model.types.find((x) => x.key === key);
    const has = (t.parents ?? []).includes(parent);
    if (!has && makesCycle(this.model, key, parent)) {
      this.error = `A ${t.singular.toLowerCase()} can't sit inside a ${this.label(parent).toLowerCase()}: that would make a loop.`;
      cx.notify(); return;
    }
    t.parents = has ? t.parents.filter((p) => p !== parent) : [...(t.parents ?? []), parent];
    this.touch(); cx.notify();
  }

  moveOrder(cx, key, by) {
    const o = this.model.explorer.order;
    const i = o.indexOf(key), j = i + by;
    if (i < 0 || j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    this.touch(); cx.notify();
  }

  addField(cx, t) {
    const label = this.newField[t.key].value().trim();
    const key = keyOf(label);
    if (!key) { this.error = "Name the field."; cx.notify(); return; }
    if (t.fields.some((f) => f.key === key)) { this.error = `${t.plural} already have a field called ${label}.`; cx.notify(); return; }
    t.fields.push({ key, label, required: false });
    this.newField[t.key].set_value("");
    this.touch(); cx.notify();
  }

  save(cx) {
    if (!this.live) { this.error = "This workspace's services don't keep a scope model yet, so nothing was saved."; cx.notify(); return; }
    this.busy = true; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const { types, explorer, engine_types } = this.model;
      const r = await api("/api/scope-model", { method: "PUT", body: { types, explorer, engine_types, expect_revision: this.model.revision }, scoped: false });
      this.busy = false;
      if (r.ok) {
        this.model.revision = r.data?.revision ?? (this.model.revision ?? 1) + 1;
        this.dirty = false; this.saved = true;
        setShared("captain.explorer.rev", Date.now());
      } else {
        // The services' reasons, as they are ("the hierarchy has a loop: ...", "the scope model changed ...").
        this.error = String(r.error || "Captain couldn't save the scope model.");
      }
      cx.notify();
    });
  }

  input(c, id, st) {
    return div().id(id).h(30).flex().items_center().px(8).bg(c.surface).border_1().border_color(c.grey300).cursor_text()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(st ? Input.new(st).w_full().text_size(12.5).text_color(c.navy) : div());
  }

  chip(c, id, label, on, onClick) {
    return h_flex().id(id).h(26).px(8).gap(5).items_center().cursor_pointer().text_size(12)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
      .on_click(onClick).when(on, (e) => e.child(svg(icon("check")).size(11))).child(label);
  }

  intro(c, text) { return div().pb(12).text_size(12.5).line_height("19px").text_color(c.grey900).child(text); }

  types(c) {
    return v_flex().gap(8)
      .child(this.intro(c, "The kinds of scope this workspace organizes engines by. They're optional: a workspace can keep its engines at the top level."))
      .children(this.model.types.map((t) => h_flex().id(`type-${t.key}`).h(42).px(12).gap(10).items_center().border_1().border_color(c.rule)
        .child(div().flex_1().min_w(0).child(div().text_size(13).font_semibold().child(t.singular || t.key))
          .child(div().text_size(11.5).text_color(c.grey500).truncate().child(t.description || "No description yet.")))
        .child(Button(c, `type-rm-${t.key}`, "Remove", (_e, cx) => this.removeType(cx, t.key), { small: true }))))
      .child(h_flex().pt(6).gap(8).items_center()
        .child(div().w(220).child(this.input(c, "type-new", this.newType)))
        .child(Button(c, "type-add", "Add a scope type", (_e, cx) => this.addType(cx), { small: true })))
      .child(div().pt(4).text_size(12).text_color(c.grey500).child("Removing a type that scopes still use is refused when you save: archive or move them first."));
  }

  terms(c) {
    return v_flex().gap(14)
      .child(this.intro(c, "The words Captain uses for each type: in the Explorer, its menus (\"Add brand\") and every screen."))
      .children(this.model.types.map((t) => v_flex().gap(6)
        .child(div().text_size(12.5).font_semibold().child(t.singular || t.key))
        .child(h_flex().gap(8)
          .child(v_flex().flex_1().gap(3).child(div().text_size(11.5).text_color(c.grey500).child("Singular")).child(this.input(c, `t-${t.key}-s`, this.states[`${t.key}.singular`])))
          .child(v_flex().flex_1().gap(3).child(div().text_size(11.5).text_color(c.grey500).child("Plural")).child(this.input(c, `t-${t.key}-p`, this.states[`${t.key}.plural`]))))
        .child(v_flex().gap(3).child(div().text_size(11.5).text_color(c.grey500).child("Description")).child(this.input(c, `t-${t.key}-d`, this.states[`${t.key}.description`])))));
  }

  hierarchy(c) {
    return v_flex().gap(12)
      .child(this.intro(c, "Which types each type may sit inside. Every type may also sit at the top level, and engines can belong to any scope."))
      .children(this.model.types.map((t) => v_flex().gap(6).child(div().text_size(12.5).font_semibold().child(`${t.singular || t.key} can sit inside`))
        .child(h_flex().flex_wrap().gap(6)
          .children(this.model.types.filter((p) => p.key !== t.key).map((p) =>
            this.chip(c, `h-${t.key}-${p.key}`, p.singular || p.key, (t.parents ?? []).includes(p.key), (_e, cx) => this.toggleParent(cx, t.key, p.key))))
          .when(this.model.types.length < 2, (e) => e.child(div().text_size(12).text_color(c.grey500).child("Add another type first."))))));
  }

  explorer(c) {
    const o = this.model.explorer.order;
    const hidden = new Set(this.model.explorer.hidden ?? []);
    return v_flex().gap(8)
      .child(this.intro(c, "How the Explorer groups the tree, top to bottom, and which levels it shows. A hidden level's scopes step aside and what's inside them moves up."))
      .children(o.map((key, i) => h_flex().id(`ord-${key}`).h(36).px(12).gap(8).items_center().border_1().border_color(c.rule)
        .child(div().w(18).text_size(12).text_color(c.grey500).child(String(i + 1)))
        .child(div().flex_1().text_size(13).text_color(hidden.has(key) ? c.grey500 : c.navy).child(this.label(key)))
        .child(this.chip(c, `vis-${key}`, hidden.has(key) ? "Hidden" : "Shown", !hidden.has(key), (_e, cx) => {
          const h = new Set(this.model.explorer.hidden ?? []);
          if (h.has(key)) h.delete(key); else h.add(key);
          this.model.explorer.hidden = [...h]; this.touch(); cx.notify();
        }))
        .child(Button(c, `ord-up-${key}`, "Up", (_e, cx) => this.moveOrder(cx, key, -1), { small: true, disabled: i === 0 }))
        .child(Button(c, `ord-down-${key}`, "Down", (_e, cx) => this.moveOrder(cx, key, 1), { small: true, disabled: i === o.length - 1 }))))
      .child(div().pt(4).text_size(12).text_color(c.grey500).child("Engines always show under the deepest scope they belong to."));
  }

  fields(c) {
    return v_flex().gap(14)
      .child(this.intro(c, "Facts each type's scopes carry, like an owner or a market. Captain asks for them when someone adds one."))
      .children(this.model.types.map((t) => v_flex().gap(6)
        .child(div().text_size(12.5).font_semibold().child(t.plural || t.key))
        .children(t.fields.map((f) => h_flex().id(`fld-${t.key}-${f.key}`).h(32).px(10).gap(8).items_center().border_1().border_color(c.rule)
          .child(div().flex_1().text_size(12.5).child(f.label ?? f.key))
          .child(this.chip(c, `fld-req-${t.key}-${f.key}`, "Required", Boolean(f.required), (_e, cx) => { f.required = !f.required; this.touch(); cx.notify(); }))
          .child(Button(c, `fld-rm-${t.key}-${f.key}`, "Remove", (_e, cx) => { t.fields = t.fields.filter((x) => x.key !== f.key); this.touch(); cx.notify(); }, { small: true }))))
        .when(!t.fields.length, (e) => e.child(div().text_size(12).text_color(c.grey500).child("No fields yet.")))
        .child(h_flex().gap(8).items_center()
          .child(div().w(200).child(this.input(c, `fld-new-${t.key}`, this.newField[t.key])))
          .child(Button(c, `fld-add-${t.key}`, "Add a field", (_e, cx) => this.addField(cx, t), { small: true })))));
  }

  render() {
    const c = palette(isDark(theme()));
    const body = !this.model ? v_flex().gap(8).children([0, 1, 2, 3].map((i) => div().id(`sm-sk-${i}`).h(36).bg(c.surface2)))
      : { types: this.types, terms: this.terms, hierarchy: this.hierarchy, explorer: this.explorer, fields: this.fields }[this.tab].call(this, c);
    return v_flex().id("scope-model").size_full().bg(c.surface).rounded(RADIUS).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(52).flex_none().px(20).gap(10).items_center()
        .child(div().size(28).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("waypoints")).size(15)))
        .child(div().flex_1().text_size(15).font_semibold().child("Scope model"))
        .child(div().id("sm-close").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => closeOverlay(props()?.overlay)).child(this.dirty ? "Cancel" : "Close")))
      .child(div().px(20).child(Tabs(c, "sm-tabs", TABS.map(([id, label]) => ({ id, label })), this.tab, (id, cx) => { this.tab = id; cx.notify(); })))
      .child(div().id("sm-body").flex_1().min_h(0).overflow_y_scroll().child(div().px(20).py(16).child(body)))
      .child(h_flex().min_h(60).flex_none().px(20).py(10).gap(10).items_center().border_t(1).border_color(c.rule)
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(this.error ? c.fail : c.good)
          .child(this.error ?? (this.saved ? "Saved." : this.model && !this.live ? "Not saved anywhere yet: this workspace's services don't keep a scope model." : "")))
        .child(Button(c, "sm-save", this.busy ? "Saving..." : "Save", (_e, cx) => this.save(cx), { kind: "primary", disabled: !this.dirty || this.busy })));
  }
}
