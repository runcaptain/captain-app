// CAPTAIN: one page for a dataset, in the shape of Databricks Unity Catalog's table page. Tabs:
// - Overview: description, who made it and when, rows, role, classes and tags, the schema.
// - Sample data: every row, a page at a time, in the kit grid every screen uses (GET /api/datasets/{name}/rows:
//   search, WHERE and sort on the backend), so it reads the same as the table it came from.
// - Details: every version (open any), its files, and its provenance: the source, connection, and the SQL that made it.
// - Lineage: a drawn graph upstream and downstream (GET /api/lineage/graph when the backend has it, else /api/lineage,
//   cut to the chosen depth here), with column-level lineage for a picked column (GET /api/lineage/columns).
// - Insights: what uses it (pipelines, runs, environments, datasets made from it), the notebooks that name it, who works
//   with it, and recent activity (insights.js, GET /api/datasets/{name}/insights).
// - Quality: per column, the share of empty values, distinct count, min and max, plus freshness (GET /api/profile).
//   Hidden while the backend has no profile route.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { api, query } from "./kit/cst.js";
import { ResultGrid } from "./kit/grid.js";
import { MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { LineageGraph } from "./kit/lineage.js";
import { int, num } from "./kit/num.js";
import { icon } from "./kit/icons.js";
// The Health tab (a queries or judgments set): app-shell's, self-contained in the kit.
import { EvalHealth } from "./kit/evals.js";
import { DatasetInsights } from "./insights.js";
import { withMlflow } from "./kit/mlflow.js";

const enc = (name) => String(name).split("/").map(encodeURIComponent).join("/");
const fmtInt = (n) => int(n ?? 0);
const fmtBytes = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : b >= 1e3 ? `${(b / 1e3).toFixed(1)} kB` : `${b ?? 0} B`);
const pct = (x) => (x == null ? "" : `${num(x * 100, 1)}%`);
function ago(iso) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "";
  const s = (Date.now() - t) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} minutes ago` : s < 129600 ? `${Math.round(s / 3600)} hours ago` : `${Math.round(s / 86400)} days ago`;
}
/** "name@v3" as {label: name, version: 3}, for graph nodes. */
const split = (label) => { const m = /^(.*)@v(\d+)$/.exec(String(label ?? "")); return m ? { label: m[1], version: Number(m[2]) } : { label: String(label ?? "") }; };

export const TABS = [["overview", "Overview"], ["sample", "Sample data"], ["details", "Details"], ["lineage", "Lineage"], ["insights", "Insights"], ["quality", "Quality"]];

export class DatasetPage {
  constructor(host) {
    this.host = host;
    this.tab = "overview";
    this.lineage = new LineageGraph({ id: "ds-lineage", onFocus: (id, cx) => this.refocus(cx, id), onColumn: (col, cx) => this.traceColumn(cx, col) });
    this.graphFor = null;
    this.graphError = null;
    this.profile = null; // {columns, freshness} | {missing: true} | {error}
    this.profileFor = null;
    this.hasProfile = true; // false once the backend says it has no profile route
    this.rows = null; // Sample data: the kit grid, paged
    this.rowsFor = null;
    this.file = null; // a dataset with several files: the one shown
    // Image columns (GET /api/datasets/{name}/media): thumbnails in the grid, and where relative names live.
    this.media = null; // {columns: [{column, relative, sample, template, guessed}]} | null
    this.where = InputState.new({ placeholder: "s3://bucket/images/{pid}/{value}" });
    this.where.on("submit", (_e, cx) => this.saveWhere(cx));
    this.whereNote = null;
    this.locate = null; // {state: "looking" | "found" | "missing" | "manual", progress, how, suggestions}
    this.health = new EvalHealth(); // the Health tab (kit/evals.js)
    this.insights = new DatasetInsights(host); // the Insights tab (insights.js)
  }

  /** A new dataset (or version) on the page: lineage and quality load again when their tab opens. */
  reset() {
    this.graphFor = null;
    this.profileFor = null;
    this.profile = null;
    this.rows = null;
    this.rowsFor = null;
    this.file = null;
    this.media = null;
    this.whereNote = null;
    this.locate = null;
    this.insights?.reset();
  }

  open(cx, tab) {
    this.tab = tab;
    if (tab === "lineage") this.loadLineage(cx);
    if (tab === "quality") this.loadProfile(cx);
    if (tab === "sample") this.loadRows(cx);
    if (tab === "insights") this.insights.load(cx, this.host.detail);
    if (tab === "health" && this.host.detail) this.health.open(cx, this.host.detail);
    cx.notify();
  }

  nodeId(d) {
    return `dataset:${d.name}@v${d.version}`;
  }

  loadLineage(cx, focus) {
    const d = this.host.detail;
    if (!d || d.error) return;
    const id = focus ?? this.nodeId(d);
    if (this.graphFor === id) return;
    this.graphFor = id;
    this.graphError = null;
    cx.spawn(async (cx) => {
      const { up, down } = this.lineage;
      let r = await api(`/api/lineage/graph?node=${encodeURIComponent(id)}&up=${Math.max(up, 4)}&down=${Math.max(down, 4)}`);
      if (!r.ok && r.status === 404) r = await api("/api/lineage");
      if (this.graphFor !== id) return;
      if (!r.ok) { this.graphError = "Couldn't load its lineage."; cx.notify(); return; }
      const nodes = (r.data.nodes ?? []).map((n) => ({ ...n, ...split(n.label ?? n.id), columns: n.id === this.nodeId(d) ? (d.schema_fields ?? []).map((f) => f.name) : n.columns }));
      // MLflow runs and model versions upstream of the pipelines they serve, when MLflow is connected and linked.
      const g = await withMlflow({ nodes, edges: r.data.edges ?? [] });
      if (this.graphFor !== id) return;
      this.lineage.set(g, id);
      cx.notify();
    });
  }

  refocus(cx, id) {
    this.graphFor = null;
    this.lineage.set(this.lineage.graph, id);
    this.graphFor = id;
    cx.notify();
  }

  traceColumn(cx, col) {
    const d = this.host.detail;
    if (!col || !d) { this.lineage.setColumns([]); cx.notify(); return; }
    cx.spawn(async (cx) => {
      const r = await api(`/api/lineage/columns?dataset=${encodeURIComponent(`${d.name}@v${d.version}`)}&column=${encodeURIComponent(col)}`);
      // {sources: [{table, column, node}], known, reason}: each source column feeds this column of the focused dataset.
      const focus = this.lineage.focus;
      const srcs = r.ok ? r.data?.sources ?? [] : [];
      this.lineage.setColumns(srcs.map((x) => ({ from: { node: x.node, column: x.column }, to: { node: focus, column: col } })));
      this.columnNote = !r.ok ? (r.status === 404 ? "Column-level lineage needs a newer backend." : "Couldn't trace that column.")
        : r.data?.known === false ? (r.data.reason ? `${r.data.reason[0].toUpperCase()}${r.data.reason.slice(1)}${/[.!?]$/.test(r.data.reason) ? "" : "."}` : "Captain can't tell where that column came from.")
        : srcs.length ? `${col} comes from ${srcs.map((x) => `${x.table}.${x.column}`).join(", ")}.` : null;
      cx.notify();
    });
  }

  /** Sample data: the dataset's rows, paged by the services. */
  loadRows(cx, force = false) {
    const d = this.host.detail;
    if (!d || d.error) return;
    const key = `${d.name}@v${d.version}:${this.file ?? ""}`;
    if (this.rowsFor === key && !force) return;
    this.rowsFor = key;
    const path = `/api/datasets/${enc(d.name)}/rows`;
    const extra = { v: d.version, file: this.file ?? undefined };
    this.rows = new ResultGrid({ id: "ds-rows", name: d.name, searchable: true, openRow: this.openRow ?? null, load: (q) => api(`${path}${query({ ...q, ...extra })}`, { timeout: 60 }),
      thumbs: async (srcs, size, dest) => { const r = await api("/api/media/thumbs", { body: { srcs, size, dest }, timeout: 120 }); return r.ok ? r.data : {}; } });
    this.rows.fetch(cx);
    this.loadMedia(cx, key);
  }

  /** The dataset's image columns, so the grid shows them as pictures. Asked once per dataset and file. */
  loadMedia(cx, key) {
    const d = this.host.detail;
    this.media = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${enc(d.name)}/media${query({ v: d.version, file: this.file ?? undefined })}`, { timeout: 60 });
      if (this.rowsFor !== key || !r.ok) return;
      this.media = r.data;
      const cols = (r.data?.columns ?? []).filter((m) => !m.relative || m.template);
      this.rows?.setMedia(cx, cols);
      const open = (r.data?.columns ?? []).find((m) => m.relative);
      if (open?.template && !this.where.value()) this.where.set_value(open.template);
      // Bare names with nowhere saved: go and find them.
      if (open && !open.template) this.startLocate(cx, open, key);
      cx.notify();
    });
  }

  /** Look for where a column's bare image names live (POST .../media/locate, a job), and use what's found. */
  startLocate(cx, m, key) {
    const d = this.host.detail;
    this.locate = { state: "looking", progress: 0, column: m.column };
    const sleep = (ms) => new Promise((res) => cx.timer.after(ms, () => res()));
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${enc(d.name)}/media/locate`, { body: { column: m.column, v: d.version, file: this.file ?? undefined }, timeout: 30 });
      if (!r.ok || !r.data?.job) { this.locate = { state: "missing", suggestions: [], column: m.column }; cx.notify(); return; }
      for (let i = 0; i < 600 && this.rowsFor === key; i++) {
        const j = await api(`/api/jobs/${encodeURIComponent(r.data.job)}`);
        if (this.rowsFor !== key) return;
        if (j.ok && j.data?.status === "done") {
          const res = j.data.result ?? {};
          if (res.template) {
            m.template = res.template;
            this.where.set_value(res.template);
            this.locate = { state: "found", how: res.how, column: m.column };
            this.rows?.setMedia(cx, (this.media.columns ?? []).filter((x) => !x.relative || x.template));
          } else this.locate = { state: "missing", searched: res.searched, suggestions: res.suggestions ?? [], column: m.column };
          cx.notify();
          return;
        }
        if (j.ok && j.data?.status === "failed") { this.locate = { state: "missing", suggestions: [], column: m.column }; cx.notify(); return; }
        if (j.ok) { this.locate.progress = j.data?.progress ?? 0; cx.notify(); }
        await sleep(600);
      }
    });
  }

  /** A suggested folder picked: the services find the layout inside it and save it. */
  pickFolder(cx, uri) {
    const d = this.host.detail;
    const m = (this.media?.columns ?? []).find((x) => x.relative);
    if (!m) return;
    this.whereNote = { text: `Looking in ${uri}...`, tone: "muted" };
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${enc(d.name)}/media`, { method: "PUT", body: { column: m.column, folder: uri, v: d.version, file: this.file ?? undefined }, timeout: 60 });
      if (r.ok) {
        m.template = r.data.template;
        this.where.set_value(r.data.template);
        this.whereNote = null;
        this.locate = { state: "found", how: `in ${uri}`, column: m.column };
        this.rows?.setMedia(cx, (this.media.columns ?? []).filter((x) => !x.relative || x.template));
      } else this.whereNote = { text: String(r.data?.detail ?? r.error ?? "Nothing there."), tone: "fail" };
      cx.notify();
    });
    cx.notify();
  }

  /** Save where a column's relative image names live; the services check the first row's image first. */
  saveWhere(cx) {
    const d = this.host.detail;
    const m = (this.media?.columns ?? []).find((x) => x.relative);
    if (!m) return;
    const template = this.where.value().trim();
    this.whereNote = { text: "Checking the first row's image...", tone: "muted" };
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${enc(d.name)}/media`, { method: "PUT", body: { column: m.column, template, v: d.version, file: this.file ?? undefined }, timeout: 60 });
      if (r.ok) {
        m.template = template; m.guessed = false;
        this.whereNote = null;
        this.locate = { state: "found", how: "the address you gave", column: m.column };
        this.rows?.setMedia(cx, (this.media.columns ?? []).filter((x) => !x.relative || x.template));
      } else this.whereNote = { text: String(r.data?.detail ?? r.error ?? "Couldn't save it."), tone: "fail" };
      cx.notify();
    });
    cx.notify();
  }

  /** Bare image names: Captain looks for them itself, says where it found them, or offers the likeliest folders. */
  whereBar(c) {
    const m = (this.media?.columns ?? []).find((x) => x.relative);
    const L = this.locate;
    if (!m || !L) return null;
    const tone = { good: c.good, fail: c.fail }[this.whereNote?.tone] ?? c.grey500;
    if (L.state === "looking") return h_flex().id("ds-media-where").flex_none().gap(8).px(10).py(8).items_center().border_1().border_color(c.rule).bg(c.surface2)
      .child(div().flex_1().min_w(0).text_size(12.5).text_color(c.grey900).child(`${m.column} holds image file names. Looking for where the pictures are...`))
      .child(div().text_size(12).font_family(MONO).text_color(c.grey500).child(`${Math.round((L.progress ?? 0) * 100)}%`));
    if (L.state === "found") return h_flex().id("ds-media-where").flex_none().gap(8).px(10).py(6).items_center().text_size(12)
      .child(div().flex_1().min_w(0).truncate().text_color(c.grey900).child(`Pictures for ${m.column} from ${m.template} (${L.how}).`))
      .child(div().id("ds-media-change").flex_none().cursor_pointer().text_color(c.blue).on_click((_e, cx) => { this.locate = { ...L, state: "manual" }; cx.notify(); }).child("Change"));
    const sug = L.suggestions ?? [];
    return v_flex().id("ds-media-where").flex_none().gap(8).p(10).border_1().border_color(c.blue).bg(c.blueLight)
      .child(div().text_size(12.5).text_color(c.navy).child(L.state === "manual"
        ? `Where ${m.column}'s pictures live.`
        : `${m.column} holds image file names like ${m.sample}. Captain didn't find the pictures${L.searched ? ` (it looked through ${L.searched})` : ""}.${sug.length ? " These folders might hold them:" : ""}`))
      .when(sug.length > 0 && L.state !== "manual", (e) => e.child(v_flex().gap(4).children(sug.slice(0, 8).map((x, i) =>
        h_flex().id(`ds-media-sug-${i}`).gap(8).px(8).py(6).items_center().cursor_pointer().bg(c.surface).border_1().border_color(c.rule)
          .hover((st) => st.border_color(c.blue))
          .on_click((_e, cx) => this.pickFolder(cx, x.uri))
          .child(svg(icon("folder")).size(14).flex_none().text_color(c.grey500))
          .child(div().flex_1().min_w(0).truncate().text_size(12).font_family(MONO).text_color(c.navy).child(x.uri))
          .child(div().flex_none().text_size(11.5).text_color(c.grey500).child(x.why))))))
      .child(h_flex().gap(8).items_center()
        .child(div().id("ds-media-input").flex_1().min_w(0).h(28).px(8).flex().items_center().border_1().border_color(c.grey300).bg(c.surface)
          .child(Input.new(this.where).w_full().text_size(12).font_family(MONO).text_color(c.navy)))
        .child(Button(c, "ds-media-save", "Show pictures", (_e, cx) => this.saveWhere(cx), { kind: "primary", small: true })))
      .child(div().text_size(11.5).text_color(tone).child(this.whereNote?.text ?? "Or type the address: {value} is the file name, and any column of the row works too, like {pid}."));
  }

  loadProfile(cx) {
    const d = this.host.detail;
    if (!d || d.error) return;
    const key = `${d.name}@v${d.version}`;
    if (this.profileFor === key) return;
    this.profileFor = key;
    this.profile = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/profile?dataset=${encodeURIComponent(key)}`, { timeout: 120 });
      if (this.profileFor !== key) return;
      if (r.ok) this.profile = r.data;
      else if (r.status === 404 && /not found/i.test(String(r.error))) { this.hasProfile = false; this.profile = { missing: true }; }
      else this.profile = { error: "Couldn't profile this dataset." };
      cx.notify();
    });
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  tabs(c) {
    const tabs = [...TABS.filter(([id]) => id !== "quality" || this.hasProfile), ...(EvalHealth.applies(this.host.detail) ? [["health", "Health"]] : [])];
    return h_flex().flex_none().flex_wrap().gap(4).px(20).pb(10).children(tabs.map(([id, label]) =>
      div().id(`dtab-${id}`).h(26).px(10).flex().items_center().cursor_pointer().text_size(12)
        .border_1().border_color(this.tab === id ? c.navy : c.grey300).bg(this.tab === id ? c.navy : c.surface).text_color(this.tab === id ? c.surface : c.grey900)
        .on_click((_e, cx) => this.open(cx, id)).child(label)));
  }

  body(c, w, d) {
    const h = this.host;
    if (this.tab === "sample") return this.sample(c);
    if (this.tab === "details") return this.details(c, d);
    if (this.tab === "lineage") return this.lineageView(c, w);
    if (this.tab === "insights") return this.insights.render(c);
    if (this.tab === "quality") return this.quality(c, d);
    if (this.tab === "health") return v_flex().id("ds-health").flex_1().min_h(0).overflow_y_scrollbar().child(this.health.render(c));
    return v_flex().id("ds-overview").flex_1().min_h(0).overflow_y_scrollbar().pb(12).child(h.facts(c, d)).child(h.schema(c, d));
  }

  sample(c) {
    const g = this.rows;
    const files = g?.last?.files ?? [];
    return v_flex().id("ds-sample").flex_1().min_h(0).px(20).py(8).gap(8)
      .when(files.length > 1, (e) => e.child(h_flex().id("ds-files").flex_none().h(26).gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
        .child(div().flex_none().text_color(c.grey500).child("File"))
        .children(files.slice(0, 12).map((f, i) => {
          const on = (this.file ?? files[0].file) === f.file;
          return div().id(`ds-file-${i}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
            .border_1().border_color(on ? c.blue : c.grey300).text_color(on ? c.blue : c.grey900)
            .on_click((_e, cx) => { this.file = f.file; this.loadRows(cx); }).child(String(f.file).split("/").pop());
        }))))
      .when(!!this.whereBar(c), (e) => e.child(this.whereBar(c)))
      .when(!g, (e) => e.child(Skeleton(c, 8)))
      .when(!!g, (e) => e.child(v_flex().flex_1().min_h(240).border_1().border_color(c.rule).child(g.render(c, { fill: true }))));
  }

  details(c, d) {
    const p = d.provenance ?? {};
    const latest = (this.host.list ?? []).find((x) => x.name === d.name)?.latest ?? d.version;
    const versions = Array.from({ length: latest }, (_, i) => latest - i);
    const row = (k, v) => v ? h_flex().gap(10).text_size(12.5).child(div().w(110).flex_none().text_color(c.grey500).child(k)).child(div().flex_1().min_w(0).child(v)) : null;
    const source = { sql: "A SQL query", file: "A file", notebook: "A notebook", label: "A label", grading: "Rating", import: "An import" }[p.source] ?? p.source ?? "";
    return v_flex().id("ds-details").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(10).gap(14)
      .child(v_flex().gap(6)
        .child(div().text_size(13).font_medium().child("Versions"))
        .child(h_flex().flex_wrap().gap(6).children(versions.map((v) => div().id(`ver-${v}`).h(24).px(8).flex().items_center().cursor_pointer().font_family(MONO).text_size(12)
          .border_1().border_color(v === d.version ? c.blue : c.grey300).bg(v === d.version ? c.blueLight : c.surface).text_color(v === d.version ? c.blue : c.navy)
          .on_click((_e, cx) => this.host.selectVersion(cx, d.name, v)).child(`v${v}${v === latest ? " latest" : ""}`))))
        .child(div().text_size(11.5).text_color(c.grey500).child("Versions are write-once: a new import is a new version, and old ones stay readable.")))
      .child(v_flex().gap(6)
        .child(div().text_size(13).font_medium().child("Where it came from"))
        .child(row("Source", source))
        .child(row("Connection", p.connection))
        .child(row("File", p.file))
        .child(row("Notebook", p.notebook))
        .child(row("Made by", d.created_by ? `${d.created_by}, ${ago(d.created_at)}` : null))
        .when(!!p.sql, (e) => e.child(v_flex().gap(4)
          .child(h_flex().gap(8).items_center().child(div().text_size(12.5).text_color(c.grey500).child("The SQL that made it"))
            .child(Button(c, "copy-sql", "Copy SQL", (_e, cx) => cx.write_to_clipboard(p.sql), { kind: "quiet", small: true })))
          .child(div().p(10).bg(c.surface2).border_1().border_color(c.rule).text_size(12).font_family(MONO).child(p.sql)))))
      .child(v_flex().gap(6)
        .child(div().text_size(13).font_medium().child(`Files ${fmtInt((d.files ?? []).length)}`))
        .children((d.files ?? []).slice(0, 50).map((f, i) => h_flex().id(`file-${i}`).gap(10).text_size(12)
          .child(div().flex_1().min_w(0).truncate().font_family(MONO).child(f.path))
          .child(div().flex_none().font_family(MONO).text_color(c.grey500).child(fmtBytes(f.bytes))))))
      .when(!!d.holdout_seal, (e) => e.child(row("Test set", "Sealed: scored only once an offline evaluation's plan is fixed.")))
      .when(!!d.judgment_source, (e) => e.child(row("Judgments from", String(d.judgment_source))));
  }

  lineageView(c, w) {
    if (this.graphError) return v_flex().flex_1().p(20).child(div().text_size(12.5).text_color(c.fail).child(this.graphError));
    if (!this.lineage.graph.nodes?.length) return v_flex().flex_1().child(Skeleton(c, 6));
    return v_flex().id("ds-lineage-view").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(10).gap(8)
      .child(div().text_size(12).text_color(c.grey900).child("Where this dataset comes from and what uses it. Double-click a box to center on it."))
      .child(this.lineage.render(c, { width: w ? w - 40 - 2 - 12 : 0 })) // the view's padding, the canvas's border, a scrollbar
      .when(!!this.columnNote, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(this.columnNote)));
  }

  quality(c, d) {
    const p = this.profile;
    if (!p) return v_flex().flex_1().child(Skeleton(c, 6));
    if (p.error) return v_flex().flex_1().p(20).child(div().text_size(12.5).text_color(c.fail).child(p.error));
    if (p.missing) return v_flex().flex_1().p(20).child(div().text_size(12.5).text_color(c.grey900).child("Quality needs a newer Captain backend."));
    const cols = p.columns ?? [];
    const range = (x) => (x.min == null && x.max == null ? "" : String(x.min) === String(x.max) ? String(x.min) : `${x.min ?? ""} to ${x.max ?? ""}`);
    const head = (t, w) => div().w(w).flex_none().px(8).text_right().child(t);
    return v_flex().id("ds-quality").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(10).gap(10)
      .child(h_flex().gap(16).text_size(12.5)
        .when(!!(p.created ?? p.freshness?.updated_at), (e) => e.child(div().child(`This version was made ${ago(p.created ?? p.freshness.updated_at)}`)))
        .when(p.rows != null, (e) => e.child(div().child(`${fmtInt(p.rows)} rows profiled`))))
      // Fits the pane: the column name and the range share what's left, long values cut with an ellipsis.
      .child(div().id("q-x").w_full().child(v_flex().w_full().border_1().border_color(c.rule)
        .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
          .child(div().flex_1().min_w(110).px(10).child("Column"))
          .child(head("Empty", 64)).child(head("Distinct", 76))
          .child(div().flex_1().min_w(80).px(8).child("Range")))
        .children(cols.map((x, i) => h_flex().id(`q-${i}`).h(30).border_b(1).border_color(c.rule).text_size(12).whitespace_nowrap().items_center()
          .child(h_flex().flex_1().min_w(110).px(10).gap(6).child(div().min_w(0).truncate().font_family(MONO).child(x.name))
            .child(div().text_size(11).text_color(c.grey500).child(String(x.type ?? "").toLowerCase())))
          .child(div().w(64).flex_none().px(8).text_right().font_family(MONO).text_color((x.nulls ?? x.null_share ?? 0) > 0.2 ? c.warn : c.navy).child(pct(x.nulls ?? x.null_share)))
          .child(div().w(76).flex_none().px(8).text_right().font_family(MONO).child(x.distinct != null ? fmtInt(x.distinct) : ""))
          // Smallest to largest, one cell: shortened with an ellipsis, in full on hover.
          .child(div().flex_1().min_w(80).px(8).truncate().font_family(MONO).tooltip(range(x)).child(range(x)))))));
  }
}
