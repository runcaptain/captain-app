// CAPTAIN: Data, native. Two submenus and no more (Edgar, 2026-11-12):
// - Tables: every table of the workspace in one list (catalog.js): the datasets Captain keeps versions of and each
//   connected database's tables. A row opens that table's page in place: a dataset's (dataset-page.js: Rows, About,
//   and a "Where this came from" button) or a database table's (tables.js: Rows, About).
// - Sources: where the data and the models come from: each database connection, with the button that reads its
//   tables, and each model registry (models.js).
// Labels left the screen (`cst labels` still defines and previews them). Notebooks is its own view.
//
// The mod never holds or sorts a whole dataset: gpui-kit stops a render after 50 ms (docs/captain/RISKS.md). The
// grid keeps a few pages of 200 rows around what is on screen and drops the rest.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar, InputState, Input } from "gpui-base";
import { theme, shared, props as layoutProps } from "herdr";
import { api, retry, details, watchWorkspace, watchScope, watchConnections, keepFresh } from "./kit/cst.js";
import { watchLive } from "./kit/live.js";
import { watchView } from "./kit/view.js";
import { SendToAgent } from "./kit/context.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Loading, Skeleton, Button, Submenus } from "./kit/ui.js";
import { askAgent, openForm } from "./kit/agent.js";
import { Library } from "./library.js";
import { Models } from "./models.js";
import { Tables } from "./tables.js";
import { Catalog } from "./catalog.js";
import { DatasetPage } from "./dataset-page.js";
import { openOverlay, run, setShared } from "herdr";
import { int } from "./kit/num.js";

/** The import wizard, starting in this folder. */
const openImport = (folder) => openOverlay({ mod: "captain-import", kind: "dialog", width: 900, height: 640, props: { folder } });

const ROW = 26;
const ICON = (n) => `mods/captain-data/icons/${n}.svg`;
const enc = (name) => name.split("/").map(encodeURIComponent).join("/");
const numeric = (t) => /int|double|float|decimal|numeric|real|bigint|hugeint|number/i.test(t ?? "");
const fmtInt = (n) => int(n);
const typeIcon = (t) => (numeric(t) ? "number" : /bool/i.test(t) ? "bool" : /date|time/i.test(t) ? "date" : /json|struct|map|list/i.test(t) ? "json" : "text");

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

/** The submenus, and the earlier tab names that still open one (a layout, `cst app open`, an older link). */
const SUBMENUS = [{ id: "tables", label: "Tables" }, { id: "sources", label: "Sources" }];
const EARLIER_TAB = { datasets: "tables", tables: "tables", models: "sources", sources: "sources", labels: "tables" };

export default class CaptainData extends View {
  init(_props, cx) {
    this.tab = "tables"; // "tables" | "sources"
    this.width = 1000;
    this.classes = [];
    this.cls = null;
    this.list = null;
    this.listError = null;
    this.sel = null; // the dataset on the page
    this.detail = null;
    this.shown = null; // what the Tables submenu shows over the list: null (the list), "dataset" or "table"
    this.lib = new Library(this);
    this.lib.load(cx);
    this.models = new Models(this);
    this.tables = new Tables(this);
    this.catalog = new Catalog(this);
    this.catalog.load(cx);
    this.page = new DatasetPage(this);
    // Layout props (a layout, or the real-window check): {tab, dataset, pageTab, tablesPick, tablesTab, ...}.
    const p = layoutProps?.() ?? {};
    if (p.tab) this.tab = EARLIER_TAB[p.tab] ?? "tables";
    if (p.pageTab) this.page.open(cx, p.pageTab);
    if (Number.isInteger(p.openRow)) this.page.openRow = p.openRow; // a sample row to open, for screenshots
    if (p.dataset) this.wanted = String(p.dataset); // a dataset to open on its page
    if (p.find) this.catalog.find.set_value(String(p.find)); // a search to start with
    if (Array.isArray(p.openGroups)) for (const g of p.openGroups) this.catalog.open.add(String(g)); // groups to show open (the real-window check)
    if (p.tablesTab) this.tables.tab = p.tablesTab === "rows" ? "rows" : "about";
    if (p.tablesPick) this.tables.want = p.tablesPick; // a database table to open on its page, by name
    // {importWith: {conn, sql, name}}: the import wizard, open on that query (a layout, or the real-window check).
    if (p.importWith) cx.timer.after(1500, () => openOverlay({ mod: "captain-import", kind: "dialog", width: 900, height: 640, props: { start: p.importWith } }));
    this.tables.load(cx);
    if (this.tab === "sources") this.models.load(cx);
    if (p.registryAttach) { this.tab = "sources"; this.models.load(cx); this.models.toggleAttach(cx); } // the attach form, open on the bucket list
    this.load(cx);
    // The search box: asked once the person has stopped typing for a moment. The window's own state, not data.
    cx.timer.every(500, (cx) => this.catalog.tick(cx));
    // Work this screen started (an upload, a scan of a database, a bucket attached): each job says so itself on the
    // live channel, and is read once each time it does. Nothing asks after a job on a clock.
    watchLive(cx, ["job:"], (cx, why, topics) => {
      const mine = (id) => why !== "changed" || topics.includes(`job:${id}`);
      if (this.lib.job && mine(this.lib.job.id)) this.lib.tick(cx);
      if (this.models.job && mine(this.models.job.id)) this.models.tick(cx);
      if ([...this.tables.jobs.values()].some((j) => mine(j.id))) this.tables.tick(cx);
    });
    // Pages the grid asked for while drawing are fetched here, outside render.
    // Until the backend answers, ask again every 3 s: the screen never stays on "starting" by itself.
    cx.timer.every(3000, (cx) => {
      if (this.starting || (this.list === null && this.loadedOnce) || (this.listError && !this.listR?.status)) this.load(cx);
      // The one list, until it loads (while the services start, or after a failure that wasn't an answer).
      if (this.catalog.rows === null || (this.catalog.error && !this.catalog.error.status)) this.catalog.load(cx);
    });
    // The lists stay current by themselves: the moment a table, a source or a dataset changes (the live channel),
    // at once when Data comes back into view, and at once when a form changes something.
    keepFresh(cx, "data", (cx) => this.refreshLists(cx), ["data", "table:"]);
    // "Open <dataset>" from the palette: captain-go sets shared "captain.open" {section: "data", dataset, tab?, at}.
    // tab (a dataset page tab: health, insights, quality...) opens once that dataset is on the page.
    // Shared values outlive the app: an open from before this screen started (the last session's import) is done.
    const stale = shared("captain.open");
    if (stale?.at && Date.now() - stale.at > 10000) this.opened = stale.at;
    cx.timer.every(500, (cx) => {
      const o = shared("captain.open");
      if (this.wantTab && this.detail?.name === this.wantTab.dataset) { const t = this.wantTab.tab; this.wantTab = null; this.page.open(cx, t); }
      // The sidebar's entry: Data's home, the tables' groups as the person left them.
      if (o?.section === "data" && o.home && o.at !== this.opened) {
        this.opened = o.at;
        this.wanted = null; this.wantTab = null;
        this.shown = null;
        this.showTab(cx, "tables");
      }
      // {section: "data", view: tables | sources (or an earlier tab's name)}: that submenu of Data.
      if (o?.section === "data" && o.view && !o.dataset && o.at !== this.opened) {
        this.opened = o.at;
        this.showTab(cx, EARLIER_TAB[o.view] ?? "tables");
      }
      if (o?.section === "data" && o.dataset && o.at !== this.opened) {
        this.opened = o.at;
        this.wantTab = o.tab ? { dataset: o.dataset, tab: o.tab } : null;
        // Just imported, or opened from elsewhere: list it, and open its page.
        this.catalog.load(cx);
        this.lib.load(cx);
        if (this.list?.some((d) => d.name === o.dataset) && !o.fresh) this.openDataset(cx, o.dataset);
        else { this.wanted = o.dataset; this.load(cx); }
        cx.notify();
      }
    });
    // What this screen shows, for agents (cst app context): the tab, the dataset open and its tab.
    watchView(cx, "data", () => ({ tab: this.tab, dataset: this.shown === "dataset" ? this.sel ?? null : null,
      version: this.shown === "dataset" ? this.detail?.version ?? null : null, dataset_tab: this.shown === "dataset" && this.sel ? this.page?.tab ?? null : null,
      table: this.shown === "table" && this.tables.sel ? `${this.tables.sel.conn}.${this.tables.sel.name}` : null }));
    // The Explorer's pick (a brand, site, engine or project) filters every list: reload what's shown.
    watchScope(cx, (cx) => {
      this.load(cx);
      this.lib.load(cx);
      this.catalog.load(cx);
      if (this.models.regs !== null) this.models.load(cx);
      this.tables.load(cx);
      cx.notify();
    });
    // A database connected or removed anywhere in the app: relist it here.
    watchConnections(cx, (cx) => { this.tables.load(cx); this.catalog.load(cx); cx.notify(); });
    // Another space, another workspace: start over on its tables.
    watchWorkspace(cx, (cx) => {
      this.list = null; this.sel = null; this.detail = null; this.shown = null;
      this.cls = null; this.classes = [];
      this.lib = new Library(this);
      this.lib.load(cx);
      this.models = new Models(this);
      if (this.tab === "sources") this.models.load(cx);
      this.tables = new Tables(this);
      this.tables.load(cx);
      this.catalog = new Catalog(this);
      this.catalog.load(cx);
      this.load(cx);
      cx.notify();
    });
  }

  // ---- going places --------------------------------------------------------------------------------------------

  showTab(cx, id) {
    this.tab = id;
    this.graph = null;
    if (id === "sources") { this.tables.show(cx); if (this.models.regs === null) this.models.load(cx); }
    else this.catalog.load(cx);
    cx.notify();
  }

  /** Reload what is listed, quietly: the groups stay as they are, and so does the page of each. */
  refreshLists(cx) {
    if (this.graph || this.catalog.rows === null) return;
    // A page is open: only its facts. The rows on the page, a filter being typed and the place in the grid are
    // never touched.
    if (this.shown) {
      if (this.shown === "dataset") this.refreshDatasetFacts(cx);
      else this.tables.refreshFacts(cx);
      return;
    }
    if (this.tab === "sources") { this.tables.load(cx); if (this.models.regs !== null && !this.models.job) this.models.load(cx); }
    else this.catalog.refresh(cx);
  }

  /** The open dataset's facts: how many versions Captain keeps and which is the latest (the list), and its record
   *  when it is still the version on the page (row count, description, health's inputs). Nothing else is asked. */
  refreshDatasetFacts(cx) {
    const name = this.sel, was = this.detail;
    if (!name || !was || was.error) return;
    cx.spawn(async (cx) => {
      const [ds, one] = await Promise.all([api(`/api/datasets${this.cls ? `?class=${encodeURIComponent(this.cls)}` : ""}`), api(`/api/datasets/${enc(name)}?v=${encodeURIComponent(was.version)}`)]);
      if (this.sel !== name || this.detail !== was) return; // the person moved on while it was asked
      let changed = false;
      if (ds.ok && Array.isArray(ds.data) && ds.data.some((d) => d.name === name) && JSON.stringify(ds.data) !== JSON.stringify(this.list)) { this.list = ds.data; changed = true; }
      if (one.ok && one.data?.version === was.version) {
        for (const k of ["row_count", "description", "truncated", "tags", "classes", "role"]) {
          if (JSON.stringify(one.data[k]) !== JSON.stringify(was[k])) { was[k] = one.data[k]; changed = true; }
        }
      }
      if (changed) cx.notify();
    });
  }

  /** A dataset's page, over the list. */
  openDataset(cx, name) { this.select(cx, name); }

  /** A database table's page, over the list. */
  openTable(cx, conn, schema, name) {
    this.tab = "tables";
    this.shown = "table";
    this.tables.pick(cx, conn, schema, name);
  }

  /** Back to the one list. */
  backToList(cx) {
    this.shown = null;
    this.catalog.load(cx);
    cx.notify();
  }

  /** Sources, from the list's "its tables haven't been read yet" line. */
  showSources(cx) { this.showTab(cx, "sources"); }

  /** Tables, with that database's group open: "See its tables" on a source's card. */
  showTablesOf(cx, conn) {
    this.shown = null;
    this.catalog.find.set_value("");
    this.catalog.reveal(`db:${conn}`);
    this.showTab(cx, "tables");
  }

  /** A box of the lineage graph was clicked: open it where it has a page. Answers whether it did. A table Captain
   *  keeps opens here, a database's table opens here, an evaluation opens in Runs. A model version, a model and a
   *  deployment have no page in Data: the graph shows their details instead. */
  openNode(cx, node) {
    const id = String(node?.id ?? "");
    this.graph = null;
    if (node.kind === "dataset") {
      const m = /^dataset:(.*?)(?:@v(\d+))?$/.exec(id);
      if (!m) return false;
      if (m[2]) { this.tab = "tables"; this.shown = "dataset"; this.page.tab = "sample"; this.selectVersion(cx, m[1], Number(m[2])); }
      else { this.page.tab = "sample"; this.select(cx, m[1]); }
      return true;
    }
    if (node.kind === "table") {
      // lineage names it table:<connection>/<schema.table>, lower case; the cached tables have the real names.
      const m = /^table:([^/]+)\/(.+)$/.exec(id);
      const hit = m ? (this.catalog.rows ?? []).find((r) => r.kind === "table" && r.connection === m[1] && `${String(r.schema).replace(/"/g, "")}.${r.name}`.toLowerCase().endsWith(m[2])) : null;
      if (!hit) return false;
      this.tables.tab = "rows";
      this.openTable(cx, hit.connection, hit.schema, hit.name);
      return true;
    }
    if (node.kind === "backtest" || node.kind === "run") {
      const m = /^(?:backtest|run|backtest-run):(.*?)(?:@v(\d+))?$/.exec(id);
      if (!m) return false;
      setShared("captain.open", { section: "runs", backtest: m[1], ...(m[2] ? { version: Number(m[2]) } : {}), at: Date.now() });
      run("layout.select", { stack: "sections", child: "runs" });
      return true;
    }
    return false;
  }

  /** "Where this came from" for something that has no page in Data (a model, from Sources): the graph, over the
   *  screen, with a crumb back. */
  showGraph(cx, id, title, back) {
    this.graph = { id, title, back: back ?? this.tab };
    this.page.graphFor = null;
    this.page.graphError = null;
    this.page.lineage.set({ nodes: [], edges: [] }, null);
    this.page.loadLineage(cx, id);
    cx.notify();
  }

  importDataset(folder) { openImport(folder); }

  // ---- data ---------------------------------------------------------------------------------------------------

  load(cx) {
    cx.spawn(async (cx) => {
      const [cl, ds] = await Promise.all([api("/api/dataset-classes"), api(`/api/datasets${this.cls ? `?class=${encodeURIComponent(this.cls)}` : ""}`)]);
      if (cl.ok) this.classes = cl.data?.classes ?? [];
      if (ds.ok) {
        this.list = ds.data ?? [];
        this.listError = null;
        if (this.wanted && this.list.some((d) => d.name === this.wanted)) {
          this.openDataset(cx, this.wanted);
          this.wanted = null;
        } else if (this.wanted) {
          // The Explorer's pick hides it: say so rather than leave the import nowhere to be seen.
          const pick = shared("captain.scope");
          this.lib.note = { text: `${this.wanted} isn't shown because the Explorer has ${pick?.label ?? "a brand or project"} picked. Clear the Explorer's pick (under Explorer in the sidebar) to see it.`, tone: "muted" };
          this.wanted = null;
        }
        // The dataset on the page is gone (another scope, or removed): back to the list.
        if (this.shown === "dataset" && this.sel && !this.list.some((d) => d.name === this.sel)) { this.sel = null; this.detail = null; this.shown = null; }
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

  /** Open a dataset's page, over the list. */
  select(cx, name) {
    this.tab = "tables";
    this.shown = "dataset";
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

  header(c) {
    // Wraps in a narrow pane: the buttons go to a second line rather than out of view.
    return h_flex().min_h(52).flex_none().flex_wrap().px(20).py(8).gap(14).items_center().border_b(1).border_color(c.rule)
      .child(div().flex_none().text_size(18).font_semibold().text_color(c.navy).child("Data"))
      .child(Submenus(c, SUBMENUS, this.tab, (id, cx) => this.showTab(cx, id)))
      .child(div().flex_1())
      // One filled button: bringing data in. On a table's page the page has its own actions.
      .when(this.tab === "tables" && !this.shown, (e) => e
        .child(Button(c, "import-dataset", "Add a table", () => openImport(), { kind: "primary", small: true })))
      .when(this.tab === "sources", (e) => e
        .child(Button(c, "tables-connect", "Connect a database", () => openForm("database"), { kind: "primary", small: true })))
      .child(div().id("data-refresh").size(28).flex().flex_none().items_center().justify_center().cursor_pointer()
        .border_1().border_color(c.grey300).text_color(c.grey900).hover((s) => s.text_color(c.blue).border_color(c.blue))
        .tooltip("Refresh").on_click((_e, cx) => { if (this.tab === "sources") { this.tables.load(cx); this.models.load(cx); } else { this.load(cx); this.lib.load(cx); this.catalog.load(cx); } })
        .child(this.icon("refresh", 14)));
  }

  /** The plain failure panel (sentence, Retry, Copy details), for the library to show. */
  troubleView(c, id, r, fallback, onRetry) {
    return trouble(c, id, r, fallback, onRetry);
  }

  classColor(name) {
    return this.classes.find((x) => x.name === name)?.color ?? "#64748b";
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

  /** The crumb over a table's page: back to the one list. */
  crumb(c, name) {
    return h_flex().id("data-crumb").flex_none().h(34).px(20).gap(6).items_center().border_b(1).border_color(c.rule).text_size(12.5)
      .child(div().id("crumb-tables").text_color(c.blue).cursor_pointer().hover((s) => s.underline()).on_click((_e, cx) => this.backToList(cx)).child("Tables"))
      .child(this.icon("chevron-right", 11, c.grey500))
      .child(div().min_w(0).truncate().text_color(c.navy).child(name ?? ""));
  }

  detailPane(c, w) {
    const d = this.detail;
    if (!d) return v_flex().flex_1().child(Skeleton(c, 8));
    if (d.error) return v_flex().flex_1().p(24).text_size(13).text_color(c.fail).child(d.error);
    const latest = (this.list ?? []).find((x) => x.name === d.name)?.latest ?? d.version;
    return v_flex().id("dataset-page").flex_1().min_w(0).min_h(0)
      // A file dropped on its page lands in its folder: the same name there is a new version of it.
      .on_drop((e, cx) => this.lib.importPaths(cx, e.paths ?? [], this.page.folder()))
      .when(!!this.lib.job, (e) => e.child(this.lib.progress(c)))
      .when(!!this.lib.summary, (e) => e.child(this.lib.summaryPanel(c)))
      .child(h_flex().flex_none().flex_wrap().gap(8).px(20).pt(14).pb(6).items_center()
        .child(this.icon("table", 16, c.navy))
        .child(div().flex_none().max_w(420).child(named(c, d.name, d.version, { size: 15, mono: false })))
        .children((d.classes ?? []).map((k) => h_flex().h(20).px(6).gap(5).items_center().border_1().border_color(c.grey300).text_size(11)
          .child(div().size(7).bg(this.classColor(k))).child(div().text_color(c.navy).child(k))))
        .child(div().flex_1())
        .child(SendToAgent(c, "send-dataset", { ref: `dataset:${d.name}@v${d.version}`, kind: "dataset", label: `${d.name}@v${d.version}` }))
        .child(Button(c, "agent-dataset", "Ask an agent about it", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, datasetPrompt(d))), { small: true })))
      // One line saying what it is: a table Captain keeps versions of, and which one this is.
      .child(div().id("ds-what").flex_none().px(20).pb(8).text_size(12).text_color(c.grey500)
        .child(`A table Captain keeps versions of. This is version ${d.version} of ${latest}${d.external?.length ? ", kept by reference" : ""}.${d.description ? ` ${d.description}` : ""}`))
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

  /** Sources: the databases Captain reads tables from, then the registries it reads models from. */
  sources(c, w) {
    return v_flex().id("sources").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(14).gap(18)
      .child(v_flex().gap(8)
        .child(div().text_size(14).font_semibold().child("Databases"))
        .child(this.tables.cards(c)))
      .child(this.models.render(c, w));
  }

  render() {
    const c = palette(isDark(theme()));
    const w = this.width;
    const none = Array.isArray(this.catalog.rows) && !this.catalog.rows.length && !this.catalog.find.value().trim() && !this.catalog.connections.length;
    // Never blank: the header always shows, and the body says what is happening.
    const body = this.starting || this.catalog.starting
      ? v_flex().flex_1().items_center().justify_center().child(Loading(c, "", true))
      // A model's "Where this came from": the graph over the screen, with a crumb back to where it was asked.
      : this.graph
        ? v_flex().id("data-graph").flex_1().min_h(0)
          .child(h_flex().id("data-crumb").flex_none().h(34).px(20).gap(6).items_center().border_b(1).border_color(c.rule).text_size(12.5)
            .child(div().id("crumb-back").text_color(c.blue).cursor_pointer().hover((s) => s.underline()).on_click((_e, cx) => { this.graph = null; cx.notify(); }).child(this.graph.back === "sources" ? "Sources" : "Tables"))
            .child(this.icon("chevron-right", 11, c.grey500))
            .child(div().min_w(0).truncate().text_color(c.navy).child(`Where ${this.graph.title} came from`)))
          .child(this.page.lineageView(c, w, this.height ? this.height - 110 : 0))
      : this.tab === "sources"
        ? this.sources(c, w)
      : this.shown === "dataset"
        ? v_flex().flex_1().min_h(0).child(this.crumb(c, this.sel)).child(this.detailPane(c, w))
      : this.shown === "table"
        ? v_flex().flex_1().min_h(0).child(this.crumb(c, this.tables.sel?.name)).child(this.tables.body(c, w))
      : none
        ? this.lib.empty(c)
        : this.catalog.render(c, w);
    return v_flex().id("root").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      .on_size_change((size, cx) => { this.width = size.width; this.height = size.height; cx.notify(); })
      .child(this.header(c))
      .when(!!this.lib.note && this.tab === "tables", (e) => e.child(h_flex().id("data-note").flex_none().px(20).py(6).gap(8).items_start().border_b(1).border_color(c.rule).text_size(12)
        .text_color(this.lib.note.tone === "fail" ? c.fail : this.lib.note.tone === "good" ? c.good : c.grey900)
        .child(div().flex_1().min_w(0).child(this.lib.note.text))
        .child(div().id("data-note-x").cursor_pointer().on_click((_e, cx) => { this.lib.note = null; cx.notify(); }).child(this.icon("x", 11)))))
      .child(body);
  }
}
