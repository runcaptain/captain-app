// CAPTAIN: one scope action from the Explorer's menus: Add (a brand, site, engine, project or custom type, with the
// scope model's metadata fields), Rename, Move (to an allowed parent, never under itself) or Archive.
// Props: {action: "add", kind, parent} (the default: add an engine at the top level) | {action: "rename" | "move" | "archive", node: {id, kind, label}}.
// Writes go through kit/scopes.js scopeAction, then bump shared "captain.explorer.rev" so the Explorer re-reads.
import { View, div } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, setShared } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { Button, plainError } from "./kit/ui.js";
import { loadExplorer, words, scopeAction, moveTargets } from "./kit/scopes.js";

export default class Scope extends View {
  init(_props, cx) {
    // Opened with no action (the palette, a test): add an engine at the top level.
    this.p = { action: "add", kind: "engine", parent: { id: "ws", kind: "workspace" }, ...(props() ?? {}) };
    this.model = null; this.tree = null; this.live = false;
    this.target = null; this.busy = false; this.error = null;
    this.name = InputState.new({ placeholder: this.p.action === "add" ? `e.g. ${this.example()}` : "" });
    if (this.p.action === "rename") this.name.set_value(this.p.node?.label ?? "");
    this.name.on("change", () => { this.error = null; });
    this.name.on("submit", (_e, cx) => this.save(cx));
    this.fieldStates = {};
    cx.spawn(async (cx) => {
      const r = await loadExplorer(null);
      this.model = r.model; this.tree = r.tree; this.live = r.live;
      for (const f of this.fields()) { const st = InputState.new({ placeholder: f.placeholder ? `e.g. ${f.placeholder}` : "" }); this.fieldStates[f.key] = st; }
      cx.notify();
    });
  }

  example() {
    return { brand: "Acme Outdoors", site: "US storefront", engine: "Product search", project: "Session-aware ranking" }[this.p.kind] ?? "";
  }

  kind() { return this.p.kind ?? this.p.node?.kind; }

  // The scope model's metadata fields that apply to this kind.
  fields() {
    if (this.p.action !== "add") return [];
    return (this.model?.fields ?? []).filter((f) => !f.applies_to || f.applies_to.includes(this.kind()));
  }

  title() {
    const w = words(this.model, this.kind());
    const label = this.p.node?.label;
    return { add: w.add, rename: `Rename ${label}`, move: `Move ${label}`, archive: `Archive ${label}` }[this.p.action] ?? "";
  }

  save(cx) {
    if (this.busy) return;
    const a = this.p.action;
    const name = this.name.value().trim();
    if ((a === "add" || a === "rename") && !name) { this.error = "Give it a name."; cx.notify(); return; }
    if (a === "move" && !this.target) { this.error = "Pick where it goes."; cx.notify(); return; }
    if (!this.live) { this.error = "This workspace's services don't keep scopes yet, so nothing was saved."; cx.notify(); return; }
    const fields = {};
    for (const f of this.fields()) { const v = this.fieldStates[f.key]?.value().trim(); if (v) fields[f.key] = v; }
    this.busy = true; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await scopeAction(a, { id: this.p.node?.id, kind: this.p.kind, name, parent: a === "move" ? this.target : this.p.parent?.id ?? null, fields });
      this.busy = false;
      if (!r.ok) { this.error = plainError(r.error, "Captain couldn't save that."); cx.notify(); return; }
      setShared("captain.explorer.rev", Date.now());
      if (a === "archive" || a === "move") setShared("captain.scope", null);
      closeOverlay(props()?.overlay);
    });
  }

  input(c, id, st) {
    return div().id(id).h(32).flex().items_center().px(10).bg(c.surface).border_1().border_color(c.grey300).cursor_text()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(st).w_full().text_size(13).text_color(c.navy));
  }

  body(c) {
    const a = this.p.action;
    const w = words(this.model, this.kind());
    const label = (t, optional) => h_flex().gap(6).items_center().child(div().text_size(12.5).font_semibold().text_color(c.navy).child(t))
      .when(Boolean(optional), (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("optional")));
    if (a === "archive") {
      return v_flex().gap(10).child(div().text_size(13).line_height("20px").text_color(c.grey900)
        .child(`${this.p.node?.label} leaves the Explorer and stops filtering the views. Its runs, datasets and decisions stay.`));
    }
    if (a === "move") {
      const targets = moveTargets(this.tree, this.model, this.p.node ?? {});
      return v_flex().gap(8).child(label("Move it under"))
        .child(!this.tree ? div().h(30).w(200).bg(c.surface2)
          : !targets.length ? div().text_size(12.5).text_color(c.grey500).child(`There's nowhere else a ${w.singular.toLowerCase()} can go. Scope model sets who may hold whom.`)
          : v_flex().max_h(220).overflow_y_scroll().id("scope-targets").border_1().border_color(c.grey300).children(targets.map((t) => {
            const on = this.target === t.id;
            return div().id(`scope-t-${t.id}`).h(30).px(10).flex().items_center().text_size(12.5).cursor_pointer()
              .bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).hover((s) => (on ? s : s.bg(c.surface2)))
              .on_click((_e, cx) => { this.target = t.id; this.error = null; cx.notify(); }).child(div().truncate().child(t.label));
          })));
    }
    return v_flex().gap(12)
      .when(a === "add" && Boolean(w.description), (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(w.description)))
      .when(a === "add" && Boolean(this.p.parent?.label) && this.p.parent?.kind !== "workspace", (e) => e.child(div().text_size(12.5).text_color(c.grey500).child(`In ${this.p.parent.label}.`)))
      .child(v_flex().gap(6).child(label("Name")).child(this.input(c, "scope-name", this.name)))
      .children(this.fields().map((f) => v_flex().gap(6).child(label(f.label ?? f.key, !f.required))
        .child(this.fieldStates[f.key] ? this.input(c, `scope-f-${f.key}`, this.fieldStates[f.key]) : div().h(32).bg(c.surface2))));
  }

  render() {
    const c = palette(isDark(theme()));
    const a = this.p.action;
    const verb = { add: "Add", rename: "Rename", move: "Move", archive: "Archive" }[a] ?? "Save";
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
