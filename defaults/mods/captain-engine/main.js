// CAPTAIN: an engine's page (Edgar's spec, 2026-11-06), opened by picking an engine in the Explorer. Its tabs read
// the services' lists filtered to it (the kit sends X-CST-Scope "engine:<id>", backend 4d6a983d):
//   Overview        the engine: what it serves, where it belongs, its pipelines, and a count for every tab
//   Investigations  /api/investigations          Experiments  /api/backtests
//   Runs            /api/runs (every run)         Evaluations  /api/grading
//   Datasets        /api/datasets                 Signals      /api/signals
//   Versions        its pipelines' versions (/api/pipelines) and models (/api/models)
// A row opens the record in its section (shared "captain.open"). Rename, Move and Archive open captain-scope.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, shared, setShared, run, openOverlay, notify } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { api, watchScope, watchWorkspace } from "./kit/cst.js";
import { Button, Badge, Tabs, Empty, Skeleton, Problem } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { nodeName } from "./kit/scopes.js";
import { int } from "./kit/num.js";
import { costState, loadPlaces, refreshEstimate, needsConfirm, RunsOn, LOCAL } from "./kit/cost.js";

const TABS = [["overview", "Overview"], ["investigations", "Experiments"], ["experiments", "Offline evaluations"], ["runs", "Runs"],
  ["evaluations", "Rating"], ["datasets", "Datasets"], ["signals", "Signals"], ["versions", "Versions"]];
const SERVES = { search: "Search", recommendation: "Recommendations" };
const list = (r) => (r?.ok ? (Array.isArray(r.data) ? r.data : r.data?.items ?? []) : null);
const when = (iso) => (iso ? String(iso).slice(0, 10) : "");

export default class Engine extends View {
  init(_props, cx) {
    this.tab = "overview";
    this.engine = null; this.data = {}; this.where = [];
    // Retrain asks first where it runs and what that costs (kit cost.js): the row opens under the version picked.
    this.cost = costState();
    this.asking = null; // the model version whose "Runs on" row is open
    cx.timer.every(500, (cx) => {
      if (!this.asking) return;
      cx.spawn(async (cx) => { if (await refreshEstimate(this.cost, { kind: "training", size: { how: "command" } })) cx.notify(); });
    });
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
      const [engines, tree, inv, bt, rn, ds, pl, md, sg, gr] = await Promise.all([
        api("/api/engines", { scoped: false }), api("/api/explorer", { scoped: false }),
        api("/api/investigations"), api("/api/backtests"), api("/api/runs"), api("/api/datasets"), api("/api/pipelines"), api("/api/models"),
        api("/api/signals"), api("/api/grading"),
      ]);
      if (seq !== this.seq) return;
      this.engine = (list(engines) ?? []).find((e) => e.id === pick.id) ?? { id: pick.id, name: pick.label, missing: true };
      this.where = this.scopesOf(tree.ok ? tree.data?.tree ?? [] : [], this.engine.scopes ?? []);
      // Every list here is the services', filtered to the engine (X-CST-Scope).
      this.data = { investigations: inv, experiments: bt, runs: rn, datasets: ds, versions: pl, models: md, signals: sg, evaluations: gr };
      cx.notify();
    });
    cx.notify();
  }

  /** Retrain clicked: first show where it would run and what that costs. Nothing starts yet. */
  askRetrain(cx, x) {
    if (this.retraining) return;
    if (this.asking === x.name) { this.asking = null; cx.notify(); return; }
    this.asking = x.name;
    this.cost.confirming = false; this.cost.estimate = null; this.cost.asked = null;
    // It opens on the place the training command declares, else this computer. The place shown is the place sent.
    this.cost.where = x.train_compute && (this.cost.places ?? []).some((p) => p.value === x.train_compute) ? x.train_compute : LOCAL;
    cx.spawn(async (cx) => { if (!this.cost.places) await loadPlaces(this.cost); cx.notify(); });
    cx.notify();
  }

  /** Retrain a pipeline from its declared training command, then watch it in Runs, live. Above the amount set in
   *  Settings, the first click only repeats the number. The second starts it. */
  retrain(cx, x) {
    if (this.retraining) return;
    if (needsConfirm(this.cost)) { this.cost.confirming = true; cx.notify(); return; }
    this.retraining = x.name;
    const compute = this.cost.where === LOCAL ? "local" : this.cost.where;
    cx.spawn(async (cx) => {
      const r = await api(`/api/pipelines/${x.name.split("/").map(encodeURIComponent).join("/")}/retrain`, { body: { compute } });
      this.retraining = null;
      this.asking = null; this.cost.confirming = false;
      if (r.ok && r.data?.live) {
        setShared("captain.open", { section: "runs", live: r.data.live, at: Date.now() });
        run("layout.select", { stack: "sections", child: "runs" });
      } else notify({ title: `Couldn't start retraining ${x.name}`, body: String(r.data?.detail ?? r.error ?? ""), level: "error" });
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
      .child(h_flex().flex_wrap().gap(10).children([["investigations", "Experiments"], ["experiments", "Offline evaluations"], ["runs", "Runs"],
        ["evaluations", "Evaluations"], ["datasets", "Datasets"], ["versions", "Versions"]].map(([k, l]) => card(k, l))))
      .child(v_flex().gap(6)
        .child(div().text_size(12.5).font_semibold().text_color(c.navy).child("Model versions"))
        .child((e.pipelines ?? []).length
          ? h_flex().flex_wrap().gap(6).children(e.pipelines.map((p) => Badge(c, p, "neutral")))
          : div().text_size(12.5).text_color(c.grey500).child("No model versions belong to this engine yet. A model version belongs to the engine that lists it.")));
  }

  body(c) {
    const t = this.tab;
    if (t === "overview") return this.overview(c);
    if (t === "investigations") return this.listTab(c, "investigations", { title: "No experiments here yet", body: "Experiments linked to this engine show here." },
      (x, i) => this.row(c, `eng-inv-${i}`, x.title ?? x.id, x.question ?? x.hypothesis ?? "", [x.status], () => this.open("investigations", { investigation: x.id })));
    if (t === "experiments") return this.listTab(c, "experiments", { title: "No offline evaluations here yet", body: "Offline evaluations comparing this engine's model versions show here." },
      (x, i) => this.row(c, `eng-exp-${i}`, x.name, (x.arms ?? []).join(" vs "), [x.primary_metric, `${x.runs ?? 0} runs`], () => this.open("comparisons", { backtest: x.name, version: x.latest })));
    if (t === "runs") return this.listTab(c, "runs", { title: "No runs here yet", body: "Every run of this engine's offline evaluations shows here, newest first." },
      (x, i) => this.row(c, `eng-run-${i}`, `${x.name}, run ${x.version}`, [(x.arms ?? []).join(" vs "), when(x.created), x.by].filter(Boolean).join(", "),
        [x.complete === false ? "incomplete" : null, x.mode, x.wall_s != null ? `${Math.round(x.wall_s)} s` : null], () => this.open("runs", { backtest: x.name, version: x.version })));
    if (t === "evaluations") return this.listTab(c, "evaluations", { title: "No rating queues here yet", body: "Rating over this engine's model versions shows here." },
      (x, i) => this.row(c, `eng-eval-${i}`, x.name, `${x.graded ?? 0} of ${x.pairs ?? 0} rated, depth ${x.depth ?? "?"}`, [x.queries], () => this.open("runs", { grading: x.name })));
    if (t === "datasets") return this.listTab(c, "datasets", { title: "No datasets here yet", body: "Datasets linked to this engine show here." },
      (x, i) => this.row(c, `eng-ds-${i}`, x.name, x.description ?? "", [x.kind, x.rows != null ? `${int(x.rows)} rows` : null, `v${x.latest ?? 1}`], () => this.open("data", { dataset: x.name })));
    if (t === "signals") return this.listTab(c, "signals", { title: "No signals here yet", body: "Signals this engine ranks with show here." },
      (x, i) => this.row(c, `eng-sig-${i}`, x.name ?? x.id, x.description ?? x.source ?? "", [x.status]));
    // Versions: its pipelines' versions, then its models.
    const p = this.data.versions, m = this.data.models;
    if (!p || !m) return Skeleton(c, 5);
    if (!p.ok) return Problem(c, p, (cx) => this.load(cx));
    const pipes = list(p), models = list(m) ?? [];
    return v_flex().w_full()
      .child(div().px(14).py(8).text_size(12).font_semibold().text_color(c.grey500).child("MODEL VERSIONS"))
      .child(pipes.length ? v_flex().border_t(1).border_color(c.rule).children(pipes.map((x, i) => v_flex().child(h_flex().child(div().flex_1().min_w(0).child(this.row(c, `eng-pl-${i}`, `${x.name}@v${x.latest ?? 1}`, x.description ?? "", [x.engine, x.repro_status && x.repro_status.replace(/-/g, " ")])))
        // A pipeline that declares its training command can retrain from here; Runs opens on it, live.
        .when(!!x.trains, (e) => e.child(div().id(`eng-retrain-${i}`).px(10).text_size(12).text_color(c.blue).cursor_pointer().hover((s) => s.underline())
          .on_click((_e, cx) => this.askRetrain(cx, x)).child(this.retraining === x.name ? "Starting..." : "Retrain")))
        .child(div().id(`eng-whatif-${i}`).px(14).text_size(12).text_color(c.blue).cursor_pointer().hover((s) => s.underline())
          .on_click(() => openOverlay({ mod: "captain-sandbox", kind: "dialog", width: 0.9, height: 0.9, props: { pipeline: x.name } })).child("Evaluation settings")))
        // Before it starts: where it trains and what that costs, then the button that starts it.
        .when(this.asking === x.name, (e) => e.child(v_flex().id(`eng-retrain-ask-${i}`).px(14).pb(12).gap(8)
          .child(RunsOn(c, "eng-runs-on", this.cost, {
            onPick: (p, _e, cx) => { if (this.cost.where !== p.value) this.cost.estimate = null; this.cost.where = p.value; this.cost.confirming = false; cx.notify(); },
            onToggle: (_e, cx) => { this.cost.open = !this.cost.open; cx.notify(); },
            onAdd: () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "compute" } }),
          }, { note: "It retrains from the training command this model version declares. Runs opens on it, live." }))
          .child(h_flex().gap(8)
            .child(Button(c, "eng-retrain-go", this.retraining === x.name ? "Starting..." : this.cost.confirming ? "Start anyway" : "Start retraining", (_e, cx) => this.retrain(cx, x), { kind: "primary", small: true, disabled: Boolean(this.retraining) }))
            .child(Button(c, "eng-retrain-cancel", "Not now", (_e, cx) => { this.asking = null; this.cost.confirming = false; cx.notify(); }, { small: true, kind: "quiet" })))))))
        : div().px(14).pb(8).text_size(12.5).text_color(c.grey500).child("No model versions here yet."))
      .child(div().px(14).pt(14).pb(8).text_size(12).font_semibold().text_color(c.grey500).child("MODELS"))
      .child(models.length ? v_flex().border_t(1).border_color(c.rule).children(models.map((x, i) => this.row(c, `eng-md-${i}`, x.name ?? x.id, [x.architecture, x.brand].filter(Boolean).join(", "), [x.version != null ? `v${x.version}` : null, when(x.created)])))
        : div().px(14).text_size(12.5).text_color(c.grey500).child("No models here yet. Attach a model registry to see them."));
  }

  // No engine picked (All engines): the workspace's engines, to pick one, and what an engine is when there are none.
  chooser(c) {
    const es = (this.engines ?? []).filter((e) => !e.archived);
    const add = () => openOverlay({ mod: "captain-scope", kind: "dialog", width: 460, props: { action: "add", kind: "engine", type: "engine", parent: null } });
    return v_flex().size_full().p(24).gap(12)
      .child(h_flex().items_center().gap(12)
        .child(div().flex_1().text_size(18).font_semibold().text_color(c.navy).child("All engines"))
        .when(es.length > 0, (e) => e.child(Button(c, "eng-add", "Add engine", add, { small: true }))))
      .child(div().text_size(13).text_color(c.grey900).child("The search and recommendation systems you're improving, across every brand. Pick one to see its experiments, evaluations, runs and versions."))
      .child(!this.engines ? Skeleton(c, 3) : !es.length
        ? v_flex().id("eng-empty").mt(12).p(24).gap(10).items_center().border_1().border_color(c.rule).bg(c.surface2)
          .child(svg(icon("search")).size(28).text_color(c.blue))
          .child(div().text_size(15).font_semibold().text_color(c.navy).child("No engines yet"))
          .child(div().max_w(460).text_size(13).line_height("20px").text_color(c.grey900).child("An engine is one lasting name for a search or recommendation system. Its models and their versions change over time; the engine stays, so its history stays together."))
          .child(div().text_size(12.5).text_color(c.grey500).child("For example: North Pole gift search."))
          .child(div().pt(4).child(Button(c, "eng-add-first", "Add your first engine", add, { kind: "primary" })))
        : v_flex().border_t(1).border_color(c.rule).children(es.map((e, i) => this.row(c, `eng-pick-${i}`, e.name, SERVES[e.type] ?? e.type,
          [`${(e.pipelines ?? []).length} model versions`], (_ev, cx) => { setShared("captain.scope", { kind: "engine", id: e.id, label: e.name }); cx.notify(); }))));
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
