// CAPTAIN: Data and signals, Datasets tab, as a directory: the person's own folders of datasets.
// - A folder tree (GET /api/datasets/tree) with breadcrumbs. New folder, rename, remove an empty one.
// - Select datasets (Cmd-click adds, Shift-click a range), then drag them onto a folder or use "Move to"
//   (POST /api/library/move). Moving copies nothing and changes no version.
// - Drop files or folders from Finder onto the tree or onto a folder, or use "Import files" with a path or the folder
//   picker: POST /api/datasets/import {paths, folder} runs as a job. Its progress shows, then what happened: added,
//   updated (a new version), unchanged, and each skipped file with its reason.
// - An empty workspace is one big drop zone. The demo store is a small link under it.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { pickFolder, home } from "herdr";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { openForm } from "./kit/agent.js";

const fmtInt = (n) => Number(n ?? 0).toLocaleString("en-US");
const plural = (n, w) => `${fmtInt(n)} ${w}${n === 1 ? "" : "s"}`;
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const base = (p) => String(p).split("/").filter(Boolean).pop() ?? p;
const refName = (ref) => String(ref ?? "").replace(/@v\d+$/, "");
const refVersion = (ref) => /@v(\d+)$/.exec(String(ref ?? ""))?.[1] ?? null;

/** Every dataset under a folder node, depth first. */
function datasetsIn(node) {
  return [...(node.datasets ?? []), ...(node.folders ?? []).flatMap(datasetsIn)];
}
function find(node, path) {
  if ((node.path ?? "") === path) return node;
  for (const f of node.folders ?? []) { const r = find(f, path); if (r) return r; }
  return null;
}
function allFolders(node, out = []) {
  for (const f of node.folders ?? []) { out.push(f.path); allFolders(f, out); }
  return out;
}

export class Library {
  /** host: the Data view (select, load, classColor, icon). */
  constructor(host) {
    this.host = host;
    this.tree = null;
    this.error = null;
    this.open = new Set(); // expanded folder paths
    this.cur = ""; // the folder the breadcrumbs show; new folders and imports go here
    this.picked = new Set(); // selected dataset names
    this.anchor = null;
    this.drag = null; // {names} while a press on a dataset is held
    this.job = null; // {id, label, progress, status, message}
    this.summary = null; // the last import's result
    this.note = null; // {text, tone}
    this.mode = null; // "new" | "import" | "rename" | "move"
    this.newFolder = InputState.new({ placeholder: "Folder name" });
    this.importPath = InputState.new({ placeholder: "~/Downloads/catalogs" });
    this.renameTo = InputState.new({ placeholder: "New name" });
    this.newFolder.on("submit", (_e, cx) => this.makeFolder(cx));
    this.importPath.on("submit", (_e, cx) => this.importPaths(cx, [expand(this.importPath.value())], this.cur));
    this.renameTo.on("submit", (_e, cx) => this.renameFolder(cx));
  }

  // ---- data ------------------------------------------------------------------------------------------------------

  load(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/datasets/tree");
      if (r.ok) {
        this.tree = r.data ?? { path: "", folders: [], datasets: [] };
        this.error = null;
        if (!this.opened) { this.opened = true; for (const f of this.tree.folders ?? []) this.open.add(f.path); }
        if (this.cur && !find(this.tree, this.cur)) this.cur = "";
        if (this.revealing) { this.reveal(this.revealing); }
      } else if (!r.starting) this.error = r;
      cx.notify();
    });
  }

  /** Open the tree down to a dataset's folder and make that folder current (after an import, or "Open" from elsewhere).
   *  Waits for the next tree load when the dataset isn't in this one yet. */
  reveal(name, cx) {
    const walk = (node) => {
      if ((node.datasets ?? []).some((d) => d.name === name)) return node.path ?? "";
      for (const f of node.folders ?? []) { const p = walk(f); if (p !== null) return p; }
      return null;
    };
    const path = this.tree ? walk(this.tree) : null;
    if (path === null) { this.revealing = name; if (cx) this.load(cx); return; }
    this.revealing = null;
    const parts = path ? path.split("/") : [];
    for (let i = 1; i <= parts.length; i++) this.open.add(parts.slice(0, i).join("/"));
    this.cur = path;
  }

  refresh(cx) {
    this.load(cx);
    this.host.load(cx);
  }

  say(text, tone = "muted") { this.note = { text, tone }; }

  async call(cx, path, opts, done) {
    const r = await api(path, opts);
    if (r.ok) { if (done) this.say(done, "good"); this.refresh(cx); }
    else this.say(sentence(r.data?.detail ?? r.data?.error ?? r.error) || "That didn't work.", "fail");
    cx.notify();
    return r;
  }

  makeFolder(cx) {
    const name = this.newFolder.value().trim().replace(/\//g, "-");
    if (!name) return;
    const path = this.cur ? `${this.cur}/${name}` : name;
    this.newFolder.set_value("");
    this.mode = null;
    this.open.add(this.cur);
    cx.spawn(async (cx) => { await this.call(cx, "/api/library/folders", { body: { path } }, `Made ${path}.`); this.cur = path; this.open.add(path); });
  }

  renameFolder(cx) {
    const to = this.renameTo.value().trim().replace(/\//g, "-");
    if (!to || !this.cur) return;
    const parent = this.cur.split("/").slice(0, -1).join("/");
    const dest = parent ? `${parent}/${to}` : to;
    const from = this.cur;
    this.mode = null;
    cx.spawn(async (cx) => { const r = await this.call(cx, "/api/library/rename", { body: { from, to: dest } }, `Renamed ${base(from)} to ${to}.`); if (r.ok) this.cur = dest; });
  }

  removeFolder(cx) {
    const path = this.cur;
    if (!path) return;
    cx.spawn(async (cx) => {
      const r = await this.call(cx, `/api/library/folders?path=${encodeURIComponent(path)}`, { method: "DELETE" }, `Removed ${base(path)}.`);
      if (r.ok) this.cur = path.split("/").slice(0, -1).join("/");
    });
  }

  move(cx, names, folder) {
    if (!names.length) return;
    this.mode = null;
    cx.spawn(async (cx) => {
      const r = await this.call(cx, "/api/library/move", { body: { names, folder } }, `Moved ${plural(names.length, "dataset")} to ${folder || "the top level"}.`);
      if (r.ok) { this.picked.clear(); this.open.add(folder); }
    });
  }

  importPaths(cx, paths, folder) {
    paths = paths.filter(Boolean);
    if (!paths.length) { this.say("Type the path of a file or folder, or choose a folder.", "fail"); cx.notify(); return; }
    this.mode = null;
    this.summary = null;
    this.note = null;
    const label = paths.length === 1 ? base(paths[0]) : plural(paths.length, "item");
    cx.spawn(async (cx) => {
      const r = await api("/api/datasets/import", { body: { paths, folder: folder ?? "" } });
      if (!r.ok) { this.say(sentence(r.data?.detail ?? r.error) || "Couldn't start the import.", "fail"); cx.notify(); return; }
      this.importPath.set_value("");
      this.job = { id: r.data.job, label, folder: folder ?? "", progress: 0, status: "running", message: "" };
      this.open.add(folder ?? "");
      cx.notify();
    });
    cx.notify();
  }

  chooseFolder(cx) {
    cx.spawn(async (cx) => {
      const p = await pickFolder({ prompt: "Import" });
      if (p) this.importPaths(cx, [p], this.cur);
    });
  }

  /** Every half second while an import runs: its progress, then what it did. */
  tick(cx) {
    const j = this.job;
    if (!j || j.status !== "running" || j.polling) return;
    j.polling = true;
    cx.spawn(async (cx) => {
      const r = await api(`/api/jobs/${encodeURIComponent(j.id)}`);
      j.polling = false;
      if (!r.ok) return;
      j.progress = r.data.progress ?? j.progress;
      j.message = r.data.message ?? "";
      if (r.data.status === "done") {
        j.status = "done";
        this.summary = { ...r.data.result, folder: j.folder, label: j.label };
        this.job = null;
        this.refresh(cx);
        // The dataset on screen got a new version: show it.
        const sel = this.host.sel;
        if (sel && (r.data.result?.updated ?? []).some((x) => refName(x.ref) === sel)) this.host.select(cx, sel);
      } else if (r.data.status === "failed") {
        this.job = null;
        this.say(sentence(r.data.error ?? r.data.message) || "The import stopped.", "fail");
      }
      cx.notify();
    });
  }

  // ---- selection and dragging ------------------------------------------------------------------------------------

  flat() {
    const out = [];
    const walk = (node) => {
      for (const f of node.folders ?? []) if (this.open.has(f.path)) walk(f);
      for (const d of node.datasets ?? []) out.push(d.name);
    };
    if (this.tree) walk(this.tree);
    return out;
  }

  press(e, cx, name) {
    const m = e.modifiers ?? {};
    if (m.platform) {
      if (this.picked.has(name)) this.picked.delete(name); else this.picked.add(name);
      this.anchor = name;
    } else if (m.shift && this.anchor) {
      const order = this.flat();
      const [a, b] = [order.indexOf(this.anchor), order.indexOf(name)].sort((x, y) => x - y);
      if (a >= 0 && b >= 0) for (const n of order.slice(a, b + 1)) this.picked.add(n);
    } else {
      if (!this.picked.has(name)) { this.picked.clear(); this.picked.add(name); }
      this.anchor = name;
      this.host.select(cx, name);
    }
    this.drag = { names: [...this.picked] };
    cx.notify();
  }

  release(cx, folder) {
    const d = this.drag;
    this.drag = null;
    if (!d || folder === null || folder === undefined) { cx.notify(); return; }
    const moving = d.names.filter((n) => (n.includes("/") ? n.split("/").slice(0, -1).join("/") : "") !== folder);
    if (moving.length) this.move(cx, moving, folder);
    cx.notify();
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  icon(name, size, color) {
    // Folders and chevrons ship with Data and signals; the rest are the shared set.
    if (["folder", "folder-open", "chevron-right", "chevron-down", "table"].includes(name)) return this.host.icon(name, size, color);
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  render(c, w) {
    const t = this.tree;
    return v_flex().id("library").w(w).h_full().flex_none().border_r(1).border_color(c.rule)
      // A press on a dataset ends here when it isn't released on a folder.
      .on_mouse_up("left", (_e, cx) => { if (this.drag) { this.drag = null; cx.notify(); } })
      .child(this.toolbar(c))
      .when(!!this.job, (e) => e.child(this.progress(c)))
      .when(!!this.summary, (e) => e.child(this.summaryPanel(c)))
      .when(!!this.note, (e) => e.child(h_flex().flex_none().px(10).py(6).gap(8).items_start().text_size(12)
        .text_color(this.note.tone === "fail" ? c.fail : this.note.tone === "good" ? c.good : c.grey900)
        .child(div().flex_1().min_w(0).child(this.note.text))
        .child(div().id("lib-note-x").cursor_pointer().on_click((_e, cx) => { this.note = null; cx.notify(); }).child(this.icon("x", 11)))))
      .child(div().id("lib-tree").flex_1().min_h(0).overflow_y_scrollbar()
        .on_drop((e, cx) => this.importPaths(cx, e.paths ?? [], this.cur))
        // Files from Finder over the tree: it lights up, so it's clear a drop lands in the current folder.
        .drag_over((st) => st.bg(c.blueLight))
        .when(t === null && !this.error, (e) => e.child(div().p(12).text_size(12).text_color(c.grey500).child("")))
        .when(!!this.host.listError, (e) => e.child(div().p(10).child(this.host.troubleView(c, "datasets-list", this.host.listR, this.host.listError, (cx) => this.refresh(cx)))))
        .when(!!this.error && !this.host.listError, (e) => e.child(div().p(10).child(this.host.troubleView(c, "folders", this.error, "Couldn't load your folders.", (cx) => this.load(cx)))))
        .when(!!t, (e) => e.child(this.folderRow(c, { name: "All datasets", path: "", folders: t.folders, datasets: t.datasets }, 0, true))
          .children(this.rows(c, t, 1))));
  }

  toolbar(c) {
    const crumbs = this.cur ? this.cur.split("/") : [];
    const node = this.tree ? find(this.tree, this.cur) : null;
    const empty = node && !(node.folders ?? []).length && !(node.datasets ?? []).length;
    const field = (id, st, go, label) => h_flex().gap(6).items_center()
      .child(div().id(id).flex_1().min_w(0).h(26).px(8).flex().items_center().border_1().border_color(c.grey300)
        .child(Input.new(st).w_full().text_size(12).text_color(c.navy)))
      .child(Button(c, `${id}-go`, label, (_e, cx) => go(cx), { kind: "primary", small: true }));
    return v_flex().flex_none().gap(8).p(10).border_b(1).border_color(c.rule)
      .child(h_flex().flex_wrap().gap(4).items_center().text_size(12)
        .child(div().id("crumb-root").cursor_pointer().text_color(crumbs.length ? c.blue : c.navy).font_medium()
          .on_click((_e, cx) => { this.cur = ""; cx.notify(); }).child("All datasets"))
        .children(crumbs.flatMap((p, i) => [
          this.icon("chevron-right", 11, c.grey500),
          div().id(`crumb-${i}`).cursor_pointer().text_color(i === crumbs.length - 1 ? c.navy : c.blue).font_medium()
            .on_click((_e, cx) => { this.cur = crumbs.slice(0, i + 1).join("/"); cx.notify(); }).child(p),
        ])))
      .when((this.host.classes ?? []).length > 0, (e) => e.child(h_flex().flex_wrap().gap(4)
        .child(this.host.chip(c, "All", null, !this.host.cls, (_e, cx) => { this.host.cls = null; cx.notify(); }))
        .children(this.host.classes.map((k) => this.host.chip(c, k.name, k.color, this.host.cls === k.name, (_e, cx) => { this.host.cls = this.host.cls === k.name ? null : k.name; cx.notify(); }, k.datasets)))))
      .child(h_flex().flex_wrap().gap(6)
        .child(Button(c, "lib-import", "Import files", (_e, cx) => { this.mode = this.mode === "import" ? null : "import"; cx.notify(); }, { kind: "primary", small: true }))
        .child(Button(c, "lib-new", "New folder", (_e, cx) => { this.mode = this.mode === "new" ? null : "new"; cx.notify(); }, { small: true }))
        .when(!!this.cur, (e) => e.child(Button(c, "lib-rename", "Rename", (_e, cx) => { this.renameTo.set_value(base(this.cur)); this.mode = this.mode === "rename" ? null : "rename"; cx.notify(); }, { small: true })))
        .when(!!this.cur && empty, (e) => e.child(Button(c, "lib-remove", "Remove folder", (_e, cx) => this.removeFolder(cx), { kind: "quiet", small: true })))
        .when(this.picked.size > 0, (e) => e.child(Button(c, "lib-move", `Move ${fmtInt(this.picked.size)} to`, (_e, cx) => { this.mode = this.mode === "move" ? null : "move"; cx.notify(); }, { small: true }))))
      .when(this.mode === "new", (e) => e.child(field("lib-new-name", this.newFolder, (cx) => this.makeFolder(cx), "Make")))
      .when(this.mode === "rename", (e) => e.child(field("lib-rename-name", this.renameTo, (cx) => this.renameFolder(cx), "Rename")))
      .when(this.mode === "import", (e) => e.child(v_flex().gap(6)
        .child(field("lib-import-path", this.importPath, (cx) => this.importPaths(cx, [expand(this.importPath.value())], this.cur), "Import"))
        .child(h_flex().gap(8).items_center()
          .child(Button(c, "lib-pick", "Choose a folder", (_e, cx) => this.chooseFolder(cx), { small: true }))
          .child(div().text_size(11).text_color(c.grey500).child(`Into ${this.cur || "the top level"}. CSV, TSV, Parquet, JSON or JSON lines; a folder keeps its structure. Or drag files here.`)))))
      .when(this.mode === "move" && this.picked.size > 0, (e) => e.child(h_flex().flex_wrap().gap(4)
        .children(["", ...(this.tree ? allFolders(this.tree) : [])].map((p, i) => div().id(`move-to-${i}`).h(22).px(6).flex().items_center().cursor_pointer()
          .border_1().border_color(c.grey300).text_size(11.5).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue))
          .on_click((_e, cx) => this.move(cx, [...this.picked], p)).child(p || "Top level")))));
  }

  rows(c, node, depth) {
    const out = [];
    for (const f of node.folders ?? []) {
      out.push(this.folderRow(c, f, depth, false));
      if (this.open.has(f.path)) out.push(...this.rows(c, f, depth + 1));
    }
    const cls = this.host.cls;
    const shown = (node.datasets ?? []).filter((d) => !cls || (d.classes ?? []).includes(cls));
    // Top-level datasets sit under their own heading, so they aren't lost below the folders.
    if (depth === 1 && shown.length && (node.folders ?? []).length) out.push(h_flex().id("lib-loose").h(26).flex_none().pl(8 + depth * 14 + 17).pr(8).items_center().text_size(11).text_color(c.grey500)
      .child(div().flex_1().child("Not in a folder")).child(div().child(fmtInt(shown.length))));
    for (const d of shown) out.push(this.datasetRow(c, d, depth));
    return out;
  }

  folderRow(c, f, depth, root) {
    const on = this.cur === f.path;
    const open = root || this.open.has(f.path);
    const n = datasetsIn(f).length;
    return h_flex().id(`folder-${f.path || "root"}`).h(30).flex_none().pl(8 + depth * 14).pr(8).gap(6).items_center().cursor_pointer()
      .bg(on ? c.surface2 : c.surface).hover((s) => s.bg(this.drag ? c.blueLight : c.surface2))
      .on_click((_e, cx) => { this.cur = f.path; if (!root) { if (this.open.has(f.path)) this.open.delete(f.path); else this.open.add(f.path); } cx.notify(); })
      .on_mouse_up("left", (_e, cx) => { if (this.drag) { this.release(cx, f.path); cx.stop_propagation(); } })
      .on_drop((e, cx) => { this.importPaths(cx, e.paths ?? [], f.path); cx.stop_propagation(); })
      .drag_over((st) => st.bg(c.blueLight).text_color(c.blue))
      .when(!root, (e) => e.child(this.icon(open ? "chevron-down" : "chevron-right", 11, c.grey500)))
      .child(this.icon(open && !root ? "folder-open" : "folder", 14, on ? c.blue : c.grey900))
      .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_medium().text_color(on ? c.blue : c.navy).child(f.name))
      .child(div().flex_none().text_size(11).text_color(c.grey500).child(n ? fmtInt(n) : ""));
  }

  datasetRow(c, d, depth) {
    const on = this.host.sel === d.name;
    const picked = this.picked.has(d.name);
    return h_flex().id(`ds-${d.name}`).h(30).flex_none().pl(8 + depth * 14 + 17).pr(8).gap(6).items_center().cursor_pointer()
      .bg(picked ? c.blueLight : c.surface).border_l(2).border_color(on ? c.blue : "#00000000")
      .hover((s) => (picked ? s : s.bg(c.surface2)))
      .tooltip(`${d.name}${d.description ? `\n${d.description}` : ""}`)
      .on_mouse_down("left", (e, cx) => this.press(e, cx, d.name))
      .child(this.host.icon("table", 13, on ? c.blue : c.grey500))
      .child(div().flex_1().min_w(48).truncate().text_size(12.5).font_family(MONO).text_color(on ? c.blue : c.navy).child(d.leaf ?? d.name))
      .when(d.latest != null, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10.5).font_family(MONO).text_color(c.grey900).child(`v${d.latest}`)))
      .child(div().flex_none().h(18).px(5).flex().items_center().text_size(10.5).text_color(c.grey900).border_1().border_color(c.rule).child(d.kind ?? "data"))
      .child(div().flex_none().w(46).text_right().text_size(11).font_family(MONO).text_color(c.grey500).child(d.rows ? fmtInt(d.rows) : ""));
  }

  progress(c) {
    const j = this.job;
    const pct = Math.round((j.progress ?? 0) * 100);
    return v_flex().flex_none().gap(6).px(10).py(8).border_b(1).border_color(c.rule)
      .child(h_flex().gap(8).text_size(12).child(div().flex_1().min_w(0).truncate().child(`Importing ${j.label} into ${j.folder || "the top level"}`))
        .child(div().text_color(c.grey500).font_family(MONO).child(`${pct}%`)))
      .child(div().h(4).w_full().bg(c.surface2).child(div().h_full().w(`${Math.max(2, pct)}%`).bg(c.blue)))
      .when(!!j.message && j.message !== "done", (e) => e.child(div().text_size(11).text_color(c.grey500).truncate().child(j.message)));
  }

  summaryPanel(c) {
    const s = this.summary;
    const added = s.imported ?? [], updated = s.updated ?? [], same = s.unchanged ?? [], skipped = s.skipped ?? [];
    const head = [added.length && `Added ${added.length}`, updated.length && `updated ${updated.length} (new version)`, same.length && `${same.length} unchanged`, skipped.length && `${skipped.length} skipped`]
      .filter(Boolean).join(", ") || "Nothing to import";
    const item = (x, i, kind) => h_flex().id(`imp-${kind}-${i}`).gap(6).items_center().text_size(11.5)
      .child(this.icon(kind === "skipped" ? "circle-x" : kind === "same" ? "check" : "circle-check", 11, kind === "skipped" ? c.warn : c.good))
      .child(div().min_w(0).truncate().font_family(MONO).text_color(c.navy).child(refName(x.ref) || base(x.path ?? x.file ?? "")))
      .when(!!refVersion(x.ref), (e) => e.child(div().flex_none().px(4).border_1().border_color(kind === "updated" ? c.blue : c.grey300).text_size(10.5).font_family(MONO)
        .text_color(kind === "updated" ? c.blue : c.grey900).child(`v${refVersion(x.ref)}`)))
      .when(kind !== "skipped" && x.rows != null, (e) => e.child(div().flex_none().text_color(c.grey500).child(`${x.kind ?? ""}, ${plural(x.rows, "row")}`)))
      .when(kind === "skipped", (e) => e.child(div().flex_1().min_w(0).truncate().text_color(c.grey900).child(sentence(x.reason))).tooltip(`${x.path}: ${x.reason}`));
    return v_flex().id("lib-summary").flex_none().gap(4).px(10).py(8).border_b(1).border_color(c.rule).bg(c.surface2)
      .child(h_flex().gap(8).items_center().text_size(12.5).font_medium()
        .child(div().flex_1().child(`${head}.`))
        .child(div().id("lib-summary-x").cursor_pointer().on_click((_e, cx) => { this.summary = null; cx.notify(); }).child(this.icon("x", 11))))
      .children(added.slice(0, 8).map((x, i) => item(x, i, "added")))
      .children(updated.slice(0, 8).map((x, i) => item(x, i, "updated")))
      .children(skipped.slice(0, 8).map((x, i) => item(x, i, "skipped")))
      .when(added.length + updated.length + skipped.length > 24, (e) => e.child(div().text_size(11).text_color(c.grey500).child("and more")));
  }

  /** The whole Datasets tab when the workspace has no datasets yet. */
  empty(c) {
    return v_flex().id("lib-empty").flex_1().p(24).items_center().justify_center()
      .on_drop((e, cx) => this.importPaths(cx, e.paths ?? [], ""))
      .child(v_flex().id("lib-drop-box").w_full().max_w(560).py(48).px(24).gap(12).items_center().border_2().border_dashed().border_color(c.grey300)
        .drag_over((st) => st.border_color(c.blue).bg(c.blueLight))
        .child(this.icon("folder-open", 30, c.grey500))
        .child(div().text_size(16).font_semibold().text_center().child("Drag your catalogs, queries, judgments and logs here"))
        .child(div().max_w(440).text_center().text_size(12.5).line_height("19px").text_color(c.grey900)
          .child("Drop files or whole folders: CSV, TSV, Parquet, JSON or JSON lines. Folders keep their structure, and dropping a file again makes a new version."))
        .child(h_flex().gap(8)
          .child(Button(c, "empty-pick", "Choose a folder", (_e, cx) => this.chooseFolder(cx), { kind: "primary" }))
          .child(Button(c, "empty-import", "Type a path", (_e, cx) => { this.mode = this.mode === "import" ? null : "import"; cx.notify(); })))
        .when(this.mode === "import", (e) => e.child(h_flex().w(420).gap(6).items_center()
          .child(div().id("empty-path").flex_1().min_w(0).h(28).px(8).flex().items_center().border_1().border_color(c.grey300)
            .child(Input.new(this.importPath).w_full().text_size(12).text_color(c.navy)))
          .child(Button(c, "empty-path-go", "Import", (_e, cx) => this.importPaths(cx, [expand(this.importPath.value())], ""), { kind: "primary", small: true }))))
        .child(div().id("empty-demo").cursor_pointer().text_size(12).text_color(c.grey500).hover((s) => s.text_color(c.blue))
          .on_click(() => openForm("demo")).child("or try the demo store")))
      .when(!!this.job, (e) => e.child(div().w_full().max_w(560).child(this.progress(c))))
      .when(!!this.summary, (e) => e.child(div().w_full().max_w(560).child(this.summaryPanel(c))))
      .when(!!this.note, (e) => e.child(div().pt(8).text_size(12).text_color(this.note.tone === "fail" ? c.fail : c.grey900).child(this.note.text)));
  }
}
