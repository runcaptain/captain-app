// captain-ui: the MLflow bridge, read-only (backend/docs/api.md, "MLflow bridge"). Captain only searches MLflow: it
// never writes there. Two pieces:
//
//   const ml = new MlflowList({ id: "ml", onSetup: () => openForm("mlflow") });
//   ml.load(cx);  ml.render(c, w)     // experiments, then their runs (status, metrics, params); registered models and
//                                     // their versions, each with "Served by pipeline..." (POST /api/mlflow/links)
//   graph = await withMlflow(graph)   // MLflow runs and model versions upstream of the pipelines they serve
//
// Errors read as the services say them (MLflow refused the login, couldn't be reached, isn't set up).
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { api } from "./cst.js";
import { shared } from "herdr";
import { MONO } from "./tokens.js";
import { Button, Skeleton } from "./ui.js";
import { icon } from "./icons.js";
import { Brand } from "./brands.js";
import { num, int } from "./num.js";

const sentence = (t, fallback) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : fallback; };
const said = (r, fallback) => sentence(r.data?.error ?? r.data?.detail ?? r.error, fallback);
const STATUS = { FINISHED: "finished", RUNNING: "running", FAILED: "failed", KILLED: "stopped", SCHEDULED: "waiting" };
const day = (iso) => String(iso ?? "").slice(0, 10);

/** MLflow's lineage merged into a Captain lineage graph: a model version upstream of each version of the pipeline that
 *  serves it, and the MLflow run that produced the model upstream of that. The graph is returned as it was when MLflow
 *  isn't set up, has no links, or can't be read. */
export async function withMlflow(graph) {
  const r = await api("/api/mlflow/lineage", { scoped: false });
  if (!r.ok || !(r.data?.nodes ?? []).length) return graph;
  const nodes = [...(graph?.nodes ?? [])];
  const edges = [...(graph?.edges ?? [])];
  const have = new Set(nodes.map((n) => n.id));
  const pipeName = (id) => (/^pipeline:(.*)@v\d+$/.exec(id) ?? [])[1];
  const byId = new Map(r.data.nodes.map((n) => [n.id, n]));
  const added = new Set();
  for (const e of r.data.edges ?? []) {
    if (e.relation !== "served_by") continue;
    const targets = nodes.filter((n) => n.kind === "pipeline" && pipeName(n.id) === pipeName(e.dst));
    if (!targets.length) continue;
    if (!have.has(e.src)) { nodes.push({ ...byId.get(e.src) }); have.add(e.src); added.add(e.src); }
    for (const t of targets) edges.push({ src: e.src, dst: t.id, relation: "served_by" });
  }
  for (const e of r.data.edges ?? []) {
    if (e.relation !== "produced" || !added.has(e.dst)) continue;
    if (!have.has(e.src)) { nodes.push({ ...byId.get(e.src) }); have.add(e.src); }
    edges.push({ src: e.src, dst: e.dst, relation: "produced" });
  }
  return { ...graph, nodes, edges };
}

export class MlflowList {
  constructor({ id = "ml", onSetup } = {}) {
    this.id = id;
    this.onSetup = onSetup;
    this.status = null; // {configured, uri, databricks_connection, links}
    this.experiments = null; // [...] | {error}
    this.models = null; // [...] | {error}
    this.open = new Set(); // experiment ids and model names shown open
    this.runs = new Map(); // experiment id -> [...] | {error} | null (loading)
    this.linking = null; // `${model}@${version}`: its pipeline list is open
    this.pipelines = null;
    this.note = null;
  }

  load(cx) {
    // Read again when MLflow is connected or changed anywhere (the Connect MLflow form says so).
    if (!this.watching && cx.timer) {
      this.watching = true;
      this.seen = shared("captain.mlflow.changed") ?? null;
      cx.timer.every(1000, (cx) => { const now = shared("captain.mlflow.changed") ?? null; if (now !== this.seen) { this.seen = now; this.reload(cx); } });
    }
    this.fetch(cx);
  }

  reload(cx) {
    this.experiments = null; this.models = null; this.runs.clear(); this.open.clear();
    this.fetch(cx);
  }

  fetch(cx) {
    cx.spawn(async (cx) => {
      const s = await api("/api/mlflow", { scoped: false });
      if (!s.ok) { this.status = { error: said(s, "Couldn't read the MLflow settings.") }; cx.notify(); return; }
      this.status = s.data;
      if (!s.data.configured) { cx.notify(); return; }
      const [e, m] = await Promise.all([api("/api/mlflow/experiments", { scoped: false, timeout: 60 }), api("/api/mlflow/models", { scoped: false, timeout: 60 })]);
      this.experiments = e.ok ? e.data : { error: said(e, "Couldn't read MLflow's experiments.") };
      this.models = m.ok ? m.data : { error: said(m, "Couldn't read MLflow's registered models.") };
      cx.notify();
    });
  }

  toggleExperiment(cx, id) {
    if (this.open.has(id)) { this.open.delete(id); cx.notify(); return; }
    this.open.add(id);
    if (!this.runs.has(id)) {
      this.runs.set(id, null);
      cx.spawn(async (cx) => {
        const r = await api(`/api/mlflow/experiments/${encodeURIComponent(id)}/runs?limit=50`, { scoped: false, timeout: 60 });
        this.runs.set(id, r.ok ? r.data : { error: said(r, "Couldn't read this experiment's runs.") });
        cx.notify();
      });
    }
    cx.notify();
  }

  toggleModel(cx, name) {
    if (this.open.has(`m:${name}`)) this.open.delete(`m:${name}`); else this.open.add(`m:${name}`);
    cx.notify();
  }

  /** "Served by pipeline...": the pipelines to pick from. */
  openLink(cx, key) {
    this.linking = this.linking === key ? null : key;
    if (this.linking && this.pipelines === null) {
      cx.spawn(async (cx) => {
        const r = await api("/api/pipelines", { scoped: false });
        this.pipelines = r.ok && Array.isArray(r.data) ? r.data.map((p) => p.name).filter(Boolean) : [];
        cx.notify();
      });
    }
    cx.notify();
  }

  link(cx, model, version, pipeline) {
    this.linking = null;
    cx.spawn(async (cx) => {
      const r = await api("/api/mlflow/links", { body: { model, version, pipeline }, scoped: false });
      if (r.ok) {
        (this.status.links ??= {})[`${model}@${version}`] = r.data.pipeline;
        this.note = { text: `${model} version ${version} is served by ${r.data.pipeline}. It shows upstream of that pipeline in lineage.`, tone: "good" };
      } else this.note = { text: said(r, "Couldn't record that link."), tone: "fail" };
      cx.notify();
    });
    cx.notify();
  }

  icon(name, size, color) {
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  render(c, w = 320) {
    const id = this.id;
    const s = this.status;
    const head = h_flex().gap(6).items_center().px(10).py(6).child(Brand(c, "mlflow", 14)).child(div().text_size(12).font_medium().text_color(c.navy).child("MLflow"))
      .when(!!s?.configured, (e) => e.child(div().flex_1().min_w(0).truncate().text_size(11).text_color(c.grey500).child(s.databricks_connection ? `on ${s.databricks_connection}` : s.uri ?? "")));
    if (!s) return v_flex().id(id).child(head).child(div().px(10).child(Skeleton(c, 2)));
    if (s.error) return v_flex().id(id).child(head).child(div().px(10).text_size(12).text_color(c.fail).child(s.error));
    if (!s.configured) return v_flex().id(id).gap(4).child(head)
      .child(div().px(10).text_size(12).line_height("18px").text_color(c.grey900).child("See your MLflow experiments, runs and registered models here, read-only, and which pipeline serves each model."))
      .when(!!this.onSetup, (e) => e.child(div().px(10).pb(6).child(Button(c, `${id}-setup`, "Connect MLflow", () => this.onSetup(), { small: true }))));
    const row = (rid, depth, kids, onClick) => h_flex().id(rid).min_h(26).py(3).pl(10 + depth * 14).pr(10).gap(6).items_center().text_size(12)
      .when(!!onClick, (e) => e.cursor_pointer().hover((st) => st.bg(c.surface2)).on_click(onClick)).children(kids);
    const chev = (open) => this.icon(open ? "chevron-down" : "chevron-right", 11, c.grey500);
    const exps = this.experiments, models = this.models;
    return v_flex().id(id).gap(2).child(head)
      .when(!!this.note, (e) => e.child(div().px(10).text_size(11.5).text_color(this.note.tone === "fail" ? c.fail : c.good).child(this.note.text)))
      .child(div().px(10).pt(4).text_size(11).text_color(c.grey500).child("Experiments"))
      .when(exps === null, (e) => e.child(div().px(10).child(Skeleton(c, 2))))
      .when(!!exps?.error, (e) => e.child(div().px(10).text_size(12).text_color(c.fail).child(exps.error)))
      .when(Array.isArray(exps) && !exps.length, (e) => e.child(div().px(10).text_size(12).text_color(c.grey500).child("No experiments yet.")))
      .children(Array.isArray(exps) ? exps.flatMap((x) => {
        const open = this.open.has(x.id);
        const runs = this.runs.get(x.id);
        return [
          row(`${id}-exp-${x.id}`, 0, [chev(open), div().flex_1().min_w(0).truncate().text_color(c.navy).child(x.name ?? x.id), div().flex_none().text_size(11).text_color(c.grey500).child(day(x.updated))], (_e, cx) => this.toggleExperiment(cx, x.id)),
          ...(!open ? [] : runs === null || runs === undefined ? [div().pl(34).child(Skeleton(c, 1))]
            : runs.error ? [div().pl(34).pr(10).text_size(12).text_color(c.fail).child(runs.error)]
            : !runs.length ? [div().pl(34).text_size(12).text_color(c.grey500).child("No runs.")]
            : runs.map((r) => v_flex().id(`${id}-run-${r.run_id}`).pl(34).pr(10).py(3).gap(1).text_size(11.5)
              .child(h_flex().gap(6).items_center()
                .child(div().flex_1().min_w(0).truncate().text_color(c.navy).child(r.name || r.run_id?.slice(0, 8)))
                .child(div().flex_none().text_color(r.status === "FAILED" ? c.fail : r.status === "RUNNING" ? c.blue : c.grey500).child(STATUS[r.status] ?? String(r.status ?? "").toLowerCase())))
              .when(Object.keys(r.metrics ?? {}).length > 0, (e) => e.child(div().truncate().font_family(MONO).text_color(c.grey900)
                .child(Object.entries(r.metrics).slice(0, 4).map(([k, v]) => `${k} ${num(v, 4)}`).join("  "))))
              .when(Object.keys(r.params ?? {}).length > 0, (e) => e.child(div().truncate().font_family(MONO).text_color(c.grey500)
                .child(Object.entries(r.params).slice(0, 4).map(([k, v]) => `${k}=${v}`).join("  ")))))),
        ];
      }) : [])
      .child(div().px(10).pt(6).text_size(11).text_color(c.grey500).child("Registered models"))
      .when(models === null, (e) => e.child(div().px(10).child(Skeleton(c, 2))))
      .when(!!models?.error, (e) => e.child(div().px(10).text_size(12).text_color(c.fail).child(models.error)))
      .when(Array.isArray(models) && !models.length, (e) => e.child(div().px(10).text_size(12).text_color(c.grey500).child("No registered models.")))
      .children(Array.isArray(models) ? models.flatMap((m) => {
        const open = this.open.has(`m:${m.name}`);
        return [
          row(`${id}-model-${m.name}`, 0, [chev(open), div().flex_1().min_w(0).truncate().text_color(c.navy).child(m.name), div().flex_none().text_size(11).text_color(c.grey500).child(`${int((m.versions ?? []).length)} versions`)], (_e, cx) => this.toggleModel(cx, m.name)),
          ...(!open ? [] : (m.versions ?? []).flatMap((v) => {
            const key = `${m.name}@${v.version}`;
            const served = s.links?.[key];
            return [
              v_flex().id(`${id}-mv-${key}`).pl(34).pr(10).py(3).gap(2).text_size(11.5)
                .child(h_flex().gap(6).items_center()
                  .child(div().font_family(MONO).text_color(c.navy).child(`v${v.version}`))
                  .child(div().text_color(c.grey500).child([String(v.status ?? "").toLowerCase(), v.stage && v.stage !== "None" ? v.stage.toLowerCase() : null, ...(v.aliases ?? []).map((a) => `@${a}`)].filter(Boolean).join(", ")))
                  .child(div().flex_1())
                  .child(div().id(`${id}-link-${key}`).cursor_pointer().text_color(c.blue).on_click((_e, cx) => this.openLink(cx, key))
                    .child(served ? `Served by ${served}` : "Served by pipeline...")))
                .when(!!v.run_id, (e) => e.child(div().font_family(MONO).text_color(c.grey500).child(`from run ${v.run_id.slice(0, 8)}`))),
              ...(this.linking !== key ? [] : [v_flex().id(`${id}-pick-${key}`).ml(34).mr(10).border_1().border_color(c.grey300).bg(c.surface)
                .when(this.pipelines === null, (e) => e.child(div().p(6).child(Skeleton(c, 1))))
                .when(Array.isArray(this.pipelines) && !this.pipelines.length, (e) => e.child(div().p(6).text_size(12).text_color(c.grey500).child("No pipelines in this workspace yet.")))
                .children((this.pipelines ?? []).map((p, i) => div().id(`${id}-pick-${key}-${i}`).h(24).px(8).flex().items_center().cursor_pointer().text_size(12).font_family(MONO)
                  .text_color(p === served ? c.blue : c.navy).hover((st) => st.bg(c.surface2)).on_click((_e, cx) => this.link(cx, m.name, v.version, p)).child(p)))]),
            ];
          })),
        ];
      }) : [])
      .child(div().px(10).pt(4).pb(6).text_size(10.5).text_color(c.grey500).child("Read-only: Captain only searches MLflow, it never changes it."));
  }
}
