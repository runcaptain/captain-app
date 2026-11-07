// CAPTAIN: an engine's page (Edgar's spec, 2026-11-06), opened by picking an engine in the Explorer. Its tabs read
// the services' lists filtered to it (the kit sends X-CST-Scope "engine:<id>", backend 4d6a983d):
//   Overview        the engine: what it serves, where it belongs, its pipelines, and a count for every tab
//   Investigations  /api/investigations          Experiments  /api/backtests
//   Runs            each experiment's runs        Evaluations  grading queues over the engine's pipelines
//   Datasets        /api/datasets                 Signals      /api/signals (the whole workspace: not per engine yet)
//   Versions        its pipelines' versions (/api/pipelines) and models (/api/models)
// A row opens the record in its section (shared "captain.open"). Rename, Move and Archive open captain-scope.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, shared, setShared, run, openOverlay } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { api, watchScope, watchWorkspace } from "./kit/cst.js";
import { Button, Badge, Tabs, Empty, Skeleton, Problem } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { nodeName } from "./kit/scopes.js";

const TABS = [["overview", "Overview"], ["investigations", "Investigations"], ["experiments", "Experiments"], ["runs", "Runs"],
  ["evaluations", "Evaluations"], ["datasets", "Datasets"], ["signals", "Signals"], ["versions", "Versions"]];
const SERVES = { search: "Search", recommendation: "Recommendations" };
const list = (r) => (r?.ok ? (Array.isArray(r.data) ? r.data : r.data?.items ?? []) : null);
const base = (ref) => String(ref ?? "").split("@")[0];
const when = (iso) => (iso ? String(iso).slice(0, 10) : "");

export default class Engine extends View {
  init(_props, cx) {
    this.tab = "overview";
    this.engine = null; this.data = {}; this.where = [];
    this.load(cx);
    watchScope(cx, (cx) => { this.tab = "overview"; this.load(cx); });
    watchWorkspace(cx, (cx) => this.load(cx));
    // A rename or move from the dialog bumps shared "captain.explorer.rev".
    let rev = shared("captain.explorer.rev") ?? null;
    cx.timer.every(1000, (cx) => { const r = shared("captain.explorer.rev") ?? null; if (r !== rev) { rev = r; this.load(cx); } });
  }

  pick() { const s = shared("captain.scope"); return s?.kind === "engine" ? s : null; }

  load(cx) {
    const pick = this.pick();
    this.data = {};
    if (!pick) { this.engine = null; this.loadEngines(cx); cx.notify(); return; }
    const seq = (this.seq = (this.seq ?? 0) + 1);
    cx.spawn(async (cx) => {
      // The engine itself and where it belongs, unfiltered; its records, filtered to it.
      const [engines, tree, inv, bt, ds, pl, md, sg, gr] = await Promise.all([
        api("/api/engines", { scoped: false }), api("/api/explorer", { scoped: false }),
        api("/api/investigations"), api("/api/backtests"), api("/api/datasets"), api("/api/pipelines"), api("/api/models"),
        api("/api/signals"), api("/api/grading"),
      ]);
      if (seq !== this.seq) return;
      this.engine = (list(engines) ?? []).find((e) => e.id === pick.id) ?? { id: pick.id, name: pick.label, missing: true };
      this.where = this.scopesOf(tree.ok ? tree.data?.tree ?? [] : [], this.engine.scopes ?? []);
      const mine = new Set((this.engine.pipelines ?? []).map(base));
      const grading = list(gr);
      this.data = {
        investigations: inv, experiments: bt, datasets: ds, versions: pl, models: md, signals: sg,
        evaluations: grading === null ? gr : { ok: true, data: grading.filter((g) => (g.pipelines ?? []).some((p) => mine.has(base(p)))) },
      };
      cx.notify();
    });
    cx.notify();
  }

  loadEngines(cx) {
    cx.spawn(async (cx) => { const r = await api("/api/engines", { scoped: false }); this.engines = list(r) ?? []; cx.notify(); });
  }

  /** The names of the scopes an engine belongs to, with their path ("Acme Outdoors / US storefront"). */
  scopesOf(tree, ids) {
    const out = [];
    const walk = (n, path) => {
      const next = n.kind === "scope" ? [...path, n.name] : path;
      if (n.kind === "scope" && ids.includes(n.id)) out.push(next.join(" / "));
      for (const ch of n.children ?? []) walk(ch, next);
    };
    for (const n of tree) walk(n, []);
    return out;
  }

  open(section, extra) {
    setShared("captain.open", { section, ...extra, at: Date.now() });
    run("layout.select", { stack: "sections", child: section });
  }

  count(key) {
    if (key === "runs") { const b = list(this.data.experiments); return b ? b.reduce((n, x) => n + (x.runs ?? 0), 0) : null; }
    if (key === "versions") { const p = list(this.data.versions), m = list(this.data.models); return p && m ? p.length + m.length : p?.length ?? null; }
    const l = list(this.data[key]);
    return l ? l.length : null;
  }

  // One row: a title, a quiet line under it, chips on the right; clicking opens the record.
  row(c, id, title, sub, chips, onClick) {
    return h_flex().id(id).min_h(44).px(14).py(6).gap(10).items_center().border_b(1).border_color(c.rule)
      .when(Boolean(onClick), (e) => e.cursor_pointer().hover((s) => s.bg(c.surface2)).on_click(onClick))
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(div().truncate().text_size(13).font_medium().text_color(c.navy).child(title))
        .when(Boolean(sub), (e) => e.child(div().truncate().text_size(12).text_color(c.grey500).child(sub))))
      .children((chips ?? []).filter(Boolean).map((ch) => Badge(c, ch, "neutral")));
  }

  listTab(c, key, empty, draw, note) {
    const r = this.data[key];
    if (!r) return Skeleton(c, 5);
    if (!r.ok) return Problem(c, r, (cx) => this.load(cx));
    const items = list(r);
    return v_flex().w_full()
      .when(Boolean(note), (e) => e.child(div().px(14).py(8).text_size(12).text_color(c.grey500).child(note)))
      .child(items.length ? v_flex().border_t(1).border_color(c.rule).children(items.map((x, i) => draw(x, i))) : Empty(c, empty.title, empty.body));
  }

  overview(c) {
    const e = this.engine;
    const card = (key, label) => {
      const n = this.count(key);
      return v_flex().id(`eng-card-${key}`).flex_1().min_w(110).p(12).gap(4).border_1().border_color(c.rule).cursor_pointer()
        .hover((s) => s.border_color(c.blue)).on_click((_e, cx) => { this.tab = key; cx.notify(); })
        .child(div().text_size(20).font_semibold().text_color(c.navy).child(n === null ? "..." : String(n)))
        .child(div().text_size(12).text_color(c.grey500).child(label));
    };
    return v_flex().gap(16).p(16)
      .when(Boolean(e.description), (x) => x.child(div().text_size(13).line_height("20px").text_color(c.grey900).child(e.description)))
      .child(h_flex().flex_wrap().gap(10).children([["investigations", "Investigations"], ["experiments", "Experiments"], ["runs", "Runs"],
        ["evaluations", "Evaluations"], ["datasets", "Datasets"], ["versions", "Versions"]].map(([k, l]) => card(k, l))))
      .child(v_flex().gap(6)
        .child(div().text_size(12.5).font_semibold().text_color(c.navy).child("Pipelines"))
        .child((e.pipelines ?? []).length
          ? h_flex().flex_wrap().gap(6).children(e.pipelines.map((p) => Badge(c, p, "neutral")))
          : div().text_size(12.5).text_color(c.grey500).child("No pipelines belong to this engine yet. A pipeline belongs to the engine that lists it.")));
  }

  body(c) {
    const t = this.tab;
    if (t === "overview") return this.overview(c);
    if (t === "investigations") return this.listTab(c, "investigations", { title: "No investigations here yet", body: "Investigations linked to this engine show here." },
      (x, i) => this.row(c, `eng-inv-${i}`, x.title ?? x.id, x.question ?? x.hypothesis ?? "", [x.status], () => this.open("investigations", { investigation: x.id })));
    if (t === "experiments") return this.listTab(c, "experiments", { title: "No experiments here yet", body: "Backtests comparing this engine's pipelines show here." },
      (x, i) => this.row(c, `eng-exp-${i}`, x.name, (x.arms ?? []).join(" vs "), [x.primary_metric, `${x.runs ?? 0} runs`], () => this.open("comparisons", { backtest: x.name, version: x.latest })));
    if (t === "runs") return this.listTab(c, "experiments", { title: "No runs here yet", body: "Each experiment's runs show here." },
      (x, i) => this.row(c, `eng-run-${i}`, `${x.name}, run ${x.latest ?? 1}`, `${x.runs ?? 0} run${x.runs === 1 ? "" : "s"} in all`, [x.primary_metric], () => this.open("runs", { backtest: x.name })),
      "The latest run of each experiment. Runs has every run, with logs and manifests.");
    if (t === "evaluations") return this.listTab(c, "evaluations", { title: "No evaluations here yet", body: "Grading over this engine's pipelines shows here." },
      (x, i) => this.row(c, `eng-eval-${i}`, x.name, `${x.graded ?? 0} of ${x.pairs ?? 0} graded, depth ${x.depth ?? "?"}`, [x.queries], () => this.open("runs", { grading: x.name })),
      "Grading queues that include one of this engine's pipelines.");
    if (t === "datasets") return this.listTab(c, "datasets", { title: "No datasets here yet", body: "Datasets linked to this engine show here." },
      (x, i) => this.row(c, `eng-ds-${i}`, x.name, x.description ?? "", [x.kind, x.rows != null ? `${Number(x.rows).toLocaleString("en-US")} rows` : null, `v${x.latest ?? 1}`], () => this.open("data", { dataset: x.name })));
    if (t === "signals") return this.listTab(c, "signals", { title: "No signals yet", body: "Signals you rank with show here." },
      (x, i) => this.row(c, `eng-sig-${i}`, x.name ?? x.id, x.description ?? "", [x.status]),
      "Signals aren't linked to engines yet, so this is every signal in the workspace.");
    // Versions: its pipelines' versions, then its models.
    const p = this.data.versions, m = this.data.models;
    if (!p || !m) return Skeleton(c, 5);
    if (!p.ok) return Problem(c, p, (cx) => this.load(cx));
    const pipes = list(p), models = list(m) ?? [];
    return v_flex().w_full()
      .child(div().px(14).py(8).text_size(12).font_semibold().text_color(c.grey500).child("PIPELINES"))
      .child(pipes.length ? v_flex().border_t(1).border_color(c.rule).children(pipes.map((x, i) => this.row(c, `eng-pl-${i}`, `${x.name}@v${x.latest ?? 1}`, x.description ?? "", [x.engine, x.repro_status && x.repro_status.replace(/-/g, " ")])))
        : div().px(14).pb(8).text_size(12.5).text_color(c.grey500).child("No pipeline versions here yet."))
      .child(div().px(14).pt(14).pb(8).text_size(12).font_semibold().text_color(c.grey500).child("MODELS"))
      .child(models.length ? v_flex().border_t(1).border_color(c.rule).children(models.map((x, i) => this.row(c, `eng-md-${i}`, x.name ?? x.id, [x.architecture, x.brand].filter(Boolean).join(", "), [x.version != null ? `v${x.version}` : null, when(x.created)])))
        : div().px(14).text_size(12.5).text_color(c.grey500).child("No models here yet. Attach a model registry to see them."));
  }

  // No engine picked: the workspace's engines, to pick one.
  chooser(c) {
    const es = this.engines;
    return v_flex().size_full().p(24).gap(12)
      .child(div().text_size(18).font_semibold().text_color(c.navy).child("Engines"))
      .child(div().text_size(13).text_color(c.grey900).child("Pick an engine in the Explorer, or here, to see its investigations, experiments, runs and versions."))
      .child(!es ? Skeleton(c, 3) : !es.length
        ? Empty(c, "No engines yet", "Add one from the Explorer's + menu: name the search or recommendation system you measure.")
        : v_flex().border_t(1).border_color(c.rule).children(es.filter((e) => !e.archived).map((e, i) => this.row(c, `eng-pick-${i}`, e.name, SERVES[e.type] ?? e.type,
          [`${(e.pipelines ?? []).length} pipelines`], (_ev, cx) => { setShared("captain.scope", { kind: "engine", id: e.id, label: e.name }); cx.notify(); }))));
  }

  render() {
    const c = palette(isDark(theme()));
    if (!this.pick()) return v_flex().id("engine").size_full().bg(c.surface).font_family(FONT).child(this.chooser(c));
    const e = this.engine;
    const node = e ? { kind: "engine", id: e.id, type: e.type, name: e.name } : null;
    return v_flex().id("engine").size_full().bg(c.surface).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(56).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
        .child(div().size(30).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue)
          .child(svg(icon(e?.type === "recommendation" ? "sparkles" : "search")).size(16)))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(h_flex().gap(8).items_center()
            .child(div().truncate().text_size(17).font_semibold().child(e ? e.name : nodeName(this.pick())))
            .when(Boolean(e?.type), (x) => x.child(Badge(c, SERVES[e.type] ?? e.type, "blue"))))
          .child(div().truncate().text_size(12).text_color(c.grey500)
            .child(!e ? "" : e.missing ? "This engine isn't in the workspace any more." : this.where.length ? `In ${this.where.join(", ")}` : "Not in any scope")))
        .when(Boolean(node) && !e?.missing, (x) => x
          .child(Button(c, "eng-rename", "Rename", () => openOverlay({ mod: "captain-scope", kind: "dialog", width: 460, props: { action: "rename", node } }), { small: true }))
          .child(Button(c, "eng-move", "Move", () => openOverlay({ mod: "captain-scope", kind: "dialog", width: 460, props: { action: "move", node } }), { small: true }))
          .child(Button(c, "eng-archive", "Archive", () => openOverlay({ mod: "captain-scope", kind: "dialog", width: 460, props: { action: "archive", node } }), { small: true }))))
      .child(div().px(20).child(Tabs(c, "eng-tabs", TABS.map(([id, label]) => ({ id, label, count: id === "overview" ? null : this.count(id) })), this.tab, (id, cx) => { this.tab = id; cx.notify(); })))
      .child(div().id("eng-body").flex_1().min_h(0).overflow_y_scroll().child(!e ? Skeleton(c, 6) : this.body(c)));
  }
}
