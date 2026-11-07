// CAPTAIN: Import a dataset, in three steps, as an overlay (openOverlay({mod: "captain-import", kind: "dialog"})).
// Props: {folder?} starts in that folder; {start: {conn, sql | table, name}} opens on that query's preview, or that
// table whole.
// 1. Where from: a grid of sources. A file or folder from this computer, every connected database with its mark, and
//    "Connect a new source" (the typed database form). Product analytics sources show as coming.
// 2. A database's tables (read once through the table browser) and suggested queries: the backend's own
//    (GET /api/connections/{name}/suggestions) when it has them, else ones made here from the tables.
// 3. Prepare for training (POST /api/prepare/preview, on a sample): what the data is (the services' guess first),
//    which column holds each field the role needs, the cleaning, and the cleaned rows with every change in words. A
//    whole table goes this way too, never as a raw copy. Import runs POST /api/prepare/import as a job, into the
//    role's folder (catalogs/, queries/, judgments/, logs/, imports/) unless another is picked; the full name shows
//    first, and a name already used by other data says so. When it's in, the wizard closes, a toast says what came
//    in, and Data and signals opens on the new dataset (shared "captain.open").
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, EditorState, Editor, Popup } from "gpui-base";
import { theme, props, closeOverlay, openOverlay, pickFolder, home, shared, setShared, notify, run } from "herdr";
import { api, query, watchConnections } from "./kit/cst.js";
import { palette, isDark, FONT, MONO, RADIUS } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand, engineTitle } from "./kit/brands.js";
import { ResultGrid } from "./kit/grid.js";
import { int } from "./kit/num.js";

const enc = encodeURIComponent;
const fmtInt = (n) => int(n ?? 0);
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());
// Product analytics sources: connected ones show with the databases; the rest offer their connect form.
const ANALYTICS = [["posthog", "PostHog", "Product events, queried with HogQL"], ["ga4", "Google Analytics 4", "Site search terms and pages"]];
const plainSchema = (s) => String(s ?? "").replace(/"/g, "");
// Where each kind of dataset goes unless you pick another folder.
export const KIND_FOLDER = { catalog: "catalogs", queries: "queries", judgments: "judgments", events: "logs", logs: "logs", generic: "imports" };

/** The name a dataset is saved under, by the services' own rule (data/prepare.py _slug): lower case letters and
 *  digits, everything else one "-" ("Hello World" is "hello-world", "sales_v2" is "sales-v2"). */
export function datasetName(text) {
  return String(text ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}

/** A folder as the person sees it, tidied: its levels trimmed, empty ones dropped. Capitals stay ("Shop"). */
export function folderShown(text) {
  return String(text ?? "").split("/").map((x) => x.trim()).filter(Boolean).join("/");
}

/** The lowercase prefix a folder gives the dataset's name ("Shop/Q4" makes "shop/q4/..."), by the services' rule. */
export function folderPath(text) {
  return String(text ?? "").split("/").map(datasetName).filter(Boolean).join("/");
}

/** The kind a query's rows most likely are, from the suggestion or the query's words. */
// "What is this?": the roles the services prepare data for, in words, and the role a suggestion's kind means.
export const ROLES = [["catalog", "Catalog"], ["queries", "Search queries"], ["judgments", "Relevance labels"], ["events", "Events or logs"], ["generic", "Something else"]];
const ROLE_OF_KIND = { catalog: "catalog", queries: "queries", judgments: "judgments", events: "events", logs: "events", generic: "generic" };
// The cleaning the services do (all on by default).
export const CLEANING = [["trim", "Trim text"], ["dedupe", "Drop duplicates"], ["keep_other_columns", "Keep other columns"]];

/** The change report in one line: "6 rows in, 3 kept: supplierID becomes id, rows missing id are dropped". */
export function reportLine(result) {
  const r = result?.report ?? {};
  const renames = Object.entries(result?.mapping ?? {}).filter(([to, from]) => from && from !== to).map(([to, from]) => `${from} becomes ${to}`);
  const parts = [...renames, ...(r.steps ?? [])];
  return `${fmtInt(r.rows_in)} row${r.rows_in === 1 ? "" : "s"} in, ${fmtInt(r.rows_out)} kept${parts.length ? `: ${parts.join(", ")}` : ""}${r.sampled ? " (on a 1,000-row sample)" : ""}.`;
}

const kindOf = (hint, sql) => hint ?? (/\b(search|query|queries)\b/i.test(sql) ? "queries" : /\b(event|log|logs|clicks?|pageviews?)\b/i.test(sql) ? "events" : "generic");
// A query over a recent time window (its rows depend on what happened lately).
const timeWindow = (sql) => /\binterval\b|date_sub|dateadd|date_add|current_date|current_timestamp|now\(\)|\b\d+\s*days?\b|daysago|-\d+d\b/i.test(sql);

/** Queries worth starting from, made from a database's tables when the backend has none to suggest. */
/** A table's full name for SQL: every part of its schema (a Databricks catalog too), quoted where it must be. */
export function qualified(schema, table) {
  return [...String(schema ?? "").split(".").filter(Boolean), table].map((p) => p.replace(/"/g, ""))
    .map((p) => (/^[a-z_][a-z0-9_]*$/.test(p) ? p : `"${p}"`)).join(".");
}

export function localSuggestions(tree) {
  const out = [];
  for (const s of tree?.schemas ?? []) for (const t of s.tables ?? []) {
    const parts = [...String(s.name).split(".").filter(Boolean), t.name].map((p) => (/^[a-z_][a-z0-9_]*$/.test(p.replace(/"/g, "")) ? p.replace(/"/g, "") : `"${p.replace(/"/g, "")}"`));
    const q = parts.slice(-2).join(".");
    out.push({ title: `1,000-row sample of ${t.name}`, sql: `select * from ${q} limit 1000`, table: t.name });
    if (/search|query|queries|event|log/i.test(t.name)) out.push({ title: `Everything in ${t.name}`, sql: `select * from ${q}`, table: t.name });
  }
  return out.slice(0, 12);
}

export default class CaptainImport extends View {
  init(_props, cx) {
    this.step = 1;
    this.conns = null;
    this.source = null; // {kind: "file"} | {kind: "db", conn}
    this.tree = null;
    this.suggestions = null;
    this.reading = false;
    this.prep = null; // step 3: {source, role, mapping, options, result, grid, loading, error}
    this.folders = [""];
    this.folder = props()?.folder ?? "";
    this.folderTouched = !!props()?.folder; // picked by hand (or opened in a folder): the kind no longer moves it
    this.newFolder = InputState.new({ placeholder: "Or type a new folder, like imports/2026" });
    this.newFolder.on("change", (_e, cx) => cx.notify());
    this.names = new Set(); // every dataset name in the workspace, to catch a name already in use
    this.clash = null; // {name, version, source, same} when the name is taken
    this.clashFor = null;
    this.allowNewVersion = false;
    this.note = null;
    this.busy = false;
    this.path = InputState.new({ placeholder: "~/Downloads/catalogs" });
    this.name = InputState.new({ placeholder: "Name the dataset, like products" });
    this.name.on("change", (_e, cx) => { this.allowNewVersion = false; cx.notify(); });
    // SQL for databases (and PostHog's HogQL); JSON for a Google Analytics 4 report spec.
    this.editors = {
      sql: EditorState.new({ language: "sql", value: "", font_family: MONO, font_size: 13, line_numbers: true }),
      json: EditorState.new({ language: "json", value: "", font_family: MONO, font_size: 13, line_numbers: true }),
    };
    this.dests = [{ kind: null, id: null, name: "None" }];
    this.dest = this.dests[0];
    this.path.on("submit", (_e, cx) => this.importFiles(cx, [expand(this.path.value())]));
    this.loadSources(cx);
    cx.timer.every(500, (cx) => this.tick(cx));
    // A database connected from here ("Connect a new source") or anywhere else shows up at once.
    watchConnections(cx, (cx) => cx.spawn(async (cx) => {
      const seq = (this.connSeq = (this.connSeq ?? 0) + 1);
      const k = await api("/api/connections", { scoped: false });
      if (seq !== this.connSeq) return;
      if (k.ok && Array.isArray(k.data)) this.conns = k.data;
      cx.notify();
    }));
  }

  /** Sources, folders, destinations and the names in use. Asked again until the services answer. */
  loadSources(cx) {
    cx.spawn(async (cx) => {
      // Every source and folder, whatever the Explorer has picked: the pick is where the import goes, not what it reads.
      const [k, t, ex, ds] = await Promise.all([api("/api/connections", { scoped: false }), api("/api/datasets/tree", { scoped: false }), api("/api/explorer", { scoped: false }), api("/api/datasets", { scoped: false })]);
      if (ds.ok && Array.isArray(ds.data)) this.names = new Set(ds.data.map((d) => d.name));
      // Not answered yet (the services are starting, or a request failed): keep the placeholder and ask again shortly,
      // never an empty list that stays empty.
      if (!(k.ok && Array.isArray(k.data))) {
        this.sourcesTries = (this.sourcesTries ?? 0) + 1;
        if (k.failed || this.sourcesTries > 40) this.note = { text: "Couldn't list your databases. Close this and try again.", tone: "fail" };
        else cx.timer.after(750, (cx) => this.loadSources(cx));
        cx.notify();
        return;
      }
      this.conns = k.data;
      this.dests = [{ kind: null, id: null, name: "None" }];
      this.folders = [""];
      // Where it belongs: a project, or a brand, site or engine from the Explorer. The Explorer's pick is the default.
      const walk = (nodes, depth) => { for (const n of nodes ?? []) {
        if (["scope", "engine", "project"].includes(n.kind) && n.id) this.dests.push({ kind: n.kind, id: n.id, name: n.name ?? n.label, type: n.type, depth });
        walk(n.children, n.kind === "group" ? depth : depth + 1);
      } };
      if (ex.ok) walk(ex.data?.tree, 0);
      const pick = shared("captain.scope");
      this.dest = this.dests.find((d) => d.kind === pick?.kind && d.id === pick?.id) ?? this.dests[0];
      if (t.ok) { const walk = (n) => { for (const f of n.folders ?? []) { this.folders.push(f.path); walk(f); } }; walk(t.data ?? {}); }
      // Opened on a query (props.start = {conn, sql, name}): straight to its preview.
      const start = props()?.start;
      if (start?.conn && start.table && this.conns.some((k) => k.name === start.conn)) {
        // A table by name: its tables load, then it opens whole.
        this.startTable = start.table;
        this.autoImport = !!start.autoImport; // the real-window check: press Import once the preview is ready
        if (start.name) this.name.set_value(start.name);
        this.pick(cx, { kind: "db", conn: start.conn });
      } else if (start?.conn && this.conns.some((k) => k.name === start.conn)) {
        this.source = { kind: "db", conn: start.conn };
        this.sql.set_value(start.sql ?? "");
        if (start.name) this.name.set_value(start.name);
        this.step = 3;
        this.runPreview(cx);
      }
      cx.notify();
    });
  }

  /** The name it will be saved under. */
  get finalName() { return datasetName(this.name.value()); }

  /** The folder, exactly as shown and sent: the typed one, else the picked one, else none. A new dataset goes to the
   *  top level unless a folder is picked here, so it's always in plain sight under All datasets (Edgar's "hello" landed
   *  in whatever folder happened to be open and he couldn't find it). The library keeps capitals ("Shop"). */
  get target() {
    const typed = folderShown(this.newFolder.value());
    if (typed) return typed;
    return this.folderTouched ? this.folder : "";
  }

  /** The dataset's full name: its folder, then its name (catalogs/products). */
  get fullName() {
    const n = this.finalName;
    const prefix = folderPath(this.target);
    return n && prefix ? `${prefix}/${n}` : n;
  }

  /** A name nobody has used in the target folder: the name itself, else name-2, name-3 ... */
  freshName(base) {
    base = datasetName(base) || "dataset";
    const prefix = folderPath(this.target);
    const taken = (n) => this.names.has(prefix ? `${prefix}/${n}` : n) || this.names.has(n);
    if (!taken(base)) return base;
    for (let i = 2; ; i++) if (!taken(`${base}-${i}`)) return `${base}-${i}`;
  }

  /** Is this name taken, and by what? Looked up when the name or folder changes. */
  checkName(cx) {
    const name = this.fullName;
    if (name === this.clashFor) return;
    this.clashFor = name;
    this.clash = null;
    if (!name || !this.names.has(name)) return;
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${name.split("/").map(enc).join("/")}`, { scoped: false });
      if (this.clashFor !== name || !r.ok) return;
      const pv = r.data.provenance ?? {};
      const what = pv.sql ?? pv.table ?? "";
      const source = pv.connection ? `${pv.connection}: ${String(what).replace(/\s+/g, " ").slice(0, 80)}` : pv.source ?? "a file";
      const src = this.prep?.source ?? {};
      const same = pv.connection === this.source?.conn && String(what).trim() === String(src.sql ?? src.table ?? "").trim();
      this.clash = { name, version: r.data.version, source, same };
      cx.notify();
    });
  }

  close() { closeOverlay(props().overlay); }

  /** The editor for this source: JSON for Google Analytics 4, SQL for everything else. */
  get sql() {
    const k = (this.conns ?? []).find((x) => x.name === this.source?.conn);
    return this.editors[(k?.kind ?? k?.engine) === "ga4" ? "json" : "sql"];
  }

  pick(cx, source) {
    this.source = source;
    this.note = null;
    if (source.kind === "db") { this.step = 2; this.loadTables(cx); }
    cx.notify();
  }

  loadTables(cx) {
    const conn = this.source.conn;
    this.tree = null;
    this.suggestions = null;
    cx.spawn(async (cx) => {
      const [t, s] = await Promise.all([api(`/api/tables/${enc(conn)}`), api(`/api/connections/${enc(conn)}/suggestions`)]);
      if (this.source?.conn !== conn) return;
      if (t.ok) this.tree = t.data;
      else if (t.status === 404) this.tree = { unread: true };
      else this.tree = { error: "Couldn't list its tables." };
      // Opened on a table by name (props.start.table): open it whole once its schema is known.
      const want = this.startTable && (this.tree?.schemas ?? []).find((s) => (s.tables ?? []).some((x) => x.name === this.startTable));
      if (want) { const table = this.startTable; this.startTable = null; this.useTable(cx, want.name, table); }
      // The backend's suggestions ({pulled, suggestions: [{title, why, query}]}), else ones made here from the tables.
      const list = s.ok ? (Array.isArray(s.data) ? s.data : s.data?.suggestions) : null;
      this.suggestions = Array.isArray(list) && list.length ? list.map((x) => ({ title: x.title, sql: typeof (x.query ?? x.sql) === "string" ? x.query ?? x.sql : JSON.stringify(x.query, null, 2), why: x.why, kind: x.kind })) : localSuggestions(t.ok ? t.data : null);
      cx.notify();
    });
  }

  readTables(cx) {
    const conn = this.source.conn;
    this.reading = true;
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/${enc(conn)}/pull`, { body: {} });
      if (!r.ok) { this.reading = false; this.note = { text: sentence(r.data?.error ?? r.error) || "Couldn't read its tables.", tone: "fail" }; cx.notify(); return; }
      this.pullJob = r.data.job;
      cx.notify();
    });
  }

  tick(cx) {
    if (this.step === 3 && this.source?.kind === "db") this.checkName(cx);
    if (this.pullJob && !this.polling) {
      this.polling = true;
      cx.spawn(async (cx) => {
        const r = await api(`/api/jobs/${enc(this.pullJob)}`);
        this.polling = false;
        if (r.ok && (r.data.status === "done" || r.data.status === "failed")) {
          this.pullJob = null;
          this.reading = false;
          if (r.data.status === "failed") this.note = { text: sentence(r.data.error ?? r.data.message) || "Couldn't read its tables.", tone: "fail" };
          this.loadTables(cx);
        }
        cx.notify();
      });
    }
    if (this.prepJob && !this.polling) {
      this.polling = true;
      cx.spawn(async (cx) => {
        const r = await api(`/api/jobs/${enc(this.prepJob)}`);
        this.polling = false;
        if (r.ok && r.data.status === "done") { this.prepJob = null; await this.prepared(cx, r.data.result ?? {}); }
        else if (r.ok && r.data.status === "failed") {
          this.prepJob = null;
          this.busy = false;
          this.note = { text: sentence(r.data.error ?? r.data.message) || "The import stopped.", tone: "fail" };
        }
        cx.notify();
      });
    }
    if (this.importJob && !this.polling) {
      this.polling = true;
      cx.spawn(async (cx) => {
        const r = await api(`/api/jobs/${enc(this.importJob)}`);
        this.polling = false;
        if (r.ok && r.data.status === "done") {
          this.importJob = null;
          const res = r.data.result ?? {};
          await this.link([...(res.imported ?? []), ...(res.updated ?? [])].map((x) => String(x.ref ?? "").replace(/@v\d+$/, "")).filter(Boolean));
          this.busy = false;
          const done = [...(res.imported ?? []), ...(res.updated ?? [])].map((x) => String(x.ref ?? "").replace(/@v\d+$/, "")).filter(Boolean);
          const n = done.length;
          const skipped = (res.skipped ?? []).length ? ` Skipped ${res.skipped.length}: ${res.skipped.map((x) => sentence(x.reason)).join(" ")}` : "";
          if (n) this.finish(done[0], `Added ${n} dataset${n === 1 ? "" : "s"}`, `${n === 1 ? done[0] : `${done[0]} and ${n - 1} more`}${this.folder ? `, into ${this.folder}` : ""}.${skipped}`, skipped ? "warning" : "success");
          else this.note = { text: `Nothing was added.${skipped}`, tone: "muted" };
        } else if (r.ok && r.data.status === "failed") {
          this.importJob = null;
          this.busy = false;
          this.note = { text: sentence(r.data.error ?? r.data.message) || "The import stopped.", tone: "fail" };
        }
        cx.notify();
      });
    }
  }

  /** A suggested or written query: prepared for training like a table. */
  useQuery(cx, sql, name, kind) {
    this.kindHint = kind ?? null;
    this.sql.set_value(sql);
    this.startPrepare(cx, { conn: this.source.conn, sql }, { role: ROLE_OF_KIND[kind] ?? null, name });
  }

  /** A table picked: prepared for training whole (never a raw copy). */
  useTable(cx, schema, table) {
    this.kindHint = null;
    this.sql.set_value(`select * from ${qualified(schema, table)}`);
    this.startPrepare(cx, { conn: this.source.conn, table: `${schema}.${table}` }, { name: table });
  }

  /** Step 3, "Prepare for training": the services guess the role and the column mapping, then the person adjusts. */
  startPrepare(cx, source, { role = null, name = null } = {}) {
    this.prep = { source, role, mapping: null, options: { trim: true, dedupe: true, keep_other_columns: true }, result: null, loading: false, error: null, open: null, seedName: name };
    this.clashFor = null;
    this.allowNewVersion = false;
    this.step = 3;
    this.preparePreview(cx);
    cx.notify();
  }

  /** Ask for the cleaned preview with the current role, mapping and cleaning. Only the newest answer counts. */
  preparePreview(cx) {
    const p = this.prep;
    if (!p?.source) return;
    const seq = (p.seq = (p.seq ?? 0) + 1);
    p.loading = true;
    p.started = Date.now();
    cx.spawn(async (cx) => {
      const r = await api("/api/prepare/preview", { body: { source: p.source, role: p.role ?? undefined, mapping: p.mapping ?? undefined, options: p.options }, timeout: 180 });
      if (this.prep !== p || seq !== p.seq) return;
      p.loading = false;
      if (!r.ok) { p.error = sentence(r.data?.error ?? r.data?.detail ?? r.error) || "Couldn't prepare this."; cx.notify(); return; }
      const d = r.data;
      p.error = null;
      p.result = d;
      p.role = d.role;
      p.mapping = { ...(d.mapping ?? {}) };
      if (!this.name.value().trim()) this.name.set_value(this.freshName(p.seedName ?? d.suggested_name ?? "data"));
      this.clashFor = null;
      if (this.autoImport && !d.missing?.length) { this.autoImport = false; cx.timer?.after(500, (cx) => this.importQuery(cx)); }
      p.grid = new ResultGrid({ id: "import-preview", name: this.finalName || "prepared", table: { columns: d.page?.columns ?? [], rows: d.page?.rows ?? [], row_count: d.report?.rows_out } });
      cx.notify();
    });
    cx.notify();
  }

  /** The query in the editor, prepared again (after editing it). */
  runPreview(cx) {
    const sql = this.sql.value().trim();
    if (!sql || !this.source?.conn) return;
    if (this.prep?.source && !this.prep.source.table) { this.prep.source = { conn: this.source.conn, sql }; this.clashFor = null; this.preparePreview(cx); }
    else this.startPrepare(cx, { conn: this.source.conn, sql }, { role: ROLE_OF_KIND[this.kindHint] ?? null });
  }

  setRole(cx, role) {
    const p = this.prep;
    if (!p || p.role === role) return;
    p.role = role;
    p.mapping = null; // the services map the new role's columns afresh
    p.open = null;
    if (!this.folderTouched) this.clashFor = null;
    this.preparePreview(cx);
  }

  setColumn(cx, field, col) {
    const p = this.prep;
    p.mapping = { ...(p.mapping ?? {}), [field]: col };
    p.open = null;
    this.preparePreview(cx);
  }

  toggleCleaning(cx, key) {
    const p = this.prep;
    p.options = { ...p.options, [key]: !p.options[key] };
    this.preparePreview(cx);
  }

  /** What stops an import now, in words, or null. */
  get blocker() {
    const p = this.prep;
    if (!p?.result) return p?.loading ? "Preparing" : "Prepare it first.";
    if ((p.result.missing ?? []).length) return (p.result.report?.problems ?? [])[0] ?? `Pick a column for ${p.result.missing.join(", ")}.`;
    if (p.result.report?.rows_out === 0) return "Cleaning left no rows. Check the column choices.";
    if (!this.finalName) return "Name the dataset first.";
    if (this.clash && !this.clash.same && !this.allowNewVersion) return `${this.clash.name} already holds other data.`;
    return null;
  }

  /** Prepare the whole source and save it: a job, then the toast and Data on the new dataset. */
  importQuery(cx) {
    const p = this.prep;
    const stop = this.blocker;
    if (stop) { this.note = { text: stop, tone: "fail" }; cx.notify(); return; }
    const dest = this.dest;
    this.busy = true;
    this.note = null;
    cx.spawn(async (cx) => {
      const r = await api("/api/prepare/import", { body: { source: p.source, role: p.role, mapping: p.mapping, options: p.options, name: this.finalName, folder: this.target, project: dest.kind === "project" ? dest.id : undefined } });
      if (!r.ok) { this.busy = false; this.note = { text: sentence(r.data?.error ?? r.error) || "Couldn't start the import.", tone: "fail" }; cx.notify(); return; }
      this.prepJob = r.data.job;
      cx.notify();
    });
    cx.notify();
  }

  /** The prepare job finished: link it to the brand, then the toast and the jump. */
  async prepared(cx, res) {
    const dest = this.dest;
    const linked = await this.link([res.name]);
    this.busy = false;
    const label = (this.prep?.result?.roles ?? []).find((x) => x.role === res.role || x.role === this.prep?.role)?.label ?? "dataset";
    const where = res.folder ? ` In ${res.folder}${dest.kind ? `, for ${dest.name}` : ""}.` : dest.kind ? ` For ${dest.name}.` : "";
    const body = `${reportLine({ ...this.prep?.result, report: res.report }).replace(/\.$/, "")}. Ready for training as ${label.toLowerCase()}.${where}${linked}`;
    this.finish(res.name, `Imported ${res.name}, version ${res.version}`, body, linked ? "warning" : "success");
  }

  /** It's in: a toast says what came in, Data and signals opens on it, and the wizard closes. */
  finish(name, title, body, level = "success") {
    this.names.add(name);
    setShared("captain.open", { section: "data", dataset: name, fresh: true, at: Date.now() });
    run("layout.select", { stack: "sections", child: "data" });
    notify({ title, body, level, timeout: 12000, action: { name: "layout.select", args: { stack: "sections", child: "data" } } });
    this.close();
  }

  /** Attach datasets to the chosen brand, site or engine (a project is set by the import itself). "" when fine. */
  async link(names) {
    const d = this.dest;
    if (!d.kind || d.kind === "project" || !names.length) return "";
    const r = await api("/api/scopes/link", { body: { items: names.map((name) => ({ kind: "dataset", name })), [d.kind === "engine" ? "engine" : "scope"]: d.id } });
    return r.ok ? "" : ` It couldn't be linked to ${d.name}: ${sentence(r.data?.error ?? r.error) || "try again from the Explorer."}`;
  }

  importFiles(cx, paths) {
    paths = paths.filter(Boolean);
    if (!paths.length) return;
    this.busy = true;
    cx.spawn(async (cx) => {
      const r = await api("/api/datasets/import", { body: { paths, folder: folderShown(this.newFolder.value()) || this.folder, project: this.dest.kind === "project" ? this.dest.id : undefined } });
      if (!r.ok) { this.busy = false; this.note = { text: sentence(r.data?.error ?? r.error) || "Couldn't start the import.", tone: "fail" }; cx.notify(); return; }
      this.importJob = r.data.job;
      cx.notify();
    });
    cx.notify();
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  icon(name, size, color) {
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  tile(c, id, mark, title, sub, onClick, { soon = false, on = false } = {}) {
    // Titles wrap rather than cut ("Connect a new source", "Connect Google Analytics 4").
    return v_flex().id(id).w(196).min_h(96).p(12).gap(6).justify_between().border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface)
      .when(!soon, (e) => e.cursor_pointer().hover((s) => s.border_color(c.blue)).on_click(onClick))
      .child(h_flex().gap(8).items_center().child(mark).child(div().flex_1().min_w(0).text_size(13).line_height("17px").font_medium().text_color(soon ? c.grey500 : c.navy).child(title)))
      .child(div().text_size(11.5).text_color(c.grey500).child(sub));
  }

  render() {
    const c = palette(isDark(theme()));
    const steps = [[1, "Where from"], [2, "What to bring in"], [3, "Prepare and import"]];
    return v_flex().id("import").size_full().bg(c.surface).rounded(RADIUS).font_family(FONT).text_color(c.navy)
      .child(h_flex().h(56).flex_none().px(20).gap(14).items_center().border_b(1).border_color(c.rule)
        .child(this.icon("database", 18, c.navy))
        .child(div().text_size(16).font_semibold().child("Import a dataset"))
        .child(h_flex().gap(6).items_center().children(steps.flatMap(([n, label], i) => [
          ...(i ? [this.icon("chevron-right", 11, c.grey300)] : []),
          div().id(`step-${n}`).text_size(12).text_color(this.step === n ? c.blue : n < this.step ? c.navy : c.grey500)
            .when(n < this.step, (e) => e.cursor_pointer().on_click((_e, cx) => { this.step = n; cx.notify(); })).child(label),
        ])))
        .child(div().flex_1())
        .child(div().id("import-close").cursor_pointer().text_color(c.grey500).on_click(() => this.close()).child(this.icon("x", 14))))
      .when(!!this.note, (e) => e.child(div().flex_none().px(20).py(8).text_size(12.5)
        .text_color(this.note.tone === "fail" ? c.fail : this.note.tone === "good" ? c.good : c.grey900).child(this.note.text)))
      .child(this.step === 1 ? this.sources(c) : this.step === 2 ? this.choose(c) : this.previewStep(c));
  }

  sources(c) {
    const file = this.source?.kind === "file";
    return v_flex().id("import-sources").flex_1().min_h(0).overflow_y_scrollbar().p(20).gap(14)
      .child(div().text_size(12.5).text_color(c.grey900).child("Pick where the data lives. Nothing is copied until you import."))
      .child(h_flex().flex_wrap().gap(10)
        .child(this.tile(c, "src-file", this.icon("upload", 18, c.navy), "A file or folder", "CSV, Parquet, JSON on this computer", (_e, cx) => this.pick(cx, { kind: "file" }), { on: file }))
        .children((this.conns ?? []).map((k) => this.tile(c, `src-${k.name}`, Brand(c, k.kind ?? k.engine, 18), k.name, `${engineTitle(k.engine ?? k.kind)}${k.read_only === false ? ", writes allowed" : ""}`,
          (_e, cx) => this.pick(cx, { kind: "db", conn: k.name }))))
        .children(ANALYTICS.filter(([kind]) => !(this.conns ?? []).some((k) => (k.kind ?? k.engine) === kind)).map(([kind, label, sub]) =>
          this.tile(c, `src-${kind}`, Brand(c, kind, 18), `Connect ${label}`, sub,
            () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "database", values: { kind } } }))))
        .child(this.tile(c, "src-new", this.icon("plus", 18, c.blue), "Connect a new source", "A warehouse or database", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "database" } }))))
      .when(this.conns === null, (e) => e.child(Skeleton(c, 2)))
      .when(file, (e) => e.child(v_flex().gap(8).p(14).border_1().border_color(c.rule)
        .child(div().text_size(12.5).font_medium().child("Which files"))
        .child(h_flex().gap(8).items_center()
          .child(h_flex().flex_1().min_w(0).h(28).px(8).items_center().border_1().border_color(c.grey300).child(Input.new(this.path).w_full().text_size(12).text_color(c.navy)))
          .child(Button(c, "file-pick", "Choose a folder", (_e, cx) => cx.spawn(async (cx) => { const p = await pickFolder({ prompt: "Import" }); if (p) this.importFiles(cx, [p]); }), { small: true }))
          .child(Button(c, "file-go", this.busy ? "Importing" : "Import", (_e, cx) => this.importFiles(cx, [expand(this.path.value())]), { kind: "primary", small: true, disabled: this.busy })))
        .child(this.folderPick(c))
        .child(this.destPick(c))
        .child(div().text_size(11.5).text_color(c.grey500).child("You can also drag files from Finder onto Data and signals."))));
  }

  destPick(c) {
    return h_flex().id("import-dest").gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
      .child(div().flex_none().text_color(c.grey500).child("For"))
      .children(this.dests.map((d, i) => div().id(`dest-${i}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
        .border_1().border_color(this.dest === d ? c.blue : c.grey300).text_color(this.dest === d ? c.blue : c.grey900)
        .tooltip(d.kind ? `${d.kind === "project" ? "Project" : d.type ? `${d.type[0].toUpperCase()}${d.type.slice(1)}` : "Engine"}: ${d.name}` : "Not tied to a brand or project")
        .on_click((_e, cx) => { this.dest = d; cx.notify(); }).child(d.name)));
  }

  folderPick(c, forQuery = false) {
    const target = forQuery ? this.target : folderShown(this.newFolder.value()) || this.folder;
    const typed = !!folderShown(this.newFolder.value());
    const list = [...this.folders];
    if (target && !list.includes(target)) list.push(target);
    return v_flex().gap(6)
      .child(h_flex().id("import-folder").gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
        .child(div().w(60).flex_none().text_size(12).text_color(c.grey900).child("Folder"))
        .children(list.map((f, i) => div().id(`folder-${i}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
          .border_1().border_color(!typed && target === f ? c.blue : c.grey300).text_color(!typed && target === f ? c.blue : c.grey900)
          .on_click((_e, cx) => { this.folder = f; this.folderTouched = true; this.newFolder.set_value(""); cx.notify(); })
          .child(`${f || "Top level"}${f && !this.folders.includes(f) ? " (new)" : ""}`))))
      .child(h_flex().gap(8).items_center()
        .child(div().w(60).flex_none())
        .child(h_flex().w(280).h(26).px(8).items_center().border_1().border_color(typed ? c.blue : c.grey300).child(Input.new(this.newFolder).w_full().text_size(11.5).text_color(c.navy))));
  }

  /** What it will be saved as, before anything is saved. */
  saveLine(c) {
    const name = this.finalName;
    const typed = this.name.value().trim();
    if (!name) return div().text_size(11.5).text_color(c.grey500).child("Name it to see where it goes.");
    const vv = this.clash?.same ? `, as version ${this.clash.version + 1}` : this.clash && this.allowNewVersion ? `, as version ${this.clash.version + 1}` : "";
    return h_flex().id("import-save-as").gap(6).items_center().text_size(12)
      .child(div().w(60).flex_none().text_color(c.grey900).child("Saved as"))
      .child(div().font_family(MONO).text_color(c.navy).child(this.fullName))
      .when(!!this.target, (e) => e.child(div().text_color(c.grey900).child(`in ${this.target}`)))
      .child(div().text_color(c.grey500).child(`${typed && typed !== name ? `(from "${typed}")` : ""}${vv}`));
  }

  clashView(c) {
    const k = this.clash;
    const fresh = this.freshName(this.finalName);
    return v_flex().id("import-clash").gap(6).p(10).border_1().border_color(c.warn).text_size(12)
      .child(div().text_color(c.navy).child(`${k.name} already exists (version ${k.version}, from ${k.source}). Import as a new version, or pick another name.`))
      .child(h_flex().gap(6)
        .child(Button(c, "clash-rename", `Name it ${fresh}`, (_e, cx) => { this.name.set_value(fresh); this.allowNewVersion = false; cx.notify(); }, { kind: "primary", small: true }))
        .child(Button(c, "clash-version", `Import as version ${k.version + 1}`, (_e, cx) => { this.allowNewVersion = true; cx.notify(); }, { small: true, disabled: this.allowNewVersion })));
  }

  choose(c) {
    const t = this.tree;
    const conn = this.source?.conn;
    return h_flex().flex_1().min_h(0)
      .child(v_flex().id("import-tables").w(260).h_full().flex_none().border_r(1).border_color(c.rule).overflow_y_scrollbar().p(12).gap(4)
        .child(div().text_size(12.5).font_medium().child(`Tables in ${conn}`))
        .when(t === null, (e) => e.child(Skeleton(c, 5)))
        .when(!!t?.error, (e) => e.child(div().text_size(12).text_color(c.fail).child(t.error)))
        .when(!!t?.unread, (e) => e.child(v_flex().gap(6).child(div().text_size(12).text_color(c.grey900).child("Captain hasn't read this database's tables yet."))
          .child(Button(c, "read-tables", this.reading ? "Reading" : "Read its tables", (_e, cx) => this.readTables(cx), { kind: "primary", small: true, disabled: this.reading }))))
        .children((t?.schemas ?? []).flatMap((s) => [
          div().pt(6).text_size(11.5).text_color(c.grey500).child(plainSchema(s.name)),
          ...(s.tables ?? []).map((x) => h_flex().id(`tbl-${s.name}-${x.name}`).h(26).px(6).gap(6).items_center().cursor_pointer().hover((st) => st.bg(c.surface2))
            .on_click((_e, cx) => this.useTable(cx, s.name, x.name))
            .child(this.icon("table", 12, c.grey500)).child(div().flex_1().min_w(0).truncate().text_size(12).font_family(MONO).child(x.name))),
        ])))
      .child(v_flex().id("import-suggest").flex_1().min_w(0).h_full().overflow_y_scrollbar().p(16).gap(10)
        .child(div().text_size(13).font_medium().child("Suggested queries"))
        .child(div().text_size(12).text_color(c.grey900).child("Start from one of these, or write your own on the next step."))
        .when(this.suggestions === null, (e) => e.child(Skeleton(c, 4)))
        .when(Array.isArray(this.suggestions) && !this.suggestions.length, (e) => e.child(div().text_size(12).text_color(c.grey500).child("Read its tables to get suggestions.")))
        .children((this.suggestions ?? []).map((s, i) => v_flex().id(`sug-${i}`).gap(4).p(10).cursor_pointer().border_1().border_color(c.rule).hover((st) => st.border_color(c.blue))
          .on_click((_e, cx) => this.useQuery(cx, s.sql, s.table, s.kind))
          .child(h_flex().gap(8).items_center()
            .child(div().flex_1().min_w(0).text_size(12.5).font_medium().child(s.title))
            .child(Button(c, `sug-preview-${i}`, "Preview", (_e, cx) => this.useQuery(cx, s.sql, s.table, s.kind), { small: true })))
          .when(!!s.why, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(s.why)))
          .child(div().text_size(11.5).font_family(MONO).text_color(c.grey900).truncate().child(s.sql))))
        .child(Button(c, "write-own", "Write my own query", (_e, cx) => { this.step = 3; cx.notify(); }, { small: true })));
  }

  previewStep(c) {
    const p = this.prep;
    const r = p?.result;
    const isQuery = !p?.source?.table;
    const spec = (r?.roles ?? []).find((x) => x.role === p?.role);
    const stop = this.blocker;
    const secs = p?.loading ? Math.floor((Date.now() - (p.started ?? Date.now())) / 1000) : 0;
    const label = (t) => div().w(96).flex_none().text_size(12).text_color(c.grey900).child(t);
    const chip = (id, text, on, onClick) => div().id(id).h(24).px(8).flex().flex_none().items_center().cursor_pointer().text_size(12)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900).on_click(onClick).child(text);
    return v_flex().id("import-preview-step").flex_1().min_h(0).overflow_y_scrollbar().p(16).gap(10)
      .child(div().text_size(13).font_medium().child("Prepare for training"))
      .when(isQuery, (e) => e.child(div().flex_none().h(90).border_1().border_color(c.rule).child(Editor.new(this.sql).size_full()))
        .child(h_flex().flex_none().gap(8).items_center()
          .child(Button(c, "preview-run", p?.source ? "Prepare again" : "Prepare this query", (_e, cx) => this.runPreview(cx), { small: true }))
          .when(timeWindow(this.sql.value()) && r?.report?.rows_in === 0, (e) => e.child(div().text_size(11.5).text_color(c.warn)
            .child("No rows: this query looks at a recent time window, and this account may have had no events in it.")))))
      .when(!isQuery, (e) => e.child(div().text_size(12).text_color(c.grey500).child(`The whole of ${p.source.table.split(".").pop()} on ${p.source.conn}, cleaned the way you choose below.`)))
      .when(!!p?.error, (e) => e.child(div().p(10).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).text_color(c.fail).child(p.error)))
      .when(!!p?.loading && !r, (e) => e.child(v_flex().gap(6).child(div().text_size(12).text_color(c.grey900).child(`Reading a sample from ${p.source.conn}, ${secs} s`)).child(Skeleton(c, 5))))
      .when(!!r, (e) => e
        // What is this? The services' guess is preselected.
        .child(h_flex().flex_wrap().gap(6).items_center().child(label("What is this?"))
          .children(ROLES.map(([k, t]) => chip(`role-${k}`, t, p.role === k, (_e, cx) => this.setRole(cx, k)))))
        // One row per column the role needs: required first, marked; an unmapped required one in red.
        .child(v_flex().id("prep-map").gap(4)
          .child(h_flex().items_center().child(label("Columns")).child(div().text_size(11.5).text_color(c.grey500).child(spec ? `What ${spec.label.toLowerCase()} needs, and which of your columns holds it.` : "")))
          .children([...(spec?.required ?? []).map((f) => [f, true]), ...(spec?.optional ?? []).map((f) => [f, false])].map(([f, req]) => this.mapRow(c, f, req))))
        .children((r.report?.problems ?? []).map((t, i) => div().id(`prep-problem-${i}`).text_size(12).text_color(c.fail).child(t)))
        .children((r.report?.warnings ?? []).map((t, i) => div().id(`prep-warn-${i}`).text_size(12).text_color(c.warn).child(t)))
        .child(h_flex().flex_wrap().gap(6).items_center().child(label("Cleaning"))
          .children(CLEANING.map(([k, t]) => chip(`clean-${k}`, t, !!p.options[k], (_e, cx) => this.toggleCleaning(cx, k)))))
        .child(h_flex().gap(8).items_start().child(label("What changes"))
          .child(div().id("prep-report").flex_1().min_w(0).text_size(12).text_color(c.navy).child(reportLine(r)))
          .when(!!p.loading, (e) => e.child(div().flex_none().text_size(11.5).text_color(c.grey500).child(`Updating, ${secs} s`))))
        .when(!!p.grid, (e) => e.child(div().flex_none().border_1().border_color(c.rule).child(p.grid.render(c, { height: 200 })))))
      .child(v_flex().flex_none().gap(8).pt(4).border_t(1).border_color(c.rule)
        .child(h_flex().gap(8).items_center()
          .child(div().w(60).flex_none().text_size(12).text_color(c.grey900).child("Name"))
          .child(h_flex().flex_1().min_w(0).h(28).px(8).items_center().border_1().border_color(c.grey300).child(Input.new(this.name).w_full().text_size(12).text_color(c.navy))))
        .child(this.folderPick(c, true))
        .child(this.destPick(c))
        .child(this.saveLine(c))
        .when(!!this.clash && !this.clash.same, (e) => e.child(this.clashView(c)))
        .child(h_flex().gap(8).items_center()
          .child(Button(c, "import-go", this.busy ? "Importing" : this.clash && this.allowNewVersion ? `Import as version ${this.clash.version + 1}` : "Import for training", (_e, cx) => this.importQuery(cx),
            { kind: "primary", small: true, disabled: this.busy || !!stop }))
          .child(div().text_size(11.5).text_color(stop && r ? c.fail : c.grey500).child(stop && r ? stop : "Cleans the whole source the same way and saves it as a write-once dataset."))));
  }

  /** One needed column: its name (required ones marked) and a list of the source's columns to pick from. */
  mapRow(c, field, required) {
    const p = this.prep;
    const r = p.result;
    const col = p.mapping?.[field] ?? null;
    const missing = required && !col;
    const trigger = h_flex().id(`map-btn-${field}`).h(24).w(220).px(8).gap(6).items_center().cursor_pointer().text_size(12)
      .border_1().border_color(missing ? c.fail : p.open === field ? c.blue : c.grey300).bg(c.surface)
      .on_click((_e, cx) => { p.open = p.open === field ? null : field; cx.notify(); })
      .child(div().flex_1().min_w(0).truncate().font_family(MONO).text_color(col ? c.navy : missing ? c.fail : c.grey500).child(col ?? (required ? "Pick a column" : "Not used")))
      .child(this.icon("chevron-down", 11, c.grey500));
    const opt = (key, text, on, value) => div().id(`map-${field}-${key}`).h(24).px(8).flex().items_center().cursor_pointer().text_size(12).font_family(MONO)
      .text_color(on ? c.blue : c.navy).when(on, (e) => e.bg(c.blueLight)).hover((st) => st.bg(c.surface2)).on_click((_e, cx) => this.setColumn(cx, field, value)).child(text);
    return h_flex().id(`map-row-${field}`).gap(8).items_center()
      .child(h_flex().w(96).flex_none().gap(3).items_center().text_size(12).font_family(MONO).text_color(missing ? c.fail : c.navy)
        .child(div().truncate().child(field)).when(required, (e) => e.child(div().text_color(missing ? c.fail : c.grey500).child("*"))))
      .child(Popup.new(`map-pop-${field}`, trigger).anchor("bottom_left")
        .when(p.open === field, (e) => e.content(v_flex().id(`map-list-${field}`).w(240).max_h(260).overflow_y_scrollbar().py(4).bg(c.surface).border_1().border_color(c.grey300)
          .child(opt("none", required ? "None" : "Not used", !col, null))
          .children((r.columns ?? []).map((x, i) => opt(`c${i}`, x, col === x, x))))))
      .when(missing, (e) => e.child(div().text_size(11.5).text_color(c.fail).child("required")));
  }

}
