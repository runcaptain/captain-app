// CAPTAIN: Runs, native. Backtests and their runs, workflows and their run history, human grading, and replays
// over randomized logs, in the Captain Search desktop app's look. It replaces the web view of those screens.
// Every list here is short (backtests, workflows, queues, replays); anything long (a run's per-query numbers) is
// for Comparisons, which pages it from the backend.
import { View, div, svg, PathBuilder } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, run, setShared, shared } from "herdr";
import { api, retry, details, watchWorkspace } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Loading, Skeleton, Button } from "./kit/ui.js";
import { askAgent, copyPrompt, openForm } from "./kit/agent.js";
import { metricWords } from "./kit/words.js";

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
function say(r, fallback) {
  if (r?.timedOut) return "This took too long.";
  // The services went away or never came up: the kit's own plain sentence ("Captain's services stopped.").
  // Belt and braces: anything that reads like a tool's or transport's output never shows.
  if (r?.failed && r.error && !/curl|127\.0|localhost|\bport\b|https?:|error sending|traceback|engine|daemon/i.test(r.error)) return String(r.error);
  const e = String(r?.error ?? "");
  // A route this backend doesn't have answers FastAPI's bare "Not Found": not a sentence for people.
  if (r?.status && e && !/^HTTP \d+$/.test(e) && !/^(Not Found|Method Not Allowed|Internal Server Error)$/i.test(e)) return e;
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
    "backtest run": "Run backtest", "backtest plan": "Plan backtest", "dataset import": "Import dataset",
    "dataset create": "Create dataset", "label define": "Define label", "label build": "Build label",
    "grade start": "Start grading", "grading start": "Start grading", "replay run": "Run replay",
    "pipeline publish": "Publish pipeline", "deploy promote": "Promote", "handoff create": "Hand off",
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
    `In Captain, look at the backtest ${name}: it compares ${(b.arms ?? []).map(bare).join(" against ")}.`,
    b.preregistration?.hypothesis ? `The hypothesis was: ${b.preregistration.hypothesis}` : "",
    p ? `On ${metricWords(p.metric)}, B minus A is ${p.delta} with a 95% interval of [${p.ci_low}, ${p.ci_high}], verdict ${p.verdict}.` : "",
    "Explain what drives the difference: which queries moved and why, and whether the result can be trusted.",
    "Use the cst CLI to read the run. Change nothing without asking me.",
  ].filter(Boolean).join(" ");
}

const EMPTY = {
  backtests: ["No backtests yet", "A backtest runs two versions of your search on the same queries and tells you whether the difference is real.", "Run a backtest", "backtest"],
  workflows: ["No workflows yet", "A workflow runs steps on a schedule, such as importing fresh logs and re-running a backtest each night. Ask an agent to set one up.", null, null],
  grading: ["Nothing to grade yet", "Grading asks people which products are relevant for a query. Their answers become judgments backtests can trust.", "Start grading", "grading"],
  replays: ["No replays yet", "A replay re-scores searches people really made, against a new version of your search.", null, null],
};

export default class CaptainRuns extends View {
  init(_props, cx) {
    this.tab = "backtests";
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
    this.load(cx, "backtests");
    cx.timer.every(1000, (cx) => this.pollJob(cx));
    // Until the backend answers, ask again every 3 s.
    // Until the list loads, ask again every 3 s: while the services start, and after any failure that wasn't the
    // backend answering (they stopped or restarted).
    cx.timer.every(3000, (cx) => { if (this.starting || (this.error && !this.errorR?.status)) this.load(cx, this.tab); });
    // Opened by name (palette): shared "captain.open" {section: "runs", backtest, at}.
    cx.timer.every(500, (cx) => {
      const o = shared("captain.open");
      if (o?.section === "runs" && o.backtest && o.at !== this.opened) {
        this.opened = o.at;
        this.tab = "backtests";
        this.pick(cx, "backtests", o.backtest);
        cx.notify();
      }
    });
    // Another space, another workspace: drop what was shown and load this tab again.
    watchWorkspace(cx, (cx) => {
      this.data = {}; this.sel = {}; this.detail = {}; this.open = {}; this.grader = null; this.error = null;
      this.load(cx, this.tab);
      cx.notify();
    });
  }

  // ---- data ---------------------------------------------------------------------------------------------------

  load(cx, tab) {
    const path = { backtests: "/api/backtests", workflows: "/api/workflows", grading: "/api/grading", replays: "/api/replays" }[tab];
    cx.spawn(async (cx) => {
      const r = await api(path);
      this.starting = !r.ok && !!r.starting;
      if (this.starting) { cx.notify(); return; }
      if (r.ok) this.data[tab] = r.data ?? [];
      this.error = r.ok ? null : say(r, `Couldn't load ${tab}.`);
      this.errorR = r;
      const list = this.data[tab] ?? [];
      const key = (x) => x.name;
      if (list.length && !list.some((x) => key(x) === this.sel[tab])) this.pick(cx, tab, key(list[0]));
      cx.notify();
    });
  }

  pick(cx, tab, name) {
    this.sel[tab] = name;
    if (tab === "backtests") {
      this.detail.backtest = null;
      this.detail.run = null;
      cx.spawn(async (cx) => {
        const r = await api(`/api/backtests/${enc(name)}`);
        if (this.sel.backtests !== name) return;
        this.detail.backtest = r.ok ? r.data : { error: say(r, "Couldn't load this backtest.") };
        const v = r.ok ? r.data.runs?.at(-1)?.version : null;
        if (v) this.pickRun(cx, name, v);
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
    cx.spawn(async (cx) => {
      const r = await api(`/api/runs/${enc(name)}?v=${v}`);
      if (this.sel.backtests !== name || this.detail.runV !== v) return;
      this.detail.run = r.ok ? r.data : { error: say(r, "Couldn't load this run.") };
      cx.notify();
    });
  }

  start(cx, label, path, after) {
    this.job = { label, status: "queued", message: null, after };
    cx.spawn(async (cx) => {
      const r = await api(path, { method: "POST", body: {} });
      if (!r.ok) this.job = { label, status: "failed", message: say(r, "Couldn't start it.") };
      else this.job.id = r.data?.job;
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

  openGrader(cx, name) {
    this.grader = { name, next: null, count: 0, error: null, busy: false };
    this.nextPair(cx);
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
    const tabs = [["backtests", "Backtests", "backtest"], ["workflows", "Workflows", "workflow"], ["grading", "Grading", "grade"], ["replays", "Replays", "replay"]];
    return h_flex().h(52).flex_none().px(20).gap(14).items_center().border_b(1).border_color(c.rule)
      .child(div().flex_none().text_size(18).font_semibold().child("Runs"))
      .child(h_flex().flex_none().border_1().border_color(c.grey300).children(tabs.map(([id, label, icon]) =>
        h_flex().id(`tab-${id}`).h(28).px(12).gap(6).items_center().cursor_pointer().border_r(1).border_color(c.grey300).text_size(12.5)
          .bg(this.tab === id ? c.navy : c.surface).text_color(this.tab === id ? c.surface : c.grey900)
          .on_click((_e, cx) => { this.tab = id; this.grader = null; if (!this.data[id]) this.load(cx, id); cx.notify(); })
          .child(this.icon(icon, 13)).child(div().child(label)))))
      .child(div().flex_1())
      .when(this.tab === "backtests", (e) => e.child(Button(c, "new-backtest", "Run a backtest", () => openForm("backtest"), { kind: "primary", small: true })))
      .when(this.tab === "grading", (e) => e.child(Button(c, "new-grading", "Start grading", () => openForm("grading"), { kind: "primary", small: true })))
      .child(div().id("runs-refresh").size(28).flex().flex_none().items_center().justify_center().cursor_pointer()
        .border_1().border_color(c.grey300).text_color(c.grey900).hover((s) => s.text_color(c.blue).border_color(c.blue))
        .tooltip("Refresh").on_click((_e, cx) => { this.load(cx, this.tab); if (this.sel[this.tab]) this.pick(cx, this.tab, this.sel[this.tab]); })
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

  /** The left list of a tab: an icon on every row, the name, a second line, and a status at the right. */
  list(c, w, tab, rows, icon, sub, right) {
    return v_flex().id(`${tab}-list`).w(w).h_full().flex_none().border_r(1).border_color(c.rule).overflow_y_scrollbar()
      .when(!!this.error, (e) => e.child(div().p(10).child(trouble(c, `${tab}-load`, this.errorR, this.error, (cx) => this.load(cx, tab)))))
      .when(this.data[tab] === undefined && !this.error, (e) => e.child(div().child(Skeleton(c, 8))))
      .when(Array.isArray(this.data[tab]) && !rows.length, (e) => {
        const [title, body, action, task] = EMPTY[tab] ?? ["Nothing here yet", "", null, null];
        return e.child(v_flex().p(14).gap(8)
          .child(div().text_size(13).font_medium().child(title))
          .child(div().text_size(12).line_height("18px").text_color(c.grey900).child(body))
          .when(!!action, (e) => e.child(Button(c, `empty-${tab}`, action, () => openForm(task), { small: true }))));
      })
      .children(rows.map((x) => {
        const active = x.name === this.sel[tab];
        return h_flex().id(`${tab}-${x.name}`).w_full().h(48).flex_none().px(10).gap(8).items_center().cursor_pointer()
          .border_l(2).border_color(active ? c.blue : "#00000000").bg(active ? c.blueLight : c.surface).hover((s) => (active ? s : s.bg(c.surface2)))
          .tooltip(x.description ?? x.name)
          .on_click((_e, cx) => this.pick(cx, tab, x.name))
          .child(this.icon(icon, 15, active ? c.blue : c.grey500))
          .child(v_flex().flex_1().min_w(0)
            .child(div().truncate().text_size(12.5).font_family(MONO).text_color(active ? c.blue : c.navy).child(x.name))
            .child(div().truncate().text_size(11).text_color(c.grey500).child(sub(x))))
          .child(right ? right(x) : div());
      }));
  }

  // ---- backtests ----------------------------------------------------------------------------------------------

  backtests(c, w) {
    const rows = this.data.backtests ?? [];
    const lw = Math.max(220, Math.min(320, Math.floor(w * 0.26)));
    return h_flex().flex_1().min_h(0)
      .child(this.list(c, lw, "backtests", rows, "backtest", (x) => `${(x.arms ?? []).map(bare).join(" vs ")} · ${plural(x.runs ?? 0, "run")}`))
      .child(this.backtestDetail(c));
  }

  backtestDetail(c) {
    const b = this.detail.backtest;
    if (!this.sel.backtests) return v_flex().flex_1().p(24).text_size(13).text_color(c.grey500).child("Pick a backtest.");
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
        .when(b.mode !== "imported", (e) => e.child(this.button(c, "run-backtest", running ? "Running" : "Run backtest", "run", (_e, cx) => this.start(cx, "Backtest", `/api/backtests/${enc(name)}/run`, (cx) => this.pick(cx, "backtests", name)), true, !!running))))
      .child(div().text_size(13).font_medium().child(b.preregistration?.hypothesis ?? ""))
      .child(h_flex().gap(8)
        .child(Button(c, "bt-agent", "Open an agent in this space", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, backtestPrompt(name, b, r))), { small: true }))
        .child(Button(c, "bt-prompt", "Copy prompt", (_e, cx) => copyPrompt(cx, backtestPrompt(name, b, r)), { kind: "quiet", small: true })))
      .child(h_flex().flex_wrap().gap(16).text_size(12).text_color(c.grey900)
        .children((b.arms ?? []).map((a, i) => h_flex().gap(6).items_center().child(div().text_color(c.grey500).child(String.fromCharCode(65 + i))).child(named(c, a))))
        .child(div().child(`primary ${b.preregistration?.primary_metric ?? ""}, MDE ${b.preregistration?.mde ?? ""}`)))
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
      .child(!r ? ((b.runs ?? []).length ? Skeleton(c, 4) : div().text_size(12.5).text_color(c.grey500).child("No runs yet.")) : r.error ? div().text_size(12.5).text_color(c.fail).child(r.error) : this.runSummary(c, name, r));
  }

  runSummary(c, name, r) {
    const p = (r.comparisons ?? []).find((x) => x.primary) ?? r.comparisons?.[0];
    const metrics = ["ndcg@10", "p@10", "mrr"].filter((m) => (r.arms ?? []).some((a) => a.offline?.[m] !== undefined));
    const cols = [...metrics, "judged@10", "per 1k"];
    return v_flex().gap(10)
      .when(!!p, (e) => e.child(h_flex().flex_wrap().gap(10).items_center().p(12).border_1().border_color(c.rule)
        .child(this.icon(p.delta > 0 ? "up" : p.delta < 0 ? "down" : "flat", 16, p.verdict === "win" ? c.blue : p.verdict === "loss" ? c.fail : c.grey500))
        .child(div().id("bt-primary").text_size(13).font_medium().tooltip(p.metric).child(`${metricWords(p.metric)}, B minus A`))
        .child(div().text_size(14).font_family(MONO).child(fmtD(p.delta)))
        .child(div().text_size(12).font_family(MONO).text_color(c.grey900).child(`[${fmtD(p.ci_low)}, ${fmtD(p.ci_high)}]`))
        .child(this.badge(c, p.verdict, p.verdict))
        .when(!!p.evidence, (e) => e.child(this.badge(c, p.evidence)))
        .child(div().flex_1())
        .child(div().text_size(12).text_color(c.grey500).child([r.wall_s != null ? `ran in ${Math.round(r.wall_s)} s` : null, r.cost_usd != null ? `cost $${Number(r.cost_usd).toFixed(2)}` : null].filter(Boolean).join(" · ")))))
      .child(div().id("arm-table-x").w_full().overflow_x_scrollbar().child(v_flex().min_w(200 + cols.length * 104).border_1().border_color(c.rule)
        .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
          .child(div().flex_1().min_w(200).px(10).child("Arm"))
          .children(cols.map((m) => div().id(`armh-${m}`).w(104).flex_none().px(8).text_right().truncate().tooltip(m).child(metricWords(m)))))
        .children((r.arms ?? []).map((a, i) => h_flex().id(`arm-${i}`).h(30).border_b(1).border_color(c.rule).text_size(12.5).items_center().whitespace_nowrap()
          .child(h_flex().flex_1().min_w(200).px(10).gap(6).child(div().text_color(c.grey500).child(String.fromCharCode(65 + i))).child(named(c, a.arm)))
          .children(metrics.map((m) => div().w(104).flex_none().px(8).text_right().font_family(MONO).child(a.offline?.[m] !== undefined ? Number(a.offline[m]).toFixed(4) : "")))
          .child(div().w(92).flex_none().px(8).text_right().font_family(MONO).text_color((a.judged_at_10 ?? 1) < 0.9 ? c.fail : c.navy).child(a.judged_at_10 != null ? `${Math.round(a.judged_at_10 * 100)}%` : "none"))
          .child(div().w(92).flex_none().px(8).text_right().truncate().font_family(MONO).text_color(a.cost?.serve_usd_per_1k != null ? c.navy : c.grey500).child(a.cost?.serve_usd_per_1k != null ? `$${Number(a.cost.serve_usd_per_1k).toFixed(4)}` : "none"))))))
      .child(h_flex().gap(8)
        .child(this.button(c, "open-compare", "Compare in detail", "compare", () => {
          setShared("captain.compare", { backtest: name, version: r.version });
          run("layout.select", { stack: "sections", child: "comparisons" });
        })));
  }

  // ---- workflows ----------------------------------------------------------------------------------------------

  workflows(c, w) {
    const rows = this.data.workflows ?? [];
    const lw = Math.max(220, Math.min(320, Math.floor(w * 0.26)));
    return h_flex().flex_1().min_h(0)
      .child(this.list(c, lw, "workflows", rows, "workflow",
        (x) => [plural(x.steps ?? 0, "step"), x.schedule ? cronEnglish(x.schedule) : null].filter(Boolean).join(" · "),
        (x) => (x.last_status ? this.statusIcon(c, x.last_status) : div())))
      .child(this.workflowDetail(c));
  }

  workflowDetail(c) {
    const w = this.detail.workflow;
    if (!this.sel.workflows) return v_flex().flex_1().p(24).text_size(13).text_color(c.grey500).child("Pick a workflow. Make one with New, Workflow.");
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
        .child(this.button(c, "run-workflow", running ? "Running" : "Run now", "run", (_e, cx) => this.start(cx, "Workflow", `/api/workflows/${enc(name)}/run`, (cx) => this.pick(cx, "workflows", name)), true, !!running)))
      .when(!!w.description, (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(w.description)))
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

  grading(c) {
    if (this.grader) return this.graderView(c);
    const qs = this.data.grading ?? [];
    return v_flex().id("grading").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(14).gap(10)
      .when(!!this.error, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.error)))
      .when(Array.isArray(this.data.grading) && !qs.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("No grading queues. Make one with New, Grading queue.")))
      .children(qs.map((q) => {
        const pub = this.published?.queue === q.name ? this.published : null;
        return v_flex().id(`queue-${q.name}`).border_1().border_color(c.rule).p(14).gap(8)
          .child(h_flex().gap(8).items_center()
            .child(this.icon("grade", 15, c.navy))
            .child(div().flex_1().min_w(0).truncate().font_family(MONO).text_size(13).font_medium().child(q.name))
            .child(this.button(c, `publish-${q.name}`, "Publish", "publish", (_e, cx) => this.publish(cx, q), false, !q.graded))
            .child(this.button(c, `grade-${q.name}`, "Grade", "grade", (_e, cx) => this.openGrader(cx, q.name), true)))
          .child(h_flex().gap(10).items_center()
            .child(div().flex_1().h(8).bg(c.grey300).child(div().h_full().bg(c.blue).w(`${Math.round((q.share ?? 0) * 100)}%`)))
            .child(div().flex_none().font_family(MONO).text_size(12).child(`${q.graded} of ${q.pairs} pairs`)))
          .child(h_flex().flex_wrap().gap(14).text_size(12).text_color(c.grey900)
            .child(div().child(`queries ${q.queries}`))
            .child(div().child(`${(q.pipelines ?? []).join(", ")}, top ${q.depth}`))
            .child(div().child(`graders ${Object.keys(q.graders ?? {}).length ? Object.entries(q.graders).map(([w, n]) => `${w} (${n})`).join(", ") : "none yet"}`))
            .child(div().child(q.kappa === null || q.kappa === undefined ? `kappa not yet (${q.agreement_pairs ?? 0} of 10 shared pairs)` : `kappa ${Number(q.kappa).toFixed(2)}`)))
          .when(!!pub, (e) => e.child(div().text_size(12.5).text_color(pub.status === "failed" ? c.fail : c.navy)
            .child(pub.status === "done" ? `Published ${pub.ref}: HUMAN_GRADED judgments.` : pub.status === "failed" ? pub.error : "Publishing")))
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
        .child(this.button(c, "grader-back", "All queues", "chevron-left", (_e, cx) => { this.grader = null; this.load(cx, "grading"); }))
        .child(div().font_family(MONO).text_size(13).child(g.name))
        .child(div().flex_1())
        .child(div().text_size(12.5).text_color(c.grey900).child(n ? (n.done ? "All graded" : `${n.remaining} left`) : "").when(g.count > 0, (e) => e.child(`, ${g.count} graded now`))))
      .child(v_flex().id("grader-body").flex_1().min_h(0).overflow_y_scrollbar().px(28).py(20).gap(16).max_w(900)
        .when(!!g.error, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(g.error)))
        .when(!n && !g.error, (e) => e.child(Skeleton(c, 6)))
        .when(!!n?.done, (e) => e.child(div().text_size(14).child("You graded every pair in this queue. Publish it from the list.")))
        .when(!!n?.pair, (e) => e
          .child(v_flex().gap(2)
            .child(div().text_size(11).text_color(c.grey500).child("QUERY"))
            .child(div().text_size(26).font_semibold().child(n.pair.query))
            .child(div().text_size(11.5).text_color(c.grey500).child(`first seen in ${n.pair.first_seen}${n.pair.overlap ? ", also graded by others" : ""}`)))
          .child(v_flex().p(14).gap(6).border_1().border_color(c.grey300)
            .child(div().text_size(16).font_medium().child(field("title") ?? n.pair.product_id))
            .child(h_flex().flex_wrap().gap(14).text_size(12.5).text_color(c.grey900)
              .when(!!field("brand"), (e) => e.child(div().child(field("brand"))))
              .when(!!field("category"), (e) => e.child(div().child(field("category"))))
              .when(!!field("price"), (e) => e.child(div().font_family(MONO).child(`$${Number(field("price")).toFixed(2)}`)))
              .child(div().font_family(MONO).text_color(c.grey500).child(n.pair.product_id)))
            .when(!!field("description"), (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(field("description")))))
          .child(h_flex().gap(8).children(GRADES.map(([v, label]) => v_flex().id(`grade-${v}`).flex_1().h(76).items_center().justify_center().gap(2).cursor_pointer()
            .border_1().border_color(v === 3 ? c.blue : c.grey300).hover((s) => s.bg(c.blueLight).border_color(c.blue))
            .on_click((_e, cx) => this.grade(cx, v))
            .child(div().text_size(20).font_family(MONO).child(String(v)))
            .child(div().text_size(13).font_medium().child(label)))))
          .child(div().id("grade-note").h(30).px(8).flex().items_center().border_1().border_color(c.grey300)
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
            .child(Input.new(this.gradeNote).w_full().text_size(12.5).text_color(c.navy)))
          .child(div().text_size(11.5).text_color(c.grey500).child("Keys 0 to 3 grade the pair. Click outside the note first."))));
  }

  // ---- replays ------------------------------------------------------------------------------------------------

  replays(c, w) {
    const rows = this.data.replays ?? [];
    const lw = Math.max(220, Math.min(300, Math.floor(w * 0.24)));
    const r = rows.find((x) => x.name === this.sel.replays) ?? null;
    return h_flex().flex_1().min_h(0)
      .child(this.list(c, lw, "replays", rows, "replay", (x) => `${(x.curve ?? []).length} points · ${x.evidence ?? ""}`))
      .child(!r ? v_flex().flex_1().p(24).text_size(13).text_color(c.grey500).child("Pick a replay.") : this.replayDetail(c, r, w - lw));
  }

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
    const H = 140;
    const ys = pts.map((p) => p.outcomes?.[name]?.delta_vs_never ?? 0);
    const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
    const span = hi - lo || 1;
    const X = (r) => `${(4 + r * 92).toFixed(2)}%`;
    const Y = (v) => `${(6 + (1 - (v - lo) / span) * 86).toFixed(2)}%`;
    const color = [c.blue, c.navy, c.fail][i % 3];
    let line = PathBuilder.stroke(2);
    if (kind === "directional") line = line.dash_array([6, 4]);
    pts.forEach((p, k) => (line = k ? line.line_to(X(p.trigger_rate), Y(ys[k])) : line.move_to(X(p.trigger_rate), Y(ys[k]))));
    const zero = PathBuilder.stroke(1).move_to("4%", Y(0)).line_to("96%", Y(0));
    return v_flex().id(`curve-${name}`).w(w).gap(4)
      .child(h_flex().gap(6).text_size(12).child(div().font_medium().child(name)).child(div().text_color(c.grey500).child(`(${kind})`))
        .child(div().flex_1()).child(div().font_family(MONO).text_color(c.grey500).child(`${fmtD(lo)} to ${fmtD(hi)}`)))
      .child(div().relative().w_full().h(H).border_1().border_color(c.rule)
        .child(div().absolute().inset_0().child(window.paint_path(zero.build(), c.grey300)))
        .child(div().absolute().inset_0().child(window.paint_path(line.build(), color))))
      .child(h_flex().text_size(10.5).text_color(c.grey500).font_family(MONO)
        .child(div().child("0%")).child(div().flex_1()).child(div().child("trigger rate")).child(div().flex_1()).child(div().child("100%")));
  }

  render() {
    const c = palette(isDark(theme()));
    const w = this.width;
    // Never blank: the header always shows, and the body says what is happening.
    const body = this.starting ? v_flex().flex_1().items_center().justify_center().child(Loading(c, "", true)) : this.tab === "backtests" ? this.backtests(c, w) : this.tab === "workflows" ? this.workflows(c, w) : this.tab === "grading" ? this.grading(c) : this.replays(c, w);
    return v_flex().id("root").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      .on_size_change((size, cx) => { this.width = size.width; cx.notify(); })
      .child(this.header(c))
      .child(this.jobLine(c))
      .child(body);
  }
}
