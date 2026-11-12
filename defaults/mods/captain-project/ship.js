// CAPTAIN: the Ship part of a project's page as a path a person walks: 1 Stage, 2 Try it, 3 Connect your site,
// 4 A/B test, 5 Launch review. An A/B test means nothing until a site is calling the endpoint, so step 3 comes first:
// where it runs and who can reach it, the code to call it, a live count of real requests, and shadow mode
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
import { exec, appHome, openOverlay, run } from "herdr";
import { MONO } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Badge, Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { SendToAgent } from "./kit/context.js";
import { costState, refreshEstimate, CostLine, costSlice } from "./kit/cost.js";

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
    this.cloudCost = costState(); // what the same endpoint would cost in the connected cloud
    this.places = null;    // {cloud: the connected login or null, targets: compute targets that can serve}
    this.tab = "try";      // the deployment page's open tab
    this.sheet = false;    // the "what serving in your cloud needs" sheet
    this.cost = costState(); // what the staged endpoint costs to keep up (POST /api/costs/estimate, kit/cost.js)
    this.conn = null;      // GET /staging/connect: the address, the snippets, how many real requests arrived
    this.shadow = null;    // GET /shadow: what the candidate would change, with no shopper seeing it
    this.opts = null;      // GET /ab/options: has a site sent anything, and what each share means in sessions a day
    this.diff = null;      // GET /staging/differ: the queries where the two versions differ most
    this.code = null;      // which snippet is open
    this.whyOpen = new Set(); // results whose "Why is this here?" is open
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
      if (r.ok) {
        this.ab = r.data;
        // The readout just crossed "enough": the page's own steps and Next line are one read behind. Read again, once.
        const known = (this.page.stages?.ship?.steps ?? []).find((x) => x.id === "ab")?.done;
        const key = `${r.data?.ab?.id}:${Boolean(r.data?.enough)}`;
        if (r.data?.enough && known === false && this.abTold !== key) { this.abTold = key; this.page.reload?.(cx); }
      }
      cx.notify();
    });
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(pid)}/staging`, { scoped: false });
      if (this.pid() !== pid) return;
      if (r.ok) { this.status = r.data; this.problem = null; this.price(cx); this.settle(); this.around(cx); } else if (!r.starting) this.problem = plainError(r.error, "Captain couldn't read staging.");
      cx.notify();
    });
  }

  /** What sits around a running endpoint: who is calling it, shadow mode, what the A/B form may offer, and (once
   *  per staged version) the queries where the two versions differ. Nothing is asked while nothing runs. */
  around(cx) {
    const pid = this.pid(), s = this.staged();
    if (!this.running()) { this.conn = null; this.opts = null; this.diff = null; this.diffFor = null; return; }
    const get = (path, set) => cx.spawn(async (cx) => { const r = await api(`/api/projects/${enc(pid)}${path}`, { scoped: false, timeout: 60 }); if (this.pid() === pid && r.ok) { set(r.data); cx.notify(); } });
    get("/staging/connect", (d) => {
      this.conn = d;
      // The first real request just arrived: the page's own steps and Next line are one read behind. Read again.
      const known = (this.page.stages?.ship?.steps ?? []).find((x) => x.id === "connect")?.done;
      if (d.connected && known === false && this.toldFor !== s.id) { this.toldFor = s.id; this.page.reload?.(cx); }
    });
    if (this.both()) {
      get("/shadow", (d) => { this.shadow = d; });
      get("/ab/options", (d) => { this.opts = d; });
      if (this.diffFor !== s.id) { this.diffFor = s.id; this.diff = null; get("/staging/differ", (d) => { this.diff = d; }); }
    }
  }

  /** Where a model could run besides this computer: the connected cloud login, and the compute targets there that
   *  can serve. Asked once per project. The estimate for the first of them comes from the shared estimator. */
  loadPlaces(cx) {
    const pid = this.pid();
    if (!pid || this.placesFor === pid) return;
    this.placesFor = pid;
    cx.spawn(async (cx) => {
      const [t, cl, su] = await Promise.all([api("/api/compute", { scoped: false }), api("/api/cloud/summary", { scoped: false }), api("/api/serving/setup", { scoped: false })]);
      if (this.pid() !== pid) return;
      this.serving = su.ok ? su.data : null; // what Captain set up in the cloud account, so it can be listed and removed
      const targets = (t.ok && Array.isArray(t.data) ? t.data : []).filter((x) => x.kind !== "local" && (x.purposes ?? []).includes("serve") && x.kind === "aws-sagemaker");
      this.places = { cloud: cl.ok ? cl.data?.active ?? null : null, targets };
      const first = targets[0];
      // Would it fit on this computer? The plan answers from the catalog's size and what is free right now.
      const cand = this.pick();
      if (cand || this.page.exps?.champion) {
        const fp = await api(`/api/projects/${enc(pid)}/stage`, { body: { dry_run: true, target: "local", ...(cand ? { candidate: cand.id } : { baseline: true }) }, scoped: false });
        this.fit = fp.ok ? fp.data?.local_fit ?? null : null;
      }
      if (first) await refreshEstimate(this.cloudCost, { kind: "staging", where: first.name, size: { instances: Number(first.config?.instance_count ?? 1), instance_type: first.config?.instance_type ?? "ml.m5.large" } });
      cx.notify();
    });
  }

  /** The cloud's name as a person says it. */
  cloudName() { const p = this.places?.cloud?.provider ?? (this.places?.targets?.length ? "aws" : null); return { aws: "AWS", gcp: "Google Cloud", azure: "Azure" }[p] ?? null; }

  /** Where this project was last staged, so the same choice is offered first next time. */
  lastWhere() { try { return localStorage.getItem(`stage-where:${this.pid()}`) || null; } catch { return null; } }
  stageAt(cx, where) {
    try { localStorage.setItem(`stage-where:${this.pid()}`, where === "local" ? "local" : "cloud"); } catch {}
    const cand = this.pick();
    this.stage(cx, { ...(cand ? { candidate: cand.id } : { baseline: true }), target: where });
  }

  copy(cx, text, said) { cx.write_to_clipboard?.(text); this.note = { tone: "good", text: said }; cx.notify(); }

  /** One small write (shadow on or off, a test request, a decision), then read everything again. */
  act(cx, path, body, done, after) {
    if (this.busy) return;
    this.cx = cx; this.note = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}${path}`, { body: body ?? {}, scoped: false, timeout: 60 });
      this.note = r.ok ? (done ? { tone: "good", text: typeof done === "function" ? done(r.data) : done } : null) : { tone: "fail", text: plainError(r.error, "That didn't work. Try again.") };
      if (r.ok) after?.(r.data, cx);
      this.load(cx);
      this.page.reload?.(cx);
    });
  }

  /** Copy without showing: the endpoint key goes from the services straight to the clipboard. */
  copyKey(cx) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/key`, { scoped: false });
      if (r.ok && r.data?.key) { cx.write_to_clipboard?.(r.data.key); this.note = { tone: "good", text: "The key is on your clipboard. Give it to your site as CAPTAIN_STAGING_KEY. It is not shown anywhere." }; }
      else this.note = { tone: "fail", text: plainError(r.error, "Captain couldn't read the key on this computer.") };
      cx.notify();
    });
  }

  /** Put a query in the Try it box and ask it (from a list of queries that differ). */
  tryQuery(cx, q) { this.tab = "try"; this.box.set_value(q); this.ask(cx); }

  /** The estimate for where it runs (or is about to run). Asks the services only when the question changed. */
  price(cx, where, size) {
    const s = this.staged();
    const at = where ?? (s?.status === "running" ? (s.kind === "local" ? "local" : s.target) : null);
    if (!at) return;
    const sz = size ?? (s?.cost?.instance_type ? { instances: s.cost.instance_count ?? 1, instance_type: s.cost.instance_type } : {});
    cx.spawn(async (cx) => { if (await refreshEstimate(this.cost, { kind: "staging", where: at, size: sz })) cx.notify(); });
  }

  /** For agents: where it runs and the estimate. */
  costSlice() { return this.running() ? costSlice({ ...this.cost, where: this.staged().kind === "local" ? "local" : this.staged().target }) : null; }

  /** From the project's own answer (GET /stages): ask for the details only when something is staged or tested. */
  sync(cx, ship) {
    this.cx = cx;
    this.loadPlaces(cx);
    if (ship?.staging || ship?.ab) return this.load(cx);
    const pid = this.pid();
    if (pid !== this.shown) { this.shown = pid; this.result = null; this.note = null; this.confirm = null; this.askError = null; this.setup = null; this.ask2 = null; }
    this.status = { status: "none", staged: null, text: "Nothing is staged." };
    this.ab = { status: "none", experiment: null, can_measure: [] };
    this.problem = null;
  }

  /** A progress label describes work in flight, never a finished step. When staging itself says the work is over
   *  (a new endpoint is running, or it is stopped), the label goes, whatever the job's own answer is doing. */
  settle() {
    const b = this.busyFor, st = this.status?.status, id = this.staged()?.id ?? null;
    if (!b) return;
    if ((b.what === "stage" && st === "running" && id !== b.was) || (b.what === "stop" && st === "stopped")) { this.busy = null; this.busyFor = null; this.settled = b.what; }
  }

  /** Redraw the countdown, and look again while something is running, starting, or being staged or stopped. */
  tick(cx) {
    const s = this.status?.status;
    if (this.pid() && (this.busyFor || (!this.busy && (s === "running" || s === "starting")))) { this.load(cx); if (this.page.deployOpen) this.loadTab(cx); }
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
      // The services no longer know the job (they were restarted), or staging itself already said the work is
      // over: stop waiting. What is true is read from the project, not from a job that can't answer.
      if (r.status === 404 || (i > 0 && !this.busy)) return { ok: true, result: null };
      if (r.ok && r.data?.status === "done") return { ok: true, result: r.data.result };
      if (r.ok && r.data?.status === "failed") return { ok: false, error: plainError(r.data.message, "It didn't finish.") };
      if (r.ok && r.data?.message && r.data.status === "running" && this.busy) { this.busy = r.data.message; this.cx?.notify(); }
      try { await exec("sleep", ["0.5"]); } catch { break; }
    }
    return { ok: false, error: "It's still starting. Look again in a minute." };
  }

  /** Start staging. `body` is {candidate} or {baseline: true}; `confirmed` after the person agreed to the cost. */
  stage(cx, body, confirmed = false) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = "Getting it ready"; this.busyStep = "stage"; this.note = null; this.confirm = null;
    this.busyFor = { what: "stage", was: this.running() ? this.staged().id : null }; this.settled = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/stage`, { body: { ...body, ...(confirmed ? { confirm: true } : {}) }, scoped: false });
      if (r.status === 409 && r.data?.needs_confirm) {
        const p = r.data.plan;
        this.busy = null; this.busyFor = null; this.confirm = { plan: p, body };
        this.price(cx, p.target, { instances: p.cost?.instance_count ?? 1, instance_type: p.cost?.instance_type });
        cx.notify(); return;
      }
      if (!r.ok) { this.busy = null; this.busyFor = null; this.note = { tone: "fail", text: plainError(r.error, "Staging didn't start. Try again.") }; cx.notify(); return; }
      const cloud = r.data?.plan?.kind && r.data.plan.kind !== "local";
      const done = await this.wait(r.data.job, cloud ? 30 : 3);
      this.busy = null; this.busyFor = null; this.result = null;
      // Staging itself may have said "running" before the job answered: the outcome is the same, so say it.
      const mv = done.result?.model_version ?? (this.settled === "stage" ? this.staged()?.model_version : null);
      this.note = !done.ok ? { tone: "fail", text: done.error } : mv ? { tone: "good", text: `${short(mv)} is staged. Try it below.` } : null;
      this.settled = null;
      this.load(cx);
      this.page.reload?.(cx);
    });
  }

  stop(cx) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = "Stopping it"; this.busyStep = "stage"; this.note = null; this.stopAsk = false;
    this.busyFor = { what: "stop" }; this.settled = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/unstage`, { body: {}, scoped: false });
      const done = r.ok ? await this.wait(r.data.job, 5) : { ok: false, error: plainError(r.error, "It didn't stop. Try again.") };
      this.busy = null; this.busyFor = null; this.result = null;
      this.note = done.ok && done.result?.status === "stopped" ? { tone: "good", text: "Staging is stopped. Nothing is left running." }
        : done.ok && !done.result ? (this.settled === "stop" ? { tone: "good", text: "Staging is stopped. Nothing is left running." } : null)
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
    this.busyStep = "ab";
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
    this.busy = "Sending simulated shoppers"; this.busyStep = "ab"; this.note = null;
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
      this.connectStepData(),
      this.abStepData(),
      this.launchStepData(),
    ];
  }

  /** Both versions are on the endpoint, so traffic can be split between them. */
  both() {
    const st = this.staged();
    return this.running() && Boolean(st?.arms?.baseline) && Boolean(st?.arms?.candidate) && st.how !== "image";
  }

  connectStepData() {
    const on = this.running(), k = this.conn;
    const text = !on ? "After it is staged, point your site at it and watch the first request arrive."
      : !k ? "Looking for requests from your site..."
      : k.connected ? k.live
      : k.blocked ?? "Waiting for the first request. Copy a snippet below into your site.";
    return { n: 3, id: "connect", title: "Connect your site", done: Boolean(on && k?.connected), text, action: on && !k?.connected ? "connect" : null, label: null,
      ...(on ? {} : { waiting_on: "Stage a model version first." }) };
  }

  abStepData() {
    const r = this.ab, a = r?.ab, live = a?.status === "running", o = this.opts;
    const done = Boolean(a && (r.enough || r.brought));
    let text;
    if (a?.status === "rolled_back" && r.rolled_back) text = r.rolled_back.text;
    else if (!a || !live) text = !this.both() ? "After a candidate is staged beside the baseline, split traffic between them."
      : o && !o.ready ? o.text
      : a ? `${a.status === "rolled_back" ? "Rolled back to the baseline." : "The last test ended."} Start another when you're ready.`
      : "Your site is sending requests. Split them between the baseline and the candidate, and see which one shoppers prefer.";
    else if (a.mode === "own") text = r.text;
    // The result itself is in the readout below, once: here only what is running.
    else text = `${a.practice ? "Practice on simulated shoppers. " : ""}The candidate gets ${pct(a.split)} of traffic.${r.enough ? "" : " Not enough data yet."}`;
    const can = this.both() && !live;
    return { n: 4, id: "ab", title: "A/B test", done, text: String(text ?? "").trim(), action: can ? (o?.ready ? "ab-start" : "ab-practice") : null,
      label: can ? (o?.ready ? (a ? "Start another A/B test" : "Start an A/B test") : "Practice with simulated shoppers") : null,
      ...(a || this.both() ? {} : { waiting_on: "Stage a candidate beside the baseline first." }) };
  }

  /** What launch review would be sent: the test's candidate, else the staged candidate, else the baseline. */
  reviewOf() { return this.ab?.ab?.arms?.candidate ?? this.staged()?.arms?.candidate ?? this.page.exps?.champion ?? null; }

  launchStepData() {
    const of = this.reviewOf(), sent = Boolean((this.page.stages?.ship?.steps ?? []).find((x) => x.id === "launch-review")?.done);
    const failed = this.staged()?.check?.status === "fail";
    return { n: 5, id: "launch-review", title: "Launch review", done: sent,
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
    // A progress label shows on the step that started the work, and nowhere else.
    if (this.busy && this.busyStep !== "ab") buttons.child(div().id("ship-busy").text_size(12).text_color(c.grey500).child(`${this.busy}...`));
    else if (this.busy) { /* the A/B step is busy: this step offers nothing meanwhile */ }
    else if (on) {
      if (cand && servesBaseline) buttons.child(Button(c, "ship-stage", "Stage the candidate", (_e, cx) => this.stage(cx, { candidate: cand.id }), { small: true, kind: "primary" }));
      if (champ && !servesBaseline) buttons.child(Button(c, "ship-restage-baseline", "Restage with the baseline", (_e, cx) => this.stage(cx, { baseline: true }), { small: true }));
      buttons.child(Button(c, "ship-stop", "Stop", (_e, cx) => { this.stopAsk = true; cx.notify(); }, { small: true }));
    } else if (st.action === "stage") buttons.child(Button(c, "ship-stage", "Stage it", (_e, cx) => this.stage(cx, { candidate: cand.id }), { small: true, kind: "primary" }));
    else if (st.action === "stage-baseline") buttons.child(Button(c, "ship-stage", "Stage the baseline", (_e, cx) => this.stage(cx, { baseline: true }), { small: true, kind: "primary" }));
    if (s && !this.busy) buttons.child(SendToAgent(c, "ship-send", { ref: `staging:${this.pid()}`, kind: "staging", label: `staging for ${page.project()?.name ?? this.pid()}` }));

    const lines = [];
    const costLine = on && !this.confirm ? v_flex().id("ship-line-cost").gap(2)
      .child(CostLine(c, "ship-cost", this.cost.estimate ?? s?.cost?.estimate ?? { text: s?.cost?.text ?? "", basis: [] }, this.cost, { onToggle: (_e, cx) => { this.cost.open = !this.cost.open; cx.notify(); } }))
      .when(s?.kind !== "local", (e) => e.child(div().id("ship-cost-billing").text_size(12).text_color(c.grey900).child("This keeps billing until you stop it."))) : null;
    if (!on && s?.spent?.text) lines.push(["spent", s.spent.text]);
    if (!on && !s) lines.push(["what", "Staging runs a model version as a live endpoint you can try before launch review. It runs in your own cloud, or on this computer."]);
    if (!on && s?.status === "stopped") lines.push(["stopped", `Last staged: ${short(s.model_version)}. ${this.status.text}`]);
    if (this.status?.warning) lines.push(["warning", this.status.warning]);
    const below = v_flex().pl(46).pr(12).pb(12).gap(6)
      .when(Boolean(costLine), (e) => e.child(costLine))
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
      .child(div().id("ship-confirm-where").text_size(12).text_color(c.grey900).child(`${p.cost?.instance_count ?? 1} x ${p.cost?.instance_type ?? "an instance"} on ${p.target}, in your own cloud account.`))
      .child(CostLine(c, "ship-confirm-cost", this.cost.estimate ?? p.cost?.estimate ?? { text: p.cost?.text ?? "", basis: [] }, this.cost, { onToggle: (_e, cx) => { this.cost.open = !this.cost.open; cx.notify(); } }))
      .child(div().id("ship-confirm-billing").text_size(12).font_medium().text_color(c.navy).child("This keeps billing until you stop it."))
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
        .when(x.price != null, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`$${Number(x.price).toFixed(2)}`)))
        .when(Boolean(x.why), (e) => e.child(div().id(`${id}-why`).text_size(11.5).text_color(c.blue).cursor_pointer()
          .on_click((_e, cx) => { if (this.whyOpen.has(id)) this.whyOpen.delete(id); else this.whyOpen.add(id); cx.notify(); }).child("Why is this here?")))
        .when(Boolean(x.why) && this.whyOpen.has(id), (e) => e.child(v_flex().id(`${id}-signals`).gap(1).pt(2)
          .children(x.why.map((w, k) => div().id(`${id}-signal-${k}`).text_size(11.5).line_height("16px").text_color(c.grey900)
            .child(`${Math.round(w.weight * 100)}% of what the model learned: ${w.label} (${Number(w.value).toFixed(2)})`))))))
      // Against the baseline: where it was. Words, so it reads the same in a screen reader and a picture.
      .when(Boolean(x.move) && x.move !== "same", (e) => e.child(div().id(`${id}-move`).flex_none().px(6).h(18).flex().items_center().text_size(11)
        .bg(x.move === "down" ? c.failLight : c.goodLight).text_color(x.move === "down" ? c.fail : c.good)
        .child(x.move === "new" ? "new" : `${x.move} ${Math.abs(x.was - x.position)}`)));
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
      .when(Boolean(r?.staged?.why_note) && !r.baseline, (e) => e.child(div().id("ship-try-why-note").text_size(12).text_color(c.grey500).child(r.staged.why_note)))
      .when(Boolean(this.diff?.checked), (e) => e.child(this.differBox(c)))
      .when(Boolean(this.checkLine(c)), (e) => e.child(this.checkLine(c)));
    return this.row(c, st, null, below);
  }

  /** "Queries where these two differ most", from a sample of the project's own queries. When none differ it says
   *  so, and why: a candidate that reproduces keyword search is called that. */
  differBox(c) {
    const d = this.diff;
    return v_flex().id("ship-differ").gap(4)
      .when(d.mostly_same, (e) => e.child(h_flex().id("ship-same").gap(6).items_start().p(8).bg(c.warnLight)
        .child(svg(icon("triangle-alert")).size(13).mt(2).flex_none().text_color(c.warn))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.navy).child(d.text))))
      .child(div().text_size(12).font_semibold().text_color(c.navy).child("Queries where these two differ most"))
      .when(!d.queries.length, (e) => e.child(div().id("ship-differ-none").text_size(12).line_height("17px").text_color(c.grey900)
        .child(d.mostly_same ? `None of the ${d.checked} queries checked rank differently.` : d.none_differ ?? "None of the queries checked rank differently.")))
      .children(d.queries.slice(0, 6).map((q, i) => h_flex().id(`ship-differ-${i}`).gap(8).items_center().cursor_pointer().hover((x) => x.bg(c.surface2))
        .on_click((_e, cx) => this.tryQuery(cx, q.query))
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.blue).child(q.query))
        .child(div().flex_none().text_size(11.5).text_color(c.grey500).child(`${q.moved} of the top 10 in another place`))))
      .when(!d.mostly_same && Boolean(d.text) && d.queries.length > 0, (e) => e.child(div().text_size(12).text_color(c.grey500).child(d.text)));
  }

  // ---- connect your site, and shadow mode -----------------------------------------------------------------------

  connectStep(c, st) {
    if (!this.running() || !this.conn) return this.row(c, st, null, null);
    const k = this.conn, names = Object.keys(k.snippets ?? {});
    const open = this.code && names.includes(this.code) ? this.code : null;
    const below = v_flex().pl(46).pr(12).pb(12).gap(8)
      .child(h_flex().gap(10).items_start().text_size(12)
        .child(div().w(70).flex_none().text_color(c.grey500).child("Address"))
        .child(div().id("ship-connect-address").flex_1().min_w(0).font_family(MONO).text_size(11.5).text_color(c.navy).child(String(k.address ?? ""))))
      .child(div().id("ship-connect-where").text_size(12).line_height("17px").text_color(c.grey900).child(k.text))
      .when(Boolean(k.blocked), (e) => e.child(h_flex().id("ship-connect-blocked").gap(6).items_start().p(8).bg(c.warnLight)
        .child(svg(icon("triangle-alert")).size(13).mt(2).flex_none().text_color(c.warn))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.navy).child(k.blocked))))
      .child(h_flex().id("ship-connect-live").gap(6).items_center()
        .child(svg(icon(k.connected ? "circle-check" : "clock")).size(13).flex_none().text_color(k.connected ? c.good : c.grey500))
        .child(div().text_size(12.5).font_medium().text_color(k.connected ? c.good : c.grey900).child(k.live))
        .when(k.test + k.simulated > 0, (e) => e.child(div().id("ship-connect-other").text_size(11.5).text_color(c.grey500)
          .child(`Not counted: ${[k.test ? `${k.test} test` : null, k.simulated ? `${k.simulated} simulated` : null].filter(Boolean).join(", ")}.`))))
      .child(h_flex().flex_wrap().gap(6).items_center()
        .child(Button(c, "ship-connect-key", "Copy the key", (_e, cx) => this.copyKey(cx), { small: true }))
        .child(Button(c, "ship-connect-test", "Send a test request", (_e, cx) => this.act(cx, "/staging/test", {}, (d) => d.text), { small: true }))
        .children(names.map((n) => this.pick1(c, `ship-connect-code-${n.replace(/ /g, "-")}`, n === "javascript" ? "JavaScript" : n === "python" ? "Python" : n, open === n, () => { this.code = open === n ? null : n; }))))
      .child(div().id("ship-connect-key-note").text_size(12).line_height("17px").text_color(c.grey500).child(k.key_note))
      .when(Boolean(open), (e) => e.child(v_flex().id("ship-connect-snippet").gap(4)
        .child(div().p(10).bg(c.surface2).border_1().border_color(c.rule).font_family(MONO).text_size(11).line_height("15px").text_color(c.navy)
          .children(String(k.snippets[open]).split("\n").map((ln) => div().child(ln === "" ? " " : ln))))
        .child(h_flex().gap(8).items_center()
          .child(Button(c, "ship-connect-copy", "Copy this code", (_e, cx) => { cx.write_to_clipboard?.(k.snippets[open]); this.note = { tone: "good", text: "The code is on your clipboard. It reads the key from CAPTAIN_STAGING_KEY, so the key is never in your code." }; cx.notify(); }, { small: true, kind: "primary" }))
          .child(div().id("ship-connect-fallback").flex_1().min_w(0).text_size(12).line_height("17px").text_color(c.grey900).child(k.fallback)))))
      .when(this.both(), (e) => e.child(this.shadowBox(c)));
    return this.row(c, st, null, below);
  }

  /** Shadow mode: shoppers keep the baseline, the candidate ranks the same requests silently, and this is what it
   *  would have changed. Comes before any split. */
  shadowBox(c) {
    const r = this.shadow ?? { status: "none" }, on = r.status === "running";
    const abLive = this.ab?.ab?.status === "running" && this.ab.ab.mode === "captain";
    return v_flex().id("ship-shadow").p(10).gap(6).bg(c.surface2).border_1().border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(div().text_size(12.5).font_semibold().child("Shadow mode"))
        .when(on, (e) => e.child(Badge(c, "on", "good")))
        .when(Boolean(r.label) && r.status !== "none", (e) => e.child(Badge(c, r.label, r.evidence === "REAL_TRAFFIC" ? "good" : "warn")))
        .child(div().flex_1())
        .when(!on && !abLive, (e) => e.child(Button(c, "ship-shadow-start", r.status === "none" ? "Start shadow mode" : "Start shadow mode again", (_e, cx) => this.act(cx, "/shadow/start", {}, "Shadow mode is on. Shoppers keep getting the baseline's results."), { small: true, kind: "primary" })))
        .when(on, (e) => e.child(Button(c, "ship-shadow-stop", "End shadow mode", (_e, cx) => this.act(cx, "/shadow/stop", {}, "Shadow mode is off."), { small: true }))))
      .child(div().id("ship-shadow-text").text_size(12).line_height("17px").text_color(c.grey900)
        .child(r.status === "none" ? "Your site keeps showing the baseline's results. The candidate ranks the same requests silently, and Captain shows what it would have changed. No shopper sees the candidate." : r.text))
      .when(Boolean(r.ms), (e) => e.child(div().id("ship-shadow-ms").text_size(12).text_color(c.grey900)
        .child(`Response time, 95 in 100 requests under: ${r.ms.baseline.p95} ms baseline, ${r.ms.candidate.p95} ms candidate.`)))
      .when((r.queries ?? []).length > 0, (e) => e.child(v_flex().id("ship-shadow-queries").gap(2)
        .child(div().text_size(12).font_semibold().text_color(c.navy).child("Queries that differ most. Click one to open it in Try it."))
        .children(r.queries.slice(0, 6).map((q, i) => h_flex().id(`ship-shadow-q${i}`).gap(8).items_center().cursor_pointer().hover((x) => x.bg(c.surface))
          .on_click((_e, cx) => this.tryQuery(cx, q.query))
          .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.blue).child(q.query))
          .child(div().flex_none().text_size(11.5).text_color(c.grey500).child(`${q.moved} of the top 10 in another place, ${q.requests} request${q.requests === 1 ? "" : "s"}`))))));
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
      .when(Boolean(f.practice), (e) => e.child(div().id("ship-ab-practice").p(8).bg(c.warnLight).text_size(12.5).line_height("18px").text_color(c.navy)
        .child("Practice with simulated shoppers. Everything it shows is SIMULATED, and a practice test can't go to launch review.")))
      .when(f.mode === "captain", (e) => e.child(line("The candidate's share of traffic", [0.01, 0.05, 0.25, 0.5].map((x) => this.pick1(c, `ship-ab-split-${Math.round(x * 100)}`, pct(x), f.split === x, () => { f.split = x; }))))
        // What the share means here, in this endpoint's own sessions a day.
        .child(div().id("ship-ab-share-text").text_size(12).line_height("17px").text_color(c.grey900)
          .child(f.practice ? `${pct(f.split)} of the simulated shoppers go to the candidate.` : (this.opts?.shares ?? []).find((x) => Math.abs(x.split - f.split) < 1e-9)?.text ?? `${pct(f.split)} of sessions`)))
      .child(line("What decides it", can.map((m) => this.pick1(c, `ship-ab-primary-${m}`, METRICS[m], f.primary === m, () => { f.primary = m; }))))
      .when(f.mode === "captain", (e) => e.child(line("Guardrails: click one to change it", [...can.filter((m) => m !== f.primary), "latency"].map((m) => {
        const must = (f.must ??= { latency: true })[m];
        return this.pick1(c, `ship-ab-guard-${m}`, `${m === "latency" ? "Response time" : METRICS[m]}: ${must ? "must not get worse" : "watch"}`, Boolean(must), () => { f.must[m] = !must; });
      }))))
      .child(div().id("ship-ab-setup-note").text_size(12).line_height("17px").text_color(c.grey900).child(f.mode === "captain"
        ? "If a guardrail marked must not get worse clearly drops, Captain pauses the ramp and says why. One marked watch is shown and never blocks. A shopper always sees the same version."
        : "Captain gives you an address for each version on the endpoint. Split traffic in your system, then bring the readout in here."))
      .child(h_flex().gap(6)
        .child(Button(c, "ship-ab-start-go", "Start the A/B test", (_e, cx) => this.abDo(cx, "start", { split: f.split, primary: f.primary, mode: f.mode, practice: Boolean(f.practice),
          guardrails: [...can.filter((m) => m !== f.primary), "latency"].map((m) => `${m}:${(f.must ?? { latency: true })[m] ? "must" : "watch"}`) },
          f.practice ? `The practice test is running on simulated shoppers: the candidate gets ${pct(f.split)}. SIMULATED.`
            : f.mode === "captain" ? `The A/B test is running: the candidate gets ${pct(f.split)} of traffic.` : "The A/B test is recorded. Split the traffic in your own system."), { small: true, kind: "primary" }))
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
        .child(Badge(c, r.label ?? "No traffic yet", r.evidence === "REAL_TRAFFIC" ? "good" : r.evidence ? "warn" : "neutral"))
        .when(Boolean(r.evidence) && r.evidence !== "REAL_TRAFFIC", (e) => e.child(div().id("ship-ab-evidence").text_size(12).text_color(c.warn).child(r.evidence === "SIMULATED"
          ? "SIMULATED: these shoppers are generated. Nothing here says what real shoppers will do."
          : "Test traffic: these requests are not shoppers."))))
      .when(!r.evidence && this.conn?.reachable === false, (e) => e.child(div().id("ship-ab-unreachable").text_size(12).text_color(c.grey900)
        .child("A site can't reach this endpoint, so no shopper will arrive. Only this computer can send it requests.")))
      .child(h_flex().flex_wrap().items_start().gap(10).child(arm("baseline", "ship-ab-baseline")).child(arm("candidate", "ship-ab-candidate")))
      .child(div().id("ship-ab-primary").text_size(12.5).line_height("18px").text_color(r.enough ? c.navy : c.grey900).child(r.primary?.text ?? r.text))
      .children((r.guardrails ?? []).map((g, i) => h_flex().id(`ship-ab-guard-${i}`).gap(6).items_center()
        .child(svg(icon(g.ok ? "circle-check" : "triangle-alert")).size(12).flex_none().text_color(g.ok ? c.good : c.fail))
        .child(div().flex_1().min_w(0).text_size(12).line_height("17px").text_color(g.ok ? c.grey900 : c.fail).child(g.text))))
      .when(Boolean(r.hard_stop), (e) => e.child(div().id("ship-ab-paused").px(10).py(6).text_size(12.5).bg(c.failLight).text_color(c.fail).child(`${r.hard_stop} Roll back, or ramp down.`)))
      .when((r.segments ?? []).length > 0, (e) => e.child(v_flex().id("ship-ab-segments").gap(3)
        .child(div().text_size(12).font_semibold().text_color(c.navy).child("By kind of query and shopper"))
        .children(r.segments.map((x) => div().id(`ship-ab-segment-${x.segment}`).text_size(12).line_height("17px")
          .text_color(x.verdict === "candidate behind" ? c.fail : x.enough ? c.grey900 : c.grey500).child(x.text)))
        .when(Boolean(r.segment_warning), (y) => y.child(div().id("ship-ab-segment-warning").text_size(12.5).font_medium().text_color(c.fail).child(r.segment_warning)))))
      .when(Boolean(r.srm), (e) => e.child(div().id("ship-ab-srm").text_size(12).text_color(r.srm.ok ? c.grey500 : c.fail).child(r.srm.text)))
      .when(Boolean(r.needed?.text) && a.status === "running", (e) => e.child(div().id("ship-ab-needed").text_size(12.5).line_height("18px").text_color(c.navy).child(r.needed.text)))
      .when(a.status === "running", (e) => e.child(this.endings(c)))
      .when(Boolean(r.decision), (e) => e.child(div().id("ship-ab-decision").text_size(12.5).text_color(c.navy)
        .child(`Decided: ${r.decision.label}, by ${r.decision.by} on ${String(r.decision.at).slice(0, 10)}.${r.decision.note ? ` "${r.decision.note}"` : ""}`)))
      .when(a.status === "running", (e) => e.child(h_flex().id("ship-ab-actions").flex_wrap().gap(6).items_center()
        .child(div().text_size(12).font_semibold().child("Ramp"))
        .children(RAMP.map((x) => this.pick1(c, `ship-ab-ramp-${Math.round(x * 100)}`, pct(x), Math.abs((a.split ?? 0) - x) < 1e-9, () => { if (Math.abs((a.split ?? 0) - x) > 1e-9 && !(r.hard_stop && x > (a.split ?? 0))) this.ask2 = { kind: "ramp", split: x }; })))
        .child(Button(c, "ship-ab-rollback", "Roll back", (_e, cx) => { this.ask2 = { kind: "rollback" }; cx.notify(); }, { small: true }))
        .when(Boolean(a.practice), (x) => x.child(Button(c, "ship-ab-simulate", "Send simulated shoppers at staging", (_e, cx) => this.simulate(cx), { small: true, kind: "quiet", disabled: Boolean(this.busy) })))
        .child(Button(c, "ship-ab-refresh", "Read again", (_e, cx) => this.load(cx), { small: true, kind: "quiet" }))));
  }

  /** How a test ends: three choices, each recorded. Ship it opens launch review with the readout attached, and is
   *  only offered on enough real traffic. */
  endings(c) {
    const r = this.ab, cand = r.ab.arms.candidate;
    return v_flex().id("ship-ab-endings").gap(4)
      .child(h_flex().flex_wrap().gap(6).items_center()
        .child(div().text_size(12).font_semibold().child("Decide"))
        .child(Button(c, "ship-ab-ship", "Ship it", (_e, cx) => this.act(cx, "/ab/decide", { choice: "ship" }, "Recorded: ship it. Launch review opens with this readout attached.",
          (d) => form("handoff", { pipeline: d.launch_review?.pipeline ?? cand })), { small: true, kind: "primary", disabled: !r.can_ship }))
        .child(Button(c, "ship-ab-keep", "Keep the baseline", (_e, cx) => this.act(cx, "/ab/decide", { choice: "keep-baseline" }, "Recorded: keep the baseline. Every shopper gets the baseline again."), { small: true }))
        .child(Button(c, "ship-ab-more", "Keep testing", (_e, cx) => this.act(cx, "/ab/decide", { choice: "keep-testing" }, "Recorded: keep testing."), { small: true })))
      .when(!r.can_ship && Boolean(r.cannot_ship), (e) => e.child(div().id("ship-ab-cannot-ship").text_size(12).text_color(c.grey900).child(r.cannot_ship)));
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

  /** Open the A/B form. `practice`: on simulated shoppers, for an endpoint no site is calling. */
  openSetup(practice) {
    const can = this.opts?.can_measure ?? this.ab?.can_measure ?? [];
    this.setup = { split: practice ? 0.5 : 0.05, primary: can.includes("purchases") ? "purchases" : "clicks", mode: "captain", must: { latency: true }, practice: Boolean(practice) };
  }

  abStep(c, st) {
    const r = this.ab, a = r?.ab, o = this.opts;
    const open = !this.setup && !this.busy;
    // The form opens once a site is sending real requests. Before that, the only way in says what it is: practice.
    const btn = h_flex().flex_none().flex_wrap().gap(6).items_center()
      .when(open && st.action === "ab-start", (e) => e.child(Button(c, "ship-ab-start", st.label, (_e, cx) => { this.openSetup(false); cx.notify(); }, { small: true, kind: a ? "secondary" : "primary" })))
      .when(open && this.both() && a?.status !== "running", (e) => e.child(Button(c, "ship-ab-practice-go", "Practice with simulated shoppers", (_e, cx) => { this.openSetup(true); cx.notify(); }, { small: true, kind: o?.ready ? "quiet" : "secondary" })));
    if (this.busy && this.busyStep === "ab") btn.child(div().id("ship-ab-busy").text_size(12).text_color(c.grey500).child(`${this.busy}...`));
    const body = [];
    if (this.setup) body.push(this.setupBox(c));
    const filed = a?.status === "rolled_back" && r.rolled_back;
    if (filed) body.push(div().id("ship-ab-filed").text_size(12).text_color(c.grey500).child("That test's readout is filed with the project. It is no longer current."));
    else if (a && a.mode === "own") body.push(this.ownBox(c));
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

  chip(c, id, text, tone = "neutral") {
    const col = { good: [c.goodLight, c.good], warn: [c.warnLight, c.warn], bad: [c.failLight, c.fail], neutral: [c.surface2, c.grey900] }[tone];
    return div().id(id).h(22).px(8).flex().flex_none().items_center().text_size(11.5).font_medium().bg(col[0]).text_color(col[1]).child(text);
  }

  /** Open the guided setup: ask the services what it would create in the connected account, and what it costs. */
  openSetupSheet(cx) {
    this.sheet = !this.sheet;
    cx.notify();
    if (!this.sheet) return;
    const login = this.places?.cloud;
    if (!login) return;
    this.setupPlan = null; this.setupProblem = null; this.policy = null;
    const region = login.region ?? "us-east-1";
    cx.spawn(async (cx) => {
      const r = await api(`/api/serving/setup?profile=${enc(login.name)}&region=${enc(region)}`, { scoped: false, timeout: 60 });
      if (r.ok && r.data?.plan) this.setupPlan = { ...r.data.plan, status: r.data.status };
      else this.setupProblem = plainError(r.data?.plan_error ?? r.error, "Captain couldn't work out what to set up.");
      cx.notify();
    });
  }

  /** Create the serving setup with the connected login, after the person has read what it creates. */
  runSetup(cx) {
    const p = this.setupPlan;
    if (!p || this.busy) return;
    this.cx = cx;
    this.busy = "Setting up serving in your account"; this.busyStep = "stage"; this.note = null; this.policy = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/serving/setup", { body: { profile: p.profile, region: p.region, confirm: true }, scoped: false });
      let done = { ok: false, error: plainError(r.error, "The setup didn't start.") };
      if (r.ok) {
        done = await this.wait(r.data.job, 45);
        if (!done.ok) { const j = await api(`/api/jobs/${enc(r.data.job)}`, { scoped: false }); this.policy = j.ok ? j.data?.policy ?? null : null; }
      }
      this.busy = null; this.busyFor = null;
      this.note = done.ok ? { tone: "good", text: "Serving is set up in your account. You can stage there now." } : { tone: "fail", text: done.error };
      if (done.ok) { this.sheet = false; this.placesFor = null; this.loadPlaces(cx); }
      cx.notify();
    });
  }

  /** Take the serving setup out of the cloud account again. Asks first. */
  removeSetup(cx) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = "Removing the serving setup"; this.busyStep = "stage"; this.note = null; this.removeAsk = false;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/serving/setup", { method: "DELETE", scoped: false });
      const done = r.ok ? await this.wait(r.data.job, 10) : { ok: false, error: plainError(r.error, "It couldn't be removed.") };
      this.busy = null; this.busyFor = null;
      this.note = done.ok ? { tone: done.result?.status === "removed" ? "good" : "fail", text: done.result?.text ?? "The serving setup is removed." } : { tone: "fail", text: done.error };
      this.placesFor = null; this.loadPlaces(cx);
      cx.notify();
    });
  }

  /** What Captain created in the cloud account for serving, always listed, with the one action that removes it. */
  servingLine(c) {
    const s = this.serving;
    if (!s || s.status === "none" || s.status === "removed" || !(s.created ?? []).length) return null;
    return v_flex().id("ship-serving").gap(4)
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(div().flex_1().min_w(240).text_size(12).line_height("17px").text_color(c.grey900).child(s.text))
        .when(!this.removeAsk, (e) => e.child(Button(c, "ship-serving-remove", "Remove serving setup", (_e, cx) => { this.removeAsk = true; cx.notify(); }, { small: true, kind: "quiet" }))))
      .when(Boolean(this.removeAsk), (e) => e.child(h_flex().id("ship-serving-ask").p(8).gap(8).items_center().bg(c.surface2).border_1().border_color(c.rule)
        .child(div().flex_1().min_w(0).text_size(12).text_color(c.grey900).child("Remove everything Captain created for serving in your account? You can set it up again later."))
        .child(Button(c, "ship-serving-yes", "Remove it", (_e, cx) => this.removeSetup(cx), { small: true, kind: "primary" }))
        .child(Button(c, "ship-serving-no", "Keep it", (_e, cx) => { this.removeAsk = false; cx.notify(); }, { small: true }))));
  }

  /** What "Set up serving" will create in the connected account, read from the services, with the cost and a
   *  confirm. A login that lacks a permission gets the sentence and a button that copies the policy for an admin. */
  setupSheet(c) {
    const cloud = this.cloudName() ?? "your cloud", p = this.setupPlan;
    return v_flex().id("ship-setup-sheet").p(10).gap(5).bg(c.surface2).border_1().border_color(c.rule)
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(`Set up serving in your ${cloud} account`))
      .when(!p && !this.setupProblem, (e) => e.child(div().text_size(12).text_color(c.grey500).child("Working out what to create...")))
      .when(Boolean(this.setupProblem), (e) => e.child(div().id("ship-setup-problem").text_size(12).text_color(c.fail).child(this.setupProblem)))
      .when(Boolean(p), (e) => e
        .child(div().text_size(12).text_color(c.grey900).child(`Captain will create these in ${p.region}, each tagged so it can be found and removed:`))
        .children((p?.creates ?? []).map((r, i) => v_flex().id(`ship-setup-row-${i}`).py(2)
          .child(h_flex().gap(8).child(div().w(150).flex_none().text_size(12).text_color(c.grey500).child(r.resource)).child(div().font_family(MONO).text_size(11.5).text_color(c.navy).child(r.name)))
          .child(div().pl(158).text_size(12).line_height("17px").text_color(c.grey900).child(r.why))))
        .child(div().id("ship-setup-cost").text_size(12).line_height("17px").font_medium().text_color(c.navy).child(p?.cost?.text ?? ""))
        .child(div().text_size(12).line_height("17px").text_color(c.grey500).child("Nothing is created until you say so. Remove serving setup, under Details, takes all of it away again.")))
      .when(Boolean(this.policy), (e) => e.child(h_flex().id("ship-setup-policy").gap(8).items_center()
        .child(div().flex_1().min_w(0).text_size(12).text_color(c.grey900).child("Your login needs more permissions for this. The exact policy for an admin is ready to copy."))
        .child(Button(c, "ship-setup-policy-copy", "Copy the policy", (_e, cx) => this.copy(cx, JSON.stringify(this.policy, null, 2), "The policy is on your clipboard. Send it to whoever manages your AWS account."), { small: true, kind: "primary" }))))
      .child(h_flex().gap(6)
        .when(Boolean(p), (e) => e.child(Button(c, "ship-setup-go", "Create these", (_e, cx) => this.runSetup(cx), { small: true, kind: "primary", disabled: Boolean(this.busy) })))
        .child(Button(c, "ship-setup-open", "Open Cloud and compute", () => openOverlay({ mod: "captain-cloud", kind: "dialog", width: 760, height: 640 }), { small: true }))
        .child(Button(c, "ship-setup-close", "Not now", (_e, cx) => { this.sheet = false; cx.notify(); }, { small: true, kind: "quiet" })));
  }

  /** The two places a model can run, side by side with what each costs. This computer is free and only you can
   *  reach it. The connected cloud costs by the hour and a site can reach it. The last choice comes first. */
  choices(c) {
    const cand = this.pick(), champ = this.page.exps?.champion ?? null;
    if (!cand && !champ) return div().id("ship-nothing").text_size(12.5).text_color(c.grey500).child("Set a baseline or train a candidate first. Then you can stage it.");
    const what = cand ? short(cand.pipeline) : short(champ);
    const cloud = this.cloudName(), target = this.places?.targets?.[0] ?? null, last = this.lastWhere();
    const est = this.cloudCost.estimate;
    const card = (id, title, lines, button) => v_flex().id(id).flex_1().min_w(260).p(12).gap(6).bg(c.surface).border_1().border_color(c.rule)
      .child(div().text_size(13).font_semibold().text_color(c.navy).child(title))
      .children(lines.filter(Boolean).map((t, i) => div().id(`${id}-l${i}`).text_size(12).line_height("17px").text_color(c.grey900).child(t)))
      .child(div().flex_1()).child(h_flex().child(button));
    // This computer is only a normal choice when the model fits in what is free here right now.
    // Two tests: is it under the size limit for staging on this computer, and does it fit in what is free now.
    const fit = this.fit, over = fit?.outcome === "over-limit", wont = fit?.outcome === "wont-fit" || over;
    const here = card("ship-choice-local", "On this computer", ["Free. Only you can reach it.", wont ? null : "Good for trying a model. A site can't send it traffic."],
      Button(c, "ship-stage", cand ? "Stage it" : "Stage the baseline", (_e, cx) => this.stageAt(cx, "local"), { small: true, kind: last === "cloud" && target ? "secondary" : "primary", disabled: wont }))
      .when(Boolean(fit?.text), (e) => e.child(div().id("ship-fit").text_size(12).line_height("17px")
        .text_color(wont ? c.fail : fit.outcome === "tight" ? c.warn : c.grey500).child(fit.text)))
      .when(over, (e) => e.child(div().id("ship-fit-limit").text_size(12).text_color(c.blue).cursor_pointer()
        .on_click(() => run("app.settings")).child("Change the limit in Settings")));
    const there = !cloud ? null : card("ship-choice-cloud", `In your ${cloud} account`, ["A site can reach it.",
      target ? est?.text ?? "Working out the cost..." : `Serving isn't set up in your ${cloud} account yet.`, target ? "This keeps billing until you stop it." : null],
      target ? Button(c, "ship-stage-cloud", `Stage in your ${cloud} account`, (_e, cx) => this.stageAt(cx, target.name), { small: true, kind: last === "cloud" || wont ? "primary" : "secondary" })
        : Button(c, "ship-setup-cloud", `Set up serving in your ${cloud}`, (_e, cx) => this.openSetupSheet(cx), { small: true }));
    return v_flex().id("ship-choices").gap(8)
      .child(div().text_size(12.5).text_color(c.grey900).child(`Where should ${what} run?`))
      // The cloud comes first when it was the last choice, or when this computer isn't a choice for this model.
      .child(h_flex().flex_wrap().items_stretch().gap(10).children((last === "cloud" || wont) && there ? [there, here] : [here, there].filter(Boolean)))
      .when(!cloud, (e) => e.child(div().id("ship-no-cloud").text_size(12).text_color(wont ? c.navy : c.grey500)
        .child(wont ? "No cloud is connected. Connect a cloud login to stage this model (the cloud chip at the bottom of the sidebar)."
          : "To stage where a site can reach it, connect a cloud login (the cloud chip at the bottom of the sidebar).")));
  }

  /** The deployment's header: is it up, where is it, is anything calling it, and the action for now. Nothing here
   *  is behind a disclosure: the address, its Copy, the key and a ready test request are always in view. */
  header(c) {
    const s = this.staged(), on = this.running(), k = this.conn, page = this.page;
    const cand = this.pick(), champ = page.exps?.champion ?? null;
    const local = s?.kind === "local", cloud = this.cloudName(), target = this.places?.targets?.[0] ?? null;
    const address = on ? (s.endpoint?.url ?? s.endpoint?.name ?? "") : null;
    const name = on || s ? short(s.model_version) : cand ? short(cand.pipeline) : champ ? short(champ) : "Nothing to stage yet";
    const state = this.busy && this.busyStep !== "ab" ? ["Working", "warn"] : on ? ["1 copy live", "good"] : s?.status === "starting" ? ["Starting", "warn"] : s?.status === "failed" ? ["Failed", "bad"] : s ? ["Stopped", "neutral"] : ["Not staged", "neutral"];
    const actions = h_flex().flex_none().flex_wrap().gap(6).items_center();
    if (this.busy && this.busyStep !== "ab") actions.child(div().id("ship-busy").text_size(12).text_color(c.grey500).child(`${this.busy}...`));
    else if (on && !this.busy) {
      if (local && cloud && target) actions.child(v_flex().items_end().gap(2)
        .child(Button(c, "ship-stage-cloud", `Stage in your ${cloud} account`, (_e, cx) => this.stageAt(cx, target.name), { small: true, kind: "primary" }))
        .child(div().id("ship-stage-cloud-cost").text_size(11.5).text_color(c.grey500).child(this.cloudCost.estimate?.text ?? "")));
      else if (local && cloud) actions.child(Button(c, "ship-setup-cloud", `Set up serving in your ${cloud}`, (_e, cx) => this.openSetupSheet(cx), { small: true }));
      if (cand && s.serve === "baseline" && !(s.arms ?? {}).candidate) actions.child(Button(c, "ship-stage", "Stage the candidate", (_e, cx) => this.stage(cx, { candidate: cand.id }), { small: true, kind: "primary" }));
      if (champ && s.serve !== "baseline") actions.child(Button(c, "ship-restage-baseline", "Restage with the baseline", (_e, cx) => this.stage(cx, { baseline: true }), { small: true }));
      actions.child(Button(c, "ship-stop", "Stop", (_e, cx) => { this.stopAsk = true; cx.notify(); }, { small: true }));
    }
    if (s && !this.busy) actions.child(SendToAgent(c, "ship-send", { ref: `staging:${this.pid()}`, kind: "staging", label: `staging for ${page.project()?.name ?? this.pid()}` }));
    const costLine = on && !this.confirm ? v_flex().id("ship-line-cost").gap(2)
      .child(CostLine(c, "ship-cost", this.cost.estimate ?? s?.cost?.estimate ?? { text: s?.cost?.text ?? "", basis: [] }, this.cost, { onToggle: (_e, cx) => { this.cost.open = !this.cost.open; cx.notify(); } }))
      .when(!local, (e) => e.child(div().id("ship-cost-billing").text_size(12).text_color(c.grey900).child("This keeps billing until you stop it."))) : null;
    return v_flex().id("ship-header").w_full().p(14).gap(10).bg(c.surface).border_1().border_color(c.rule)
      .child(h_flex().gap(10).items_start().flex_wrap()
        .child(v_flex().flex_1().min_w(260).gap(6)
          .child(h_flex().gap(8).items_center().flex_wrap()
            .child(div().id("ship-name").font_family(MONO).text_size(16).font_semibold().text_color(c.navy).child(name))
            .when(on && Boolean(s.arms?.baseline) && s.serve !== "baseline", (e) => e.child(div().id("ship-beside").text_size(12).text_color(c.grey500).child(`beside the baseline ${short(s.arms.baseline)}`))))
          .child(h_flex().id("ship-chips").gap(6).flex_wrap()
            .child(this.chip(c, "ship-chip-state", state[0], state[1]))
            .when(on, (e) => e
              .child(this.chip(c, "ship-chip-running", `${k?.running ?? 0} request${(k?.running ?? 0) === 1 ? "" : "s"} running`))
              .child(this.chip(c, "ship-chip-where", local ? "This computer" : `Your ${cloud ?? "cloud"} account`))
              .when(Boolean(s.needs_key), (x) => x.child(this.chip(c, "ship-chip-key", "Key required")))
              // What the traffic is, only once there is some. Never "real" before a real request.
              .when(Boolean(k?.evidence_label), (x) => x.child(this.chip(c, "ship-chip-traffic", k.evidence_label, k.real ? "good" : "warn"))))))
        .child(actions))
      .when(on, (e) => e.child(v_flex().id("ship-reach").gap(4)
        .child(h_flex().gap(8).items_center().flex_wrap()
          .child(div().id("ship-address").font_family(MONO).text_size(12.5).text_color(c.navy).child(address))
          .child(Button(c, "ship-copy-address", "Copy", (_e, cx) => this.copy(cx, address, "The address is on your clipboard."), { small: true }))
          .child(Button(c, "ship-copy-key", "Copy the key", (_e, cx) => this.copyKey(cx), { small: true }))
          .when(Boolean(k?.test_request), (x) => x.child(Button(c, "ship-copy-test", "Copy a test request", (_e, cx) => this.copy(cx, k.test_request, "A test request is on your clipboard. Set CAPTAIN_STAGING_KEY in that shell first: Copy the key gives it to you."), { small: true }))))
        .child(div().id("ship-where").text_size(12).line_height("17px").text_color(c.grey900)
          .child(local ? "Only this computer can reach it." : `It runs in your ${cloud ?? "cloud"} account. Your site can call it.`))
        // How long it has been up and when it stops by itself. Where it runs is already said above, once.
        .when(Boolean(this.status.health), (x) => x.child(div().id("ship-status").text_size(12).line_height("17px").text_color(c.grey500)
          .child(`${this.status.health.shadow ? "Shadow mode is on: every request is answered with the baseline's results. " : ""}Up ${span(this.status.health.uptime_s)}. Stops by itself in ${span(this.status.health.stops_in_s)} if nobody uses it.`)))))
      .when(!on && Boolean(s) && !this.busy, (e) => e.child(div().id("ship-line-stopped").text_size(12).line_height("17px").text_color(c.grey900)
        .child(s.status === "stopped" ? `Last staged: ${short(s.model_version)}. ${this.status.text}` : this.status.text)))
      .when(!on && Boolean(s?.spent?.text), (e) => e.child(div().id("ship-line-spent").text_size(12).text_color(c.grey900).child(s.spent.text)))
      .when(!on && !s, (e) => e.child(div().id("ship-line-what").text_size(12).line_height("17px").text_color(c.grey900)
        .child("Staging runs a model version as a live endpoint you can try before launch review. It runs in your own cloud, or on this computer.")))
      .when(!on && !this.busy && !this.confirm, (e) => e.child(this.choices(c)))
      .when(Boolean(costLine), (e) => e.child(costLine))
      .when(Boolean(this.status?.warning), (e) => e.child(div().id("ship-line-warning").text_size(12).text_color(c.warn).child(this.status.warning)))
      .when(Boolean(this.note), (e) => e.child(div().id("ship-note").px(10).py(6).text_size(12).bg(this.note.tone === "fail" ? c.failLight : c.goodLight)
        .text_color(this.note.tone === "fail" ? c.fail : c.good).child(this.note.text)))
      .when(Boolean(this.problem), (e) => e.child(div().id("ship-problem").text_size(12).text_color(c.fail).child(this.problem)))
      .when(this.sheet, (e) => e.child(this.setupSheet(c)))
      .when(Boolean(this.servingLine(c)), (e) => e.child(this.servingLine(c)))
      .when(Boolean(this.confirm), (e) => e.child(this.confirmBox(c)))
      .when(Boolean(this.stopAsk) && on, (e) => e.child(this.stopBox(c)))
      .when(Boolean(s), (e) => e.child(this.detailsBox(c, s)));
  }

  /** What a tab needs, asked when it is opened and again while it stays open. */
  loadTab(cx) {
    const pid = this.pid();
    const get = (path, set) => cx.spawn(async (cx) => { const r = await api(`/api/projects/${enc(pid)}${path}`, { scoped: false, timeout: 60 }); if (this.pid() === pid && r.ok) { set(r.data); cx.notify(); } });
    if (this.tab === "requests") get("/staging/requests?limit=50", (d) => { this.reqs = d; });
    if (this.tab === "versions") get("/staging/versions", (d) => { this.vers = d; });
  }

  /** Open one request beside the list. The list keeps its place. */
  openRequest(cx, id) {
    this.reqOpen = { id, data: null };
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/requests/${enc(id)}`, { scoped: false, timeout: 60 });
      if (this.reqOpen?.id === id) { this.reqOpen = { id, data: r.ok ? r.data : null, problem: r.ok ? null : plainError(r.error, "That request couldn't be read.") }; cx.notify(); }
    });
  }

  /** Requests, newest first, and the one that is open beside them. */
  requestsTab(c) {
    const d = this.reqs, open = this.reqOpen;
    const clock = (ts) => String(ts ?? "").slice(11, 23);
    const cell = (w, t, mono) => div().w(w).flex_none().truncate().text_size(12).when(Boolean(mono), (e) => e.font_family(MONO).text_size(11.5)).child(String(t ?? ""));
    const list = v_flex().id("dep-requests").flex_1().min_w(360)
      .child(div().id("dep-requests-text").pb(6).text_size(12).text_color(c.grey500).child(d ? d.text : "Reading the request log..."))
      .when(Boolean(d?.rows?.length), (e) => e
        .child(h_flex().gap(8).py(4).text_color(c.grey500).border_b(1).border_color(c.rule)
          .child(cell(92, "Time")).child(div().flex_1().min_w(80).text_size(12).child("Query")).child(cell(80, "Answered by")).child(cell(64, "Took")).child(cell(74, "Traffic")).child(cell(70, "Status")))
        .children(d.rows.map((r, i) => h_flex().id(`dep-request-${i}`).gap(8).py(5).items_center().cursor_pointer().border_b(1).border_color(c.rule)
          .when(open?.id === r.request_id, (x) => x.bg(c.blueLight)).hover((x) => x.bg(c.surface2))
          .on_click((_e, cx) => this.openRequest(cx, r.request_id))
          .child(cell(92, clock(r.ts), true)).child(div().flex_1().min_w(80).truncate().text_size(12.5).text_color(c.navy).child(r.query))
          .child(cell(80, r.arm === "baseline" ? "Baseline" : "Candidate")).child(cell(64, `${Number(r.ms).toFixed(1)} ms`)).child(cell(74, r.traffic))
          .child(div().w(70).flex_none().text_size(12).text_color(c.good).child("Answered")))));
    const x = open?.data;
    const panel = !open ? null : v_flex().id("dep-request-panel").w(360).flex_none().p(10).gap(6).bg(c.surface2).border_1().border_color(c.rule)
      .child(h_flex().gap(6).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().child(x?.query ?? "Opening..."))
        .child(Button(c, "dep-request-close", "Close", (_e, cx) => { this.reqOpen = null; cx.notify(); }, { small: true, kind: "quiet" })))
      .when(Boolean(open.problem), (e) => e.child(div().text_size(12).text_color(c.fail).child(open.problem)))
      .when(Boolean(x), (e) => e
        .child(div().id("dep-request-meta").text_size(12).line_height("17px").text_color(c.grey900)
          .child(`${x.arm === "baseline" ? "Baseline" : "Candidate"} ${short(x.model_version)} answered in ${Number(x.ms).toFixed(1)} ms. ${x.traffic === "real" ? "Real traffic." : x.traffic === "test" ? "Test traffic." : "Simulated."}`))
        .child(h_flex().gap(6).items_center().child(div().text_size(11.5).text_color(c.grey500).child("Request")).child(div().id("dep-request-id").font_family(MONO).text_size(11.5).child(x.request_id)))
        .children(x.results.map((p, i) => v_flex().id(`dep-request-r${i}`).py(4).border_t(1).border_color(c.rule)
          .child(h_flex().gap(6).child(div().w(18).flex_none().text_size(11.5).text_color(c.grey500).child(String(p.position))).child(div().flex_1().min_w(0).truncate().text_size(12.5).child(p.title))
            .when((p.events ?? []).length > 0, (y) => y.child(Badge(c, p.events[p.events.length - 1], "good"))))
          .when(Boolean(p.signals), (y) => y.child(div().pl(24).text_size(11).line_height("15px").text_color(c.grey500)
            .child(p.signals.filter((g) => g.value).slice(0, 4).map((g) => `${g.label} ${Number(g.value).toFixed(2)}`).join(", "))))))
        .when(Boolean(x.shadow), (e2) => e2.child(div().id("dep-request-shadow").text_size(12).text_color(c.grey900)
          .child(x.shadow.same ? "Shadowed: the candidate would have returned the same top 10." : "Shadowed: the candidate would have returned a different top 10."))));
    return h_flex().id("dep-requests-wrap").w_full().p(12).gap(12).items_start().bg(c.surface).border_1().border_color(c.rule).child(list).when(Boolean(panel), (e) => e.child(panel));
  }

  /** Put an earlier version back, after a yes. */
  rollBackTo(cx, n) {
    if (this.busy) return;
    this.cx = cx;
    this.busy = `Rolling back to version ${n}`; this.busyStep = "stage"; this.note = null; this.verAsk = null;
    this.busyFor = { what: "stage", was: this.running() ? this.staged().id : null }; this.settled = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/versions/${n}/rollback`, { body: {}, scoped: false });
      const done = r.ok ? await this.wait(r.data.job, 30) : { ok: false, error: plainError(r.error, "It couldn't be rolled back.") };
      this.busy = null; this.busyFor = null;
      this.note = done.ok ? { tone: "good", text: `Rolled back to version ${n}.` } : { tone: "fail", text: done.error };
      this.load(cx); this.loadTab(cx);
      this.page.reload?.(cx);
    });
  }

  /** Every version put on this endpoint, the live one marked, and Roll back on recent ones. */
  versionsTab(c) {
    const d = this.vers;
    return v_flex().id("dep-versions").w_full().p(12).gap(2).bg(c.surface).border_1().border_color(c.rule)
      .child(div().pb(6).text_size(12).text_color(c.grey500).child(d ? d.text : "Reading the versions..."))
      .children((d?.rows ?? []).map((r) => v_flex().id(`dep-version-${r.n}`).py(6).border_t(1).border_color(c.rule)
        .child(h_flex().gap(10).items_center().flex_wrap()
          .child(div().w(36).flex_none().text_size(12.5).font_semibold().child(`v${r.n}`))
          .child(div().flex_1().min_w(160).truncate().font_family(MONO).text_size(12).text_color(c.navy).child(short(r.model_version)))
          .when(r.live, (e) => e.child(Badge(c, "Live", "good")))
          .child(div().flex_none().text_size(12).text_color(c.grey500).child(`${String(r.at).slice(0, 16).replace("T", " ")} by ${r.by ?? "someone"}`))
          .when(Boolean(r.can_roll_back) && !this.busy, (e) => e.child(Button(c, `dep-version-${r.n}-back`, "Roll back", (_e, cx) => { this.verAsk = r.n; cx.notify(); }, { small: true }))))
        .when(this.verAsk === r.n, (e) => e.child(h_flex().id("dep-version-ask").mt(6).p(8).gap(8).items_center().bg(c.surface2).border_1().border_color(c.rule)
          .child(div().flex_1().min_w(0).text_size(12).text_color(c.grey900).child(`Put ${short(r.model_version)} back on the endpoint? What runs now is replaced.`))
          .child(Button(c, "dep-version-yes", "Roll back", (_e, cx) => this.rollBackTo(cx, r.n), { small: true, kind: "primary" }))
          .child(Button(c, "dep-version-no", "Not now", (_e, cx) => { this.verAsk = null; cx.notify(); }, { small: true }))))));
  }

  /** The deployment page: the header, then tabs. */
  deployment(c) {
    const [, tryIt, connect, ab, launch] = this.steps();
    const tabs = [["requests", "Requests"], ["try", "Try it"], ["traffic", "Traffic"], ["versions", "Versions"]];
    const tab = h_flex().id("dep-tabs").gap(18).border_b(1).border_color(c.rule).children(tabs.map(([id, label]) =>
      div().id(`dep-tab-${id}`).py(8).text_size(13).font_medium().cursor_pointer().border_b(2).border_color(this.tab === id ? c.blue : "#00000000")
        .text_color(this.tab === id ? c.blue : c.grey900).on_click((_e, cx) => { this.tab = id; this.loadTab(cx); cx.notify(); }).child(label)));
    const body = this.tab === "requests" ? this.requestsTab(c) : this.tab === "versions" ? this.versionsTab(c) : this.tab === "try"
      ? v_flex().id("dep-try").w_full().bg(c.surface).border_1().border_color(c.rule).child(this.tryStep(c, tryIt))
      : v_flex().id("dep-traffic").w_full().bg(c.surface).border_1().border_color(c.rule)
        .when(this.page.shipFrom !== "ab", (e) => e.child(this.connectStep(c, connect)))
        .child(div().border_t(1).border_color(c.rule).child(this.abStep(c, ab)))
        .child(div().border_t(1).border_color(c.rule).child(this.launchStep(c, launch)));
    return v_flex().id("deployment").w_full().gap(12).child(this.header(c)).child(tab).child(body);
  }

  /** The Ship part of the project page: a short card that says where the model is and links to its page. */
  summary(c) {
    const s = this.staged(), on = this.running(), k = this.conn;
    const address = on ? (s.endpoint?.url ?? s.endpoint?.name ?? "") : null;
    const counts = on && k ? [`${k.real} real request${k.real === 1 ? "" : "s"}`, k.test ? `${k.test} test` : null, k.simulated ? `${k.simulated} simulated` : null].filter(Boolean).join(", ") : null;
    const [, , , , launch] = this.steps();
    return v_flex().id("ship-summary").w_full().bg(c.surface).border_1().border_color(c.rule)
      .child(h_flex().p(12).gap(12).items_center().flex_wrap()
        .child(v_flex().flex_1().min_w(280).gap(3)
          .child(h_flex().gap(8).items_center().flex_wrap()
            .child(div().text_size(13).font_semibold().text_color(c.navy).child("Deployment"))
            .child(this.chip(c, "ship-summary-state", on ? "1 copy live" : s?.status === "failed" ? "Failed" : s ? "Stopped" : "Not staged", on ? "good" : "neutral"))
            .when(on, (e) => e.child(this.chip(c, "ship-summary-where", s.kind === "local" ? "This computer" : `Your ${this.cloudName() ?? "cloud"} account`))))
          .child(div().id("ship-summary-text").text_size(12).line_height("17px").text_color(c.grey500)
            .child(on ? this.status.text : s ? this.status.text : "Staging runs a model version as a live endpoint you can try before launch review. It runs in your own cloud, or on this computer."))
          .when(on, (e) => e.child(h_flex().gap(8).items_center().flex_wrap()
            .child(div().id("ship-summary-address").font_family(MONO).text_size(12).text_color(c.navy).child(address))
            .child(Button(c, "ship-summary-copy", "Copy", (_e, cx) => this.copy(cx, address, "The address is on your clipboard."), { small: true }))
            .when(Boolean(counts), (x) => x.child(div().id("ship-summary-counts").text_size(12).text_color(c.grey900).child(counts))))))
        .child(Button(c, "ship-open", on ? "Open the deployment" : "Stage it", (_e, cx) => this.page.openDeployment(cx), { small: true, kind: "primary" })))
      .child(div().border_t(1).border_color(c.rule).child(this.launchStep(c, launch)));
  }

  render(c) {
    const [stage, tryIt, connect, ab, launch] = this.steps();
    const from = this.page.shipFrom; // the real-window check starts lower down, where a picture can't scroll
    return v_flex().id("ship-path").w_full().bg(c.surface).border_1().border_color(c.rule)
      .when(!from, (e) => e.child(this.stageStep(c, stage))
        .child(div().border_t(1).border_color(c.rule).child(this.tryStep(c, tryIt))))
      .when(from !== "ab", (e) => e.child(div().border_t(1).border_color(c.rule).child(this.connectStep(c, connect))))
      .child(div().border_t(1).border_color(c.rule).child(this.abStep(c, ab)))
      .child(div().border_t(1).border_color(c.rule).child(this.launchStep(c, launch)));
  }
}
