// CAPTAIN: Data and signals, Notebooks tab: a SQL cell's tools (Querybook's cell menu, snippets and query history).
//   Format (keywords upper or lower case), Explain (the engine's plan, read-only), Filled-in query (the {{names}} in
//   the cell with the notebook's values), Translate (to another engine's SQL), Snippets (insert, or save this one),
//   and History (queries run through Captain on this connection).
// {{name}} in a SQL cell is a template value: the notebook keeps the values (saved in the file), and {{today}},
// {{yesterday}} and {{week_ago}} are always there. A cell runs with its values filled in.
//
// Backend routes: POST /api/sql/format, /api/sql/transpile, /api/sql/explain; GET /api/sql/history;
// GET|POST /api/snippets, PUT|DELETE /api/snippets/{id}.
import { div } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";

const VAR_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;
const DIALECTS = [["snowflake", "Snowflake"], ["bigquery", "BigQuery"], ["databricks", "Databricks"], ["postgres", "Postgres"], ["redshift", "Redshift"],
  ["duckdb", "DuckDB"], ["trino", "Trino"], ["mysql", "MySQL"], ["tsql", "SQL Server"], ["clickhouse", "ClickHouse"]];
const BUILT_IN = ["today", "yesterday", "week_ago"];
const day = (offset) => new Date(Date.now() - offset * 86400000).toISOString().slice(0, 10);
const sentence = (t, fallback) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : fallback; };
const ago = (iso) => {
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return !Number.isFinite(m) ? "" : m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};

/** The {{names}} in a query, in order, once each. */
export function placeholders(sql) {
  const out = [];
  for (const m of String(sql ?? "").matchAll(VAR_RE)) if (!out.includes(m[1])) out.push(m[1]);
  return out;
}

/** The query with its {{names}} filled; `missing` lists names with no value. */
export function fill(sql, values) {
  const all = { today: day(0), yesterday: day(1), week_ago: day(7), ...values };
  const missing = [];
  const text = String(sql ?? "").replace(VAR_RE, (whole, name) => {
    const v = all[name];
    if (v === undefined || v === null || v === "") { if (!missing.includes(name)) missing.push(name); return whole; }
    return String(v);
  });
  return { text, missing };
}

export class SqlTools {
  constructor(nb) {
    this.nb = nb;
    this.cell = null; // the cell whose tools are open
    this.panel = null; // "explain" | "filled" | "translate" | "snippets" | "save" | "history"
    this.busy = false;
    this.note = null; // {text, tone}
    this.out = null; // the open panel's result
    this.snippets = null;
    this.history = null;
    this.varInputs = new Map(); // name -> InputState
    this.find = InputState.new({ placeholder: "Find a snippet" });
    this.find.on("change", (_e, cx) => cx.notify());
    this.title = InputState.new({ placeholder: "Name this snippet" });
  }

  // ---- template values (saved in the notebook file) ---------------------------------------------------------------

  values() {
    return this.nb.open?.base?.metadata?.captain?.vars ?? {};
  }

  setValue(name, v) {
    const open = this.nb.open;
    if (!open) return;
    open.base ??= {};
    open.base.metadata ??= { kernelspec: { name: "python3", display_name: "Python 3", language: "python" } };
    open.base.metadata.captain ??= {};
    (open.base.metadata.captain.vars ??= {})[name] = v;
    this.nb.edited();
  }

  /** What a SQL cell sends to run: its text with the values filled. Throws a sentence when one is missing. */
  rendered(cell) {
    const { text, missing } = fill(cell.source, this.values());
    if (missing.length) throw new Error(`Give ${missing.map((m) => `{{${m}}}`).join(", ")} a value first: open the cell's tools, then "Filled-in query".`);
    return text;
  }

  // ---- actions ------------------------------------------------------------------------------------------------------

  source(cell) {
    return this.nb.editors.get(cell.id)?.value() ?? cell.source;
  }

  replace(cx, cell, sql) {
    cell.source = sql;
    this.nb.editors.get(cell.id)?.set_value(sql);
    this.nb.edited();
    cx.notify();
  }

  toggle(cx, cell) {
    if (this.cell === cell.id) { this.cell = null; this.panel = null; }
    else { this.cell = cell.id; this.panel = null; this.note = null; this.out = null; }
    cx.notify();
  }

  show(cx, cell, panel) {
    this.cell = cell.id;
    this.panel = this.panel === panel ? null : panel;
    this.note = null;
    this.out = null;
    if (this.panel === "filled") {
      for (const name of placeholders(this.source(cell)).filter((n) => !BUILT_IN.includes(n))) {
        if (this.varInputs.has(name)) continue;
        const st = InputState.new({ placeholder: "value", value: String(this.values()[name] ?? "") });
        st.on("change", (_e, cx) => { this.setValue(name, st.value()); cx.notify(); });
        this.varInputs.set(name, st);
      }
    }
    if (this.panel === "snippets") this.loadSnippets(cx, cell);
    if (this.panel === "history") this.loadHistory(cx, cell);
    if (this.panel === "explain") this.explain(cx, cell);
    cx.notify();
  }

  call(cx, path, opts, done, fallback) {
    this.busy = true;
    this.note = null;
    cx.spawn(async (cx) => {
      const r = await api(path, opts);
      this.busy = false;
      if (r.ok) done(r.data);
      else this.note = { text: sentence(r.error, fallback), tone: "fail" };
      cx.notify();
    });
    cx.notify();
  }

  format(cx, cell, kase) {
    this.call(cx, "/api/sql/format", { body: { sql: this.source(cell), case: kase, conn: cell.meta.conn ?? undefined } },
      (d) => { this.replace(cx, cell, d.sql); this.note = { text: kase === "lower" ? "Formatted, keywords in lower case." : "Formatted.", tone: "good" }; },
      "Couldn't format this query.");
  }

  explain(cx, cell) {
    if (!cell.meta.conn) { this.note = { text: "Pick a connection first: the plan comes from that database.", tone: "fail" }; return; }
    let sql;
    try { sql = this.rendered({ source: this.source(cell) }); } catch (e) { this.note = { text: e.message, tone: "fail" }; return; }
    this.call(cx, "/api/sql/explain", { body: { conn: cell.meta.conn, sql } }, (d) => { this.out = d; }, "Couldn't get the plan for this query.");
  }

  translate(cx, cell, to) {
    this.call(cx, "/api/sql/transpile", { body: { sql: this.source(cell), to, conn: cell.meta.conn ?? undefined } }, (d) => { this.out = { ...d, label: DIALECTS.find(([k]) => k === to)?.[1] ?? to }; },
      "Couldn't translate this query.");
  }

  engine(cell) {
    return this.nb.conns.find((k) => k.name === cell.meta.conn)?.engine;
  }

  loadSnippets(cx, cell) {
    const e = this.engine(cell);
    this.call(cx, `/api/snippets${e ? `?engine=${encodeURIComponent(e)}` : ""}`, undefined, (d) => { this.snippets = d; }, "Couldn't load the snippets.");
  }

  insert(cx, cell, s) {
    const cur = this.source(cell);
    this.replace(cx, cell, cur.trim() ? `${cur.replace(/\s+$/, "")}\n${s.sql}` : s.sql);
    this.note = { text: s.vars?.length ? `Inserted "${s.title}". Fill ${s.vars.map((v) => `{{${v}}}`).join(", ")} in "Filled-in query".` : `Inserted "${s.title}".`, tone: "good" };
  }

  saveSnippet(cx, cell) {
    const title = this.title.value().trim();
    if (!title) { this.note = { text: "Name the snippet first.", tone: "fail" }; cx.notify(); return; }
    this.call(cx, "/api/snippets", { body: { title, sql: this.source(cell), engine: this.engine(cell) ?? null } },
      (d) => { this.title.set_value(""); this.snippets = null; this.note = { text: `Saved "${d.title}". Anyone in this workspace can insert it.`, tone: "good" }; },
      "Couldn't save the snippet.");
  }

  deleteSnippet(cx, cell, s) {
    this.call(cx, `/api/snippets/${encodeURIComponent(s.id)}`, { method: "DELETE" }, () => { this.snippets = (this.snippets ?? []).filter((x) => x.id !== s.id); this.note = { text: `Deleted "${s.title}".`, tone: "good" }; },
      "Couldn't delete the snippet.");
  }

  loadHistory(cx, cell) {
    const q = cell.meta.conn ? `?conn=${encodeURIComponent(cell.meta.conn)}&limit=50` : "?limit=50";
    this.call(cx, `/api/sql/history${q}`, undefined, (d) => { this.history = d; }, "Couldn't load the query history.");
  }

  // ---- drawing ------------------------------------------------------------------------------------------------------

  chip(c, id, label, onClick, on = false) {
    return div().id(id).h(22).px(7).flex().flex_none().items_center().cursor_pointer().text_size(11.5)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900)
      .hover((s) => s.border_color(c.blue)).on_click(onClick).child(label);
  }

  /** The tools row under a SQL cell's connection bar, and the open panel. */
  render(c, cell) {
    if (this.cell !== cell.id) return null;
    const id = cell.id;
    const p = this.panel;
    const bar = h_flex().id(`sqlt-${id}`).flex_none().min_w(0).h(32).px(10).gap(5).items_center().border_b(1).border_color(c.rule).overflow_x_scrollbar()
      .child(this.chip(c, `sqlt-fmt-${id}`, "Format", (_e, cx) => this.format(cx, cell, "upper")))
      .child(this.chip(c, `sqlt-low-${id}`, "Format, lower case", (_e, cx) => this.format(cx, cell, "lower")))
      .child(this.chip(c, `sqlt-explain-${id}`, "Explain", (_e, cx) => this.show(cx, cell, "explain"), p === "explain"))
      .child(this.chip(c, `sqlt-filled-${id}`, "Filled-in query", (_e, cx) => this.show(cx, cell, "filled"), p === "filled"))
      .child(this.chip(c, `sqlt-tr-${id}`, "Translate", (_e, cx) => this.show(cx, cell, "translate"), p === "translate"))
      .child(this.chip(c, `sqlt-snip-${id}`, "Snippets", (_e, cx) => this.show(cx, cell, "snippets"), p === "snippets"))
      .child(this.chip(c, `sqlt-save-${id}`, "Save as snippet", (_e, cx) => this.show(cx, cell, "save"), p === "save"))
      .child(this.chip(c, `sqlt-hist-${id}`, "History", (_e, cx) => this.show(cx, cell, "history"), p === "history"));
    const note = this.note ? div().text_size(11.5).text_color(this.note.tone === "fail" ? c.fail : this.note.tone === "good" ? c.good : c.grey500).child(this.note.text) : null;
    const body = p ? this[`${p}Panel`](c, cell) : null;
    return v_flex().flex_none().w_full()
      .child(bar)
      .when(!!(body || note || this.busy), (e) => e.child(v_flex().id(`sqlt-panel-${id}`).flex_none().px(10).py(8).gap(6).border_b(1).border_color(c.rule).bg(c.surface2)
        .when(this.busy, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("Working")))
        .when(!!note, (e) => e.child(note))
        .when(!!body, (e) => e.child(body))));
  }

  code(c, id, text) {
    return div().id(id).max_h(220).overflow_y_scrollbar().px(8).py(6).border_1().border_color(c.rule).bg(c.surface).text_size(12).font_family(MONO).text_color(c.navy).child(text);
  }

  explainPanel(c, cell) {
    if (!this.out) return null;
    return v_flex().gap(4)
      .child(div().text_size(11.5).text_color(c.grey500).child(`The plan from ${this.out.connection}. Nothing was run.`))
      .child(this.code(c, `sqlt-plan-${cell.id}`, this.out.plan || "The database returned an empty plan."));
  }

  filledPanel(c, cell) {
    const names = placeholders(this.source(cell)).filter((n) => !BUILT_IN.includes(n));
    const { text, missing } = fill(this.source(cell), this.values());
    return v_flex().gap(6)
      .child(div().text_size(11.5).text_color(c.grey500).child(names.length
        ? "The values below go where the query says {{name}}. They save with the notebook. {{today}}, {{yesterday}} and {{week_ago}} are always filled."
        : "This query has no {{name}} values. Write {{name}} anywhere in it to fill it in at run time."))
      .children(names.filter((n) => this.varInputs.has(n)).map((n) => h_flex().gap(8).items_center().text_size(11.5)
        .child(div().w(120).flex_none().font_family(MONO).text_color(c.navy).truncate().child(`{{${n}}}`))
        .child(div().w(220).h(24).px(6).flex().items_center().border_1().border_color(missing.includes(n) ? c.fail : c.grey300).bg(c.surface)
          .child(Input.new(this.varInputs.get(n)).w_full().text_size(11.5).font_family(MONO)))))
      .when(missing.length > 0, (e) => e.child(div().text_size(11.5).text_color(c.fail).child(`Still empty: ${missing.map((m) => `{{${m}}}`).join(", ")}. The cell won't run until they have values.`)))
      .child(this.code(c, `sqlt-filled-text-${cell.id}`, text || "This cell is empty."));
  }

  translatePanel(c, cell) {
    const from = this.engine(cell);
    return v_flex().gap(6)
      .child(div().text_size(11.5).text_color(c.grey500).child(`From ${from ?? "standard SQL"} to:`))
      .child(h_flex().id(`sqlt-to-${cell.id}`).min_w(0).gap(4).overflow_x_scrollbar()
        .children(DIALECTS.filter(([k]) => k !== from).map(([k, label]) => this.chip(c, `sqlt-to-${cell.id}-${k}`, label, (_e, cx) => this.translate(cx, cell, k), this.out?.to === k))))
      .when(!!this.out, (e) => e
        .children((this.out.warnings ?? []).map((w, i) => div().id(`sqlt-warn-${i}`).text_size(11.5).text_color(c.warn).child(`Check this by hand: ${sentence(w, "")}`)))
        .child(this.code(c, `sqlt-tr-text-${cell.id}`, this.out.sql))
        .child(h_flex().gap(6)
          .child(this.chip(c, `sqlt-use-${cell.id}`, `Use the ${this.out.label} version`, (_e, cx) => { this.replace(cx, cell, this.out.sql); this.note = { text: `This cell is now ${this.out.label} SQL. Pick a ${this.out.label} connection to run it.`, tone: "good" }; this.out = null; }))));
  }

  snippetsPanel(c, cell) {
    const q = this.find.value().trim().toLowerCase();
    const list = (this.snippets ?? []).filter((s) => !q || `${s.title} ${s.description ?? ""} ${s.sql}`.toLowerCase().includes(q));
    return v_flex().gap(6)
      .child(div().w(260).h(24).px(6).flex().items_center().border_1().border_color(c.grey300).bg(c.surface).child(Input.new(this.find).w_full().text_size(11.5)))
      .when(this.snippets !== null && !list.length, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(this.snippets.length
        ? "No snippet matches." : "No snippets yet. Write a query you reuse, then \"Save as snippet\". Write {{name}} where a value changes.")))
      .child(v_flex().id(`sqlt-snips-${cell.id}`).max_h(240).overflow_y_scrollbar().gap(4).children(list.map((s, i) => h_flex().id(`sqlt-snippet-${i}`).gap(8).items_start().px(8).py(6).border_1().border_color(c.rule).bg(c.surface)
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(h_flex().gap(6).items_center().text_size(12).font_medium().child(div().truncate().child(s.title))
            .when(!!s.golden, (e) => e.child(div().text_size(10.5).px(4).border_1().border_color(c.good).text_color(c.good).child("Recommended")))
            .when(!!s.engine, (e) => e.child(div().text_size(10.5).text_color(c.grey500).child(s.engine))))
          .when(!!s.description, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(s.description)))
          .child(div().text_size(11).font_family(MONO).text_color(c.navy).truncate().child(s.sql.split("\n")[0])))
        .child(this.chip(c, `sqlt-insert-${i}`, "Insert", (_e, cx) => this.insert(cx, cell, s)))
        .child(this.chip(c, `sqlt-del-${i}`, "Delete", (_e, cx) => this.deleteSnippet(cx, cell, s))))));
  }

  savePanel(c, cell) {
    const vars = placeholders(this.source(cell));
    return v_flex().gap(6)
      .child(div().text_size(11.5).text_color(c.grey500).child(vars.length
        ? `Saves this cell's query for everyone in the workspace. Inserting it asks for ${vars.map((v) => `{{${v}}}`).join(", ")}.`
        : "Saves this cell's query for everyone in the workspace. Write {{name}} where a value should change each time."))
      .child(h_flex().gap(6).items_center()
        .child(div().w(260).h(24).px(6).flex().items_center().border_1().border_color(c.grey300).bg(c.surface).child(Input.new(this.title).w_full().text_size(11.5)))
        .child(this.chip(c, `sqlt-save-go-${cell.id}`, "Save snippet", (_e, cx) => this.saveSnippet(cx, cell))));
  }

  historyPanel(c, cell) {
    if (this.history === null) return null;
    if (!this.history.length) return div().text_size(11.5).text_color(c.grey500).child(cell.meta.conn ? `No queries on ${cell.meta.conn} in the last 30 days.` : "No queries in the last 30 days.");
    return v_flex().id(`sqlt-hist-list-${cell.id}`).max_h(260).overflow_y_scrollbar().gap(2).children(this.history.map((h, i) => h_flex().id(`sqlt-past-${i}`).gap(8).items_center().px(8).h(28).border_1().border_color(c.rule).bg(c.surface)
      .child(div().w(8).h(8).flex_none().rounded_full().bg(h.ok === false ? c.fail : c.good))
      .child(div().flex_1().min_w(0).truncate().text_size(11.5).font_family(MONO).text_color(c.navy).child(h.sql.replace(/\s+/g, " ")))
      .child(div().flex_none().text_size(11).text_color(c.grey500).child([h.ok === false ? "failed" : h.rows !== null && h.rows !== undefined ? `${h.rows} rows` : null, h.source, h.user, ago(h.at)].filter(Boolean).join(", ")))
      .child(this.chip(c, `sqlt-reuse-${i}`, "Use", (_e, cx) => { this.replace(cx, cell, h.sql); if (h.connection && !cell.meta.conn) cell.meta.conn = h.connection; this.note = { text: "That query is in this cell now.", tone: "good" }; }))));
  }
}
