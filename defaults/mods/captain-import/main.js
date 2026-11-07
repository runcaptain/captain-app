// CAPTAIN: Import a dataset, in three steps, as an overlay (openOverlay({mod: "captain-import", kind: "dialog"})).
// 1. Where from: a grid of sources. A file or folder from this computer, every connected database with its mark, and
//    "Connect a new source" (the typed database form). Product analytics sources show as coming.
// 2. A database's tables (read once through the table browser) and suggested queries: the backend's own
//    (GET /api/connections/{name}/suggestions) when it has them, else ones made here from the tables.
// 3. Preview up to 1,000 rows (POST /api/connections/{name}/preview, else POST /api/sql), then import it as a dataset
//    into a folder (POST /api/sql {save}, then POST /api/library/move).
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, EditorState, Editor } from "gpui-base";
import { theme, props, closeOverlay, openOverlay, pickFolder, home } from "herdr";
import { api } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { ResultGrid } from "./kit/grid.js";

const enc = encodeURIComponent;
const fmtInt = (n) => Number(n ?? 0).toLocaleString("en-US");
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());
const SOON = [["posthog", "PostHog", "activity"], ["ga", "Google Analytics", "chart-line"]];
const plainSchema = (s) => String(s ?? "").replace(/"/g, "");

/** Queries worth starting from, made from a database's tables when the backend has none to suggest. */
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
    this.note = null;
    this.busy = false;
    this.path = InputState.new({ placeholder: "~/Downloads/catalogs" });
    this.name = InputState.new({ placeholder: "Name the dataset, like products" });
    this.sql = EditorState.new({ language: "sql", value: "", font_family: MONO, font_size: 13, line_numbers: true });
    this.path.on("submit", (_e, cx) => this.importFiles(cx, [expand(this.path.value())]));
    cx.spawn(async (cx) => {
      const [k, t] = await Promise.all([api("/api/connections"), api("/api/datasets/tree")]);
      this.conns = k.ok && Array.isArray(k.data) ? k.data : [];
      if (t.ok) { const walk = (n) => { for (const f of n.folders ?? []) { this.folders.push(f.path); walk(f); } }; walk(t.data ?? {}); }
      cx.notify();
    });
    cx.timer.every(500, (cx) => this.tick(cx));
  }

  close() { closeOverlay(props().overlay); }

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
      this.suggestions = s.ok && Array.isArray(s.data) ? s.data.map((x) => ({ title: x.title, sql: x.sql, why: x.why })) : localSuggestions(t.ok ? t.data : null);
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
          this.busy = false;
          const res = r.data.result ?? {};
          const n = (res.imported ?? []).length + (res.updated ?? []).length;
          this.note = { text: `Added ${n} dataset${n === 1 ? "" : "s"}${(res.skipped ?? []).length ? `, skipped ${res.skipped.length}: ${res.skipped.map((x) => sentence(x.reason)).join(" ")}` : "."}`, tone: n ? "good" : "muted" };
        } else if (r.ok && r.data.status === "failed") {
          this.importJob = null;
          this.busy = false;
          this.note = { text: sentence(r.data.error ?? r.data.message) || "The import stopped.", tone: "fail" };
        }
        cx.notify();
      });
    }
  }

  useQuery(cx, sql, name) {
    this.sql.set_value(sql);
    if (name && !this.name.value().trim()) this.name.set_value(name);
    this.step = 3;
    this.runPreview(cx);
  }

  runPreview(cx) {
    const conn = this.source.conn;
    const sql = this.sql.value().trim();
    if (!sql) return;
    this.preview = { loading: true };
    cx.spawn(async (cx) => {
      let r = await api(`/api/connections/${enc(conn)}/preview`, { body: { sql, limit: 1000 }, timeout: 120 });
      if (!r.ok && r.status === 404 && /not found/i.test(String(r.error))) r = await api("/api/sql", { body: { conn, sql, limit: 1000 }, timeout: 120 });
      if (!r.ok) { this.preview = { error: sentence(r.data?.error ?? r.data?.detail ?? r.error) || "Couldn't run that query." }; cx.notify(); return; }
      const d = r.data;
      this.preview = { grid: new ResultGrid({ id: "import-preview", name: this.name.value().trim() || "preview", table: { columns: d.columns, rows: d.rows, row_count: d.row_count ?? d.rows.length, limited: d.truncated, limit: 1000 } }), rows: d.row_count ?? d.rows.length, ms: d.ms };
      cx.notify();
    });
    cx.notify();
  }

  importQuery(cx) {
    const name = this.name.value().trim().replace(/\s+/g, "-");
    if (!name) { this.note = { text: "Name the dataset first.", tone: "fail" }; cx.notify(); return; }
    const conn = this.source.conn, sql = this.sql.value().trim(), folder = this.folder;
    this.busy = true;
    cx.spawn(async (cx) => {
      const r = await api("/api/sql", { body: { conn, sql, save: name }, timeout: 600 });
      if (!r.ok) { this.busy = false; this.note = { text: sentence(r.data?.error ?? r.error) || "Couldn't import it.", tone: "fail" }; cx.notify(); return; }
      const ref = r.data.dataset ?? name;
      if (folder) await api("/api/library/move", { body: { names: [ref.replace(/@v\d+$/, "")], folder } });
      this.busy = false;
      this.note = { text: `Imported ${ref.replace(/@v(\d+)$/, " (version $1)")}${folder ? ` into ${folder}` : ""}: ${fmtInt(r.data.row_count)} rows.`, tone: "good" };
      this.done = true;
      cx.notify();
    });
    cx.notify();
  }

  importFiles(cx, paths) {
    paths = paths.filter(Boolean);
    if (!paths.length) return;
    this.busy = true;
    cx.spawn(async (cx) => {
      const r = await api("/api/datasets/import", { body: { paths, folder: this.folder } });
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
    return v_flex().id("import").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
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
        .children(SOON.map(([id, label, ic]) => this.tile(c, `src-${id}`, this.icon(ic, 18, c.grey500), label, "Coming soon", null, { soon: true })))
        .child(this.tile(c, "src-new", this.icon("plus", 18, c.blue), "Connect a new source", "A warehouse or database", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "database" } }))))
      .when(this.conns === null, (e) => e.child(Skeleton(c, 2)))
      .when(file, (e) => e.child(v_flex().gap(8).p(14).border_1().border_color(c.rule)
        .child(div().text_size(12.5).font_medium().child("Which files"))
        .child(h_flex().gap(8).items_center()
          .child(h_flex().flex_1().min_w(0).h(28).px(8).items_center().border_1().border_color(c.grey300).child(Input.new(this.path).w_full().text_size(12).text_color(c.navy)))
          .child(Button(c, "file-pick", "Choose a folder", (_e, cx) => cx.spawn(async (cx) => { const p = await pickFolder({ prompt: "Import" }); if (p) this.importFiles(cx, [p]); }), { small: true }))
          .child(Button(c, "file-go", this.busy ? "Importing" : "Import", (_e, cx) => this.importFiles(cx, [expand(this.path.value())]), { kind: "primary", small: true, disabled: this.busy })))
        .child(this.folderPick(c))
        .child(div().text_size(11.5).text_color(c.grey500).child("You can also drag files from Finder onto Data and signals."))));
  }

  folderPick(c) {
    return h_flex().id("import-folder").gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
      .child(div().flex_none().text_color(c.grey500).child("Into"))
      .children(this.folders.map((f, i) => div().id(`folder-${i}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
        .border_1().border_color(this.folder === f ? c.blue : c.grey300).text_color(this.folder === f ? c.blue : c.grey900)
        .on_click((_e, cx) => { this.folder = f; cx.notify(); }).child(f || "Top level")));
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
            .on_click((_e, cx) => this.useQuery(cx, `select * from ${plainSchema(s.name).split(".").pop()}.${x.name} limit 1000`, x.name))
            .child(this.icon("table", 12, c.grey500)).child(div().flex_1().min_w(0).truncate().text_size(12).font_family(MONO).child(x.name))),
        ])))
      .child(v_flex().id("import-suggest").flex_1().min_w(0).h_full().overflow_y_scrollbar().p(16).gap(10)
        .child(div().text_size(13).font_medium().child("Suggested queries"))
        .child(div().text_size(12).text_color(c.grey900).child("Start from one of these, or write your own on the next step."))
        .when(this.suggestions === null, (e) => e.child(Skeleton(c, 4)))
        .when(Array.isArray(this.suggestions) && !this.suggestions.length, (e) => e.child(div().text_size(12).text_color(c.grey500).child("Read its tables to get suggestions.")))
        .children((this.suggestions ?? []).map((s, i) => v_flex().id(`sug-${i}`).gap(4).p(10).cursor_pointer().border_1().border_color(c.rule).hover((st) => st.border_color(c.blue))
          .on_click((_e, cx) => this.useQuery(cx, s.sql, s.table))
          .child(div().text_size(12.5).font_medium().child(s.title))
          .when(!!s.why, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(s.why)))
          .child(div().text_size(11.5).font_family(MONO).text_color(c.grey900).truncate().child(s.sql))))
        .child(Button(c, "write-own", "Write my own query", (_e, cx) => { this.step = 3; cx.notify(); }, { small: true })));
  }

  previewStep(c) {
    const p = this.preview;
    return v_flex().id("import-preview-step").flex_1().min_h(0).p(16).gap(10)
      .child(div().flex_none().h(130).border_1().border_color(c.rule).child(Editor.new(this.sql).size_full()))
      .child(h_flex().flex_none().gap(8).items_center()
        .child(Button(c, "preview-run", "Preview 1,000 rows", (_e, cx) => this.runPreview(cx), { small: true }))
        .when(!!p?.grid, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`${fmtInt(p.rows)} rows${p.ms != null ? ` in ${p.ms} ms` : ""}`))))
      .when(!!p?.loading, (e) => e.child(Skeleton(c, 6)))
      .when(!!p?.error, (e) => e.child(div().p(10).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).text_color(c.fail).child(p.error)))
      .when(!!p?.grid, (e) => e.child(div().flex_1().min_h(0).border_1().border_color(c.rule).child(p.grid.render(c, { height: 220 }))))
      .child(v_flex().flex_none().gap(8).pt(4).border_t(1).border_color(c.rule)
        .child(h_flex().gap(8).items_center()
          .child(div().w(60).flex_none().text_size(12).text_color(c.grey900).child("Name"))
          .child(h_flex().flex_1().min_w(0).h(28).px(8).items_center().border_1().border_color(c.grey300).child(Input.new(this.name).w_full().text_size(12).text_color(c.navy))))
        .child(this.folderPick(c))
        .child(h_flex().gap(8)
          .child(Button(c, "import-go", this.busy ? "Importing" : this.done ? "Import again" : "Import", (_e, cx) => this.importQuery(cx), { kind: "primary", small: true, disabled: this.busy || !p?.grid }))
          .when(!!this.done, (e) => e.child(Button(c, "import-close-done", "Done", () => this.close(), { small: true })))
          .child(div().text_size(11.5).text_color(c.grey500).child("Imports the query's full result as a write-once dataset. Running it again later makes a new version."))));
  }
}
