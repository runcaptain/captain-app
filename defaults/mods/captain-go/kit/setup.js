// captain-ui: start a workspace on your own storage, then bring in your work (Edgar, 2026-11-06: "I'd rather this
// storage also be connected as well during onboarding and then I just begin dragging all of my local work into the
// platform"). Three steps, hosted by the welcome tour (first run) and by captain-setup (New workspace):
//   1. Where it keeps its work: a folder on this Mac, or S3 / Google Cloud Storage / Azure with a cloud login.
//      POST /api/storage/check says in words whether Captain can read and write there, or that it already holds a
//      workspace (then it offers to open that one). POST /api/workspaces, then .../use, and a space bound to it.
//   2. Bring in your work: a drop zone for files and folders from Finder (POST /api/datasets/import, a job).
//   3. Attach your model registry, optional (POST /api/registries, a job).
// The host calls start(cx) from init (inputs can't be made in render), render(c, cx) to draw, and passes done().
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { pickFolder, openOverlay, run, spaces, shared, setShared } from "herdr";
import { api } from "./cst.js";
import { watchLive } from "./live.js";
import { Button, plainError } from "./ui.js";
import { icon } from "./icons.js";
import { MONO } from "./tokens.js";

const CLOUDS = [
  { id: "aws", label: "Amazon S3", scheme: "s3://" },
  { id: "gcp", label: "Google Cloud Storage", scheme: "gs://" },
  { id: "azure", label: "Azure Blob Storage", scheme: "az://" },
];

const sleep = (cx, ms) => new Promise((resolve) => cx.timer.after(ms, () => resolve()));
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Wait for a backend job: {ok, result} or {ok: false, error}. */
async function finished(cx, job, onProgress) {
  for (let i = 0; i < 1800; i++) {
    const r = await api(`/api/jobs/${encodeURIComponent(job)}`);
    if (r.ok) {
      const st = r.data?.status;
      if (st === "done") return { ok: true, result: r.data?.result ?? {} };
      if (st === "failed") return { ok: false, error: r.data?.message || r.data?.error || "It didn't finish." };
      onProgress?.(r.data?.progress ?? null, r.data?.message ?? null);
    }
    await sleep(cx, 700);
  }
  return { ok: false, error: "This took too long." };
}

export class StorageSetup {
  constructor({ first = false, done } = {}) {
    this.first = first;
    this.done = done ?? (() => {});
    this.step = "storage";
    this.where = "local";
    this.cloud = "aws";
    this.folder = null;
    this.profiles = null;
    this.profile = null;
    this.check = null;
    this.busy = null;
    this.error = null;
    this.created = null;
    this.imports = [];
    this.dropping = false;
    this.registry = null;
  }

  start(cx) {
    this.name = InputState.new({ placeholder: "e.g. my-search" });
    this.bucket = InputState.new({ placeholder: "e.g. acme-search" });
    this.prefix = InputState.new({ placeholder: "e.g. captain" });
    this.regUri = InputState.new({ placeholder: "e.g. s3://acme-models/registry" });
    for (const st of [this.name, this.bucket, this.prefix]) st.on("change", () => { this.check = null; this.error = null; });
    this.loadProfiles(cx);
    // A login connected meanwhile (Connect a cloud account opens over this) shows up without a reload.
    // Through the one live channel: read again the moment a login changes. No timer re-reads the logins.
    watchLive(cx, ["logins"], (cx) => { if (this.step === "storage" && this.where === "cloud") this.loadProfiles(cx); });
  }

  loadProfiles(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/cloud/profiles");
      if (!r.ok) return;
      this.profiles = Array.isArray(r.data) ? r.data : [];
      const mine = this.profiles.filter((p) => p.provider === this.cloud);
      if (!mine.some((p) => p.name === this.profile)) this.profile = mine[0]?.name ?? null;
      cx.notify();
    });
  }

  // ---- step 1: storage --------------------------------------------------------------------------------------

  uri() {
    if (this.where === "local") return this.folder;
    const bucket = this.bucket.value().trim().replace(/^[a-z]+:\/\//, "").replace(/\/+$/, "");
    if (!bucket) return null;
    const prefix = this.prefix.value().trim().replace(/^\/+|\/+$/g, "");
    return `${CLOUDS.find((x) => x.id === this.cloud).scheme}${bucket}${prefix ? `/${prefix}` : ""}`;
  }

  nameError() {
    const n = this.name.value().trim();
    if (!n) return "Give the workspace a name.";
    if (!/^[A-Za-z0-9][A-Za-z0-9-]*$/.test(n)) return "Letters, digits and dashes only.";
    return null;
  }

  runCheck(cx) {
    const uri = this.uri();
    if (!uri) { this.error = this.where === "local" ? "Choose a folder first." : "Type the bucket's name."; cx.notify(); return; }
    this.busy = "check"; this.error = null; this.check = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/storage/check", { body: { uri, profile: this.where === "cloud" ? this.profile : null } });
      this.busy = null;
      if (r.ok) this.check = { ...r.data, uri };
      else this.error = plainError(r.error, "Captain couldn't check that place.");
      cx.notify();
    });
  }

  create(cx, existing) {
    const name = existing ?? this.name.value().trim();
    const bad = existing ? null : this.nameError();
    if (bad) { this.error = bad; cx.notify(); return; }
    this.busy = "create"; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const body = { name, storage_uri: this.check.uri, ...(this.where === "cloud" && this.profile ? { profile: this.profile } : {}) };
      const made = await api("/api/workspaces", { body });
      if (!made.ok) { this.busy = null; this.error = made.status === 409 ? `There's already a workspace called ${name}.` : plainError(made.error, "Captain couldn't make the workspace."); cx.notify(); return; }
      const ws = made.data?.name ?? name;
      const used = await api(`/api/workspaces/${encodeURIComponent(ws)}/use`, { body: {} });
      this.busy = null;
      if (!used.ok) { this.error = plainError(used.error, "Captain couldn't switch to it."); cx.notify(); return; }
      this.bindSpace(ws);
      this.created = ws;
      this.step = "drop";
      cx.notify();
    });
  }

  /** A space for the new workspace, bound to it, so its screens and terminals use it. The old ones stay. */
  bindSpace(ws) {
    const before = new Set(spaces().map((s) => s.id));
    run("space.new", { name: ws });
    const made = spaces().find((s) => !before.has(s.id));
    if (!made) return;
    setShared("captain.spaces", { ...(shared("captain.spaces") ?? {}), [made.id]: { workspace: ws } });
    setShared("captain.workspace", ws);
  }

  // ---- step 2: drop ---------------------------------------------------------------------------------------

  importPaths(cx, paths) {
    if (!paths?.length) return;
    const entry = { paths, state: "running", progress: null, result: null, error: null };
    this.imports.unshift(entry);
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/datasets/import", { body: { paths, folder: "" } });
      if (!r.ok || !r.data?.job) { entry.state = "failed"; entry.error = plainError(r.error, "Captain couldn't start the import."); cx.notify(); return; }
      const out = await finished(cx, r.data.job, (p) => { entry.progress = p; cx.notify(); });
      entry.state = out.ok ? "done" : "failed";
      entry.result = out.result ?? null;
      entry.error = out.ok ? null : plainError(out.error, "The import didn't finish.");
      cx.notify();
    });
  }

  // ---- step 3: registry ----------------------------------------------------------------------------------

  attach(cx) {
    const uri = this.regUri.value().trim();
    if (!/^(s3|gs|az):\/\/\S+|^\//.test(uri)) { this.error = "Type the registry's address, like s3://bucket/models."; cx.notify(); return; }
    this.busy = "registry"; this.error = null; this.registry = null; cx.notify();
    cx.spawn(async (cx) => {
      const profile = uri.startsWith("s3://") ? (this.profiles ?? []).find((p) => p.provider === "aws")?.name : null;
      const r = await api("/api/registries", { body: { uri, ...(profile ? { profile } : {}) } });
      const out = r.ok && r.data?.job ? await finished(cx, r.data.job) : { ok: false, error: r.error || "Captain couldn't attach it." };
      this.busy = null;
      if (out.ok) this.registry = out.result ?? {};
      else this.error = plainError(out.error, "Captain couldn't attach it.");
      cx.notify();
    });
  }

  // ---- drawing --------------------------------------------------------------------------------------------

  label(c, text, optional) {
    return h_flex().gap(6).items_center()
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(text))
      .when(Boolean(optional), (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("optional")));
  }

  input(c, id, state) {
    return div().id(id).h(32).flex().items_center().px(10).bg(c.surface).border_1().border_color(c.grey300).cursor_text()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .child(state ? Input.new(state).w_full().text_size(13).text_color(c.navy) : div().h(14).w(120).bg(c.surface2));
  }

  choice(c, id, options, value, pick) {
    return h_flex().flex_wrap().gap(6).children(options.map((o) => {
      const on = o.id === value;
      return h_flex().id(`${id}-${o.id}`).h(30).px(10).gap(6).items_center().cursor_pointer().text_size(12.5)
        .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
        .hover((s) => (on ? s : s.border_color(c.blue)))
        .on_click((_e, cx) => { pick(o.id, cx); cx.notify(); })
        .when(Boolean(o.icon), (e) => e.child(svg(icon(o.icon)).size(14)))
        .child(o.label);
    }));
  }

  note(c, text, tone = "neutral") {
    const color = tone === "fail" ? c.fail : tone === "good" ? c.good : c.grey900;
    return div().text_size(12.5).line_height("19px").text_color(color).child(text);
  }

  stepper(c) {
    const steps = [["storage", "Storage"], ["drop", "Your work"], ["registry", "Model registry"]];
    const at = steps.findIndex(([id]) => id === this.step);
    return h_flex().gap(16).pb(16).children(steps.map(([id, name], i) => h_flex().gap(6).items_center()
      .child(div().size(18).flex().items_center().justify_center().text_size(11).font_semibold()
        .bg(i <= at ? c.solid : c.surface2).text_color(i <= at ? c.onBlue : c.grey500).child(String(i + 1)))
      .child(div().text_size(12.5).text_color(i === at ? c.navy : c.grey500).child(name))));
  }

  storageStep(c) {
    const mine = (this.profiles ?? []).filter((p) => p.provider === this.cloud);
    const ch = this.check;
    return v_flex().gap(14)
      .child(div().text_size(22).font_semibold().text_color(c.navy).child(this.first ? "Where should Captain keep your work?" : "A new workspace"))
      .child(this.note(c, "Keep your datasets, runs and decisions in a folder you own."))
      .child(v_flex().gap(6).child(this.label(c, "Name")).child(this.input(c, "setup-name", this.name)))
      .child(v_flex().gap(6).child(this.label(c, "Keep it in"))
        .child(this.choice(c, "setup-where", [{ id: "local", label: "A folder on this Mac", icon: "hard-drive" }, { id: "cloud", label: "Cloud storage", icon: "cloud" }],
          this.where, (v, cx) => { this.where = v; this.check = null; this.error = null; if (v === "cloud") this.loadProfiles(cx); })))
      .when(this.where === "local", (e) => e.child(h_flex().gap(10).items_center()
        .child(Button(c, "setup-pick", this.folder ? "Choose another folder" : "Choose a folder", (_e, cx) => {
          cx.spawn(async (cx) => {
            const f = await pickFolder({ prompt: "Keep this workspace here" });
            if (f) { this.folder = f; this.check = null; this.error = null; }
            cx.notify();
          });
        }))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_family(MONO).text_color(this.folder ? c.navy : c.grey500)
          .child(this.folder ? this.folder.replace(/^\/Users\/[^/]+/, "~") : "No folder chosen"))))
      .when(this.where === "cloud", (e) => e
        .child(this.choice(c, "setup-cloud", CLOUDS.map((x) => ({ id: x.id, label: x.label })), this.cloud,
          (v, cx) => { this.cloud = v; this.profile = null; this.check = null; this.loadProfiles(cx); }))
        .child(v_flex().gap(6).child(this.label(c, "Cloud login"))
          .child(this.profiles === null ? div().h(30).w(200).bg(c.surface2)
            : mine.length ? this.choice(c, "setup-profile", mine.map((p) => ({ id: p.name, label: p.name })), this.profile, (v) => { this.profile = v; this.check = null; })
            : h_flex().gap(10).items_center()
              .child(this.note(c, `No ${CLOUDS.find((x) => x.id === this.cloud).label.split(" ")[0]} login yet.`))
              .child(Button(c, "setup-connect", "Connect a cloud account", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "cloud" } }), { small: true }))))
        .child(h_flex().gap(10)
          .child(v_flex().flex_1().gap(6).child(this.label(c, "Bucket")).child(this.input(c, "setup-bucket", this.bucket)))
          .child(v_flex().flex_1().gap(6).child(this.label(c, "Folder in it", true)).child(this.input(c, "setup-prefix", this.prefix)))))
      .when(Boolean(ch), (e) => e.child(v_flex().gap(4).p(10).bg(ch.ok && !ch.existing_workspace ? c.blueLight : c.surface2)
        .when(Boolean(ch.error), (x) => x.child(this.note(c, plainError(ch.error, "Captain couldn't reach that place."), "fail")))
        .when(!ch.error, (x) => x.child(this.note(c, ch.can_write ? "Captain can read and write here." : ch.can_read ? "Captain can read here but not write." : "Captain can't read here.", ch.can_write ? "good" : "fail")))
        .when(Boolean(ch.existing_workspace), (x) => x.child(h_flex().gap(10).items_center()
          .child(div().flex_1().min_w(0).child(this.note(c, `This already holds the workspace ${ch.existing_workspace}.`)))
          .child(Button(c, "setup-open-existing", `Open ${ch.existing_workspace}`, (_e, cx) => this.create(cx, ch.existing_workspace), { kind: "primary", small: true }))))))
      .when(Boolean(this.error), (e) => e.child(this.note(c, this.error, "fail")))
      ;
  }

  dropStep(c) {
    const zone = v_flex().id("setup-drop").h(170).items_center().justify_center().gap(8)
      .border_1().border_color(c.grey300).bg(c.surface2)
      .drag_over((s) => s.border_color(c.blue).bg(c.blueLight))
      .on_drop((event, cx) => this.importPaths(cx, event.paths ?? []))
      .child(svg(icon("upload")).size(28).text_color(c.blue))
      .child(div().text_size(14).font_semibold().text_color(c.navy).child("Drag your catalogs, queries, judgments and logs here"))
      .child(div().px(24).text_size(12).text_color(c.grey500).child("Drop files or whole folders: CSV, TSV, Parquet, JSON or JSON lines. Folders keep their structure, and dropping a file again makes a new version."));
    return v_flex().gap(14)
      .child(div().text_size(18).font_semibold().text_color(c.navy).child("Bring in your work"))
      .child(this.note(c, `Drag your catalogs, queries, judgments and logs in from Finder. They land in ${this.created ?? "this workspace"}, and you can organize them into folders in Data and signals.`))
      .child(zone)
      .child(h_flex().gap(8).child(Button(c, "setup-pick-import", "Choose a folder to bring in", (_e, cx) => {
        cx.spawn(async (cx) => { const f = await pickFolder({ prompt: "Bring this folder into Captain" }); if (f) this.importPaths(cx, [f]); cx.notify(); });
      }, { small: true })))
      .children(this.imports.slice(0, 6).map((it, i) => this.importRow(c, it, i)));
  }

  importRow(c, it, i) {
    const what = it.paths.length === 1 ? it.paths[0].split("/").filter(Boolean).pop() : plural(it.paths.length, "item");
    const r = it.result ?? {};
    const counts = [r.imported && `${plural(r.imported, "dataset")} added`, r.updated && `${r.updated} updated`, r.unchanged && `${r.unchanged} unchanged`]
      .filter(Boolean).join(", ");
    const skipped = Array.isArray(r.skipped) ? r.skipped : [];
    return v_flex().id(`setup-import-${i}`).gap(3).py(8).border_b(1).border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(svg(icon(it.state === "done" ? "circle-check" : it.state === "failed" ? "circle-x" : "clock")).size(14)
          .text_color(it.state === "done" ? c.good : it.state === "failed" ? c.fail : c.grey500))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.navy).child(what))
        .child(div().text_size(12).text_color(c.grey500).child(it.state === "running"
          ? (it.progress != null ? `${Math.round(it.progress * 100)}%` : "Bringing it in...")
          : it.state === "done" ? (counts || "Nothing new") : "")))
      .when(Boolean(it.error), (e) => e.child(div().pl(22).child(this.note(c, it.error, "fail"))))
      .when(skipped.length > 0, (e) => e.child(div().pl(22).child(this.note(c,
        `Skipped ${plural(skipped.length, "file")}: ${skipped.slice(0, 3).map((s) => `${String(s.path).split("/").pop()} (${s.reason})`).join(", ")}${skipped.length > 3 ? ", ..." : ""}`))));
  }

  registryStep(c) {
    const reg = this.registry;
    return v_flex().gap(14)
      .child(div().text_size(18).font_semibold().text_color(c.navy).child("Attach your model registry"))
      .child(this.note(c, "Optional. Connect your trained models to see their versions and lineage."))
      .child(div().id("setup-registry-details").text_size(12).text_color(c.blue).cursor_pointer()
        .on_click((_e, cx) => { this.registryDetails = !this.registryDetails; cx.notify(); })
        .child(this.registryDetails ? "Hide folder layout" : "Show folder layout"))
      .when(Boolean(this.registryDetails), (e) => e.child(this.note(c, "Use <architecture>/<brand>/<version>/MANIFEST.yaml under your registry folder.")))
      .child(v_flex().gap(6).child(this.label(c, "Address")).child(this.input(c, "setup-registry", this.regUri)))
      .when(Boolean(reg), (e) => e.child(this.note(c, `Attached. ${reg.models != null ? `${plural(reg.models, "model")} found.` : ""}`.trim(), "good")))
      .when(Boolean(this.error), (e) => e.child(this.note(c, this.error, "fail")))
      .child(h_flex().child(Button(c, "setup-attach", this.busy === "registry" ? "Attaching..." : "Attach", (_e, cx) => this.attach(cx), { disabled: Boolean(this.busy) })));
  }

  /** The step's own body. The host draws the frame; footer() gives the buttons. */
  body(c) {
    return v_flex().when(!this.first || this.step !== "storage", (e) => e.child(this.stepper(c)))
      .child(this.step === "storage" ? this.storageStep(c) : this.step === "drop" ? this.dropStep(c) : this.registryStep(c));
  }

  /** Footer buttons for the step: Back/Next in the later steps. Step 1 advances by creating the workspace. */
  footer(c) {
    if (this.step === "storage") {
      const ch = this.check;
      if (ch?.existing_workspace) return h_flex();
      const ready = ch?.ok && ch.can_write;
      return Button(c, ready ? "setup-create" : "setup-check",
        this.busy === "check" ? "Checking..." : this.busy === "create" ? "Creating..." : ready ? "Create workspace" : "Check access",
        (_e, cx) => ready ? this.create(cx) : this.runCheck(cx),
        { kind: "primary", disabled: Boolean(this.busy) || !this.uri() });
    }
    const busy = this.imports.some((x) => x.state === "running") || this.busy === "registry";
    return h_flex().gap(8)
      .when(this.step === "registry", (e) => e.child(Button(c, "setup-back", "Back", (_e, cx) => { this.step = "drop"; this.error = null; cx.notify(); })))
      .child(this.step === "drop"
        ? Button(c, "setup-next", "Next", (_e, cx) => { this.step = "registry"; this.error = null; cx.notify(); }, { kind: "primary", disabled: busy })
        : Button(c, "setup-done", this.registry ? "Done" : "Skip and finish", () => this.done(this.created), { kind: "primary", disabled: busy }));
  }
}
