// captain-ui: the result grid every Captain screen shows a query's rows in (notebook cells, SQL, Data). Querybook's
// result tools, natively: sort (typed as text or number), text and number filters, column stats, detected column
// types with display formats, hidden columns, CSV download and TSV copy, and a warning when a limit cut the result.
//
//   const grid = new ResultGrid({ id: "cell-3", table: {columns: [{name, type}], rows: [[...]], row_count, truncated,
//                                  var, limit}, name: "df_1" });
//   grid.render(c, { height: 300 })     // in render; keep the instance on the view
//
// It works on the rows it was given (a cell's first 1,000, say): sorting, filtering and stats say so when the result
// has more. Filter inputs are made in click handlers, never while drawing (the app refuses that).
//
// Paged from the services: any route that answers in the one row shape ({columns: [{name, type, kind}], rows, offset,
// limit, has_more, total?, ms}), such as a warehouse table's /rows or a dataset's /rows. Sorting and the WHERE box go
// to the services, so they cover the whole table, and Previous and Next page through it. Make it outside render.
//   const grid = new ResultGrid({ id: "rows", name: "products", load: (q) => api(`/api/tables/c/s/t/rows${query(q)}`) });
//   grid.fetch(cx);                      // q = {offset, limit, order, desc, where, count}
//   The rows come first, without a count: counting a big file means reading all of it (seconds on S3), so the total
//   is asked for separately once the rows are on screen, and the footer says "counting" meanwhile.
//   grid.render(c, { fill: true })       // takes the height its parent gives it and scrolls inside
// `source` ("Databricks") names where rows come from while a slow page loads: "Reading rows from Databricks, 4 s".
//
// Columns format by their `kind` when the services send one: numbers right-aligned, decimals exactly as sent, lists
// and structs shortened ("[3] a, b, c"). Clicking a row opens it below the grid, every column written out in full.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar, InputState, Input, Popup, EditorState, Editor } from "gpui-base";
import { writeTextFile, readTextFile, home } from "herdr";
import { MONO } from "./tokens.js";
import { icon } from "./icons.js";
import { int, num } from "./num.js";

const ROW = 26;
const COL = 160;
const NUM_W = 52;

export const TEXT_OPS = [["contains", "contains"], ["not_contains", "doesn't contain"], ["eq", "is"], ["ne", "isn't"], ["starts", "starts with"], ["ends", "ends with"]];
export const NUMBER_OPS = [["eq", "="], ["ne", "≠"], ["gt", ">"], ["lt", "<"], ["between", "between"]];
// The grid's column types for the services' kinds (backend data/cells.py).
const KIND_TYPE = { integer: "number", number: "number", decimal: "number", bool: "boolean", list: "json", struct: "json", date: "time", time: "time", timestamp: "time" };
const FORMATS = {
  time: [["plain", "Plain"]],
  number: [["plain", "Plain"], ["commas", "1,234"], ["dollar", "$"], ["percent", "%"], ["human", "1.2k"]],
  id: [["plain", "Plain"]],
  string: [["plain", "Plain"], ["capitalize", "Capitalize"]],
  url: [["plain", "Plain"], ["link", "Link"]],
  json: [["plain", "Short"], ["raw", "Raw"]],
  boolean: [["plain", "Plain"], ["yesno", "Yes / No"]],
};

const isNum = (v) => typeof v === "number" || (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v)));
const toNum = (v) => (typeof v === "number" ? v : Number(v));
// A cell shows one line, cut to its column: laying out kilobytes of text to show forty characters is what made wide
// JSON columns slow to draw. The whole value stays in the row (row details, CSV, copy).
const CLIP = 240;
const clip = (s) => (s.length > CLIP ? `${s.slice(0, CLIP)}...` : s);
const text = (v) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const fmtInt = (n) => int(n ?? 0);
const fmtNum = (n) => (Number.isInteger(n) ? fmtInt(n) : num(n, 4));
const human = (n) => {
  const a = Math.abs(n);
  return a >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : a >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : a >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : fmtNum(n);
};
// A failed page in one plain sentence: never the transport's words.
const sayError = (r) => {
  if (r.timedOut) return "This took too long. Narrow it with a WHERE, or try again.";
  if (r.starting || r.failed) return "Captain's services aren't answering. Try again in a moment.";
  // Terminal colour codes some engines put in their messages never reach the screen.
  const e = String(r.data?.error ?? r.data?.detail ?? r.error ?? "").replace(/\u001b\[[0-9;]*m/g, "").trim();
  return e ? `${e[0].toUpperCase()}${e.slice(1)}${/[.!?]$/.test(e) ? "" : "."}` : "Couldn't load these rows.";
};
const csvCell = (v) => { const s = text(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };

/** What a column holds, from its declared type and a sample of its values. */
export function detectType(name, declared, values) {
  const vs = values.filter((v) => v !== null && v !== undefined && v !== "").slice(0, 200);
  const t = String(declared ?? "").toLowerCase();
  if (/bool/.test(t) || (vs.length && vs.every((v) => typeof v === "boolean" || v === "true" || v === "false"))) return "boolean";
  if (/(^|_)id$|^id$|_key$/.test(String(name).toLowerCase()) && vs.every((v) => isNum(v) || typeof v === "string")) return "id";
  if (/int|float|double|decimal|numeric|real|number/.test(t) || (vs.length && vs.every(isNum))) return "number";
  if (/json|struct|map|list|array|object/.test(t) || (vs.length && vs.every((v) => typeof v === "object" || /^\s*[[{]/.test(String(v))))) return "json";
  if (vs.length && vs.every((v) => /^https?:\/\//.test(String(v)))) return "url";
  return "string";
}

/** A column's type: from the services' kind when there is one (an id stays an id, text is looked at), else detected. */
export function columnType(col, values) {
  const t = KIND_TYPE[col.kind];
  if (t === "number" && /(^|_)id$|^id$|_key$/.test(String(col.name).toLowerCase()) && col.kind === "integer") return "id";
  if (t) return t;
  if (col.kind === "text") { const d = detectType(col.name, "", values); return d === "number" ? "string" : d; }
  return detectType(col.name, col.type, values);
}

/** A list or struct, shortened to fit a cell: "[3] a, b, c" or "{2} k: v, k: v". */
export function shortJson(v) {
  let o = v;
  if (typeof v === "string") { try { o = JSON.parse(v); } catch { return v; } }
  if (Array.isArray(o)) return `[${o.length}] ${o.map((x) => (x !== null && typeof x === "object" ? JSON.stringify(x) : String(x))).join(", ")}`;
  if (o && typeof o === "object") { const e = Object.entries(o); return `{${e.length}} ${e.map(([k, x]) => `${k}: ${x !== null && typeof x === "object" ? JSON.stringify(x) : String(x)}`).join(", ")}`; }
  return text(v);
}

/** A cell written out in full: lists and structs as indented JSON. */
export function fullCell(v, type) {
  if (v === null || v === undefined) return "null";
  if (type === "json") { try { return JSON.stringify(typeof v === "string" ? JSON.parse(v) : v, null, 2); } catch { return text(v); } }
  return text(v);
}

/** Sum, average, median, min, max (numbers), unique count and the five commonest values. */
export function columnStats(values, type) {
  const present = values.filter((v) => v !== null && v !== undefined && v !== "");
  const counts = new Map();
  for (const v of present) { const k = text(v); counts.set(k, (counts.get(k) ?? 0) + 1); }
  const common = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5);
  const out = { count: values.length, empty: values.length - present.length, unique: counts.size, common };
  if (type === "number") {
    const ns = present.map(toNum).filter(Number.isFinite).sort((a, b) => a - b);
    if (ns.length) {
      const sum = ns.reduce((a, b) => a + b, 0);
      const mid = Math.floor(ns.length / 2);
      Object.assign(out, { sum, avg: sum / ns.length, median: ns.length % 2 ? ns[mid] : (ns[mid - 1] + ns[mid]) / 2, min: ns[0], max: ns[ns.length - 1] });
    }
  }
  return out;
}

/** Does this value pass the filter {op, value, value2}? Number filters compare as numbers. */
export function passes(v, f, type) {
  if (!f || f.value === "" || f.value === undefined) return true;
  if (type === "number") {
    const n = toNum(v), a = Number(f.value), b = Number(f.value2);
    if (!Number.isFinite(n)) return false;
    return { eq: n === a, ne: n !== a, gt: n > a, lt: n < a, between: n >= Math.min(a, b) && n <= Math.max(a, b) }[f.op] ?? true;
  }
  const s = text(v).toLowerCase(), q = String(f.value).toLowerCase();
  return { contains: s.includes(q), not_contains: !s.includes(q), eq: s === q, ne: s !== q, starts: s.startsWith(q), ends: s.endsWith(q) }[f.op] ?? true;
}

export class ResultGrid {
  constructor({ id, table, name, load, pageSize = 100, searchable = false, source = null, openRow = null }) {
    this.id = id;
    this.name = name ?? table?.var ?? "result";
    this.openRow = null; // the row shown in full below the grid
    this.openFirst = openRow; // a row to open once the first page is in (a screen's start props)
    // Paged from the services (load(q) resolves like kit/cst.js api()).
    this.load = load ?? null;
    if (this.load) {
      this.page = { offset: 0, limit: pageSize, has_more: false, total: null, ms: null };
      this.order = null; // {name, desc}
      this.where = "";
      this.whereBox = InputState.new({ placeholder: "WHERE, for example: price < 100" });
      this.whereBox.on("submit", (_e, cx) => this.applyWhere(cx));
      // A search across every column, for routes that take `search` (a dataset's rows).
      this.search = "";
      this.searchBox = searchable ? InputState.new({ placeholder: "Search every column" }) : null;
      this.searchBox?.on("submit", (_e, cx) => this.applyWhere(cx));
      this.loading = false;
      this.error = null;
      this.source = source;
      this.orderOpen = false; // the Order by list
    }
    this.set(table);
  }

  get paged() { return !!this.load; }

  /** Ask the services for a page. Keeps the order and WHERE; a new WHERE or order starts at the first row. */
  fetch(cx, offset = this.page.offset) {
    const seq = (this.seq = (this.seq ?? 0) + 1);
    const q = { offset, limit: this.page.limit, order: this.order?.name, desc: this.order?.desc ? "true" : undefined, where: this.where || undefined, search: this.search || undefined };
    this.loading = true;
    this.started = Date.now();
    // Redraw each second while a page loads, so the time on screen moves (one timer per grid).
    if (!this.ticking && cx.timer) { this.ticking = true; cx.timer.every(1000, (cx) => { if (this.loading) cx.notify(); }); }
    cx.spawn(async (cx) => {
      const r = await this.load(q);
      if (seq !== this.seq) return;
      this.loading = false;
      if (r.ok) {
        const d = r.data ?? {};
        this.last = d; // the whole answer, for what a screen reads beside the rows (a dataset's files)
        // The last page says the total by itself: no count to ask for.
        const total = d.total ?? this.page.total ?? (!d.has_more ? (d.offset ?? offset) + (d.rows ?? []).length : null);
        this.page = { offset: d.offset ?? offset, limit: d.limit ?? this.page.limit, has_more: !!d.has_more, total: total ?? null, ms: d.ms ?? null, sql: d.sql };
        this.error = null;
        this.openRow = null;
        this.set({ columns: d.columns ?? [], rows: d.rows ?? [], row_count: total ?? undefined });
        if (this.openFirst !== null && this.table.rows[this.openFirst]) { this.toggleRow(this.openFirst); this.openFirst = null; }
        if ((total === null || total === undefined) && (d.has_more || offset > 0)) this.count(cx, q);
      } else this.error = sayError(r);
      cx.notify();
    });
    cx.notify();
  }

  /** The total for the current filter, asked for after the rows are shown. Only the number is used from the answer. */
  count(cx, q) {
    const key = JSON.stringify([q.where, q.search]);
    if (this.counting === key) return;
    this.counting = key;
    cx.spawn(async (cx) => {
      const r = await this.load({ ...q, offset: 0, limit: 1, order: undefined, desc: undefined, count: "true" });
      if (this.counting !== key) return; // the filter changed meanwhile
      this.counting = null;
      const total = r.ok ? r.data?.total : null;
      if (total !== null && total !== undefined && JSON.stringify([this.where || undefined, this.search || undefined]) === key) {
        this.page.total = total;
        this.table.row_count = total;
      }
      cx.notify();
    });
  }

  applyWhere(cx) {
    this.where = this.whereBox.value().trim();
    this.search = this.searchBox?.value().trim() ?? "";
    this.page.total = null; // a new WHERE: count again
    this.counting = null;
    this.fetch(cx, 0);
  }

  /** The first rows shown, as {columns, rows}: what a test compares across screens. */
  firstPage() {
    return { columns: (this.table.columns ?? []).map((c) => c.name), rows: (this.table.rows ?? []).slice(0, this.page?.limit ?? 100) };
  }

  /** New rows (a cell ran again): keeps sort, filters, formats and hidden columns that still apply. */
  set(table) {
    this.table = table ?? { columns: [], rows: [] };
    const rows = this.table.rows ?? [];
    this.types = (this.table.columns ?? []).map((c, j) => columnType(c, rows.map((r) => r[j])));
    this.sort ??= null; // {col, desc}
    this.filters ??= {}; // col -> {op, value, value2}
    this.formats ??= {}; // col -> format id
    this.hidden ??= new Set();
    this.menu = null; // column index whose menu is open
    this.inputs ??= new Map(); // `${col}:a` / `${col}:b` -> InputState
    this.cache = null;
    this.note = null;
  }

  /** Row indexes after filters and sort, cached until either changes. */
  view() {
    const key = JSON.stringify([this.sort, this.filters, (this.table.rows ?? []).length]);
    if (this.cache?.key === key) return this.cache.rows;
    const rows = this.table.rows ?? [];
    let idx = rows.map((_, i) => i);
    for (const [col, f] of Object.entries(this.filters)) idx = idx.filter((i) => passes(rows[i][col], f, this.types[col]));
    if (this.sort) {
      const { col, desc } = this.sort;
      const num = this.types[col] === "number";
      idx.sort((a, b) => {
        const x = rows[a][col], y = rows[b][col];
        if (x === null || x === undefined) return 1;
        if (y === null || y === undefined) return -1;
        const d = num ? toNum(x) - toNum(y) : text(x).localeCompare(text(y), undefined, { numeric: true });
        return desc ? -d : d;
      });
    }
    this.cache = { key, rows: idx };
    return idx;
  }

  show(v, col) {
    const type = this.types[col], f = this.formats[col] ?? "plain";
    if (v === null || v === undefined) return "";
    if (type === "number" && isNum(v)) {
      const n = toNum(v);
      if (f === "commas") return fmtNum(n);
      if (f === "dollar") return `$${num(n, 2, 2)}`;
      if (f === "percent") return `${num(n * 100, 2)}%`;
      if (f === "human") return human(n);
      return String(v);
    }
    if (type === "boolean" && f === "yesno") return v === true || v === "true" ? "Yes" : "No";
    if (type === "string" && f === "capitalize") { const s = text(v); return s ? `${s[0].toUpperCase()}${s.slice(1)}` : s; }
    if (type === "json") return clip(f === "raw" ? text(v) : shortJson(v));
    return clip(text(v));
  }

  /** The rows shown, as CSV text (header first). */
  csv(sep = ",") {
    const cols = (this.table.columns ?? []).map((c, j) => [c, j]).filter(([, j]) => !this.hidden.has(j));
    const rows = this.table.rows ?? [];
    const cell = sep === "," ? csvCell : (v) => text(v).replace(/[\t\n]/g, " ");
    return [cols.map(([c]) => cell(c.name)).join(sep), ...this.view().map((i) => cols.map(([, j]) => cell(rows[i][j])).join(sep))].join("\n");
  }

  partNote(n) {
    const total = this.table.row_count ?? n;
    return n < total ? ` (the first ${fmtInt(n)} of ${fmtInt(total)} rows)` : "";
  }

  // ---- actions (event handlers) ------------------------------------------------------------------------------------

  toggleSort(cx, col) {
    if (this.paged) {
      // The whole table, sorted by the services: ascending, descending, then as stored.
      const name = this.table.columns[col]?.name;
      const o = this.order;
      this.order = !o || o.name !== name ? { name, desc: false } : !o.desc ? { name, desc: true } : null;
      this.fetch(cx, 0);
      return;
    }
    const s = this.sort;
    this.sort = !s || s.col !== col ? { col, desc: false } : !s.desc ? { col, desc: true } : null;
    cx.notify();
  }

  openMenu(cx, col) {
    this.menu = this.menu === col ? null : col;
    if (this.menu !== null) {
      for (const k of ["a", "b"]) {
        if (this.inputs.has(`${col}:${k}`)) continue;
        const st = InputState.new({ placeholder: k === "a" ? "value" : "and" , value: k === "a" ? String(this.filters[col]?.value ?? "") : String(this.filters[col]?.value2 ?? "") });
        st.on("submit", (_e, cx) => this.applyFilter(cx, col));
        this.inputs.set(`${col}:${k}`, st);
      }
      this.pendingOp ??= {};
      this.pendingOp[col] ??= this.filters[col]?.op ?? (this.types[col] === "number" ? "gt" : "contains");
    }
    cx.notify();
  }

  applyFilter(cx, col) {
    const a = this.inputs.get(`${col}:a`)?.value().trim() ?? "";
    const b = this.inputs.get(`${col}:b`)?.value().trim() ?? "";
    if (a === "") delete this.filters[col];
    else this.filters[col] = { op: this.pendingOp?.[col] ?? "contains", value: a, value2: b };
    cx.notify();
  }

  clearFilter(cx, col) {
    delete this.filters[col];
    this.inputs.get(`${col}:a`)?.set_value("");
    this.inputs.get(`${col}:b`)?.set_value("");
    cx.notify();
  }

  async exportCsv(cx) {
    const file = `${String(this.name).replace(/[^\w.-]+/g, "-")}.csv`;
    const path = `${home()}/Downloads/${file}`;
    try {
      const old = await readTextFile(path);
      await writeTextFile(path, this.csv(","), old);
      this.note = `Saved ~/Downloads/${file}${this.partNote(this.view().length)}.`;
    } catch {
      this.note = `Couldn't write ~/Downloads/${file}.`;
    }
    cx.notify();
  }

  copyTsv(cx) {
    cx.write_to_clipboard(this.csv("\t"));
    this.note = `Copied ${fmtInt(this.view().length)} rows as TSV${this.partNote(this.view().length)}.`;
    cx.notify();
  }

  // ---- drawing -----------------------------------------------------------------------------------------------------

  icon(name, size, color) {
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  chip(c, id, label, on, onClick) {
    return div().id(id).h(22).px(6).flex().flex_none().items_center().cursor_pointer().text_size(11.5)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900)
      .on_click(onClick).child(label);
  }

  render(c, { height = 300, fill = false } = {}) {
    const id = this.id;
    const all = this.table.columns ?? [];
    const cols = all.map((col, j) => [col, j]).filter(([, j]) => !this.hidden.has(j));
    const rows = this.table.rows ?? [];
    const idx = this.view();
    const width = NUM_W + cols.length * COL;
    const bodyH = Math.max(ROW, Math.min(idx.length, Math.floor(height / ROW)) * ROW);
    // Nothing to show yet while the services read the first page: skeleton rows, where from and for how long.
    const waiting = this.paged && this.loading && !rows.length;
    const secs = waiting ? Math.floor((Date.now() - (this.started ?? Date.now())) / 1000) : 0;
    const skeleton = v_flex().id(`gwait-${id}`).w_full().gap(0)
      .child(h_flex().h(ROW + 4).px(10).gap(8).items_center().text_size(12).text_color(c.grey900)
        .child(div().child(`Reading rows${this.source ? ` from ${this.source}` : ""}`))
        .child(div().font_family(MONO).text_color(c.grey500).child(`${secs} s`)))
      .children(Array.from({ length: 8 }, (_, i) => h_flex().h(ROW).px(10).gap(16).items_center().border_b(1).border_color(c.rule)
        .children([0, 1, 2, 3].map((k) => div().h(10).w(60 + ((i * 37 + k * 53) % 90)).bg(c.surface2)))));
    const filters = this.paged ? 0 : Object.keys(this.filters).length;
    const base = this.paged ? this.page.offset : 0;
    const list = v_virtual_list(`grid-${id}`, idx.length, ROW, (i) => `${id}:${idx[i]}`, (range) => {
      const out = [];
      for (let i = range.start; i < range.end; i++) {
        const r = rows[idx[i]];
        out.push(h_flex().h(ROW).border_b(1).border_color(c.rule).text_size(12).whitespace_nowrap()
          .when(this.openRow === idx[i], (e) => e.bg(c.blueLight))
          .child(div().w(NUM_W).flex_none().px(6).text_right().font_family(MONO).text_color(c.grey500).child(String(base + idx[i] + 1)))
          .children(cols.map(([, j]) => {
            const t = this.types[j];
            return div().w(COL).flex_none().px(8).truncate().font_family(MONO)
              .when(t === "number", (e) => e.text_right())
              .when(t === "url" && this.formats[j] === "link", (e) => e.text_color(c.blue).underline())
              .child(this.show(r[j], j));
          })));
      }
      return out;
    });
    const header = h_flex().h(ROW).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
      .child(div().w(NUM_W).flex_none().px(6).text_right().text_color(c.grey500).child("#"))
      .children(cols.map(([col, j]) => {
        const s = this.paged ? (this.order?.name === col.name ? this.order : null) : this.sort?.col === j ? this.sort : null;
        const filtered = !!this.filters[j];
        return h_flex().w(COL).flex_none().h_full().px(8).gap(4).items_center()
          .child(div().id(`gh-${id}-${j}`).flex_1().min_w(0).truncate().cursor_pointer().text_color(s || filtered ? c.blue : c.navy)
            .tooltip(`${col.name}: ${this.types[j]}${col.type ? ` (${col.type})` : ""}. Click to sort${this.paged ? " the whole table" : ""}.`)
            .on_click((_e, cx) => this.toggleSort(cx, j)).child(col.name))
          .when(!!s, (e) => e.child(svg(icon("arrow-up-down")).size(11).flex_none().text_color(c.blue)))
          .when(filtered, (e) => e.child(this.icon("filter", 11, c.blue)))
          .child(div().id(`gm-${id}-${j}`).size(18).flex().flex_none().items_center().justify_center().cursor_pointer()
            .text_color(this.menu === j ? c.blue : c.grey500).hover((st) => st.text_color(c.blue)).tooltip("Filter, format, stats, hide")
            .on_click((_e, cx) => this.openMenu(cx, j)).child(this.icon("chevron-down", 12)));
      }));
    const total = this.table.row_count ?? rows.length;
    const body = waiting ? skeleton : div().id(`gx-${id}`).w_full().when(fill, (e) => e.flex_1().min_h(0)).overflow_x_scrollbar().child(v_flex().w(width).when(fill, (e) => e.h_full())
        .child(header)
        .child(div().when(fill, (e) => e.flex_1().min_h(0)).when(!fill, (e) => e.h(bodyH)).relative()
          .when(!idx.length, (e) => e.child(div().px(10).py(4).text_size(12).text_color(c.grey500).child(this.loading ? "Loading rows" : filters || this.where ? "No rows match." : "No rows.")))
          .when(idx.length > 0, (e) => e.child(list.size_full().on_item_click((key, cx) => { const r = Number(String(key).split(":").pop()); this.toggleRow(r); cx.notify(); })).child(Scrollbar.vertical(`grid-${id}`).viewport_from_layout().absolute().inset_0()))));
    return v_flex().id(`grid-wrap-${id}`).w_full().when(fill, (e) => e.flex_1().min_h(0).h_full())
      .when(this.paged, (e) => e.child(this.whereRow(c)))
      .when(this.paged && !!this.error, (e) => e.child(div().px(10).py(6).text_size(12).text_color(c.fail).child(this.error)))
      .when(this.menu !== null && this.menu !== undefined && all[this.menu], (e) => e.child(this.columnMenu(c, this.menu)))
      .child(body)
      .when(this.openRow !== null && !!rows[this.openRow], (e) => e.child(this.rowPanel(c, this.openRow)))
      .when(this.paged, (e) => e.child(this.pager(c)))
      .child(h_flex().flex_none().h(28).px(10).gap(10).items_center().text_size(11.5).text_color(c.grey500).border_t(1).border_color(c.rule)
        .when(!!this.table.var, (e) => e.child(div().font_family(MONO).text_color(c.navy).child(this.table.var)))
        .when(!this.paged, (e) => e.child(div().child(filters ? `${fmtInt(idx.length)} of ${fmtInt(rows.length)} rows match` : `${fmtInt(total)} row${total === 1 ? "" : "s"}, ${all.length} column${all.length === 1 ? "" : "s"}`)))
        .when(this.paged, (e) => e.child(div().child(`${all.length} column${all.length === 1 ? "" : "s"}`)))
        .when(!this.paged && rows.length < total, (e) => e.child(div().child(`showing the first ${fmtInt(rows.length)}${this.table.var ? `: all of them are in ${this.table.var}` : ""}`)))
        .when(!!this.table.limited, (e) => e.child(div().text_color(c.warn).child(this.table.limit ? `Limited to ${fmtInt(this.table.limit)} rows: raise the row limit for more` : "The query hit its row limit")))
        .when(this.hidden.size > 0, (e) => e.child(div().id(`gshow-${id}`).cursor_pointer().text_color(c.blue)
          .on_click((_e, cx) => { this.hidden.clear(); cx.notify(); }).child(`${this.hidden.size} hidden, show all`)))
        .child(div().flex_1())
        .when(!!this.note, (e) => e.child(div().min_w(0).truncate().text_color(c.grey900).child(this.note)))
        .child(div().id(`gcsv-${id}`).flex_none().cursor_pointer().text_color(c.blue).on_click((_e, cx) => cx.spawn(async (cx) => this.exportCsv(cx))).child("Download CSV"))
        .child(div().id(`gtsv-${id}`).flex_none().cursor_pointer().text_color(c.blue).on_click((_e, cx) => this.copyTsv(cx)).child("Copy TSV")));
  }

  whereRow(c) {
    const id = this.id;
    // Wraps in a narrow pane rather than squeezing the box to nothing.
    return h_flex().flex_none().flex_wrap().px(10).py(6).gap(8).items_center().border_b(1).border_color(c.rule).text_size(11.5)
      .when(!!this.searchBox, (e) => e.child(div().w(200).flex_none().h(24).px(6).flex().items_center().border_1().border_color(this.search ? c.blue : c.grey300)
        .child(Input.new(this.searchBox).w_full().text_size(11.5).text_color(c.navy))))
      .child(div().flex_none().text_color(c.grey500).child("Where"))
      .child(div().flex_1().min_w(180).max_w(480).h(24).px(6).flex().items_center().border_1().border_color(this.where ? c.blue : c.grey300)
        .child(Input.new(this.whereBox).w_full().text_size(11.5).font_family(MONO).text_color(c.navy)))
      .child(this.chip(c, `gwhere-${id}`, "Apply", false, (_e, cx) => this.applyWhere(cx)))
      .when(!!this.where || !!this.search, (e) => e.child(this.chip(c, `gwhere-clear-${id}`, "Clear", false, (_e, cx) => { this.whereBox.set_value(""); this.searchBox?.set_value(""); this.applyWhere(cx); })))
      .child(this.orderBy(c));
  }

  /** Order by: one button, and the columns in a list that opens over the grid (never a wall of chips). */
  orderBy(c) {
    const id = this.id;
    const o = this.order;
    const pick = (order) => (_e, cx) => { this.order = order; this.orderOpen = false; this.fetch(cx, 0); };
    const opt = (key, label, on, onClick) => div().id(`gorder-${id}-${key}`).h(24).px(8).flex().flex_none().items_center().cursor_pointer().text_size(12)
      .text_color(on ? c.blue : c.navy).when(on, (e) => e.bg(c.blueLight)).hover((st) => st.bg(c.surface2)).on_click(onClick).child(label);
    const trigger = h_flex().id(`gorder-btn-${id}`).h(24).px(8).gap(5).flex_none().items_center().cursor_pointer().text_size(11.5)
      .border_1().border_color(o || this.orderOpen ? c.blue : c.grey300).text_color(o ? c.blue : c.grey900)
      .on_click((_e, cx) => { this.orderOpen = !this.orderOpen; cx.notify(); })
      .child(div().max_w(200).truncate().child(o ? `Order by ${o.name}${o.desc ? ", largest first" : ""}` : "Order by"))
      .child(this.icon("chevron-down", 11));
    const cols = (this.table.columns ?? []).map((x) => x.name);
    return Popup.new(`gorder-pop-${id}`, trigger).anchor("bottom_left")
      .when(this.orderOpen, (e) => e.content(v_flex().id(`gorder-list-${id}`).w(260).max_h(300).overflow_y_scrollbar().py(4).bg(c.surface).border_1().border_color(c.grey300)
        .child(opt("none", "As stored", !o, pick(null)))
        .when(!!o, (e) => e.child(opt("dir", o?.desc ? "Smallest first instead" : "Largest first instead", false, pick(o ? { name: o.name, desc: !o.desc } : null))))
        .child(div().h(1).my(4).bg(c.rule))
        .children(cols.map((n, i) => opt(`c${i}`, n, o?.name === n, pick({ name: n, desc: false }))))));
  }

  /** "1 to 100 of 12,480", Previous and Next. */
  pager(c) {
    const id = this.id;
    const { offset, limit, has_more, total, ms } = this.page;
    const n = (this.table.rows ?? []).length;
    const span = n ? `${fmtInt(offset + 1)} to ${fmtInt(offset + n)}` : "No rows";
    const of = total !== null && total !== undefined ? ` of ${fmtInt(total)}` : this.counting ? ", counting the rest" : has_more ? ", more after" : "";
    const step = (label, gid, on, to) => div().id(gid).h(22).px(8).flex().flex_none().items_center().text_size(11.5).border_1()
      .border_color(on ? c.grey300 : c.rule).text_color(on ? c.navy : c.grey300)
      .when(on, (e) => e.cursor_pointer().hover((st) => st.border_color(c.blue)).on_click((_e, cx) => this.fetch(cx, to))).child(label);
    return h_flex().id(`gpager-${id}`).h(32).px(10).gap(8).items_center().border_t(1).border_color(c.rule).text_size(11.5)
      .child(div().text_color(c.navy).child(`${span}${n ? of : ""}`))
      .when(ms !== null && ms !== undefined, (e) => e.child(div().text_color(c.grey500).child(`${fmtInt(ms)} ms`)))
      .when(this.loading, (e) => e.child(div().text_color(c.grey500).child("Loading")))
      .child(div().flex_1())
      .child(step("Previous", `gprev-${id}`, offset > 0 && !this.loading, Math.max(0, offset - limit)))
      .child(step("Next", `gnext-${id}`, has_more && !this.loading, offset + limit));
  }

  /** One row, every column in full: lists and structs written out. */
  /** Open or close a row. Its text goes in a read-only editor, made here (an editor can't be made while drawing). */
  toggleRow(i) {
    this.openRow = this.openRow === i ? null : i;
    if (this.openRow === null) return;
    const r = this.table.rows[i];
    const cols = this.table.columns ?? [];
    const base = this.paged ? this.page.offset : 0;
    const key = `${base + i}:${cols.length}`;
    if (r && this.rowText?.key !== key) {
      const w = Math.min(24, Math.max(4, ...cols.map((col) => String(col.name).length)));
      const lines = cols.flatMap((col, j) => {
        const v = r[j] === null || r[j] === undefined ? "null" : String(fullCell(r[j], this.types[j]));
        const [first, ...rest] = v.split("\n");
        return [`${String(col.name).padEnd(w)}  ${first}`, ...rest.map((l) => `${" ".repeat(w + 2)}${l}`)];
      });
      this.rowText = { key, lines: lines.length, state: EditorState.new({ language: "txt", readonly: true, value: lines.join("\n"), font_family: MONO, font_size: 12, soft_wrap: true, line_numbers: false }) };
    }
  }

  rowPanel(c, i) {
    const id = this.id;
    const r = this.table.rows[i];
    const base = this.paged ? this.page.offset : 0;
    const cols = this.table.columns ?? [];
    const text = this.rowText?.key === `${base + i}:${cols.length}` ? this.rowText : null;
    const json = () => JSON.stringify(Object.fromEntries(cols.map((col, j) => [col.name, r[j] ?? null])), null, 2);
    return v_flex().id(`grow-${id}`).flex_none().h(Math.min(300, 44 + (text?.lines ?? cols.length) * 18)).px(10).py(8).gap(6).border_t(1).border_color(c.rule).bg(c.surface2).text_size(12)
      .child(h_flex().flex_none().gap(10).items_center()
        .child(div().font_medium().text_color(c.navy).child(`Row ${fmtInt(base + i + 1)}`))
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child("Select any part to copy it."))
        .child(div().id(`grow-json-${id}`).flex_none().cursor_pointer().text_size(11.5).text_color(c.blue).on_click((_e, cx) => { cx.write_to_clipboard(json()); this.note = "Copied the row as JSON."; cx.notify(); }).child("Copy as JSON"))
        .child(div().id(`grow-x-${id}`).cursor_pointer().text_color(c.grey500).on_click((_e, cx) => { this.openRow = null; cx.notify(); }).child(this.icon("x", 12))))
      .child(v_flex().flex_1().min_h(0).bg(c.surface).when(!!text, (e) => e.child(Editor.new(text.state).flex_1().min_h(0))));
  }

  columnMenu(c, j) {
    const id = this.id;
    const col = this.table.columns[j];
    const type = this.types[j];
    const ops = type === "number" ? NUMBER_OPS : TEXT_OPS;
    const op = this.pendingOp?.[j] ?? ops[0][0];
    const values = (this.table.rows ?? []).map((r) => r[j]);
    const st = columnStats(values, type);
    const row = (label, el) => h_flex().gap(8).items_center().child(div().w(52).flex_none().text_color(c.grey500).child(label)).child(el);
    const a = this.inputs.get(`${j}:a`), b = this.inputs.get(`${j}:b`);
    const box = (s) => div().w(110).h(22).px(6).flex().items_center().border_1().border_color(c.grey300).child(Input.new(s).w_full().text_size(11.5).font_family(MONO).text_color(c.navy));
    const stat = (k, v) => h_flex().gap(4).child(div().text_color(c.grey500).child(k)).child(div().font_family(MONO).text_color(c.navy).child(v));
    return v_flex().id(`gmenu-${id}`).w_full().p(10).gap(8).border_b(1).border_color(c.rule).bg(c.surface2).text_size(11.5)
      .child(h_flex().gap(8).items_center()
        .child(div().text_size(12.5).font_medium().text_color(c.navy).child(col.name))
        .child(div().text_color(c.grey500).child(type))
        .child(div().flex_1())
        .child(div().id(`ghide-${id}-${j}`).cursor_pointer().text_color(c.blue).on_click((_e, cx) => { this.hidden.add(j); this.menu = null; cx.notify(); }).child("Hide column"))
        .child(div().id(`gclose-${id}`).cursor_pointer().text_color(c.grey500).on_click((_e, cx) => { this.menu = null; cx.notify(); }).child(this.icon("x", 12))))
      .child(row("Sort", h_flex().gap(4)
        .child(this.chip(c, `gs-asc-${id}-${j}`, type === "number" ? "Smallest first" : "A to Z", this.paged ? this.order?.name === col.name && !this.order.desc : this.sort?.col === j && !this.sort.desc,
          (_e, cx) => { if (this.paged) { this.order = { name: col.name, desc: false }; this.fetch(cx, 0); } else { this.sort = { col: j, desc: false }; cx.notify(); } }))
        .child(this.chip(c, `gs-desc-${id}-${j}`, type === "number" ? "Largest first" : "Z to A", this.paged ? this.order?.name === col.name && this.order.desc : this.sort?.col === j && this.sort.desc,
          (_e, cx) => { if (this.paged) { this.order = { name: col.name, desc: true }; this.fetch(cx, 0); } else { this.sort = { col: j, desc: true }; cx.notify(); } }))))
      .when(this.paged, (e) => e.child(row("Filter", div().text_color(c.grey500).child(`Filter the whole ${this.searchBox ? "dataset" : "table"} with the Where box above.`))))
      .when(!this.paged, (e) => e.child(row("Filter", h_flex().flex_wrap().gap(4).items_center()
        .children(ops.map(([k, label]) => this.chip(c, `gop-${id}-${j}-${k}`, label, op === k, (_e, cx) => { (this.pendingOp ??= {})[j] = k; cx.notify(); })))
        .when(!!a, (e) => e.child(box(a)))
        .when(!!b && op === "between", (e) => e.child(box(b)))
        .child(this.chip(c, `gapply-${id}-${j}`, "Apply", false, (_e, cx) => this.applyFilter(cx, j)))
        .when(!!this.filters[j], (e) => e.child(this.chip(c, `gclear-${id}-${j}`, "Clear", false, (_e, cx) => this.clearFilter(cx, j)))))))
      .child(row("Format", h_flex().gap(4).children((FORMATS[type] ?? FORMATS.string).map(([k, label]) =>
        this.chip(c, `gf-${id}-${j}-${k}`, label, (this.formats[j] ?? "plain") === k, (_e, cx) => { this.formats[j] = k; cx.notify(); })))))
      .child(row("Stats", h_flex().flex_wrap().gap(12)
        .when(st.sum !== undefined, (e) => e.child(stat("sum", fmtNum(st.sum))).child(stat("average", fmtNum(st.avg))).child(stat("median", fmtNum(st.median)))
          .child(stat("min", fmtNum(st.min))).child(stat("max", fmtNum(st.max))))
        .child(stat("unique", fmtInt(st.unique)))
        .when(st.empty > 0, (e) => e.child(stat("empty", fmtInt(st.empty))))
        .child(div().text_color(c.grey500).child(this.paged ? "(this page)" : this.partNote(values.length).trim()))))
      .child(row("Common", h_flex().flex_wrap().gap(10).children(st.common.map(([v, n]) =>
        h_flex().gap(4).child(div().max_w(160).truncate().font_family(MONO).text_color(c.navy).child(v || "(empty)")).child(div().text_color(c.grey500).child(fmtInt(n)))))));
  }
}
