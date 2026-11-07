// CAPTAIN: one action from the Explorer's menus, on the services' routes (kit/scopes.js):
//   add      a scope of a type (Brand, Site or your own) with that type's fields, or an engine (search or
//            recommendation), inside a scope or at the top level;
//   rename   a scope or engine;
//   move     to the top level or a scope the type allows, never inside itself (the services check it again);
//   archive  it leaves the Explorer; its records stay.
// Props: {action: "add", kind: "scope" | "engine", type, parent: {id, type, name} | null}
//      | {action: "rename" | "move" | "archive", node: {kind, id, type, name}}
// With no props (the palette, a test) it adds an engine at the top level. After a change it bumps shared
// "captain.explorer.rev" so the Explorer re-reads.
import { View, div } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, setShared, shared } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { Button, plainError } from "./kit/ui.js";
import { loadExplorer, words, scopeAction, moveTargets } from "./kit/scopes.js";

const EXAMPLE = { brand: "Acme Outdoors", site: "US storefront", engine: "Product search" };

export default class Scope extends View {
  init(_props, cx) {
    const given = props() ?? {};
    this.p = given.action ? given : { action: "add", kind: "engine", type: "engine", parent: null, ...given };
    this.model = null; this.tree = null; this.live = false;
    this.target = undefined; this.engineType = "search"; this.busy = false; this.error = null;
    const ex = EXAMPLE[this.p.type];
    this.name = InputState.new({ placeholder: this.p.action === "add" && ex ? `e.g. ${ex}` : "" });
    if (this.p.action === "rename") this.name.set_value(this.p.node?.name ?? "");
    this.name.on("change", () => { this.error = null; });
    this.name.on("submit", (_e, cx) => this.save(cx));
    this.fieldStates = {};
    cx.spawn(async (cx) => {
      const r = await loadExplorer();
      this.model = r.model; this.tree = r.tree; this.live = r.live;
      this.engineType = (this.model.engine_types ?? ["search"])[0];
      for (const f of this.fields()) this.fieldStates[f.key] = InputState.new({ placeholder: f.placeholder ? `e.g. ${f.placeholder}` : "" });
      cx.notify();
    });
  }

  typeKey() { return this.p.action === "add" ? this.p.type : this.p.node?.kind === "engine" ? "engine" : this.p.node?.type; }

  // The type's own fields, asked for when a scope of it is added.
  fields() {
    if (this.p.action !== "add" || this.p.kind !== "scope") return [];
    return ((this.model?.types ?? []).find((t) => t.key === this.p.type)?.fields ?? []).filter((f) => f && f.key);
  }

  title() {
    const name = this.p.node?.name;
    return { add: words(this.model, this.p.type).add, rename: `Rename ${name}`, move: `Move ${name}`, archive: `Archive ${name}` }[this.p.action] ?? "";
  }

  save(cx) {
    if (this.busy) return;
    const a = this.p.action;
    const name = this.name.value().trim();
    if ((a === "add" || a === "rename") && !name) { this.error = "Give it a name."; cx.notify(); return; }
    if (a === "move" && this.target === undefined) { this.error = "Pick where it goes."; cx.notify(); return; }
    if (!this.live) { this.error = "This workspace's services don't keep scopes yet, so nothing was saved."; cx.notify(); return; }
    const fields = {};
    for (const f of this.fields()) { const v = this.fieldStates[f.key]?.value().trim(); if (v) fields[f.key] = v; }
    this.busy = true; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await scopeAction(a, {
        kind: this.p.kind, type: this.p.type, node: this.p.node, name, fields, engineType: this.engineType,
        parent: a === "move" ? this.target : this.p.parent?.id ?? null,
      });
      this.busy = false;
      // The services refuse with a plain reason ("Site can't move there"): show it as it is.
      if (!r.ok) { this.error = plainError(r.error, "Captain couldn't save that."); cx.notify(); return; }
      setShared("captain.explorer.rev", Date.now());
      const picked = shared("captain.scope");
      if ((a === "archive" || a === "move") && picked?.id === this.p.node?.id) setShared("captain.scope", null);
      closeOverlay(props()?.overlay);
    });
  }

  input(c, id, st) {
    return div().id(id).h(32).flex().items_center().px(10).bg(c.surface).border_1().border_color(c.grey300).cursor_text()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(st).w_full().text_size(13).text_color(c.navy));
  }

  choice(c, id, opts, value, pick) {
    return h_flex().flex_wrap().gap(6).children(opts.map((o) => {
      const on = o.id === value;
      return div().id(`${id}-${o.id}`).h(30).px(10).flex().items_center().cursor_pointer().text_size(12.5).border_1()
        .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
        .on_click((_e, cx) => { pick(o.id); this.error = null; cx.notify(); }).child(o.label);
    }));
  }

  body(c) {
    const a = this.p.action;
    const w = words(this.model, this.typeKey());
    const label = (t, optional) => h_flex().gap(6).items_center().child(div().text_size(12.5).font_semibold().text_color(c.navy).child(t))
      .when(Boolean(optional), (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("optional")));
    if (a === "archive") {
      return div().text_size(13).line_height("20px").text_color(c.grey900)
        .child(`${this.p.node?.name} leaves the Explorer and stops filtering the views. Its runs, datasets and decisions stay.`);
    }
    if (a === "move") {
      const targets = moveTargets(this.tree, this.model, this.p.node ?? {});
      return v_flex().gap(8).child(label("Move it to"))
        .child(!this.tree ? div().h(30).w(200).bg(c.surface2)
          : v_flex().id("scope-targets").max_h(240).overflow_y_scroll().border_1().border_color(c.grey300).children(targets.map((t, i) => {
            const on = this.target === t.id;
            return div().id(`scope-t-${i}`).h(30).px(10).flex().items_center().text_size(12.5).cursor_pointer()
              .bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).hover((s) => (on ? s : s.bg(c.surface2)))
              .on_click((_e, cx) => { this.target = t.id; this.error = null; cx.notify(); }).child(div().truncate().child(t.label));
          })))
        .child(div().text_size(12).text_color(c.grey500).child(`Scope model sets where a ${w.singular.toLowerCase()} may go.`));
    }
    const types = (this.model?.engine_types ?? ["search", "recommendation"]).map((t) => ({ id: t, label: t === "recommendation" ? "Recommendations" : t === "search" ? "Search" : t }));
    return v_flex().gap(12)
      .when(a === "add" && Boolean(w.description), (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(w.description)))
      .when(a === "add" && Boolean(this.p.parent?.name), (e) => e.child(div().text_size(12.5).text_color(c.grey500).child(`In ${this.p.parent.name}.`)))
      .child(v_flex().gap(6).child(label("Name")).child(this.input(c, "scope-name", this.name)))
      .when(a === "add" && this.p.kind === "engine", (e) => e.child(v_flex().gap(6).child(label("It serves"))
        .child(this.choice(c, "scope-etype", types, this.engineType, (v) => { this.engineType = v; }))))
      .children(this.fields().map((f) => v_flex().gap(6).child(label(f.label ?? f.key, !f.required))
        .child(this.fieldStates[f.key] ? this.input(c, `scope-f-${f.key}`, this.fieldStates[f.key]) : div().h(32).bg(c.surface2))));
  }

  render() {
    const c = palette(isDark(theme()));
    const verb = { add: "Add", rename: "Rename", move: "Move", archive: "Archive" }[this.p.action] ?? "Save";
    return v_flex().id("scope").size_full().bg(c.surface).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(52).flex_none().px(20).items_center().border_b(1).border_color(c.rule)
        .child(div().flex_1().min_w(0).truncate().text_size(15).font_semibold().child(this.title()))
        .child(div().id("scope-cancel").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => closeOverlay(props()?.overlay)).child("Cancel")))
      .child(v_flex().flex_1().min_h(0).px(20).py(16).gap(10).child(this.body(c))
        .when(Boolean(this.error), (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.error))))
      .child(h_flex().h(60).flex_none().px(20).gap(8).items_center().justify_end().border_t(1).border_color(c.rule)
        .child(Button(c, "scope-cancel-b", "Cancel", () => closeOverlay(props()?.overlay)))
        .child(Button(c, "scope-save", this.busy ? `${verb}...` : verb, (_e, cx) => this.save(cx), { kind: "primary", disabled: this.busy })));
  }
}
