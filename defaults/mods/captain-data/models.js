// CAPTAIN: Data and signals, Models tab: the model registries this workspace reads (GET /api/registries), attaching
// one (an address like s3://bucket/prefix and a cloud login, POST /api/registries as a job; the same address again
// syncs it), detaching one (DELETE /api/registries?uri=), and every model across all of them grouped by architecture,
// each with what it was built from (GET /api/models). Registries are read, never written.
// Attaching starts from a list of the login's S3 buckets with the registries marked (GET /api/registries/discover),
// and a team without one can make one: through a CloudFormation stack in their account (POST /api/registries/create,
// a job), or as Terraform or a CloudFormation template saved to Downloads (GET /api/registries/template).
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { LineageGraph } from "./kit/lineage.js";
import { shared, writeTextFile, readTextFile, home } from "herdr";
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
    this.disc = null; // {loading} | {account, buckets, registries} | {error}: the login's buckets
    this.discFor = undefined;
    this.showAll = false;
    this.typing = false; // typing an address instead of picking a bucket
    this.making = false; // the Create a registry panel
    this.region = "us-west-1";
    this.find = InputState.new({ placeholder: "Find a bucket" });
    this.find.on("change", (_e, cx) => cx.notify());
    this.newName = InputState.new({ placeholder: "captain-models" });
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

  toggleAttach(cx) {
    this.attaching = !this.attaching;
    if (this.attaching) this.loadBuckets(cx);
    cx.notify();
  }

  /** The login's S3 buckets, registries first. Reads only; takes a few seconds on an account with many buckets. */
  loadBuckets(cx, force = false) {
    const key = this.profile ?? "";
    if (this.discFor === key && !force && this.disc && !this.disc.error) return;
    this.discFor = key;
    this.disc = { loading: true };
    cx.spawn(async (cx) => {
      const r = await api(`/api/registries/discover${this.profile ? `?profile=${encodeURIComponent(this.profile)}` : ""}`, { timeout: 120 });
      if (this.discFor !== key) return;
      if (r.ok) {
        this.disc = r.data;
        if (!this.newName.value().trim() && r.data.account) this.newName.set_value(`captain-models-${r.data.account}`);
        if (!r.data.registries) this.making = true; // nothing to pick: offer to make one
      } else {
        this.disc = { error: r.status === 404 && /not found/i.test(String(r.data?.detail ?? r.error)) ? "Listing buckets needs a newer Captain backend. Type the address instead."
          : sentence(r.data?.detail ?? r.error) || "Couldn't list this login's buckets." };
        this.typing = true;
      }
      cx.notify();
    });
  }

  pickLogin(cx, name) {
    this.profile = name;
    this.loadBuckets(cx);
    cx.notify();
  }

  attachBucket(cx, b) {
    this.run(cx, { uri: b.uri, profile: this.profile ?? undefined, label: b.bucket }, `Reading ${b.uri}`);
  }

  /** Make a registry bucket in AWS through a CloudFormation stack, then attach it (a job). */
  create(cx) {
    const bucket = this.newName.value().trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(bucket)) {
      this.note = { text: "A bucket name is 3 to 63 lowercase letters, digits and hyphens.", tone: "fail" };
      cx.notify();
      return;
    }
    this.note = null;
    cx.spawn(async (cx) => {
      const r = await api("/api/registries/create", { body: { bucket, region: this.region, profile: this.profile ?? undefined }, timeout: 30 });
      if (!r.ok) { this.note = { text: sentence(r.data?.detail ?? r.error) || "Couldn't start.", tone: "fail" }; cx.notify(); return; }
      this.job = { id: r.data.job, label: `Creating s3://${bucket} in ${this.region}`, done: `Made s3://${bucket} and attached it. It's empty until your training writes its first model.`, progress: 0 };
      cx.notify();
    });
  }

  /** The same registry as infrastructure code, saved to Downloads for the team's own pipeline to apply. */
  saveTemplate(cx, format) {
    const bucket = this.newName.value().trim().toLowerCase() || "captain-models";
    cx.spawn(async (cx) => {
      const r = await api(`/api/registries/template?bucket=${encodeURIComponent(bucket)}&region=${encodeURIComponent(this.region)}&format=${format}`);
      if (!r.ok) { this.note = { text: sentence(r.data?.detail ?? r.error) || "Couldn't make it.", tone: "fail" }; cx.notify(); return; }
      const path = `${home()}/Downloads/${r.data.filename}`;
      try {
        const old = await readTextFile(path);
        await writeTextFile(path, r.data.text, old);
        this.note = { text: `Saved ~/Downloads/${r.data.filename}. Apply it with: ${r.data.how}`, tone: "good" };
      } catch {
        this.note = { text: `Couldn't write ~/Downloads/${r.data.filename}.`, tone: "fail" };
      }
      cx.notify();
    });
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
        this.note = { text: j.done ?? `${j.label.replace(/^Reading /, "Attached ").replace(/^Syncing /, "Synced ")}.`, tone: "good" };
        this.disc = null; // the list marks what's attached: read it again next time
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
    return v_flex().id("models").flex_none().gap(14)
      .child(h_flex().gap(10).items_center()
        .child(div().text_size(14).font_semibold().child("Model registries"))
        .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(c.grey900).child("Where your trained models live. Captain reads them and never writes to them."))
        .child(Button(c, "reg-attach", "Attach a registry", (_e, cx) => this.toggleAttach(cx), { small: true })))
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
      .when(this.owners.length > 1, (e) => e.child(h_flex().id("model-owners").flex_none().h(26).gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
        .child(div().flex_none().text_color(c.grey500).child("Whose"))
        .children(this.owners.map((o, i) => div().id(`owner-${i}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer()
          .border_1().border_color(this.owner === o ? c.blue : c.grey300).bg(this.owner === o ? c.blueLight : c.surface).text_color(this.owner === o ? c.blue : c.grey900)
          .on_click((_e, cx) => { this.owner = o; this.load(cx); cx.notify(); }).child(o.name)))))
      .when(!!this.projectLineage?.nodes?.length, (e) => e.child(v_flex().flex_none().gap(6)
        .child(div().text_size(14).font_semibold().child(`${this.owner.name}: models, what they were built from, and where they run`))
        .child(this.projectGraph.render(c))))
      .when(this.owner?.kind && !this.models.length && this.regs !== null, (e) => e.child(div().text_size(12).text_color(c.grey500).child(`No models for ${this.owner.name} yet.`)))
      .when(this.models.length > 0, (e) => e.child(this.lineage(c)));
  }

  form(c) {
    const box = (id, st) => div().id(id).flex_1().min_w(0).h(28).px(8).flex().items_center().border_1().border_color(c.grey300).bg(c.surface).child(Input.new(st).w_full().text_size(12).text_color(c.navy));
    const opt = (id, on, label, click) => div().id(id).h(22).px(6).flex().flex_none().items_center().cursor_pointer().text_size(12)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900).on_click(click).child(label);
    const link = (id, label, click) => div().id(id).flex_none().cursor_pointer().text_size(12).text_color(c.blue).on_click(click).child(label);
    const label = (t) => div().w(70).flex_none().text_color(c.grey900).child(t);
    return v_flex().id("reg-form").gap(12).p(12).border_1().border_color(c.blue).bg(c.surface2)
      .child(h_flex().gap(8).items_center().text_size(12).child(label("Login"))
        .child(h_flex().flex_1().flex_wrap().gap(4)
          .child(opt("reg-prof-none", !this.profile, "This computer's default", (_e, cx) => this.pickLogin(cx, null)))
          .children(this.profiles.filter((p) => !p.provider || p.provider === "aws").map((p) =>
            opt(`reg-prof-${p.name}`, this.profile === p.name, p.name, (_e, cx) => this.pickLogin(cx, p.name))))))
      .child(this.bucketList(c, link))
      .child(v_flex().gap(8)
        .child(link("reg-type", this.typing ? "Hide the address field" : "Type an address instead", (_e, cx) => { this.typing = !this.typing; cx.notify(); }))
        .when(this.typing, (e) => e
          .child(h_flex().gap(8).items_center().text_size(12).child(label("Address")).child(box("reg-uri", this.uri)))
          .child(h_flex().gap(8).items_center().text_size(12).child(label("Name")).child(box("reg-label", this.label)))
          .child(h_flex().gap(8).child(Button(c, "reg-attach-go", "Attach and read", (_e, cx) => this.attach(cx), { kind: "primary", small: true, disabled: !!this.job })))))
      .child(div().h(1).bg(c.rule))
      .child(this.makePanel(c, box, opt, label))
      .child(h_flex().gap(8).child(Button(c, "reg-attach-cancel", "Close", (_e, cx) => { this.attaching = false; cx.notify(); }, { kind: "quiet", small: true })));
  }

  /** The login's buckets: registries first and ready to attach, the rest behind Show all. */
  bucketList(c, link) {
    const d = this.disc;
    const head = h_flex().gap(8).items_center()
      .child(div().text_size(13).font_medium().child("Your S3 buckets"))
      .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(c.grey500).child(
        !d || d.loading ? "" : d.error ? "" : `${d.registries} registr${d.registries === 1 ? "y" : "ies"} in ${d.buckets.length} bucket${d.buckets.length === 1 ? "" : "s"}${d.account ? ` in account ${d.account}` : ""}`))
      .child(div().id("reg-disc-refresh").flex_none().cursor_pointer().on_click((_e, cx) => this.loadBuckets(cx, true)).child(this.icon("refresh", 13, c.grey900)));
    if (!d || d.loading) return v_flex().gap(6).child(head).child(div().text_size(12).text_color(c.grey900).child("Looking through this login's buckets for model registries. This takes a few seconds."));
    if (d.error) return v_flex().gap(6).child(head).child(div().text_size(12).text_color(c.fail).child(d.error));
    const q = this.find.value().trim().toLowerCase();
    const shown = d.buckets.filter((b) => (this.showAll || b.kind === "registry") && (!q || b.uri.toLowerCase().includes(q)));
    const KIND = { registry: ["registry", "good"], empty: ["empty", "neutral"], other: ["not a registry", "neutral"], workspace: ["Captain workspace", "neutral"], "no-access": ["can't read", "bad"] };
    return v_flex().gap(6).child(head)
      .when(!d.registries && !this.showAll, (e) => e.child(div().text_size(12).text_color(c.grey900).child("None of these buckets holds a model registry yet. Make one below, or show every bucket.")))
      .when(this.showAll, (e) => e.child(div().id("reg-find").h(28).px(8).flex().items_center().border_1().border_color(c.grey300).bg(c.surface).child(Input.new(this.find).w_full().text_size(12))))
      .child(v_flex().id("reg-buckets").max_h(320).overflow_y_scrollbar().gap(4).children(shown.slice(0, 200).map((b, i) => {
        const [k, tone] = KIND[b.kind] ?? [b.kind, "neutral"];
        const can = b.kind === "registry" && !b.attached;
        return h_flex().id(`reg-b-${i}`).gap(8).items_center().px(8).py(6).border_1().border_color(c.rule).bg(c.surface)
          .when(can, (e) => e.cursor_pointer().on_click((_e, cx) => this.attachBucket(cx, b)))
          .child(this.icon("database", 14, b.kind === "registry" ? c.navy : c.grey500))
          .child(v_flex().flex_1().min_w(0)
            .child(div().truncate().text_size(12.5).font_family(MONO).text_color(b.kind === "registry" ? c.navy : c.grey900).child(b.uri.replace(/^s3:\/\//, "")))
            .child(div().truncate().text_size(11.5).text_color(c.grey500).child([b.region, b.detail].filter(Boolean).join(", "))))
          .child(this.chip(c, k, tone))
          .when(b.attached, (e) => e.child(this.chip(c, "attached", "blue")))
          .when(can, (e) => e.child(Button(c, `reg-b-go-${i}`, "Attach", (_e, cx) => this.attachBucket(cx, b), { kind: "primary", small: true, disabled: !!this.job })));
      })))
      .child(link("reg-showall", this.showAll ? "Show registries only" : `Show all ${d.buckets.length} buckets`, (_e, cx) => { this.showAll = !this.showAll; cx.notify(); }));
  }

  /** Make a registry: in AWS now through CloudFormation, or as Terraform or a template for the team to apply. */
  makePanel(c, box, opt, label) {
    const head = h_flex().id("reg-make-head").gap(8).items_center().cursor_pointer().on_click((_e, cx) => { this.making = !this.making; cx.notify(); })
      .child(this.icon(this.making ? "chevron-down" : "chevron-right", 12, c.grey900))
      .child(div().text_size(13).font_medium().child("No registry yet? Create one"));
    if (!this.making) return head;
    const REGIONS = ["us-west-1", "us-west-2", "us-east-1", "us-east-2", "eu-west-1", "eu-central-1", "ap-southeast-1"];
    return v_flex().gap(8).child(head)
      .child(div().text_size(12).line_height("18px").text_color(c.grey900).child(
        "A private, encrypted, versioned S3 bucket for models at architecture / brand / version. Captain makes it through a CloudFormation stack in your AWS account, so it shows in the AWS console with its history. Deleting the stack keeps the bucket and its models. If your team manages infrastructure in code, save it as Terraform or CloudFormation instead."))
      .child(h_flex().gap(8).items_center().text_size(12).child(label("Bucket")).child(box("reg-new-name", this.newName)))
      .child(h_flex().gap(8).items_center().text_size(12).child(label("Region"))
        .child(h_flex().flex_1().flex_wrap().gap(4).children(REGIONS.map((r) => opt(`reg-region-${r}`, this.region === r, r, (_e, cx) => { this.region = r; cx.notify(); })))))
      .child(h_flex().gap(8).flex_wrap()
        .child(Button(c, "reg-create", "Create in AWS", (_e, cx) => this.create(cx), { kind: "primary", small: true, disabled: !!this.job }))
        .child(Button(c, "reg-tf", "Save Terraform", (_e, cx) => this.saveTemplate(cx, "terraform"), { small: true }))
        .child(Button(c, "reg-cfn", "Save CloudFormation", (_e, cx) => this.saveTemplate(cx, "cloudformation"), { small: true })));
  }

  countText() {
    const n = new Set(this.models.map((m) => m.registry).filter(Boolean)).size || (this.regs ?? []).length;
    const from = (this.regs ?? []).length ? `across ${n} registr${n === 1 ? "y" : "ies"}` : "from a registry read earlier and not attached now";
    return `${this.models.length} model${this.models.length === 1 ? "" : "s"} ${from}. Click a model to show what it was built from.`;
  }

  /** Every model across the attached registries, by architecture, each with what it was built from. */
  lineage(c) {
    const byArch = new Map();
    for (const m of this.models) { if (!byArch.has(m.architecture)) byArch.set(m.architecture, []); byArch.get(m.architecture).push(m); }
    const order = [...byArch.keys()].sort((a, b) => (Number(/(\d+)/.exec(a)?.[1] ?? 99) - Number(/(\d+)/.exec(b)?.[1] ?? 99)) || a.localeCompare(b));
    const focus = this.focus ? this.models.find((m) => m.id === this.focus) : null;
    const related = new Set(focus ? [focus.id, ...(focus.parent_ids ?? focus.parents ?? []).map(String)] : []);
    return v_flex().flex_none().gap(10)
      .child(h_flex().gap(8).items_center().child(this.icon("lineage", 15, c.navy)).child(div().text_size(14).font_semibold().child("Models and lineage"))
        .child(div().text_size(12).text_color(c.grey500).child(this.countText())))
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
