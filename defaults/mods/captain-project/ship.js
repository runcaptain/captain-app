// CAPTAIN: the Ship part of a project's page as a path a person walks: 1 Stage, 2 Try it, 3 A/B test, 4 Launch review
// (Edgar, 2026-11-10: "make it easier to stage ml models to be deployed ... business user friendly"). A staged model
// runs in the team's own cloud, or on this computer; Captain hosts nothing. Each step has a status, one primary
// action and a tick when it is done. Words stay plain: where it runs, what it costs, when it stops by itself. The
// endpoint name, the instance type and the log sit behind Details.
//   GET  /api/projects/{id}/staging          what is staged and how it is doing
//   POST /api/projects/{id}/stage            start it (a job). 409 {needs_confirm, plan} when a cost needs a yes
//   POST /api/projects/{id}/unstage          stop it and remove what Captain made
//   POST /api/projects/{id}/staging/query    the Try it box: the staged results beside the baseline's
//   POST /api/projects/{id}/staging/check    did serving compute the features training computes? One line.
//   GET  /api/projects/{id}/ab               the A/B readout: sessions per arm, the primary metric, guardrails
//   POST /api/projects/{id}/ab/start|ramp|rollback|stop|simulate   each one click, with a confirmation, in history
import { div, svg, image } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { exec, appHome, openOverlay } from "herdr";
import { MONO } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Badge, Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { SendToAgent } from "./kit/context.js";

const enc = encodeURIComponent;
const THUMB = 96;
const short = (ref) => String(ref ?? "").replace(/^.*\//, "");
const pct = (x) => `${Math.round(Number(x) * 100)}%`;
const form = (task, values = {}) => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task, values } });
export const METRICS = { clicks: "Sessions with a click", purchases: "Sessions with a purchase", revenue: "Revenue per session" };
const RAMP = [0.01, 0.05, 0.25, 0.5, 1]; // start small: 1% first

/** "3 h 42 min", "12 min", "under a minute". */
export function span(seconds) {
  const m = Math.floor(Math.max(0, Number(seconds) || 0) / 60);
  if (m < 1) return "under a minute";
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 60)} h`;
}

export class Ship {
  /** `page` is the project page: it gives the project id, its candidates and the baseline, and is redrawn through cx. */
  constructor(page) {
    this.page = page;
    this.status = null;   // GET /staging
    this.problem = null;  // a sentence, when staging could not be read
    this.busy = null;     // what is in flight, in words: "Starting the endpoint"
    this.note = null;     // {tone, text}: the last thing that happened
    this.confirm = null;  // a plan whose cost needs a yes
    this.details = false;
    this.result = null; this.asking = false; this.askError = null;
    this.thumbs = new Map(); // image address -> file name | null (failed) | undefined (asked)
    this.ab = null;        // GET /ab: the readout
    this.setup = null;     // the A/B form while it is open: {split, primary, mode}
    this.ask2 = null;      // a ramp or a rollback waiting for a yes: {kind, split?}
    this.box = InputState.new({ placeholder: "Type what a shopper would search for" });
    this.box.on("submit", (_e, cx) => this.ask(cx ?? this.cx));
  }

  pid() { return this.page.pid(); }

  load(cx) {
    this.cx = cx;
    const pid = this.pid();
    if (!pid) return;
    if (pid !== this.shown) { this.shown = pid; this.status = null; this.result = null; this.note = null; this.confirm = null; this.askError = null; this.ab = null; this.setup = null; this.ask2 = null; }
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(pid)}/ab`, { scoped: false, timeout: 60 });
      if (this.pid() !== pid) return;
      if (r.ok) this.ab = r.data;
      cx.notify();
    });
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(pid)}/staging`, { scoped: false });
      if (this.pid() !== pid) return;
      if (r.ok) { this.status = r.data; this.problem = null; } else if (!r.starting) this.problem = plainError(r.error, "Captain couldn't read staging.");
      cx.notify();
    });
  }

  /** From the project's own answer (GET /stages): ask for the details only when something is staged or tested. */
  sync(cx, ship) {
    this.cx = cx;
    if (ship?.staging || ship?.ab) return this.load(cx);
    const pid = this.pid();
    if (pid !== this.shown) { this.shown = pid; this.result = null; this.note = null; this.confirm = null; this.askError = null; this.setup = null; this.ask2 = null; }
    this.status = { status: "none", staged: null, text: "Nothing is staged." };
    this.ab = { status: "none", experiment: null, can_measure: [] };
    this.problem = null;
  }

  /** Redraw the countdown, and look again while something is running or starting. */
  tick(cx) {
    const s = this.status?.status;
    if (this.pid() && !this.busy && (s === "running" || s === "starting")) this.load(cx);
  }

  running() { return this.status?.status === "running"; }
  staged() { return this.status?.staged ?? null; }

  /** The candidate the Stage button would start: the one open on the page, else the newest judged, else the newest. */
  pick() {
    const xs = (this.page.exps?.experiments ?? []).filter((e) => !e.is_champion);
    const open = xs.find((e) => e.id === this.page.open);
    return open ?? [...xs].reverse().find((e) => e.evaluation) ?? xs[xs.length - 1] ?? null;
  }

  async wait(id, minutes) {
    for (let i = 0; i < minutes * 120; i++) {
      const r = await api(`/api/jobs/${enc(id)}`, { scoped: false });
      if (r.ok && r.data?.status === "done") return { ok: true, result: r.data.result };
      if (r.ok && r.data?.status === "failed") return { ok: false, error: plainError(r.data.message, "It didn't finish.") };
      if (r.ok && r.data?.message && r.data.status === "running") { this.busy = r.data.message; this.cx?.notify(); }
      try { await exec("sleep", ["0.5"]); } catch { break; }
    }
    return { ok: false, error: "It's still starting. Look again in a minute." };
  }

  /** Start staging. `body` is {candidate} or {baseline: true}; `confirmed` after the person agreed to the cost. */
  stage(cx, body, confirmed = false) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = "Getting it ready"; this.note = null; this.confirm = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/stage`, { body: { ...body, ...(confirmed ? { confirm: true } : {}) }, scoped: false });
      if (r.status === 409 && r.data?.needs_confirm) { this.busy = null; this.confirm = { plan: r.data.plan, body }; cx.notify(); return; }
      if (!r.ok) { this.busy = null; this.note = { tone: "fail", text: plainError(r.error, "Staging didn't start. Try again.") }; cx.notify(); return; }
      const cloud = r.data?.plan?.kind && r.data.plan.kind !== "local";
      const done = await this.wait(r.data.job, cloud ? 30 : 3);
      this.busy = null; this.result = null;
      this.note = done.ok ? { tone: "good", text: `${short(done.result?.model_version)} is staged. Try it below.` } : { tone: "fail", text: done.error };
      this.load(cx);
      this.page.reload?.(cx);
    });
  }

  stop(cx) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = "Stopping it"; this.note = null; this.stopAsk = false;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/unstage`, { body: {}, scoped: false });
      const done = r.ok ? await this.wait(r.data.job, 5) : { ok: false, error: plainError(r.error, "It didn't stop. Try again.") };
      this.busy = null; this.result = null;
      this.note = done.ok && done.result?.status === "stopped" ? { tone: "good", text: "Staging is stopped. Nothing is left running." }
        : { tone: "fail", text: done.ok ? plainError(done.result?.error, "It stopped, but something could not be removed.") : done.error };
      this.load(cx);
      this.page.reload?.(cx);
    });
  }

  ask(cx) {
    const q = this.box.value().trim();
    if (!q) { this.askError = "Type a query to try."; cx.notify(); return; }
    if (this.asking) return;
    this.asking = true; this.askError = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/query`, { body: { query: q, k: 8 }, scoped: false, timeout: 60 });
      this.asking = false;
      if (r.ok) { this.result = r.data; this.pictures(cx); } else this.askError = plainError(r.error, "The staged model didn't answer. Try again.");
      cx.notify();
    });
  }

  /** One A/B action (start, ramp, roll back, stop), then read everything again. */
  abDo(cx, what, body, done) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = { start: "Starting the A/B test", ramp: "Changing the split", rollback: "Rolling back", stop: "Ending the test" }[what];
    this.note = null; this.ask2 = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/ab/${what}`, { body: body ?? {}, scoped: false, timeout: 120 });
      this.busy = null;
      this.note = r.ok ? { tone: "good", text: done } : { tone: "fail", text: plainError(r.error, "That didn't work. Try again.") };
      if (r.ok && what === "start") this.setup = null;
      this.load(cx);
      this.page.reload?.(cx);
    });
  }

  /** Simulated shoppers at staging, so the loop can be seen with no traffic yet. Everything they produce reads SIMULATED. */
  simulate(cx) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = "Sending simulated shoppers"; this.note = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/ab/simulate`, { body: { sessions: 1000 }, scoped: false });
      const done = r.ok ? await this.wait(r.data.job, 30) : { ok: false, error: plainError(r.error, "The simulated shoppers didn't start.") };
      this.busy = null;
      this.note = done.ok ? { tone: "good", text: done.result?.text ?? "Simulated shoppers sent." } : { tone: "fail", text: done.error };
      this.load(cx);
      this.page.reload?.(cx);
    });
  }

  /** Compare what the endpoint served with what training computes, and show the answer as one line. */
  check(cx) {
    if (this.checking) return;
    this.checking = true;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/check`, { body: {}, scoped: false, timeout: 120 });
      this.checking = false;
      if (!r.ok) this.note = { tone: "fail", text: plainError(r.error, "The check didn't finish. Try again.") };
      this.load(cx);
      this.page.reload?.(cx);
    });
  }

  /** The check's line under Try it: green when serving matches training, red when it doesn't, and what that blocks. */
  checkLine(c) {
    const k = this.staged()?.check, on = this.running();
    if (!on && !k) return null;
    const tone = k?.status === "pass" ? c.good : k?.status === "fail" ? c.fail : c.grey500;
    const text = k?.text ?? "Not checked yet: does the endpoint compute the same features training did?";
    return h_flex().id("ship-check").gap(8).items_center().flex_wrap()
      .child(svg(icon(k?.status === "pass" ? "circle-check" : k?.status === "fail" ? "triangle-alert" : "clock")).size(13).flex_none().text_color(tone))
      .child(div().id("ship-check-text").text_size(12).line_height("17px").text_color(k?.status === "fail" ? c.fail : c.grey900)
        .child(k?.status === "fail" ? `${text} Launch review will ask for a reason to go on.` : text))
      .when(on, (e) => e.child(Button(c, "ship-check-go", this.checking ? "Checking..." : k ? "Check again" : "Check now", (_e, cx) => this.check(cx), { small: true, kind: "quiet", disabled: Boolean(this.checking) })));
  }

  /** Thumbnails for the results that have a picture (POST /api/media/thumbs, as the data grid does). */
  pictures(cx) {
    const want = [];
    for (const side of [this.result?.staged, this.result?.baseline]) for (const x of side?.results ?? []) if (x.image && !this.thumbs.has(x.image)) want.push(x.image);
    if (!want.length) return;
    for (const s of want) this.thumbs.set(s, undefined);
    cx.spawn(async (cx) => {
      const r = await api("/api/media/thumbs", { body: { srcs: [...new Set(want)], dest: `${appHome()}/cache/thumbs`, size: THUMB }, scoped: false, timeout: 60 });
      for (const s of want) this.thumbs.set(s, r.ok ? r.data?.[s]?.file ?? null : null);
      cx.notify();
    });
  }

  // ---- the steps, as plain data: the page draws them, and agents read them (cst app context) ---------------------

  steps() {
    const st = this.staged(), on = this.running();
    const cand = this.pick(), champ = this.page.exps?.champion ?? null;
    const what = st?.model_version ? short(st.model_version) : null;
    const tried = on && Boolean(st?.tried || this.result);
    return [
      { n: 1, id: "stage", title: "Stage", done: on,
        text: on ? this.status.text : this.busy ?? (st?.status === "failed" ? this.status.text
          : cand ? `Run ${short(cand.pipeline)} as a live endpoint you can try, beside the baseline.`
          : champ ? "Run the baseline as a live endpoint you can try." : "Set a baseline or train a candidate first. Then you can stage it."),
        action: on ? null : cand ? "stage" : champ ? "stage-baseline" : null, label: on ? null : cand ? "Stage it" : champ ? "Stage the baseline" : null },
      { n: 2, id: "try", title: "Try it", done: tried,
        text: on ? `Type a query and see what ${what} returns${st?.arms?.baseline && st.serve !== "baseline" ? ", beside the baseline" : ""}.` : "After it is staged, type a query and see the results.",
        action: on ? "try" : null, label: null, ...(on ? {} : { waiting_on: "Stage a model version first." }) },
      this.abStepData(),
      this.launchStepData(),
    ];
  }

  /** Both versions are on the endpoint, so traffic can be split between them. */
  both() {
    const st = this.staged();
    return this.running() && Boolean(st?.arms?.baseline) && Boolean(st?.arms?.candidate) && st.how !== "image";
  }

  abStepData() {
    const r = this.ab, a = r?.ab, live = a?.status === "running";
    const done = Boolean(a && (r.enough || r.brought));
    let text;
    if (!a) text = this.both() ? "Split traffic between the baseline and the candidate, and see which one shoppers prefer." : "After a candidate is staged beside the baseline, split traffic between them.";
    else if (a.mode === "own") text = r.text;
    else if (live) text = `The candidate gets ${pct(a.split)} of traffic. ${r.primary?.text ?? ""}`;
    else text = `${a.status === "rolled_back" ? "Rolled back to the baseline." : "Ended."} ${r.primary?.text ?? ""}`;
    return { n: 3, id: "ab", title: "A/B test", done, text: text.trim(), action: this.both() && !live ? "ab-start" : null, label: this.both() && !live ? (a ? "Start another A/B test" : "Start an A/B test") : null,
      ...(a || this.both() ? {} : { waiting_on: "Stage a candidate beside the baseline first." }) };
  }

  /** What launch review would be sent: the test's candidate, else the staged candidate, else the baseline. */
  reviewOf() { return this.ab?.ab?.arms?.candidate ?? this.staged()?.arms?.candidate ?? this.page.exps?.champion ?? null; }

  launchStepData() {
    const of = this.reviewOf(), sent = Boolean((this.page.stages?.ship?.steps ?? []).find((x) => x.id === "launch-review")?.done);
    const failed = this.staged()?.check?.status === "fail";
    return { n: 4, id: "launch-review", title: "Launch review", done: sent,
      text: sent ? `${short(of)} was sent to launch review. Your own deployment process takes it from there.`
        : failed ? "Served features don't match training, so launch review will ask for a reason to go on."
        : "Check it against your launch criteria and pass it to your own deployment process. Captain deploys nothing itself.",
      action: of && !sent ? "handoff" : null, label: of && !sent ? "Send to launch review" : null, ...(of ? {} : { waiting_on: "Set a baseline or train a candidate first." }) };
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  mark(c, st) {
    return st.done
      ? div().id(`ship-done-${st.id}`).size(22).flex_none().flex().items_center().justify_center().rounded_full().bg(c.goodLight).text_color(c.good).child(svg(icon("check")).size(13))
      : div().size(22).flex_none().flex().items_center().justify_center().rounded_full().border_1().border_color(c.grey300).text_size(11.5).font_semibold().text_color(c.grey900).child(String(st.n));
  }

  row(c, st, action, below) {
    return v_flex().id(`ship-step-${st.id}`).w_full()
      .child(h_flex().p(12).gap(12).items_center().flex_wrap()
        .child(this.mark(c, st))
        .child(v_flex().flex_1().min_w(280).gap(1)
          .child(div().text_size(13).font_semibold().text_color(c.navy).child(st.title))
          .child(div().id(`ship-text-${st.id}`).text_size(12).line_height("17px").text_color(c.grey500).child(st.text)))
        .when(Boolean(action), (e) => e.child(h_flex().flex_none().gap(6).items_center().child(action))))
      .when(Boolean(below), (e) => e.child(below));
  }

  stageStep(c, st) {
    const s = this.staged(), on = this.running(), page = this.page;
    const cand = this.pick(), champ = page.exps?.champion ?? null;
    const servesBaseline = s?.serve === "baseline";
    const buttons = h_flex().flex_none().flex_wrap().gap(6).items_center();
    if (this.busy) buttons.child(div().id("ship-busy").text_size(12).text_color(c.grey500).child(`${this.busy}...`));
    else if (on) {
      if (cand && servesBaseline) buttons.child(Button(c, "ship-stage", "Stage the candidate", (_e, cx) => this.stage(cx, { candidate: cand.id }), { small: true, kind: "primary" }));
      if (champ && !servesBaseline) buttons.child(Button(c, "ship-restage-baseline", "Restage with the baseline", (_e, cx) => this.stage(cx, { baseline: true }), { small: true }));
      buttons.child(Button(c, "ship-stop", "Stop", (_e, cx) => { this.stopAsk = true; cx.notify(); }, { small: true }));
    } else if (st.action === "stage") buttons.child(Button(c, "ship-stage", "Stage it", (_e, cx) => this.stage(cx, { candidate: cand.id }), { small: true, kind: "primary" }));
    else if (st.action === "stage-baseline") buttons.child(Button(c, "ship-stage", "Stage the baseline", (_e, cx) => this.stage(cx, { baseline: true }), { small: true, kind: "primary" }));
    if (s && !this.busy) buttons.child(SendToAgent(c, "ship-send", { ref: `staging:${this.pid()}`, kind: "staging", label: `staging for ${page.project()?.name ?? this.pid()}` }));

    const lines = [];
    if (on && s?.cost?.text) lines.push(["cost", s.cost.text]);
    if (!on && !s) lines.push(["what", "Staging runs a model version as a live endpoint you can try before launch review. It runs in your own cloud, or on this computer."]);
    if (!on && s?.status === "stopped") lines.push(["stopped", `Last staged: ${short(s.model_version)}. ${this.status.text}`]);
    if (this.status?.warning) lines.push(["warning", this.status.warning]);
    const below = v_flex().pl(46).pr(12).pb(12).gap(6)
      .children(lines.map(([k, t]) => div().id(`ship-line-${k}`).text_size(12).line_height("17px").text_color(c.grey900).child(t)))
      .when(Boolean(this.note), (e) => e.child(div().id("ship-note").px(10).py(6).text_size(12).bg(this.note.tone === "fail" ? c.failLight : c.goodLight)
        .text_color(this.note.tone === "fail" ? c.fail : c.good).child(this.note.text)))
      .when(Boolean(this.problem), (e) => e.child(div().id("ship-problem").text_size(12).text_color(c.fail).child(this.problem)))
      .when(Boolean(this.confirm), (e) => e.child(this.confirmBox(c)))
      .when(Boolean(this.stopAsk) && on, (e) => e.child(this.stopBox(c)))
      .when(Boolean(s), (e) => e.child(this.detailsBox(c, s)));
    return this.row(c, st, buttons, below);
  }

  /** Before a cloud deploy: where it runs, what it costs, that it keeps billing. Nothing starts without a yes. */
  confirmBox(c) {
    const p = this.confirm.plan;
    return v_flex().id("ship-confirm").p(10).gap(6).bg(c.warnLight).border_1().border_color(c.warn)
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(`Start ${short(p.model_version)} in your own cloud?`))
      .child(div().id("ship-confirm-cost").text_size(12).line_height("17px").text_color(c.grey900).child(p.cost?.text ?? "This starts a cloud endpoint that bills until you stop it."))
      .children([...(p.warnings ?? []), p.preflight?.note].filter(Boolean).map((w, i) => div().id(`ship-confirm-warning-${i}`).text_size(12).line_height("17px").font_medium().text_color(c.fail).child(w)))
      .child(div().text_size(12).line_height("17px").text_color(c.grey900).child(`Captain stops it after ${span((this.staged()?.idle_minutes ?? 240) * 60)} with no requests, as long as Captain or the endpoint itself can reach your cloud.`))
      .child(h_flex().gap(6)
        .child(Button(c, "ship-confirm-yes", "Start it", (_e, cx) => this.stage(cx, this.confirm.body, true), { small: true, kind: "primary" }))
        .child(Button(c, "ship-confirm-no", "Not now", (_e, cx) => { this.confirm = null; cx.notify(); }, { small: true })));
  }

  stopBox(c) {
    return h_flex().id("ship-stop-ask").p(10).gap(10).items_center().bg(c.surface2).border_1().border_color(c.rule)
      .child(div().flex_1().min_w(0).text_size(12).text_color(c.grey900).child("Stop staging? The endpoint goes away and its log is kept."))
      .child(Button(c, "ship-stop-yes", "Stop it", (_e, cx) => this.stop(cx), { small: true, kind: "primary" }))
      .child(Button(c, "ship-stop-no", "Keep it running", (_e, cx) => { this.stopAsk = false; cx.notify(); }, { small: true }));
  }

  /** What an engineer asks for: the endpoint, where it runs, the instance, the log, and every resource Captain made. */
  detailsBox(c, s) {
    const ep = s.endpoint ?? {};
    const rows = [
      ["Model version", s.model_version],
      s.arms?.baseline && s.serve !== "baseline" ? ["Baseline beside it", s.arms.baseline] : null,
      ["Runs on", s.kind === "local" ? "This computer (only reachable from here)" : `${s.target}${ep.region ? `, ${ep.region}` : ""}`],
      ep.url || ep.name ? ["Endpoint", ep.url ?? ep.name] : null,
      s.cost?.instance_type ? ["Instance", `${s.cost.instance_count} x ${s.cost.instance_type}`] : null,
      s.started_at ? ["Started", `${String(s.started_at).slice(0, 16).replace("T", " ")} UTC by ${s.started_by ?? "someone"}`] : null,
      ["Stops by itself after", `${span((s.idle_minutes ?? 240) * 60)} with no requests`],
      s.needs_key ? ["Access", "Needs its key, kept in this computer's keychain"] : null,
      ep.log ? ["Log", ep.log] : null,
      ...(s.resources ?? []).map((r) => [`Created: ${r.type}`, r.name]),
      s.error ? ["Problem", s.error] : null,
    ].filter(Boolean);
    return v_flex().id("ship-details").gap(4)
      .child(h_flex().id("ship-details-toggle").gap(4).items_center().cursor_pointer().text_size(12).text_color(c.blue)
        .on_click((_e, cx) => { this.details = !this.details; cx.notify(); })
        .child(svg(icon(this.details ? "chevron-down" : "chevron-right")).size(11).flex_none())
        .child("Details"))
      .when(this.details, (e) => e.child(v_flex().id("ship-details-rows").p(10).gap(3).bg(c.surface2).border_1().border_color(c.rule)
        .children(rows.map(([k, v], i) => h_flex().id(`ship-detail-${i}`).gap(10).items_start().text_size(12)
          .child(div().w(150).flex_none().text_color(c.grey500).child(k))
          .child(div().flex_1().min_w(0).font_family(MONO).text_size(11.5).text_color(c.navy).child(String(v)))))));
  }

  resultCard(c, x, id) {
    const f = x.image ? this.thumbs.get(x.image) : null;
    const pic = f ? image(`cache/thumbs/${f}`).w(40).h(40).flex_none() : div().w(40).h(40).flex_none().bg(c.surface2);
    return h_flex().id(id).gap(8).py(5).items_center().border_t(1).border_color(c.rule)
      .child(div().w(18).flex_none().text_size(11.5).text_color(c.grey500).child(String(x.position)))
      .when(Boolean(x.image), (e) => e.child(pic))
      .child(v_flex().flex_1().min_w(0)
        .child(div().truncate().text_size(12.5).text_color(c.navy).child(x.title))
        .when(x.price != null, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`$${Number(x.price).toFixed(2)}`))));
  }

  side(c, side, name, id) {
    return v_flex().id(id).flex_1().min_w(240).p(10).gap(2).bg(c.surface).border_1().border_color(c.rule)
      .child(h_flex().gap(6).items_center()
        .child(div().text_size(12.5).font_semibold().child(name))
        .child(Badge(c, short(side.model_version)))
        .child(div().flex_1())
        .child(div().id(`${id}-ms`).text_size(11.5).text_color(c.grey500).child(`${Number(side.ms).toFixed(side.ms < 10 ? 1 : 0)} ms`)))
      .children(side.results.map((x, i) => this.resultCard(c, x, `${id}-r${i}`)))
      .when(!side.results.length, (e) => e.child(div().py(8).text_size(12).text_color(c.grey500).child("Nothing found for this query.")));
  }

  tryStep(c, st) {
    if (!this.running()) return this.row(c, st, null, null);
    const r = this.result, s = this.staged();
    const name = s?.serve === "baseline" ? "Baseline" : "Candidate";
    const below = v_flex().pl(46).pr(12).pb(12).gap(8)
      .child(h_flex().gap(8)
        .child(div().id("ship-try-box").flex_1().h(32).flex().items_center().px(10).bg(c.surface).border_1().border_color(c.grey300).cursor_text()
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.box).w_full().text_size(13).text_color(c.navy)))
        .child(Button(c, "ship-try-go", this.asking ? "Asking..." : "Try it", (_e, cx) => this.ask(cx), { kind: "primary", disabled: this.asking })))
      .when(Boolean(this.askError), (e) => e.child(div().id("ship-try-error").text_size(12).text_color(c.fail).child(this.askError)))
      .when(Boolean(r), (e) => e
        .child(h_flex().id("ship-try-results").flex_wrap().items_start().gap(10)
          .child(this.side(c, r.staged, name, "ship-try-staged"))
          .when(Boolean(r.baseline), (x) => x.child(this.side(c, r.baseline, "Baseline", "ship-try-baseline"))))
        .child(div().id("ship-try-summary").text_size(12).text_color(c.grey500).child(
          (r.baseline ? (r.same_order ? "Both return the same results in the same order for this query. " : `${r.moved} of the top ${r.staged.results.length} differ from the baseline. `) : "")
          + "Queries typed here are test traffic. They never count as shoppers.")))
      .when(Boolean(this.checkLine(c)), (e) => e.child(this.checkLine(c)));
    return this.row(c, st, null, below);
  }

  pick1(c, id, label, on, onPick) {
    return div().id(id).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
      .on_click((_e, cx) => { onPick(); cx.notify(); }).child(label);
  }

  /** Before a test starts: the candidate's share, the metric that decides it, and who splits the traffic. */
  setupBox(c) {
    const f = this.setup, can = this.ab?.can_measure ?? ["clicks", "purchases"];
    const line = (label, kids) => v_flex().gap(4).child(div().text_size(12).font_semibold().text_color(c.navy).child(label)).child(h_flex().flex_wrap().gap(6).children(kids));
    return v_flex().id("ship-ab-setup").p(10).gap(10).bg(c.surface2).border_1().border_color(c.rule)
      .child(line("Who splits the traffic", [
        this.pick1(c, "ship-ab-mode-captain", "Captain's staging endpoint", f.mode === "captain", () => { f.mode = "captain"; }),
        this.pick1(c, "ship-ab-mode-own", "My own A/B system", f.mode === "own", () => { f.mode = "own"; })]))
      .when(f.mode === "captain", (e) => e.child(line("The candidate's share of traffic", [0.01, 0.05, 0.25, 0.5].map((x) => this.pick1(c, `ship-ab-split-${Math.round(x * 100)}`, pct(x), f.split === x, () => { f.split = x; })))))
      .child(line("What decides it", can.map((m) => this.pick1(c, `ship-ab-primary-${m}`, METRICS[m], f.primary === m, () => { f.primary = m; }))))
      .when(f.mode === "captain", (e) => e.child(line("Guardrails: click one to change it", [...can.filter((m) => m !== f.primary), "latency"].map((m) => {
        const must = (f.must ??= { latency: true })[m];
        return this.pick1(c, `ship-ab-guard-${m}`, `${m === "latency" ? "Response time" : METRICS[m]}: ${must ? "must not get worse" : "watch"}`, Boolean(must), () => { f.must[m] = !must; });
      }))))
      .child(div().id("ship-ab-setup-note").text_size(12).line_height("17px").text_color(c.grey900).child(f.mode === "captain"
        ? "If a guardrail marked must not get worse clearly drops, Captain pauses the ramp and says why. One marked watch is shown and never blocks. A shopper always sees the same version."
        : "Captain gives you an address for each version on the endpoint. Split traffic in your system, then bring the readout in here."))
      .child(h_flex().gap(6)
        .child(Button(c, "ship-ab-start-go", "Start the A/B test", (_e, cx) => this.abDo(cx, "start", { split: f.split, primary: f.primary, mode: f.mode,
          guardrails: [...can.filter((m) => m !== f.primary), "latency"].map((m) => `${m}:${(f.must ?? { latency: true })[m] ? "must" : "watch"}`) },
          f.mode === "captain" ? `The A/B test is running: the candidate gets ${pct(f.split)} of traffic.` : "The A/B test is recorded. Split the traffic in your own system."), { small: true, kind: "primary" }))
        .child(Button(c, "ship-ab-start-no", "Not now", (_e, cx) => { this.setup = null; cx.notify(); }, { small: true })));
  }

  /** The readout: where the numbers came from, each arm, the primary metric with its interval, and the guardrails. */
  readoutBox(c) {
    const r = this.ab, a = r.ab;
    const tone = r.evidence === "REAL_TRAFFIC" ? "good" : "warn";
    const arm = (name, id) => {
      const x = r.arms?.[name];
      const v = r.primary?.[name];
      return v_flex().id(id).flex_1().min_w(200).p(10).gap(2).bg(c.surface).border_1().border_color(c.rule)
        .child(h_flex().gap(6).items_center().child(div().text_size(12.5).font_semibold().child(name === "baseline" ? "Baseline" : "Candidate")).child(Badge(c, short(x?.model_version))))
        .child(div().id(`${id}-sessions`).text_size(12).text_color(c.grey900).child(`${Number(x?.sessions ?? 0).toLocaleString("en-US")} sessions`))
        .when(v != null, (e) => e.child(div().id(`${id}-value`).text_size(16).font_semibold().text_color(c.navy).child(r.primary.metric === "revenue" ? `$${Number(v).toFixed(2)}` : `${(Number(v) * 100).toFixed(1)}%`))
          .child(div().text_size(11.5).text_color(c.grey500).child(METRICS[r.primary.metric] ?? r.primary.metric)));
    };
    return v_flex().id("ship-ab-readout").gap(8)
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(Badge(c, r.label ?? "No traffic yet", tone))
        .when(r.evidence !== "REAL_TRAFFIC", (e) => e.child(div().id("ship-ab-evidence").text_size(12).text_color(c.warn).child(r.evidence === "SIMULATED"
          ? "SIMULATED: these shoppers are generated. Nothing here says what real shoppers will do."
          : "Test traffic: these requests are not shoppers."))))
      .child(h_flex().flex_wrap().items_start().gap(10).child(arm("baseline", "ship-ab-baseline")).child(arm("candidate", "ship-ab-candidate")))
      .child(div().id("ship-ab-primary").text_size(12.5).line_height("18px").text_color(r.enough ? c.navy : c.grey900).child(r.primary?.text ?? r.text))
      .children((r.guardrails ?? []).map((g, i) => h_flex().id(`ship-ab-guard-${i}`).gap(6).items_center()
        .child(svg(icon(g.ok ? "circle-check" : "triangle-alert")).size(12).flex_none().text_color(g.ok ? c.good : c.fail))
        .child(div().text_size(12).text_color(g.ok ? c.grey900 : c.fail).child(g.text))))
      .when(Boolean(r.hard_stop), (e) => e.child(div().id("ship-ab-paused").px(10).py(6).text_size(12.5).bg(c.failLight).text_color(c.fail).child(`${r.hard_stop} Roll back, or ramp down.`)))
      .when((r.segments ?? []).length > 0, (e) => e.child(v_flex().id("ship-ab-segments").gap(3)
        .child(div().text_size(12).font_semibold().text_color(c.navy).child("By kind of query and shopper"))
        .children(r.segments.map((x) => div().id(`ship-ab-segment-${x.segment}`).text_size(12).line_height("17px")
          .text_color(x.verdict === "candidate behind" ? c.fail : x.enough ? c.grey900 : c.grey500).child(x.text)))
        .when(Boolean(r.segment_warning), (y) => y.child(div().id("ship-ab-segment-warning").text_size(12.5).font_medium().text_color(c.fail).child(r.segment_warning)))))
      .when(Boolean(r.srm), (e) => e.child(div().id("ship-ab-srm").text_size(12).text_color(r.srm.ok ? c.grey500 : c.fail).child(r.srm.text)))
      .when(a.status === "running", (e) => e.child(h_flex().id("ship-ab-actions").flex_wrap().gap(6).items_center()
        .child(div().text_size(12).font_semibold().child("Ramp"))
        .children(RAMP.map((x) => this.pick1(c, `ship-ab-ramp-${Math.round(x * 100)}`, pct(x), Math.abs((a.split ?? 0) - x) < 1e-9, () => { if (Math.abs((a.split ?? 0) - x) > 1e-9 && !(r.hard_stop && x > (a.split ?? 0))) this.ask2 = { kind: "ramp", split: x }; })))
        .child(Button(c, "ship-ab-rollback", "Roll back", (_e, cx) => { this.ask2 = { kind: "rollback" }; cx.notify(); }, { small: true }))
        .child(Button(c, "ship-ab-simulate", "Send simulated shoppers at staging", (_e, cx) => this.simulate(cx), { small: true, kind: "quiet", disabled: Boolean(this.busy) }))
        .child(Button(c, "ship-ab-refresh", "Read again", (_e, cx) => this.load(cx), { small: true, kind: "quiet" }))));
  }

  /** A ramp or a rollback asks first: one click, one confirmation. */
  askBox(c) {
    const k = this.ask2, a = this.ab?.ab;
    const text = k.kind === "ramp" ? `Give the candidate ${pct(k.split)} of traffic? It gets ${pct(a?.split ?? 0)} now.` : "Roll back? Every shopper gets the baseline again, and this test ends.";
    return h_flex().id("ship-ab-ask").p(10).gap(10).items_center().bg(c.surface2).border_1().border_color(c.rule)
      .child(div().flex_1().min_w(0).text_size(12).text_color(c.grey900).child(text))
      .child(k.kind === "ramp"
        ? Button(c, "ship-ab-ask-yes", `Ramp to ${pct(k.split)}`, (_e, cx) => this.abDo(cx, "ramp", { split: k.split }, `The candidate now gets ${pct(k.split)} of traffic.`), { small: true, kind: "primary" })
        : Button(c, "ship-ab-ask-yes", "Roll back", (_e, cx) => this.abDo(cx, "rollback", {}, "Rolled back. Every shopper gets the baseline again."), { small: true, kind: "primary" }))
      .child(Button(c, "ship-ab-ask-no", "Not now", (_e, cx) => { this.ask2 = null; cx.notify(); }, { small: true }));
  }

  /** A test the team runs in its own system: where each version answers, and the readout they bring back. */
  ownBox(c) {
    const r = this.ab, adr = r.addresses ?? r.ab?.addresses ?? {};
    return v_flex().id("ship-ab-own").gap(6)
      .children(Object.entries(adr).map(([name, x]) => h_flex().id(`ship-ab-address-${name}`).gap(10).text_size(12)
        .child(div().w(80).flex_none().text_color(c.grey500).child(name === "baseline" ? "Baseline" : "Candidate"))
        .child(div().flex_1().min_w(0).font_family(MONO).text_size(11.5).child(x.url ?? `${x.endpoint}${x.region ? ` in ${x.region}` : ""}, arm ${name}`))))
      .when(Boolean(r.brought), (e) => e.child(h_flex().gap(8).items_center().child(Badge(c, r.label, r.evidence === "REAL_TRAFFIC" ? "good" : "warn"))
        .child(div().id("ship-ab-brought").text_size(12.5).text_color(c.navy).child(r.brought.text))))
      .child(h_flex().gap(6)
        .child(Button(c, "ship-ab-bring", r.brought ? "Bring in a newer readout" : "Bring in the readout", () => form("ab-result", { project: this.pid(), traffic: "real" }), { small: true, kind: r.brought ? "secondary" : "primary" }))
        .when(r.ab?.status === "running", (e) => e.child(Button(c, "ship-ab-end", "End the test", (_e, cx) => this.abDo(cx, "stop", {}, "The A/B test is ended."), { small: true }))));
  }

  abStep(c, st) {
    const r = this.ab, a = r?.ab;
    const btn = st.action === "ab-start" && !this.setup && !this.busy
      ? Button(c, "ship-ab-start", st.label, (_e, cx) => { this.setup = { split: 0.05, primary: (r?.can_measure ?? []).includes("purchases") ? "purchases" : "clicks", mode: "captain", must: { latency: true } }; cx.notify(); }, { small: true, kind: a ? "secondary" : "primary" }) : null;
    const body = [];
    if (this.setup) body.push(this.setupBox(c));
    if (a && a.mode === "own") body.push(this.ownBox(c));
    else if (a && r.arms) body.push(this.readoutBox(c));
    if (this.ask2 && a?.status === "running") body.push(this.askBox(c));
    const below = body.length ? v_flex().pl(46).pr(12).pb(12).gap(8).children(body) : null;
    return this.row(c, st, btn, below);
  }

  launchStep(c, st) {
    const of = this.reviewOf();
    const btn = h_flex().flex_none().gap(6).items_center()
      .when(st.action === "handoff", (e) => e.child(Button(c, "ship-handoff", "Send to launch review", () => form("handoff", { pipeline: of }), { small: true, kind: this.ab?.enough ? "primary" : "secondary" })))
      .child(Button(c, "ship-open-handoff", "Open Launch review", () => this.page.goTo?.("handoff"), { small: true, kind: "quiet" }));
    return this.row(c, st, btn, null);
  }

  render(c) {
    const [stage, tryIt, ab, launch] = this.steps();
    return v_flex().id("ship-path").w_full().bg(c.surface).border_1().border_color(c.rule)
      .child(this.stageStep(c, stage))
      .child(div().border_t(1).border_color(c.rule).child(this.tryStep(c, tryIt)))
      .child(div().border_t(1).border_color(c.rule).child(this.abStep(c, ab)))
      .child(div().border_t(1).border_color(c.rule).child(this.launchStep(c, launch)));
  }
}
