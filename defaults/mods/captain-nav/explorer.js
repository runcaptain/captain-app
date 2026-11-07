// CAPTAIN: the Explorer, under the workspace selector (Edgar's spec, 2026-11-06). The tree is the services'
// (GET /api/explorer, kit/scopes.js): the scope groups (Brand > Site > Engine by default, Brand and Site optional),
// unscoped engines, Projects, and Settings > Scope model, with All engines and All experiments as real lists, like
// folders (Edgar: "All Experiments and All Engines aren't lists"): the engines (GET /api/engines) and the experiments
// newest first (GET /api/backtests: latest run, else creation), each following the Explorer's pick. Selecting a node sets
// shared "captain.scope" = {kind: scope | engine | project | view, id, label}, which the five sections read to
// filter to it and below; "All engines" is the whole workspace. Each scope's and engine's menu holds its actions
// (Add brand, Add site, Add engine, Rename, Move, Archive), which open captain-scope.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { shared, setShared, openOverlay, run, layout } from "herdr";
import { icon } from "./kit/icons.js";
import { api, workspace } from "./kit/cst.js";
import { loadExplorer, words, childKinds, nodeIcon, nodeName, nodeKey, currentScope } from "./kit/scopes.js";

const INDENT = 12;
// A click this soon after the tree appears, or after its rows moved, is ignored (see meant()).
const SETTLE_MS = 1200, MOVED_MS = 350;
// What the two workspace-wide lists hold, as hover text (Edgar: "not sure the differences between them").
const ABOUT = {
  "all-engines": "The search and recommendation systems you're improving, across every brand",
  "all-experiments": "Every comparison you've run, newest first",
};
const DIALOG = (props) => ({ mod: "captain-scope", kind: "dialog", width: 460, props });
const ADD_ENGINE = DIALOG({ action: "add", kind: "engine", type: "engine", parent: null });
// The experiments list shows this many, then "Show all N" (which opens Runs).
const FIRST = 20;
// Folded until opened: a long list shouldn't push the tree down on first look.
const START_CLOSED = new Set(["all-experiments"]);

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
    // The experiments follow the Explorer's pick (X-CST-Scope): re-read them when it changes.
    let pick = JSON.stringify(shared("captain.scope") ?? null);
    cx.timer.every(400, (cx) => { const p = JSON.stringify(shared("captain.scope") ?? null); if (p !== pick) { pick = p; this.load(cx); } });
  }

  load(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        const sample = Boolean(shared("captain.explorer.sample"));
        const [r, en, bt] = await Promise.all([loadExplorer(),
          sample ? null : api("/api/engines", { scoped: false }), sample ? null : api("/api/backtests")]);
        this.engines = en ? (en.ok ? (en.data ?? []).filter((e) => !e.archived) : this.engines ?? null) : null;
        // Already newest first (the services sort by latest run, else creation).
        this.experiments = bt ? (bt.ok ? bt.data ?? [] : this.experiments ?? null) : [];
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

  // What's folded, per workspace (shared "captain.explorer.folds" = {workspace: {key: open}}). Everything starts open
  // but the experiments list.
  folds() { return (shared("captain.explorer.folds") ?? {})[workspace() ?? shared("captain.workspace") ?? ""] ?? {}; }

  isOpen(key) { const f = this.folds(); return key in f ? Boolean(f[key]) : !START_CLOSED.has(key); }

  toggle(key) {
    const ws = workspace() ?? shared("captain.workspace") ?? "";
    const all = { ...(shared("captain.explorer.folds") ?? {}) };
    all[ws] = { ...(all[ws] ?? {}), [key]: !this.isOpen(key) };
    setShared("captain.explorer.folds", all);
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
    if (n.kind === "group" || n.kind === "list") return;
    // A hint row does what it says ("No engines yet: add one"); "Show all" opens Runs.
    if (n.kind === "hint") { if (n.overlay) openOverlay(n.overlay); return; }
    if (n.kind === "more") { run("layout.select", { stack: "sections", child: "runs" }); return; }
    // An experiment opens in Comparisons, at its latest run.
    if (n.kind === "experiment") {
      setShared("captain.open", { section: "comparisons", backtest: n.name, version: n.latest ?? 1, at: Date.now() });
      run("layout.select", { stack: "sections", child: "comparisons" });
      return;
    }
    setShared("captain.scope", { kind: n.kind, id: n.id, label: nodeName(n) });
    // An engine opens its page; anything else goes back to the views, filtered to it.
    if (n.kind === "engine") run("layout.select", { stack: "sections", child: "engine" });
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

  /** How many engines the workspace has (one engine can show under several scopes: counted once). */
  engineCount() {
    if (this.engines) return this.engines.length;
    const ids = new Set();
    const walk = (n) => { if (n.kind === "engine") ids.add(n.id); (n.children ?? []).forEach(walk); };
    (this.tree ?? []).forEach(walk);
    return ids.size;
  }

  /** The tree as shown: All engines and All experiments become lists of the real things, and an empty list says so. */
  shown() {
    const engines = this.engines ?? (() => { const out = new Map(); const walk = (n) => { if (n.kind === "engine") out.set(n.id, n); (n.children ?? []).forEach(walk); }; (this.tree ?? []).forEach(walk); return [...out.values()]; })();
    const exps = this.experiments ?? [];
    return (this.tree ?? []).map((n) => {
      if (n.kind === "view" && n.id === "all-engines") {
        return { kind: "list", id: n.id, key: n.id, label: n.label, about: ABOUT[n.id], count: engines.length, children: engines.length
          ? engines.map((e) => ({ kind: "engine", id: e.id, name: e.name, type: e.type, key: `all-engines/${e.id}` }))
          : [{ kind: "hint", key: "all-engines/none", label: "No engines yet: add one", overlay: ADD_ENGINE }] };
      }
      if (n.kind === "view" && n.id === "all-experiments") {
        const first = exps.slice(0, FIRST).map((b) => ({ kind: "experiment", key: `all-experiments/${b.name}`, name: b.name, latest: b.latest, lastRun: b.last_run, lastComplete: b.last_run_complete, runs: b.runs }));
        return { kind: "list", id: n.id, key: n.id, label: n.label, about: ABOUT[n.id], count: exps.length, children: exps.length
          ? [...first, ...(exps.length > FIRST ? [{ kind: "more", key: "all-experiments/more", label: `Show all ${exps.length}` }] : [])]
          : [{ kind: "hint", key: "all-experiments/none", label: "No experiments yet: run one", overlay: { mod: "captain-forms", kind: "dialog", width: 560, props: { task: "backtest" } } }] };
      }
      if (n.kind === "group" && n.type === "project" && !(n.children ?? []).length) {
        return { ...n, children: [{ kind: "hint", key: "projects/none", label: "No projects yet" }] };
      }
      return n;
    });
  }

  /** An experiment's marker: its latest run finished, didn't finish, or it hasn't run yet (the date on hover). */
  marker(c, n) {
    const day = String(n.lastRun ?? "").slice(0, 10);
    const [color, words] = !n.lastRun ? [c.grey300, "Not run yet"]
      : n.lastComplete === false ? [c.warn ?? "#d97706", `Its latest run, ${day}, didn't finish`] : [c.good, `Latest run ${day}, finished`];
    return div().id(`ex-mark-${n.key}`).size(7).flex_none().rounded_full().bg(color).tooltip(words);
  }

  row(c, n, depth, holder, path) {
    const key = nodeKey(n, path);
    const scope = currentScope();
    const on = Boolean(scope) && scope.kind === n.kind && scope.id === n.id;
    const kids = n.children ?? [];
    const open = kids.length > 0 && this.isOpen(key);
    const acts = this.actions(n, holder);
    const header = n.kind === "group";
    const folder = header || n.kind === "list";
    const hint = n.kind === "hint" || n.kind === "more";
    const count = n.kind === "list" ? n.count : 0;
    const line = h_flex().id(`ex-${key}`).h(26).flex_none().pr(6).gap(5).items_center().cursor_pointer()
      .when(Boolean(n.about), (e) => e.tooltip(n.about))
      .pl(8 + depth * INDENT).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : header || hint ? c.grey500 : c.navy)
      .text_size(header || hint ? 11.5 : 12.5).when(header || n.kind === "list", (e) => e.font_medium())
      .hover((s) => (on ? s : s.bg(c.surface2)))
      .on_click((_e, cx) => { if (folder) { if (kids.length) this.toggle(key); } else this.select(n); cx.notify(); })
      .on_mouse_down("right", (_e, cx) => { if (acts.length) { this.menu = this.menu === key ? null : key; cx.notify(); } })
      .child(div().id(`ex-fold-${key}`).size(12).flex_none().flex().items_center().justify_center()
        .when(kids.length > 0, (e) => e.text_color(c.grey500).on_click((_e, cx) => { cx.stop_propagation(); this.toggle(key); cx.notify(); })
          .child(svg(icon(open ? "chevron-down" : "chevron-right")).size(11))))
      .child(n.kind === "experiment" ? div().size(13).flex_none().flex().items_center().justify_center().child(this.marker(c, n))
        : hint ? div().size(13).flex_none()
        : svg(icon(n.kind === "list" ? (n.id === "all-experiments" ? "flask-conical" : "boxes") : nodeIcon(n))).size(13).flex_none().text_color(on ? c.blue : c.grey500))
      .child(div().flex_1().min_w(0).truncate().child(nodeName(n)))
      .when(count > 0, (e) => e.child(div().min_w(20).px(6).text_size(10.5).line_height("18px").font_family("JetBrains Mono")
        .bg(c.surface2).text_color(c.grey900).flex().justify_center().child(String(count))))
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
      .children(this.shown().map((n, i) => this.row(c, n, 0, null, `t${i}`)));
  }
}
