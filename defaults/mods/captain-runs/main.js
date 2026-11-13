// CAPTAIN: Runs, native. Two submenus and no more (Edgar, 2026-11-12):
// - Runs: everything that runs, in one list with a kind column and filters: offline evaluations, jobs on any
//   compute target, rating queues, workflows and off-policy evaluations, with what is running now at the top. A row
//   opens it beside the list. An offline evaluation opens with its comparison in place (comparison.js, which was the
//   Compare screen): the verdict in one of three words, Better, No clear difference or Worse, then the numbers.
// - Compare: regression testing (regression.js). Every evaluation run, newest first, with the change against the
//   model it is compared with; one model's runs alone; and any two runs side by side.
// Every list here is short; anything long (a run's per-query numbers) is paged from the services.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, run, shared, openOverlay, props as layoutProps } from "herdr";
import { Curve } from "./kit/chart.js";
import { api, retry, details, watchWorkspace, watchScope, keepFresh } from "./kit/cst.js";
import { watchView } from "./kit/view.js";
import { SendToAgent } from "./kit/context.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Loading, Skeleton, Button, Submenus } from "./kit/ui.js";
import { askAgent, copyPrompt, openForm, EVALUATION_FORM } from "./kit/agent.js";
import { Jobs, jobWords, jobState, place } from "./jobs.js";
import { Live, liveKind, going } from "./live.js";
import { Comparison } from "./comparison.js";
import { Regression } from "./regression.js";
import { metricWords } from "./kit/words.js";
import { int } from "./kit/num.js";
import { costState, loadPlaces, refreshEstimate, needsConfirm, costSlice, costText, placeChanged, RunsOn, LOCAL } from "./kit/cost.js";

const ICON = (n) => `mods/captain-runs/icons/${n}.svg`;
const enc = (name) => name.split("/").map(encodeURIComponent).join("/");
const GROUPS = new Set(["backtest", "data", "sql", "queries", "judge", "sim", "deploy", "compute", "label", "monitor", "bench", "pipeline", "demo", "secret", "conn", "cost", "propose", "index", "workflow", "grade", "replay", "workspace", "lineage", "models", "skill", "cloud", "sync"]);
const VERB = { add: "add", new: "add", create: "add", list: "index", rm: "rm", remove: "rm", run: "run", show: "show", test: "test" };
const STATUS = { done: "done", failed: "failed", error: "failed", skipped: "skipped", queued: "queued", running: "running" };
const GRADES = [[0, "Irrelevant"], [1, "Partial"], [2, "Relevant"], [3, "Exact"]];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const fmtD = (x) => (x === null || x === undefined ? "" : `${x >= 0 ? "+" : ""}${Number(x).toFixed(4)}`);
const when = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "");
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** "0 7 * * 1" as "at 07:00 UTC on Monday"; anything else as the cron itself. */
function cronEnglish(cron) {
  const p = String(cron ?? "").trim().split(/\s+/);
  if (p.length !== 5 || !/^\d+$/.test(p[0]) || !/^\d+$/.test(p[1]) || p[2] !== "*" || p[3] !== "*") return cron ? `cron ${cron}` : null;
  const at = `at ${p[1].padStart(2, "0")}:${p[0].padStart(2, "0")} UTC`;
  if (p[4] === "*") return `${at} every day`;
  if (/^\d$/.test(p[4])) return `${at} on ${DAYS[+p[4] % 7]}`;
  return `${at}, days ${p[4]}`;
}

/** A failed call in words for people: the backend's own sentence when it answered, else a plain one. Transport
 *  details (hosts, ports, tool output) never show: they go behind Copy details. */
function say(r, fallback, missing) {
  if (r?.timedOut) return "This took too long.";
  // The services went away or never came up: the kit's own plain sentence ("Captain's services stopped.").
  // Belt and braces: anything that reads like a tool's or transport's output never shows.
  if (r?.failed && r.error && !/curl|127\.0|localhost|\bport\b|https?:|error sending|traceback|engine|daemon/i.test(r.error)) return String(r.error);
  const e = String(r?.error ?? "");
  // A route this backend doesn't have answers FastAPI's bare "Not Found": not a sentence for people.
  // A storage key or path ("backtest-runs/x/v1/MANIFEST.yaml") isn't a sentence either: it goes to Copy details.
  const pathy = /^[\w.@-]+(\/[\w.@-]+)+$/.test(e.trim()) || /\.(ya?ml|json|parquet|csv)$/i.test(e.trim());
  if (r?.status === 404 && missing) return missing;
  if (r?.status && e && !pathy && !/^HTTP \d+$/.test(e) && !/^(Not Found|Method Not Allowed|Internal Server Error)$/i.test(e)) return e;
  if (/time ?out|timed out/i.test(e)) return "This took too long.";
  return fallback;
}

/** A section that couldn't load: one plain sentence, Retry, and Copy details. A skeleton while still starting. */
function trouble(c, id, r, fallback, onRetry) {
  if (r?.starting) return Skeleton(c);
  return v_flex().w_full().p(14).gap(10).bg(c.failLight).border_1().border_color(c.fail)
    .child(div().text_size(13).text_color(c.fail).child(say(r, fallback)))
    .child(h_flex().gap(8)
      .child(Button(c, `${id}-retry`, "Retry", (_e, cx) => { if (!r?.status) retry(); onRetry?.(cx); }, { kind: "primary", small: true }))
      .when(!r?.status, (e) => e.child(Button(c, `${id}-copy`, "Copy details", (_e, cx) => cx.write_to_clipboard(details(r?.error ?? null)), { small: true }))));
}

/** What a workflow step does, in words: "Run backtest demo-tuned-vs-bm25", never the command line. */
function stepWords(command) {
  const parts = (command ?? []).filter((x) => !String(x).startsWith("-"));
  const [noun, verb, ...rest] = parts.map(String);
  const what = rest.length ? ` ${rest.join(" ")}` : "";
  const VERBS = {
    "backtest run": "Run offline evaluation", "backtest plan": "Plan offline evaluation", "dataset import": "Import dataset",
    "dataset create": "Create dataset", "label define": "Define label", "label build": "Build label",
    "grade start": "Start rating", "grading start": "Start rating", "replay run": "Run off-policy evaluation",
    "pipeline publish": "Publish model version", "deploy promote": "Promote", "handoff create": "Send to launch review",
  };
  const known = VERBS[`${noun} ${verb}`];
  if (known) return `${known}${what}`;
  const words = [verb, noun].filter(Boolean).join(" ");
  return words ? `${words[0].toUpperCase()}${words.slice(1)}${what}` : "Step";
}

/** A versioned name as people read it: the name, then a small version chip. Accepts "name@v3" or (name, 3). */
function named(c, name, version, { size = 12.5, color, mono = true } = {}) {
  if (version == null) {
    const m = /^(.*)@v(\d+)$/.exec(String(name ?? ""));
    if (m) [name, version] = [m[1], m[2]];
  }
  return h_flex().min_w(0).gap(6).items_center()
    .child(div().min_w(0).truncate().text_size(size).when(mono, (e) => e.font_family(MONO)).text_color(color ?? c.navy).child(String(name ?? "")))
    .when(version != null, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10.5).font_family(MONO).text_color(c.grey900).child(`v${version}`)));
}
const bare = (ref) => String(ref ?? "").replace(/@v\d+$/, "");

/** What an agent should look into for a backtest result. */
function backtestPrompt(name, b, r) {
  const p = (r?.comparisons ?? []).find((x) => x.primary) ?? r?.comparisons?.[0];
  return [
    `In Captain, look at the offline evaluation ${name} (cst backtest): it compares ${(b.arms ?? []).map(bare).join(" against ")}.`,
    b.preregistration?.hypothesis ? `The hypothesis was: ${b.preregistration.hypothesis}` : "",
    p ? `On ${metricWords(p.metric)}, B minus A is ${p.delta} with a 95% interval of [${p.ci_low}, ${p.ci_high}], verdict ${p.verdict}.` : "",
    "Explain what drives the difference: which queries moved and why, and whether the result can be trusted.",
    "Use the cst CLI to read the run. Change nothing without asking me.",
  ].filter(Boolean).join(" ");
}

/** The submenus. */
const SUBMENUS = [{ id: "runs", label: "Runs" }, { id: "compare", label: "Compare" }];
/** The kinds of thing the one list holds: [the state key, the word in the Kind column, the filter's label, the icon]. */
export const KINDS = [
  ["backtests", "Offline evaluation", "Offline evaluations", "backtest"],
  ["jobs", "Job", "Jobs", "compute"],
  ["grading", "Rating", "Rating", "grade"],
  ["workflows", "Workflow", "Workflows", "workflow"],
  ["replays", "Off-policy evaluation", "Off-policy", "replay"],
];
const KIND_WORD = Object.fromEntries(KINDS.map(([k, word]) => [k, word]));
/** Earlier tab names that still open something (a layout, `cst app open runs --tab jobs`, an older link). */
const EARLIER_TAB = { live: "live", backtests: "backtests", workflows: "workflows", grading: "grading", replays: "replays", jobs: "jobs" };

const EMPTY = {
  backtests: ["No offline evaluations yet", "An offline evaluation runs two versions of your search on the same queries and tells you whether the difference is real.", "Run an offline evaluation", "backtest"],
  workflows: ["No workflows yet", "A workflow runs steps on a schedule, such as importing fresh logs and re-running an offline evaluation each night. Ask an agent to set one up.", null, null],
  grading: ["Nothing to rate yet", "Rating asks people which products are relevant for a query. Their answers become judgments offline evaluations can trust.", "Start rating", "grading"],
  replays: ["No off-policy evaluations yet", "Off-policy evaluation (counterfactual replay of logged traffic) re-scores searches people really made, against a new version of your search.", null, null],
};

export default class CaptainRuns extends View {
  init(_props, cx) {
    this.tab = "runs"; // "runs" | "compare"
    this.kind = null; // what the right side shows: backtests | workflows | grading | replays | jobs | live
    this.filter = null; // the list's filter: a kind, "live" (running now), or null for everything
    this.width = 1000;
    this.data = {};
    this.sel = {};
    this.detail = {};
    this.open = {};
    this.job = null;
    this.error = null;
    this.grader = null;
    this.gradeNote = InputState.new({ placeholder: "Note, optional" });
    this.publishName = InputState.new({ placeholder: "judgments dataset name" });
    this.keys = cx.focus_handle();
    // Where an offline evaluation or a workflow runs and what that costs (kit cost.js): the row over "Run".
    this.cost = { backtests: costState(), workflows: costState() };
    this.loadPlaces(cx);
    cx.timer.every(500, (cx) => this.priceNow(cx));
    // The compute picker on the cloud chip changed where runs go: the "Runs on" row follows.
    let place = placeChanged();
    cx.timer.every(500, (cx) => { const now = placeChanged(); if (now !== place) { place = now; this.cost.backtests.picked = false; this.loadPlaces(cx); } });
    cx.timer.every(1000, (cx) => this.pollJob(cx));
    // Compute jobs: their state and log lines. Live runs: what's running now, drawn as it goes (live.js).
    this.jobs = new Jobs(this);
    this.live = new Live(this);
    // An evaluation's comparison, in place (comparison.js), and regression testing (regression.js).
    this.cmp = new Comparison(this);
    this.reg = new Regression(this);
    cx.timer.every(150, (cx) => { if (this.tab === "runs" && this.kind === "backtests") this.cmp.tick(cx); });
    this.loadAll(cx);
    cx.timer.every(1000, (cx) => { if (this.kind === "live") this.live.tick(cx); });
    cx.timer.every(4000, (cx) => this.live.load(cx)); // what is running now leads the list wherever Runs is
    cx.timer.every(2000, (cx) => this.jobs.tick(cx));
    // Layout props (a layout, or the real-window check): {tab: "jobs" | "grading" | "live" | "compare", live?: id,
    // backtest?, version?, section?, query?} opens on that.
    const p = layoutProps?.() ?? {};
    if (p.tab === "compare") this.showTab(cx, "compare");
    else if (p.tab === "live") { this.kind = "live"; this.filter = "live"; this.live.load(cx, p.live ?? null); }
    else if (EARLIER_TAB[p.tab]) { this.kind = p.tab; this.filter = p.tab; }
    if (p.backtest) { this.wantRun = { name: String(p.backtest), version: p.version ?? null }; this.pick(cx, "backtests", String(p.backtest)); }
    if (p.section ?? p.cmpTab) this.cmp.show(cx, p.section ?? p.cmpTab);
    if (p.query) this.cmp.wantQuery = p.query;
    if (p.autoProfile) { this.cmp.autoProfile = true; this.cmp.sections.add("profile"); }
    // Until the backend answers, ask again every 3 s.
    // Until the list loads, ask again every 3 s: while the services start, and after any failure that wasn't the
    // backend answering (they stopped or restarted).
    cx.timer.every(3000, (cx) => { if (this.starting || (this.error && !this.errorR?.status)) this.loadAll(cx); });
    // Opened by name (the palette, the Explorer, a project page, `cst app open`): shared "captain.open"
    // {section: "runs", backtest | replay | live | tab, version?, at}. An evaluation opens with its comparison in
    // place; `tab` on an evaluation opens that section of it (queries, segments, surfaces, profile).
    const stale = shared("captain.open");
    if (stale?.at && Date.now() - stale.at > 10000) this.opened = stale.at;
    cx.timer.every(500, (cx) => {
      const o = shared("captain.open");
      // A link written for the Compare screen (section "comparisons") lands here: Runs comes forward and opens it.
      const old = o?.section === "comparisons";
      if ((o?.section !== "runs" && !old) || o.at === this.opened) return;
      this.opened = o.at;
      this.grader = null;
      this.opened1 = true;
      if (old) run("layout.select", { stack: "sections", child: "runs" });
      // The sidebar's entry: Runs' home, the list. In a narrow pane a row's page gives way to it.
      if (o.home) { this.opened1 = false; this.wantRun = null; this.showTab(cx, "runs"); return; }
      if (o.live) { this.tab = "runs"; this.kind = "live"; this.live.load(cx, o.live); }
      else if (o.backtest || o.replay) {
        const [kind, name] = o.replay ? ["replays", o.replay] : ["backtests", o.backtest];
        if (!this.data[kind]) this.load(cx, kind);
        if (o.backtest) this.wantRun = { name, version: o.version ?? null };
        this.pick(cx, kind, name);
        if (o.backtest && o.tab) this.cmp.show(cx, o.tab);
        if (o.profile) { this.cmp.autoProfile = true; this.cmp.sections.add("profile"); }
      } else if (o.tab === "compare") this.showTab(cx, "compare");
      else if (EARLIER_TAB[o.tab]) this.showKind(cx, o.tab);
      cx.notify();
    });
    // The lists stay current by themselves: every few seconds while Runs is shown, at once when it comes back into
    // view, and at once when a form changes something (a new evaluation, a new workflow). What is open stays open.
    keepFresh(cx, "runs", (cx) => {
      if (this.starting) return;
      for (const k of ["backtests", "workflows", "grading", "replays"]) this.load(cx, k);
      if (this.tab === "compare") this.reg.load(cx);
    });
    // The Explorer's pick filters the lists: load them again.
    watchScope(cx, (cx) => { this.data = {}; this.loadAll(cx); if (this.tab === "compare") this.reg.load(cx); cx.notify(); });
    // What this screen shows, for agents (cst app context): the submenu, the kind of thing open and which one.
    watchView(cx, "runs", () => ({ tab: this.tab, kind: this.tab === "runs" ? this.kind : null, selected: this.tab === "runs" ? this.selected() : null,
      backtest: this.tab === "runs" && this.kind === "backtests" ? this.sel.backtests ?? null : null,
      run_version: this.tab === "runs" && this.kind === "backtests" ? this.detail.runV ?? null : null,
      ...(this.tab === "compare" ? this.reg.slice() : {}),
      ...(this.costWant() ? { runs_on: costSlice(this.cost[this.kind]) } : {}) }));
    // Another space, another workspace: drop what was shown and load again.
    watchWorkspace(cx, (cx) => {
      this.data = {}; this.sel = {}; this.detail = {}; this.open = {}; this.grader = null; this.error = null; this.kind = null;
      this.jobs = new Jobs(this);
      this.live = new Live(this);
      this.cmp = new Comparison(this);
      this.reg = new Regression(this);
      this.cost = { backtests: costState(), workflows: costState() };
      this.loadPlaces(cx);
      this.loadAll(cx);
      if (this.tab === "compare") this.reg.load(cx);
      cx.notify();
    });
  }

  // ---- going places --------------------------------------------------------------------------------------------

  showTab(cx, id) {
    this.tab = id;
    this.grader = null;
    if (id === "compare") this.reg.load(cx);
    // Something was loaded while Compare was shown: it is what Runs opens on.
    else if (!this.kind) this.kind = ["backtests", "workflows", "grading", "replays"].find((k) => this.sel[k]) ?? null;
    cx.notify();
  }

  /** The list filtered to one kind, with its first row open (an earlier tab's name, "Open workflows"). */
  showKind(cx, kind) {
    this.tab = "runs";
    this.filter = kind;
    this.grader = null;
    if (kind === "live") { this.kind = "live"; this.live.load(cx); }
    else if (kind === "jobs") { this.kind = "jobs"; this.jobs.load(cx); }
    else { this.kind = kind; if (!this.data[kind]) this.load(cx, kind); else if (!this.sel[kind] && this.data[kind][0]) this.pick(cx, kind, this.data[kind][0].name); }
    cx.notify();
  }

  /** What the open thing is called, for the crumb in a narrow pane. */
  selectedTitle() {
    if (this.kind === "jobs") { const j = (this.jobs.list ?? []).find((x) => x.id === this.jobs.sel); return j ? jobWords(j) : null; }
    if (this.kind === "live") return (this.live.list ?? []).find((x) => x.id === this.live.sel)?.title ?? null;
    return this.selected();
  }

  /** The name of what the right side shows. */
  selected() {
    if (this.kind === "jobs") return this.jobs.sel ?? null;
    if (this.kind === "live") return this.live.sel ?? null;
    return this.kind ? this.sel[this.kind] ?? null : null;
  }

  // ---- where it runs, and what that costs -----------------------------------------------------------------------

  /** The places work can run, and the place offline evaluations use unless one names its own (Settings). */
  loadPlaces(cx) {
    const st = this.cost.backtests;
    cx.spawn(async (cx) => {
      const [, ds] = await Promise.all([loadPlaces(st), api("/api/datasets", { scoped: false })]);
      if (st !== this.cost.backtests) return; // another workspace since
      this.usual = st.usual ?? null; // the workspace's default place (kit cost.js reads it with the places)
      // Rows of each dataset by name: a query set's size decides how long an offline evaluation takes.
      this.rows = Object.fromEntries((ds.ok && Array.isArray(ds.data) ? ds.data : []).map((d) => [d.name, d.rows ?? 0]));
      this.cost.workflows.places = st.places;
      this.placeFor(this.detail.backtest);
      cx.notify();
    });
  }

  /** An offline evaluation opens on the place it would run without a pick: its own, else the one set in Settings. */
  placeFor(b) {
    const st = this.cost.backtests;
    const want = b && !b.error ? b.compute ?? this.usual ?? LOCAL : this.usual ?? LOCAL;
    st.where = (st.places ?? []).some((p) => p.value === want) ? want : LOCAL;
    st.confirming = false;
  }

  /** What the open tab would start, for the estimate: {kind, size, where}, or null when nothing here runs. */
  costWant() {
    if (this.tab !== "runs") return null;
    if (this.kind === "backtests") {
      const b = this.detail.backtest;
      if (!this.sel.backtests || !b || b.error || b.mode === "imported") return null;
      // The query set is named "name@v2:split": its row count is the whole set's, so a split reads a little high.
      const queries = this.rows?.[bare(String(b.queries ?? "").split(":")[0])] || undefined;
      return { kind: "evaluation", size: { ...(queries ? { queries } : {}), arms: (b.arms ?? []).length || 2, mode: b.mode ?? undefined }, where: this.cost.backtests.where };
    }
    // A workflow's steps run from this computer; a step that names a place in your cloud is billed there.
    if (this.kind === "workflows") return this.sel.workflows && this.detail.workflow && !this.detail.workflow.error ? { kind: "workflow", size: {}, where: LOCAL } : null;
    return null;
  }

  /** From the timer, never from render: ask for the estimate when the place or the thing picked changed. */
  priceNow(cx) {
    const st = this.cost[this.kind];
    if (!st) return;
    const want = this.costWant();
    cx.spawn(async (cx) => { if (await refreshEstimate(st, want)) cx.notify(); });
  }

  /** The "Runs on" row. An offline evaluation picks its place; a workflow shows where its steps start. */
  runsOn(c, tab) {
    const st = this.cost[tab];
    return RunsOn(c, `runs-on-${tab}`, st, {
      onPick: (p, _e, cx) => { if (st.where !== p.value) st.estimate = null; st.where = p.value; st.confirming = false; cx.notify(); this.priceNow(cx); },
      onToggle: (_e, cx) => { st.open = !st.open; cx.notify(); },
      onAdd: () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "compute" } }),
    }, tab === "workflows" ? { fixed: true, note: "Each step starts from this computer. A step set to run in your cloud is billed there, and its cost shows in Jobs when it finishes." } : {});
  }

  /** Run the open offline evaluation where the row says. Above the amount set in Settings, the first click only
   *  repeats the number. The second starts it. */
  runBacktest(cx, name) {
    const st = this.cost.backtests;
    if (needsConfirm(st)) { st.confirming = true; cx.notify(); return; }
    st.confirming = false;
    // The place is sent whenever it is not this computer, and also when this computer was picked over the usual place.
    const usual = this.detail.backtest?.compute ?? this.usual ?? LOCAL;
    const body = st.where !== LOCAL || usual !== LOCAL ? { compute: st.where } : {};
    // When it finishes its result loads; a person watching it live stays there, with "Open the result".
    this.start(cx, "Offline evaluation", `/api/backtests/${enc(name)}/run`, (cx) => { this.cmp.sel = null; this.pick(cx, "backtests", name, { show: this.kind !== "live" }); }, body);
  }

  // ---- data ---------------------------------------------------------------------------------------------------

  /** Every list the one list is made of. */
  loadAll(cx) {
    for (const k of ["backtests", "workflows", "grading", "replays"]) this.load(cx, k);
    this.jobs.load(cx); // asks each place for its jobs' state, so a row's status is true now, not as of the last look
    this.live.load(cx);
  }

  load(cx, tab) {
    if (tab === "jobs") { this.jobs.load(cx); return; }
    if (tab === "live") { this.live.load(cx); return; }
    const path = { backtests: "/api/backtests", workflows: "/api/workflows", grading: "/api/grading", replays: "/api/replays" }[tab];
    if (!path) return;
    cx.spawn(async (cx) => {
      const r = await api(path);
      // Offline evaluations decide whether the screen is still starting, or failed: they are asked for first.
      if (tab === "backtests") {
        this.starting = !r.ok && !!r.starting;
        if (this.starting) { cx.notify(); return; }
        this.error = r.ok ? null : say(r, "Couldn't load your runs.");
        this.errorR = r;
      }
      if (r.ok) this.data[tab] = r.data ?? [];
      const list = this.data[tab] ?? [];
      // Nothing open yet: open the first of this kind when it is the kind asked for, or the first evaluation.
      const want = this.kind === tab || (!this.kind && tab === "backtests");
      if (want && list.length && !list.some((x) => x.name === this.sel[tab])) this.pick(cx, tab, list[0].name, { show: this.tab === "runs" });
      cx.notify();
    });
  }

  /** Open something beside the list. `show: false` loads it without bringing it forward: a first row opened by
   *  itself while Compare is shown, or a result that came in while its run is being watched live. */
  pick(cx, tab, name, { show = true } = {}) {
    if (show) { this.tab = "runs"; this.kind = tab; this.grader = null; }
    this.sel[tab] = name;
    if (tab === "backtests") {
      this.detail.backtest = null;
      this.detail.run = null;
      cx.spawn(async (cx) => {
        const r = await api(`/api/backtests/${enc(name)}`);
        if (this.sel.backtests !== name) return;
        this.detail.backtest = r.ok ? r.data : { error: say(r, "Couldn't load this offline evaluation.") };
        this.placeFor(this.detail.backtest);
        // The run asked for with it (the palette, a project page), else its newest.
        const asked = this.wantRun?.name === name ? this.wantRun.version : null;
        this.wantRun = null;
        const v = r.ok ? (asked != null && r.data.runs?.some((x) => x.version === asked) ? asked : r.data.runs?.at(-1)?.version) : null;
        // An imported run's version is 0: still a run.
        if (v !== null && v !== undefined) this.pickRun(cx, name, v);
        else this.cmp.sel = null;
        cx.notify();
      });
    }
    if (tab === "workflows") {
      this.detail.workflow = null;
      this.detail.wfRuns = null;
      cx.spawn(async (cx) => {
        const [w, rs] = await Promise.all([api(`/api/workflows/${enc(name)}`), api(`/api/workflows/${enc(name)}/runs`)]);
        if (this.sel.workflows !== name) return;
        this.detail.workflow = w.ok ? w.data : { error: say(w, "Couldn't load this workflow.") };
        this.detail.wfRuns = rs.ok ? rs.data ?? [] : [];
        cx.notify();
      });
    }
    cx.notify();
  }

  pickRun(cx, name, v) {
    this.detail.runV = v;
    this.detail.run = null;
    this.cmp.open(cx, name, v); // its comparison, in place
    cx.spawn(async (cx) => {
      const r = await api(`/api/runs/${enc(name)}?v=${v}`);
      if (this.sel.backtests !== name || this.detail.runV !== v) return;
      this.detail.run = r.ok ? r.data : { error: say(r, "Couldn't load this run.") };
      cx.notify();
    });
  }

  start(cx, label, path, after, body = {}) {
    this.job = { label, status: "queued", message: null, after };
    cx.spawn(async (cx) => {
      const r = await api(path, { method: "POST", body });
      if (!r.ok) this.job = { label, status: "failed", message: say(r, "Couldn't start it.") };
      else {
        this.job.id = r.data?.job;
        // A run that streams: watch it in Live.
        if (r.data?.live) { this.kind = "live"; this.opened1 = true; this.grader = null; this.live.load(cx, r.data.live); }
      }
      cx.notify();
    });
    cx.notify();
  }

  pollJob(cx) {
    const j = this.job;
    if (!j?.id || j.status === "done" || j.status === "failed" || j.polling) return;
    j.polling = true;
    cx.spawn(async (cx) => {
      const r = await api(`/api/jobs/${encodeURIComponent(j.id)}`);
      j.polling = false;
      if (r.ok) {
        j.status = r.data.status;
        j.message = r.data.message ?? r.data.log?.at(-1) ?? null;
        if (j.status === "done" || j.status === "failed") j.after?.(cx);
      }
      cx.notify();
    });
  }

  /** From a finished live run to its result. */
  openBacktest(cx, name) {
    this.cmp.sel = null; // a finished run: read its result afresh
    this.load(cx, "backtests");
    this.pick(cx, "backtests", name);
    cx.notify();
  }

  /** A live run ended: what it made shows in its list without a refresh. */
  onLiveEnded(cx, head) {
    if (head.kind === "backtest") delete this.data.backtests;
  }

  openGrader(cx, name) {
    this.grader = { name, next: null, count: 0, error: null, busy: false, rubric: null };
    this.nextPair(cx);
    // The queue's rubric (captain/rubrics/<domain>.md), so every grader reads the same words beside each pair.
    cx.spawn(async (cx) => {
      const r = await api(`/api/grading/${encodeURIComponent(name)}`);
      if (this.grader?.name === name && r.ok && r.data?.rubric?.text) { this.grader.rubric = r.data.rubric; cx.notify(); }
    });
    this.keys.focus();
  }

  nextPair(cx) {
    const g = this.grader;
    cx.spawn(async (cx) => {
      const r = await api(`/api/grading/${encodeURIComponent(g.name)}/next`);
      g.next = r.ok ? r.data : null;
      g.error = r.ok ? null : say(r, "Couldn't load the next pair.");
      cx.notify();
    });
  }

  grade(cx, value) {
    const g = this.grader;
    if (!g?.next?.pair || g.busy) return;
    g.busy = true;
    const note = this.gradeNote.value().trim();
    cx.spawn(async (cx) => {
      const r = await api(`/api/grading/${encodeURIComponent(g.name)}/grade`, { method: "POST", body: { pair: g.next.pair.id, grade: value, ...(note ? { note } : {}) } });
      g.busy = false;
      if (!r.ok) g.error = say(r, "Couldn't save the grade.");
      else {
        g.count++;
        this.gradeNote.set_value("");
        const n = await api(`/api/grading/${encodeURIComponent(g.name)}/next`);
        g.next = n.ok ? n.data : null;
      }
      cx.notify();
    });
    cx.notify();
  }

  publish(cx, q) {
    const into = this.publishName.value().trim() || `${q.name}-human`;
    this.published = { queue: q.name, status: "working" };
    cx.spawn(async (cx) => {
      const r = await api(`/api/grading/${encodeURIComponent(q.name)}/publish`, { method: "POST", body: { into } });
      this.published = r.ok ? { queue: q.name, status: "done", ref: `${r.data.name}@v${r.data.version}` } : { queue: q.name, status: "failed", error: say(r, "Couldn't publish.") };
      this.load(cx, "grading");
      cx.notify();
    });
    cx.notify();
  }

  // ---- pieces -------------------------------------------------------------------------------------------------

  /** The plain failure panel, for the Jobs tab. */
  troubleView(c, id, r, fallback, onRetry) {
    return trouble(c, id, r, fallback, onRetry);
  }

  icon(n, size = 14, color) {
    return svg(ICON(n)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  /** A command's icons: its group, and its verb when the verb is clear. */
  commandIcon(path, color) {
    const g = GROUPS.has(path[0]) ? path[0] : "command";
    const v = path.length > 1 ? VERB[path[path.length - 1]] : null;
    return h_flex().flex_none().gap(2).items_center().child(this.icon(g, 14, color)).when(!!v, (e) => e.child(this.icon(v, 10, color)));
  }

  statusIcon(c, s) {
    const n = STATUS[s] ?? "queued";
    const col = n === "done" ? c.blue : n === "failed" ? c.fail : c.grey500;
    return this.icon(n, 14, col);
  }

  badge(c, text, tone) {
    const fg = tone === "win" || tone === "blue" ? c.blue : tone === "loss" || tone === "fail" ? c.fail : c.grey900;
    return div().flex_none().h(20).px(6).flex().items_center().border_1().border_color(fg).text_size(10.5).font_medium().text_color(fg).child(String(text).toUpperCase());
  }

  button(c, id, label, icon, onClick, primary = false, disabled = false) {
    return h_flex().id(id).h(28).px(10).gap(6).items_center().flex_none().text_size(12.5)
      .when(!disabled, (e) => e.cursor_pointer())
      .bg(primary ? c.blue : c.surface).text_color(primary ? c.onBlue : c.navy).border_1().border_color(primary ? c.blue : c.grey300)
      .when(disabled, (e) => e.opacity(0.5))
      .when(!disabled && !primary, (e) => e.hover((s) => s.border_color(c.blue).text_color(c.blue)))
      .when(!disabled, (e) => e.on_click(onClick))
      .child(this.icon(icon, 13)).child(div().child(label));
  }

  header(c) {
    return h_flex().min_h(52).py(8).flex_wrap().flex_none().px(20).gap(14).items_center().border_b(1).border_color(c.rule)
      .child(div().flex_none().text_size(18).font_semibold().child("Runs"))
      .child(Submenus(c, SUBMENUS, this.tab, (id, cx) => this.showTab(cx, id)))
      .child(div().flex_1())
      // One filled button: setting up an evaluation.
      .child(Button(c, "new-grading", "Start rating", () => openForm("grading"), { small: true }))
      .child(Button(c, "new-backtest", "New evaluation", () => openForm("backtest"), { kind: "primary", small: true }))
      .child(div().id("runs-refresh").size(28).flex().flex_none().items_center().justify_center().cursor_pointer()
        .border_1().border_color(c.grey300).text_color(c.grey900).hover((s) => s.text_color(c.blue).border_color(c.blue))
        .tooltip("Refresh").on_click((_e, cx) => {
          if (this.tab === "compare") { this.reg.load(cx); return; }
          this.loadAll(cx);
          if (this.kind && this.sel[this.kind]) { if (this.kind === "backtests") this.cmp.sel = null; this.pick(cx, this.kind, this.sel[this.kind]); }
        })
        .child(this.icon("refresh", 14)));
  }

  jobLine(c) {
    const j = this.job;
    if (!j) return div();
    const tone = j.status === "failed" ? c.fail : j.status === "done" ? c.blue : c.grey900;
    return h_flex().flex_none().px(20).py(8).gap(8).items_center().border_b(1).border_color(c.rule).text_size(12.5).text_color(tone)
      .child(this.statusIcon(c, j.status === "running" || j.status === "queued" ? "running" : j.status))
      .child(div().flex_none().font_medium().child(`${j.label}: ${j.status}`))
      .child(div().flex_1().min_w(0).truncate().text_color(c.grey900).child(j.message ?? ""))
      .when(j.status === "done" || j.status === "failed", (e) => e.child(div().id("job-close").cursor_pointer().text_color(c.grey500).hover((s) => s.text_color(c.navy))
        .on_click((_e, cx) => { this.job = null; cx.notify(); }).child(this.icon("x", 13))));
  }

  /** Every row of the one list: [{kind, id, title, word, sub, status, tone, at, running}]. What is running now comes
   *  first, then the newest. `word` is the Kind column; `status` is one status, in words. */
  rowsAll() {
    const out = [];
    const ms = (iso) => { const t = Date.parse(iso ?? ""); return Number.isFinite(t) ? t : 0; };
    // What is running now (live.js), whatever its kind: it leads the list.
    for (const x of this.live.list ?? this.live.running ?? []) {
      const on = going(x.status);
      if (!on && this.filter !== "live") continue; // finished live runs show under "Running now" only: their results are the rows below
      const pct = typeof x.progress === "number" ? `, ${Math.round(x.progress * 100)}%` : "";
      out.push({ kind: "live", id: x.id, title: x.title, word: liveKind(x), icon: "running", running: on, at: ms(x.started_at ?? x.created_at),
        status: x.status === "queued" ? "Waiting to start" : on ? `Running${pct}` : x.status === "failed" ? "Failed" : "Finished", tone: x.status === "failed" ? "fail" : on ? "blue" : null,
        sub: on ? [x.stage, x.where && x.where !== "local" ? `on ${x.where}` : null].filter(Boolean).join(", ") : "" });
    }
    for (const x of this.data.backtests ?? []) out.push({ kind: "backtests", id: x.name, title: x.name, word: KIND_WORD.backtests, icon: "backtest", at: ms(x.last_run ?? x.created),
      status: x.runs ? plural(x.runs, "run") : "Not run yet", sub: (x.arms ?? []).map(bare).join(" against "), tip: x.description });
    for (const j of this.jobs.list ?? []) { const [word, tone, on] = jobState(j); out.push({ kind: "jobs", id: j.id, title: jobWords(j), word: KIND_WORD.jobs, icon: j.target_kind === "local" ? "compute" : "cloud", at: ms(j.submitted_at), status: word, tone, running: on,
      // Where it ran, and what it cost once it has finished (the services' own sentence).
      sub: costText(j.cost) || place(j), subId: costText(j.cost) ? `job-cost-${j.id}` : null }); }
    for (const q of this.data.grading ?? []) out.push({ kind: "grading", id: q.name, title: q.name, word: KIND_WORD.grading, icon: "grade", at: ms(q.created_at),
      status: `${int(q.graded ?? 0)} of ${int(q.pairs ?? 0)} rated`, sub: "" });
    for (const x of this.data.workflows ?? []) out.push({ kind: "workflows", id: x.name, title: x.name, word: KIND_WORD.workflows, icon: "workflow", at: ms(x.last_run),
      status: x.last_status === "failed" ? "Last run failed" : x.last_status ? `Last run ${STATUS[x.last_status] ?? x.last_status}` : "Not run yet", tone: x.last_status === "failed" ? "fail" : null,
      sub: [plural(x.steps ?? 0, "step"), x.schedule ? cronEnglish(x.schedule) : null].filter(Boolean).join(", "), tip: x.description });
    for (const x of this.data.replays ?? []) out.push({ kind: "replays", id: x.name, title: x.name, word: KIND_WORD.replays, icon: "replay", at: ms(x.created_at),
      status: `${(x.curve ?? []).length} points`, sub: "", tip: x.description });
    const shown = out.filter((r) => !this.filter || r.kind === this.filter);
    return shown.sort((a, b) => (Number(!!b.running) - Number(!!a.running)) || (b.at - a.at) || a.title.localeCompare(b.title));
  }

  /** The one list: filters, then Name, Kind and Status. */
  runList(c, w) {
    const rows = this.rowsAll();
    const loaded = Array.isArray(this.data.backtests);
    const counts = { live: this.live.active, ...Object.fromEntries(KINDS.map(([k]) => [k, k === "jobs" ? (this.jobs.list ?? []).length : (this.data[k] ?? []).length])) };
    const chip = (id, label, n) => h_flex().id(`runs-kind-${id ?? "all"}`).h(26).px(8).gap(5).flex_none().items_center().cursor_pointer().text_size(12)
      .border_1().border_color(this.filter === id ? c.blue : c.grey300).bg(this.filter === id ? c.blueLight : c.surface).text_color(this.filter === id ? c.blue : c.grey900)
      .on_click((_e, cx) => { this.filter = id; if (id === "live") this.live.load(cx); if (id === "jobs") this.jobs.load(cx); cx.notify(); })
      .child(div().child(label)).when(!!n, (e) => e.child(div().text_size(10.5).text_color(c.grey500).child(String(n))));
    const active = (r) => r.kind === this.kind && r.id === this.selected();
    const open = (cx, r) => {
      this.grader = null;
      this.opened1 = true; // in a narrow pane, the row's page takes the place of the list
      if (r.kind === "live") { this.kind = "live"; this.live.select(cx, r.id); }
      else if (r.kind === "jobs") { this.kind = "jobs"; this.jobs.pick(cx, r.id); }
      else this.pick(cx, r.kind, r.id);
      cx.notify();
    };
    return v_flex().id("runs-list").w(w).h_full().flex_none().border_r(1).border_color(c.rule)
      .child(h_flex().id("runs-filters").flex_none().flex_wrap().gap(6).p(10).border_b(1).border_color(c.rule)
        .child(chip(null, "All"))
        .child(chip("live", "Running now", counts.live))
        // A kind the workspace has none of gets no chip: the list doesn't advertise what nobody uses.
        .children(KINDS.filter(([k]) => counts[k] > 0 || this.filter === k).map(([k, , label]) => chip(k, label, counts[k]))))
      .child(h_flex().flex_none().h(28).px(10).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().text_color(c.grey900).items_center()
        .child(div().flex_1().min_w(0).px(4).child("Name"))
        .child(div().w(128).flex_none().px(4).child("Kind"))
        .child(div().w(96).flex_none().px(4).child("Status")))
      .child(v_flex().id("runs-rows").flex_1().min_h(0).overflow_y_scrollbar()
        .when(!!this.error, (e) => e.child(div().p(10).child(trouble(c, "runs-load", this.errorR, this.error, (cx) => this.loadAll(cx)))))
        .when(!loaded && !this.error, (e) => e.child(div().child(Skeleton(c, 8))))
        .when(loaded && !rows.length && !this.error, (e) => {
          const [title, body, action, task] = EMPTY[this.filter] ?? (this.filter === "live" ? ["Nothing is running", "", null, null] : this.filter === "jobs" ? ["No jobs yet", "A job is work Captain runs once on a compute target: this computer or your cloud.", null, null]
            : ["No runs yet", "An offline evaluation runs two versions of your search on the same queries and tells you whether the difference is real.", "New evaluation", "backtest"]);
          return e.child(v_flex().id("runs-empty").p(14).gap(8)
            .child(div().text_size(13).font_medium().child(title))
            .when(!!body, (x) => x.child(div().text_size(12).line_height("18px").text_color(c.grey900).child(body)))
            .when(!!action, (x) => x.child(Button(c, `empty-${this.filter ?? "all"}`, action, () => openForm(task), { small: true })))
            // Results from your own system compare the same way (captain-rankings).
            .when(!this.filter || this.filter === "backtests", (x) => x.child(Button(c, "empty-rankings", "Compare your own rankings", () => openOverlay({ mod: "captain-rankings", kind: "dialog", width: 760, height: 660 }), { small: true, kind: "quiet" }))));
        })
        .children(rows.map((r) => {
          const on = active(r);
          const tone = r.tone === "fail" ? c.fail : r.tone === "blue" ? c.blue : r.tone === "good" ? c.good : c.grey900;
          return h_flex().id(`${r.kind === "jobs" ? "job" : r.kind}-${r.id}`).w_full().min_h(44).flex_none().px(10).py(4).items_center().cursor_pointer()
            .border_l(2).border_color(on ? c.blue : "#00000000").bg(on ? c.blueLight : c.surface).hover((st) => (on ? st : st.bg(c.surface2)))
            .tooltip(r.tip ?? r.title)
            .on_click((_e, cx) => open(cx, r))
            .child(h_flex().flex_1().min_w(0).px(4).gap(8).items_center()
              .child(this.icon(r.icon, 15, on ? c.blue : c.grey500))
              .child(v_flex().flex_1().min_w(0)
                .child(div().truncate().text_size(12.5).text_color(on ? c.blue : c.navy).child(r.title))
                .when(!!r.sub, (e) => e.child(div().when(!!r.subId, (d) => d.id(r.subId)).truncate().text_size(11).text_color(c.grey500).child(r.sub)))))
            .child(div().w(128).flex_none().px(4).truncate().text_size(12).text_color(c.grey900).child(r.word))
            .child(div().w(96).flex_none().px(4).truncate().text_size(12).text_color(tone).tooltip(r.status).child(r.status));
        })));
  }

  /** What is open, beside the list. */
  detailPane(c, w) {
    if (this.grader) return this.graderView(c);
    if (this.kind === "backtests") return this.backtestDetail(c, w);
    if (this.kind === "workflows") return this.workflowDetail(c);
    if (this.kind === "grading") return this.grading(c);
    if (this.kind === "replays") { const r = (this.data.replays ?? []).find((x) => x.name === this.sel.replays) ?? null; return r ? this.replayDetail(c, r, w) : v_flex().flex_1(); }
    if (this.kind === "jobs") return this.jobs.detail(c);
    if (this.kind === "live") return this.live.detail(c, w);
    return v_flex().id("runs-none").flex_1().p(24).text_size(13).text_color(c.grey500).child(Array.isArray(this.data.backtests) && this.rowsAll().length ? "Pick a row to open it." : "");
  }

  // ---- offline evaluations ------------------------------------------------------------------------------------

  backtestDetail(c, w) {
    const b = this.detail.backtest;
    if (!this.sel.backtests) return v_flex().flex_1();
    if (!b) return v_flex().flex_1().child(Skeleton(c, 8));
    if (b.error) return v_flex().flex_1().p(24).text_size(13).text_color(c.fail).child(b.error);
    const name = this.sel.backtests;
    const r = this.detail.run;
    const running = this.job && this.job.status !== "done" && this.job.status !== "failed";
    return v_flex().id("backtest-detail").flex_1().min_w(0).h_full().overflow_y_scrollbar().px(20).py(14).gap(12)
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(this.icon("backtest", 16, c.navy))
        .child(div().min_w(0).truncate().text_size(15).font_medium().font_family(MONO).child(name))
        .when(!!b.mode, (e) => e.child(this.badge(c, b.mode === "historic" ? "historic replay" : b.mode)))
        .child(div().flex_1())
        .when(!!this.live.running.find((x) => x.of === name), (e) => e.child(Button(c, "bt-watch", "Watch it live", (_e, cx) => {
          this.kind = "live"; this.live.load(cx, this.live.running.find((x) => x.of === name).id); cx.notify(); }, { small: true })))
        .when(b.mode !== "imported", (e) => e.child(this.button(c, "run-backtest", running ? "Running" : this.cost.backtests.confirming ? "Start anyway" : "Run evaluation", "run", (_e, cx) => this.runBacktest(cx, name), false, !!running))
          // Regression testing: the same test, on another model. The form opens on this test; only the model changes.
          .child(Button(c, "bt-rerun-model", "Run it on another model", () => openOverlay(EVALUATION_FORM({ test: name })), { small: true }))))
      .child(div().text_size(13).font_medium().child(b.preregistration?.hypothesis ?? ""))
      .when(b.mode !== "imported", (e) => e.child(this.runsOn(c, "backtests")))
      .child(h_flex().flex_wrap().gap(16).text_size(12).text_color(c.grey900)
        .children((b.arms ?? []).map((a, i) => h_flex().gap(6).items_center().child(div().text_color(c.grey500).child(String.fromCharCode(65 + i))).child(named(c, a))
          // Backtest settings (captain-sandbox): try other settings of this arm's pipeline.
          .child(div().id(`bt-whatif-${i}`).text_size(11.5).text_color(c.blue).cursor_pointer().hover((s) => s.underline())
            .on_click(() => openOverlay({ mod: "captain-sandbox", kind: "dialog", width: 0.9, height: 0.9, props: { pipeline: String(a) } })).child("Evaluation settings"))))
        .child(div().child(b.preregistration?.primary_metric ? `Judged on ${metricWords(b.preregistration.primary_metric)}${b.preregistration.mde ? `, looking for a change of at least ${b.preregistration.mde}` : ""}` : "")))
      .child(h_flex().flex_wrap().gap(6).children((b.runs ?? []).map((x) => {
        const on = x.version === this.detail.runV;
        return h_flex().id(`run-v${x.version}`).h(26).px(8).gap(6).items_center().cursor_pointer().text_size(12)
          .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
          .on_click((_e, cx) => this.pickRun(cx, name, x.version))
          .child(this.icon("run", 12)).child(div().font_family(MONO).child(`v${x.version}`))
          .child(div().text_color(c.grey500).child(when(x.created)))
          .when(x.independent === false, (e) => e.child(div().text_color(c.fail).child("not independent")))
          .when(x.complete === false, (e) => e.child(div().text_color(c.fail).child("incomplete")));
      })))
      // The comparison, in place: the verdict in one of three words, then the numbers and what is behind them.
      .child((b.runs ?? []).length ? this.cmp.render(c, Math.max(320, (w ?? 800) - 40)) : div().id("bt-no-runs").text_size(12.5).text_color(c.grey500).child("Not run yet."));
  }

  // ---- workflows ----------------------------------------------------------------------------------------------

  workflowDetail(c) {
    const w = this.detail.workflow;
    if (!this.sel.workflows) return v_flex().flex_1();
    if (!w) return v_flex().flex_1().child(Skeleton(c, 8));
    if (w.error) return v_flex().flex_1().p(24).text_size(13).text_color(c.fail).child(w.error);
    const name = this.sel.workflows;
    const runs = this.detail.wfRuns ?? [];
    const running = this.job && this.job.status !== "done" && this.job.status !== "failed";
    return v_flex().id("workflow-detail").flex_1().min_w(0).h_full().overflow_y_scrollbar().px(20).py(14).gap(12)
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(this.icon("workflow", 16, c.navy))
        .child(named(c, name, w.version, { size: 15 }))
        .when(!!w.schedule, (e) => e.child(h_flex().gap(4).items_center().text_size(12).text_color(c.grey900).child(this.icon("schedule", 12)).child(div().child(cronEnglish(w.schedule)))))
        .child(div().flex_1())
        .child(this.button(c, "run-workflow", running ? "Running" : "Run now", "run", (_e, cx) => this.start(cx, "Workflow", `/api/workflows/${enc(name)}/run`, (cx) => this.pick(cx, "workflows", name)), false, !!running)))
      .when(!!w.description, (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(w.description)))
      .child(this.runsOn(c, "workflows"))
      .child(div().text_size(13).font_medium().child("Steps"))
      .child(v_flex().border_1().border_color(c.rule).children((w.steps ?? []).map((s) => h_flex().id(`step-${s.id}`).min_h(36).px(10).py(6).gap(10).items_center().border_b(1).border_color(c.rule)
        .child(this.commandIcon(s.command, c.navy))
        .child(v_flex().flex_1().min_w(0)
          .child(h_flex().gap(8).child(div().font_family(MONO).text_size(12.5).font_medium().child(s.id))
            .child(div().min_w(0).truncate().text_size(12).text_color(c.grey900).child(stepWords(s.command))))
          .when((s.needs ?? []).length > 0 || !!s.when, (e) => e.child(div().truncate().text_size(11).text_color(c.grey500)
            .child([(s.needs ?? []).length ? `after ${s.needs.join(", ")}` : null, s.when ? `only when ${s.when}` : null].filter(Boolean).join(" · ")))))
        .tooltip(stepWords(s.command)))))
      .child(div().text_size(13).font_medium().child(`Runs (${runs.length})`))
      .when(!runs.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("Not run yet.")))
      .children(runs.slice(0, 20).map((r) => {
        const key = `${name}#${r.version}`;
        const open = !!this.open[key];
        return v_flex().border_1().border_color(c.rule)
          .child(h_flex().id(`wfrun-${r.version}`).h(34).px(10).gap(8).items_center().cursor_pointer().hover((s) => s.bg(c.surface2))
            .on_click((_e, cx) => { this.open[key] = !open; cx.notify(); })
            .child(this.icon(open ? "chevron-down" : "chevron-right", 12, c.grey500))
            .child(this.statusIcon(c, r.status))
            .child(div().font_family(MONO).text_size(12.5).child(`run ${r.version}`))
            .child(div().text_size(12).text_color(r.status === "failed" ? c.fail : c.grey900).child(r.status))
            .child(div().flex_1())
            .child(div().text_size(11.5).text_color(c.grey500).child(`${r.trigger ?? ""} ${when(r.created_at)}`)))
          .when(open, (e) => e.children((r.steps ?? []).map((s) => h_flex().id(`wfstep-${r.version}-${s.id}`).min_h(28).px(10).pl(34).py(4).gap(8).items_center().border_t(1).border_color(c.rule).text_size(12)
            .child(this.statusIcon(c, s.status))
            .child(div().w(90).flex_none().truncate().font_family(MONO).child(s.id))
            .child(div().flex_1().min_w(0).truncate().text_color(s.status === "failed" ? c.fail : c.grey900)
              .child(s.error ?? (s.status === "skipped" ? s.reason ?? "skipped" : s.command ?? "")))
            .child(div().flex_none().font_family(MONO).text_color(c.grey500).child(s.ms != null ? `${s.ms} ms` : ""))
            .tooltip(s.error ?? s.command ?? s.reason ?? s.id))));
      }));
  }

  // ---- grading ------------------------------------------------------------------------------------------------

  /** The rating queue picked in the list: how far it is, who rated, Rate and Publish. */
  grading(c) {
    if (this.grader) return this.graderView(c);
    const qs = (this.data.grading ?? []).filter((q) => q.name === this.sel.grading);
    return v_flex().id("grading").flex_1().min_w(0).min_h(0).overflow_y_scrollbar().px(20).py(14).gap(10)
      .children(qs.map((q) => {
        const pub = this.published?.queue === q.name ? this.published : null;
        return v_flex().id(`queue-${q.name}`).border_1().border_color(c.rule).p(14).gap(8)
          .child(h_flex().gap(8).items_center()
            .child(this.icon("grade", 15, c.navy))
            .child(div().flex_1().min_w(0).truncate().font_family(MONO).text_size(13).font_medium().child(q.name))
            .child(this.button(c, `publish-${q.name}`, "Publish", "publish", (_e, cx) => this.publish(cx, q), false, !q.graded))
            .child(this.button(c, `grade-${q.name}`, "Rate", "grade", (_e, cx) => this.openGrader(cx, q.name))))
          .child(h_flex().gap(10).items_center()
            .child(div().flex_1().h(8).bg(c.grey300).child(div().h_full().bg(c.blue).w(`${Math.round((q.share ?? 0) * 100)}%`)))
            .child(div().flex_none().font_family(MONO).text_size(12).child(`${int(q.graded)} of ${int(q.pairs)} pairs`)))
          .child(h_flex().flex_wrap().gap(14).text_size(12).text_color(c.grey900)
            // In words: which queries, whose results, who has graded, and whether graders agree (Cohen's kappa).
            .child(div().child(`Queries from ${String(q.queries ?? "").replace(/:(\w+)$/, " ($1)")}`))
            .child(div().child(`The top ${q.depth} results of ${(q.pipelines ?? []).join(" and ")}`))
            .child(div().child(Object.keys(q.graders ?? {}).length ? `Rated by ${Object.entries(q.graders).map(([w, n]) => `${w} (${int(n)})`).join(", ")}` : "Nobody has rated yet"))
            .child(div().tooltip("Cohen's kappa, on pairs two raters both rated").child(q.kappa === null || q.kappa === undefined
              ? `Agreement between raters: not measured until 10 pairs are rated twice (${int(q.agreement_pairs ?? 0)} so far)`
              : `Agreement between raters: ${Number(q.kappa).toFixed(2)}`)))
          .when(!!pub, (e) => e.child(h_flex().gap(10).items_center()
            .child(div().flex_1().min_w(0).text_size(12.5).text_color(pub.status === "failed" ? c.fail : c.navy)
              .child(pub.status === "done" ? `Published ${pub.ref}: judgments graded by people.` : pub.status === "failed" ? pub.error : "Publishing"))
            // Carry them on: these grades become the labels that judge a project's experiments.
            .when(pub.status === "done", (x) => x.child(Button(c, `use-labels-${q.name}`, "Use as a project's labels", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "pin", args: { pickProject: true },
              values: { slot: "labels", ref: pub.ref, project: shared("captain.scope")?.kind === "project" ? shared("captain.scope").id : undefined } } }), { small: true })))))
          .when(q.graded > 0 && !pub, (e) => e.child(h_flex().gap(8).items_center().text_size(12)
            .child(div().text_color(c.grey900).child("Publish as"))
            .child(div().id(`pubname-${q.name}`).w(240).h(28).px(8).flex().items_center().border_1().border_color(c.grey300)
              .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
              .child(Input.new(this.publishName).w_full().text_size(12).text_color(c.navy)))));
      }));
  }

  graderView(c) {
    const g = this.grader;
    const n = g.next;
    const p = n?.product ?? {};
    const field = (k) => (p[k] === undefined || p[k] === null ? null : String(p[k]));
    return v_flex().id("grader").flex_1().min_h(0).track_focus(this.keys)
      .on_key_down((e, cx) => {
        if (/^[0-3]$/.test(e.key)) this.grade(cx, Number(e.key));
      })
      .child(h_flex().h(44).flex_none().px(20).gap(10).items_center().border_b(1).border_color(c.rule)
        .child(this.button(c, "grader-back", "Back to the queue", "chevron-left", (_e, cx) => { this.grader = null; this.load(cx, "grading"); }))
        .child(div().font_family(MONO).text_size(13).child(g.name))
        .child(div().flex_1())
        .child(div().text_size(12.5).text_color(c.grey900).child(n ? (n.done ? "All rated" : `${n.remaining} left`) : "").when(g.count > 0, (e) => e.child(`, ${g.count} rated now`))))
      .child(v_flex().id("grader-body").flex_1().min_h(0).overflow_y_scrollbar().px(28).py(20).gap(16).max_w(900)
        .when(!!g.error, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(g.error)))
        .when(!n && !g.error, (e) => e.child(Skeleton(c, 6)))
        .when(!!n?.done, (e) => e.child(div().text_size(14).child("You rated every pair in this queue. Go back to it to publish.")))
        .when(!!n?.pair, (e) => e
          .child(v_flex().gap(2)
            .child(div().text_size(11).text_color(c.grey500).child("QUERY"))
            .child(div().text_size(26).font_semibold().child(n.pair.query))
            .child(div().text_size(11.5).text_color(c.grey500).child(`first seen in ${n.pair.first_seen}${n.pair.overlap ? ", also rated by others" : ""}`)))
          .child(v_flex().p(14).gap(6).border_1().border_color(c.grey300)
            .child(div().text_size(16).font_medium().child(field("title") ?? n.pair.product_id))
            .child(h_flex().flex_wrap().gap(14).text_size(12.5).text_color(c.grey900)
              .when(!!field("brand"), (e) => e.child(div().child(field("brand"))))
              .when(!!field("category"), (e) => e.child(div().child(field("category"))))
              .when(!!field("price"), (e) => e.child(div().font_family(MONO).child(`$${Number(field("price")).toFixed(2)}`)))
              .child(div().font_family(MONO).text_color(c.grey500).child(n.pair.product_id)))
            .when(!!field("description"), (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(field("description")))))
          .when(!!g.rubric?.text, (e) => e.child(v_flex().id("grader-rubric").max_h(180).overflow_y_scrollbar().p(12).gap(4).bg(c.surface2)
            .child(div().text_size(11).text_color(c.grey500).child(`RUBRIC: ${String(g.rubric.name ?? "").toUpperCase()}`))
            .child(div().text_size(12.5).line_height("19px").text_color(c.grey900).child(String(g.rubric.text)))))
          .child(h_flex().gap(8).children(GRADES.map(([v, label]) => v_flex().id(`grade-${v}`).flex_1().h(76).items_center().justify_center().gap(2).cursor_pointer()
            .border_1().border_color(v === 3 ? c.blue : c.grey300).hover((s) => s.bg(c.blueLight).border_color(c.blue))
            .on_click((_e, cx) => this.grade(cx, v))
            .child(div().text_size(20).font_family(MONO).child(String(v)))
            .child(div().text_size(13).font_medium().child(label)))))
          .child(div().id("grade-note").h(30).px(8).flex().items_center().border_1().border_color(c.grey300)
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
            .child(Input.new(this.gradeNote).w_full().text_size(12.5).text_color(c.navy)))
          .child(div().text_size(11.5).text_color(c.grey500).child("Keys 0 to 3 give the grade. Click outside the note first."))));
  }

  // ---- replays ------------------------------------------------------------------------------------------------

  replayDetail(c, r, w) {
    const pts = [...(r.curve ?? [])].sort((a, b) => a.trigger_rate - b.trigger_rate);
    const kindOf = (o) => pts[0]?.outcomes?.[o]?.kind ?? ((r.direct ?? []).includes(o) ? "direct" : "directional");
    const chartW = Math.max(260, Math.min(520, Math.floor((w - 60) / Math.max(1, Math.min(2, (r.outcomes ?? []).length)))));
    return v_flex().id("replay-detail").flex_1().min_w(0).h_full().overflow_y_scrollbar().px(20).py(14).gap(12)
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(this.icon("replay", 16, c.navy))
        .child(named(c, r.name, r.version, { size: 15 }))
        .child(this.badge(c, r.evidence ?? "")))
      .when(!!r.description, (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(r.description)))
      .child(h_flex().gap(18).text_size(12).text_color(c.grey900)
        .child(h_flex().gap(6).items_center().child(div().w(22).h(2).bg(c.navy)).child(div().child("direct: measured")))
        .child(h_flex().gap(6).items_center().child(h_flex().gap(3).child(div().w(6).h(2).bg(c.navy)).child(div().w(6).h(2).bg(c.navy)).child(div().w(6).h(2).bg(c.navy))).child(div().child("directional: which way, not how much"))))
      .child(h_flex().flex_wrap().gap(16).children((r.outcomes ?? []).map((o, i) => this.curve(c, o, kindOf(o), pts, chartW, i))))
      .child(div().text_size(13).font_medium().child("Operating points"))
      .child(v_flex().border_1().border_color(c.rule)
        .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium()
          .child(div().w(90).flex_none().px(8).text_right().child("Threshold"))
          .child(div().w(90).flex_none().px(8).text_right().child("Trigger"))
          .children((r.outcomes ?? []).map((o) => div().flex_1().min_w(120).px(8).text_right().truncate().child(`${o} (${kindOf(o)})`))))
        .children([...pts].reverse().map((p, i) => h_flex().id(`pt-${i}`).h(26).border_b(1).border_color(c.rule).text_size(12).font_family(MONO)
          .child(div().w(90).flex_none().px(8).text_right().child(Number(p.threshold).toFixed(3)))
          .child(div().w(90).flex_none().px(8).text_right().child(`${(p.trigger_rate * 100).toFixed(1)}%`))
          .children((r.outcomes ?? []).map((o) => {
            const x = p.outcomes?.[o];
            return div().flex_1().min_w(120).px(8).text_right().truncate().child(x ? `${fmtD(x.delta_vs_never)} ± ${(1.96 * x.se).toFixed(4)}` : "");
          })))))
      .when(!!r.note, (e) => e.child(div().text_size(12).text_color(c.grey900).child(r.note)));
  }

  /** One outcome's change against never treating, by trigger rate: a line, dashed when it is only directional. */
  curve(c, name, kind, pts, w, i) {
    const ys = pts.map((p) => p.outcomes?.[name]?.delta_vs_never ?? 0);
    const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
    return v_flex().id(`curve-${name}`).w(w).gap(4)
      .child(h_flex().gap(6).text_size(12).child(div().font_medium().child(name)).child(div().text_color(c.grey500).child(`(${kind})`))
        .child(div().flex_1()).child(div().font_family(MONO).text_color(c.grey500).child(`${fmtD(lo)} to ${fmtD(hi)}`)))
      // The kit's Curve (kit/chart.js): its line used to draw flat along the top in the app.
      .child(Curve(c, pts.map((p, k) => ({ x: p.trigger_rate, y: ys[k] })), { height: 140, color: [c.blue, c.navy, c.fail][i % 3], dashed: kind === "directional" }))
      .child(h_flex().text_size(10.5).text_color(c.grey500).font_family(MONO)
        .child(div().child("0%")).child(div().flex_1()).child(div().child("trigger rate")).child(div().flex_1()).child(div().child("100%")));
  }


  render() {
    const c = palette(isDark(theme()));
    const w = this.width;
    // The list takes half: a name needs room beside its kind and its status.
    const lw = Math.max(400, Math.min(620, Math.floor(w * 0.5)));
    // Never blank: the header always shows, and the body says what is happening.
    const body = this.starting ? v_flex().flex_1().items_center().justify_center().child(Loading(c, "", true))
      : this.tab === "compare" ? this.reg.render(c, w)
      // Rating a pair takes the whole width: the list steps aside until "Back to the queue".
      : this.grader ? this.graderView(c)
      // Beside an open terminal column there is no room for both: the list, then what a row opens to, with a crumb
      // back, the way Data does it.
      : w < 820
        ? (this.opened1 && this.kind
          ? v_flex().flex_1().min_h(0)
            .child(h_flex().id("runs-crumb").flex_none().h(34).px(20).gap(6).items_center().border_b(1).border_color(c.rule).text_size(12.5)
              .child(div().id("crumb-runs").text_color(c.blue).cursor_pointer().hover((s) => s.underline()).on_click((_e, cx) => { this.opened1 = false; cx.notify(); }).child("Runs"))
              .child(this.icon("chevron-right", 11, c.grey500))
              .child(div().min_w(0).truncate().text_color(c.navy).child(String(this.selectedTitle() ?? ""))))
            .child(h_flex().flex_1().min_h(0).child(this.detailPane(c, w)))
          : h_flex().flex_1().min_h(0).child(this.runList(c, w)))
        : h_flex().flex_1().min_h(0).child(this.runList(c, lw)).child(this.detailPane(c, w - lw));
    return v_flex().id("root").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      .on_size_change((size, cx) => { this.width = size.width; cx.notify(); })
      .child(this.header(c))
      .child(this.jobLine(c))
      .child(body);
  }
}
