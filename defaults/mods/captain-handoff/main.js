// CAPTAIN: Handoff, native. Captain stays outside production: a promote writes
// a candidate bundle and runs the readiness checks, and deploys nothing. The
// customer's own system deploys it, and that link and the online outcomes are
// recorded back here (REAL_TRAFFIC).
// GET /api/handoffs, GET /api/handoffs/{id}; record with cst handoff link / outcome.
import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, openOverlay } from "herdr";
import { api, watchWorkspace, watchScope } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Card, Badge, Button, Tabs, Empty, ErrorState, Loading, Problem, Table, RefChip, toneOf } from "./kit/ui.js";

const TABS = [["readiness", "Readiness"], ["evidence", "Evidence"], ["candidate", "Candidate"], ["links", "Deployments"], ["outcomes", "Online outcomes"]];
const words = (s) => String(s ?? "").replace(/[_-]/g, " ");
const day = (t) => (t ? String(t).slice(0, 16).replace("T", " ") : "");

/** Where a handoff stands, from its own events: live, deployed, or handed off. */
export function stage(h) {
  const deps = (h.links ?? []).filter((e) => e.kind === "deployment");
  if (deps.some((e) => e.live)) return "live";
  if (deps.length) return "deployed";
  return h.status ?? "handed off";
}

/** Gates passed out of all, and whether any were overridden. */
export function readiness(h) {
  const gates = h.readiness?.gates ?? [];
  return { ok: gates.filter((g) => g.ok).length, all: gates.length, overridden: (h.readiness?.overridden ?? []).length };
}

const task = (id, args) => () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: id, args } });

export default class CaptainHandoff extends View {
  init(_props, cx) {
    this.open = null;
    this.detail = null;
    this.tab = "readiness";
    this.table = new Table({
      id: "handoffs",
      rowKey: (r) => String(r.id),
      onRow: (r, cx) => this.show(r.id, cx),
      // What a handoff is, and how to make one.
      empty: (c) => Empty(c, "No handoffs yet",
        "A handoff passes a pipeline version that won its backtest to your own deployment process, after Captain checks it's ready. Captain deploys nothing itself, and records what went live and how it did.",
        Button(c, "ho-empty-new", "Hand off a candidate", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "handoff" } }), { kind: "primary" })),
      columns: [
        { key: "pipeline", label: "Candidate", render: (r, c) => RefChip(c, r.pipeline ?? r.id) },
        { key: "environment", label: "Environment", width: 150, optional: true, render: (r) => `${r.environment ?? ""}${r.tier ? ` (${r.tier})` : ""}` },
        { key: "stage", label: "Stage", width: 110, render: (r, c) => Badge(c, words(stage(r)), stage(r) === "live" ? "good" : stage(r) === "deployed" ? "blue" : "neutral") },
        { key: "readiness", label: "Readiness", width: 120, optional: true, render: (r, c) => { const x = readiness(r); return Badge(c, x.overridden ? `${x.ok}/${x.all}, overridden` : `${x.ok}/${x.all} passed`, x.overridden ? "warn" : x.ok === x.all ? "good" : "bad"); } },
        { key: "evidence", label: "Evidence", width: 130, optional: true, render: (r) => words(r.evidence?.evidence_label ?? "none") },
        { key: "outcomes", label: "Outcomes", width: 80, optional: true, render: (r) => String((r.links ?? []).filter((e) => e.kind === "outcome").length) },
        { key: "created_at", label: "Handed off", width: 130, render: (r) => day(r.created_at) },
      ],
      fetch: async () => {
        const r = await api("/api/handoffs");
        if (!r.ok) return { error: r.error, starting: r.starting, failed: r.failed };
        const rows = Array.isArray(r.data) ? r.data : [];
        return { rows, total: rows.length };
      },
    });
    this.table.load(cx);
    // The Explorer pick: the list reloads filtered to it (the services read X-CST-Scope).
    watchScope(cx, (cx) => { this.open = null; this.detail = null; this.table.load(cx); cx.notify(); });
    watchWorkspace(cx, (cx) => { this.open = null; this.detail = null; this.table.load(cx); cx.notify(); });
    cx.timer.every(15000, (cx) => (this.open ? this.load(this.open, cx) : this.table.load(cx)));
  }

  show(id, cx) {
    this.open = id; this.detail = null; this.tab = "readiness";
    this.load(id, cx);
    cx.notify();
  }

  load(id, cx) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/handoffs/${encodeURIComponent(id)}`);
      if (this.open !== id) return;
      this.detail = r.ok ? { data: r.data } : { problem: r };
      cx.notify();
    });
  }

  field(c, label, value, mono = false) {
    if (typeof value === "string" && /@v?\d+$/.test(value)) {
      return h_flex().gap(12).items_center().py(4).child(div().w(170).flex_none().text_size(12).text_color(c.grey500).child(label)).child(RefChip(c, value));
    }
    return h_flex().gap(12).items_start().py(4)
      .child(div().w(170).flex_none().text_size(12).text_color(c.grey500).child(label))
      .child(div().flex_1().min_w(0).text_size(13).text_color(c.navy).when(mono, (e) => e.font_family(MONO).text_size(12)).child(value == null || value === "" ? "none" : String(value)));
  }

  section(c, h) {
    const events = h.links ?? [];
    if (this.tab === "readiness") {
      const gates = h.readiness?.gates ?? [];
      return v_flex().gap(14)
        .child(Card(c, { title: "Readiness checks" }, ...(gates.length ? gates.map((g) => h_flex().gap(10).items_center().pb(6).border_b(1).border_color(c.rule)
          .child(Badge(c, g.ok ? "passed" : (h.readiness?.overridden ?? []).includes(g.gate) ? "overridden" : "failed", g.ok ? "good" : (h.readiness?.overridden ?? []).includes(g.gate) ? "warn" : "bad"))
          .child(div().flex_1().min_w(0).text_size(13).text_color(c.navy).child(words(g.gate)))
          .child(div().text_size(12).font_family(MONO).text_color(c.grey900).child(String(g.detail ?? g.value ?? g.message ?? "")))) : [div().text_size(13).text_color(c.grey500).child("This environment has no readiness checks.")])))
        .when(Boolean(h.readiness?.override_reason), (e) => e.child(ErrorState(c, `Overridden: ${h.readiness.override_reason}`)))
        .child(Card(c, { title: "Limitations" }, ...((h.limitations ?? []).length ? h.limitations.map((l) => div().text_size(13).line_height("19px").text_color(c.navy).child(l)) : [div().text_size(13).text_color(c.grey500).child("None recorded.")])))
        .child(Card(c, { title: "What happens next" }, div().text_size(13).line_height("19px").text_color(c.grey900).child(h.next ?? "Deploy it through your own process, then record the deployment here.")));
    }
    if (this.tab === "evidence") {
      const ev = h.evidence;
      if (!ev) return Empty(c, "No backtest evidence", "No backtest has this version as a challenger, so the handoff carries no offline evidence.");
      return Card(c, { title: "Evidence" },
        this.field(c, "Run", ev.run, true),
        h_flex().gap(12).items_center().py(4).child(div().w(170).flex_none().text_size(12).text_color(c.grey500).child("Evidence label")).child(Badge(c, words(ev.evidence_label), ev.evidence_label === "REAL_TRAFFIC" ? "good" : "warn")),
        this.field(c, "Simulated label", words(ev.simulated_label)),
        this.field(c, "Primary comparison", ev.primary ? `${ev.primary.metric ?? "primary"}: ${ev.primary.delta ?? ev.primary.diff ?? ""} ${ev.primary.verdict ? `(${ev.primary.verdict})` : ""}` : null),
        this.field(c, "Complete", ev.complete ? "yes" : "no"),
        this.field(c, "Independent", ev.independent ? "yes" : "no: the queries or judgments overlap"),
        this.field(c, "Queries", ev.queries, true),
        this.field(c, "Judgments", ev.judgments, true),
        this.field(c, "Segments that lose", (ev.segments_failing ?? []).map((s) => s.segment ?? s.name ?? JSON.stringify(s)).join(", ") || "none"));
    }
    if (this.tab === "candidate") {
      const cand = h.candidate ?? {};
      const cost = h.cost ?? {};
      return v_flex().gap(14)
        .child(Card(c, { title: "Candidate" }, ...Object.entries(cand).filter(([k]) => k !== "files").map(([k, v]) => this.field(c, words(k), typeof v === "object" && v !== null ? JSON.stringify(v) : v, true)),
          this.field(c, "files", (cand.files ?? []).length ? `${cand.files.length} file(s)` : "none")))
        .child(Card(c, { title: "Cost and serving" }, this.field(c, "Training (USD)", cost.train_usd), this.field(c, "Training GPU hours", cost.train_gpu_hours),
          this.field(c, "GPU type", cost.gpu_type), this.field(c, "Serving, per 1,000 (USD)", cost.serve_usd_per_1k),
          this.field(c, "p99 budget (ms)", h.serving_requirements?.p99_ms_budget), this.field(c, "Target", h.serving_requirements?.target, true)));
    }
    const kind = this.tab === "links" ? "deployment" : "outcome";
    const list = events.filter((e) => e.kind === kind);
    if (!list.length) {
      return this.tab === "links"
        ? Empty(c, "Not deployed yet", "Deploy this candidate through your own system, then record the deployment so Captain knows what's live.", Button(c, "ho-link", "Record a deployment", task("record-deployment", { id: this.open }), { kind: "primary" }))
        : Empty(c, "No online outcomes", "Attach results from your experiment system. They count as REAL_TRAFFIC evidence.", Button(c, "ho-out", "Add an outcome", task("add-outcome", { id: this.open }), { kind: "primary" }));
    }
    return Card(c, { title: this.tab === "links" ? `Deployments (${list.length})` : `Online outcomes (${list.length})` }, ...list.slice().reverse().map((e) => v_flex().gap(4).pb(8).border_b(1).border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .when(kind === "deployment", (x) => x.child(div().text_size(13).font_semibold().text_color(c.navy).child(`${e.system} ${e.deployment_id}`)).child(Badge(c, e.live ? "live" : "not live", e.live ? "good" : "neutral")))
        .when(kind === "outcome", (x) => x.child(div().text_size(13).font_semibold().text_color(c.navy).child(`${e.metric}: ${e.value}`))
          .when(e.ci_low != null && e.ci_high != null, (y) => y.child(div().text_size(12).font_family(MONO).text_color(c.grey900).child(`[${e.ci_low}, ${e.ci_high}]`)))
          .child(Badge(c, "real traffic", "good")))
        .child(div().flex_1())
        .child(div().text_size(12).text_color(c.grey500).child(`${e.by ?? ""} ${day(e.at)}`)))
      .when(Boolean(e.url) || Boolean(e.source), (x) => x.child(div().text_size(12).font_family(MONO).text_color(c.grey900).truncate().child(e.url ?? `${e.source}${e.experiment ? `, ${e.experiment}` : ""}`)))));
  }

  detailView(c) {
    const back = Button(c, "ho-back", "All handoffs", (_e, cx) => { this.open = null; this.detail = null; this.table.load(cx); cx.notify(); }, { kind: "quiet", small: true });
    if (!this.detail) return v_flex().size_full().p(24).gap(10).child(h_flex().child(back)).child(Loading(c));
    if (this.detail.problem) return v_flex().size_full().p(24).gap(10).child(h_flex().child(back)).child(Problem(c, this.detail.problem, (cx) => this.load(this.open, cx)));
    const h = this.detail.data ?? {};
    const x = readiness(h);
    const events = h.links ?? [];
    return v_flex().size_full().min_h(0)
      .child(v_flex().flex_none().px(24).pt(16).gap(10)
        .child(h_flex().child(back))
        .child(h_flex().gap(10).items_center()
          .child(div().flex_1().min_w(0).child(RefChip(c, h.pipeline ?? this.open)))
          .child(Button(c, "ho-link2", "Record a deployment", task("record-deployment", { id: this.open })))
          .child(Button(c, "ho-out2", "Add an outcome", task("add-outcome", { id: this.open }), { kind: "primary" })))
        .child(h_flex().gap(8).items_center()
          .child(Badge(c, words(stage(h)), stage(h) === "live" ? "good" : stage(h) === "deployed" ? "blue" : "neutral"))
          .child(Badge(c, x.overridden ? `${x.ok}/${x.all} checks, overridden` : `${x.ok}/${x.all} checks passed`, x.overridden ? "warn" : x.ok === x.all ? "good" : "bad"))
          .child(div().text_size(12).text_color(c.grey500).child(`to ${h.environment ?? "?"}${h.tier ? ` (${h.tier})` : ""}, by ${h.created_by ?? "?"} ${day(h.created_at)}`)))
        .child(Tabs(c, "ho-tab", TABS.map(([id, label]) => ({ id, label, count: id === "links" ? events.filter((e) => e.kind === "deployment").length : id === "outcomes" ? events.filter((e) => e.kind === "outcome").length : undefined })), this.tab, (id, cx) => { this.tab = id; cx.notify(); })))
      .child(div().id("ho-body").flex_1().min_h(0).overflow_y_scroll().px(24).py(16).child(this.section(c, h)));
  }

  list(c) {
    return v_flex().size_full().p(24).gap(14)
      .child(h_flex().gap(12).items_center()
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(20).font_semibold().text_color(c.navy).child("Handoff"))
          .child(div().text_size(12.5).text_color(c.grey500).child("Captain hands candidates to your deployment process and deploys nothing itself. What went live, and how it did, is recorded here.")))
        .child(Button(c, "ho-new", "Hand off a candidate", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "handoff" } }), { kind: "primary" })))
      .child(div().flex_1().min_h(0).child(this.table.render(c)));
  }

  render() {
    const c = palette(isDark(theme()));
    return div().size_full().bg(c.canvas).font_family(FONT).text_color(c.navy).child(this.open ? this.detailView(c) : this.list(c));
  }
}
