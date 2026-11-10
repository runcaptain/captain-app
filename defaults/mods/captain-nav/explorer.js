// CAPTAIN: the Explorer, under the workspace selector (Edgar's spec, 2026-11-06). The tree is the services'
// (GET /api/explorer, kit/scopes.js): the scope groups (Brand > Site > Engine by default, Brand and Site optional),
// unscoped engines and Projects (the levels themselves are set in Settings, Project Structure), with All engines and All experiments as real lists, like
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
import { MlflowList } from "./kit/mlflow.js";
import { Brand } from "./kit/brands.js";
import { ProjectIcon } from "./kit/projecticon.js";

const INDENT = 12;
// A click this soon after the tree appears, or after its rows moved, is ignored (see meant()).
const SETTLE_MS = 1200, MOVED_MS = 350;
// What the two workspace-wide lists hold, as hover text (Edgar: "not sure the differences between them").
const ABOUT = {
  "all-engines": "The search and recommendation systems you're improving, across every brand",
  "all-experiments": "Every offline evaluation you've run, newest first",
};
const DIALOG = (props) => ({ mod: "captain-scope", kind: "dialog", width: 460, props });
const ADD_ENGINE = DIALOG({ action: "add", kind: "engine", type: "engine", parent: null });
// The experiments list shows this many, then "Show all N" (which opens Runs).
const FIRST = 20;
// Folded until opened: a long list shouldn't push the tree down on first look.
const START_CLOSED = new Set(["all-experiments", "mlflow"]);

export class Explorer {
  constructor() {
    this.tree = null;
    this.model = null;
    this.actionsTop = [];
    this.live = false;
    this.menu = null;
    this.loading = false;
    this.rev = null;
    // MLflow, read-only (kit/mlflow.js): read the first time its heading opens, so a workspace without it pays nothing.
    this.mlflow = new MlflowList({ id: "ex-ml", header: false, onSetup: () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "mlflow" } }) });
    this.mlflowLoaded = false;
  }

  start(cx) {
    this.load(cx);
    // Left open last time: read it as soon as its fold reads open. The workspace (and so the remembered fold) may
    // not be known yet at start, so look again each second until it has been read once.
    if (this.isOpen("mlflow")) this.openMlflow(cx);
    cx.timer.every(1000, (cx) => { if (!this.mlflowLoaded && this.isOpen("mlflow")) { this.openMlflow(cx); cx.notify(); } });
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

  /** The MLflow heading: opens and closes like the lists above it (remembered per workspace), through the same click
   *  guard, and reads MLflow the first time it opens. */
  toggleMlflow(cx) {
    if (!this.meant()) return;
    this.toggle("mlflow");
    if (this.isOpen("mlflow")) this.openMlflow(cx);
    cx.notify();
  }

  openMlflow(cx) {
    if (this.mlflowLoaded) return;
    this.mlflowLoaded = true;
    this.mlflow.load(cx);
  }

  mlflowRow(c) {
    const open = this.isOpen("mlflow");
    return v_flex().w_full()
      .child(h_flex().id("ex-mlflow").h(26).flex_none().pl(8).pr(6).gap(5).items_center().cursor_pointer()
        .bg(c.surface).text_color(c.navy).text_size(12.5).font_medium().hover((s) => s.bg(c.surface2))
        .tooltip("MLflow experiments, runs and registered models, read-only")
        .on_click((_e, cx) => this.toggleMlflow(cx))
        .child(div().size(12).flex_none().flex().items_center().justify_center().text_color(c.grey500).child(svg(icon(open ? "chevron-down" : "chevron-right")).size(11)))
        .child(Brand(c, "mlflow", 13))
        .child(div().flex_1().min_w(0).truncate().child("MLflow")))
      .when(open, (e) => e.child(div().pl(10).child(this.mlflow.render(c, 220))));
  }

  // Whether an engine page or a project home is the section on show (the sections stack's active child).
  onEnginePage() {
    const find = (n) => (n?.id === "sections" ? n : (n?.children ?? []).map(find).find(Boolean));
    return ["engine", "project"].includes(find(layout())?.active);
  }

  /** False for a click that can't be meant: in the first moments after the tree appears (the click that brings the
   *  window forward lands on whatever is under the pointer) or just after the rows moved. Nothing opens by itself at
   *  launch. */
  meant() {
    const now = Date.now();
    return Boolean(this.firstAt) && now - this.firstAt > SETTLE_MS && now - (this.movedAt ?? 0) > MOVED_MS;
  }

  select(n) {
    this.menu = null;
    if (!this.meant()) return;
    if (n.kind === "group" || n.kind === "list") return;
    // A hint row does what it says ("No engines yet: add one"); "Show all" opens Runs.
    if (n.kind === "hint") { if (n.overlay) openOverlay(n.overlay); return; }
    // All: no project or scope picked, so every section shows the whole workspace. A project home goes back to a view.
    if (n.kind === "all") {
      setShared("captain.scope", null);
      if (this.onEnginePage?.()) run("layout.select", { stack: "sections", child: "data" });
      return;
    }
    if (n.kind === "more") { run("layout.select", { stack: "sections", child: "runs" }); return; }
    // An experiment opens in Comparisons, at its latest run.
    if (n.kind === "experiment") {
      setShared("captain.open", { section: "comparisons", backtest: n.name, version: n.latest ?? 1, at: Date.now() });
      run("layout.select", { stack: "sections", child: "comparisons" });
      return;
    }
    setShared("captain.scope", { kind: n.kind, id: n.id, label: nodeName(n) });
    // An engine opens its page; anything else goes back to the views, filtered to it.
    // An engine opens its page, a project its home; anything else goes back to the views, filtered to it.
    if (n.kind === "engine") run("layout.select", { stack: "sections", child: "engine" });
    else if (n.kind === "project") run("layout.select", { stack: "sections", child: "project" });
    else if (this.onEnginePage?.()) run("layout.select", { stack: "sections", child: "investigations" });
  }

  /** A node's menu. `holder` is the scope a group sits in ({id, type, name}), or null at the top level. */
  actions(n, holder) {
    const m = this.model;
    const add = (kind, under) => ({ label: words(m, kind).add, props: { action: "add", kind: kind === "engine" ? "engine" : "scope", type: kind, parent: under } });
    if (n.kind === "group") {
      if (n.type === "project") return [{ label: "New project", overlay: { mod: "captain-forms", kind: "dialog", width: 560, props: { task: "project" } } }];
      return [add(n.type, holder)];
    }
    if (n.kind === "project") {
      const name = n.name ?? n.label ?? n.id;
      return [{ label: "Change icon", overlay: { mod: "captain-project-icon", kind: "dialog", width: 560, height: 560, props: { project: { id: n.id, name, icon: n.icon ?? null } } } },
        { label: "Delete project", danger: true, overlay: { mod: "captain-forms", kind: "dialog", width: 560, props: { task: "delete-project", values: { project: n.id, name } } } }];
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
        .text_color(a.props?.action === "archive" || a.danger ? c.fail : c.navy).hover((s) => s.bg(c.surface2))
        .on_click((_e, cx) => { this.menu = null; cx.notify(); openOverlay(a.overlay ?? DIALOG(a.props)); })
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

  /** The tree as shown (Edgar, 2026-11-07: "experience everything through projects"): Projects first, each opening
   *  its home; no All engines or All experiments views; and no empty level groups, so a workspace whose scope model is
   *  still empty shows its projects and nothing bare. */
  shown() {
    const tree = (this.tree ?? []).filter((n) => !(n.kind === "view" && (n.id === "all-engines" || n.id === "all-experiments")))
      // Project Structure lives in Settings now (Edgar, 2026-11-09), so a "Scope model" row from older services is dropped.
      .filter((n) => n.kind !== "settings" && n.id !== "scope-model")
      .filter((n) => !(n.kind === "group" && n.type !== "project" && !(n.children ?? []).length))
      .map((n) => (n.kind === "group" && n.type === "project" && !(n.children ?? []).length
        ? { ...n, children: [{ kind: "hint", key: "projects/none", label: "No projects yet: add one", overlay: { mod: "captain-forms", kind: "dialog", width: 560, props: { task: "project" } } }] }
        // All, first under Projects (Edgar, 2026-11-07): back to the whole workspace, no project picked.
        : n.kind === "group" && n.type === "project"
          ? { ...n, children: [{ kind: "all", key: "projects/all", label: "All", about: "Everything in this workspace, not only one project's" }, ...n.children] }
          : n));
    const projects = tree.filter((n) => n.kind === "group" && n.type === "project");
    return [...projects, ...tree.filter((n) => !projects.includes(n))];
  }


  /** An experiment's marker: its latest run is fully graded, has too few grades to trust nDCG, or it hasn't run yet.
   *  (last_run_graded: judged@10 at least 0.9.) */
  marker(c, n) {
    const day = String(n.lastRun ?? "").slice(0, 10);
    const [color, words] = !n.lastRun ? [c.grey300, "Not run yet"]
      : n.lastGraded === false ? [c.warn, `Not enough grades (latest run ${day}). Fewer than 90% of the top results are graded, so nDCG reads low. Grade more, or import labels.`]
      : [c.good, `Latest run ${day}, fully graded`];
    return div().id(`ex-mark-${n.key}`).size(7).flex_none().rounded_full().bg(color).tooltip(words);
  }

  row(c, n, depth, holder, path) {
    const key = nodeKey(n, path);
    const scope = currentScope();
    const on = n.kind === "all" ? !scope : Boolean(scope) && scope.kind === n.kind && scope.id === n.id;
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
        : n.kind === "project" && n.icon ? ProjectIcon(c, n, 13)
        : svg(icon(n.kind === "all" ? "boxes" : n.kind === "list" ? (n.id === "all-experiments" ? "flask-conical" : "boxes") : nodeIcon(n))).size(13).flex_none().text_color(on ? c.blue : c.grey500))
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
      // MLflow goes after Projects.
      .children(this.shown().flatMap((n, i) => {
        const r = this.row(c, n, 0, null, `t${i}`);
        return n.kind === "group" && n.type === "project" ? [r, this.mlflowRow(c)] : [r];
      }))
      .when(!this.shown().some((n) => n.kind === "group" && n.type === "project"), (e) => e.child(this.mlflowRow(c)));
  }
}

/** A project in the Explorer's tree, by id, for its name and icon elsewhere (the sidebar's "in <project>"). */
export function projectNode(tree, id) {
  let hit = null;
  const walk = (n) => { if (!hit && n.kind === "project" && n.id === id) hit = n; (n.children ?? []).forEach(walk); };
  (tree ?? []).forEach(walk);
  return hit;
}
