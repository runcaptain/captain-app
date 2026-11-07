// CAPTAIN: a project's home (Edgar, 2026-11-07: "experience everything through projects"). The Explorer's project row
// opens it, with the project as the pick (shared "captain.scope" {kind: "project"}). From GET /api/projects/{id}/home:
// its datasets, pipelines, models (registry and MLflow), notebooks, recent runs, investigations and connections, each
// opening where it lives: a dataset in Data, a run in Comparisons, an investigation in Investigations, a notebook, a
// model or a connection in Data's tab for it, a pipeline in Backtest settings.
// Choose models (Edgar, 2026-11-07: "change which models were included"): every registry model, by brand, each with a
// box. A whole brand includes its future models too (PUT /api/projects/{id} brands). One model is put in or left out
// on its own (POST /api/projects/assign), which beats the brand rule.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, shared, setShared, run, openOverlay, props as layoutProps } from "herdr";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { api, watchScope, watchWorkspace, watchRecords } from "./kit/cst.js";
import { Badge, Button, Empty, Skeleton, Problem } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int } from "./kit/num.js";

const day = (iso) => (iso ? String(iso).slice(0, 10) : "");
const go = (section, extra) => { setShared("captain.open", { section, ...extra, at: Date.now() }); run("layout.select", { stack: "sections", child: section }); };

export default class ProjectHome extends View {
  init(_props, cx) {
    this.home = null; this.problem = null; this.projects = null;
    this.choosing = false; this.allModels = null; this.owners = {}; this.note = null; this.busy = false;
    this.load(cx);
    if ((layoutProps?.() ?? {}).chooseModels) this.toggleChoosing(cx); // a layout, or the real-window check
    watchScope(cx, (cx) => this.load(cx));
    watchWorkspace(cx, (cx) => this.load(cx));
    watchRecords(cx, (cx) => this.load(cx));
    let rev = shared("captain.explorer.rev") ?? null;
    cx.timer.every(1000, (cx) => { const r = shared("captain.explorer.rev") ?? null; if (r !== rev) { rev = r; this.load(cx); } });
  }

  pick() { const s = shared("captain.scope"); return s?.kind === "project" ? s : null; }

  load(cx) {
    const pick = this.pick();
    const seq = (this.seq = (this.seq ?? 0) + 1);
    this.problem = null;
    if (!pick) {
      // No project picked: every project, to pick one here.
      this.home = null;
      cx.spawn(async (cx) => {
        const r = await api("/api/projects", { scoped: false });
        if (seq !== this.seq) return;
        this.projects = r.ok ? (Array.isArray(r.data) ? r.data : r.data?.projects ?? []) : [];
        cx.notify();
      });
      cx.notify();
      return;
    }
    if (this.home?.project?.id !== pick.id) this.home = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${encodeURIComponent(pick.id)}/home`, { scoped: false });
      if (seq !== this.seq) return;
      if (r.ok) this.home = r.data; else { this.home = null; this.problem = r; }
      cx.notify();
    });
  }

  /** Every registry model, and which project holds each brand, for the picker. */
  loadChoices(cx) {
    cx.spawn(async (cx) => {
      const [m, p] = await Promise.all([api("/api/models", { scoped: false }), api("/api/projects", { scoped: false })]);
      this.allModels = m.ok && Array.isArray(m.data) ? m.data : [];
      this.owners = {};
      for (const pr of p.ok ? (Array.isArray(p.data) ? p.data : p.data?.projects ?? []) : []) for (const b of pr.brands ?? []) this.owners[b] = pr;
      cx.notify();
    });
  }

  toggleChoosing(cx) {
    this.choosing = !this.choosing;
    this.note = null;
    if (this.choosing) this.loadChoices(cx);
    cx.notify();
  }

  /** Save one change, then read the project again. */
  change(cx, req, note) {
    if (this.busy) return;
    this.busy = true;
    cx.spawn(async (cx) => {
      for (const [path, opts] of req) {
        const r = await api(path, { ...opts, scoped: false });
        if (!r.ok) { this.busy = false; this.note = { text: String(r.data?.detail ?? r.error ?? "Couldn't save that."), tone: "fail" }; cx.notify(); return; }
      }
      this.busy = false;
      this.note = { text: note, tone: "good" };
      setShared("captain.explorer.rev", Date.now()); // the Explorer and other screens count again
      this.load(cx);
      this.loadChoices(cx);
    });
    cx.notify();
  }

  toggleModel(cx, m, isIn) {
    const pid = this.home.project.id;
    this.change(cx, [["/api/projects/assign", { body: { project: isIn ? null : pid, items: [{ kind: "model", name: m.id }] } }]],
      isIn ? `Left ${m.brand} ${m.version} out of ${this.home.project.name}.` : `Added ${m.brand} ${m.version} to ${this.home.project.name}.`);
  }

  toggleBrand(cx, brand, models, on) {
    const p = this.home.project;
    const brands = on ? (p.brands ?? []).filter((b) => b !== brand) : [...(p.brands ?? []), brand];
    const req = [[`/api/projects/${encodeURIComponent(p.id)}`, { method: "PUT", body: { brands } }]];
    // Turning a brand on brings back every model of it, including ones left out one by one.
    if (!on) req.push(["/api/projects/assign", { body: { project: p.id, clear: true, items: models.map((m) => ({ kind: "model", name: m.id })) } }]);
    this.change(cx, req, on ? `${brand}'s models are no longer included by brand. Models you added one by one stay.` : `Every ${brand} model is in ${p.name}, and new ones will be too.`);
  }

  box(c, on) {
    return svg(icon(on ? "square-check" : "square")).size(15).flex_none().text_color(on ? c.blue : c.grey500);
  }

  /** The picker: every registry model by brand, with a box for the brand and one for each model. */
  chooser(c) {
    const h = this.home, all = this.allModels;
    if (!all) return div().px(14).py(10).text_size(12.5).text_color(c.grey500).child("Reading the registries...");
    if (!all.length) return div().px(14).py(10).text_size(12.5).text_color(c.grey500).child("No registry models yet. Attach a registry in Data and signals, Models.");
    const mine = new Set((h.models ?? []).map((m) => m.id));
    const byBrand = new Map();
    for (const m of all) { if (!byBrand.has(m.brand)) byBrand.set(m.brand, []); byBrand.get(m.brand).push(m); }
    const brands = [...byBrand.keys()].sort();
    return v_flex().id("ph-choose").max_h(420).overflow_y_scrollbar().border_b(1).border_color(c.rule).bg(c.surface2)
      .child(div().px(14).py(8).text_size(12).text_color(c.grey900).child("Tick a brand to include all its models, and the ones trained later. Or tick models one by one."))
      .children(brands.map((b, bi) => {
        const ms = byBrand.get(b).sort((x, y) => String(x.version).localeCompare(String(y.version), undefined, { numeric: true }));
        const on = (h.project.brands ?? []).includes(b);
        const other = !on && this.owners[b] && this.owners[b].id !== h.project.id ? this.owners[b].name : null;
        return v_flex().id(`ph-cb-${bi}`)
          .child(h_flex().id(`ph-brand-${bi}`).h(32).px(14).gap(8).items_center().cursor_pointer().hover((s) => s.bg(c.surface))
            .on_click((_e, cx) => this.toggleBrand(cx, b, ms, on))
            .child(this.box(c, on))
            .child(div().text_size(12.5).font_semibold().child(`All ${b} models`))
            .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(
              `${ms.filter((m) => mine.has(m.id)).length} of ${ms.length} in this project${other ? `. Now in ${other}, ticking moves the brand here` : ""}`)))
          .children(ms.map((m, i) => {
            const isIn = mine.has(m.id);
            return h_flex().id(`ph-pick-${bi}-${i}`).h(28).pl(36).pr(14).gap(8).items_center().cursor_pointer().hover((s) => s.bg(c.surface))
              .on_click((_e, cx) => this.toggleModel(cx, m, isIn))
              .child(this.box(c, isIn))
              .child(div().w(180).flex_none().truncate().font_family(MONO).text_size(12).child(`${m.brand} ${m.version}`))
              .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child([m.architecture, m.status_kind ?? m.status].filter(Boolean).join(", ")));
          }));
      }));
  }

  // One row that opens something: a name, a line under it, chips, and where it goes.
  row(c, id, title, sub, chips, onClick, mono = true) {
    return h_flex().id(id).min_h(40).px(14).py(6).gap(10).items_center().border_b(1).border_color(c.rule).cursor_pointer()
      .hover((s) => s.bg(c.surface2)).on_click(onClick)
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(div().truncate().text_size(13).font_medium().text_color(c.navy).when(mono, (e) => e.font_family(MONO).text_size(12.5)).child(title))
        .when(Boolean(sub), (e) => e.child(div().truncate().text_size(12).text_color(c.grey500).child(sub))))
      .children((chips ?? []).filter(Boolean).map((ch) => Badge(c, ch, "neutral")))
      .child(svg(icon("chevron-right")).size(12).flex_none().text_color(c.grey300));
  }

  // A section: its title and count, its rows, or one line saying how something joins it.
  section(c, key, title, items, rowOf, empty, action = null, extra = null) {
    return v_flex().id(`ph-${key}`).w_full().border_1().border_color(c.rule).bg(c.surface)
      .child(h_flex().h(34).px(14).gap(8).items_center().border_b(1).border_color(c.rule)
        .child(div().text_size(12).font_semibold().text_color(c.grey500).child(title.toUpperCase()))
        .child(div().flex_1().text_size(12).font_family(MONO).text_color(c.grey500).child(int(items.length)))
        .when(Boolean(action), (e) => e.child(action)))
      .when(Boolean(extra), (e) => e.child(extra))
      .children(items.length ? items.slice(0, 12).map((x, i) => rowOf(x, i)) : [div().px(14).py(10).text_size(12.5).text_color(c.grey500).child(empty)]);
  }

  body(c) {
    const h = this.home, id = h.project?.id ?? "";
    const joins = `Assign it to the project, or name it starting with ${id}.`;
    const models = [...(h.models ?? []).map((m) => ({ ...m, from: "registry" })), ...(h.mlflow_models ?? []).map((m) => ({ ...m, from: "MLflow" }))];
    return v_flex().gap(14)
      .child(this.section(c, "datasets", "Datasets", h.datasets ?? [],
        (d, i) => this.row(c, `ph-ds-${i}`, d.name, d.description || `${int(d.rows)} rows, by ${d.created_by ?? "?"} ${day(d.created_at)}`, [d.version != null ? `v${d.version}` : null], () => go("data", { dataset: d.name })),
        `No datasets yet. ${joins}`))
      .child(this.section(c, "runs", "Recent runs", h.runs ?? [],
        (r, i) => this.row(c, `ph-run-${i}`, r.backtest, `${(r.arms ?? []).join(" vs ")}, ${day(r.created_at)}`, [String(r.run ?? "").split("@")[1] ?? null],
          () => go("comparisons", { backtest: r.backtest, version: Number(String(r.run ?? "").split("@v")[1]) || undefined })),
        `No runs yet. Run a backtest on one of its pipelines.`))
      .child(this.section(c, "pipelines", "Pipelines", h.pipelines ?? [],
        (p, i) => this.row(c, `ph-pl-${i}`, `${p.name}@v${p.version ?? 1}`, `Backtest other settings (by ${p.created_by ?? "?"} ${day(p.created_at)})`, [],
          () => openOverlay({ mod: "captain-sandbox", kind: "dialog", width: 0.9, height: 0.9, props: { pipeline: p.name } })),
        `No pipelines yet. ${joins}`))
      .child(this.section(c, "models", "Models", models,
        (m, i) => this.row(c, `ph-md-${i}`, m.model ?? m.name ?? m.brand ?? m.id ?? "", m.pipeline ? `${m.from}, served by ${m.pipeline}` : [m.from, m.architecture, m.status_kind].filter(Boolean).join(", "), [m.version != null ? (/^\d+$/.test(String(m.version)) ? `v${m.version}` : String(m.version)) : null], () => go("data", { view: "models" })),
        "No models yet. Choose models, or tick a registry brand to bring all of its models.",
        Button(c, "ph-choose-models", this.choosing ? "Done" : "Choose models", (_e, cx) => this.toggleChoosing(cx), { small: true, kind: this.choosing ? "primary" : "secondary" }),
        this.choosing ? v_flex().child(this.note ? div().px(14).py(6).text_size(12).text_color(this.note.tone === "fail" ? c.fail : c.good).child(this.note.text) : null).child(this.chooser(c)) : null))
      .child(this.section(c, "notebooks", "Notebooks", h.notebooks ?? [],
        (n, i) => this.row(c, `ph-nb-${i}`, n.name ?? n.title ?? "", n.description ?? "", [], () => go("data", { view: "notebooks", notebook: n.name })),
        "No notebooks yet. Assign a saved notebook to the project."))
      .when((h.investigations ?? []).length > 0, (e) => e.child(this.section(c, "investigations", "Investigations", h.investigations,
        (v, i) => this.row(c, `ph-inv-${i}`, v.title ?? v.id, v.state ?? "", [], () => go("investigations", { investigation: v.id }), false), "")))
      .when((h.connections ?? []).length > 0, (e) => e.child(this.section(c, "connections", "Connections", h.connections,
        (k, i) => this.row(c, `ph-conn-${i}`, k.name, k.engine ?? k.kind ?? "", [], () => go("data", { view: "tables" })), "")));
  }

  render() {
    const c = palette(isDark(theme()));
    const pick = this.pick(), h = this.home;
    const head = h_flex().h(64).flex_none().px(24).gap(12).items_center().border_b(1).border_color(c.rule)
      .child(div().size(32).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("workflow")).size(16)))
      .child(v_flex().flex_1().min_w(0)
        .child(div().text_size(18).font_semibold().truncate().child(h?.project?.name ?? pick?.label ?? "Projects"))
        .child(div().text_size(12).text_color(c.grey500).truncate().child(h?.project?.description || (pick ? "Everything in this project, each opening where it lives." : "Pick a project to see everything in it."))))
      .child(Button(c, "ph-new", "New project", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "project" } }), { small: true }));
    let body;
    if (!pick) {
      const ps = this.projects;
      body = ps === null ? Skeleton(c, 4)
        : !ps.length ? Empty(c, "No projects yet", "A project groups one company's datasets, pipelines, models, notebooks and runs.", Button(c, "ph-empty-new", "New project", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "project" } }), { kind: "primary" }))
        : this.section(c, "all", "Projects", ps, (p, i) => this.row(c, `ph-p-${i}`, p.name, p.description ?? "", [], () => setShared("captain.scope", { kind: "project", id: p.id, label: p.name }), false), "");
    } else if (this.problem) body = Problem(c, this.problem, (cx) => this.load(cx));
    else if (!h) body = Skeleton(c, 6);
    else body = this.body(c);
    return v_flex().id("project-home").size_full().bg(c.canvas).font_family(FONT).text_color(c.navy)
      .child(head)
      .child(div().id("ph-body").flex_1().min_h(0).overflow_y_scroll().child(v_flex().px(24).py(16).child(body)));
  }
}
