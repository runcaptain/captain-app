// CAPTAIN: the Explorer, under the workspace selector (Edgar's spec, 2026-11-06). The tree is the services'
// (GET /api/explorer, kit/scopes.js): Brand > Site > Engine by default, Brand and Site optional, then All engines,
// All experiments, Projects, and Settings > Scope model. Selecting a node sets shared "captain.scope" = {kind, id,
// label}, which the five sections read to filter to it and below; the workspace, All engines and All experiments
// clear it. Each scope's menu holds its actions (Add brand, Add site, Add engine, Rename, Move, Archive), which
// open captain-scope.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { shared, setShared, openOverlay, run } from "herdr";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { loadExplorer, words, childKinds, nodeIcon, currentScope } from "./kit/scopes.js";

const SCOPED = new Set(["brand", "site", "engine", "project"]);
const INDENT = 12;

export class Explorer {
  constructor() {
    this.tree = null;
    this.model = null;
    this.live = false;
    this.menu = null;
    this.loading = false;
    this.rev = null;
  }

  start(cx, workspaceName) {
    this.workspaceName = workspaceName;
    this.load(cx);
    // A dialog that changed the tree bumps shared "captain.explorer.rev"; the services' tree is re-read now and then.
    cx.timer.every(1000, (cx) => {
      const rev = shared("captain.explorer.rev") ?? null;
      if (rev !== this.rev) { this.rev = rev; this.load(cx); }
    });
    cx.timer.every(15000, (cx) => this.load(cx));
  }

  load(cx, workspaceName) {
    if (workspaceName !== undefined) this.workspaceName = workspaceName;
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        const r = await loadExplorer(this.workspaceName);
        this.tree = r.tree; this.model = r.model; this.live = r.live;
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  isOpen(n, depth) {
    const o = shared("captain.explorer.open");
    if (!Array.isArray(o)) return depth < 6 && n.kind !== "settings";
    return o.includes(n.id);
  }

  toggle(n, depth) {
    const now = this.isOpen(n, depth);
    const set = Array.isArray(shared("captain.explorer.open")) ? new Set(shared("captain.explorer.open")) : this.allOpen();
    if (now) set.delete(n.id); else set.add(n.id);
    setShared("captain.explorer.open", [...set]);
  }

  // Everything open by default turns into an explicit list the first time someone folds a node.
  allOpen() {
    const out = new Set();
    const walk = (n, d) => { if (d < 6 && n.kind !== "settings") out.add(n.id); for (const ch of n.children ?? []) walk(ch, d + 1); };
    if (this.tree) walk(this.tree, 0);
    return out;
  }

  select(n) {
    this.menu = null;
    if (n.kind === "scope-model") {
      openOverlay({ mod: "captain-scope-model", kind: "dialog", width: 760, height: 640 });
      return;
    }
    if (n.kind === "settings" || n.kind === "group") return;
    if (SCOPED.has(n.kind)) setShared("captain.scope", { kind: n.kind, id: n.id, label: n.label });
    else setShared("captain.scope", null);
    if (n.kind === "all-experiments") run("layout.select", { stack: "sections", child: "runs" });
  }

  /** The actions in a node's menu. `parent` is the scope a group belongs to ("workspace" at the root). */
  actions(n, parent) {
    const m = this.model;
    const add = (kind, under) => ({ label: words(m, kind).add, props: { action: "add", kind, parent: under } });
    if (n.kind === "workspace") return childKinds(m, "workspace").map((k) => add(k, { id: n.id, kind: "workspace", label: n.label }));
    if (n.kind === "group" && n.of && n.of !== "project") return [add(n.of, parent)];
    if (n.kind === "group" && n.of === "project") return [{ label: "Add project", props: { action: "add", kind: "project", parent: { id: "ws", kind: "workspace" } } }];
    if (!SCOPED.has(n.kind)) return [];
    const me = { id: n.id, kind: n.kind, label: n.label };
    return [
      ...(n.kind === "engine" || n.kind === "project" ? [] : childKinds(m, n.kind).map((k) => add(k, me))),
      { label: "Rename", props: { action: "rename", node: me } },
      ...(n.kind === "project" ? [] : [{ label: "Move", props: { action: "move", node: me } }]),
      { label: "Archive", props: { action: "archive", node: me } },
    ];
  }

  rowIcon(c, n, on) {
    if (n.kind === "engine") return n.system ? Brand(c, n.system, 13) : svg(icon("search")).size(13).flex_none().text_color(on ? c.blue : c.grey500);
    return svg(icon(nodeIcon(n))).size(13).flex_none().text_color(on ? c.blue : c.grey500);
  }

  row(c, n, depth, parent) {
    const scope = currentScope();
    const on = scope ? scope.id === n.id : n.kind === "all-engines";
    const kids = n.children ?? [];
    const open = kids.length > 0 && this.isOpen(n, depth);
    const acts = this.actions(n, parent);
    const group = n.kind === "group" || n.kind === "settings";
    const line = h_flex().id(`ex-${n.id}`).h(26).flex_none().pr(6).gap(5).items_center().cursor_pointer()
      .pl(8 + depth * INDENT).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : group ? c.grey500 : c.navy)
      .text_size(group ? 11.5 : 12.5).when(group, (e) => e.font_medium())
      .hover((s) => (on ? s : s.bg(c.surface2)))
      .on_click((_e, cx) => { if (group && kids.length) this.toggle(n, depth); else this.select(n); cx.notify(); })
      .on_mouse_down("right", (_e, cx) => { if (acts.length) { this.menu = this.menu === n.id ? null : n.id; cx.notify(); } })
      .child(div().id(`ex-fold-${n.id}`).size(12).flex_none().flex().items_center().justify_center()
        .when(kids.length > 0, (e) => e.text_color(c.grey500).on_click((_e, cx) => { cx.stop_propagation(); this.toggle(n, depth); cx.notify(); })
          .child(svg(icon(open ? "chevron-down" : "chevron-right")).size(11))))
      .child(this.rowIcon(c, n, on))
      .child(div().flex_1().min_w(0).truncate().child(n.label))
      .when(acts.length > 0, (e) => e.child(div().id(`ex-more-${n.id}`).px(4).text_size(13).text_color(c.grey500).tooltip("Actions")
        .hover((s) => s.text_color(c.navy)).on_click((_e, cx) => { cx.stop_propagation(); this.menu = this.menu === n.id ? null : n.id; cx.notify(); })
        .child("...")));
    const menu = this.menu === n.id ? v_flex().ml(20 + depth * INDENT).mr(8).mb(4).py(4).border_1().border_color(c.grey300).bg(c.surface)
      .children(acts.map((a, i) => div().id(`ex-act-${n.id}-${i}`).h(26).px(10).flex().items_center().text_size(12.5).cursor_pointer()
        .text_color(a.props.action === "archive" ? c.fail : c.navy).hover((s) => s.bg(c.surface2))
        .on_click((_e, cx) => { this.menu = null; cx.notify(); openOverlay({ mod: "captain-scope", kind: "dialog", width: 460, props: a.props }); })
        .child(a.label))) : null;
    const me = SCOPED.has(n.kind) || n.kind === "workspace" ? { id: n.id, kind: n.kind, label: n.label } : parent;
    return v_flex().w_full().child(line).when(Boolean(menu), (e) => e.child(menu))
      .when(open, (e) => e.children(kids.map((ch) => this.row(c, ch, depth + 1, me))));
  }

  render(c) {
    if (!this.tree) return v_flex().px(12).py(8).gap(6).children([0, 1, 2].map((i) => div().id(`ex-sk-${i}`).h(12).w(120 - i * 20).bg(c.surface2)));
    // The workspace itself is the selector above: the tree starts with its children.
    const root = this.tree;
    const ws = { id: root.id, kind: "workspace", label: root.label };
    return v_flex().w_full()
      .child(h_flex().h(26).px(14).items_center()
        .child(div().flex_1().text_size(11).font_semibold().text_color(c.grey500).child("EXPLORER"))
        .when(this.actions(root).length > 0, (e) => e.child(div().id("ex-root-more").px(4).text_size(13).text_color(c.grey500).cursor_pointer()
          .tooltip("Add").hover((s) => s.text_color(c.navy)).on_click((_e, cx) => { this.menu = this.menu === root.id ? null : root.id; cx.notify(); })
          .child(svg(icon("plus")).size(12)))))
      .when(this.menu === root.id, (e) => e.child(v_flex().mx(8).mb(4).py(4).border_1().border_color(c.grey300).bg(c.surface)
        .children(this.actions(root).map((a, i) => div().id(`ex-act-root-${i}`).h(26).px(10).flex().items_center().text_size(12.5).cursor_pointer()
          .text_color(c.navy).hover((s) => s.bg(c.surface2))
          .on_click((_e, cx) => { this.menu = null; cx.notify(); openOverlay({ mod: "captain-scope", kind: "dialog", width: 460, props: a.props }); })
          .child(a.label)))))
      .children((root.children ?? []).map((ch) => this.row(c, ch, 0, ws)));
  }
}
