// CAPTAIN: Data and signals, Notebooks tab. Querybook-style docs, native: SQL cells on a connection (each result is a
// DataFrame in the notebook's Python), Python cells on the notebook's Jupyter kernel, text cells in markdown, and charts
// drawn from a result. Cmd-Enter runs a cell, Shift-Enter runs it and moves on, Cmd-S saves a version. Drafts save
// themselves. Files: open, import and export the backend's eight notebook formats.
//
// Backend routes: GET /api/notebooks, GET|PUT /api/notebooks/{name}[/draft], POST .../save, POST .../import,
// GET .../export?format=, GET /api/notebook-formats, GET /api/connections, and the kernel (kit/kernel.js).
// Writes through a SQL connection are refused unless that connection is writable: the backend's read-only guard.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar, InputState, Input, EditorState, Editor } from "gpui-base";
import { readTextFile, writeTextFile, home, theme } from "herdr";
import { api } from "./kit/cst.js";
import { MONO, palette, isDark } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Kernel } from "./kit/kernel.js";
import { fromNotebook, toNotebook, newCell, tablesByVar, errorSentence } from "./kit/notebook.js";
import { ChartView, MARKS, AGGS, COLORS as CHART_COLORS, shape as shapeChart } from "./kit/charts.js";
import { ResultGrid } from "./kit/grid.js";
import { askAgent, copyPrompt } from "./kit/agent.js";
import { SqlTools } from "./sql-tools.js";

const KINDS = { sql: ["SQL", "database"], python: ["Python", "terminal"], markdown: ["Text", "file-text"], chart: ["Chart", "bar-chart-3"] };
const LANG = { sql: "sql", python: "python", markdown: "markdown" };
const ROW = 26;
const LIMIT = 1000; // Querybook's default row limit for a query cell
const LIMITS = [100, 1000, 10000, 100000];
const COL = 150;
const enc = (name) => String(name).split("/").map(encodeURIComponent).join("/");
const fmtInt = (n) => Number(n ?? 0).toLocaleString("en-US");
const secs = (ms) => (ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`);
const cellText = (v) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());
const plainNote = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };

export class Notebooks {
  constructor() {
    this.list = null;
    this.conns = null; // null until listed: never "No database connected" from a list that wasn't fetched
    this.connsError = null;
    this.formats = [];
    this.open = null; // {name, version, base, draft}
    this.cells = [];
    this.state = new Map(); // cell id -> {status: "running" | "done" | "failed" | "stopped", started, ms}
    this.editors = new Map(); // cell id -> EditorState
    this.inputs = new Map(); // cell id -> InputState (SQL result name)
    this.grids = new Map(); // output key -> ResultGrid
    this.editing = new Set(); // markdown cells shown as source
    this.kernel = null;
    this.kstate = "off";
    this.dirty = false;
    this.lastEdit = 0;
    this.savingDraft = false;
    this.note = null; // {text, tone}
    this.exporting = false;
    this.running = false;
    this.execCount = 0;
    this.newName = InputState.new({ placeholder: "Name a new notebook" });
    this.importPath = InputState.new({ placeholder: "~/Downloads/analysis.ipynb" });
    this.sqlTools = new SqlTools(this);
  }

  // ---- data ------------------------------------------------------------------------------------------------------

  load(cx) {
    cx.spawn(async (cx) => {
      const seq = (this.connSeq = (this.connSeq ?? 0) + 1);
      const [nb, cs, fs] = await Promise.all([api("/api/notebooks"), api("/api/connections"), api("/api/notebook-formats")]);
      this.list = nb.ok && Array.isArray(nb.data) ? nb.data : [];
      // A notebook made before the list arrived (from a table's example query) stays in it.
      if (this.open?.name && !this.list.some((n) => n.name === this.open.name)) this.list.unshift({ name: this.open.name, latest: null, has_draft: true });
      this.listError = nb.ok ? null : "Couldn't load notebooks.";
      if (seq === this.connSeq) this.setConns(cs);
      this.formats = fs.ok && Array.isArray(fs.data) ? fs.data.filter((f) => f.export) : [];
      const want = this.want && this.list.some((n) => n.name === this.want) ? this.want : this.list[0]?.name;
      this.want = null;
      if (!this.open && want) this.openNotebook(cx, want);
      cx.notify();
    });
  }

  setConns(r) {
    if (r.ok && Array.isArray(r.data)) { this.conns = r.data; this.connsError = null; }
    else if (!r.starting) this.connsError = "Couldn't list the databases. Refresh to try again.";
  }

  /** Relist the connections (the tab was opened, or one was connected or removed elsewhere). */
  loadConns(cx) {
    const seq = (this.connSeq = (this.connSeq ?? 0) + 1);
    cx.spawn(async (cx) => {
      const r = await api("/api/connections");
      if (seq !== this.connSeq) return; // only the newest answer counts
      this.setConns(r);
      if (r.starting) cx.timer.after(1000, (cx) => { if (seq === this.connSeq) this.loadConns(cx); });
      cx.notify();
    });
  }

  openNotebook(cx, name) {
    this.flushDraft(cx);
    this.kernel?.close();
    this.kernel = null;
    this.kstate = "off";
    this.open = { name, version: null, base: null, loading: true };
    this.cells = [];
    this.state.clear();
    this.grids.clear();
    this.releaseEditors();
    this.note = null;
    this.exporting = false;
    this.sqlTools.cell = null;
    this.sqlTools.varInputs.clear();
    cx.spawn(async (cx) => {
      const r = await api(`/api/notebooks/${enc(name)}`);
      if (this.open?.name !== name) return;
      if (!r.ok) { this.open = { name, error: "Couldn't open this notebook." }; cx.notify(); return; }
      this.open = { name, version: r.data.version, base: r.data.notebook, draft: !!r.data.draft };
      this.cells = fromNotebook(r.data.notebook);
      if (!this.cells.length) this.cells = [newCell("sql", [], this.conns?.[0]?.name)];
      // Opened from a table's example query: that query, on its connection, as the first cell.
      if (this.seedSql) {
        const cell = newCell("sql", [], this.seedSql.conn);
        cell.source = this.seedSql.sql;
        this.cells = [cell];
        this.seedSql = null;
        this.edited();
      }
      for (const cell of this.cells) this.prepare(cell);
      if (this.runOnOpen) { this.runOnOpen = false; this.runAll(cx); }
      const sqlCell = this.toolsOnOpen && this.cells.find((x) => x.kind === "sql");
      if (sqlCell) { this.sqlTools.cell = sqlCell.id; if (this.toolsOnOpen.panel) this.sqlTools.show(cx, sqlCell, this.toolsOnOpen.panel); this.toolsOnOpen = null; }
      cx.notify();
    });
    cx.notify();
  }

  newNotebook(cx) {
    const name = this.newName.value().trim().replace(/\s+/g, "-");
    if (!name) { this.note = { text: "Give the notebook a name first.", tone: "fail" }; cx.notify(); return; }
    this.newName.set_value("");
    this.list ??= [];
    if (!this.list.some((n) => n.name === name)) this.list = [{ name, latest: null, has_draft: true }, ...this.list];
    this.openNotebook(cx, name);
    this.dirty = true;
  }

  releaseEditors() {
    for (const e of this.editors.values()) e.release();
    for (const i of this.inputs.values()) i.release();
    this.editors.clear();
    this.inputs.clear();
    this.editing.clear();
  }

  edited() {
    this.dirty = true;
    this.lastEdit = Date.now();
  }

  /** Every half second: autosave a draft 1.5 s after the last edit, and redraw while a cell runs (its clock). */
  tick(cx) {
    if (this.open?.base !== undefined && this.dirty && !this.savingDraft && Date.now() - this.lastEdit > 1500) this.flushDraft(cx);
    if ([...this.state.values()].some((s) => s.status === "running")) cx.notify();
  }

  flushDraft(cx) {
    if (!this.open?.name || !this.dirty || this.open.error) return;
    const name = this.open.name;
    const nb = toNotebook(this.cells, this.open.base);
    this.dirty = false;
    this.savingDraft = true;
    cx.spawn(async (cx) => {
      const r = await api(`/api/notebooks/${enc(name)}/draft`, { method: "PUT", body: { notebook: nb } });
      this.savingDraft = false;
      if (!r.ok) { this.dirty = true; this.note = { text: "Couldn't save the draft. Captain will try again.", tone: "fail" }; }
      else if (this.open?.name === name) this.open.draft = true;
      cx.notify();
    });
  }

  save(cx) {
    if (!this.open?.name || this.open.error) return;
    const name = this.open.name;
    const nb = toNotebook(this.cells, this.open.base);
    this.note = { text: "Saving", tone: "muted" };
    cx.spawn(async (cx) => {
      const r = await api(`/api/notebooks/${enc(name)}/save`, { body: { notebook: nb, message: "" } });
      if (this.open?.name !== name) return;
      if (r.ok) {
        this.open.version = r.data.version;
        this.open.draft = false;
        this.dirty = false;
        this.note = { text: `Saved as version ${r.data.version}.`, tone: "good" };
        const row = this.list?.find((n) => n.name === name);
        if (row) Object.assign(row, { latest: r.data.version, has_draft: false });
      } else this.note = { text: plainNote(r.error) || "Couldn't save.", tone: "fail" };
      cx.notify();
    });
    cx.notify();
  }

  importFile(cx) {
    const path = expand(this.importPath.value());
    if (!path) { this.note = { text: "Type the path of a notebook file first.", tone: "fail" }; cx.notify(); return; }
    cx.spawn(async (cx) => {
      const text = await readTextFile(path);
      if (text === null) { this.note = { text: `There's no file at ${this.importPath.value().trim()}, or it's over 1 MB.`, tone: "fail" }; cx.notify(); return; }
      const filename = path.split("/").pop();
      const name = filename.replace(/\.[^.]+$/, "").replace(/\s+/g, "-");
      const r = await api(`/api/notebooks/${enc(name)}/import`, { body: { filename, content: text } });
      if (!r.ok) { this.note = { text: plainNote(r.error) || "Couldn't read that notebook.", tone: "fail" }; cx.notify(); return; }
      this.importPath.set_value("");
      const fmt = this.formatLabel(r.data.format);
      this.load(cx);
      this.openNotebook(cx, r.data.name);
      this.note = { text: [`Imported ${filename} (${fmt}) as version ${r.data.version}.`, ...(r.data.warnings ?? []).map(plainNote)].join(" "), tone: "good" };
      cx.notify();
    });
  }

  exportFile(cx, fmt) {
    const name = this.open?.name;
    if (!name) return;
    this.exporting = false;
    cx.spawn(async (cx) => {
      if (this.dirty) this.flushDraft(cx);
      const r = await api(`/api/notebooks/${enc(name)}/export?format=${encodeURIComponent(fmt)}`);
      if (!r.ok) { this.note = { text: this.open?.version ? plainNote(r.error) || "Couldn't export." : "Save a version first, then export it.", tone: "fail" }; cx.notify(); return; }
      const path = `${home()}/Downloads/${r.data.filename}`;
      try {
        const old = await readTextFile(path);
        await writeTextFile(path, r.data.content, old);
        this.note = { text: `Exported to ~/Downloads/${r.data.filename}.`, tone: "good" };
      } catch {
        this.note = { text: `Couldn't write ~/Downloads/${r.data.filename}.`, tone: "fail" };
      }
      cx.notify();
    });
  }

  formatLabel(id) {
    return this.formats.find((f) => f.id === id)?.label ?? id;
  }

  // ---- running ---------------------------------------------------------------------------------------------------

  ensureKernel() {
    if (!this.kernel) this.kernel = new Kernel(this.open.name, (m, cx) => this.onMsg(m, cx), (s) => { this.kstate = s; });
    return this.kernel;
  }

  onMsg(m, cx) {
    const cell = this.cells.find((c) => c.id === m.cell_id);
    if (!cell) return;
    if (m.type === "stream") {
      const last = cell.outputs[cell.outputs.length - 1];
      if (last?.kind === "stream" && last.name === m.name) last.text += m.text;
      else cell.outputs.push({ kind: "stream", name: m.name, text: m.text });
    } else if (m.type === "table") {
      cell.outputs.push({ kind: "table", table: { var: m.var, columns: m.columns, rows: m.rows, row_count: m.row_count, truncated: m.truncated, limited: !!m.limited, limit: m.limit ?? cell.meta.limit ?? LIMIT } });
      // More rows than came back, held in a variable: the grid pages, sorts and filters all of them in Python.
      if (m.var && (m.row_count ?? 0) > (m.rows ?? []).length && cx) {
        const key = `${cell.id}-${cell.outputs.length - 1}`;
        const kernel = this.kernel;
        const g = new ResultGrid({ id: key, name: `${this.open?.name ?? "notebook"}-${m.var}`, source: "this notebook's Python",
          load: (q) => kernel.page(cx, { var: m.var, ...q }) });
        this.grids.set(key, g);
        g.fetch(cx);
      }
    } else if (m.type === "display") {
      cell.outputs.push({ kind: "display", mime: m.mime, data: m.data });
    } else if (m.type === "error") {
      cell.outputs.push({ kind: "error", ename: m.ename, evalue: m.evalue, traceback: m.traceback ?? [] });
    }
  }

  /** Runs one cell. Resolves true when it worked. Text cells and charts need no kernel. */
  async run(cx, cell) {
    if (cell.kind === "markdown") { this.editing.delete(cell.id); cx.notify(); return true; }
    if (cell.kind === "chart") return true;
    if (cell.kind === "sql" && !cell.meta.conn) {
      this.state.set(cell.id, { status: "failed" });
      cell.outputs = [{ kind: "error", ename: "", evalue: "Pick a connection for this SQL cell first.", traceback: [] }];
      cx.notify();
      return false;
    }
    cell.source = this.editors.get(cell.id)?.value() ?? cell.source;
    let code = cell.source;
    if (cell.kind === "sql") {
      try { code = this.sqlTools.rendered(cell); } catch (e) {
        this.state.set(cell.id, { status: "failed" });
        cell.outputs = [{ kind: "error", ename: "", evalue: e.message, traceback: [] }];
        cx.notify();
        return false;
      }
    }
    cell.outputs = [];
    for (const k of [...this.grids.keys()]) if (k.startsWith(`${cell.id}-`)) this.grids.delete(k);
    this.state.set(cell.id, { status: "running", started: Date.now() });
    cx.notify();
    let r;
    try {
      r = await this.ensureKernel().execute(cx, { cell_id: cell.id, kind: cell.kind, code, n: ++this.execCount, conn: cell.meta.conn ?? undefined, var: cell.meta.var ?? undefined, limit: cell.kind === "sql" ? cell.meta.limit ?? LIMIT : undefined });
    } catch (e) {
      r = { ok: false, ms: 0, lost: true };
      cell.outputs.push({ kind: "error", ename: "", evalue: String(e?.message ?? "Couldn't start Python for this notebook."), traceback: [] });
    }
    const st = this.state.get(cell.id);
    const stopped = cell.outputs.some((o) => o.kind === "error" && o.ename === "KeyboardInterrupt");
    this.state.set(cell.id, { status: r.ok ? "done" : stopped ? "stopped" : "failed", ms: r.ms || (st?.started ? Date.now() - st.started : 0) });
    if (r.lost && !cell.outputs.some((o) => o.kind === "error")) cell.outputs.push({ kind: "error", ename: "", evalue: "Python stopped while this cell ran. Run it again to start a fresh session.", traceback: [] });
    cell.execCount = this.execCount;
    this.edited();
    cx.notify();
    return r.ok;
  }

  runCell(cx, cell, next = false) {
    cx.spawn(async (cx) => {
      await this.run(cx, cell);
      if (next) {
        const i = this.cells.indexOf(cell);
        if (i === this.cells.length - 1) this.add(cx, cell.kind === "markdown" ? "python" : cell.kind);
        this.focusHint = this.cells[i + 1]?.id;
        cx.notify();
      }
    });
  }

  runAll(cx) {
    if (this.running) return;
    this.running = true;
    cx.spawn(async (cx) => {
      for (const cell of [...this.cells]) {
        if (!this.running) break;
        if (!(await this.run(cx, cell))) break;
      }
      this.running = false;
      cx.notify();
    });
    cx.notify();
  }

  stop() {
    this.running = false;
    this.kernel?.interrupt();
  }

  // ---- editing ---------------------------------------------------------------------------------------------------

  add(cx, kind, after) {
    const cell = newCell(kind, this.cells, this.conns?.[0]?.name);
    if (kind === "chart") {
      const vars = [...tablesByVar(this.cells).keys()];
      cell.meta.chart = { mark: "bar", data: vars[vars.length - 1] };
    }
    const i = after ? this.cells.indexOf(after) + 1 : this.cells.length;
    this.cells.splice(i, 0, cell);
    this.prepare(cell);
    if (kind === "markdown") this.editing.add(cell.id);
    this.edited();
    cx.notify();
  }

  remove(cx, cell) {
    this.cells = this.cells.filter((c) => c !== cell);
    this.editors.get(cell.id)?.release();
    this.editors.delete(cell.id);
    this.state.delete(cell.id);
    this.edited();
    cx.notify();
  }

  move(cx, cell, by) {
    const i = this.cells.indexOf(cell), j = i + by;
    if (j < 0 || j >= this.cells.length) return;
    [this.cells[i], this.cells[j]] = [this.cells[j], this.cells[i]];
    this.edited();
    cx.notify();
  }

  /** A cell's editor and name field. The app only lets state be made outside drawing: on load, on add. */
  prepare(cell) {
    const c = palette(isDark(theme()));
    if (cell.kind !== "chart" && !this.editors.has(cell.id)) {
      const st = EditorState.new({
        language: LANG[cell.kind] ?? "python", value: cell.source, font_family: MONO, font_size: 13, line_numbers: true, soft_wrap: cell.kind === "markdown",
        syntax: { keyword: c.blue, string: c.good, comment: c.grey500, number: c.warn, function: c.navy, type: c.navy, constant: c.warn, boolean: c.warn, operator: c.grey900 },
      });
      st.on("change", (_e, cx) => { cell.source = st.value(); this.edited(); cx.notify(); });
      this.editors.set(cell.id, st);
    }
    if (cell.kind === "sql" && !this.inputs.has(cell.id)) {
      const st = InputState.new({ placeholder: "df_1", value: cell.meta.var ?? "" });
      st.on("change", (_e, cx) => { cell.meta.var = st.value().trim().replace(/[^A-Za-z0-9_]/g, "_") || null; this.edited(); cx.notify(); });
      this.inputs.set(cell.id, st);
    }
  }

  onKey(e, cx) {
    if (e.keystroke === "cmd-s") { this.flushDraft(cx); this.save(cx); }
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  icon(name, size, color) {
    // chevron-up isn't in the shared set yet: it ships with this mod.
    return svg(name === "chevron-up" ? "mods/captain-data/icons/chevron-up.svg" : icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  render(c, w) {
    const lw = Math.max(200, Math.min(260, Math.floor(w * 0.2)));
    return h_flex().id("notebooks").flex_1().min_h(0)
      .on_key_down((e, cx) => this.onKey(e, cx))
      .child(this.side(c, lw))
      .child(this.body(c, w - lw));
  }

  field(c, id, st, iconName, buttonLabel, onGo) {
    return h_flex().gap(6).items_center()
      .child(h_flex().id(id).flex_1().min_w(0).h(28).px(8).gap(6).items_center().border_1().border_color(c.grey300)
        .child(this.icon(iconName, 13, c.grey500)).child(div().flex_1().min_w(0).child(Input.new(st).w_full().text_size(12).text_color(c.navy))))
      .child(Button(c, `${id}-go`, buttonLabel, (_e, cx) => onGo(cx), { small: true }));
  }

  side(c, w) {
    const list = this.list;
    return v_flex().id("nb-list").w(w).h_full().flex_none().border_r(1).border_color(c.rule)
      .child(v_flex().flex_none().gap(8).p(10).border_b(1).border_color(c.rule)
        .child(this.field(c, "nb-new", this.newName, "plus", "New", (cx) => this.newNotebook(cx)))
        .child(this.field(c, "nb-import", this.importPath, "file-text", "Import", (cx) => this.importFile(cx)))
        .child(div().text_size(11).text_color(c.grey500).child(`Jupyter, Jupytext, Databricks, marimo, Quarto, R Markdown, MyST or Zeppelin.`)))
      .child(v_flex().id("nb-names").flex_1().min_h(0).overflow_y_scrollbar()
        .when(list === null, (e) => e.child(Skeleton(c, 6)))
        .when(!!this.listError, (e) => e.child(div().p(12).text_size(12.5).text_color(c.fail).child(this.listError)))
        .when(Array.isArray(list) && !list.length, (e) => e.child(div().p(12).text_size(12).line_height("18px").text_color(c.grey900)
          .child("No notebooks yet. Name one above, or import one you already have.")))
        .children((list ?? []).map((n) => {
          const on = this.open?.name === n.name;
          return h_flex().id(`nb-${n.name}`).h(38).flex_none().px(10).gap(8).items_center().cursor_pointer()
            .border_l(2).border_color(on ? c.blue : "#00000000").bg(on ? c.blueLight : c.surface).hover((s) => (on ? s : s.bg(c.surface2)))
            .tooltip(n.name)
            .on_click((_e, cx) => this.openNotebook(cx, n.name))
            .child(this.icon("file-text", 14, on ? c.blue : c.grey500))
            .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_family(MONO).text_color(on ? c.blue : c.navy).child(n.name))
            .when(n.latest != null, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10.5).font_family(MONO).text_color(c.grey900).child(`v${n.latest}`)))
            .when(!!n.has_draft, (e) => e.child(div().flex_none().text_size(10.5).text_color(c.grey500).child("draft")));
        })));
  }

  kernelWords() {
    return { off: "Python starts when you run a cell", connecting: "Starting Python", idle: "Python ready", busy: "Running", lost: "Python stopped" }[this.kstate] ?? "";
  }

  body(c, w) {
    const o = this.open;
    if (!o) return v_flex().flex_1().items_center().justify_center().gap(10).p(24)
      .child(this.icon("file-text", 22, c.grey500))
      .child(div().text_size(15).font_medium().child("Notebooks"))
      .child(div().max_w(460).text_center().text_size(12.5).line_height("19px").text_color(c.grey900)
        .child("Query a warehouse in SQL, work on the results in Python, write down what you found, and chart it. Each save is a version you can come back to."));
    if (o.loading) return v_flex().flex_1().child(Skeleton(c, 10));
    if (o.error) return v_flex().flex_1().p(24).text_size(13).text_color(c.fail).child(o.error);
    const busy = this.kstate === "busy" || this.running;
    return v_flex().flex_1().min_w(0).h_full()
      .child(h_flex().flex_none().flex_wrap().gap(8).px(20).py(10).items_center().border_b(1).border_color(c.rule)
        .child(this.icon("file-text", 16, c.navy))
        .child(div().min_w(0).truncate().text_size(15).font_medium().font_family(MONO).child(o.name))
        .when(o.version != null, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10.5).font_family(MONO).text_color(c.grey900).child(`v${o.version}`)))
        .child(h_flex().gap(5).items_center().text_size(11.5).text_color(c.grey500)
          .child(div().size(7).rounded_full().bg(this.kstate === "busy" ? c.blue : this.kstate === "idle" ? c.good : this.kstate === "lost" ? c.fail : c.grey300))
          .child(div().child(this.kernelWords())))
        .child(div().text_size(11.5).text_color(c.grey500).child(this.dirty || this.savingDraft ? "Saving draft" : o.draft ? "Draft saved" : ""))
        .child(div().flex_1())
        .child(busy ? Button(c, "nb-stop", "Stop", () => this.stop(), { small: true }) : Button(c, "nb-run-all", "Run all", (_e, cx) => this.runAll(cx), { small: true }))
        .child(Button(c, "nb-export", "Export", (_e, cx) => { this.exporting = !this.exporting; cx.notify(); }, { small: true }))
        .child(Button(c, "nb-save", "Save version", (_e, cx) => { this.flushDraft(cx); this.save(cx); }, { kind: "primary", small: true })))
      .when(this.exporting, (e) => e.child(h_flex().flex_none().flex_wrap().gap(6).px(20).py(8).items_center().border_b(1).border_color(c.rule).bg(c.surface2)
        .child(div().text_size(12).text_color(c.grey900).child("Export the saved version as"))
        .children(this.formats.map((f) => Button(c, `nb-fmt-${f.id}`, f.label, (_e, cx) => this.exportFile(cx, f.id), { kind: "quiet", small: true })))))
      .when(!!this.note, (e) => e.child(h_flex().flex_none().px(20).py(6).gap(8).items_center().text_size(12)
        .text_color(this.note.tone === "fail" ? c.fail : this.note.tone === "good" ? c.good : c.grey900)
        .child(div().flex_1().min_w(0).child(this.note.text))
        .child(div().id("nb-note-x").cursor_pointer().on_click((_e, cx) => { this.note = null; cx.notify(); }).child(this.icon("x", 12)))))
      .child(v_flex().id("nb-cells").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(12).gap(12)
        .children(this.cells.map((cell, i) => this.cell(c, cell, i, w - 40)))
        .child(h_flex().flex_none().gap(8).pt(4).pb(24)
          .child(div().text_size(12).text_color(c.grey500).child("Add"))
          .children(Object.entries(KINDS).map(([k, [label, ic]]) => h_flex().id(`nb-add-${k}`).h(26).px(8).gap(6).items_center().cursor_pointer()
            .border_1().border_color(c.grey300).text_size(12).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue))
            .on_click((_e, cx) => this.add(cx, k)).child(this.icon(ic, 12)).child(div().child(label))))));
  }

  status(c, cell) {
    const s = this.state.get(cell.id);
    if (!s) return div();
    const rows = cell.outputs.find((o) => o.kind === "table")?.table;
    if (s.status === "running") return h_flex().gap(5).items_center().text_size(11.5).text_color(c.blue)
      .child(div().size(7).rounded_full().bg(c.blue)).child(div().child(`Running, ${secs(Date.now() - s.started)}`));
    const word = s.status === "done" ? "Done" : s.status === "stopped" ? "Stopped" : "Failed";
    return h_flex().gap(6).items_center().text_size(11.5).text_color(s.status === "failed" ? c.fail : c.grey900)
      .child(this.icon(s.status === "done" ? "circle-check" : "circle-x", 12))
      .child(div().child([word, s.ms ? secs(s.ms) : null, rows ? `${fmtInt(rows.row_count)} row${rows.row_count === 1 ? "" : "s"}` : null].filter(Boolean).join(", ")));
  }

  cell(c, cell, i, w) {
    const [label, ic] = KINDS[cell.kind] ?? KINDS.python;
    const running = this.state.get(cell.id)?.status === "running";
    const code = cell.kind === "sql" || cell.kind === "python";
    const head = h_flex().flex_none().h(34).px(10).gap(8).items_center().bg(c.surface2).border_b(1).border_color(c.rule)
      .child(this.icon(ic, 13, c.navy)).child(div().text_size(12).font_medium().child(label))
      .child(div().flex_1())
      .child(this.status(c, cell))
      .when(cell.kind === "sql", (e) => e.child(this.tool(c, `tools-${cell.id}`, "settings", "Format, explain, translate, snippets and history", (_e, cx) => this.sqlTools.toggle(cx, cell))))
      .when(code && !running, (e) => e.child(this.tool(c, `run-${cell.id}`, "play", "Run (Cmd-Enter)", (_e, cx) => this.runCell(cx, cell))))
      .when(running, (e) => e.child(this.tool(c, `stop-${cell.id}`, "circle-x", "Stop", () => this.stop())))
      .when(cell.kind === "markdown", (e) => e.child(this.tool(c, `edit-${cell.id}`, this.editing.has(cell.id) ? "check" : "file-text", this.editing.has(cell.id) ? "Done" : "Edit",
        (_e, cx) => { if (this.editing.has(cell.id)) this.editing.delete(cell.id); else this.editing.add(cell.id); cx.notify(); })))
      .child(this.tool(c, `up-${cell.id}`, "chevron-up", "Move up", (_e, cx) => this.move(cx, cell, -1), i === 0))
      .child(this.tool(c, `down-${cell.id}`, "chevron-down", "Move down", (_e, cx) => this.move(cx, cell, 1), i === this.cells.length - 1))
      .child(this.tool(c, `del-${cell.id}`, "x", "Delete cell", (_e, cx) => this.remove(cx, cell)));
    const lines = Math.max(3, Math.min(24, cell.source.split("\n").length + 1));
    const showSource = code || (cell.kind === "markdown" && this.editing.has(cell.id));
    return v_flex().id(`cell-${cell.id}`).flex_none().w_full().border_1().border_color(running ? c.blue : c.rule)
      .on_key_down((e, cx) => {
        if (e.keystroke === "cmd-enter") this.runCell(cx, cell);
        else if (e.keystroke === "shift-enter") this.runCell(cx, cell, true);
      })
      .child(head)
      .when(cell.kind === "sql", (e) => e.child(h_flex().flex_none().h(28).px(10).gap(6).items_center().border_b(1).border_color(c.rule).text_size(11.5).text_color(c.grey500)
        .child(div().flex_none().child("On"))
        .child(div().flex_1().min_w(0).child(this.connPicker(c, cell)))
        .child(div().flex_none().child("rows"))
        .children(LIMITS.map((n) => div().id(`lim-${cell.id}-${n}`).h(20).px(5).flex().flex_none().items_center().cursor_pointer()
          .border_1().border_color((cell.meta.limit ?? LIMIT) === n ? c.blue : c.grey300).text_color((cell.meta.limit ?? LIMIT) === n ? c.blue : c.grey900)
          .tooltip(`Return at most ${fmtInt(n)} rows`).on_click((_e, cx) => { cell.meta.limit = n; this.edited(); cx.notify(); }).child(n >= 1000 ? `${n / 1000}k` : String(n))))
        .child(div().flex_none().child("as"))
        .child(div().w(120).h(22).px(6).flex().items_center().border_1().border_color(c.grey300).when(this.inputs.has(cell.id), (e) => e.child(Input.new(this.inputs.get(cell.id)).w_full().text_size(11.5).font_family(MONO).text_color(c.navy))))))
      .when(cell.kind === "sql" && this.sqlTools.cell === cell.id, (e) => e.child(this.sqlTools.render(c, cell)))
      .when(showSource && this.editors.has(cell.id), (e) => e.child(div().flex_none().w_full().h(lines * 19 + 14).child(Editor.new(this.editors.get(cell.id)).size_full())))
      .when(cell.kind === "markdown" && !this.editing.has(cell.id), (e) => e.child(this.markdown(c, cell)))
      .when(cell.kind === "chart", (e) => e.child(this.chart(c, cell, w)))
      .children(cell.outputs.map((o, k) => this.output(c, cell, o, k, w)));
  }

  tool(c, id, name, tip, onClick, disabled = false) {
    return div().id(id).size(24).flex().flex_none().items_center().justify_center().text_color(disabled ? c.grey300 : c.grey900)
      .when(!disabled, (e) => e.cursor_pointer().hover((s) => s.text_color(c.blue)).on_click(onClick))
      .tooltip(tip).child(this.icon(name, 13));
  }

  connPicker(c, cell) {
    if (this.conns === null) return div().text_size(11.5).text_color(this.connsError ? c.fail : c.grey500).child(this.connsError ?? "Listing databases");
    if (!this.conns.length) return div().text_size(11.5).text_color(c.fail).child("No database connected");
    return h_flex().id(`conn-${cell.id}`).min_w(0).gap(4).overflow_x_scrollbar()
      .children(this.conns.map((k) => {
        const on = cell.meta.conn === k.name;
        return h_flex().id(`conn-${cell.id}-${k.name}`).h(22).px(6).gap(4).flex_none().items_center().cursor_pointer().text_size(11.5)
          .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900)
          .tooltip(k.read_only === false ? `${k.name}: writes allowed` : `${k.name}: read-only`)
          .on_click((_e, cx) => { cell.meta.conn = k.name; this.edited(); cx.notify(); })
          .child(div().child(k.name)).when(!!k.engine, (e) => e.child(div().text_color(c.grey500).child(k.engine)));
      }));
  }

  markdown(c, cell) {
    const lines = cell.source.split("\n");
    if (!cell.source.trim()) return div().id(`md-${cell.id}`).px(12).py(10).text_size(12.5).text_color(c.grey500).cursor_pointer()
      .on_click((_e, cx) => { this.editing.add(cell.id); cx.notify(); }).child("Empty text cell. Click to write.");
    return v_flex().id(`md-${cell.id}`).px(14).py(10).gap(4).cursor_pointer()
      .on_click((e, cx) => { if (e.click_count >= 2) { this.editing.add(cell.id); cx.notify(); } })
      .children(lines.map((l) => {
        const h = /^(#{1,3})\s+(.*)$/.exec(l);
        if (h) return div().text_size([18, 15.5, 13.5][h[1].length - 1]).font_semibold().child(h[2]);
        const b = /^\s*[-*]\s+(.*)$/.exec(l);
        if (b) return h_flex().gap(6).text_size(13).child(div().child("•")).child(div().flex_1().child(b[1]));
        if (/^```/.test(l)) return div().h(2);
        return l.trim() ? div().text_size(13).line_height("20px").child(l) : div().h(6);
      }));
  }

  /** A chart cell: Querybook's nine kinds and their settings, over any result in the notebook. */
  chart(c, cell, w) {
    const spec = (cell.meta.chart ??= { mark: "bar" });
    if (spec.mark === "point") spec.mark = "scatter"; // older notebooks
    if (typeof spec.y === "string") spec.y = [spec.y];
    const tables = tablesByVar(this.cells);
    const name = spec.data ?? spec.var ?? [...tables.keys()].pop();
    const t = tables.get(name);
    const cols = (t?.columns ?? []).map((x) => x.name);
    const numeric = (t?.columns ?? []).filter((x) => /int|float|double|decimal|numeric|real|number/i.test(String(x.type))).map((x) => x.name);
    if (t) {
      if (!cols.includes(spec.x)) spec.x = cols[0];
      spec.y = (spec.y ?? []).filter((y) => cols.includes(y));
      if (!spec.y.length) { const first = numeric.find((n) => n !== spec.x) ?? cols[1]; if (first) spec.y = [first]; }
    }
    const change = (k, v) => (_e, cx) => { spec[k] = v; this.edited(); cx.notify(); };
    const chip = (id, label, on, onClick) => div().id(id).h(22).px(6).flex().flex_none().items_center().cursor_pointer().text_size(11.5)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900).on_click(onClick).child(label);
    const pick = (id, opts, cur, k) => h_flex().id(id).min_w(0).gap(4).overflow_x_scrollbar().children(opts.map(([v, label]) => chip(`${id}-${v}`, label, (cur ?? opts[0][0]) === v, change(k, v))));
    const toggle = (id, label, k) => chip(`${id}-${cell.id}`, label, !!spec[k], change(k, !spec[k]));
    const row = (label, el) => h_flex().gap(8).items_center().text_size(11.5).child(div().w(64).flex_none().text_color(c.grey500).child(label)).child(el);
    const xy = spec.mark === "scatter" || spec.mark === "bubble";
    const colOpts = cols.map((k) => [k, k]);
    const data = t ? shapeChart(t, spec) : { series: [] };
    return v_flex().px(10).py(8).gap(6)
      .child(row("Data", tables.size ? pick(`ch-d-${cell.id}`, [...tables.keys()].map((k) => [k, k]), name, "data") : div().text_color(c.grey500).child("Run a SQL cell first: its result is what a chart draws.")))
      .when(!!t, (e) => e
        .child(row("Kind", pick(`ch-m-${cell.id}`, MARKS, spec.mark ?? "bar", "mark")))
        .child(row(xy ? "Across (x)" : "Categories", pick(`ch-x-${cell.id}`, colOpts, spec.x, "x")))
        .child(row(xy ? "Up (y)" : "Values", h_flex().id(`ch-y-${cell.id}`).min_w(0).gap(4).overflow_x_scrollbar().children((numeric.length ? numeric : cols).map((k) => {
          const on = spec.y.includes(k);
          return chip(`ch-y-${cell.id}-${k}`, k, on, (_e, cx) => { spec.y = on ? spec.y.filter((y) => y !== k) : xy ? [k] : [...spec.y, k]; this.edited(); cx.notify(); });
        }))))
        .child(row("Split by", pick(`ch-s-${cell.id}`, [["", "Nothing"], ...colOpts.filter(([k]) => k !== spec.x)], spec.series ?? "", "series")))
        .when(spec.mark === "bubble", (e) => e.child(row("Size", pick(`ch-z-${cell.id}`, numeric.map((k) => [k, k]), spec.z ?? numeric[0], "z"))))
        .when(!xy, (e) => e
          .child(row("Combine", pick(`ch-a-${cell.id}`, AGGS, spec.agg ?? "none", "agg")))
          .child(row("Order", pick(`ch-o-${cell.id}`, [["none", "As returned"], ["x", "By category"], ["value-desc", "Largest first"], ["value-asc", "Smallest first"]], spec.sort ?? "none", "sort"))))
        // One chip row per setting: a scrolling row inside a wrapping one overlaps in GPUI.
        .child(row("Numbers", pick(`ch-f-${cell.id}`, [["none", "Plain"], ["$", "$"], ["%", "%"]], spec.format ?? "none", "format")))
        .when(spec.mark !== "pie" && spec.mark !== "doughnut" && spec.mark !== "table", (e) => e.child(row("Scale", pick(`ch-sc-${cell.id}`, [["linear", "Linear"], ["log", "Log"]], spec.scale ?? "linear", "scale"))))
        .child(row("Legend", pick(`ch-l-${cell.id}`, [["right", "Right"], ["bottom", "Below"], ["none", "None"]], spec.legend ?? (data.series.length > 1 ? "right" : "none"), "legend")))
        .child(row("Show", h_flex().id(`ch-show-${cell.id}`).min_w(0).gap(4).overflow_x_scrollbar()
          .child(toggle("ch-lab", "Values on marks", "labels"))
          .when(spec.mark === "bar" || spec.mark === "hbar", (e) => e.child(toggle("ch-stack", "Stack", "stack")))
          .when(spec.mark === "line", (e) => e.child(toggle("ch-conn", "Connect gaps", "connect")))
          .when(!xy && spec.mark !== "pie" && spec.mark !== "doughnut", (e) => e.child(toggle("ch-swap", "Swap rows and series", "swap")))))
        .when(data.series.length > 1, (e) => e.child(row("Colors", h_flex().id(`ch-col-${cell.id}`).min_w(0).gap(6).overflow_x_scrollbar().children(data.series.slice(0, 12).map((sr, i) =>
          h_flex().id(`ch-col-${cell.id}-${i}`).flex_none().gap(4).items_center().cursor_pointer().tooltip("Click to change its color")
            .on_click((_e, cx) => { const k = CHART_COLORS.indexOf(sr.color); (spec.colors ??= {})[sr.name] = CHART_COLORS[(k + 1) % CHART_COLORS.length]; this.edited(); cx.notify(); })
            .child(div().size(10).bg(sr.color)).child(div().text_size(11).text_color(c.grey900).child(sr.name)))))))
        .child(ChartView(c, t, spec, { width: Math.max(320, (w ?? 640) - 24), height: 260,
          onToggle: (n, cx) => { const h = new Set(spec.hidden ?? []); if (h.has(n)) h.delete(n); else h.add(n); spec.hidden = [...h]; this.edited(); cx.notify(); } })));
  }

  output(c, cell, o, k, w) {
    const key = `${cell.id}-${k}`;
    if (o.kind === "stream") return div().id(`out-${key}`).px(12).py(8).border_t(1).border_color(c.rule).text_size(12).font_family(MONO)
      .text_color(o.name === "stderr" ? c.warn : c.navy).child(o.text.length > 20000 ? `${o.text.slice(0, 20000)}\n(cut at 20,000 characters)` : o.text);
    if (o.kind === "error") return this.error(c, cell, o, key);
    if (o.kind === "table") {
      let g = this.grids.get(key);
      if (!g) { g = new ResultGrid({ id: key, table: o.table, name: `${this.open?.name ?? "notebook"}-${o.table.var ?? cell.meta.var ?? "result"}` }); this.grids.set(key, g); }
      else if (!g.paged && g.table !== o.table) g.set(o.table);
      return div().id(`out-${key}`).border_t(1).border_color(c.rule).child(g.render(c, { height: 270 }));
    }
    if (o.kind === "display") {
      if (o.mime.startsWith("text/")) return div().id(`out-${key}`).px(12).py(8).border_t(1).border_color(c.rule).text_size(12).font_family(MONO).child(String(o.data).slice(0, 20000));
      return div().id(`out-${key}`).px(12).py(8).border_t(1).border_color(c.rule).text_size(12).text_color(c.grey500)
        .child(`This output is ${o.mime.startsWith("image/") ? "an image" : o.mime}. Export the notebook to see it in Jupyter.`);
    }
    return div();
  }

  error(c, cell, o, key) {
    const details = [o.ename && o.evalue ? `${o.ename}: ${o.evalue}` : o.evalue || o.ename, ...(o.traceback ?? [])].filter(Boolean).join("\n");
    const prompt = `In Captain, the notebook ${this.open?.name} has a ${KINDS[cell.kind]?.[0] ?? ""} cell that fails.\n\nThe cell:\n${cell.source}\n\nThe error:\n${details}\n\nExplain what's wrong and suggest a fix. Change nothing without asking me.`;
    return v_flex().id(`out-${key}`).px(12).py(10).gap(8).border_t(1).border_color(c.fail).bg(c.failLight)
      .child(div().text_size(12.5).text_color(c.fail).child(errorSentence(o.ename, o.evalue)))
      .when(o.ename !== "KeyboardInterrupt", (e) => e.child(h_flex().gap(8)
        .when(!!details, (e) => e.child(Button(c, `copy-${key}`, "Copy details", (_e, cx) => cx.write_to_clipboard(details), { small: true })))
        .child(Button(c, `fix-${key}`, "Ask an agent to fix it", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, prompt)), { small: true }))
        .child(Button(c, `prompt-${key}`, "Copy prompt", (_e, cx) => copyPrompt(cx, prompt), { kind: "quiet", small: true }))));
  }
}
