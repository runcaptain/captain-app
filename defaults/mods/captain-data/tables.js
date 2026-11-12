// CAPTAIN: Data's database tables. Each connection with "Read its tables" (POST /api/tables/{conn}/pull, a job) and
// when it was last read, drawn as a card under Sources; and a table's page, opened from the one list under Tables:
// Rows (where, order, CSV through the kit grid) and About (its columns, who queries it, what it's queried with,
// example queries that open in a notebook). Reading a database never writes to it.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { api, query } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { ResultGrid } from "./kit/grid.js";
import { openForm } from "./kit/agent.js";
import { setShared, run } from "herdr";
import { Brand, engineTitle } from "./kit/brands.js";
import { int } from "./kit/num.js";

const enc = encodeURIComponent;
const fmtInt = (n) => int(n ?? 0);
// "Nov 6, 2026, 7:42 PM", without Intl (the app's JavaScript has no locale data).
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const stamp = (d) => `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}, ${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, "0")} ${d.getHours() < 12 ? "AM" : "PM"}`;
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
function ago(iso) {
  if (!iso) return null;
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(s)) return null;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} minutes ago`;
  if (s < 129600) return `${Math.round(s / 3600)} hours ago`;
  return `${Math.round(s / 86400)} days ago`;
}
// A schema as people read it: DuckDB's '"t-store".main' is t-store.main.
const schemaWords = (s) => String(s ?? "").replace(/"/g, "");

// How a database is named on screen while its rows load.
const ENGINES = { databricks: "Databricks", snowflake: "Snowflake", bigquery: "BigQuery", postgres: "Postgres", postgresql: "Postgres", redshift: "Redshift", mysql: "MySQL", duckdb: "DuckDB", trino: "Trino", clickhouse: "ClickHouse", athena: "Athena", mssql: "SQL Server", sqlserver: "SQL Server" };
const engineName = (e) => (e ? ENGINES[String(e).toLowerCase()] ?? e : null);

export class Tables {
  constructor(host) {
    this.host = host;
    this.conns = null;
    this.fresh = false; // the list was fetched since the tab was last shown: only then may it say "No database connected"
    this.error = null;
    this.trees = new Map(); // conn -> {schemas} | {missing: sentence}
    this.jobs = new Map(); // conn -> {id, progress}
    this.sel = null; // {conn, schema, name}
    this.page = null; // the table page
    this.tab = "rows"; // "rows" | "about"
    this.rows = null; // Rows: a kit grid paged through the whole table
    this.note = null;
    this.colFilter = InputState.new({ placeholder: "Filter columns" });
    this.colFilter.on("change", (_e, cx) => cx.notify());
  }

  load(cx) {
    const seq = (this.loadSeq = (this.loadSeq ?? 0) + 1);
    cx.spawn(async (cx) => {
      const r = await api("/api/tables");
      if (seq !== this.loadSeq) return; // a newer list was asked for: an older answer must not overwrite it
      if (r.ok) {
        this.conns = Array.isArray(r.data) ? r.data : [];
        this.fresh = true;
        this.error = null;
        for (const k of [...this.trees.keys()]) if (!this.conns.some((x) => x.connection === k)) this.trees.delete(k);
        for (const k of this.conns) if (k.pulled_at && !this.trees.has(k.connection)) this.loadConn(cx, k.connection);
      } else if (!r.starting) this.error = r;
      // The services are still starting: ask again shortly, never sit on the placeholder.
      else cx.timer.after(1000, (cx) => { if (seq === this.loadSeq) this.load(cx); });
      cx.notify();
    });
  }

  /** The tab was opened: relist the connections, since one may have been added since this screen loaded. */
  show(cx) {
    this.fresh = false;
    this.load(cx);
  }

  loadConn(cx, conn) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/${enc(conn)}`);
      this.trees.set(conn, r.ok ? r.data : { missing: r.status === 404 ? "Not read yet." : "Couldn't load its tables." });
      // A table asked for by name (props.tablesPick): open it once its database's tables are in.
      const asked = r.ok && this.want ? (r.data.schemas ?? []).find((x) => (x.tables ?? []).some((t) => t.name === this.want)) : null;
      if (asked) { const name = this.want; this.want = null; this.host.openTable(cx, conn, asked.name, name); }
      cx.notify();
    });
  }

  pull(cx, conn) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/${enc(conn)}/pull`, { body: {} });
      if (!r.ok) { this.note = { text: sentence(r.data?.error ?? r.error) || "Couldn't start reading.", tone: "fail" }; cx.notify(); return; }
      this.jobs.set(conn, { id: r.data.job, progress: 0 });
      cx.notify();
    });
  }

  /** Every half second while a database is being read. */
  tick(cx) {
    for (const [conn, j] of this.jobs) {
      if (j.polling) continue;
      j.polling = true;
      cx.spawn(async (cx) => {
        const r = await api(`/api/jobs/${enc(j.id)}`);
        j.polling = false;
        if (!r.ok) return;
        j.progress = r.data.progress ?? j.progress;
        if (j.lineage && (r.data.status === "done" || r.data.status === "failed")) {
          this.jobs.delete(conn);
          const res = r.data.result ?? {};
          const sys = j.system;
          this.note = r.data.status === "done"
            ? { text: `Read ${fmtInt(res.table_edges)} table link${res.table_edges === 1 ? "" : "s"} and ${fmtInt(res.column_edges)} column link${res.column_edges === 1 ? "" : "s"} from ${sys}, as of ${stamp(new Date())}. They show in "Where this came from" on every table.`, tone: "good" }
            : { text: sentence(r.data.error ?? r.data.message) || `Couldn't read lineage from ${sys}.`, tone: "fail" };
          cx.notify();
          return;
        }
        if (r.data.status === "done" || r.data.status === "failed") {
          this.jobs.delete(conn);
          const res = r.data.result ?? {};
          this.note = r.data.status === "done"
            ? { text: `Read ${fmtInt(res.tables)} table${res.tables === 1 ? "" : "s"} in ${fmtInt(res.schemas)} schema${res.schemas === 1 ? "" : "s"} of ${conn}${(res.errors ?? []).length ? `. ${res.errors.length} couldn't be read: ${res.errors.map((e) => schemaWords(e.schema)).join(", ")}` : ""}.`, tone: (res.errors ?? []).length ? "muted" : "good" }
            : { text: sentence(r.data.error ?? r.data.message) || `Couldn't read ${conn}.`, tone: "fail" };
          this.load(cx);
          this.loadConn(cx, conn);
          this.host.catalog.load(cx); // its tables join the one list
        }
        cx.notify();
      });
    }
  }

  /** Databricks' Unity Catalog and Snowflake keep their own lineage: read it into Captain's graph (a job). */
  readLineage(cx, conn, system) {
    const days = this.linDays ?? 30;
    cx.spawn(async (cx) => {
      const r = await api(`/api/lineage/import/${enc(conn)}`, { body: { days } });
      if (!r.ok) { this.note = { text: sentence(r.data?.error ?? r.error) || `Couldn't read lineage from ${system}.`, tone: "fail" }; cx.notify(); return; }
      this.jobs.set(conn, { id: r.data.job, progress: 0, lineage: true, system });
      cx.notify();
    });
  }

  pick(cx, conn, schema, name) {
    this.sel = { conn, schema, name };
    this.page = null;
    this.rows = null;
    this.colFilter.set_value("");
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/${enc(conn)}/${enc(schema)}/${enc(name)}`);
      if (this.sel?.name !== name || this.sel?.schema !== schema) return;
      this.page = r.ok ? r.data : { error: sentence(r.data?.error ?? r.error) || "Couldn't load this table." };
      if (this.tab === "rows") this.loadRows(cx);
      cx.notify();
    });
    cx.notify();
  }

  /** The Rows tab: the whole table, a page at a time, sorted and filtered by the services (kit grid, paged). */
  loadRows(cx) {
    const s = this.sel;
    if (!s) return;
    const path = `/api/tables/${enc(s.conn)}/${enc(s.schema)}/${enc(s.name)}/rows`;
    const engine = (this.conns ?? []).find((k) => k.connection === s.conn)?.engine;
    this.rows = new ResultGrid({ id: "table-rows", name: s.name, source: engineName(engine) ?? s.conn, load: (q) => api(`${path}${query(q)}`, { timeout: 120 }) });
    this.rows.fetch(cx);
  }

  /** "Where this came from": the lineage graph around this table (lineage names it table:<connection>/<schema.table>). */
  showLineage(cx) {
    const s = this.sel;
    this.tab = "lineage";
    this.host.page.graphFor = null;
    this.host.page.loadLineage(cx, `table:${s.conn}/${`${schemaWords(s.schema)}.${s.name}`.toLowerCase()}`);
    cx.notify();
  }

  /** An example query, as the first cell of a new notebook on that connection. */
  openInNotebook(cx, sql) {
    const s = this.sel;
    // Notebooks is its own view: it reads this and starts the notebook (captain-notebooks).
    setShared("captain.open", { section: "notebooks", seedSql: { conn: s.conn, sql, name: `${s.name}-query` }, at: Date.now() });
    run("layout.select", { stack: "sections", child: "notebooks" });
    cx.notify();
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  icon(name, size, color) {
    if (["database", "table", "chevron-right", "chevron-down", "search", "refresh", "x"].includes(name)) return this.host.icon(name, size, color);
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  /** Sources: one card per database. Its name and kind, how many tables were read and when, and the buttons that
   *  read them (and, on Databricks and Snowflake, its lineage). */
  cards(c) {
    return v_flex().id("source-dbs").gap(8)
      .when(!!this.note, (e) => e.child(h_flex().flex_none().gap(8).items_start().text_size(12)
        .text_color(this.note.tone === "fail" ? c.fail : this.note.tone === "good" ? c.good : c.grey900)
        .child(div().flex_1().min_w(0).child(this.note.text))
        .child(div().id("tables-note-x").cursor_pointer().on_click((_e, cx) => { this.note = null; cx.notify(); }).child(this.icon("x", 11)))))
      .when((this.conns === null || (!this.fresh && !this.conns.length)) && !this.error, (e) => e.child(Skeleton(c, 3)))
      .when(!!this.error, (e) => e.child(this.host.troubleView(c, "tables", this.error, "Couldn't load your databases.", (cx) => this.load(cx))))
      .when(this.fresh && Array.isArray(this.conns) && !this.conns.length, (e) => e.child(v_flex().id("no-db").p(14).gap(6).border_1().border_dashed().border_color(c.grey300)
        .child(div().text_size(13).font_medium().child("No database connected"))
        .child(div().text_size(12).line_height("18px").text_color(c.grey900).child("Connect your warehouse to read its tables where they live."))))
      .children((this.conns ?? []).map((k) => this.card(c, k)));
  }

  card(c, k) {
    const conn = k.connection;
    const job = this.jobs.get(conn);
    const lin = ["databricks", "snowflake"].includes(String(k.engine ?? "").toLowerCase());
    const system = String(k.engine ?? "").toLowerCase() === "databricks" ? "Databricks" : "Snowflake";
    return v_flex().id(`conn-${conn}`).flex_none().p(12).gap(8).border_1().border_color(c.rule)
      .child(h_flex().gap(10).items_center()
        .child(Brand(c, k.engine, 18))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().truncate().text_size(13).font_medium().child(conn))
          .child(div().truncate().text_size(12).text_color(c.grey500).child([k.engine ? engineTitle(k.engine) : null,
            job ? (job.lineage ? `Reading lineage from ${job.system}` : `Reading its tables, ${Math.round((job.progress ?? 0) * 100)}%`)
              : k.pulled_at ? `${fmtInt(k.tables)} ${k.tables === 1 ? "table" : "tables"}, last read ${ago(k.pulled_at)}` : "Its tables haven't been read yet"].filter(Boolean).join(". "))))
        .when(!!k.pulled_at, (e) => e.child(Button(c, `see-${conn}`, "See its tables", (_e, cx) => this.host.showTablesOf(cx, conn), { small: true })))
        .child(Button(c, `pull-${conn}`, k.pulled_at ? "Read again" : "Read its tables", (_e, cx) => this.pull(cx, conn), { small: true, disabled: !!job })))
      .when(lin, (e) => e.child(h_flex().gap(6).items_center().text_size(11.5).text_color(c.grey500)
        .child(div().flex_none().child("Last"))
        .children([30, 90].map((d) => div().id(`lin-days-${conn}-${d}`).h(20).px(5).flex().flex_none().items_center().cursor_pointer()
          .border_1().border_color((this.linDays ?? 30) === d ? c.blue : c.grey300).text_color((this.linDays ?? 30) === d ? c.blue : c.grey900)
          .on_click((_e, cx) => { this.linDays = d; cx.notify(); }).child(`${d} days`)))
        .child(div().flex_1())
        .child(Button(c, `lin-${conn}`, job?.lineage ? "Reading lineage" : `Read lineage from ${system}`, (_e, cx) => this.readLineage(cx, conn, system), { small: true, disabled: !!job }))));
  }

  /** A database table's page: Rows, and About (its columns and how it is queried). */
  body(c, w) {
    const p = this.page;
    if (!this.sel) return v_flex().flex_1();
    if (!p) return v_flex().flex_1().child(Skeleton(c, 8));
    if (p.error) return v_flex().flex_1().p(24).child(div().p(14).bg(c.failLight).border_1().border_color(c.fail).text_size(13).text_color(c.fail).child(p.error));
    const views = [["rows", "Rows"], ["about", "About"]];
    return v_flex().id("table-page").flex_1().min_w(0).h_full()
      .child(v_flex().flex_none().gap(6).px(20).pt(14).pb(10).border_b(1).border_color(c.rule)
        .child(h_flex().gap(8).items_center()
          .child(this.icon(p.type === "view" ? "eye" : "table", 16, c.navy))
          .child(div().min_w(0).truncate().text_size(15).font_medium().child(p.name))
          .child(div().flex_1())
          .child(this.tab === "lineage"
            ? Button(c, "table-lineage-close", "Back to the table", (_e, cx) => { this.tab = "rows"; if (!this.rows) this.loadRows(cx); cx.notify(); }, { small: true })
            : Button(c, "table-lineage-open", "Where this came from", (_e, cx) => this.showLineage(cx), { small: true })))
        .child(div().text_size(12).text_color(c.grey500).child(`${p.type === "view" ? "A view" : "A table"} in ${this.sel.conn}, ${schemaWords(p.schema)}. Read where it lives: Captain keeps no versions of it.${p.pulled_at ? ` Last read ${ago(p.pulled_at)}.` : ""}`))
        .when(!!p.comment, (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(p.comment)))
        .when(!!p.error, (e) => e.child(div().text_size(12).text_color(c.warn).child(`Some details couldn't be read: ${p.error}`)))
        .child(h_flex().gap(4).pt(4).children(views.map(([id, label]) => div().id(`ttab-${id}`).h(26).px(10).flex().items_center().cursor_pointer().text_size(12)
          .border_1().border_color(this.tab === id ? c.navy : c.grey300).bg(this.tab === id ? c.navy : c.surface).text_color(this.tab === id ? c.surface : c.grey900)
          .on_click((_e, cx) => { this.tab = id; if (id === "rows" && !this.rows) this.loadRows(cx); cx.notify(); }).child(label)))))
      .child(this.tab === "lineage" ? this.host.page.lineageView(c, w) : this.tab === "rows" ? this.rowsView(c) : v_flex().id("table-about").flex_1().min_h(0).overflow_y_scrollbar().child(this.columns(c, p)).child(this.insights(c, p)));
  }

  columns(c, p) {
    const q = this.colFilter.value().trim().toLowerCase();
    const cols = (p.columns ?? []).filter((x) => !q || `${x.name} ${x.type} ${x.comment ?? ""}`.toLowerCase().includes(q));
    return v_flex().id("tcols").flex_none().px(20).py(12).gap(6)
      .child(div().text_size(13).font_medium().child(`Columns ${fmtInt((p.columns ?? []).length)}`))
      .child(h_flex().h(28).w(260).px(8).gap(6).items_center().border_1().border_color(c.grey300)
        .child(this.icon("search", 12, c.grey500)).child(div().flex_1().min_w(0).child(Input.new(this.colFilter).w_full().text_size(12).text_color(c.navy))))
      .when(!(p.columns ?? []).length, (e) => e.child(div().text_size(12).text_color(c.grey500).child("No columns were read for this table. Read the connection again.")))
      .child(v_flex().border_1().border_color(c.rule)
        .children(cols.map((x, i) => h_flex().id(`tcol-${i}`).min_h(30).px(10).py(5).gap(10).items_center().border_b(1).border_color(c.rule).text_size(12.5)
          .child(div().flex_1().min_w(80).truncate().font_family(MONO).child(x.name))
          .child(div().flex_none().max_w(130).truncate().font_family(MONO).text_size(11.5).text_color(c.grey900).child(String(x.type ?? "").toLowerCase()))
          .when(x.nullable === false, (e) => e.child(div().flex_none().text_size(11).text_color(c.grey500).child("required")))
          .when(!!x.comment, (e) => e.child(div().flex_1().min_w(0).truncate().text_size(12).text_color(c.grey900).child(x.comment))))));
  }

  rowsView(c) {
    const g = this.rows;
    // The grid takes the height left under the tabs and scrolls inside itself: nothing pushes it below the fold.
    return v_flex().id("table-rows-tab").flex_1().min_h(0).px(20).pt(8).pb(10).gap(6)
      .when(!g, (e) => e.child(Skeleton(c, 8)))
      .when(!!g, (e) => e.child(v_flex().flex_1().min_h(240).border_1().border_color(c.rule).child(g.render(c, { fill: true })))
        .when(!!g.page?.sql, (e) => e.child(div().flex_none().text_size(11).font_family(MONO).text_color(c.grey500).truncate().child(g.page.sql))));
  }

  insights(c, p) {
    const ins = p.insights ?? {};
    const card = (title, body) => v_flex().gap(6).p(12).border_1().border_color(c.rule).child(div().text_size(12.5).font_medium().child(title)).child(body);
    return v_flex().id("tins").flex_none().px(20).pt(4).pb(16).gap(12)
      .child(div().text_size(13).font_medium().child("How it's queried"))
      .when(!ins.queries, (e) => e.child(div().text_size(12.5).line_height("19px").text_color(c.grey900)
        .child("Nobody has queried this table through Captain yet. Queries from notebooks, from SQL in the app and from agents all count here once they run.")))
      .when(!!ins.queries, (e) => e
        .child(div().text_size(12.5).text_color(c.grey900).child(`${fmtInt(ins.queries)} quer${ins.queries === 1 ? "y" : "ies"} through Captain read this table in the last 90 days.`))
        .child(h_flex().gap(12).items_start()
          .child(div().flex_1().child(card("Who queries it", v_flex().gap(4).children((ins.frequent_users ?? []).map((u) =>
            h_flex().gap(8).text_size(12).child(div().flex_1().min_w(0).truncate().child(u.user)).child(div().font_family(MONO).text_color(c.grey500).child(fmtInt(u.queries))))))))
          .child(div().flex_1().child(card("Queried with", (ins.queried_with ?? []).length
            ? v_flex().gap(4).children(ins.queried_with.map((t) => h_flex().gap(8).text_size(12)
              .child(div().flex_1().min_w(0).truncate().font_family(MONO).child(t.table ?? t.name ?? String(t))).child(div().font_family(MONO).text_color(c.grey500).child(t.queries != null ? fmtInt(t.queries) : ""))))
            : div().text_size(12).text_color(c.grey500).child("Always queried on its own.")))))
        .child(card("Example queries", v_flex().gap(8).children((ins.examples ?? []).map((x, i) => v_flex().gap(4).p(8).bg(c.surface2)
          .child(div().text_size(12).font_family(MONO).child(String(x.sql).slice(0, 600)))
          .child(h_flex().gap(10).items_center().text_size(11).text_color(c.grey500)
            .child(div().child([x.user, ago(x.at), x.rows != null ? `${fmtInt(x.rows)} rows` : null, x.ms != null ? `${x.ms} ms` : null].filter(Boolean).join(", ")))
            .child(div().flex_1())
            .child(Button(c, `ex-nb-${i}`, "Open in a notebook", (_e, cx) => this.openInNotebook(cx, x.sql), { small: true }))))))));
  }
}
