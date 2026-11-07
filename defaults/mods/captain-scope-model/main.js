// CAPTAIN: Settings > Scope model (Edgar's spec, 2026-11-06). How this workspace organizes its engines:
//   Scope types   Brand and Site, plus your own (Region, Team, ...).
//   Terminology   each type's singular, plural and description; they write labels like "Add brand".
//   Hierarchy     which types may hold which; a choice that would make a loop is refused.
//   Explorer      the order types group in, and which levels show.
//   Metadata      fields every scope of a type carries.
// Reads and saves GET/PUT /api/scope-model (kit/scopes.js). Inputs are made when the model loads or a type or field
// is added, never in render.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, setShared, shared } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { Button, Tabs, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { api } from "./kit/cst.js";
import { DEFAULT_MODEL, makesCycle } from "./kit/scopes.js";

const TABS = [["types", "Scope types"], ["terms", "Terminology"], ["hierarchy", "Hierarchy"], ["explorer", "Explorer"], ["fields", "Metadata fields"]];
const clone = (x) => JSON.parse(JSON.stringify(x));
const slug = (s) => String(s).trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

export default class ScopeModel extends View {
  init(_props, cx) {
    this.tab = TABS.some(([id]) => id === props()?.tab) ? props().tab : "types";
    this.model = null; this.live = false; this.dirty = false; this.busy = false; this.error = null; this.saved = false;
    this.states = {};
    this.newType = InputState.new({ placeholder: "e.g. Region" });
    this.newField = InputState.new({ placeholder: "e.g. Owner" });
    cx.spawn(async (cx) => {
      if (shared("captain.explorer.sample")) { this.model = clone(DEFAULT_MODEL); }
      else {
        const r = await api("/api/scope-model", { scoped: false });
        this.live = r.ok && Boolean(r.data);
        this.model = clone(this.live ? r.data : DEFAULT_MODEL);
      }
      this.model.fields ??= [];
      this.model.explorer ??= { order: [...this.model.types.map((t) => t.id), "engine"], hidden: [] };
      this.makeStates();
      cx.notify();
    });
  }

  /** One input per term (singular, plural, description) of every type and of engines. */
  makeStates() {
    for (const t of [...this.model.types, { id: "engine", ...this.model.engine }]) {
      for (const k of ["singular", "plural", "description"]) {
        const key = `${t.id}.${k}`;
        if (this.states[key]) continue;
        const st = InputState.new({});
        st.set_value(String(t[k] ?? ""));
        st.on("change", () => { const target = t.id === "engine" ? this.model.engine : this.model.types.find((x) => x.id === t.id); if (target) target[k] = st.value(); this.touch(); });
        this.states[key] = st;
      }
    }
  }

  touch() { this.dirty = true; this.saved = false; this.error = null; }

  allKinds() { return [...this.model.types.map((t) => ({ id: t.id, label: t.singular })), { id: "engine", label: this.model.engine?.singular ?? "Engine" }]; }

  addType(cx) {
    const singular = this.newType.value().trim();
    const id = slug(singular);
    if (!id) { this.error = "Name the new scope type."; cx.notify(); return; }
    if (id === "engine" || id === "workspace" || this.model.types.some((t) => t.id === id)) { this.error = `There's already a scope type called ${singular}.`; cx.notify(); return; }
    this.model.types.push({ id, singular, plural: `${singular}s`, description: "", parents: ["workspace"] });
    this.model.explorer.order.splice(Math.max(0, this.model.explorer.order.indexOf("engine")), 0, id);
    this.model.engine.parents = [...new Set([...(this.model.engine.parents ?? []), id])];
    this.newType.set_value("");
    this.makeStates(); this.touch(); cx.notify();
  }

  removeType(cx, id) {
    this.model.types = this.model.types.filter((t) => t.id !== id);
    for (const t of this.model.types) t.parents = (t.parents ?? []).filter((p) => p !== id);
    this.model.engine.parents = (this.model.engine.parents ?? []).filter((p) => p !== id);
    this.model.explorer.order = this.model.explorer.order.filter((k) => k !== id);
    this.touch(); cx.notify();
  }

  toggleParent(cx, kind, parent) {
    const t = kind === "engine" ? this.model.engine : this.model.types.find((x) => x.id === kind);
    const has = (t.parents ?? []).includes(parent);
    if (!has && kind !== "engine" && makesCycle(this.model, kind, parent)) {
      this.error = `A ${t.singular.toLowerCase()} can't sit under a ${this.allKinds().find((k) => k.id === parent)?.label.toLowerCase() ?? parent}: that would make a loop.`;
      cx.notify(); return;
    }
    if (has && (t.parents ?? []).length === 1) { this.error = `A ${t.singular.toLowerCase()} needs at least one place it can go.`; cx.notify(); return; }
    t.parents = has ? t.parents.filter((p) => p !== parent) : [...(t.parents ?? []), parent];
    this.touch(); cx.notify();
  }

  moveOrder(cx, id, by) {
    const o = this.model.explorer.order;
    const i = o.indexOf(id), j = i + by;
    if (i < 0 || j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    this.touch(); cx.notify();
  }

  addField(cx) {
    const label = this.newField.value().trim();
    const key = slug(label).replace(/-/g, "_");
    if (!key) { this.error = "Name the field."; cx.notify(); return; }
    if (this.model.fields.some((f) => f.key === key)) { this.error = `There's already a field called ${label}.`; cx.notify(); return; }
    this.model.fields.push({ key, label, applies_to: this.allKinds().map((k) => k.id), required: false });
    this.newField.set_value("");
    this.touch(); cx.notify();
  }

  save(cx) {
    if (!this.live) { this.error = "This workspace's services don't keep a scope model yet, so nothing was saved."; cx.notify(); return; }
    this.busy = true; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/scope-model", { method: "PUT", body: this.model, scoped: false });
      this.busy = false;
      if (r.ok) { this.dirty = false; this.saved = true; setShared("captain.explorer.rev", Date.now()); }
      else this.error = plainError(r.error, "Captain couldn't save the scope model.");
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
      .child(this.intro(c, "The kinds of scope this workspace organizes engines by. Brand and Site are optional: a workspace can keep its engines at the top level."))
      .children(this.model.types.map((t) => h_flex().id(`type-${t.id}`).h(40).px(12).gap(10).items_center().border_1().border_color(c.rule)
        .child(div().flex_1().min_w(0).child(div().text_size(13).font_semibold().child(t.singular)).child(div().text_size(11.5).text_color(c.grey500).truncate().child(t.description || "No description yet.")))
        .child(t.builtin ? div().text_size(11.5).text_color(c.grey500).child("Built in")
          : Button(c, `type-rm-${t.id}`, "Remove", (_e, cx) => this.removeType(cx, t.id), { small: true }))))
      .child(h_flex().pt(6).gap(8).items_center()
        .child(div().w(220).child(this.input(c, "type-new", this.newType)))
        .child(Button(c, "type-add", "Add a scope type", (_e, cx) => this.addType(cx), { small: true })));
  }

  terms(c) {
    return v_flex().gap(14)
      .child(this.intro(c, "The words Captain uses for each type: in the Explorer, its menus (\"Add brand\") and every screen."))
      .children([...this.model.types, { id: "engine", ...this.model.engine }].map((t) => v_flex().gap(6)
        .child(div().text_size(12.5).font_semibold().child(t.id === "engine" ? "Engines" : t.singular || t.id))
        .child(h_flex().gap(8)
          .child(v_flex().flex_1().gap(3).child(div().text_size(11.5).text_color(c.grey500).child("Singular")).child(this.input(c, `t-${t.id}-s`, this.states[`${t.id}.singular`])))
          .child(v_flex().flex_1().gap(3).child(div().text_size(11.5).text_color(c.grey500).child("Plural")).child(this.input(c, `t-${t.id}-p`, this.states[`${t.id}.plural`]))))
        .child(v_flex().gap(3).child(div().text_size(11.5).text_color(c.grey500).child("Description")).child(this.input(c, `t-${t.id}-d`, this.states[`${t.id}.description`])))));
  }

  hierarchy(c) {
    const kinds = this.allKinds();
    const parents = [{ id: "workspace", label: "Top level" }, ...this.model.types.map((t) => ({ id: t.id, label: t.singular }))];
    return v_flex().gap(12)
      .child(this.intro(c, "Where each kind may sit. A choice that would put a type inside itself is refused."))
      .children(kinds.map((k) => {
        const t = k.id === "engine" ? this.model.engine : this.model.types.find((x) => x.id === k.id);
        return v_flex().gap(6).child(div().text_size(12.5).font_semibold().child(`${k.label} can sit under`))
          .child(h_flex().flex_wrap().gap(6).children(parents.filter((p) => p.id !== k.id).map((p) =>
            this.chip(c, `h-${k.id}-${p.id}`, p.label, (t.parents ?? []).includes(p.id), (_e, cx) => this.toggleParent(cx, k.id, p.id)))));
      }));
  }

  explorer(c) {
    const o = this.model.explorer.order;
    const label = (id) => this.allKinds().find((k) => k.id === id)?.label ?? id;
    const hidden = new Set(this.model.explorer.hidden ?? []);
    return v_flex().gap(8)
      .child(this.intro(c, "How the Explorer groups the tree, top to bottom, and which levels it shows. A hidden level's children move up a level."))
      .children(o.map((id, i) => h_flex().id(`ord-${id}`).h(36).px(12).gap(8).items_center().border_1().border_color(c.rule)
        .child(div().w(18).text_size(12).text_color(c.grey500).child(String(i + 1)))
        .child(div().flex_1().text_size(13).text_color(hidden.has(id) ? c.grey500 : c.navy).child(label(id)))
        .child(this.chip(c, `vis-${id}`, hidden.has(id) ? "Hidden" : "Shown", !hidden.has(id), (_e, cx) => {
          const h = new Set(this.model.explorer.hidden ?? []);
          if (h.has(id)) h.delete(id); else h.add(id);
          this.model.explorer.hidden = [...h]; this.touch(); cx.notify();
        }))
        .child(Button(c, `ord-up-${id}`, "Up", (_e, cx) => this.moveOrder(cx, id, -1), { small: true, disabled: i === 0 }))
        .child(Button(c, `ord-down-${id}`, "Down", (_e, cx) => this.moveOrder(cx, id, 1), { small: true, disabled: i === o.length - 1 }))));
  }

  fields(c) {
    const kinds = this.allKinds();
    return v_flex().gap(10)
      .child(this.intro(c, "Facts every scope of a type carries, like an owner or a market. Captain asks for them when someone adds one."))
      .children(this.model.fields.map((f) => v_flex().id(`fld-${f.key}`).gap(6).p(10).border_1().border_color(c.rule)
        .child(h_flex().items_center().child(div().flex_1().text_size(13).font_semibold().child(f.label ?? f.key))
          .child(Button(c, `fld-rm-${f.key}`, "Remove", (_e, cx) => { this.model.fields = this.model.fields.filter((x) => x.key !== f.key); this.touch(); cx.notify(); }, { small: true })))
        .child(h_flex().flex_wrap().gap(6).children(kinds.map((k) => this.chip(c, `fld-${f.key}-${k.id}`, k.label, (f.applies_to ?? []).includes(k.id), (_e, cx) => {
          const s = new Set(f.applies_to ?? []);
          if (s.has(k.id)) s.delete(k.id); else s.add(k.id);
          f.applies_to = [...s]; this.touch(); cx.notify();
        }))))))
      .when(!this.model.fields.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("No fields yet.")))
      .child(h_flex().pt(6).gap(8).items_center()
        .child(div().w(220).child(this.input(c, "fld-new", this.newField)))
        .child(Button(c, "fld-add", "Add a field", (_e, cx) => this.addField(cx), { small: true })));
  }

  render() {
    const c = palette(isDark(theme()));
    const body = !this.model ? v_flex().gap(8).children([0, 1, 2, 3].map((i) => div().id(`sm-sk-${i}`).h(36).bg(c.surface2)))
      : { types: this.types, terms: this.terms, hierarchy: this.hierarchy, explorer: this.explorer, fields: this.fields }[this.tab].call(this, c);
    return v_flex().id("scope-model").size_full().bg(c.surface).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(52).flex_none().px(20).gap(10).items_center()
        .child(div().size(28).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("waypoints")).size(15)))
        .child(div().flex_1().text_size(15).font_semibold().child("Scope model"))
        .child(div().id("sm-close").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => closeOverlay(props()?.overlay)).child(this.dirty ? "Cancel" : "Close")))
      .child(div().px(20).child(Tabs(c, "sm-tabs", TABS.map(([id, label]) => ({ id, label })), this.tab, (id, cx) => { this.tab = id; this.error = null; cx.notify(); })))
      .child(div().id("sm-body").flex_1().min_h(0).overflow_y_scroll().child(div().px(20).py(16).child(body)))
      .child(h_flex().h(60).flex_none().px(20).gap(10).items_center().border_t(1).border_color(c.rule)
        .child(div().flex_1().min_w(0).text_size(12.5).text_color(this.error ? c.fail : c.good)
          .child(this.error ?? (this.saved ? "Saved." : this.model && !this.live ? "Not saved anywhere yet: this workspace's services don't keep a scope model." : "")))
        .child(Button(c, "sm-save", this.busy ? "Saving..." : "Save", (_e, cx) => this.save(cx), { kind: "primary", disabled: !this.dirty || this.busy })));
  }
}
