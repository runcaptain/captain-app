// CAPTAIN: Comparisons, Profile tab. Where each arm's time goes, from POST /api/runs/{run}/profile (a job that replays
// the run's queries through every arm, offline, on this computer):
// - the summary sentence first ("demo-tuned adds 4.0 ms at p99 against demo-bm25, most of it in boosts (+3.8 ms)");
// - budget breaches in words, when a pipeline declares a budget;
// - one stacked bar per arm: each stage's p99, with its share of the time, all arms on one scale;
// - memory, labelled as Python allocations (not process memory);
// - the slowest queries (total, the stage that dominated, candidates); a row opens the Explain view for that query;
// - arms that can't be replayed (imported rankings), with the reason;
// - "Measured offline on this computer, not production" at the bottom, always.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { openOverlay } from "herdr";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { COLORS } from "./kit/charts.js";
import { int, num } from "./kit/num.js";

const ms = (x) => (x === null || x === undefined ? "none" : `${num(x, x < 10 ? 2 : 1)} ms`);
const mb = (b) => (b === null || b === undefined ? "none" : b >= 1e6 ? `${num(b / 1e6, 1)} MB` : `${num(b / 1e3, 0)} kB`);
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const QUERY_CHOICES = [50, 200, 1000];

export class Profiler {
  constructor(host) {
    this.host = host;
    this.byRun = new Map(); // run ref -> {job, progress, started, result, error}
    this.queries = 200;
    this.memory = true;
  }

  get cur() {
    return this.byRun.get(this.host.runRef()) ?? null;
  }

  /** Replay the run's queries through every arm to time them: a job, polled by tick(). */
  start(cx) {
    const ref = this.host.runRef();
    if (!ref) return;
    const st = { job: null, progress: 0, started: Date.now(), result: null, error: null };
    this.byRun.set(ref, st);
    cx.spawn(async (cx) => {
      const r = await api(`/api/runs/${encodeURIComponent(ref)}/profile`, { body: { max_queries: this.queries, memory: this.memory } });
      if (!r.ok) st.error = sentence(r.data?.error ?? r.data?.detail ?? r.error) || "Couldn't start the profile.";
      else st.job = r.data.job;
      cx.notify();
    });
    cx.notify();
  }

  /** Every half second from the host: follow the running profile. */
  tick(cx) {
    // props.autoProfile (a layout, or the real-window check): profile the open run once it has loaded.
    if (this.host.autoProfile && this.host.run && !this.host.run.error && !this.cur) { this.host.autoProfile = false; this.start(cx); }
    for (const [, st] of this.byRun) {
      if (!st.job || st.polling) continue;
      st.polling = true;
      cx.spawn(async (cx) => {
        const r = await api(`/api/jobs/${encodeURIComponent(st.job)}`);
        st.polling = false;
        if (!r.ok) { cx.notify(); return; }
        st.progress = r.data.progress ?? st.progress;
        if (r.data.status === "done") { st.job = null; st.result = r.data.result; }
        else if (r.data.status === "failed") { st.job = null; st.error = sentence(r.data.error ?? r.data.message) || "The profile stopped."; }
        cx.notify();
      });
    }
  }

  render(c, w) {
    const st = this.cur;
    const res = st?.result;
    const chip = (id, label, on, onClick) => div().id(id).h(24).px(8).flex().flex_none().items_center().cursor_pointer().text_size(12)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900).on_click(onClick).child(label);
    const running = !!st?.job || (!!st && !st.result && !st.error);
    const secs = running ? Math.floor((Date.now() - st.started) / 1000) : 0;
    const controls = h_flex().flex_wrap().gap(8).items_center()
      .child(Button(c, "prof-run", running ? "Profiling" : res ? "Profile again" : "Profile this run", (_e, cx) => this.start(cx), { kind: res ? "secondary" : "primary", small: true, disabled: running }))
      .child(div().text_size(12).text_color(c.grey900).child("Queries"))
      .children(QUERY_CHOICES.map((n) => chip(`prof-q-${n}`, int(n), this.queries === n, (_e, cx) => { this.queries = n; cx.notify(); })))
      .child(chip("prof-mem", "Measure memory", this.memory, (_e, cx) => { this.memory = !this.memory; cx.notify(); }))
      .when(running, (e) => e.child(div().text_size(12).text_color(c.grey500).child(`Replaying every arm, ${Math.round((st.progress ?? 0) * 100)}%, ${secs} s`)));
    return v_flex().id("profile").flex_1().min_h(0)
      .child(v_flex().id("profile-body").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(14).gap(14)
        .child(controls)
        .when(!st, (e) => e.child(div().max_w(620).text_size(12.5).line_height("19px").text_color(c.grey900)
          .child("Replays this run's queries through each arm and times every stage: retrieval, boosts, rules, reranking. You see which arm is slower, by how much at p99, and where the time goes.")))
        .when(!!st?.error, (e) => e.child(div().p(12).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).text_color(c.fail).child(st.error)))
        .when(running && !res, (e) => e.child(Skeleton(c, 5)))
        .when(!!res, (e) => this.result(e, c, res, w)))
      // Always visible: these numbers are a laptop replay.
      .child(h_flex().id("prof-where").flex_none().min_h(30).py(6).flex_wrap().px(20).gap_x(8).gap_y(2).items_center().border_t(1).border_color(c.rule).text_size(11.5).text_color(c.grey900)
        .child(div().child("Measured offline on this computer, not production."))
        .when(!!res?.measured_on, (e) => e.child(div().text_color(c.grey500).child(`${res.measured_on.machine}, Python ${res.measured_on.python}, one query at a time.`))));
  }

  result(e, c, res, w) {
    const arms = res.arms ?? [];
    // One scale for every arm: the widest stack of stage p99s.
    const widest = Math.max(1, ...arms.map((a) => (a.stages ?? []).reduce((n, s) => n + (s.p99_ms ?? 0), 0)));
    const stageNames = [...new Set(arms.flatMap((a) => (a.stages ?? []).map((s) => s.stage)))];
    const color = (stage) => COLORS[stageNames.indexOf(stage) % COLORS.length];
    return e
      .children((res.summary ?? []).map((t, i) => div().id(`prof-sum-${i}`).text_size(14).font_medium().text_color(c.navy).child(/[.!?]$/.test(String(t).trim()) ? String(t).trim() : `${String(t).trim()}.`)))
      .when(!(res.summary ?? []).length && arms.length === 1, (e) => e.child(div().text_size(13).text_color(c.grey900).child(`One arm could be timed, so there's nothing to compare it with. ${arms[0].pipeline} takes ${ms(arms[0].total?.p99_ms)} at p99.`)))
      .children(arms.flatMap((a) => (a.budget?.breaches ?? []).map((b, i) => h_flex().id(`prof-breach-${a.pipeline}-${i}`).gap(8).items_center().p(8).border_1().border_color(c.warn).text_size(12.5)
        .child(div().text_color(c.warn).font_medium().child("Over budget")).child(div().text_color(c.navy).child(sentence(`${a.pipeline}: ${b}`))))))
      .children((res.skipped ?? []).map((s, i) => div().id(`prof-skip-${i}`).text_size(12.5).text_color(c.grey900).child(sentence(`${s.arm} isn't timed: ${s.reason}`))))
      .when(!arms.length, (e) => e.child(div().text_size(13).font_medium().text_color(c.navy).child("Nothing in this run can be timed.")))
      .when(arms.length > 0, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`${int(res.queries)} queries through each arm. Each bar is the arm's stages at p99, on one scale.`)))
      .children(arms.map((a, ai) => v_flex().id(`prof-arm-${ai}`).gap(6)
        .child(h_flex().gap(10).items_center()
          .child(div().text_size(13).font_medium().font_family(MONO).child(a.pipeline))
          .child(div().text_size(12).text_color(c.grey900).child(`p50 ${ms(a.total?.p50_ms)}, p95 ${ms(a.total?.p95_ms)}, p99 ${ms(a.total?.p99_ms)}`))
          .when(a.budget?.ok === true, (e) => e.child(div().text_size(11.5).text_color(c.good).child("within budget"))))
        // Widths in percent of the pane: every arm on one scale, and never past the edge.
        .child(h_flex().id(`prof-bar-${ai}`).h(22).w_full().max_w(900).overflow_hidden().bg(c.surface2).children((a.stages ?? []).filter((s) => (s.p99_ms ?? 0) > 0).map((s) => {
          const pct = Math.max(0.5, ((s.p99_ms ?? 0) / widest) * 100);
          return div().id(`prof-seg-${ai}-${s.stage}`).w(`${pct.toFixed(2)}%`).h_full().flex_none().bg(color(s.stage)).tooltip(`${s.stage}: ${ms(s.p99_ms)} at p99, ${Math.round((s.share ?? 0) * 100)}% of the time`)
            .when(pct > 18, (e) => e.flex().items_center().px(4).overflow_hidden().child(div().truncate().text_size(10.5).text_color("#ffffff").child(`${s.stage} ${ms(s.p99_ms)}`)));
        })))
        .child(h_flex().flex_wrap().gap(12).children((a.stages ?? []).map((s) => h_flex().gap(5).items_center().text_size(11.5)
          .child(div().size(9).flex_none().bg(color(s.stage)))
          .child(div().text_color(c.navy).child(s.stage))
          .child(div().font_family(MONO).text_color(c.grey900).child(`${ms(s.p99_ms)}, ${Math.round((s.share ?? 0) * 100)}%`)))))
        .when(!!a.memory, (e) => e.child(div().text_size(11.5).text_color(c.grey900)
          .child(`Memory, Python allocations, not process memory: ${mb(a.memory.peak_python_bytes_p50)} per query at the median, ${mb(a.memory.peak_python_bytes_max)} at most.`)))))
      .child(this.slowest(c, arms));
  }

  /** The slowest queries of every arm; a row opens the Explain view for that query. */
  slowest(c, arms) {
    const rows = arms.flatMap((a) => (a.slowest ?? []).map((q) => ({ ...q, arm: a.pipeline }))).sort((x, y) => y.total_ms - x.total_ms).slice(0, 15);
    if (!rows.length) return div();
    const run = this.host.runRef();
    // The query takes what's left; the rest fit a narrow pane.
    const cell = (w, el) => (w === 0 ? div().flex_1().min_w(120) : div().w(w).flex_none()).px(8).truncate().child(el);
    return v_flex().id("prof-slow").gap(0).border_1().border_color(c.rule)
      .child(h_flex().h(26).items_center().bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium()
        .child(cell(0, "Slowest queries")).child(cell(116, "Arm")).child(cell(76, "Total")).child(cell(128, "Most time in")).child(cell(80, "Candidates")))
      .children(rows.map((q, i) => h_flex().id(`prof-slow-${i}`).h(26).items_center().border_b(1).border_color(c.rule).text_size(12).cursor_pointer()
        .hover((s) => s.bg(c.blueLight)).tooltip(q.query_id ? "Explain why it ranked as it did, stage by stage" : "This query has no id in the run, so it can't be explained")
        .when(!!q.query_id, (e) => e.on_click(() => openOverlay({ mod: "captain-explain", kind: "dialog", width: 0.9, height: 0.9, props: { run, query_id: q.query_id } })))
        .child(cell(0, q.query)).child(cell(116, div().font_family(MONO).child(q.arm)))
        .child(cell(76, div().font_family(MONO).child(ms(q.total_ms))))
        .child(cell(128, `${q.dominant_stage ?? "none"}${q.stages_ms?.[q.dominant_stage] != null ? `, ${ms(q.stages_ms[q.dominant_stage])}` : ""}`))
        .child(cell(80, div().font_family(MONO).child(q.candidates === null || q.candidates === undefined ? "" : int(q.candidates))))));
  }
}
