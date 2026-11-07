// CAPTAIN: the Explorer, under the workspace selector (Edgar's spec, 2026-11-06). The tree is the services'
// (GET /api/explorer, kit/scopes.js): All engines, All experiments, the scope groups (Brand > Site > Engine by
// default, Brand and Site optional), unscoped engines, Projects, and Settings > Scope model. Selecting a node sets
// shared "captain.scope" = {kind: scope | engine | project | view, id, label}, which the five sections read to
// filter to it and below; "All engines" is the whole workspace. Each scope's and engine's menu holds its actions
// (Add brand, Add site, Add engine, Rename, Move, Archive), which open captain-scope.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { shared, setShared, openOverlay, run, layout } from "herdr";
import { icon } from "./kit/icons.js";
import { loadExplorer, words, childKinds, nodeIcon, nodeName, nodeKey, currentScope } from "./kit/scopes.js";

const INDENT = 12;
// A click this soon after the tree appears, or after its rows moved, is ignored (see meant()).
const SETTLE_MS = 1200, MOVED_MS = 350;
const DIALOG = (props) => ({ mod: "captain-scope", kind: "dialog", width: 460, props });

export class Explorer {
  constructor() {
    this.tree = null;
    this.model = null;
    this.actionsTop = [];
    this.live = false;
    this.menu = null;
    this.loading = false;
    this.rev = null;
  }

  start(cx) {
    this.load(cx);
    // A dialog that changed the tree bumps shared "captain.explorer.rev"; the services' tree is re-read now and then.
    cx.timer.every(1000, (cx) => {
      const rev = shared("captain.explorer.rev") ?? null;
      if (rev !== this.rev) { this.rev = rev; this.load(cx); }
    });
    cx.timer.every(15000, (cx) => this.load(cx));
  }

  load(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        const r = await loadExplorer();
        // Rows that move under the pointer take a click meant for something else, so note when the shape changed.
        const shape = JSON.stringify(r.tree, (k, v) => (k === "pipelines" ? undefined : v));
        if (shape !== this.shape) { this.shape = shape; this.movedAt = Date.now(); }
        this.firstAt ??= Date.now();
        this.tree = r.tree; this.model = r.model; this.live = r.live; this.actionsTop = r.actions ?? [];
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  // Folded nodes, by key. Everything starts open, so a small tree shows whole.
  isOpen(key) { return !(shared("captain.explorer.closed") ?? []).includes(key); }

  toggle(key) {
    const closed = new Set(shared("captain.explorer.closed") ?? []);
    if (closed.has(key)) closed.delete(key); else closed.add(key);
    setShared("captain.explorer.closed", [...closed]);
  }

  // Whether the engine page is the section on show (the sections stack's active child).
  onEnginePage() {
    const find = (n) => (n?.id === "sections" ? n : (n?.children ?? []).map(find).find(Boolean));
    return find(layout())?.active === "engine";
  }

  /** False for a click that can't be meant: in the first moments after the tree appears (the click that brings the
   *  window forward lands on whatever is under the pointer) or just after the rows moved. Settings never opens by
   *  itself at launch (Edgar saw Scope model open right after starting). */
  meant() {
    const now = Date.now();
    return Boolean(this.firstAt) && now - this.firstAt > SETTLE_MS && now - (this.movedAt ?? 0) > MOVED_MS;
  }

  select(n) {
    this.menu = null;
    if (!this.meant()) return;
    if (n.kind === "settings") { openOverlay({ mod: "captain-scope-model", kind: "dialog", width: 760, height: 640 }); return; }
    if (n.kind === "group") return;
    setShared("captain.scope", n.kind === "view" && n.id === "all-engines" ? null : { kind: n.kind, id: n.id, label: nodeName(n) });
    // An engine opens its page; anything else goes back to the views, filtered to it.
    if (n.kind === "engine") run("layout.select", { stack: "sections", child: "engine" });
    else if (n.kind === "view" && n.id === "all-experiments") run("layout.select", { stack: "sections", child: "runs" });
    else if (this.onEnginePage?.()) run("layout.select", { stack: "sections", child: "investigations" });
  }

  /** A node's menu. `holder` is the scope a group sits in ({id, type, name}), or null at the top level. */
  actions(n, holder) {
    const m = this.model;
    const add = (kind, under) => ({ label: words(m, kind).add, props: { action: "add", kind: kind === "engine" ? "engine" : "scope", type: kind, parent: under } });
    if (n.kind === "group") {
      if (n.type === "project") return [];
      return [add(n.type, holder)];
    }
    if (n.kind !== "scope" && n.kind !== "engine") return [];
    const me = { kind: n.kind, id: n.id, type: n.type, name: n.name };
    return [
      ...(n.kind === "scope" ? childKinds(m, n.type).map((k) => add(k, me)) : []),
      { label: "Rename", props: { action: "rename", node: me } },
      { label: "Move", props: { action: "move", node: me } },
      { label: "Archive", props: { action: "archive", node: me } },
    ];
  }

  menuList(c, id, acts, indent) {
    return v_flex().ml(indent).mr(8).mb(4).py(4).border_1().border_color(c.grey300).bg(c.surface)
      .children(acts.map((a, i) => div().id(`ex-act-${id}-${i}`).h(26).px(10).flex().items_center().text_size(12.5).cursor_pointer()
        .text_color(a.props.action === "archive" ? c.fail : c.navy).hover((s) => s.bg(c.surface2))
        .on_click((_e, cx) => { this.menu = null; cx.notify(); openOverlay(DIALOG(a.props)); })
        .child(a.label)));
  }

  row(c, n, depth, holder, path) {
    const key = nodeKey(n, path);
    const scope = currentScope();
    const on = scope ? scope.kind === n.kind && scope.id === n.id : n.kind === "view" && n.id === "all-engines";
    const kids = n.children ?? [];
    const open = kids.length > 0 && this.isOpen(key);
    const acts = this.actions(n, holder);
    const header = n.kind === "group";
    const line = h_flex().id(`ex-${key}`).h(26).flex_none().pr(6).gap(5).items_center().cursor_pointer()
      .pl(8 + depth * INDENT).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : header ? c.grey500 : c.navy)
      .text_size(header ? 11.5 : 12.5).when(header, (e) => e.font_medium())
      .hover((s) => (on ? s : s.bg(c.surface2)))
      .on_click((_e, cx) => { if (header) { if (kids.length) this.toggle(key); } else this.select(n); cx.notify(); })
      .on_mouse_down("right", (_e, cx) => { if (acts.length) { this.menu = this.menu === key ? null : key; cx.notify(); } })
      .child(div().id(`ex-fold-${key}`).size(12).flex_none().flex().items_center().justify_center()
        .when(kids.length > 0, (e) => e.text_color(c.grey500).on_click((_e, cx) => { cx.stop_propagation(); this.toggle(key); cx.notify(); })
          .child(svg(icon(open ? "chevron-down" : "chevron-right")).size(11))))
      .child(svg(icon(nodeIcon(n))).size(13).flex_none().text_color(on ? c.blue : c.grey500))
      .child(div().flex_1().min_w(0).truncate().child(nodeName(n)))
      .when(acts.length > 0, (e) => e.child(div().id(`ex-more-${key}`).px(4).text_size(13).text_color(c.grey500).tooltip("Actions")
        .hover((s) => s.text_color(c.navy)).on_click((_e, cx) => { cx.stop_propagation(); this.menu = this.menu === key ? null : key; cx.notify(); })
        .child("...")));
    const inner = n.kind === "scope" ? { id: n.id, type: n.type, name: n.name } : holder;
    return v_flex().w_full().child(line)
      .when(this.menu === key, (e) => e.child(this.menuList(c, key, acts, 20 + depth * INDENT)))
      .when(open, (e) => e.children(kids.map((ch, i) => this.row(c, ch, depth + 1, inner, `${key}/${i}`))));
  }

  render(c) {
    if (!this.tree) return v_flex().px(12).py(8).gap(6).children([0, 1, 2].map((i) => div().id(`ex-sk-${i}`).h(12).w(120 - i * 20).bg(c.surface2)));
    // The top level's "Add": the services' actions (Add brand, Add site, Add engine), or the model's when it's new.
    const top = (this.actionsTop.length ? this.actionsTop.map((a) => a.type) : childKinds(this.model, null))
      .map((k) => ({ label: words(this.model, k).add, props: { action: "add", kind: k === "engine" ? "engine" : "scope", type: k, parent: null } }));
    return v_flex().w_full()
      .child(h_flex().h(26).px(14).items_center()
        .child(div().flex_1().text_size(11).font_semibold().text_color(c.grey500).child("EXPLORER"))
        .child(div().id("ex-root-more").px(4).text_color(c.grey500).cursor_pointer().tooltip("Add")
          .hover((s) => s.text_color(c.navy)).on_click((_e, cx) => { this.menu = this.menu === "top" ? null : "top"; cx.notify(); })
          .child(svg(icon("plus")).size(12))))
      .when(this.menu === "top", (e) => e.child(this.menuList(c, "top", top, 8)))
      .children(this.tree.map((n, i) => this.row(c, n, 0, null, `t${i}`)));
  }
}
