// CAPTAIN: Runs, live: what's running now leads the one list of runs, and "Running now" filters to it with what
// just ran (GET /api/live). This draws one run as it happens
// (GET /api/live/{id}?since=n, asked again every second while it runs). Every live run draws the same way, from
// its events (backend/core/captain_search/live.py): the stages, a progress bar with the time left, the latest of each
// metric per arm, a metric's line as it fills in, simulated shoppers side by side (the same shopper on each arm),
// and the log. Captain's backtests stream here, and so does any tool a team wires up through `cst live`. A run that
// went out as a job shows what the job cost once it has finished (GET /api/jobs/{id}, asked from the timer).
import { div, svg, PathBuilder } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { metricWords, hideAccount, clockWords } from "./kit/words.js";
import { int } from "./kit/num.js";
import { costText } from "./kit/cost.js";

const ICON = (n) => `mods/captain-runs/icons/${n}.svg`;
const KIND = { backtest: ["backtest", "Offline evaluation"], training: ["pipeline", "Training"], sweep: ["sim", "Settings sweep"], deploy: ["deploy", "Deploy"], custom: ["run", "Run"] };
const STATUS = { queued: ["queued", "Waiting to start"], running: ["running", "Running"], done: ["done", "Finished"], failed: ["failed", "Failed"], stopped: ["x", "Stopped"] };
const KEEP = { shoppers: 240, logs: 400, points: 600 };
const PCT = new Set(["conversion", "ctr", "zero_result_rate", "judged@10", "coverage"]);

// Still going: running here, or waiting for a compute target to start it.
export const going = (s) => s === "running" || s === "queued";
/** A live run's kind, in the words of the one list's Kind column. */
export const liveKind = (x) => (KIND[x?.kind] ?? KIND.custom)[1];
const bare = (ref) => String(ref ?? "").split("@")[0];
const secs = (s) => (s == null || !Number.isFinite(s) ? "" : s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
const fmt = (name, v) => (v == null ? "" : PCT.has(name) ? `${(v * 100).toFixed(1)}%` : /revenue|rpv|usd|\$/.test(name) ? `$${Number(v).toFixed(2)}` : Math.abs(v) >= 1000 ? int(v) : Number(v).toFixed(Math.abs(v) < 1 ? 4 : 2));
const label = (name) => { const w = metricWords(name); return w && w !== name ? w : String(name).replace(/_/g, " "); };

/** The steps a run goes through, as far as is known: its plan, without the planned steps it passed over. A step
 *  before the one running that never began was not needed (on a host where the image is already built), so it is
 *  not drawn and its usual time is not counted. Steps still ahead stay: they may yet run. */
export function stepsOf(head) {
  const h = head ?? {};
  const plan = h.steps?.length ? h.steps : (h.stages ?? []).map((title) => ({ title }));
  if (!Array.isArray(h.seen)) return plan;
  const at = h.status === "done" ? plan.length : plan.findIndex((s) => s.title === h.stage);
  return plan.filter((s, j) => h.seen.includes(s.title) || (at >= 0 && j > at) || (at < 0 && h.status !== "done"));
}

/** Where a run stands on the page's own clock: {elapsed, step, stepElapsed, usual, usually, progress}. A running run
 *  counts from when it started, whether or not anything new was heard from it, so its time never stands still.
 *  `last` is the time of the last thing heard (seconds since the start), used once the run is over.
 *  Progress comes from the run when it says, and is filled in between from its steps: the steps done, plus the
 *  running step's share by what it usually takes (never the whole of it until it is done). */
export function clock(head, last = 0, now = Date.now()) {
  const h = head ?? {};
  const started = Date.parse(h.started ?? "");
  const on = going(h.status);
  const elapsed = on && Number.isFinite(started) ? Math.max(last, (now - started) / 1000) : last;
  const steps = stepsOf(h);
  const i = steps.findIndex((s) => s.title === h.stage);
  const step = i >= 0 ? steps[i] : null;
  const stepElapsed = on && step && typeof h.stage_at === "number" ? Math.max(0, elapsed - h.stage_at) : null;
  const usual = step && Number(step.seconds) > 0 ? Number(step.seconds) : null;
  let progress = typeof h.progress === "number" ? h.progress : null;
  if (h.status === "done") progress = 1;
  else if (steps.length && i >= 0) {
    const all = steps.every((s) => Number(s.seconds) > 0);
    const w = (s) => (all ? Number(s.seconds) : 1);
    const total = steps.reduce((t, s) => t + w(s), 0) || 1;
    const before = steps.slice(0, i).reduce((t, s) => t + w(s), 0);
    const share = on && stepElapsed != null ? Math.min(0.95, usual ? stepElapsed / usual : stepElapsed / (stepElapsed + 60)) : 0;
    progress = Math.max(progress ?? 0, (before + w(step) * share) / total);
  }
  return { elapsed, index: i, step, stepElapsed, usual, usually: step?.usually ?? null, progress };
}

/** The quiet line under the log while the running step says nothing: "Still building the model's image. 48 s so
 *  far, usually about a minute and a half." Drawn from the page's clock, not from anything the run sends. */
export function stillWords(k) {
  if (!k?.step || k.stepElapsed == null) return null;
  const t = String(k.step.title ?? "").trim().replace(/[.]+$/, "");
  const doing = t ? t[0].toLowerCase() + t.slice(1) : "working";
  return `Still ${doing}. ${clockWords(k.stepElapsed)} so far${k.usually ? `, usually ${k.usually}` : ""}.`;
}

/** What one simulated shopper did, in a few words: "clicked #2, bought $89", "rephrased to 'x', left". */
export function shopperWords(s) {
  const out = [];
  for (const e of s.steps ?? []) {
    if (e.t === "reformulate") out.push(`rephrased to "${e.query}"`);
    else if (e.t === "click") out.push(`clicked #${e.rank}`);
    else if (e.t === "purchase") out.push(`bought${e.price ? ` $${Number(e.price).toFixed(0)}` : ""}`);
  }
  if (!out.length) out.push(s.results === 0 ? "no results, left" : "looked, left");
  else if (!s.bought && !out.some((x) => x.startsWith("clicked"))) out.push("left");
  return out.join(", ");
}

/** Lines that share one scale: [{name, color, points: [{x, y}]}]. */
function Lines(c, series, { height = 150 } = {}) {
  const box = div().relative().w_full().h(height).border_1().border_color(c.rule);
  const paint = globalThis.window?.paint_path;
  const all = series.flatMap((s) => s.points);
  if (!paint || all.length < 2) return box;
  const xs = all.map((p) => p.x), ys = all.map((p) => p.y);
  const x0 = Math.min(...xs), xspan = Math.max(...xs) - x0 || 1;
  const y0 = Math.min(...ys), y1 = Math.max(...ys), yspan = y1 - y0 || Math.abs(y1) || 1;
  const X = (v) => `${(3 + ((v - x0) / xspan) * 94).toFixed(2)}%`;
  const Y = (v) => `${(8 + (1 - (v - y0) / yspan) * 80).toFixed(2)}%`;
  for (const s of series) {
    if (s.points.length < 2) continue;
    let line = PathBuilder.stroke(2);
    s.points.forEach((p, k) => (line = k ? line.line_to(X(p.x), Y(p.y)) : line.move_to(X(p.x), Y(p.y))));
    box.child(div().absolute().inset_0().child(paint(line.build(), s.color).size_full()));
  }
  return box;
}

export class Live {
  constructor(host) {
    this.host = host;
    this.list = null; // the runs, newest first
    this.sel = null;
    this.run = null; // {head, events: {stage, metric: Map, shoppers, logs, result}, next}
    this.chart = null; // the metric whose line is drawn
    this.error = null;
    this.active = 0; // runs still going, for the tab's count
    this.running = []; // those runs, for "Watch it live" on a backtest
  }

  load(cx, pick = null) {
    cx.spawn(async (cx) => {
      const r = await api("/api/live?limit=40");
      if (r.ok) {
        this.host.starting = false; // the services answered: Runs stops waiting for them
        this.list = r.data ?? [];
        this.running = this.list.filter((x) => going(x.status));
        this.active = this.running.length;
        this.error = null;
        const want = pick ?? this.sel;
        if (want && this.list.some((x) => x.id === want)) this.select(cx, want);
        // Asked for with nothing named ("Running now", an older Live link): the one going, else the newest.
        else if (!this.sel && this.list.length && this.host.kind === "live") this.select(cx, (this.list.find((x) => going(x.status)) ?? this.list[0]).id);
      } else if (r.status) this.error = r; // the backend answered with a problem; otherwise it's still starting: ask again
      cx.notify();
    });
  }

  /** The count on the tab, asked for every few seconds wherever Runs is. */
  count(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/live?active=true&limit=50");
      if (!r.ok) return;
      const was = this.running.map((x) => x.id).join();
      this.running = r.data ?? [];
      if (this.running.length !== this.active || this.running.map((x) => x.id).join() !== was) { this.active = this.running.length; cx.notify(); }
    });
  }

  select(cx, id) {
    if (this.sel === id && this.run) return;
    this.sel = id;
    this.run = null;
    this.chart = null;
    this.fetch(cx);
    cx.notify();
  }

  /** New events since the last ask. Called every second while the open run is going. */
  tick(cx) {
    // A running run's time counts up every second on the page's own clock, heard from or not.
    if (this.run && !this.run.error && going(this.run.head?.status)) cx.notify();
    if (!this.sel || this.busy) return;
    if (this.run && !this.run.error && !going(this.run.head.status)) { this.askCost(cx); return; }
    this.fetch(cx);
  }

  /** A finished run that went out as a job: what the job cost. The cost is written a moment after the job ends, so
   *  this asks a few times, a few seconds apart, and then stops. */
  askCost(cx) {
    const run = this.run;
    const job = run?.head?.job;
    if (!job || run.cost || run.costAsking || (run.costTries ?? 0) >= 6 || Date.now() - (run.costAt ?? 0) < 4000) return;
    run.costAsking = true;
    run.costAt = Date.now();
    run.costTries = (run.costTries ?? 0) + 1;
    cx.spawn(async (cx) => {
      const r = await api(`/api/jobs/${encodeURIComponent(job)}`, { timeout: 60 });
      run.costAsking = false;
      if (r.ok && costText(r.data?.cost)) { run.cost = r.data.cost; cx.notify(); }
    });
  }

  fetch(cx) {
    const id = this.sel;
    this.busy = true;
    cx.spawn(async (cx) => {
      const since = this.run?.next ?? 0;
      const r = await api(`/api/live/${encodeURIComponent(id)}?since=${since}`);
      this.busy = false;
      if (this.sel !== id) return;
      if (!r.ok) { if (!this.run) this.run = { error: r }; cx.notify(); return; }
      if (!this.run || this.run.error) this.run = { head: r.data.run, next: 0, stages: [], metrics: new Map(), shoppers: [], logs: [], result: null, last: 0 };
      const run = this.run;
      const was = run.head.status;
      run.head = r.data.run;
      run.next = r.data.next;
      for (const e of r.data.events ?? []) this.add(run, e);
      // The list row follows the open run; a run that just ended shows its final state there too.
      const row = (this.list ?? []).find((x) => x.id === id);
      if (row) Object.assign(row, run.head);
      if (going(was) && !going(run.head.status)) this.host.onLiveEnded?.(cx, run.head);
      this.active = (this.list ?? []).filter((x) => going(x.status)).length;
      cx.notify();
    });
  }

  add(run, e) {
    run.last = Math.max(run.last, e.at ?? 0);
    if (e.type === "stage") { if (!run.stages.includes(e.name)) run.stages.push(e.name); }
    else if (e.type === "metric") {
      const key = `${e.name}\u0000${e.arm ?? ""}`;
      if (!run.metrics.has(key)) run.metrics.set(key, { name: e.name, arm: e.arm ?? null, points: [], value: null });
      const m = run.metrics.get(key);
      m.value = e.value;
      if (e.step != null) { m.points.push({ x: Number(e.step), y: Number(e.value) }); if (m.points.length > KEEP.points) m.points.splice(0, m.points.length - KEEP.points); }
    } else if (e.type === "shopper") { run.shoppers.push(e); if (run.shoppers.length > KEEP.shoppers) run.shoppers.splice(0, run.shoppers.length - KEEP.shoppers); }
    else if (e.type === "log") { run.logs.push(e); if (run.logs.length > KEEP.logs) run.logs.splice(0, run.logs.length - KEEP.logs); }
    else if (e.type === "result") run.result = e;
  }

  // ---- drawing --------------------------------------------------------------------------------------------

  detail(c, w) {
    const run = this.run;
    if (!this.sel) return v_flex().flex_1().p(24).text_size(13).text_color(c.grey500).child(Array.isArray(this.list) && !this.list.length ? "" : "Pick a run.");
    if (!run) return v_flex().flex_1().child(Skeleton(c, 8));
    if (run.error) return v_flex().flex_1().p(24).text_size(13).text_color(c.fail).child("Couldn't read this run.");
    const h = run.head;
    const icon = this.host.icon.bind(this.host);
    const [ic, kindWord] = KIND[h.kind] ?? KIND.custom;
    const [sic, sword] = STATUS[h.status] ?? STATUS.running;
    const k = clock(h, run.last);
    const pct = k.progress;
    const left = h.status === "running" && pct && pct > 0.02 ? (k.elapsed / pct) * (1 - pct) : null;
    // When the last log line came, against the clock: a step silent for a while gets the quiet line under the log.
    const heard = run.logs.length ? Number(run.logs[run.logs.length - 1].at ?? 0) : (h.stage_at ?? 0);
    const still = going(h.status) && k.elapsed - Math.max(heard, h.stage_at ?? 0) >= 8 ? stillWords(k) : null;
    const arms = h.arms?.length ? h.arms : [...new Set([...run.metrics.values()].map((m) => m.arm).filter(Boolean))];
    return v_flex().id("live-detail").flex_1().min_w(0).h_full().overflow_y_scrollbar().px(20).py(14).gap(14)
      // What it is and where it stands.
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(icon(ic, 16, c.navy))
        // A deploy's title is a sentence, in the page's font. A run named after a thing keeps that name in the code font.
        .child(div().id("live-title").min_w(0).truncate().text_size(15).font_medium().when(h.kind !== "deploy", (x) => x.font_family(MONO)).child(h.title))
        .when(h.kind !== "deploy", (x) => x.child(this.host.badge(c, kindWord)))
        .child(h_flex().gap(4).items_center().text_size(12).text_color(h.status === "failed" ? c.fail : going(h.status) ? c.blue : c.grey900)
          .child(icon(sic, 13)).child(div().child(h.where && h.where !== "local" ? `${sword}, on ${h.where}` : sword)))
        .child(div().flex_1())
        .child(div().text_size(12).text_color(c.grey500).child([k.elapsed ? `${clockWords(k.elapsed)} in` : null, left != null ? `about ${secs(left)} left` : null].filter(Boolean).join(", ")))
        .when(!going(h.status) && h.kind === "backtest" && !!h.of, (e) => e.child(Button(c, "live-open-result", "Open the result", (_e, cx) => this.host.openBacktest(cx, h.of), { small: true }))))
      .when(arms.length > 0, (e) => e.child(h_flex().flex_wrap().gap(14).text_size(12).text_color(c.grey900)
        .children(arms.map((a, i) => h_flex().gap(6).child(div().text_color(c.grey500).child(String.fromCharCode(65 + i))).child(div().font_family(MONO).text_color(c.navy).child(a))))))
      .child(this.progress(c, h, pct, k))
      .when(!!h.error || !!h.note, (e) => e.child(div().text_size(12.5).text_color(h.error ? c.fail : c.grey900).child(h.error ?? h.note)))
      .when(Boolean(costText(run.cost)), (e) => e.child(h_flex().id("live-cost").gap(6).items_center().text_size(12.5).text_color(c.navy)
        .child(icon(run.cost.free ? "compute" : "cloud", 13, c.grey500)).child(div().child(costText(run.cost)))))
      .when(run.metrics.size > 0, (e) => e.child(this.metrics(c, run, arms)))
      .when(run.shoppers.length > 0, (e) => e.child(this.shoppers(c, run, arms, w)))
      .when(run.logs.length > 0, (e) => e.child(this.logs(c, run)))
      .when(!!still, (e) => e.child(h_flex().id("live-still").gap(6).items_center().text_size(12.5).text_color(c.grey900)
        .child(icon("running", 12, c.blue)).child(div().child(still))))
      .when(!!run.result?.summary, (e) => e.child(div().text_size(12.5).text_color(c.navy).child(run.result.summary)));
  }

  progress(c, h, pct, k) {
    const stages = stepsOf(h).map((s) => s.title);
    const cur = stages.indexOf(h.stage);
    const icon = this.host.icon.bind(this.host);
    return v_flex().gap(8)
      .child(h_flex().gap(10).items_center()
        .child(div().flex_1().h(8).bg(c.surface2).border_1().border_color(c.rule)
          .child(div().h_full().w(`${Math.max(0, Math.min(100, (pct ?? 0) * 100))}%`).bg(h.status === "failed" ? c.fail : c.blue)))
        .child(div().w(44).flex_none().text_right().text_size(12).font_family(MONO).child(pct != null ? `${Math.round(pct * 100)}%` : "")))
      .when(stages.length > 0, (e) => e.child(h_flex().flex_wrap().gap(6).children(stages.map((s, i) => {
        const done = h.status === "done" || i < cur;
        const now = i === cur && h.status === "running";
        return h_flex().id(`live-stage-${i}`).h(24).px(8).gap(6).items_center().text_size(11.5)
          .border_1().border_color(now ? c.blue : c.rule).bg(now ? c.blueLight : c.surface).text_color(now ? c.blue : done ? c.navy : c.grey500)
          .child(icon(done ? "done" : now ? "running" : "queued", 12)).child(div().child(s))
          // The running step's own time, and what is usual for it when the run says.
          .when(now && k?.stepElapsed != null, (x) => x.child(div().id("live-stage-time").text_color(c.grey900)
            .child(`${clockWords(k.stepElapsed)}${k.usually ? `, usually ${k.usually}` : ""}`)));
      }))));
  }

  /** The latest of each metric, arms side by side, and the picked metric's line as it fills in. */
  metrics(c, run, arms) {
    const names = [...new Set([...run.metrics.values()].map((m) => m.name))];
    const lined = names.filter((n) => [...run.metrics.values()].some((m) => m.name === n && m.points.length > 1));
    const chart = lined.includes(this.chart) ? this.chart : lined[0] ?? null;
    const colors = [c.grey500, c.blue, c.navy, c.fail];
    const armIx = (a) => Math.max(0, arms.indexOf(a));
    return v_flex().gap(10)
      .child(h_flex().flex_wrap().gap(8).children(names.map((n) => {
        const ms = [...run.metrics.values()].filter((m) => m.name === n).sort((a, b) => armIx(a.arm) - armIx(b.arm));
        const on = n === chart;
        return v_flex().id(`live-m-${n}`).min_w(150).gap(4).px(10).py(8).border_1().border_color(on ? c.blue : c.rule)
          .when(lined.includes(n), (e) => e.cursor_pointer().on_click((_e, cx) => { this.chart = n; cx.notify(); }))
          .child(div().text_size(11.5).text_color(c.grey500).child(label(n)))
          .child(h_flex().gap(10).items_baseline().children(ms.map((m) => h_flex().gap(4).items_baseline()
            .when(!!m.arm && arms.length > 1, (e) => e.child(div().text_size(11).text_color(c.grey500).child(String.fromCharCode(65 + armIx(m.arm)))))
            .child(div().text_size(15).font_family(MONO).text_color(m.arm && armIx(m.arm) > 0 ? c.blue : c.navy).child(fmt(n, m.value))))));
      })))
      .when(!!chart, (e) => {
        const series = [...run.metrics.values()].filter((m) => m.name === chart).map((m) => ({ name: m.arm ?? chart, color: colors[armIx(m.arm) % colors.length], points: m.points }));
        return e.child(v_flex().gap(4)
          .child(Lines(c, series))
          .child(h_flex().gap(12).text_size(11).text_color(c.grey500)
            .child(div().child(`${label(chart)}, as ${run.head.kind === "backtest" ? "shoppers come in" : "the run goes on"}`))
            .children(series.map((s) => h_flex().gap(4).items_center().child(div().w(10).h(2).bg(s.color)).child(div().font_family(MONO).child(bare(s.name)))))));
      });
  }

  /** The newest simulated shoppers, each one's session on every arm in one row. */
  shoppers(c, run, arms, w) {
    const byShopper = new Map();
    for (const s of run.shoppers) {
      const k = String(s.shopper);
      if (!byShopper.has(k)) byShopper.set(k, {});
      byShopper.get(k)[s.arm] = s;
    }
    const cols = arms.length ? arms : [...new Set(run.shoppers.map((s) => s.arm))];
    const rows = [...byShopper.entries()].slice(-10).reverse();
    const cell = (s) => !s ? div().flex_1().min_w(0).text_size(12).text_color(c.grey300).child("on its way")
      : v_flex().flex_1().min_w(0).gap(2)
        .child(div().min_w(0).truncate().text_size(12.5).font_family(MONO).text_color(c.navy).child(`"${s.query ?? ""}"`))
        .child(h_flex().gap(6).items_center().text_size(12).text_color(s.bought ? c.good : s.clicked ? c.navy : c.grey500)
          .child(this.host.icon(s.bought ? "done" : s.clicked ? "up" : "flat", 12))
          .child(div().min_w(0).truncate().child(`${s.results == null ? "" : `${int(s.results)} result${s.results === 1 ? "" : "s"}, `}${shopperWords(s)}`)));
    return v_flex().gap(6)
      .child(h_flex().gap(8).items_center()
        .child(div().text_size(13).font_semibold().child("Simulated shoppers"))
        .child(div().text_size(12).text_color(c.grey500).child(`the newest, each on every version${run.head.status === "running" ? ", as they shop" : ""}`)))
      .child(v_flex().border_1().border_color(c.rule)
        .child(h_flex().h(26).px(10).gap(12).items_center().bg(c.surface2).border_b(1).border_color(c.rule).text_size(11.5).text_color(c.grey500)
          .child(div().w(72).flex_none().child("Shopper"))
          .children(cols.map((a, i) => div().flex_1().min_w(0).truncate().font_family(MONO).child(`${String.fromCharCode(65 + i)}  ${a}`))))
        .children(rows.map(([k, by], i) => h_flex().id(`live-shop-${i}`).px(10).py(6).gap(12).items_start().border_b(1).border_color(c.rule)
          .child(v_flex().w(72).flex_none().text_size(11.5).text_color(c.grey500)
            .child(div().font_family(MONO).child(`#${k}`))
            .child(div().child(Object.values(by)[0]?.device ?? "")))
          .children(cols.map((a) => cell(by[a]))))));
  }

  logs(c, run) {
    return v_flex().gap(6)
      .child(div().text_size(13).font_semibold().child("Log"))
      .child(v_flex().id("live-log").max_h(220).overflow_y_scrollbar().p(8).gap(1).border_1().border_color(c.rule).bg(c.surface2)
        .children(run.logs.slice(-120).map((l, i) => h_flex().id(`live-log-${i}`).gap(8).text_size(11.5).font_family(MONO)
          .child(div().w(54).flex_none().text_right().text_color(c.grey500).child(`${Number(l.at ?? 0).toFixed(1)}s`))
          .child(div().flex_1().min_w(0).text_color(l.level === "error" ? c.fail : c.navy).child(hideAccount(l.text))))));
  }
}

function ago(iso) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "";
  const s = (Date.now() - t) / 1000;
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
}
