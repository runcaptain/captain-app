// CAPTAIN: Data and signals, Tables tab: Querybook's table explorer, natively. Each database connection with "Read its
// tables" (POST /api/tables/{conn}/pull, a job) and when it was last read; its schemas and tables (GET
// /api/tables/{conn}); search across every table (GET /api/tables/search); and a table page with Columns, Sample rows
// (where, order, CSV through the kit grid) and Insights (who queries it, what it's queried with, example queries that
// open in a notebook). Reading a database never writes to it.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { ResultGrid } from "./kit/grid.js";
import { openForm } from "./kit/agent.js";

const enc = encodeURIComponent;
const fmtInt = (n) => Number(n ?? 0).toLocaleString("en-US");
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

export class Tables {
  constructor(host) {
    this.host = host;
    this.conns = null;
    this.error = null;
    this.trees = new Map(); // conn -> {schemas} | {missing: sentence}
    this.open = new Set(); // expanded conns and `${conn}/${schema}`
    this.jobs = new Map(); // conn -> {id, progress}
    this.sel = null; // {conn, schema, name}
    this.page = null; // the table page
    this.tab = "columns";
    this.sample = null; // {grid, sql, ms} | {error}
    this.hits = null;
    this.note = null;
    this.order = null;
    this.desc = false;
    this.search = InputState.new({ placeholder: "Search tables and columns" });
    this.colFilter = InputState.new({ placeholder: "Filter columns" });
    this.where = InputState.new({ placeholder: "WHERE, for example: price < 100" });
    this.search.on("submit", (_e, cx) => this.find(cx));
    this.search.on("change", (_e, cx) => { if (!this.search.value().trim()) { this.hits = null; cx.notify(); } });
    this.colFilter.on("change", (_e, cx) => cx.notify());
    this.where.on("submit", (_e, cx) => this.loadSample(cx));
  }

  load(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/tables");
      if (r.ok) {
        this.conns = Array.isArray(r.data) ? r.data : [];
        this.error = null;
        for (const k of this.conns) if (k.pulled_at && !this.trees.has(k.connection)) { this.open.add(k.connection); this.loadConn(cx, k.connection); }
      } else if (!r.starting) this.error = r;
      cx.notify();
    });
  }

  loadConn(cx, conn) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/${enc(conn)}`);
      this.trees.set(conn, r.ok ? r.data : { missing: r.status === 404 ? "Not read yet." : "Couldn't load its tables." });
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
        if (r.data.status === "done" || r.data.status === "failed") {
          this.jobs.delete(conn);
          const res = r.data.result ?? {};
          this.note = r.data.status === "done"
            ? { text: `Read ${fmtInt(res.tables)} table${res.tables === 1 ? "" : "s"} in ${fmtInt(res.schemas)} schema${res.schemas === 1 ? "" : "s"} of ${conn}${(res.errors ?? []).length ? `. ${res.errors.length} couldn't be read: ${res.errors.map((e) => schemaWords(e.schema)).join(", ")}` : ""}.`, tone: (res.errors ?? []).length ? "muted" : "good" }
            : { text: sentence(r.data.error ?? r.data.message) || `Couldn't read ${conn}.`, tone: "fail" };
          this.open.add(conn);
          this.load(cx);
          this.loadConn(cx, conn);
        }
        cx.notify();
      });
    }
  }

  find(cx) {
    const q = this.search.value().trim();
    if (!q) { this.hits = null; cx.notify(); return; }
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/search?q=${enc(q)}`);
      this.hits = r.ok && Array.isArray(r.data) ? r.data : [];
      cx.notify();
    });
  }

  pick(cx, conn, schema, name) {
    this.sel = { conn, schema, name };
    this.page = null;
    this.sample = null;
    this.order = null;
    this.desc = false;
    this.where.set_value("");
    this.colFilter.set_value("");
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/${enc(conn)}/${enc(schema)}/${enc(name)}`);
      if (this.sel?.name !== name || this.sel?.schema !== schema) return;
      this.page = r.ok ? r.data : { error: sentence(r.data?.error ?? r.error) || "Couldn't load this table." };
      if (this.tab === "sample") this.loadSample(cx);
      cx.notify();
    });
    cx.notify();
  }

  loadSample(cx) {
    const s = this.sel;
    if (!s) return;
    const q = new URLSearchParams({ n: "100" });
    const w = this.where.value().trim();
    if (w) q.set("where", w);
    if (this.order) { q.set("order", this.order); if (this.desc) q.set("desc", "true"); }
    this.sample = { loading: true };
    cx.spawn(async (cx) => {
      const r = await api(`/api/tables/${enc(s.conn)}/${enc(s.schema)}/${enc(s.name)}/sample?${q}`, { timeout: 60 });
      if (this.sel !== s) return;
      this.sample = r.ok
        ? { grid: new ResultGrid({ id: "table-sample", name: `${s.name}-sample`, table: { columns: r.data.columns, rows: r.data.rows, row_count: r.data.rows.length } }), sql: r.data.sql, ms: r.data.ms }
        : { error: sentence(r.data?.error ?? r.error) || "Couldn't read sample rows." };
      cx.notify();
    });
    cx.notify();
  }

  /** An example query, as the first cell of a new notebook on that connection. */
  openInNotebook(cx, sql) {
    const s = this.sel;
    this.host.nb.seedSql = { conn: s.conn, sql };
    this.host.nb.newName.set_value(`${s.name}-query`);
    this.host.tab = "notebooks";
    if (this.host.nb.list === null) this.host.nb.load(cx);
    this.host.nb.newNotebook(cx);
    cx.notify();
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  icon(name, size, color) {
    if (["database", "table", "chevron-right", "chevron-down", "search", "refresh", "x"].includes(name)) return this.host.icon(name, size, color);
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  render(c, w) {
    const lw = Math.max(260, Math.min(360, Math.floor(w * 0.32)));
    return h_flex().id("tables").flex_1().min_h(0)
      .child(this.side(c, lw))
      .child(this.body(c, w - lw));
  }

  side(c, w) {
    return v_flex().id("tables-side").w(w).h_full().flex_none().border_r(1).border_color(c.rule)
      .child(h_flex().flex_none().m(10).h(28).px(8).gap(6).items_center().border_1().border_color(c.grey300)
        .child(this.icon("search", 13, c.grey500)).child(div().flex_1().min_w(0).child(Input.new(this.search).w_full().text_size(12).text_color(c.navy))))
      .when(!!this.note, (e) => e.child(h_flex().flex_none().px(10).pb(6).gap(8).items_start().text_size(11.5)
        .text_color(this.note.tone === "fail" ? c.fail : this.note.tone === "good" ? c.good : c.grey900)
        .child(div().flex_1().min_w(0).child(this.note.text))
        .child(div().id("tables-note-x").cursor_pointer().on_click((_e, cx) => { this.note = null; cx.notify(); }).child(this.icon("x", 11)))))
      .child(v_flex().id("tables-tree").flex_1().min_h(0).overflow_y_scrollbar()
        .when(this.hits !== null, (e) => e.children(this.hitRows(c)))
        .when(this.hits === null && this.conns === null && !this.error, (e) => e.child(Skeleton(c, 6)))
        .when(!!this.error, (e) => e.child(div().p(10).child(this.host.troubleView(c, "tables", this.error, "Couldn't load your databases.", (cx) => this.load(cx)))))
        .when(this.hits === null && Array.isArray(this.conns) && !this.conns.length, (e) => e.child(v_flex().p(12).gap(6)
          .child(div().text_size(13).font_medium().child("No database connected"))
          .child(div().text_size(12).line_height("18px").text_color(c.grey900).child("Connect your warehouse to browse its tables, their columns and sample rows, and see how your team queries them."))
          .child(Button(c, "tables-connect", "Connect a database", () => openForm("database"), { kind: "primary", small: true }))))
        .when(this.hits === null, (e) => e.children((this.conns ?? []).flatMap((k) => this.connRows(c, k)))));
  }

  hitRows(c) {
    if (!this.hits.length) return [div().p(12).text_size(12).text_color(c.grey500).child("No table or column matches.")];
    return this.hits.map((h, i) => h_flex().id(`hit-${i}`).h(40).flex_none().px(10).gap(8).items_center().cursor_pointer().hover((s) => s.bg(c.surface2))
      .on_click((_e, cx) => this.pick(cx, h.connection, h.schema, h.name))
      .child(this.icon("table", 13, c.grey500))
      .child(v_flex().flex_1().min_w(0)
        .child(div().truncate().text_size(12.5).font_family(MONO).child(h.name))
        .child(div().truncate().text_size(11).text_color(c.grey500).child(`${h.connection}, ${schemaWords(h.schema)}${h.comment ? `: ${h.comment}` : ""}`))));
  }

  connRows(c, k) {
    const conn = k.connection;
    const open = this.open.has(conn);
    const job = this.jobs.get(conn);
    const tree = this.trees.get(conn);
    const rows = [v_flex().id(`conn-${conn}`).flex_none().px(10).py(8).gap(6).border_b(1).border_color(c.rule)
      .child(h_flex().gap(6).items_center().cursor_pointer()
        .on_click((_e, cx) => { if (open) this.open.delete(conn); else { this.open.add(conn); if (!tree && k.pulled_at) this.loadConn(cx, conn); } cx.notify(); })
        .child(this.icon(open ? "chevron-down" : "chevron-right", 11, c.grey500))
        .child(this.icon("database", 14, c.navy))
        .child(div().flex_1().min_w(0).truncate().text_size(13).font_medium().child(conn))
        .when(!!k.engine, (e) => e.child(div().flex_none().h(18).px(5).flex().items_center().border_1().border_color(c.rule).text_size(10.5).text_color(c.grey900).child(k.engine))))
      .child(h_flex().gap(8).items_center().text_size(11.5).text_color(c.grey500)
        .child(div().flex_1().min_w(0).truncate().child(job ? `Reading its tables, ${Math.round((job.progress ?? 0) * 100)}%`
          : k.pulled_at ? `${fmtInt(k.tables)} tables, last read ${ago(k.pulled_at)}` : "Its tables haven't been read yet"))
        .child(Button(c, `pull-${conn}`, k.pulled_at ? "Read again" : "Read its tables", (_e, cx) => this.pull(cx, conn), { small: true, disabled: !!job })))];
    if (!open) return rows;
    if (tree?.missing) return [...rows, div().px(28).py(6).text_size(11.5).text_color(c.grey500).child(tree.missing)];
    for (const s of tree?.schemas ?? []) {
      const key = `${conn}/${s.name}`;
      const so = this.open.has(key) || (tree.schemas.length === 1 && !this.open.has(`${key}:closed`));
      rows.push(h_flex().id(`schema-${key}`).h(28).flex_none().pl(24).pr(10).gap(6).items_center().cursor_pointer().hover((st) => st.bg(c.surface2))
        .on_click((_e, cx) => { if (so) { this.open.delete(key); this.open.add(`${key}:closed`); } else { this.open.add(key); this.open.delete(`${key}:closed`); } cx.notify(); })
        .child(this.icon(so ? "chevron-down" : "chevron-right", 11, c.grey500))
        .child(div().flex_1().min_w(0).truncate().text_size(12).font_medium().text_color(c.grey900).child(schemaWords(s.name)))
        .child(div().flex_none().text_size(11).text_color(c.grey500).child(fmtInt(s.tables.length))));
      if (so) for (const t of s.tables) {
        const on = this.sel?.conn === conn && this.sel?.schema === s.name && this.sel?.name === t.name;
        rows.push(h_flex().id(`table-${key}/${t.name}`).h(28).flex_none().pl(42).pr(10).gap(6).items_center().cursor_pointer()
          .bg(on ? c.blueLight : c.surface).hover((st) => (on ? st : st.bg(c.surface2))).tooltip(t.comment ?? t.name)
          .on_click((_e, cx) => this.pick(cx, conn, s.name, t.name))
          .child(this.icon(t.type === "view" ? "eye" : "table", 12, on ? c.blue : c.grey500))
          .child(div().flex_1().min_w(0).truncate().text_size(12).font_family(MONO).text_color(on ? c.blue : c.navy).child(t.name))
          .child(div().flex_none().text_size(11).text_color(c.grey500).child(t.columns ? `${fmtInt(t.columns)} col` : "")));
      }
    }
    return rows;
  }

  body(c, w) {
    const p = this.page;
    if (!this.sel) return v_flex().flex_1().items_center().justify_center().gap(8).p(24)
      .child(this.icon("table", 22, c.grey500))
      .child(div().text_size(15).font_medium().child("Your warehouse's tables"))
      .child(div().max_w(440).text_center().text_size(12.5).line_height("19px").text_color(c.grey900)
        .child("Read a connection's tables, then pick one: its columns, a sample of its rows, and how your team queries it."));
    if (!p) return v_flex().flex_1().child(Skeleton(c, 8));
    if (p.error) return v_flex().flex_1().p(24).child(div().p(14).bg(c.failLight).border_1().border_color(c.fail).text_size(13).text_color(c.fail).child(p.error));
    const tabs = [["columns", `Columns ${fmtInt((p.columns ?? []).length)}`], ["sample", "Sample rows"], ["insights", `Insights ${fmtInt(p.insights?.queries ?? 0)}`]];
    return v_flex().flex_1().min_w(0).h_full()
      .child(v_flex().flex_none().gap(6).px(20).pt(14).pb(10).border_b(1).border_color(c.rule)
        .child(h_flex().gap(8).items_center()
          .child(this.icon(p.type === "view" ? "eye" : "table", 16, c.navy))
          .child(div().min_w(0).truncate().text_size(15).font_medium().font_family(MONO).child(p.name))
          .child(div().flex_none().h(18).px(5).flex().items_center().border_1().border_color(c.rule).text_size(10.5).text_color(c.grey900).child(p.type ?? "table")))
        .child(div().text_size(11.5).text_color(c.grey500).child(`${this.sel.conn}, ${schemaWords(p.schema)}${p.pulled_at ? `, read ${ago(p.pulled_at)}` : ""}`))
        .when(!!p.comment, (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(p.comment)))
        .when(!!p.error, (e) => e.child(div().text_size(12).text_color(c.warn).child(`Some details couldn't be read: ${p.error}`)))
        .child(h_flex().gap(4).pt(4).children(tabs.map(([id, label]) => div().id(`ttab-${id}`).h(26).px(10).flex().items_center().cursor_pointer().text_size(12)
          .border_1().border_color(this.tab === id ? c.navy : c.grey300).bg(this.tab === id ? c.navy : c.surface).text_color(this.tab === id ? c.surface : c.grey900)
          .on_click((_e, cx) => { this.tab = id; if (id === "sample" && !this.sample) this.loadSample(cx); cx.notify(); }).child(label)))))
      .child(this.tab === "columns" ? this.columns(c, p) : this.tab === "sample" ? this.sampleView(c, p) : this.insights(c, p));
  }

  columns(c, p) {
    const q = this.colFilter.value().trim().toLowerCase();
    const cols = (p.columns ?? []).filter((x) => !q || `${x.name} ${x.type} ${x.comment ?? ""}`.toLowerCase().includes(q));
    return v_flex().id("tcols").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(10).gap(6)
      .child(h_flex().h(28).w(260).px(8).gap(6).items_center().border_1().border_color(c.grey300)
        .child(this.icon("search", 12, c.grey500)).child(div().flex_1().min_w(0).child(Input.new(this.colFilter).w_full().text_size(12).text_color(c.navy))))
      .when(!(p.columns ?? []).length, (e) => e.child(div().text_size(12).text_color(c.grey500).child("No columns were read for this table. Read the connection again.")))
      .child(v_flex().border_1().border_color(c.rule)
        .children(cols.map((x, i) => h_flex().id(`tcol-${i}`).min_h(30).px(10).py(5).gap(10).items_center().border_b(1).border_color(c.rule).text_size(12.5)
          .child(div().w(200).flex_none().truncate().font_family(MONO).child(x.name))
          .child(div().w(140).flex_none().truncate().font_family(MONO).text_size(11.5).text_color(c.grey900).child(String(x.type ?? "").toLowerCase()))
          .child(div().w(70).flex_none().text_size(11).text_color(c.grey500).child(x.nullable === false ? "required" : ""))
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(c.grey900).child(x.comment ?? "")))));
  }

  sampleView(c, p) {
    const s = this.sample;
    const cols = (p.columns ?? []).map((x) => x.name);
    return v_flex().flex_1().min_h(0).px(20).py(10).gap(8)
      .child(h_flex().gap(8).items_center()
        .child(h_flex().flex_1().min_w(0).h(28).px(8).items_center().border_1().border_color(c.grey300).child(Input.new(this.where).w_full().text_size(12).font_family(MONO).text_color(c.navy)))
        .child(Button(c, "sample-run", "Show rows", (_e, cx) => this.loadSample(cx), { kind: "primary", small: true })))
      .when(cols.length > 0, (e) => e.child(h_flex().id("sample-order").gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
        .child(div().flex_none().text_color(c.grey500).child("Order by"))
        .children(["", ...cols].map((k) => div().id(`ord-${k || "none"}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
          .border_1().border_color((this.order ?? "") === k ? c.blue : c.grey300).text_color((this.order ?? "") === k ? c.blue : c.grey900)
          .on_click((_e, cx) => { if ((this.order ?? "") === k && k) this.desc = !this.desc; else { this.order = k || null; this.desc = false; } this.loadSample(cx); })
          .child(k ? `${k}${(this.order ?? "") === k ? (this.desc ? ", largest first" : ", smallest first") : ""}` : "None")))))
      .when(!s || s.loading, (e) => e.child(Skeleton(c, 8)))
      .when(!!s?.error, (e) => e.child(div().p(12).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).text_color(c.fail).child(s.error)))
      .when(!!s?.grid, (e) => e.child(div().flex_1().min_h(0).border_1().border_color(c.rule).child(s.grid.render(c, { height: 360 })))
        .child(div().text_size(11).font_family(MONO).text_color(c.grey500).truncate().child(`${s.sql}  (${s.ms} ms)`)));
  }

  insights(c, p) {
    const ins = p.insights ?? {};
    const card = (title, body) => v_flex().gap(6).p(12).border_1().border_color(c.rule).child(div().text_size(12.5).font_medium().child(title)).child(body);
    return v_flex().id("tins").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(12).gap(12)
      .when(!ins.queries, (e) => e.child(div().text_size(12.5).line_height("19px").text_color(c.grey900)
        .child("No queries through Captain read this table yet. Notebook SQL cells, SQL in the app and agents' cst sql all count once they run.")))
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
