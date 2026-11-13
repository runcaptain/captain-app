// CAPTAIN: Data, bringing files in. Drop files or folders from Finder onto the list of tables (or onto the empty
// screen), or choose a folder: POST /api/datasets/import {paths, folder} runs as a job. Its progress shows, then
// what happened: added, updated (a new version), unchanged, and each skipped file with its reason.
// A file dropped on a dataset's page lands in that dataset's folder, so the same name there is a new version.
// The folder tree that used to sit beside the datasets is gone with the Datasets tab: a dataset's folder shows in
// the list's "Where it lives" column, its page moves it to another folder (About, "Where it lives"), and
// `cst data mkdir | move | rename-folder | rmdir` rename and remove folders.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { pickFolder, home } from "herdr";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { openForm } from "./kit/agent.js";
import { int } from "./kit/num.js";

const fmtInt = (n) => int(n ?? 0);
const plural = (n, w) => `${fmtInt(n)} ${w}${n === 1 ? "" : "s"}`;
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const base = (p) => String(p).split("/").filter(Boolean).pop() ?? p;
const refName = (ref) => String(ref ?? "").replace(/@v\d+$/, "");
const refVersion = (ref) => /@v(\d+)$/.exec(String(ref ?? ""))?.[1] ?? null;

export class Library {
  /** host: the Data view (select, load, classColor, icon). */
  constructor(host) {
    this.host = host;
    this.job = null; // {id, label, progress, status, message}
    this.summary = null; // the last import's result
    this.note = null; // {text, tone}
    this.mode = null; // "import": the path box on the empty screen
    this.importPath = InputState.new({ placeholder: "~/Downloads/catalogs" });
    this.importPath.on("submit", (_e, cx) => this.importPaths(cx, [expand(this.importPath.value())], ""));
  }

  /** Nothing to load: kept so the screen can ask every part to load the same way. */
  load(_cx) {}

  refresh(cx) {
    this.host.load(cx);
    this.host.catalog.load(cx);
  }

  say(text, tone = "muted") { this.note = { text, tone }; }

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
      this.tick(cx); // at once: the action just returned. The live channel says when the job moves on.
      cx.notify();
    });
    cx.notify();
  }

  chooseFolder(cx) {
    cx.spawn(async (cx) => {
      const p = await pickFolder({ prompt: "Import" });
      if (p) this.importPaths(cx, [p], "");
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

  icon(name, size, color) {
    if (["folder-open"].includes(name)) return this.host.icon(name, size, color);
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
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

  /** The whole Tables list when the workspace has no tables yet: one big drop zone. */
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
          .child(Button(c, "empty-import", "Type a path", (_e, cx) => { this.mode = this.mode === "import" ? null : "import"; cx.notify(); }))
          .child(Button(c, "empty-connect", "Connect a database", () => openForm("database"))))
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
