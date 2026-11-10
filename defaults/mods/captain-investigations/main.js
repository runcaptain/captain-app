// CAPTAIN: Investigations, native. The home section of the captain layout.
// List: GET /api/investigations?state=&q= (small; the server filters).
// Detail: GET /api/investigations/{id}: hypothesis and revisions, evidence,
// signals, runs (each with its own status; failed runs stay listed), the
// decision, and Copy prompt (GET /prompt, then the clipboard).
// Agent turn, job and investigation are separate states: each shows from its
// own field, never inferred from another.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, openOverlay, notify, run, spaces, focused, shared, setShared, exec, knownAgents } from "herdr";
import { api, query, watchWorkspace, watchScope, watchRecords } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Card, Badge, Button, Tabs, Empty, ErrorState, Loading, Problem, Table, RefChip, toneOf } from "./kit/ui.js";
import { View } from "gpui-kit";
import { icon } from "./kit/icons.js";

const STATES = [["", "All"], ["draft", "Draft"], ["running", "Running"], ["ready_for_review", "Ready for review"], ["decided", "Decided"]];
const TABS = [["overview", "Hypothesis"], ["evidence", "Evidence"], ["signals", "Signals"], ["runs", "Runs"], ["decision", "Decision"]];

const words = (s) => String(s ?? "").replace(/_/g, " ");
const who = (x) => (x == null ? "" : typeof x === "string" ? x : x.agent ? `${x.agent} (agent)` : x.user ?? x.by ?? x.name ?? "");
function when(t) {
  if (!t) return "";
  const d = new Date(typeof t === "number" ? t * (t < 1e12 ? 1000 : 1) : t);
  if (isNaN(d)) return String(t);
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return d.toISOString().slice(0, 10);
}
const outcome = (d) => (d ? words(d.outcome ?? d.decision ?? "decided") : null);

// The coding agents Captain recognises, by the name `cst` records (proposals.agent_name), with their marks.
// One-color glyphs: Claude in its orange, Codex (its mark without the tile) and Cursor in the text color.
const AGENTS = { claude: ["Claude Code", "icons/claude.svg", "#D97757"], "claude-code": ["Claude Code", "icons/claude.svg", "#D97757"],
  codex: ["Codex", "icons/captain-agent-codex.svg"], cursor: ["Cursor", "icons/cursor.svg"], "cursor-agent": ["Cursor", "icons/cursor.svg"] };
const agentKey = (name) => String(name ?? "").toLowerCase().replace(/^the agent /, "");
const agentLabel = (name) => AGENTS[agentKey(name)]?.[0] ?? `the agent ${name}`;
/** The agent's icon beside a recommendation: its logo when known, a bot glyph for other agents, a person otherwise. */
function agentMark(c, name) {
  const known = AGENTS[agentKey(name)];
  const box = div().size(28).flex_none().flex().items_center().justify_center().border_1().border_color(c.rule).bg(c.surface2);
  if (known) return box.child(svg(known[1]).size(18).text_color(known[2] ?? c.navy));
  return box.child(svg(icon(name ? "bot" : "user")).size(15).text_color(c.grey500));
}

export default class CaptainInvestigations extends View {
  init(_props, cx) {
    this.state = "";
    this.search = InputState.new({ placeholder: "Search experiments" });
    this.search.on("change", (_e, cx) => { this.q = this.search.value(); this.reload(cx); });
    this.q = "";
    this.open = null;
    this.detail = null;
    this.tab = "overview";
    this.copied = false;
    this.table = new Table({
      id: "investigations",
      rowKey: (r) => String(r.id),
      onRow: (r, cx) => this.show(r.id, cx),
      // What an investigation is, and the way to start one.
      empty: (c) => Empty(c, "No experiments yet",
        "An experiment is one question about your search, like \"does the tuned ranker help long queries?\", with its hypothesis, the evaluations that test it, and the decision you make. Agents in a space can work on it with you.",
        Button(c, "inv-empty-new", "New experiment", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "investigation" } }), { kind: "primary" })),
      columns: [
        { key: "title", label: "Experiment", render: (r) => r.title ?? r.id },
        { key: "state", label: "State", width: 130, render: (r, c) => Badge(c, words(r.state), toneOf(r.state)) },
        { key: "latest_run", label: "Latest run", width: 120, optional: true, render: (r, c) => r.latest_run ? Badge(c, words(r.latest_run.status ?? r.latest_run), toneOf(r.latest_run.status ?? r.latest_run)) : "none" },
        { key: "runs", label: "Runs", width: 50, optional: true, render: (r) => String(Array.isArray(r.runs) ? r.runs.length : r.runs ?? 0) },
        { key: "decision", label: "Decision", width: 120, optional: true, render: (r) => outcome(r.decision) ?? "not yet" },
        { key: "initiator", label: "Opened by", width: 140, optional: true, render: (r) => who(r.initiator) },
        { key: "updated_at", label: "Updated", width: 90, optional: true, render: (r) => when(r.updated_at) },
      ],
      fetch: async () => {
        const r = await api(`/api/investigations${query({ state: this.state, q: this.q })}`);
        if (!r.ok) return { error: r.error, starting: r.starting, failed: r.failed };
        const rows = Array.isArray(r.data) ? r.data : r.data?.investigations ?? [];
        return { rows, total: rows.length };
      },
    });
    this.table.load(cx);
    // Another space's workspace: back to the list, read from there.
    // The Explorer pick: the list reloads filtered to it (the services read X-CST-Scope).
    watchScope(cx, (cx) => { this.open = null; this.detail = null; this.table.load(cx); cx.notify(); });
    watchWorkspace(cx, (cx) => { this.open = null; this.detail = null; this.bound = undefined; this.table.load(cx); cx.notify(); });
    // A form saved something (a handoff, a deployment, an outcome, a decision): the list and the open one reload now.
    watchRecords(cx, (cx) => { this.table.load(cx); if (this.open) this.load(this.open, cx); cx.notify(); });
    cx.timer.every(10000, (cx) => (this.open ? this.load(this.open, cx) : this.table.load(cx)));
    // Experiments an agent or a person proposed, waiting for a person's approval (the sidebar's count).
    this.proposals = []; this.reviewing = null;
    this.loadProposals(cx);
    cx.timer.every(10000, (cx) => this.loadProposals(cx));
    // "Open investigation: ..." from the palette (captain-go sets shared "captain.open").
    cx.timer.every(500, (cx) => {
      const o = shared("captain.open");
      if (o?.section === "investigations" && o.investigation && o.at !== this.opened) {
        this.opened = o.at;
        this.show(o.investigation, cx);
      }
    });
    // A space bound to an investigation opens it here when you switch to that space.
    cx.timer.every(1500, (cx) => {
      const bound = (shared("captain.spaces") ?? {})[focused()?.space]?.investigation ?? null;
      if (bound !== this.bound) {
        this.bound = bound;
        if (bound && bound !== this.open) this.show(bound, cx);
      }
    });
  }

  /** A space for this investigation: its terminals get CST_INVESTIGATION, and switching to it opens it here. */
  openSpace(d, cx) {
    cx.spawn(async (cx) => {
      const ws = shared("captain.workspace");
      const home = shared("captain.daemon")?.home;
      let folder = null;
      if (home && ws) {
        const dir = `${home}/agent/${ws}`;
        try { if ((await exec("test", ["-d", dir])).code === 0) folder = dir; } catch {}
      }
      const before = new Set(spaces().map((x) => x.id));
      run("space.new", folder ? { name: d.title ?? this.open, folder } : { name: d.title ?? this.open });
      const made = spaces().find((x) => !before.has(x.id));
      if (made) setShared("captain.spaces", { ...(shared("captain.spaces") ?? {}), [made.id]: { workspace: ws, investigation: this.open } });
      notify({ title: "Space opened", body: `Terminals there know this experiment (CST_INVESTIGATION=${this.open}).`, level: "info" });
      cx.notify();
    });
  }

  reload(cx) {
    this.table.load(cx);
    this.loadProposals(cx);
    cx.notify();
  }

  loadProposals(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/proposals?status=proposed");
      this.proposals = r.ok && Array.isArray(r.data) ? r.data : [];
      cx.notify();
    });
  }

  review(cx, p, decision) {
    this.reviewing = p.id; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/proposals/${encodeURIComponent(p.id)}/review`, { body: { decision, note: "" } });
      this.reviewing = null;
      if (!r.ok) notify({ title: "Couldn't record the review", body: String(r.error ?? ""), level: "error" });
      else notify({ title: decision === "approved" ? "Approved" : "Rejected", body: decision === "approved" ? `${p.title}: run it from the CLI or ask the agent that proposed it.` : p.title, level: "info" });
      this.loadProposals(cx);
    });
  }

  /** Recommended experiments (`cst recommend` from an agent or a person), each with what it tests, who recommended
   *  it with their agent's icon, and Approve or Reject. */
  toReview(c) {
    const ps = this.proposals ?? [];
    if (!ps.length) return null;
    return v_flex().id("inv-proposals").gap(8).p(14).border_1().border_color(c.blue).bg(c.blueLight)
      .child(div().text_size(13).font_semibold().text_color(c.navy).child(`${ps.length} ${ps.length === 1 ? "recommendation" : "recommendations"} to review`))
      .child(div().text_size(12).text_color(c.grey900).child("Agents recommend offline evaluations from the cst CLI. Nothing runs until a person approves it."))
      .children(ps.slice(0, 5).map((p) => h_flex().id(`inv-prop-${p.id}`).gap(10).items_center().p(10).bg(c.surface).border_1().border_color(c.rule)
        .child(agentMark(c, p.agent))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(13).font_medium().text_color(c.navy).child(p.title ?? p.id))
          .child(div().text_size(12).text_color(c.grey900).child(p.hypothesis ?? ""))
          .child(div().text_size(11.5).text_color(c.grey500).child(`Recommended by ${p.agent ? agentLabel(p.agent) : p.author ?? "someone"}${(p.arms ?? []).length ? `: ${[].concat(p.arms).map((a) => a.pipeline ?? a).join(" vs ")}` : ""}`)))
        .child(Button(c, `inv-approve-${p.id}`, "Approve", (_e, cx) => this.review(cx, p, "approved"), { kind: "primary", small: true, disabled: this.reviewing === p.id }))
        .child(Button(c, `inv-reject-${p.id}`, "Reject", (_e, cx) => this.review(cx, p, "rejected"), { small: true, disabled: this.reviewing === p.id }))));
  }

  show(id, cx) {
    this.open = id; this.detail = null; this.tab = "overview"; this.copied = false;
    this.load(id, cx);
    cx.notify();
  }

  load(id, cx) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/investigations/${encodeURIComponent(id)}`);
      if (this.open !== id) return;
      this.detail = r.ok ? { data: r.data } : { problem: r };
      cx.notify();
    });
  }

  /** Start Claude Code or Codex in this space's terminal, with this investigation's prompt. */
  askAgent(cx) {
    cx.spawn(async (cx) => {
      const [p, agents] = await Promise.all([api(`/api/investigations/${encodeURIComponent(this.open)}/prompt?ask=investigate`), knownAgents()]);
      const agent = ["claude", "codex"].map((id) => agents.find((a) => a.installed && (a.id === id || a.name?.toLowerCase().startsWith(id) || a.run?.[0] === id))).find(Boolean);
      if (!p.ok || !p.data?.prompt) return notify({ title: "Couldn't build the prompt", body: String(p.error ?? ""), level: "error" });
      if (!agent) {
        cx.write_to_clipboard(p.data.prompt);
        return notify({ title: "No agent installed here", body: "The prompt is on the clipboard: paste it into Claude Code, Codex, Cursor or Devin.", level: "info" });
      }
      run("tab.new", { name: agent.name ?? "Agent", argv: agent.run, task: p.data.prompt });
      run("layout.show", { id: "work" });
      cx.notify();
    });
  }

  copyPrompt(cx) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/investigations/${encodeURIComponent(this.open)}/prompt?ask=investigate`);
      if (r.ok && r.data?.prompt) {
        cx.write_to_clipboard(r.data.prompt);
        this.copied = true;
        notify({ title: "Prompt copied", body: "Paste it to an agent in a shell here, or in Cursor, Devin or any terminal.", level: "info" });
      } else {
        notify({ title: "Couldn't build the prompt", body: String(r.error ?? ""), level: "error" });
      }
      cx.notify();
    });
  }

  run(cx) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/investigations/${encodeURIComponent(this.open)}/run`, { body: {} });
      notify(r.ok ? { title: "Run started", body: `Job ${r.data?.job ?? ""}. It shows under Runs as it goes.`, level: "info" }
        : { title: "Couldn't start a run", body: String(r.error ?? ""), level: "error" });
      this.load(this.open, cx);
    });
  }

  list(c) {
    return v_flex().size_full().p(24).gap(14)
      .child(h_flex().gap(12).items_center()
        .child(div().flex_none().text_size(20).font_semibold().text_color(c.navy).child("Experiments"))
        .child(div().flex_1())
        // The search gives way first when the section is narrow.
        .child(div().id("inv-search").flex_shrink(1).min_w(120).w(260).h(32).px(10).flex().items_center().bg(c.surface).border_1().border_color(c.grey300).cursor_text()
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.search).w_full().text_size(13).text_color(c.navy)))
        .child(Button(c, "inv-new", "New experiment", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "investigation" } }), { kind: "primary" })))
      .child(this.toReview(c) ?? div())
      .child(Tabs(c, "inv-state", STATES.map(([id, label]) => ({ id, label })), this.state, (id, cx) => { this.state = id; this.reload(cx); }))
      .child(div().flex_1().min_h(0).child(this.table.render(c)));
  }

  section(c, tab, d) {
    const runs = d.runs ?? [];
    if (tab === "overview") {
      const revs = d.revisions ?? [];
      return v_flex().gap(14)
        .child(Card(c, { title: `Hypothesis, revision ${d.revision ?? revs.length ?? 1}` }, div().text_size(14).line_height("21px").text_color(c.navy).child(d.hypothesis || "No hypothesis written yet.")))
        .when(Array.isArray(d.missing) && d.missing.length > 0, (e) => e.child(ErrorState(c, `Still missing before a run: ${d.missing.map(words).join(", ")}`)))
        .child(Card(c, { title: `Revisions (${revs.length})` }, ...(revs.length ? revs.slice().reverse().map((r, i) => v_flex().gap(4).pb(8).border_b(1).border_color(c.rule)
          .child(h_flex().gap(8).items_center().child(Badge(c, `rev ${r.revision ?? r.rev ?? revs.length - i}`, "blue")).child(div().text_size(12).text_color(c.grey500).child(`${who(r.by ?? r.initiator)} ${when(r.at ?? r.updated_at)}`)))
          .child(div().text_size(13).text_color(c.navy).child(r.hypothesis ?? ""))
          .when(Boolean(r.note), (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(r.note)))) : [div().text_size(13).text_color(c.grey500).child("No revisions yet.")])));
    }
    if (tab === "evidence") {
      const ev = d.evidence ?? [];
      return ev.length ? Card(c, { title: `Evidence (${ev.length})` }, ...ev.map((e) => h_flex().gap(10).items_start().pb(8).border_b(1).border_color(c.rule)
        .child(Badge(c, words(e.kind), "neutral")).child(v_flex().flex_1().min_w(0).gap(2)
          .child(RefChip(c, e.ref))
          .when(Boolean(e.note), (x) => x.child(div().text_size(12.5).text_color(c.grey900).child(e.note))))))
        : Empty(c, "No evidence attached", "Datasets, runs, slices, queries, signals and notes attached to this experiment show here.",
          Button(c, "ev-add", "Attach evidence", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "evidence", args: { id: this.open } } })));
    }
    if (tab === "signals") {
      const sig = d.signals ?? [];
      return sig.length ? Card(c, { title: `Signals (${sig.length})` }, ...sig.map((s) => v_flex().gap(4).pb(8).border_b(1).border_color(c.rule)
        .child(h_flex().gap(8).items_center().child(div().flex_1().text_size(13).font_semibold().text_color(c.navy).child(s.name ?? s.id))
          .children(Object.entries(s.checks ?? {}).map(([k, v]) => Badge(c, `${k}: ${words(v?.status ?? v)}`, toneOf(v?.status ?? v)))))
        .when(Boolean(s.definition), (x) => x.child(div().font_family(MONO).text_size(12).text_color(c.grey900).child(s.definition)))
        .when(Boolean(s.rationale), (x) => x.child(div().text_size(12.5).text_color(c.grey900).child(s.rationale)))))
        : Empty(c, "No candidate signals", "Signals this experiment proposes, with their availability and leakage checks, show here.");
    }
    if (tab === "runs") {
      return runs.length ? Card(c, { title: `Runs (${runs.length})` }, ...runs.slice().reverse().map((r) => v_flex().gap(4).pb(8).border_b(1).border_color(c.rule)
        .child(h_flex().gap(8).items_center()
          .child(RefChip(c, r.run ?? r.id))
          .child(Badge(c, words(r.status), toneOf(r.status)))
          .child(div().text_size(12).text_color(c.grey500).child(`rev ${r.rev ?? "?"}`))
          .child(div().flex_1())
          .child(div().text_size(12).text_color(c.grey500).child(`${who(r.agent ? { agent: r.agent } : r.by)} ${when(r.at)}`)))
        .when(Boolean(r.error), (x) => x.child(ErrorState(c, r.error)))))
        : Empty(c, "No runs yet", "A run pins the current revision and compares the baseline with the candidates.", Button(c, "run-first", "Run it", (_e, cx) => this.run(cx), { kind: "primary" }));
    }
    const dec = d.decision;
    return dec ? Card(c, { title: "Decision" },
      h_flex().gap(8).items_center().child(Badge(c, words(dec.outcome), toneOf(dec.outcome))).child(div().text_size(12).text_color(c.grey500).child(`${who(dec.by ?? dec.decided_by)} ${when(dec.at ?? dec.decided_at)}`)),
      ...[["Claim", dec.claim], ["Interpretation", dec.interpretation], ["Limitations", Array.isArray(dec.limitations) ? dec.limitations.join("; ") : dec.limitations], ["Next step", dec.next_step]]
        .filter(([, v]) => v).map(([k, v]) => v_flex().gap(2).child(div().text_size(11.5).font_medium().text_color(c.grey500).child(k)).child(div().text_size(13).line_height("19px").text_color(c.navy).child(String(v)))),
        // A decision to go on: its handoff is the next step, with the plan's candidate.
        ...(["continue", "online-test"].includes(dec.outcome) ? [h_flex().child(Button(c, "decision-handoff", "Send to launch review", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560,
          props: { task: "handoff", values: (d.plan?.candidates ?? d.candidates ?? [])[0] ? { pipeline: (d.plan?.candidates ?? d.candidates)[0] } : {} } }), { kind: "primary", small: true }))] : []))
      : Empty(c, "No decision yet", "Only a person records the decision: continue, online test, reject or inconclusive.",
        Button(c, "decide", "Record a decision", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "decision", args: { id: this.open } } }), { kind: "primary" }));
  }

  detailView(c) {
    const back = Button(c, "inv-back", "All experiments", (_e, cx) => { this.open = null; this.detail = null; this.table.load(cx); cx.notify(); }, { kind: "quiet", small: true });
    if (!this.detail) return v_flex().size_full().p(24).gap(10).child(h_flex().child(back)).child(Loading(c));
    if (this.detail.problem) return v_flex().size_full().p(24).gap(10).child(h_flex().child(back)).child(Problem(c, this.detail.problem, (cx) => this.load(this.open, cx)));
    const d = this.detail.data ?? {};
    return v_flex().size_full().min_h(0)
      .child(v_flex().flex_none().px(24).pt(16).gap(10)
        .child(h_flex().child(back))
        .child(h_flex().gap(10).items_center()
          .child(div().flex_1().min_w(0).truncate().text_size(20).font_semibold().text_color(c.navy).child(d.title ?? this.open))
          .child(Button(c, "inv-agent", "Open an agent in this space", (_e, cx) => this.askAgent(cx)))
          .child(Button(c, "inv-space", "Open a space for this experiment", (_e, cx) => this.openSpace(d, cx)))
          .child(Button(c, "inv-prompt", this.copied ? "Copied" : "Copy prompt", (_e, cx) => this.copyPrompt(cx)))
          .child(Button(c, "inv-run", "Run", (_e, cx) => this.run(cx), { kind: "primary" })))
        .child(h_flex().gap(8).items_center()
          .child(Badge(c, words(d.state), toneOf(d.state)))
          .child(div().text_size(12).text_color(c.grey500).child(`Opened by ${who(d.initiator) || "unknown"}, updated ${when(d.updated_at)}`)))
        .child(Tabs(c, "inv-tab", TABS.map(([id, label]) => ({ id, label, count: id === "runs" ? (d.runs ?? []).length : id === "evidence" ? (d.evidence ?? []).length : id === "signals" ? (d.signals ?? []).length : undefined })), this.tab, (id, cx) => { this.tab = id; cx.notify(); })))
      .child(div().id("inv-body").flex_1().min_h(0).overflow_y_scroll().px(24).py(16).child(this.section(c, this.tab, d)));
  }

  render() {
    const c = palette(isDark(theme()));
    return div().size_full().bg(c.canvas).font_family(FONT).text_color(c.navy).child(this.open ? this.detailView(c) : this.list(c));
  }
}
