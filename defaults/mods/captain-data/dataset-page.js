// CAPTAIN: one page for a dataset, in the shape of Databricks Unity Catalog's table page. Tabs:
// - Overview: description, who made it and when, rows, role, classes and tags, the schema.
// - Sample data: every row, a page at a time, in the kit grid every screen uses (GET /api/datasets/{name}/rows:
//   search, WHERE and sort on the backend), so it reads the same as the table it came from.
// - Details: every version (open any), its files, and its provenance: the source, connection, and the SQL that made it.
// - Lineage: a drawn graph upstream and downstream (GET /api/lineage/graph when the backend has it, else /api/lineage,
//   cut to the chosen depth here), with column-level lineage for a picked column (GET /api/lineage/columns).
// - Quality: per column, the share of empty values, distinct count, min and max, plus freshness (GET /api/profile).
//   Hidden while the backend has no profile route.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { api, query } from "./kit/cst.js";
import { ResultGrid } from "./kit/grid.js";
import { MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { LineageGraph } from "./kit/lineage.js";
import { int, num } from "./kit/num.js";
// The Health tab (a queries or judgments set): app-shell's, self-contained in the kit.
import { EvalHealth } from "./kit/evals.js";

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

export const TABS = [["overview", "Overview"], ["sample", "Sample data"], ["details", "Details"], ["lineage", "Lineage"], ["quality", "Quality"]];

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
    this.health = new EvalHealth(); // the Health tab (kit/evals.js)
  }

  /** A new dataset (or version) on the page: lineage and quality load again when their tab opens. */
  reset() {
    this.graphFor = null;
    this.profileFor = null;
    this.profile = null;
    this.rows = null;
    this.rowsFor = null;
    this.file = null;
  }

  open(cx, tab) {
    this.tab = tab;
    if (tab === "lineage") this.loadLineage(cx);
    if (tab === "quality") this.loadProfile(cx);
    if (tab === "sample") this.loadRows(cx);
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
      this.lineage.set({ nodes, edges: r.data.edges ?? [] }, id);
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
    this.rows = new ResultGrid({ id: "ds-rows", name: d.name, searchable: true, load: (q) => api(`${path}${query({ ...q, ...extra })}`, { timeout: 60 }) });
    this.rows.fetch(cx);
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
    return h_flex().flex_none().gap(4).px(20).pb(10).children(tabs.map(([id, label]) =>
      div().id(`dtab-${id}`).h(26).px(10).flex().items_center().cursor_pointer().text_size(12)
        .border_1().border_color(this.tab === id ? c.navy : c.grey300).bg(this.tab === id ? c.navy : c.surface).text_color(this.tab === id ? c.surface : c.grey900)
        .on_click((_e, cx) => this.open(cx, id)).child(label)));
  }

  body(c, w, d) {
    const h = this.host;
    if (this.tab === "sample") return this.sample(c);
    if (this.tab === "details") return this.details(c, d);
    if (this.tab === "lineage") return this.lineageView(c);
    if (this.tab === "quality") return this.quality(c, d);
    if (this.tab === "health") return v_flex().id("ds-health").flex_1().min_h(0).overflow_y_scrollbar().child(this.health.render(c));
    return v_flex().id("ds-overview").flex_1().min_h(0).overflow_y_scrollbar().pb(12).child(h.facts(c, d)).child(h.schema(c, d));
  }

  sample(c) {
    const g = this.rows;
    const files = g?.last?.files ?? [];
    return v_flex().id("ds-sample").flex_1().min_h(0).px(20).py(8).gap(8)
      .when(files.length > 1, (e) => e.child(h_flex().id("ds-files").gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
        .child(div().flex_none().text_color(c.grey500).child("File"))
        .children(files.slice(0, 12).map((f, i) => {
          const on = (this.file ?? files[0].file) === f.file;
          return div().id(`ds-file-${i}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
            .border_1().border_color(on ? c.blue : c.grey300).text_color(on ? c.blue : c.grey900)
            .on_click((_e, cx) => { this.file = f.file; this.loadRows(cx); }).child(String(f.file).split("/").pop());
        }))))
      .when(!g, (e) => e.child(Skeleton(c, 8)))
      .when(!!g, (e) => e.child(v_flex().flex_1().min_h(0).border_1().border_color(c.rule).child(g.render(c, { fill: true }))));
  }

  details(c, d) {
    const p = d.provenance ?? {};
    const latest = (this.host.list ?? []).find((x) => x.name === d.name)?.latest ?? d.version;
    const versions = Array.from({ length: latest }, (_, i) => latest - i);
    const row = (k, v) => v ? h_flex().gap(10).text_size(12.5).child(div().w(110).flex_none().text_color(c.grey500).child(k)).child(div().flex_1().min_w(0).child(v)) : null;
    const source = { sql: "A SQL query", file: "A file", notebook: "A notebook", label: "A label", grading: "Grading", import: "An import" }[p.source] ?? p.source ?? "";
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
      .when(!!d.holdout_seal, (e) => e.child(row("Holdout", "Sealed: scored only once a backtest's plan is fixed.")))
      .when(!!d.judgment_source, (e) => e.child(row("Judgments from", String(d.judgment_source))));
  }

  lineageView(c) {
    if (this.graphError) return v_flex().flex_1().p(20).child(div().text_size(12.5).text_color(c.fail).child(this.graphError));
    if (!this.lineage.graph.nodes?.length) return v_flex().flex_1().child(Skeleton(c, 6));
    return v_flex().id("ds-lineage-view").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(10).gap(8)
      .child(div().text_size(12).text_color(c.grey900).child("Where this dataset comes from and what uses it. Click a box to center on it."))
      .child(this.lineage.render(c))
      .when(!!this.columnNote, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(this.columnNote)));
  }

  quality(c, d) {
    const p = this.profile;
    if (!p) return v_flex().flex_1().child(Skeleton(c, 6));
    if (p.error) return v_flex().flex_1().p(20).child(div().text_size(12.5).text_color(c.fail).child(p.error));
    if (p.missing) return v_flex().flex_1().p(20).child(div().text_size(12.5).text_color(c.grey900).child("Quality needs a newer Captain backend."));
    const cols = p.columns ?? [];
    const head = (t, w) => div().w(w).flex_none().px(8).text_right().child(t);
    return v_flex().id("ds-quality").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(10).gap(10)
      .child(h_flex().gap(16).text_size(12.5)
        .when(!!(p.created ?? p.freshness?.updated_at), (e) => e.child(div().child(`This version was made ${ago(p.created ?? p.freshness.updated_at)}`)))
        .when(p.rows != null, (e) => e.child(div().child(`${fmtInt(p.rows)} rows profiled`))))
      .child(div().id("q-x").w_full().overflow_x_scrollbar().child(v_flex().min_w(620).border_1().border_color(c.rule)
        .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
          .child(div().flex_1().min_w(140).px(10).child("Column"))
          .child(head("Empty", 80)).child(head("Distinct", 90)).child(head("Min", 120)).child(head("Max", 120)))
        .children(cols.map((x, i) => h_flex().id(`q-${i}`).h(30).border_b(1).border_color(c.rule).text_size(12).whitespace_nowrap().items_center()
          .child(h_flex().flex_1().min_w(140).px(10).gap(6).child(div().min_w(0).truncate().font_family(MONO).child(x.name))
            .child(div().text_size(11).text_color(c.grey500).child(String(x.type ?? "").toLowerCase())))
          .child(div().w(80).flex_none().px(8).text_right().font_family(MONO).text_color((x.nulls ?? x.null_share ?? 0) > 0.2 ? c.warn : c.navy).child(pct(x.nulls ?? x.null_share)))
          .child(div().w(90).flex_none().px(8).text_right().font_family(MONO).child(x.distinct != null ? fmtInt(x.distinct) : ""))
          .child(div().w(120).flex_none().px(8).text_right().truncate().font_family(MONO).child(x.min != null ? String(x.min) : ""))
          .child(div().w(120).flex_none().px(8).text_right().truncate().font_family(MONO).child(x.max != null ? String(x.max) : ""))))));
  }
}
