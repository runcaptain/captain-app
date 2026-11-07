// CAPTAIN: Data and signals, Models tab: the model registries this workspace reads (GET /api/registries), attaching
// one (an address like s3://bucket/prefix and a cloud login, POST /api/registries as a job; the same address again
// syncs it), detaching one (DELETE /api/registries?uri=), and every model across all of them grouped by architecture,
// each with what it was built from (GET /api/models). Registries are read, never written.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { LineageGraph } from "./kit/lineage.js";
import { shared } from "herdr";
import { withMlflow } from "./kit/mlflow.js";

const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(s)) return "";
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
const TONE = { shipped: "good", live: "good", candidate: "blue", experiment: "neutral", failed: "bad", retired: "neutral" };

export class Models {
  constructor(host) {
    this.host = host;
    this.regs = null;
    this.models = [];
    this.profiles = [];
    this.error = null;
    this.attaching = false;
    this.profile = null;
    this.job = null;
    this.note = null;
    this.confirm = null; // uri awaiting a second click to detach
    this.focus = null; // model id whose parents are highlighted
    this.uri = InputState.new({ placeholder: "s3://bucket/models" });
    this.label = InputState.new({ placeholder: "Name it (optional)" });
    this.uri.on("submit", (_e, cx) => this.attach(cx));
    // Whose models: All, a project, or a brand, site or engine (the Explorer's pick by default).
    this.owners = [{ kind: null, id: null, name: "All" }];
    this.owner = null;
    this.projectGraph = new LineageGraph({ id: "project-lineage", onFocus: () => {} });
    this.projectLineage = null;
  }

  /** The models route for the chosen owner. The filter here decides, so the request isn't also scoped by the header. */
  modelsPath() {
    const o = this.owner;
    if (!o?.kind) return "/api/models";
    return o.kind === "project" ? `/api/models?project=${encodeURIComponent(o.id)}` : `/api/models?scope=${encodeURIComponent(`${o.kind}:${o.id}`)}`;
  }

  load(cx) {
    cx.spawn(async (cx) => {
      if (this.owners.length === 1) {
        const ex = await api("/api/explorer", { scoped: false });
        const walk = (nodes) => { for (const n of nodes ?? []) {
          if (["scope", "engine", "project"].includes(n.kind) && n.id) this.owners.push({ kind: n.kind, id: n.id, name: n.name ?? n.label });
          walk(n.children);
        } };
        if (ex.ok) walk(ex.data?.tree);
        const pick = shared("captain.scope");
        this.owner = this.owners.find((o) => o.kind === pick?.kind && o.id === pick?.id) ?? this.owners[0];
      }
      const [r, m, p] = await Promise.all([api("/api/registries"), api(this.modelsPath(), { scoped: false }), api("/api/cloud/profiles")]);
      if (r.ok) { this.regs = Array.isArray(r.data) ? r.data : []; this.error = null; } else if (!r.starting) this.error = r;
      this.models = m.ok && Array.isArray(m.data) ? m.data : [];
      this.profiles = p.ok && Array.isArray(p.data) ? p.data : [];
      // A project: its whole model lineage (models, parents, the pipelines serving them, the backtests comparing them).
      this.projectLineage = null;
      if (this.owner?.kind === "project") {
        const l = await api(`/api/projects/${encodeURIComponent(this.owner.id)}/lineage`, { scoped: false });
        if (l.ok) {
          const split = (label) => { const mm = /^(.*)@v?(\d+)$/.exec(String(label ?? "")); return mm ? { label: mm[1], version: mm[2] } : { label }; };
          this.projectLineage = { nodes: (l.data.nodes ?? []).map((n) => ({ ...n, ...split(n.label ?? n.id) })), edges: l.data.edges ?? [] };
          this.projectLineage = await withMlflow(this.projectLineage); // MLflow upstream of its pipelines
          this.projectGraph.set(this.projectLineage, null);
        }
      }
      cx.notify();
    });
  }

  run(cx, body, label) {
    this.note = null;
    cx.spawn(async (cx) => {
      const r = await api("/api/registries", { body, timeout: 30 });
      if (!r.ok) { this.note = { text: sentence(r.data?.detail ?? r.error) || "Couldn't start.", tone: "fail" }; cx.notify(); return; }
      this.job = { id: r.data.job, label, progress: 0 };
      cx.notify();
    });
    cx.notify();
  }

  attach(cx) {
    const uri = this.uri.value().trim();
    if (!/^s3:\/\/[^/]/.test(uri)) { this.note = { text: "Type the registry's address, like s3://bucket/models.", tone: "fail" }; cx.notify(); return; }
    this.run(cx, { uri, profile: this.profile ?? undefined, label: this.label.value().trim() || undefined }, `Reading ${uri}`);
  }

  sync(cx, reg) {
    this.run(cx, { uri: reg.uri, profile: reg.profile ?? undefined, label: reg.label ?? undefined }, `Syncing ${reg.label ?? reg.uri}`);
  }

  detach(cx, reg) {
    if (this.confirm !== reg.uri) { this.confirm = reg.uri; cx.notify(); return; }
    this.confirm = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/registries?uri=${encodeURIComponent(reg.uri)}`, { method: "DELETE" });
      this.note = r.ok ? { text: `Detached ${reg.label ?? reg.uri}. Its models stay readable in past snapshots; the registry itself wasn't touched.`, tone: "good" }
        : { text: sentence(r.data?.detail ?? r.error) || "Couldn't detach.", tone: "fail" };
      this.load(cx);
    });
  }

  tick(cx) {
    const j = this.job;
    if (!j || j.polling) return;
    j.polling = true;
    cx.spawn(async (cx) => {
      const r = await api(`/api/jobs/${encodeURIComponent(j.id)}`);
      j.polling = false;
      if (!r.ok) return;
      j.progress = r.data.progress ?? j.progress;
      if (r.data.status === "done") {
        this.job = null;
        this.attaching = false;
        this.uri.set_value("");
        this.label.set_value("");
        this.note = { text: `${j.label.replace(/^Reading /, "Attached ").replace(/^Syncing /, "Synced ")}.`, tone: "good" };
        this.load(cx);
      } else if (r.data.status === "failed") {
        this.job = null;
        this.note = { text: sentence(r.data.error ?? r.data.message) || "That didn't work.", tone: "fail" };
      }
      cx.notify();
    });
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  icon(name, size, color) {
    if (["lineage", "database", "refresh", "x", "chevron-right"].includes(name)) return this.host.icon(name, size, color);
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  chip(c, text, tone) {
    const col = tone === "good" ? c.good : tone === "bad" ? c.fail : tone === "blue" ? c.blue : c.grey900;
    return div().flex_none().h(18).px(5).flex().items_center().border_1().border_color(col).text_size(10.5).text_color(col).child(text);
  }

  render(c, w) {
    const regs = this.regs ?? [];
    return v_flex().id("models").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(14).gap(14)
      .child(h_flex().gap(10).items_center()
        .child(div().text_size(14).font_semibold().child("Model registries"))
        .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(c.grey900).child("Where your trained models live. Captain reads them for lineage and never writes to them."))
        .child(Button(c, "reg-attach", "Attach a registry", (_e, cx) => { this.attaching = !this.attaching; cx.notify(); }, { kind: "primary", small: true })))
      .when(this.attaching, (e) => e.child(this.form(c)))
      .when(!!this.job, (e) => e.child(v_flex().gap(6).p(10).border_1().border_color(c.rule)
        .child(h_flex().text_size(12).child(div().flex_1().child(this.job.label)).child(div().font_family(MONO).text_color(c.grey500).child(`${Math.round((this.job.progress ?? 0) * 100)}%`)))
        .child(div().h(4).w_full().bg(c.surface2).child(div().h_full().w(`${Math.max(2, Math.round((this.job.progress ?? 0) * 100))}%`).bg(c.blue)))))
      .when(!!this.note, (e) => e.child(h_flex().gap(8).items_start().text_size(12).text_color(this.note.tone === "fail" ? c.fail : this.note.tone === "good" ? c.good : c.grey900)
        .child(div().flex_1().child(this.note.text)).child(div().id("reg-note-x").cursor_pointer().on_click((_e, cx) => { this.note = null; cx.notify(); }).child(this.icon("x", 11)))))
      .when(!!this.error, (e) => e.child(this.host.troubleView(c, "registries", this.error, "Couldn't load the registries.", (cx) => this.load(cx))))
      .when(this.regs !== null && !regs.length && !this.attaching, (e) => e.child(v_flex().gap(6).p(14).border_1().border_dashed().border_color(c.grey300)
        .child(div().text_size(13).font_medium().child("No registry attached"))
        .child(div().text_size(12).line_height("18px").text_color(c.grey900)
          .child("Attach the S3 folder your training writes models to, laid out as architecture / brand / version, each with a MANIFEST.yaml. Captain shows every model, what it was built from, and where it's deployed."))))
      .children(regs.map((r, i) => h_flex().id(`reg-${i}`).gap(10).items_center().p(10).border_1().border_color(c.rule)
        .child(this.icon("database", 16, c.navy))
        .child(v_flex().flex_1().min_w(0)
          .child(h_flex().gap(8).items_center().child(div().text_size(13).font_medium().truncate().child(r.label ?? r.uri)).when(!!r.profile, (e) => e.child(this.chip(c, r.profile))))
          .child(div().text_size(11.5).font_family(MONO).text_color(c.grey500).truncate().child(r.uri))
          .child(div().text_size(11.5).text_color(c.grey900).child(`${r.models == null ? "Not read yet" : `${r.models} model${r.models === 1 ? "" : "s"}`}, synced ${ago(r.synced_at)}`)))
        .child(Button(c, `reg-sync-${i}`, "Sync", (_e, cx) => this.sync(cx, r), { small: true, disabled: !!this.job }))
        .child(Button(c, `reg-detach-${i}`, this.confirm === r.uri ? "Detach? Click again" : "Detach", (_e, cx) => this.detach(cx, r), { kind: "quiet", small: true }))))
      .when(this.owners.length > 1, (e) => e.child(h_flex().id("model-owners").gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
        .child(div().flex_none().text_color(c.grey500).child("Whose"))
        .children(this.owners.map((o, i) => div().id(`owner-${i}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
          .border_1().border_color(this.owner === o ? c.blue : c.grey300).bg(this.owner === o ? c.blueLight : c.surface).text_color(this.owner === o ? c.blue : c.grey900)
          .on_click((_e, cx) => { this.owner = o; this.load(cx); cx.notify(); }).child(o.name)))))
      .when(!!this.projectLineage?.nodes?.length, (e) => e.child(v_flex().gap(6)
        .child(div().text_size(14).font_semibold().child(`${this.owner.name}: models, what they were built from, and where they run`))
        .child(this.projectGraph.render(c))))
      .when(this.owner?.kind && !this.models.length && this.regs !== null, (e) => e.child(div().text_size(12).text_color(c.grey500).child(`No models for ${this.owner.name} yet.`)))
      .when(this.models.length > 0, (e) => e.child(this.lineage(c)));
  }

  form(c) {
    const box = (id, st) => div().id(id).flex_1().min_w(0).h(28).px(8).flex().items_center().border_1().border_color(c.grey300).child(Input.new(st).w_full().text_size(12).text_color(c.navy));
    return v_flex().gap(8).p(12).border_1().border_color(c.blue).bg(c.surface2)
      .child(h_flex().gap(8).items_center().text_size(12).child(div().w(70).flex_none().text_color(c.grey900).child("Address")).child(box("reg-uri", this.uri)))
      .child(h_flex().gap(8).items_center().text_size(12).child(div().w(70).flex_none().text_color(c.grey900).child("Name")).child(box("reg-label", this.label)))
      .child(h_flex().gap(8).items_center().text_size(12)
        .child(div().w(70).flex_none().text_color(c.grey900).child("Login"))
        .child(h_flex().flex_1().flex_wrap().gap(4)
          .child(div().id("reg-prof-none").h(22).px(6).flex().items_center().cursor_pointer().border_1().border_color(this.profile ? c.grey300 : c.blue).text_color(this.profile ? c.grey900 : c.blue)
            .on_click((_e, cx) => { this.profile = null; cx.notify(); }).child("This computer's default"))
          .children(this.profiles.map((p) => div().id(`reg-prof-${p.name}`).h(22).px(6).flex().items_center().cursor_pointer()
            .border_1().border_color(this.profile === p.name ? c.blue : c.grey300).text_color(this.profile === p.name ? c.blue : c.grey900)
            .on_click((_e, cx) => { this.profile = p.name; cx.notify(); }).child(`${p.name}${p.provider ? ` (${p.provider})` : ""}`)))))
      .child(h_flex().gap(8)
        .child(Button(c, "reg-attach-go", "Attach and read", (_e, cx) => this.attach(cx), { kind: "primary", small: true, disabled: !!this.job }))
        .child(Button(c, "reg-attach-cancel", "Cancel", (_e, cx) => { this.attaching = false; cx.notify(); }, { kind: "quiet", small: true })));
  }

  /** Every model across the attached registries, by architecture, each with what it was built from. */
  lineage(c) {
    const byArch = new Map();
    for (const m of this.models) { if (!byArch.has(m.architecture)) byArch.set(m.architecture, []); byArch.get(m.architecture).push(m); }
    const order = [...byArch.keys()].sort((a, b) => (Number(/(\d+)/.exec(a)?.[1] ?? 99) - Number(/(\d+)/.exec(b)?.[1] ?? 99)) || a.localeCompare(b));
    const focus = this.focus ? this.models.find((m) => m.id === this.focus) : null;
    const related = new Set(focus ? [focus.id, ...(focus.parent_ids ?? focus.parents ?? []).map(String)] : []);
    return v_flex().gap(10)
      .child(h_flex().gap(8).items_center().child(this.icon("lineage", 15, c.navy)).child(div().text_size(14).font_semibold().child("Models and lineage"))
        .child(div().text_size(12).text_color(c.grey500).child(`${this.models.length} across ${(this.regs ?? []).length} registr${(this.regs ?? []).length === 1 ? "y" : "ies"}. Click a model to show what it was built from.`)))
      .children(order.map((a) => v_flex().gap(4)
        .child(div().text_size(12).font_medium().text_color(c.grey900).child(a))
        .children(byArch.get(a).sort((x, y) => `${x.brand}${x.version}`.localeCompare(`${y.brand}${y.version}`, undefined, { numeric: true })).map((m) => {
          const lit = related.has(m.id);
          const parents = (m.parents ?? []).length ? m.parents : m.parent_label ? [m.parent_label] : [];
          return h_flex().id(`model-${m.id}`).gap(8).items_center().px(8).py(5).cursor_pointer()
            .border_1().border_color(lit ? c.blue : c.rule).bg(lit ? c.blueLight : c.surface)
            .on_click((_e, cx) => { this.focus = this.focus === m.id ? null : m.id; cx.notify(); })
            .child(div().w(160).flex_none().truncate().text_size(12.5).font_family(MONO).child(`${m.brand} ${m.version}`))
            .child(this.chip(c, m.status_kind ?? m.status ?? "model", TONE[m.status_kind] ?? "neutral"))
            .when(!!m.repro_status && m.repro_status !== "not-tested", (e) => e.child(this.chip(c, m.repro_status, "good")))
            .when(parents.length > 0, (e) => e.child(h_flex().min_w(0).gap(4).items_center().text_size(11.5).text_color(c.grey900)
              .child(div().flex_none().child("built from")).child(div().min_w(0).truncate().font_family(MONO).child(parents.map(String).join(", ")))))
            .child(div().flex_1())
            .child(div().flex_none().max_w(160).truncate().text_size(11).text_color(c.grey500).child((this.regs ?? []).find((r) => r.uri === m.registry)?.label ?? ""));
        }))));
  }
}
