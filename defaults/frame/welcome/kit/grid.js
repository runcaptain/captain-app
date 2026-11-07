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
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar, InputState, Input } from "gpui-base";
import { writeTextFile, readTextFile, home } from "herdr";
import { MONO } from "./tokens.js";
import { icon } from "./icons.js";

const ROW = 26;
const COL = 160;
const NUM_W = 52;

export const TEXT_OPS = [["contains", "contains"], ["not_contains", "doesn't contain"], ["eq", "is"], ["ne", "isn't"], ["starts", "starts with"], ["ends", "ends with"]];
export const NUMBER_OPS = [["eq", "="], ["ne", "≠"], ["gt", ">"], ["lt", "<"], ["between", "between"]];
const FORMATS = {
  number: [["plain", "Plain"], ["commas", "1,234"], ["dollar", "$"], ["percent", "%"], ["human", "1.2k"]],
  id: [["plain", "Plain"]],
  string: [["plain", "Plain"], ["capitalize", "Capitalize"]],
  url: [["plain", "Plain"], ["link", "Link"]],
  json: [["plain", "Raw"], ["parsed", "Parsed"]],
  boolean: [["plain", "Plain"], ["yesno", "Yes / No"]],
};

const isNum = (v) => typeof v === "number" || (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v)));
const toNum = (v) => (typeof v === "number" ? v : Number(v));
const text = (v) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const fmtInt = (n) => Number(n ?? 0).toLocaleString("en-US");
const fmtNum = (n) => (Number.isInteger(n) ? fmtInt(n) : Number(n).toLocaleString("en-US", { maximumFractionDigits: 4 }));
const human = (n) => {
  const a = Math.abs(n);
  return a >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : a >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : a >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : fmtNum(n);
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
  constructor({ id, table, name }) {
    this.id = id;
    this.name = name ?? table?.var ?? "result";
    this.set(table);
  }

  /** New rows (a cell ran again): keeps sort, filters, formats and hidden columns that still apply. */
  set(table) {
    this.table = table ?? { columns: [], rows: [] };
    const rows = this.table.rows ?? [];
    this.types = (this.table.columns ?? []).map((c, j) => detectType(c.name, c.type, rows.map((r) => r[j])));
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
      if (f === "dollar") return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      if (f === "percent") return `${(n * 100).toLocaleString("en-US", { maximumFractionDigits: 2 })}%`;
      if (f === "human") return human(n);
      return String(v);
    }
    if (type === "boolean" && f === "yesno") return v === true || v === "true" ? "Yes" : "No";
    if (type === "string" && f === "capitalize") { const s = text(v); return s ? `${s[0].toUpperCase()}${s.slice(1)}` : s; }
    if (type === "json" && f === "parsed") {
      try { const o = typeof v === "string" ? JSON.parse(v) : v; return Array.isArray(o) ? `${o.length} items: ${JSON.stringify(o).slice(0, 80)}` : Object.entries(o).map(([k, x]) => `${k}: ${text(x)}`).join(", "); } catch { return text(v); }
    }
    return text(v);
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

  render(c, { height = 300 } = {}) {
    const id = this.id;
    const all = this.table.columns ?? [];
    const cols = all.map((col, j) => [col, j]).filter(([, j]) => !this.hidden.has(j));
    const rows = this.table.rows ?? [];
    const idx = this.view();
    const width = NUM_W + cols.length * COL;
    const bodyH = Math.max(ROW, Math.min(idx.length, Math.floor(height / ROW)) * ROW);
    const filters = Object.keys(this.filters).length;
    const list = v_virtual_list(`grid-${id}`, idx.length, ROW, (i) => `${id}:${idx[i]}`, (range) => {
      const out = [];
      for (let i = range.start; i < range.end; i++) {
        const r = rows[idx[i]];
        out.push(h_flex().h(ROW).border_b(1).border_color(c.rule).text_size(12).whitespace_nowrap()
          .child(div().w(NUM_W).flex_none().px(6).text_right().font_family(MONO).text_color(c.grey500).child(String(idx[i] + 1)))
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
        const s = this.sort?.col === j ? this.sort : null;
        const filtered = !!this.filters[j];
        return h_flex().w(COL).flex_none().h_full().px(8).gap(4).items_center()
          .child(div().id(`gh-${id}-${j}`).flex_1().min_w(0).truncate().cursor_pointer().text_color(s || filtered ? c.blue : c.navy)
            .tooltip(`${col.name}: ${this.types[j]}${col.type ? ` (${col.type})` : ""}. Click to sort.`)
            .on_click((_e, cx) => this.toggleSort(cx, j)).child(col.name))
          .when(!!s, (e) => e.child(svg(icon("arrow-up-down")).size(11).flex_none().text_color(c.blue)))
          .when(filtered, (e) => e.child(this.icon("filter", 11, c.blue)))
          .child(div().id(`gm-${id}-${j}`).size(18).flex().flex_none().items_center().justify_center().cursor_pointer()
            .text_color(this.menu === j ? c.blue : c.grey500).hover((st) => st.text_color(c.blue)).tooltip("Filter, format, stats, hide")
            .on_click((_e, cx) => this.openMenu(cx, j)).child(this.icon("chevron-down", 12)));
      }));
    const total = this.table.row_count ?? rows.length;
    return v_flex().id(`grid-wrap-${id}`).w_full()
      .when(this.menu !== null && this.menu !== undefined && all[this.menu], (e) => e.child(this.columnMenu(c, this.menu)))
      .child(div().id(`gx-${id}`).w_full().overflow_x_scrollbar().child(v_flex().w(width)
        .child(header)
        .child(div().h(bodyH).relative()
          .when(!idx.length, (e) => e.child(div().px(10).py(4).text_size(12).text_color(c.grey500).child(filters ? "No rows match the filters." : "No rows.")))
          .when(idx.length > 0, (e) => e.child(list.size_full()).child(Scrollbar.vertical(`grid-${id}`).viewport_from_layout().absolute().inset_0())))))
      .child(h_flex().h(28).px(10).gap(10).items_center().text_size(11.5).text_color(c.grey500).border_t(1).border_color(c.rule)
        .when(!!this.table.var, (e) => e.child(div().font_family(MONO).text_color(c.navy).child(this.table.var)))
        .child(div().child(filters ? `${fmtInt(idx.length)} of ${fmtInt(rows.length)} rows match` : `${fmtInt(total)} row${total === 1 ? "" : "s"}, ${all.length} column${all.length === 1 ? "" : "s"}`))
        .when(rows.length < total, (e) => e.child(div().child(`showing the first ${fmtInt(rows.length)}${this.table.var ? `: all of them are in ${this.table.var}` : ""}`)))
        .when(!!this.table.limited, (e) => e.child(div().text_color(c.warn).child(this.table.limit ? `Limited to ${fmtInt(this.table.limit)} rows: raise the row limit for more` : "The query hit its row limit")))
        .when(this.hidden.size > 0, (e) => e.child(div().id(`gshow-${id}`).cursor_pointer().text_color(c.blue)
          .on_click((_e, cx) => { this.hidden.clear(); cx.notify(); }).child(`${this.hidden.size} hidden, show all`)))
        .child(div().flex_1())
        .when(!!this.note, (e) => e.child(div().min_w(0).truncate().text_color(c.grey900).child(this.note)))
        .child(div().id(`gcsv-${id}`).flex_none().cursor_pointer().text_color(c.blue).on_click((_e, cx) => cx.spawn(async (cx) => this.exportCsv(cx))).child("Download CSV"))
        .child(div().id(`gtsv-${id}`).flex_none().cursor_pointer().text_color(c.blue).on_click((_e, cx) => this.copyTsv(cx)).child("Copy TSV")));
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
        .child(this.chip(c, `gs-asc-${id}-${j}`, type === "number" ? "Smallest first" : "A to Z", this.sort?.col === j && !this.sort.desc, (_e, cx) => { this.sort = { col: j, desc: false }; cx.notify(); }))
        .child(this.chip(c, `gs-desc-${id}-${j}`, type === "number" ? "Largest first" : "Z to A", this.sort?.col === j && this.sort.desc, (_e, cx) => { this.sort = { col: j, desc: true }; cx.notify(); }))))
      .child(row("Filter", h_flex().flex_wrap().gap(4).items_center()
        .children(ops.map(([k, label]) => this.chip(c, `gop-${id}-${j}-${k}`, label, op === k, (_e, cx) => { (this.pendingOp ??= {})[j] = k; cx.notify(); })))
        .when(!!a, (e) => e.child(box(a)))
        .when(!!b && op === "between", (e) => e.child(box(b)))
        .child(this.chip(c, `gapply-${id}-${j}`, "Apply", false, (_e, cx) => this.applyFilter(cx, j)))
        .when(!!this.filters[j], (e) => e.child(this.chip(c, `gclear-${id}-${j}`, "Clear", false, (_e, cx) => this.clearFilter(cx, j))))))
      .child(row("Format", h_flex().gap(4).children((FORMATS[type] ?? FORMATS.string).map(([k, label]) =>
        this.chip(c, `gf-${id}-${j}-${k}`, label, (this.formats[j] ?? "plain") === k, (_e, cx) => { this.formats[j] = k; cx.notify(); })))))
      .child(row("Stats", h_flex().flex_wrap().gap(12)
        .when(st.sum !== undefined, (e) => e.child(stat("sum", fmtNum(st.sum))).child(stat("average", fmtNum(st.avg))).child(stat("median", fmtNum(st.median)))
          .child(stat("min", fmtNum(st.min))).child(stat("max", fmtNum(st.max))))
        .child(stat("unique", fmtInt(st.unique)))
        .when(st.empty > 0, (e) => e.child(stat("empty", fmtInt(st.empty))))
        .child(div().text_color(c.grey500).child(this.partNote(values.length).trim()))))
      .child(row("Common", h_flex().flex_wrap().gap(10).children(st.common.map(([v, n]) =>
        h_flex().gap(4).child(div().max_w(160).truncate().font_family(MONO).text_color(c.navy).child(v || "(empty)")).child(div().text_color(c.grey500).child(fmtInt(n)))))));
  }
}
