// CAPTAIN: the Ship part of a project's page as a path a person walks: 1 Stage, 2 Try it, then Launch review
// (Edgar, 2026-11-10: "make it easier to stage ml models to be deployed ... business user friendly"). A staged model
// runs in the team's own cloud, or on this computer; Captain hosts nothing. Each step has a status, one primary
// action and a tick when it is done. Words stay plain: where it runs, what it costs, when it stops by itself. The
// endpoint name, the instance type and the log sit behind Details.
//   GET  /api/projects/{id}/staging          what is staged and how it is doing
//   POST /api/projects/{id}/stage            start it (a job). 409 {needs_confirm, plan} when a cost needs a yes
//   POST /api/projects/{id}/unstage          stop it and remove what Captain made
//   POST /api/projects/{id}/staging/query    the Try it box: the staged results beside the baseline's
//   POST /api/projects/{id}/staging/check    did serving compute the features training computes? One line.
import { div, svg, image } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { exec, appHome } from "herdr";
import { MONO } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Badge, Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { SendToAgent } from "./kit/context.js";

const enc = encodeURIComponent;
const THUMB = 96;
const short = (ref) => String(ref ?? "").replace(/^.*\//, "");

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
    this.box = InputState.new({ placeholder: "Type what a shopper would search for" });
    this.box.on("submit", (_e, cx) => this.ask(cx ?? this.cx));
  }

  pid() { return this.page.pid(); }

  load(cx) {
    this.cx = cx;
    const pid = this.pid();
    if (!pid) return;
    if (pid !== this.shown) { this.shown = pid; this.status = null; this.result = null; this.note = null; this.confirm = null; this.askError = null; }
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(pid)}/staging`, { scoped: false });
      if (this.pid() !== pid) return;
      if (r.ok) { this.status = r.data; this.problem = null; } else if (!r.starting) this.problem = plainError(r.error, "Captain couldn't read staging.");
      cx.notify();
    });
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
    ];
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  mark(c, st) {
    return st.done
      ? div().id(`ship-done-${st.id}`).size(22).flex_none().flex().items_center().justify_center().rounded_full().bg(c.goodLight).text_color(c.good).child(svg(icon("check")).size(13))
      : div().size(22).flex_none().flex().items_center().justify_center().rounded_full().border_1().border_color(c.grey300).text_size(11.5).font_semibold().text_color(c.grey900).child(String(st.n));
  }

  row(c, st, action, below) {
    return v_flex().id(`ship-step-${st.id}`).w_full()
      .child(h_flex().p(12).gap(12).items_center()
        .child(this.mark(c, st))
        .child(v_flex().flex_1().min_w(0).gap(1)
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

  render(c) {
    const [stage, tryIt] = this.steps();
    return v_flex().id("ship-path").w_full().bg(c.surface).border_1().border_color(c.rule)
      .child(this.stageStep(c, stage))
      .child(div().border_t(1).border_color(c.rule).child(this.tryStep(c, tryIt)));
  }
}
