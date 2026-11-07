// CAPTAIN: Data and signals, native. The datasets of the workspace with their classes, a data grid that pages
// through any dataset where it lives (GET /api/datasets/{name}/rows: the backend sorts, searches and filters), and
// labels as code with a preview over logs. It replaces the web view of the Captain Search desktop app's Datasets
// screen, in the same look (its index.css tokens).
//
// The mod never holds or sorts a whole dataset: gpui-kit stops a render after 50 ms (docs/captain/RISKS.md). The
// grid keeps a few pages of 200 rows around what is on screen and drops the rest.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar, InputState, Input } from "gpui-base";
import { theme, shared, props as layoutProps } from "herdr";
import { api, retry, details, watchWorkspace, watchScope, watchConnections } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Loading, Skeleton, Button, Badge } from "./kit/ui.js";
import { askAgent, copyPrompt, openForm } from "./kit/agent.js";
import { Notebooks } from "./notebooks.js";
import { Library } from "./library.js";
import { Models } from "./models.js";
import { Tables } from "./tables.js";
import { DatasetPage } from "./dataset-page.js";
import { openOverlay } from "herdr";
import { int } from "./kit/num.js";

/** The import wizard, starting in this folder. */
const openImport = (folder) => openOverlay({ mod: "captain-import", kind: "dialog", width: 900, height: 640, props: { folder } });

const ROW = 26;
const ICON = (n) => `mods/captain-data/icons/${n}.svg`;
const enc = (name) => name.split("/").map(encodeURIComponent).join("/");
const stripAnsi = (s) => String(s ?? "").replace(/\u001b\[[0-9;]*m/g, "");
const numeric = (t) => /int|double|float|decimal|numeric|real|bigint|hugeint|number/i.test(t ?? "");
const fmtInt = (n) => int(n);
const typeIcon = (t) => (numeric(t) ? "number" : /bool/i.test(t) ? "bool" : /date|time/i.test(t) ? "date" : /json|struct|map|list/i.test(t) ? "json" : "text");

function cellText(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "number") return Number.isInteger(v) ? int(v) : String(+v.toFixed(6));
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** A failed call in words for people: the backend's own sentence when it answered, else a plain one. Transport
 *  details (hosts, ports, tool output) never show: they go behind Copy details. */
function say(r, fallback, missing) {
  if (r?.timedOut) return "This took too long.";
  // The services went away or never came up: the kit's own plain sentence ("Captain's services stopped.").
  // Belt and braces: anything that reads like a tool's or transport's output never shows.
  if (r?.failed && r.error && !/curl|127\.0|localhost|\bport\b|https?:|error sending|traceback|engine|daemon/i.test(r.error)) return String(r.error);
  const e = String(r?.error ?? "");
  // A route this backend doesn't have answers FastAPI's bare "Not Found": not a sentence for people.
  // A storage key or path ("backtest-runs/x/v1/MANIFEST.yaml") isn't a sentence either: it goes to Copy details.
  const pathy = /^[\w.@-]+(\/[\w.@-]+)+$/.test(e.trim()) || /\.(ya?ml|json|parquet|csv)$/i.test(e.trim());
  if (r?.status === 404 && missing) return missing;
  if (r?.status && e && !pathy && !/^HTTP \d+$/.test(e) && !/^(Not Found|Method Not Allowed|Internal Server Error)$/i.test(e)) return e;
  if (/time ?out|timed out/i.test(e)) return "This took too long.";
  return fallback;
}

/** A section that couldn't load: one plain sentence, Retry, and Copy details. A skeleton while still starting. */
function trouble(c, id, r, fallback, onRetry) {
  if (r?.starting) return Skeleton(c);
  return v_flex().w_full().p(14).gap(10).bg(c.failLight).border_1().border_color(c.fail)
    .child(div().text_size(13).text_color(c.fail).child(say(r, fallback)))
    .child(h_flex().gap(8)
      .child(Button(c, `${id}-retry`, "Retry", (_e, cx) => { if (!r?.status) retry(); onRetry?.(cx); }, { kind: "primary", small: true }))
      .when(!r?.status, (e) => e.child(Button(c, `${id}-copy`, "Copy details", (_e, cx) => cx.write_to_clipboard(details(r?.error ?? null)), { small: true }))));
}

/** A versioned name as people read it: the name, then a small version chip. */
function named(c, name, version, { size = 12.5, color, mono = true } = {}) {
  return h_flex().flex_1().min_w(0).gap(6).items_center()
    .child(div().min_w(0).truncate().text_size(size).when(mono, (e) => e.font_family(MONO)).text_color(color ?? c.navy).child(name))
    .when(version != null, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10.5).font_family(MONO).text_color(c.grey900).child(`v${version}`)));
}

/** What an agent should do with a dataset, in words it can act on. */
function datasetPrompt(d) {
  return [
    `In Captain, look at the dataset ${d.name}, version ${d.version}${d.description ? ` (${d.description})` : ""}.`,
    `It has ${d.rows ?? "an unknown number of"} rows${(d.classes ?? []).length ? ` and is classed as ${d.classes.join(", ")}` : ""}.`,
    "Describe what it holds, check it for problems (missing or duplicate keys, empty columns, odd values),",
    "and suggest the signals or labels it could feed. Use the cst CLI to read it. Change nothing without asking me.",
  ].join(" ");
}

const LABEL_PROMPT = "In Captain, help me define a label: a rule that turns logged search events into relevance judgments " +
  "(for example, a purchase within 30 minutes of a search counts as relevant). Look at the event datasets in this workspace, " +
  "propose a label that fits them, preview it on a sample, and define it with the cst CLI once I agree.";

export default class CaptainData extends View {
  init(_props, cx) {
    this.tab = "datasets";
    this.width = 1000;
    this.classes = [];
    this.cls = null;
    this.list = null;
    this.listError = null;
    this.sel = null;
    this.detail = null;
    this.labels = null;
    this.labelSel = null;
    this.preview = null;
    this.previewLogs = null;
    this.previewError = null;
    this.nb = new Notebooks();
    this.lib = new Library(this);
    this.lib.load(cx);
    this.models = new Models(this);
    this.tables = new Tables(this);
    this.page = new DatasetPage(this);
    // Layout props (a layout, or the real-window check): {tab: "notebooks", notebook, runAll, sqlTools}.
    const p = layoutProps?.() ?? {};
    if (p.tab === "models" || p.tab === "labels" || p.tab === "tables") this.tab = p.tab;
    if (p.pageTab) this.page.tab = p.pageTab;
    if (p.tablesTab) this.tables.tab = p.tablesTab; // "rows": the first table's rows, once it's picked
    if (p.tablesPick) this.tables.want = p.tablesPick; // a table by name instead of the first
    // {importWith: {conn, sql, name}}: the import wizard, open on that query (a layout, or the real-window check).
    if (p.importWith) cx.timer.after(1500, () => openOverlay({ mod: "captain-import", kind: "dialog", width: 900, height: 640, props: { start: p.importWith } }));
    if (p.tab === "tables") this.tables.load(cx);
    if (p.tab === "models") this.models.load(cx);
    if (p.tab === "notebooks") {
      this.tab = "notebooks";
      this.nb.want = p.notebook ?? null;
      this.nb.runOnOpen = !!p.runAll;
      this.nb.toolsOnOpen = p.sqlTools ?? null; // {panel}: the first SQL cell's tools, open on that panel
      this.nb.load(cx);
    }
    this.load(cx);
    // The notebook: autosave its draft, and keep a running cell's clock moving.
    cx.timer.every(500, (cx) => { if (this.tab === "notebooks") this.nb.tick(cx); this.lib.tick(cx); if (this.models.job) this.models.tick(cx); if (this.tables.jobs.size) this.tables.tick(cx); });
    // Pages the grid asked for while drawing are fetched here, outside render.
    // Until the backend answers, ask again every 3 s: the screen never stays on "starting" by itself.
    cx.timer.every(3000, (cx) => {
      if (this.starting || (this.list === null && this.loadedOnce) || (this.listError && !this.listR?.status)) this.load(cx);
      // The folder tree, until it loads (while the services start, or after a failure that wasn't an answer).
      if (this.lib.tree === null || (this.lib.error && !this.lib.error.status)) this.lib.load(cx);
    });
    // "Open <dataset>" from the palette: captain-go sets shared "captain.open" {section: "data", dataset, tab?, at}.
    // tab (a dataset page tab: health, insights, quality...) opens once that dataset is on the page.
    // Shared values outlive the app: an open from before this screen started (the last session's import) is done.
    const stale = shared("captain.open");
    if (stale?.at && Date.now() - stale.at > 10000) this.opened = stale.at;
    cx.timer.every(500, (cx) => {
      const o = shared("captain.open");
      if (this.wantTab && this.detail?.name === this.wantTab.dataset) { const t = this.wantTab.tab; this.wantTab = null; this.page.open(cx, t); }
      if (o?.section === "data" && o.dataset && o.at !== this.opened) {
        this.opened = o.at;
        this.wantTab = o.tab ? { dataset: o.dataset, tab: o.tab } : null;
        this.tab = "datasets";
        // Just imported, or opened from elsewhere: reload the folders and open them down to it.
        this.lib.revealing = o.dataset;
        this.lib.load(cx);
        if (this.list?.some((d) => d.name === o.dataset) && !o.fresh) this.select(cx, o.dataset);
        else { this.wanted = o.dataset; this.load(cx); }
        cx.notify();
      }
    });
    // The Explorer's pick (a brand, site, engine or project) filters every list: reload what's shown.
    watchScope(cx, (cx) => {
      this.load(cx);
      this.lib.load(cx);
      if (this.models.regs !== null) this.models.load(cx);
      if (this.tables.conns !== null) this.tables.load(cx);
      cx.notify();
    });
    // A database connected or removed anywhere in the app: relist it here.
    watchConnections(cx, (cx) => {
      if (this.tables.conns !== null || this.tab === "tables") this.tables.load(cx);
      this.nb.loadConns(cx);
      cx.notify();
    });
    // Another space, another workspace: start over on its datasets and labels.
    watchWorkspace(cx, (cx) => {
      this.list = null; this.sel = null; this.detail = null; this.labels = null; this.labelSel = null; this.preview = null;
      this.cls = null; this.classes = [];
      this.nb.kernel?.close();
      this.nb = new Notebooks();
      this.lib = new Library(this);
      this.lib.load(cx);
      this.models = new Models(this);
      if (this.tab === "models") this.models.load(cx);
      this.tables = new Tables(this);
      if (this.tab === "tables") this.tables.load(cx);
      if (this.tab === "notebooks") this.nb.load(cx);
      this.load(cx);
      cx.notify();
    });
  }

  // ---- data ---------------------------------------------------------------------------------------------------

  load(cx) {
    cx.spawn(async (cx) => {
      const [cl, ds] = await Promise.all([api("/api/dataset-classes"), api(`/api/datasets${this.cls ? `?class=${encodeURIComponent(this.cls)}` : ""}`)]);
      if (cl.ok) this.classes = cl.data?.classes ?? [];
      if (ds.ok) {
        this.list = ds.data ?? [];
        this.listError = null;
        if (this.wanted && this.list.some((d) => d.name === this.wanted)) {
          this.select(cx, this.wanted);
          this.wanted = null;
        } else if (this.wanted) {
          // The Explorer's pick hides it: say so rather than leave the import nowhere to be seen.
          const pick = shared("captain.scope");
          this.lib.note = { text: `${this.wanted} isn't shown because the Explorer has ${pick?.label ?? "a brand or project"} picked. Clear the Explorer's pick (under Explorer in the sidebar) to see it.`, tone: "muted" };
          this.lib.revealing = null;
          this.wanted = null;
          if (!this.sel || !this.list.some((d) => d.name === this.sel)) { if (this.list[0]) this.select(cx, this.list[0].name); }
        } else if (!this.sel || !this.list.some((d) => d.name === this.sel)) {
          if (this.list[0]) this.select(cx, this.list[0].name);
        }
      } else {
        this.starting = !!ds.starting;
        this.listError = ds.starting ? null : say(ds, "Couldn't load datasets.");
        this.listR = ds;
      }
      if (ds.ok) this.starting = false;
      this.loadedOnce = true;
      cx.notify();
    });
  }

  loadLabels(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/labels");
      this.labels = r.ok ? r.data ?? [] : [];
      this.labelsError = r.ok ? null : say(r, "Couldn't load labels.");
      if (!this.labelSel && this.labels[0]) this.labelSel = this.labels[0].name;
      cx.notify();
    });
  }

  select(cx, name) {
    this.sel = name;
    this.detail = null;
    this.page?.reset();
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${enc(name)}`);
      if (this.sel !== name) return;
      this.detail = r.ok ? r.data : { error: say(r, "Couldn't load this dataset.", "This dataset's record is missing from the workspace.") };
      if (r.ok) this.page.open(cx, this.page.tab);
      cx.notify();
    });
    cx.notify();
  }

  loadPreview(cx, logs) {
    const l = this.labels?.find((x) => x.name === this.labelSel);
    if (!l) return;
    this.previewLogs = logs;
    this.preview = null;
    this.previewError = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/labels/${encodeURIComponent(l.name)}/preview?logs=${encodeURIComponent(logs)}&v=${l.version}&limit=20`, { timeout: 60 });
      this.preview = r.ok ? r.data : null;
      this.previewError = r.ok ? null : stripAnsi(say(r, "Couldn't load the preview."));
      cx.notify();
    });
    cx.notify();
  }

  // ---- pieces -------------------------------------------------------------------------------------------------

  icon(n, size = 14, color) {
    return svg(ICON(n)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  chip(c, label, color, active, onClick, count) {
    return h_flex().id(`chip-${label}`).h(26).px(8).gap(6).items_center().flex_none().cursor_pointer()
      .border_1().border_color(active ? c.blue : c.grey300).bg(active ? c.blueLight : c.surface)
      .text_size(12).text_color(active ? c.blue : c.navy).hover((s) => s.border_color(c.blue))
      .on_click(onClick)
      .when(!!color, (e) => e.child(div().size(8).flex_none().bg(color)))
      .child(div().child(label))
      .when(count !== undefined, (e) => e.child(div().text_size(10.5).font_family(MONO).text_color(c.grey500).child(String(count))));
  }

  tabs(c) {
    const t = (id, label, icon) => h_flex().id(`tab-${id}`).h(28).px(12).gap(6).items_center().cursor_pointer()
      .border_r(1).border_color(c.grey300).text_size(12.5)
      .bg(this.tab === id ? c.navy : c.surface).text_color(this.tab === id ? c.surface : c.grey900)
      .on_click((_e, cx) => { this.tab = id; if (id === "labels" && !this.labels) this.loadLabels(cx); if (id === "notebooks" && this.nb.list === null) this.nb.load(cx); if (id === "models" && this.models.regs === null) this.models.load(cx); if (id === "tables") this.tables.show(cx); if (id === "notebooks" && this.nb.list !== null) this.nb.loadConns(cx); cx.notify(); })
      .child(this.icon(icon, 13)).child(div().child(label));
    return h_flex().flex_none().border_1().border_color(c.grey300)
      .child(t("datasets", "Datasets", "table")).child(t("labels", "Labels", "tag")).child(t("tables", "Tables", "database")).child(t("notebooks", "Notebooks", "folder-open")).child(t("models", "Models", "lineage"));
  }

  header(c) {
    // Wraps in a narrow pane: the buttons go to a second line rather than out of view.
    return h_flex().min_h(52).flex_none().flex_wrap().px(20).py(8).gap(14).items_center().border_b(1).border_color(c.rule)
      .child(div().flex_none().text_size(18).font_semibold().text_color(c.navy).child("Data and signals"))
      .child(this.tabs(c))
      // What the tab holds, only when there's room: in a narrow pane it would push the buttons out of view.
      .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.grey900)
        .when((this.width ?? 0) < 1180, (e) => e.invisible())
        .child(this.tab === "datasets" ? "Catalogs, queries, judgments and logs your experiments read." : this.tab === "notebooks" ? "SQL, Python, notes and charts, saved as versions." : this.tab === "models" ? "Trained models from your registries, and what each was built from." : this.tab === "tables" ? "Your databases' tables, their columns and rows, and how they're queried." : "Rules that turn logged events into judgments."))
      .when(this.tab === "datasets", (e) => e
        .child(Button(c, "new-class", "New class", () => openForm("dataset-class"), { small: true }))
        .child(Button(c, "import-dataset", "Import a dataset", () => openImport(), { kind: "primary", small: true })))
      .when(this.tab === "labels", (e) => e
        .child(Button(c, "label-agent", "Ask an agent to define a label", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, LABEL_PROMPT)), { small: true })))
      .child(div().id("data-refresh").size(28).flex().flex_none().items_center().justify_center().cursor_pointer()
        .border_1().border_color(c.grey300).text_color(c.grey900).hover((s) => s.text_color(c.blue).border_color(c.blue))
        .tooltip("Refresh").on_click((_e, cx) => { if (this.tab === "labels") this.loadLabels(cx); else if (this.tab === "notebooks") this.nb.load(cx); else if (this.tab === "models") this.models.load(cx); else if (this.tab === "tables") this.tables.load(cx); else { this.load(cx); this.lib.load(cx); } })
        .child(this.icon("refresh", 14)));
  }

  /** No datasets in this workspace yet: what they are for, and the ways in. */
  firstDataset(c) {
    return v_flex().flex_1().items_center().justify_center().gap(10).p(24)
      .child(this.icon("table", 22, c.grey500))
      .child(div().text_size(15).font_medium().child("Bring in your data"))
      .child(div().max_w(460).text_center().text_size(12.5).line_height("19px").text_color(c.grey900)
        .child("Experiments read four kinds of data: your catalog, the queries people search, judgments of what is relevant, and logs of what shoppers did. Import a file, save a SQL query, or start from the demo store."))
      .child(h_flex().gap(8)
        .child(Button(c, "first-import", "Import a dataset", () => openImport(), { kind: "primary" }))
        .child(Button(c, "first-demo", "Try the demo store", () => openForm("demo"))));
  }

  /** The plain failure panel (sentence, Retry, Copy details), for the library to show. */
  troubleView(c, id, r, fallback, onRetry) {
    return trouble(c, id, r, fallback, onRetry);
  }

  classColor(name) {
    return this.classes.find((x) => x.name === name)?.color ?? "#64748b";
  }

  datasetList(c, w) {
    const rows = this.list ?? [];
    return v_flex().w(w).h_full().flex_none().border_r(1).border_color(c.rule)
      .child(h_flex().flex_none().flex_wrap().gap(6).p(10).border_b(1).border_color(c.rule)
        .child(this.chip(c, "All", null, !this.cls, (_e, cx) => { this.cls = null; this.load(cx); }))
        .children(this.classes.map((k) => this.chip(c, k.name, k.color, this.cls === k.name, (_e, cx) => { this.cls = this.cls === k.name ? null : k.name; this.load(cx); }, k.datasets))))
      .child(v_flex().id("data-list").flex_1().min_h(0).overflow_y_scrollbar()
        .when(!!this.listError, (e) => e.child(div().p(10).child(trouble(c, "datasets-list", this.listR, this.listError, (cx) => this.load(cx)))))
        .when(this.list !== null && rows.length === 0 && !this.listError, (e) => e.child(div().p(12).text_size(12.5).text_color(c.grey500).child(this.cls ? "No datasets in this class." : "No datasets yet.")))
        .when(this.list === null && !this.listError, (e) => e.child(div().child(Skeleton(c, 8))))
        .children(rows.map((d) => {
          const active = d.name === this.sel;
          return h_flex().id(`ds-${d.name}`).w_full().h(34).flex_none().px(10).gap(8).items_center().cursor_pointer()
            .border_l(2).border_color(active ? c.blue : "#00000000").bg(active ? c.blueLight : c.surface)
            .hover((s) => (active ? s : s.bg(c.surface2)))
            .tooltip(`${d.name}@v${d.latest}${d.description ? `\n${d.description}` : ""}`)
            .on_click((_e, cx) => this.select(cx, d.name))
            .child(this.icon("table", 14, active ? c.blue : c.grey500))
            .child(named(c, d.name, d.latest, { color: active ? c.blue : c.navy }))
            .child(h_flex().flex_none().gap(3).children((d.classes ?? []).slice(0, 3).map((k) => div().size(7).bg(this.classColor(k)).tooltip(k))))
            .child(div().flex_none().w(54).text_right().text_size(11).font_family(MONO).text_color(c.grey500).child(d.rows ? fmtInt(d.rows) : ""));
        })));
  }

  facts(c, d) {
    const f = (k, v) => v === null || v === undefined || v === "" ? null : v_flex().flex_none().gap(2).min_w(90)
      .child(div().text_size(10.5).text_color(c.grey500).child(k.toUpperCase()))
      .child(div().text_size(12.5).text_color(c.navy).truncate().max_w(260).child(String(v)));
    const src = d.provenance?.source === "sql" ? `SQL on ${d.provenance.connection ?? "a connection"}` : d.provenance?.source ? `From ${d.provenance.source}` : null;
    return h_flex().flex_none().flex_wrap().gap(18).px(20).py(10).border_b(1).border_color(c.rule)
      .children([
        f("Rows", d.row_count > 0 ? fmtInt(d.row_count) : null),
        f("Created", d.created_at?.slice(0, 16).replace("T", " ")),
        f("By", d.created_by),
        f("Source", src),
        f("Files", d.external?.length ? `${d.external.length} by reference` : d.files?.length ? String(d.files.length) : null),
        f("Role", d.role ? `usable as ${d.role}` : null),
      ].filter(Boolean));
  }

  schema(c, d) {
    const fields = d.schema_fields?.length ? d.schema_fields : d.sample_schema ?? [];
    if (!fields.length) return div();
    const shown = fields.slice(0, 16);
    return h_flex().flex_none().flex_wrap().gap(6).px(20).py(8).border_b(1).border_color(c.rule)
      .children(shown.map((x) => h_flex().h(22).px(6).gap(5).items_center().border_1().border_color(c.rule).text_size(11.5)
        .child(this.icon(typeIcon(x.type), 11, c.grey500))
        .child(div().font_family(MONO).text_color(c.navy).child(x.name))
        .child(div().font_family(MONO).text_color(c.grey500).child(String(x.type).toLowerCase()))))
      .when(fields.length > shown.length, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`+${fields.length - shown.length} more`)));
  }

  detailPane(c, w) {
    const d = this.detail;
    if (!this.sel) return v_flex().flex_1().p(24).text_size(13).text_color(c.grey500).child("Pick a dataset on the left.");
    if (!d) return v_flex().flex_1().child(Skeleton(c, 8));
    if (d.error) return v_flex().flex_1().p(24).text_size(13).text_color(c.fail).child(d.error);
    return v_flex().flex_1().min_w(0).h_full()
      .child(h_flex().flex_none().flex_wrap().gap(8).px(20).pt(14).pb(8).items_center()
        .child(this.icon("table", 16, c.navy))
        .child(div().flex_none().max_w(420).child(named(c, d.name, d.version, { size: 15 })))
        .children((d.classes ?? []).map((k) => h_flex().h(20).px(6).gap(5).items_center().border_1().border_color(c.grey300).text_size(11)
          .child(div().size(7).bg(this.classColor(k))).child(div().text_color(c.navy).child(k))))
        .when(!!d.external?.length, (e) => e.child(div().text_size(11).text_color(c.grey500).child("by reference"))))
      .when(!!d.description, (e) => e.child(div().flex_none().px(20).pb(8).text_size(12.5).text_color(c.grey900).child(d.description)))
      .child(h_flex().flex_none().gap(8).px(20).pb(8)
        .child(Button(c, "agent-dataset", "Open an agent in this space", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, datasetPrompt(d))), { small: true }))
        .child(Button(c, "prompt-dataset", "Copy prompt", (_e, cx) => copyPrompt(cx, datasetPrompt(d)), { kind: "quiet", small: true })))
      .child(this.page.tabs(c))
      .child(this.page.body(c, w, d));
  }

  /** Open an older (or newer) version of the dataset on the page. */
  selectVersion(cx, name, v) {
    this.sel = name;
    this.detail = null;
    this.page.reset();
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${enc(name)}?v=${v}`);
      if (this.sel !== name) return;
      this.detail = r.ok ? r.data : { error: say(r, "Couldn't load this version.", "That version is missing from the workspace.") };
      if (r.ok) this.page.open(cx, this.page.tab);
      cx.notify();
    });
    cx.notify();
  }

  labelsPane(c, w) {
    const ls = this.labels;
    const l = ls?.find((x) => x.name === this.labelSel) ?? null;
    const events = (this.list ?? []).filter((d) => (d.classes ?? []).some((k) => this.classes.find((x) => x.name === k)?.role === "events"));
    const listW = Math.max(200, Math.min(280, Math.floor(w * 0.26)));
    return h_flex().flex_1().min_h(0)
      .child(v_flex().id("label-list").w(listW).h_full().flex_none().border_r(1).border_color(c.rule).overflow_y_scrollbar()
        .when(ls === null, (e) => e.child(div().child(Skeleton(c, 8))))
        .when(!!this.labelsError, (e) => e.child(div().p(12).text_size(12.5).text_color(c.fail).child(this.labelsError)))
        .when(ls !== null && !ls.length, (e) => e.child(div().p(12).text_size(12.5).text_color(c.grey500).child("No labels yet. A label turns logged events into judgments, such as a click or a purchase counting as relevant. Ask an agent in this space to define one.")))
        .children((ls ?? []).map((x) => {
          const active = x.name === this.labelSel;
          return h_flex().id(`label-${x.name}`).w_full().h(40).flex_none().px(10).gap(8).items_center().cursor_pointer()
            .border_l(2).border_color(active ? c.blue : "#00000000").bg(active ? c.blueLight : c.surface).hover((s) => (active ? s : s.bg(c.surface2)))
            .tooltip(x.description ?? x.name)
            .on_click((_e, cx) => { this.labelSel = x.name; this.preview = null; this.previewError = null; cx.notify(); })
            .child(this.icon("tag", 14, active ? c.blue : c.grey500))
            .child(v_flex().flex_1().min_w(0)
              .child(named(c, x.name, x.version, { color: active ? c.blue : c.navy }))
              .child(div().truncate().text_size(11).text_color(c.grey500).child(`${x.grain} · ${x.dtype}`)));
        })))
      .child(!l ? v_flex().flex_1().p(24).text_size(13).text_color(c.grey500).child(ls?.length ? "Pick a label." : "")
        : v_flex().id("label-detail").flex_1().min_w(0).h_full().overflow_y_scrollbar().px(20).py(14).gap(12)
          .child(h_flex().gap(8).items_center().flex_wrap()
            .child(this.icon("tag", 16, c.navy))
            .child(div().flex_none().max_w(420).child(named(c, l.name, l.version, { size: 15 })))
            .child(div().h(20).px(6).border_1().border_color(c.grey300).text_size(11).text_color(c.navy).flex().items_center().child(l.grain))
            .child(div().text_size(11.5).text_color(c.grey500).child(`versions ${(l.versions ?? [l.version]).map((v) => `v${v}`).join(", ")}`)))
          .when(!!l.description, (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(l.description)))
          .child(v_flex().border_1().border_color(c.rule).bg(c.surface2).p(12).gap(6)
            .child(div().text_size(10.5).text_color(c.grey500).child("SQL, PER " + String(l.grain).toUpperCase()))
            .child(div().text_size(12.5).font_family(MONO).text_color(c.navy).child(l.sql))
            .when(!!l.where, (e) => e.child(div().text_size(12).font_family(MONO).text_color(c.grey900).child(`where ${l.where}`))))
          .child(v_flex().gap(8)
            .child(div().text_size(13).font_medium().text_color(c.navy).child("Preview over logs"))
            .child(h_flex().flex_wrap().gap(6)
              .when(!events.length, (e) => e.child(div().text_size(12).text_color(c.grey500).child("No events datasets in this workspace.")))
              .children(events.slice(0, 12).map((d) => this.chip(c, `${d.name}@v${d.latest}`, null, this.previewLogs === `${d.name}@v${d.latest}`, (_e, cx) => this.loadPreview(cx, `${d.name}@v${d.latest}`)))))
            .when(!!this.previewError, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.previewError)))
            .when(!!this.preview, (e) => e.child(this.previewTable(c, this.preview)))));
  }

  previewTable(c, p) {
    return v_flex().gap(6)
      .child(h_flex().gap(16).text_size(12.5).text_color(c.navy)
        .child(div().child(`${fmtInt(p.groups)} ${p.grain} groups`))
        .child(div().child(`mean ${p.mean === null ? "none" : Number(p.mean).toFixed(4)}`))
        .child(div().text_color(c.grey500).child(`${fmtInt(p.non_null)} with a value`)))
      .child(v_flex().border_1().border_color(c.rule)
        .child(h_flex().h(26).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium()
          .children(p.columns.map((x) => div().flex_1().min_w(0).px(8).truncate().text_color(c.navy).child(x))))
        .children(p.rows.slice(0, 20).map((r, i) => h_flex().id(`pv-${i}`).h(24).border_b(1).border_color(c.rule).text_size(12)
          .children(r.map((v) => div().flex_1().min_w(0).px(8).truncate().text_color(c.navy).child(cellText(v)))))));
  }

  render() {
    const c = palette(isDark(theme()));
    const w = this.width;
    const listW = this.tab === "datasets" ? Math.max(300, Math.min(460, Math.floor(w * 0.42))) : Math.max(220, Math.min(320, Math.floor(w * 0.24)));
    // Never blank: the header always shows, and the body says what is happening.
    const body = this.starting
      ? v_flex().flex_1().items_center().justify_center().child(Loading(c, "", true))
      : this.tab === "notebooks"
        ? this.nb.render(c, w)
      : this.tab === "models"
        ? this.models.render(c, w)
      : this.tab === "tables"
        ? this.tables.render(c, w)
      : !this.loadedOnce && this.list === null
        ? v_flex().flex_1().items_center().justify_center().child(Loading(c, "", true))
      : this.listError && this.list === null
        ? v_flex().flex_1().p(24).child(trouble(c, "datasets", this.listR, "Couldn't load datasets.", (cx) => this.load(cx)))
        : this.tab === "datasets" && Array.isArray(this.list) && this.list.length === 0 && !this.cls && this.lib.tree && !(this.lib.tree.folders ?? []).length
          ? this.lib.empty(c)
        : this.tab === "datasets"
          ? h_flex().flex_1().min_h(0).child(this.lib.render(c, listW)).child(this.detailPane(c, w - listW))
          : this.labelsPane(c, w);
    return v_flex().id("root").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      .on_size_change((size, cx) => { this.width = size.width; cx.notify(); })
      .child(this.header(c))
      .child(body);
  }
}
