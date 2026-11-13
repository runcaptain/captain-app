// CAPTAIN: Data, Tables: every table of the workspace, grouped by where it lives (Edgar, 2026-11-12, on the first
// build: "you need to separate it by database... A giant wall of tables is not good. It needs to be a dropdown.
// It needs to be searchable with pagination per type of database... a small paginated list of 5 tables").
//
// One collapsible group per place: each connected database (its mark, its name and kind, a count), and each
// folder of the tables Captain keeps versions of. Groups start closed and the ones a person opens are remembered.
// An open group has its own search box and shows its tables five at a time, with Previous and Next and "1 to 5 of
// 39". A database with more than 15 tables is grouped again by schema; a smaller one says the schema in "Where it
// lives". The search box at the top asks the services (so a column's name finds its table) and opens every group
// that has a match.
//
// A dataset is a table Captain keeps versions of, and the Versions column says so. GET /api/data/tables answers
// the whole list. Clicking a row opens that table's page in place.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { shared, setShared } from "herdr";
import { workspace } from "./kit/cst.js";
import { api } from "./kit/cst.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand, engineTitle } from "./kit/brands.js";
import { int } from "./kit/num.js";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** When something was last read, in words: "today", "yesterday", "5 days ago", then the date. Takes a day
 *  ("2026-11-12") or a full time. */
export function readWords(when, now = Date.now()) {
  if (!when) return "Not read yet";
  const t = Date.parse(String(when).length === 10 ? `${when}T00:00:00Z` : when);
  if (!Number.isFinite(t)) return "Not read yet";
  const days = Math.floor((now - t) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 30) return `${days} days ago`;
  const d = new Date(t);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
}

/** The Versions column: a dataset says how many Captain keeps, a database table that it keeps none. */
export function versionWords(row) {
  if (row.kind !== "dataset") return "Read where it lives";
  const n = row.versions ?? 1;
  return n === 1 ? "Captain keeps 1 version" : `Captain keeps ${int(n)} versions`;
}

export const PAGE = 5; // tables shown at a time in an open group
export const BY_SCHEMA_OVER = 15; // a database with more tables than this is grouped again by schema

/** A description as plain words for a tooltip: markdown links become their words, line breaks become spaces, and
 *  it is cut to about two lines, so it is never a raw line across the window. */
export function plainTip(text, max = 150) {
  const t = String(text ?? "").replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[`*_#>]+/g, "").replace(/https?:\/\/\S+/g, "").replace(/\s+/g, " ").trim();
  return t.length > max ? `${t.slice(0, max - 3).trimEnd()}...` : t;
}

const schemaWords = (s) => String(s ?? "").replace(/"/g, "");

/** The groups for a list of rows: [{id, label, sub, engine?, rows, own?, parts?}], each connected database (by
 *  name), then one group for the tables Captain keeps. `rows` is every table in a group, `parts` the groups inside
 *  it (a big database's schemas, Captain's folders) and `own` the tables shown directly in it. */
export function groupsOf(rows) {
  const dbs = new Map(), folders = new Map();
  for (const r of rows ?? []) {
    if (r.kind === "table") (dbs.get(r.connection) ?? dbs.set(r.connection, []).get(r.connection)).push(r);
    else { const f = r.folder ?? ""; (folders.get(f) ?? folders.set(f, []).get(f)).push(r); }
  }
  const out = [];
  for (const [conn, list] of [...dbs].sort(([a], [b]) => a.localeCompare(b))) {
    const g = { id: `db:${conn}`, label: conn, sub: engineTitle(list[0].engine) ?? "Database", engine: list[0].engine, rows: list };
    if (list.length > BY_SCHEMA_OVER) {
      const by = new Map();
      for (const r of list) { const k = schemaWords(r.schema); (by.get(k) ?? by.set(k, []).get(k)).push(r); }
      if (by.size > 1) g.parts = [...by].sort(([a], [b]) => a.localeCompare(b)).map(([name, rs]) => ({ id: `${g.id}/${name}`, label: name, rows: rs }));
    }
    out.push(g);
  }
  // Captain's own tables are ONE group. It opens to its folders, nested by their path; a folder holding a single
  // table shows that table directly, and tables in no folder come last.
  const kept = [...folders.values()].flat();
  if (kept.length) {
    const node = (path) => ({ path, own: folders.get(path) ?? [], kids: [...new Set([...folders.keys()].filter((f) => f && (path ? f.startsWith(`${path}/`) : true))
      .map((f) => (path ? `${path}/` : "") + f.slice(path ? path.length + 1 : 0).split("/")[0]))].sort((a, b) => a.localeCompare(b)).map(node) });
    const build = (n, top) => {
      const parts = [], own = [];
      for (const k of n.kids) {
        const g = build(k, false);
        if (g.rows.length === 1) own.push(g.rows[0]); else parts.push(g);
      }
      own.push(...n.own);
      const rows = [...parts.flatMap((x) => x.rows), ...own];
      return top
        ? { id: "kept", label: "Kept in Captain", sub: "Tables Captain keeps versions of", path: "", rows, own, ...(parts.length ? { parts } : {}) }
        : { id: `folder:${n.path}`, label: n.path.split("/").pop(), folder: true, path: n.path, rows, own, ...(parts.length ? { parts } : {}) };
    };
    out.push(build(node(""), true));
  }
  return out;
}

/** A group and every group inside it. */
export const withParts = (g) => [g, ...(g.parts ?? []).flatMap(withParts)];

export class Catalog {
  constructor(host) {
    this.host = host;
    this.rows = null; // null until the services answer
    this.connections = [];
    this.error = null;
    this.page = new Map(); // group id -> the first row shown
    this.finds = new Map(); // group id -> its own search box (made when the rows arrive, never while drawing)
    this.find = InputState.new({ placeholder: "Search every table and column" });
    this.find.on("submit", (_e, cx) => this.load(cx));
    this.find.on("change", (_e, cx) => { this.typed = Date.now(); cx.notify(); });
    // The groups a person opened, remembered per workspace (shared values outlive the app).
    this.open = new Set((shared("captain.data.groups") ?? {})[workspace() ?? ""] ?? []);
  }

  remember() {
    setShared("captain.data.groups", { ...(shared("captain.data.groups") ?? {}), [workspace() ?? ""]: [...this.open] });
  }

  load(cx) {
    const seq = (this.seq = (this.seq ?? 0) + 1);
    const q = this.find.value().trim();
    this.asked = q;
    cx.spawn(async (cx) => {
      const r = await api(`/api/data/tables${q ? `?q=${encodeURIComponent(q)}` : ""}`);
      if (seq !== this.seq) return; // a newer search was asked for
      if (r.ok) {
        this.rows = r.data?.rows ?? [];
        this.connections = r.data?.connections ?? [];
        this.error = null;
        this.groups = groupsOf(this.rows);
        // Each group's search box, and each schema's: made here, outside drawing.
        for (const g of this.groups.flatMap(withParts)) {
          if (!this.finds.has(g.id)) {
            const st = InputState.new({ placeholder: `Search ${g.label}` });
            st.on("change", (_e, cx) => { this.page.set(g.id, 0); cx.notify(); });
            this.finds.set(g.id, st);
          }
        }
        // A search across everything opens the groups that have a match.
        this.matched = q ? new Set(this.groups.flatMap(withParts).map((x) => x.id)) : null;
        if (q) this.page = new Map();
      } else if (!r.starting) this.error = r;
      this.starting = !r.ok && !!r.starting;
      cx.notify();
    });
  }

  /** From the screen's timer: search a moment after typing stops. */
  tick(cx) {
    if (this.typed && Date.now() - this.typed > 350 && this.find.value().trim() !== (this.asked ?? "")) { this.typed = null; this.load(cx); }
  }

  isOpen(id) { return this.matched ? this.matched.has(id) : this.open.has(id); }

  toggle(cx, id) {
    if (this.matched) { if (this.matched.has(id)) this.matched.delete(id); else this.matched.add(id); }
    else { if (this.open.has(id)) this.open.delete(id); else this.open.add(id); this.remember(); }
    cx.notify();
  }

  /** Open the group a table is in (a source's "See its tables", a table just imported). */
  reveal(id) {
    this.open.add(id);
    // A folder is inside "Kept in Captain", and inside the folders above it.
    if (id.startsWith("folder:")) {
      this.open.add("kept");
      const segs = id.slice(7).split("/");
      for (let i = 1; i < segs.length; i++) this.open.add(`folder:${segs.slice(0, i).join("/")}`);
    }
    this.remember();
  }

  openRow(cx, r) {
    if (r.kind === "dataset") this.host.openDataset(cx, r.name);
    else this.host.openTable(cx, r.connection, r.schema, r.name);
  }

  /** An open group's tables: its search box, five rows, and the pager. */
  tables(c, g, narrow, depth) {
    const q = (this.finds.get(g.id)?.value() ?? "").trim().toLowerCase();
    const mine = g.own ?? g.rows;
    const all = q ? mine.filter((r) => `${r.name} ${r.where} ${r.description ?? ""}`.toLowerCase().includes(q)) : mine;
    const start = Math.min(this.page.get(g.id) ?? 0, Math.max(0, Math.floor((all.length - 1) / PAGE) * PAGE));
    const rows = all.slice(start, start + PAGE);
    const col = (t, width, right) => div().w(width).flex_none().px(8).truncate().when(!!right, (e) => e.text_right()).child(t);
    const versions = (r) => (narrow ? (r.kind === "dataset" ? `${int(r.versions ?? 1)} kept` : "Live") : versionWords(r));
    // In a database the place is the schema; in a folder it is what the table was made from.
    const where = (r) => (r.kind === "table" ? schemaWords(r.schema) : r.where);
    const pager = (id, label, to, off) => div().id(`${id}-${g.id}`).h(24).px(8).flex().flex_none().items_center().text_size(12).border_1().border_color(c.grey300)
      .text_color(off ? c.grey500 : c.navy).when(!off, (e) => e.cursor_pointer().hover((s) => s.border_color(c.blue).text_color(c.blue)).on_click((_e, cx) => { this.page.set(g.id, to); cx.notify(); })).child(label);
    const pad = 20 + depth * 20;
    return v_flex().id(`cat-tables-${g.id}`).flex_none().pl(pad).pr(12).pb(10).gap(6)
      .child(h_flex().gap(8).items_center()
        .when(mine.length > PAGE && this.finds.has(g.id), (e) => e.child(h_flex().id(`cat-gfind-${g.id}`).w(240).h(26).px(8).gap(6).items_center().border_1().border_color(q ? c.blue : c.grey300)
          .child(svg(icon("search")).size(11).flex_none().text_color(c.grey500))
          .child(div().flex_1().min_w(0).child(Input.new(this.finds.get(g.id)).w_full().text_size(12).text_color(c.navy)))))
        .child(div().flex_1())
        .child(div().id(`cat-range-${g.id}`).flex_none().text_size(12).text_color(c.grey500).child(all.length ? `${int(start + 1)} to ${int(start + rows.length)} of ${int(all.length)}` : "No table matches"))
        .when(all.length > PAGE, (e) => e
          .child(pager("cat-prev", "Previous", Math.max(0, start - PAGE), start === 0))
          .child(pager("cat-next", "Next", start + PAGE, start + PAGE >= all.length))))
      .when(rows.length > 0, (e) => e.child(v_flex().border_1().border_color(c.rule)
        .child(h_flex().h(26).bg(c.surface2).border_b(1).border_color(c.rule).text_size(11.5).font_medium().text_color(c.grey900).items_center()
          .child(div().flex_1().min_w(120).px(8).child("Name"))
          .child(div().flex_1().min_w(100).px(8).child("Where it lives"))
          .child(col("Rows", narrow ? 72 : 96, true))
          .child(col("Versions", narrow ? 84 : 190))
          .child(col("Last read", narrow ? 100 : 110)))
        .children(rows.map((r) => h_flex().id(`cat-row-${r.kind}-${r.kind === "table" ? `${r.connection}/${schemaWords(r.schema)}/` : ""}${r.name}`).h(34).flex_none().items_center().cursor_pointer().border_b(1).border_color(c.rule).text_size(12.5)
          .hover((s) => s.bg(c.surface2))
          // Plain words, about two lines: never a raw line of markdown across the window.
          .when(!!plainTip(r.description), (e) => e.tooltip(plainTip(r.description)))
          .on_click((_e, cx) => this.openRow(cx, r))
          .child(h_flex().flex_1().min_w(120).px(8).gap(8).items_center()
            .child(svg(icon(r.type === "view" ? "layers" : "table")).size(13).flex_none().text_color(c.grey500))
            .child(div().min_w(0).truncate().text_color(c.navy).child(r.kind === "dataset" && g.path && r.name.startsWith(`${g.path}/`) ? r.name.slice(g.path.length + 1) : r.name))
            .when(!!r.matched_column, (x) => x.child(div().flex_none().text_size(11).text_color(c.grey500).child(`column ${r.matched_column}`))))
          .child(div().flex_1().min_w(100).px(8).truncate().text_color(c.grey900).child(where(r)))
          .child(col(r.rows != null ? int(r.rows) : "", narrow ? 72 : 96, true))
          .child(col(versions(r), narrow ? 84 : 190))
          .child(col(readWords(r.last_read), narrow ? 100 : 110).text_color(c.grey500))))));
  }

  /** One group's heading: a chevron, the source's mark, its name, what it is, and how many tables. */
  heading(c, g, depth) {
    const open = this.isOpen(g.id);
    return h_flex().id(`cat-group-${g.id}`).h(depth ? 34 : 44).flex_none().pl(20 + depth * 20).pr(20).gap(10).items_center().cursor_pointer().hover((s) => s.bg(c.surface2))
      .on_click((_e, cx) => this.toggle(cx, g.id))
      .child(svg(icon(open ? "chevron-down" : "chevron-right")).size(12).flex_none().text_color(c.grey500))
      .when(!depth, (e) => e.child(g.engine ? Brand(c, g.engine, 16) : svg(icon("database")).size(15).flex_none().text_color(c.grey500)))
      .when(!!depth && !!g.folder, (e) => e.child(svg(icon("folder")).size(13).flex_none().text_color(c.grey500)))
      .child(div().flex_none().max_w(360).truncate().text_size(depth ? 12.5 : 13.5).font_medium().text_color(c.navy).child(g.label))
      .when(!depth && !!g.sub, (e) => e.child(div().min_w(0).truncate().text_size(12).text_color(c.grey500).child(g.sub)))
      .child(div().flex_1())
      .child(div().id(`cat-count-${g.id}`).flex_none().text_size(12).text_color(c.grey500).child(`${int(g.rows.length)} ${g.rows.length === 1 ? "table" : "tables"}`));
  }

  /** A group: its heading, and when open the groups inside it (a big database's schemas, Captain's folders) and
   *  then the tables that sit directly in it. */
  group(c, g, narrow, depth) {
    return v_flex().flex_none().child(this.heading(c, g, depth))
      .when(this.isOpen(g.id), (e) => e
        .children((g.parts ?? []).map((p) => this.group(c, p, narrow, depth + 1)))
        .when(!g.parts || (g.own ?? []).length > 0, (x) => x.child(this.tables(c, g, narrow, depth))));
  }

  render(c, w) {
    const groups = this.groups ?? [];
    const q = this.find.value().trim();
    const unread = this.connections.filter((k) => !k.pulled_at);
    const narrow = w < 900;
    const total = (this.rows ?? []).length;
    return v_flex().id("catalog").flex_1().min_h(0)
      .on_drop((e, cx) => this.host.lib.importPaths(cx, e.paths ?? [], ""))
      .child(h_flex().flex_none().flex_wrap().gap(8).px(20).py(10).items_center().border_b(1).border_color(c.rule)
        .child(h_flex().id("cat-find").w(300).h(28).px(8).gap(6).items_center().border_1().border_color(q ? c.blue : c.grey300)
          .child(svg(icon("search")).size(12).flex_none().text_color(c.grey500))
          .child(div().flex_1().min_w(0).child(Input.new(this.find).w_full().text_size(12).text_color(c.navy))))
        .when(!!q, (e) => e.child(div().id("cat-find-clear").flex_none().cursor_pointer().text_size(12).text_color(c.blue).on_click((_e, cx) => { this.find.set_value(""); this.load(cx); }).child("Clear")))
        .child(div().flex_1())
        .child(div().id("cat-count").flex_none().text_size(12).text_color(c.grey500).child(this.rows === null ? "" : `${int(total)} ${total === 1 ? "table" : "tables"}`)))
      .when(!!this.host.lib.job, (e) => e.child(this.host.lib.progress(c)))
      .when(!!this.host.lib.summary, (e) => e.child(this.host.lib.summaryPanel(c)))
      // A database connected but never read lists nothing: say so, with the button that reads it.
      .when(unread.length > 0, (e) => e.child(h_flex().id("cat-unread").flex_none().px(20).py(8).gap(10).items_center().border_b(1).border_color(c.rule).text_size(12.5).text_color(c.grey900)
        .child(div().flex_1().min_w(0).child(`${unread.map((k) => k.connection).join(", ")} ${unread.length === 1 ? "is connected, but its tables haven't been read yet" : "are connected, but their tables haven't been read yet"}.`))
        .child(Button(c, "cat-read", unread.length === 1 ? "Read its tables" : "Read their tables", (_e, cx) => this.host.showSources(cx, unread[0].connection), { small: true }))))
      .child(v_flex().id("cat-list").flex_1().min_h(0).overflow_y_scrollbar()
        .when(this.rows === null && !this.error, (e) => e.child(Skeleton(c, 8)))
        .when(!!this.error, (e) => e.child(div().p(20).child(this.host.troubleView(c, "tables-list", this.error, "Couldn't load your tables.", (cx) => this.load(cx)))))
        .when(this.rows !== null && !groups.length && !this.error, (e) => e.child(div().id("cat-none").p(20).text_size(12.5).text_color(c.grey500)
          .child(q ? `No table matches "${q}". Search looks at names, where a table lives and column names.` : "No tables yet.")))
        .children(groups.map((g) => v_flex().id(`cat-group-wrap-${g.id}`).flex_none().border_b(1).border_color(c.rule).child(this.group(c, g, narrow, 0)))));
  }
}
