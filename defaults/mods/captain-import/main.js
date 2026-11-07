// CAPTAIN: Import a dataset, in three steps, as an overlay (openOverlay({mod: "captain-import", kind: "dialog"})).
// Props: {folder?} starts in that folder; {start: {conn, sql | table, name}} opens on that query's preview, or that
// table whole.
// 1. Where from: a grid of sources. A file or folder from this computer, every connected database with its mark, and
//    "Connect a new source" (the typed database form). Product analytics sources show as coming.
// 2. A database's tables (read once through the table browser) and suggested queries: the backend's own
//    (GET /api/connections/{name}/suggestions) when it has them, else ones made here from the tables.
// 3. Preview up to 1,000 rows (POST /api/connections/{name}/preview, else POST /api/sql), then import it as a dataset
//    into a folder (POST /api/sql {save}, then POST /api/library/move). The folder starts from the kind (catalogs/,
//    queries/, judgments/, logs/, imports/), the name shows as it will be saved, a name already used by other data
//    says so, and an empty result asks first. When it's in, the wizard closes, a toast says what came in, and Data and
//    signals opens on the new dataset (shared "captain.open").
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, EditorState, Editor } from "gpui-base";
import { theme, props, closeOverlay, openOverlay, pickFolder, home, shared, setShared, notify, run } from "herdr";
import { api, query, watchConnections } from "./kit/cst.js";
import { palette, isDark, FONT, MONO, RADIUS } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { ResultGrid } from "./kit/grid.js";

const enc = encodeURIComponent;
const fmtInt = (n) => Number(n ?? 0).toLocaleString("en-US");
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());
// Product analytics sources: connected ones show with the databases; the rest offer their connect form.
const ANALYTICS = [["posthog", "PostHog", "Product events, queried with HogQL"], ["ga4", "Google Analytics 4", "Site search terms and pages"]];
const plainSchema = (s) => String(s ?? "").replace(/"/g, "");
// Where each kind of dataset goes unless you pick another folder.
export const KIND_FOLDER = { catalog: "catalogs", queries: "queries", judgments: "judgments", events: "logs", logs: "logs", generic: "imports" };

/** The name a dataset is saved under: lower case, words joined by "-" ("Hello World" is "hello-world"). */
export function datasetName(text) {
  return String(text ?? "").trim().toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/-{2,}/g, "-").replace(/^[-.]+|[-.]+$/g, "").slice(0, 100);
}

/** A folder path as the library keeps it: lower case words split by "/". */
export function folderPath(text) {
  return String(text ?? "").split("/").map(datasetName).filter(Boolean).join("/");
}

/** The kind a query's rows most likely are, from the suggestion or the query's words. */
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
    this.preview = null; // {grid, rows, ms} | {error} | {loading}
    this.folders = [""];
    this.folder = props()?.folder ?? "";
    this.folderTouched = !!props()?.folder; // picked by hand (or opened in a folder): the kind no longer moves it
    this.newFolder = InputState.new({ placeholder: "Or type a new folder, like imports/2026" });
    this.newFolder.on("change", (_e, cx) => cx.notify());
    this.names = new Set(); // every dataset name in the workspace, to catch a name already in use
    this.clash = null; // {name, version, source, same} when the name is taken
    this.clashFor = null;
    this.allowNewVersion = false;
    this.confirmEmpty = false;
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

  /** The folder it goes into: the typed one, else the picked one, else the kind's. */
  get target() {
    const typed = folderPath(this.newFolder.value());
    if (typed) return typed;
    if (this.folderTouched) return this.folder;
    return KIND_FOLDER[kindOf(this.kindHint, this.sql.value())] ?? "imports";
  }

  /** A name nobody has used: the name itself, else name-2, name-3 ... */
  freshName(base) {
    base = datasetName(base) || "dataset";
    if (!this.names.has(base)) return base;
    for (let i = 2; ; i++) if (!this.names.has(`${base}-${i}`)) return `${base}-${i}`;
  }

  /** Is this name taken, and by what? Looked up when the name changes. */
  checkName(cx) {
    const name = this.finalName;
    if (name === this.clashFor) return;
    this.clashFor = name;
    this.clash = null;
    if (!name || !this.names.has(name)) return;
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${name.split("/").map(enc).join("/")}`, { scoped: false });
      if (this.clashFor !== name || !r.ok) return;
      const pv = r.data.provenance ?? {};
      const source = pv.connection ? `${pv.connection}: ${String(pv.sql ?? "").replace(/\s+/g, " ").slice(0, 80)}` : pv.source ?? "a file";
      const same = pv.connection === this.source?.conn && String(pv.sql ?? "").trim() === this.sql.value().trim();
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

  useQuery(cx, sql, name, kind) {
    this.kindHint = kind ?? null;
    this.sql.set_value(sql);
    this.confirmEmpty = false;
    // A suggestion's name, made unique: an import never quietly becomes a new version of other data.
    if (name && !this.name.value().trim()) this.name.set_value(this.freshName(name));
    this.step = 3;
    this.runPreview(cx);
  }

  /** A table picked: the whole table, a page at a time, in the kit grid Tables uses. Imports SELECT * of it. */
  useTable(cx, schema, table) {
    const conn = this.source.conn;
    this.kindHint = null;
    this.tableSel = { schema, table };
    this.sql.set_value(`select * from ${qualified(schema, table)}`);
    if (!this.name.value().trim()) this.name.set_value(this.freshName(table));
    this.confirmEmpty = false;
    this.clashFor = null;
    this.step = 3;
    const path = `/api/tables/${enc(conn)}/${enc(schema)}/${enc(table)}/rows`;
    const grid = new ResultGrid({ id: "import-preview", name: table, load: (q) => api(`${path}${query(q)}`, { timeout: 120 }) });
    this.preview = { grid, table: true };
    grid.fetch(cx);
    cx.notify();
  }

  /** Rows the preview found: a table's count, or the query's rows. Null while unknown. */
  get previewCount() {
    const p = this.preview;
    if (!p?.grid) return null;
    if (p.table) return p.grid.page.total ?? (p.grid.page.has_more || p.grid.loading ? null : (p.grid.table.rows ?? []).length);
    return p.limited ? null : p.rows;
  }

  runPreview(cx) {
    this.tableSel = null; // a query now, even if it began as a table
    const conn = this.source.conn;
    const sql = this.sql.value().trim();
    if (!sql) return;
    this.preview = { loading: true };
    this.confirmEmpty = false;
    this.clashFor = null; // the query may differ now: look the name up again
    cx.spawn(async (cx) => {
      let r = await api(`/api/connections/${enc(conn)}/preview`, { body: { query: sql, limit: 1000 }, timeout: 120 });
      if (!r.ok && r.status === 404 && /not found/i.test(String(r.error))) r = await api("/api/sql", { body: { conn, sql, limit: 1000 }, timeout: 120 });
      if (!r.ok) { this.preview = { error: sentence(r.data?.error ?? r.data?.detail ?? r.error) || "Couldn't run that query." }; cx.notify(); return; }
      const d = r.data;
      this.preview = { grid: new ResultGrid({ id: "import-preview", name: this.name.value().trim() || "preview", table: { columns: d.columns, rows: d.rows, row_count: d.row_count ?? d.rows.length, limited: d.limited ?? d.truncated, limit: 1000 } }), rows: d.row_count ?? d.rows.length, limited: !!(d.limited ?? d.truncated), ms: d.ms };
      cx.notify();
    });
    cx.notify();
  }

  importQuery(cx) {
    const name = this.finalName;
    if (!name) { this.note = { text: "Name the dataset first.", tone: "fail" }; cx.notify(); return; }
    if (this.clash && !this.clash.same && !this.allowNewVersion) { this.note = { text: `${name} already holds other data. Pick another name, or choose "Import as version ${this.clash.version + 1}".`, tone: "fail" }; cx.notify(); return; }
    if (this.previewCount === 0 && !this.confirmEmpty) { this.confirmEmpty = true; cx.notify(); return; }
    const conn = this.source.conn, sql = this.sql.value().trim(), folder = this.target;
    const kind = this.kindHint ?? undefined;
    const previewRows = this.previewCount;
    const whole = this.tableSel ? "the whole table" : null;
    this.busy = true;
    this.note = null;
    cx.spawn(async (cx) => {
      const dest = this.dest;
      const r = await api("/api/sql", { body: { conn, sql, save: name, kind, project: dest.kind === "project" ? dest.id : undefined }, timeout: 600 });
      if (!r.ok) { this.busy = false; this.note = { text: sentence(r.data?.error ?? r.error) || "Couldn't import it.", tone: "fail" }; cx.notify(); return; }
      const ref = r.data.dataset ?? name;
      const saved = ref.replace(/@v\d+$/, "");
      const version = Number(/@v(\d+)$/.exec(ref)?.[1] ?? 1);
      const moved = folder ? await api("/api/library/move", { body: { names: [saved], folder } }) : { ok: true };
      const linked = await this.link([saved]);
      this.busy = false;
      const rows = r.data.row_count ?? 0;
      // Fewer rows than the query asked for: say it's everything there was.
      const asked = Number(/\blimit\s+(\d+)\s*;?\s*$/i.exec(sql)?.[1] ?? 0);
      const all = whole ?? (/^\s*select\s+\*\s+from\s+[^\s]+\s+limit\s+\d+\s*;?\s*$/i.test(sql) ? "the whole table" : "everything the query returned");
      // Stopped at the laptop's row limit: say so, so a cut table is never taken for a whole one.
      const count = r.data.truncated ? `The first ${fmtInt(rows)} rows: this computer's row limit stopped it. Run it on a cloud machine for the rest`
        : `${fmtInt(rows)} row${rows === 1 ? "" : "s"}${(asked && rows < asked) || whole ? `, ${all}` : ""}`;
      const differs = previewRows != null && previewRows !== rows ? ` The preview showed ${fmtInt(previewRows)}: the data changed in between.` : "";
      const forWhom = dest.kind ? `for ${dest.name}` : "";
      const where = !moved.ok ? ` It stayed at the top level: ${sentence(moved.data?.error ?? moved.error) || "the folder move failed."}${forWhom ? ` It's ${forWhom}.` : ""}`
        : folder ? ` In ${folder}${forWhom ? `, ${forWhom}` : ""}.` : forWhom ? ` ${forWhom[0].toUpperCase()}${forWhom.slice(1)}.` : "";
      const body = `${count}.${where}${differs}${linked}`;
      this.finish(saved, `Imported ${saved}, version ${version}`, body, linked || differs || !moved.ok ? "warning" : "success");
      cx.notify();
    });
    cx.notify();
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
      const r = await api("/api/datasets/import", { body: { paths, folder: folderPath(this.newFolder.value()) || this.folder, project: this.dest.kind === "project" ? this.dest.id : undefined } });
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
    return v_flex().id(id).w(176).h(96).p(12).gap(6).justify_between().border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface)
      .when(!soon, (e) => e.cursor_pointer().hover((s) => s.border_color(c.blue)).on_click(onClick))
      .child(h_flex().gap(8).items_center().child(mark).child(div().flex_1().min_w(0).truncate().text_size(13).font_medium().text_color(soon ? c.grey500 : c.navy).child(title)))
      .child(div().text_size(11.5).text_color(c.grey500).child(sub));
  }

  render() {
    const c = palette(isDark(theme()));
    const steps = [[1, "Where from"], [2, "What to bring in"], [3, "Preview and import"]];
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
        .children((this.conns ?? []).map((k) => this.tile(c, `src-${k.name}`, Brand(c, k.kind ?? k.engine, 18), k.name, `${k.engine ?? k.kind ?? "database"}${k.read_only === false ? ", writes allowed" : ""}`,
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
    const target = forQuery ? this.target : folderPath(this.newFolder.value()) || this.folder;
    const typed = !!folderPath(this.newFolder.value());
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
    const v = this.clash?.same ? `, as version ${this.clash.version + 1}` : this.clash && this.allowNewVersion ? `, as version ${this.clash.version + 1}` : "";
    return h_flex().id("import-save-as").gap(6).items_center().text_size(12)
      .child(div().w(60).flex_none().text_color(c.grey900).child("Saved as"))
      .child(div().font_family(MONO).text_color(c.navy).child(this.target ? `${this.target}/${name}` : name))
      .child(div().text_color(c.grey500).child(`${typed && typed !== name ? `(from "${typed}")` : ""}${v}`));
  }

  clashView(c) {
    const k = this.clash;
    const fresh = this.freshName(k.name);
    return v_flex().id("import-clash").gap(6).p(10).border_1().border_color(c.warn).text_size(12)
      .child(div().text_color(c.navy).child(`${k.name} already exists (version ${k.version}, from ${k.source}). Import as a new version, or pick another name.`))
      .child(h_flex().gap(6)
        .child(Button(c, "clash-rename", `Name it ${fresh}`, (_e, cx) => { this.name.set_value(fresh); this.allowNewVersion = false; cx.notify(); }, { kind: "primary", small: true }))
        .child(Button(c, "clash-version", `Import as version ${k.version + 1}`, (_e, cx) => { this.allowNewVersion = true; cx.notify(); }, { small: true, disabled: this.allowNewVersion })));
  }

  emptyView(c) {
    const windowed = timeWindow(this.sql.value());
    return v_flex().id("import-empty").gap(4).p(10).border_1().border_color(c.warn).text_size(12)
      .child(div().text_color(c.navy).child("This query returned no rows. Import anyway?"))
      .when(windowed, (e) => e.child(div().text_color(c.grey900).child("It looks at a recent time window, and this account may have had no events in it. Widen the window in the query and preview again.")))
      .child(div().text_color(c.grey500).child("\"Import anyway\" saves an empty dataset."));
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
    const p = this.preview;
    // Scrolls as a whole: the grid has a fixed height, so it never draws over the rows under it.
    return v_flex().id("import-preview-step").flex_1().min_h(0).overflow_y_scrollbar().p(16).gap(10)
      .child(div().flex_none().h(100).border_1().border_color(c.rule).child(Editor.new(this.sql).size_full()))
      .child(h_flex().flex_none().gap(8).items_center()
        .child(Button(c, "preview-run", p?.table ? "Preview this query instead" : "Preview 1,000 rows", (_e, cx) => this.runPreview(cx), { small: true }))
        .when(!!p?.grid && !p.table, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`${fmtInt(p.rows)} rows${p.ms != null ? ` in ${p.ms} ms` : ""}`)))
        .when(!!p?.table, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`The whole of ${this.tableSel?.table ?? "the table"}, a page at a time. Import brings in every row.`))))
      .when(!!p?.loading, (e) => e.child(Skeleton(c, 6)))
      .when(!!p?.error, (e) => e.child(div().p(10).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).text_color(c.fail).child(p.error)))
      .when(!!p?.grid, (e) => e.child(div().flex_none().border_1().border_color(c.rule).child(p.grid.render(c, { height: 220 }))))
      .child(v_flex().flex_none().gap(8).pt(4).border_t(1).border_color(c.rule)
        .child(h_flex().gap(8).items_center()
          .child(div().w(60).flex_none().text_size(12).text_color(c.grey900).child("Name"))
          .child(h_flex().flex_1().min_w(0).h(28).px(8).items_center().border_1().border_color(c.grey300).child(Input.new(this.name).w_full().text_size(12).text_color(c.navy))))
        .child(this.folderPick(c, true))
        .child(this.destPick(c))
        .child(this.saveLine(c))
        .when(!!this.clash && !this.clash.same, (e) => e.child(this.clashView(c)))
        .when(this.confirmEmpty, (e) => e.child(this.emptyView(c)))
        .child(h_flex().gap(8)
          .child(Button(c, "import-go", this.busy ? "Importing" : this.confirmEmpty ? "Import anyway" : this.clash && this.allowNewVersion ? `Import as version ${this.clash.version + 1}` : p?.table ? "Import this table" : "Import", (_e, cx) => this.importQuery(cx),
            { kind: "primary", small: true, disabled: this.busy || !p?.grid || !this.finalName || (!!this.clash && !this.clash.same && !this.allowNewVersion) }))
          .child(div().text_size(11.5).text_color(c.grey500).child("Imports the query's full result as a write-once dataset. Running it again later makes a new version."))));
  }
}
