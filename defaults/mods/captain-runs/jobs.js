// CAPTAIN: Runs, Jobs tab: every job on every compute target (GET /api/jobs, newest first), with live state and time,
// where it runs in words ("AWS ECS, us-west-1"), its log tail (GET /api/jobs/{id}/logs?since=, followed while it
// runs) and Stop (POST /api/jobs/{id}/cancel, a second click confirms). A job's own command line never shows: its
// name is said in words, and the command is behind Copy details.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";

const enc = encodeURIComponent;
const PLACES = {
  local: "This computer", "aws-ecs": "AWS ECS", "aws-batch": "AWS Batch", "gcp-cloud-run": "Google Cloud Run",
  "gcp-vertex": "Vertex AI", "azure-container-apps": "Azure Container Apps", "azure-ml": "Azure ML", modal: "Modal",
};
const DONE = new Set(["succeeded", "failed", "stopped"]);
const STATE = { queued: ["Waiting to start", "queued"], running: ["Running", "running"], succeeded: ["Finished", "done"], failed: ["Failed", "failed"], stopped: ["Stopped", "x"] };

/** Where a job runs, in words. */
export function place(j) {
  const kind = PLACES[j.target_kind] ?? j.target_kind ?? j.target ?? "Somewhere";
  return [kind, j.region].filter(Boolean).join(", ");
}

/** What a job does, in words, from its command (never the command line itself). */
export function jobWords(j) {
  const argv = [...(j.argv ?? [])];
  while (argv[0]?.startsWith("-")) argv.splice(0, argv[0] === "-w" || argv[0] === "--workspace" ? 2 : 1);
  const words = argv.filter((x) => !String(x).startsWith("-"));
  const [noun, verb, ...rest] = words.map(String);
  const what = rest.length ? ` ${rest[0]}` : "";
  const VERBS = {
    "backtest run": "Run backtest", "pipeline retrain": "Retrain", "data import": "Import data", "data list": "List datasets",
    "data snapshot": "Import a dataset", "tables pull": "Read the tables of", "grade queue": "Build a grading queue",
    "replay run": "Run replay", "workflow run": "Run workflow", "label build": "Build label",
  };
  const known = VERBS[`${noun} ${verb}`];
  if (known) return `${known}${what}`;
  if (noun === "sql") return `Run a query on ${verb ?? "a database"}`;
  if (!noun) return j.label ?? "A job";
  const phrase = [verb, noun].filter(Boolean).join(" ");
  return `${phrase[0].toUpperCase()}${phrase.slice(1)}${what}`;
}

function took(j) {
  const start = Date.parse(j.started_at ?? j.submitted_at ?? "");
  if (!Number.isFinite(start)) return "";
  const end = DONE.has(j.state) ? Date.parse(j.finished_at ?? j.updated_at ?? "") : Date.now();
  const s = Math.max(0, ((Number.isFinite(end) ? end : Date.now()) - start) / 1000);
  return s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}
function when(iso) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "";
  const s = (Date.now() - t) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}

export class Jobs {
  constructor(host) {
    this.host = host;
    this.list = null;
    this.error = null;
    this.sel = null;
    this.logs = new Map(); // id -> {lines, next, where, note}
    this.confirm = null;
    this.note = null;
    this.last = 0;
  }

  load(cx, refresh = true) {
    this.last = Date.now();
    cx.spawn(async (cx) => {
      const r = await api(`/api/jobs?refresh=${refresh}&limit=200`, { timeout: 60 });
      if (r.ok) {
        this.host.starting = false; // the services answered: Runs stops waiting for them
        this.list = Array.isArray(r.data) ? r.data : [];
        this.error = null;
        if (!this.sel && this.list[0]) this.pick(cx, this.list[0].id);
      } else if (!r.starting) this.error = r;
      cx.notify();
    });
  }

  pick(cx, id) {
    this.sel = id;
    this.confirm = null;
    if (!this.logs.has(id)) { this.logs.set(id, { lines: [], next: null }); this.follow(cx, id); }
    cx.notify();
  }

  follow(cx, id) {
    const l = this.logs.get(id);
    if (!l || l.loading) return;
    l.loading = true;
    cx.spawn(async (cx) => {
      const r = await api(`/api/jobs/${enc(id)}/logs${l.next ? `?since=${enc(l.next)}` : ""}`, { timeout: 60 });
      l.loading = false;
      if (r.ok) {
        l.lines = [...l.lines, ...(r.data.lines ?? [])].slice(-400);
        if (r.data.next != null) l.next = r.data.next;
        l.where = r.data.where ?? l.where;
        l.note = r.data.note ?? null;
        l.error = null;
      } else l.error = "Couldn't read its log.";
      cx.notify();
    });
  }

  /** Every 2 s while a job runs: its state and new log lines. Otherwise the list every 30 s. */
  tick(cx) {
    const live = (this.list ?? []).some((j) => !DONE.has(j.state));
    if (Date.now() - this.last > (live ? 4000 : 30000)) this.load(cx, live);
    const j = (this.list ?? []).find((x) => x.id === this.sel);
    if (j && !DONE.has(j.state)) { this.follow(cx, j.id); cx.notify(); }
  }

  stop(cx, j) {
    if (this.confirm !== j.id) { this.confirm = j.id; cx.notify(); return; }
    this.confirm = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/jobs/${enc(j.id)}/cancel`, { body: {} });
      this.note = r.ok ? (r.data?.note ? { text: r.data.note, tone: "muted" } : { text: `Stopping ${jobWords(j)}.`, tone: "good" })
        : { text: "Couldn't stop it. Try again, or stop it where it runs.", tone: "fail" };
      this.load(cx, true);
    });
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  render(c, w) {
    const lw = Math.max(240, Math.min(340, Math.floor(w * 0.34)));
    return h_flex().id("jobs").flex_1().min_h(0).child(this.side(c, lw)).child(this.detail(c));
  }

  stateChip(c, j) {
    const [word, ic] = STATE[j.state] ?? [j.state ?? "Unknown", "queued"];
    const col = j.state === "failed" ? c.fail : j.state === "succeeded" ? c.good : j.state === "running" ? c.blue : c.grey900;
    return h_flex().flex_none().gap(4).items_center().text_size(11.5).text_color(col).child(this.host.icon(ic, 12, col)).child(div().child(word));
  }

  side(c, w) {
    const list = this.list;
    return v_flex().id("jobs-list").w(w).h_full().flex_none().border_r(1).border_color(c.rule).overflow_y_scrollbar()
      .when(list === null && !this.error, (e) => e.child(Skeleton(c, 8)))
      .when(!!this.error, (e) => e.child(div().p(10).child(this.host.troubleView(c, "jobs", this.error, "Couldn't load the jobs.", (cx) => this.load(cx)))))
      .when(Array.isArray(list) && !list.length, (e) => e.child(v_flex().p(14).gap(6)
        .child(div().text_size(13).font_medium().child("No jobs yet"))
        .child(div().text_size(12).line_height("18px").text_color(c.grey900)
          .child("A job is work Captain runs once on a compute target: this computer, AWS, Google Cloud, Azure or Modal. Backtests and retrains you start on the cloud show up here with their logs."))))
      .children((list ?? []).map((j) => {
        const on = this.sel === j.id;
        return v_flex().id(`job-${j.id}`).w_full().flex_none().px(10).py(8).gap(3).cursor_pointer()
          .border_l(2).border_color(on ? c.blue : "#00000000").bg(on ? c.blueLight : c.surface).hover((s) => (on ? s : s.bg(c.surface2)))
          .on_click((_e, cx) => this.pick(cx, j.id))
          .child(h_flex().gap(6).items_center()
            .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_medium().text_color(on ? c.blue : c.navy).child(jobWords(j)))
            .child(this.stateChip(c, j)))
          .child(h_flex().gap(6).text_size(11).text_color(c.grey500)
            .child(this.host.icon(j.target_kind === "local" ? "compute" : "cloud", 11, c.grey500))
            .child(div().flex_1().min_w(0).truncate().child(`${place(j)}, ${when(j.submitted_at)}`))
            .child(div().flex_none().font_family(MONO).child(took(j))));
      }));
  }

  detail(c) {
    const j = (this.list ?? []).find((x) => x.id === this.sel);
    if (!j) return v_flex().flex_1();
    const l = this.logs.get(j.id) ?? { lines: [] };
    const live = !DONE.has(j.state);
    const details = JSON.stringify({ id: j.id, target: j.target, kind: j.target_kind, region: j.region, state: j.state, reason: j.reason, command: (j.argv ?? []).join(" "), exit_code: j.exit_code, where: l.where ?? null }, null, 2);
    const fact = (k, v) => v ? v_flex().gap(2).child(div().text_size(10.5).text_color(c.grey500).child(k.toUpperCase())).child(div().text_size(12.5).child(v)) : null;
    return v_flex().id("job-detail").flex_1().min_w(0).h_full().px(20).py(14).gap(12)
      .child(div().text_size(15).font_medium().child(jobWords(j)))
      .child(h_flex().flex_wrap().gap(10).items_center()
        .child(this.stateChip(c, j))
        .child(div().flex_1())
        .when(live, (e) => e.child(Button(c, `job-stop-${j.id}`, this.confirm === j.id ? "Stop it? Click again" : "Stop", (_e, cx) => this.stop(cx, j), { small: true })))
        .child(Button(c, `job-copy-${j.id}`, "Copy details", (_e, cx) => cx.write_to_clipboard(details), { kind: "quiet", small: true })))
      .when(!!j.reason && j.state !== "succeeded", (e) => e.child(div().text_size(12.5).text_color(j.state === "failed" ? c.fail : c.grey900).child(j.reason)))
      .when(!!this.note, (e) => e.child(div().text_size(12).text_color(this.note.tone === "fail" ? c.fail : c.grey900).child(this.note.text)))
      .child(h_flex().flex_wrap().gap(24)
        .child(fact("Where", place(j)))
        .child(fact("Started", j.started_at ? when(j.started_at) : "not yet"))
        .child(fact(live ? "Running for" : "Took", took(j)))
        .child(fact("By", j.submitted_by ?? ""))
        .when(j.exit_code != null && j.exit_code !== 0, (e) => e.child(fact("Exit code", String(j.exit_code)))))
      .child(v_flex().flex_1().min_h(0).border_1().border_color(c.rule)
        .child(h_flex().h(28).flex_none().px(10).gap(8).items_center().bg(c.surface2).border_b(1).border_color(c.rule).text_size(12)
          .child(div().font_medium().child("Log"))
          .child(div().flex_1().min_w(0).truncate().text_color(c.grey500).child(l.note ?? (live ? "following new lines" : ""))))
        .child(v_flex().id(`job-log-${j.id}`).flex_1().min_h(0).overflow_y_scrollbar().p(10)
          .when(!!l.error, (e) => e.child(div().text_size(12).text_color(c.fail).child(l.error)))
          .when(!l.lines.length && !l.error, (e) => e.child(div().text_size(12).text_color(c.grey500).child(live ? "No log lines yet." : "This job wrote no log.")))
          .children(l.lines.slice(-300).map((x) => div().text_size(11.5).font_family(MONO).text_color(c.navy).child(String(x.message ?? ""))))));
  }
}
