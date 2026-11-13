// CAPTAIN: the Ship part of a project's page as a path a person walks: 1 Deploy, 2 Try it, 3 Use it from your site,
// and it ends there, at a running endpoint with its address. Captain runs no A/B tests: a team compares models on real shoppers in its own testing tool, and
// step 3 gives that tool an address for each model. Step 3 is also where it runs and who can reach it, the code
// to call it, a live count of real requests, and a quiet test on a copy of the site's requests
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
import { exec, appHome, openOverlay, run } from "herdr";
import { MONO } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { watchLive, hear } from "./kit/live.js";
import { Badge, Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { costState, costSlice } from "./kit/cost.js";
import { ShipSection, DeployedView, loadPlaces as loadDeployPlaces, loadPlain, loadLive } from "./ship-view.js";
import { RefreshMark } from "./deploy-tabs.js";
import { DeploymentsList } from "./deploy-tabs.js";

const enc = encodeURIComponent;
const THUMB = 96;
const short = (ref) => String(ref ?? "").replace(/^.*\//, "");
const pct = (x) => `${Math.round(Number(x) * 100)}%`;
const clock = (ts) => String(ts ?? "").slice(11, 23);
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
    this.hostSheet = null; // which host's setup sheet is open
    this.cost = costState(); // what the staged endpoint costs to keep up (POST /api/costs/estimate, kit/cost.js)
    this.conn = null;      // GET /staging/connect: the address, the snippets, how many real requests arrived
    this.shadow = null;    // GET /shadow: what the candidate would change, with no shopper seeing it
    this.diff = null;      // GET /staging/differ: the queries where the two versions differ most
    this.code = null;      // which snippet is open
    this.whyOpen = new Set(); // results whose "Why is this here?" is open
    this.dp = null;        // GET /deploy/places: where this project's model can run, one card each
    this.dconfirm = null;  // a cloud deploy waiting for a yes: {place, plan, all}
    this.djob = null;      // a deploy in progress: {place, steps, message, started, failed?}
    this.dnote = null; this.dasking = null;
    this.more = null;      // which "More" item is open on the deployed model's screen
    this.range = "1h";     // the time range the header's chart and the Metrics tab share
    this.met = null;       // GET /staging/metrics: requests, response time and errors over that range
    this.liveD = null;     // GET /staging/live: stopped, idle or handling requests, and the live counts
    this.copiesD = null; this.logsD = null; this.setD = null; this.costD = null; // the tabs of those names
    this.setEdit = false; this.setNote = null;
    this.listOpen = false; this.listWhich = "live"; this.listD = null; // the list of deployments across projects
    this.logBox = InputState.new({ placeholder: "Filter the log" });
    this.logBox.on("submit", (_e, cx) => { this.logsD = null; this.loadTab(cx ?? this.cx); });
    this.box = InputState.new({ placeholder: "Type what a shopper would search for" });
    this.box.on("submit", (_e, cx) => this.ask(cx ?? this.cx));
  }

  pid() { return this.page.pid(); }

  /** A sentence in the page's words. The services and older code say "the candidate" and "the baseline". A person
   *  sees "this model" and the other model's own name ("keyword-baseline"), never a role they have to learn. A
   *  model's name that happens to contain one of those words is left alone. */
  w(text) {
    const s = this.staged();
    const other = s?.arms?.baseline ? short(s.arms.baseline).replace(/@v\d+$/, "") : "the model it's compared with";
    const Other = other.replace(/^t/, "T");
    const b = "(?<![-/\\w@])", e = "(?![-\\w@/])";
    const sub = (t, from, to) => t.replace(new RegExp(`${b}${from}${e}`, "g"), to);
    let t = String(text ?? "");
    for (const [from, to] of [["The candidate's", "This model's"], ["the candidate's", "this model's"], ["The baseline's", `${Other}'s`], ["the baseline's", `${other}'s`],
      ["The candidate", "This model"], ["the candidate", "this model"], ["The baseline", Other], ["the baseline", other],
      ["a candidate", "a model"], ["Candidate", "This model"], ["candidate", "this model"], ["Baseline", Other], ["baseline", other]]) t = sub(t, from, to);
    return t;
  }

  load(cx) {
    this.cx = cx;
    const pid = this.pid();
    if (!pid) return;
    if (pid !== this.shown) {
      this.shown = pid; this.status = null; this.result = null; this.note = null; this.confirm = null; this.askError = null;
      this.dp = null; this.dpFor = null; this.dconfirm = null; this.dnote = null; this.more = null;
      this.met = null; this.liveD = null; this.copiesD = null; this.logsD = null; this.setD = null; this.costD = null; this.reqs = null; this.vers = null; this.reqOpen = null; this.tabsFor = null;
    }
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(pid)}/staging`, { scoped: false });
      if (this.pid() !== pid) return;
      if (r.ok) {
        // What is deployed changed since the last read (a deploy, a stop or a roll back, from here, another screen
        // or a terminal): the page's own Next banner and steps are read again, at once.
        const now = `${r.data?.status}:${r.data?.staged?.model_version ?? ""}:${r.data?.staged?.id ?? ""}`;
        const changed = this.seenStatus !== undefined && this.seenFor === pid && this.seenStatus !== now;
        this.seenStatus = now; this.seenFor = pid;
        this.status = r.data; this.problem = null; this.settle(); this.around(cx);
        // The deployed model's own card (kit/modelcard.js) is drawn from its row on the Deployments list, so it is
        // the same card here as there. Read when something is deployed, and dropped when nothing is.
        if (["running", "starting"].includes(r.data?.status)) this.loadRow(cx, pid); else { this.depRow = null; this.depNums = null; }
        if (changed && !this.busy) this.page.reload?.(cx);
        if (r.data?.status === "running" && !this.page.deployOpen) loadLive(this, cx); // the project page names the state too
        // The deployment page's own reads, once per project when the page is open: the chart, the live card, the open tab.
        if (this.page.deployOpen && this.tabsFor !== pid) { this.tabsFor = pid; this.loadTab(cx); }
      } else if (!r.starting) this.problem = plainError(r.error, "Captain couldn't read what is deployed.");
      cx.notify();
    });
  }

  /** What sits around a running endpoint: who is calling it, the quiet test, and (once
   *  per staged version) the queries where the two versions differ. Nothing is asked while nothing runs. */
  around(cx) {
    const pid = this.pid(), s = this.staged();
    if (!this.running()) { this.conn = null; this.diff = null; this.diffFor = null; return; }
    const get = (path, set) => cx.spawn(async (cx) => { const r = await api(`/api/projects/${enc(pid)}${path}`, { scoped: false, timeout: 60 }); if (this.pid() === pid && r.ok) { set(r.data); cx.notify(); } });
    get("/staging/connect", (d) => {
      this.conn = d;
      // The first real request just arrived: the page's own steps and Next line are one read behind. Read again.
      const known = (this.page.stages?.ship?.steps ?? []).find((x) => x.id === "connect")?.done;
      if (d.connected && known === false && this.toldFor !== s.id) { this.toldFor = s.id; this.page.reload?.(cx); }
    });
    if (this.both()) {
      get("/shadow", (d) => { this.shadow = d; });
      if (this.diffFor !== s.id) { this.diffFor = s.id; this.diff = null; get("/staging/differ", (d) => { this.diff = d; }); }
    }
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
  tryQuery(cx, q) { this.more = null; this.box.set_value(q); this.ask(cx); }

  /** For agents: where it runs and the estimate. */
  costSlice() { return this.running() ? costSlice({ ...this.cost, where: this.staged().kind === "local" ? "local" : this.staged().target }) : null; }

  /** From the project's own answer (GET /stages): ask for the details only when something is deployed. */
  sync(cx, ship) {
    this.cx = cx;
    if (this.dpFor !== this.pid()) { this.dpFor = this.pid(); loadDeployPlaces(this, cx); } // where it can run: one card each
    if (ship?.staging) return this.load(cx);
    const pid = this.pid();
    if (pid !== this.shown) { this.shown = pid; this.result = null; this.note = null; this.confirm = null; this.askError = null; }
    this.status = { status: "none", staged: null, text: "Nothing is deployed." };
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
  /** Every few seconds, ask what is deployed. Always: a model can be deployed, stopped or replaced from a
   *  terminal or another window, and what this page says must be the present, not what it last did itself. */
  tick(cx) {
    if (!this.pid() || (this.busy && !this.busyFor)) return;
    const s = this.status?.status;
    this.load(cx);
    if (this.page.deployOpen && (s === "running" || s === "starting")) this.loadTab(cx);
  }

  /** A deploy that failed earlier is the past. Once a model is running it is one line that can be dismissed,
   *  never a panel over what is deployed. Returns whether a model is running. */
  settlePast() {
    if (this.djob?.failed && this.running() && this.staged()) {
      this.dpast = { text: `An earlier deploy to ${this.djob.place?.label ?? "this place"} didn't finish${this.djob.failed.step ? `, at "${this.djob.failed.step}"` : ""}. This model is running.` };
      this.djob = null;
    }
    return this.running();
  }

  /** Whether a deploy's own panel is on screen: one in progress, or one that failed with nothing running. */
  jobShown() { this.settlePast(); return Boolean(this.djob); }

  running() { return this.status?.status === "running"; }
  staged() { return this.status?.staged ?? null; }

  /** The candidate the Stage button would start: the one open on the page, else the newest judged, else the newest. */
  pick() {
    const xs = (this.page.exps?.experiments ?? []).filter((e) => !e.is_champion);
    const open = xs.find((e) => e.id === this.page.open);
    return open ?? [...xs].reverse().find((e) => e.evaluation) ?? xs[xs.length - 1] ?? null;
  }

  /** Wait for a job to end. Nothing is asked on a clock: the job says so itself on the live channel (`job:<id>`,
   *  on every step, progress line and its end), and each time it does the job is read once. While the channel is
   *  down the kit calls every two seconds instead, so the wait still ends. */
  async wait(id, minutes) {
    const until = Date.now() + minutes * 60000;
    let wake = null, told = false;
    const stop = watchLive(this.cx, [`job:${id}`], () => { told = true; wake?.(); });
    this.jobWake = () => wake?.(); // the page's minute clock, so a job that says nothing still meets its deadline
    try {
      for (let i = 0; Date.now() < until; i++) {
        told = false;
        const r = await api(`/api/jobs/${enc(id)}`, { scoped: false });
        // The services no longer know the job (they were restarted), or staging itself already said the work is
        // over: stop waiting. What is true is read from the project, not from a job that can't answer.
        if (r.status === 404 || (i > 0 && !this.busy)) return { ok: true, result: null };
        if (r.ok && r.data?.status === "done") return { ok: true, result: r.data.result };
        if (r.ok && r.data?.status === "failed") return { ok: false, error: plainError(r.data.message, "It didn't finish.") };
        if (r.ok && r.data?.message && r.data.status === "running" && this.busy) { this.busy = r.data.message; this.cx?.notify(); }
        // Told while reading: read again at once. Else wait to be told.
        if (!told) await new Promise((resolve) => { wake = resolve; });
        wake = null;
      }
    } finally { stop(); this.jobWake = null; }
    return { ok: false, error: "It's still starting. Look again in a minute." };
  }

  /** This project's row of the Deployments list, and its numbers for the day: what the model's card is drawn from. */
  loadRow(cx, pid) {
    cx.spawn(async (cx) => {
      const r = await api("/api/deployed", { scoped: false, timeout: 60 });
      if (this.pid() !== pid || !r.ok) return;
      const row = (r.data?.rows ?? []).find((x) => x.project === pid && x.link !== "unlinked" && x.in_cloud !== false) ?? null;
      this.depRow = row;
      cx.notify();
      // The list can still say "deploying" for a moment after the deploy's own record says it is done. Nothing
      // more is told after that, so the row would stay "Deploying" on the card. While no deploy is being followed
      // here and the row says one is under way, it is read again when the channel next says something, and a
      // second later at the latest, until the two agree.
      if (row && (row.state === "deploying" || row.deploying) && !this.djob && !this.rowAgain) {
        this.rowAgain = true;
        const ear = hear(cx, [`deployment:${pid}`, "deployed", "job:"]);
        try { await ear.next(1000); } finally { ear.stop(); this.rowAgain = false; }
        if (this.pid() === pid) this.loadRow(cx, pid);
        return;
      }
      if (!row || (this.depNums && Date.now() - (this.depNumsAt ?? 0) < 60000)) return;  // the numbers are paid reads: once a minute
      this.depNumsAt = Date.now();
      const n = await api(`/api/projects/${enc(pid)}/deployed/numbers?range=24h&env=${enc(row.env ?? "staging")}`, { scoped: false, timeout: 60 });
      if (this.pid() === pid && n.ok) { this.depNums = n.data; cx.notify(); }
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
      this.note = done.ok && done.result?.status === "stopped" ? { tone: "good", text: "It is stopped. Nothing is left running." }
        : done.ok && !done.result ? (this.settled === "stop" ? { tone: "good", text: "It is stopped. Nothing is left running." } : null)
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
      if (r.ok) { this.result = r.data; this.pictures(cx); } else this.askError = plainError(r.error, "The model didn't answer. Try again.");
      cx.notify();
    });
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
    this.settlePast();
    const st = this.staged(), on = this.running();
    const cand = this.pick(), champ = this.page.exps?.champion ?? null;
    const what = st?.model_version ? short(st.model_version) : null;
    const tried = on && Boolean(st?.tried || this.result);
    return [
      { n: 1, id: "stage", title: "Deploy", done: on,
        text: on ? this.status.text : this.busy ?? (st?.status === "failed" ? this.status.text
          : cand ? `Run ${short(cand.pipeline)} as a live endpoint you can try, beside the baseline.`
          : champ ? "Run the baseline as a live endpoint you can try." : "Train a model first. Then you can deploy it."),
        action: on ? null : cand ? "stage" : champ ? "stage-baseline" : null, label: on ? null : cand || champ ? "Deploy" : null },
      { n: 2, id: "try", title: "Try it", done: tried,
        text: on ? `Type a query and see what ${what} returns${st?.arms?.baseline && st.serve !== "baseline" ? ", beside the baseline" : ""}.` : "After it is deployed, type a query and see the results.",
        action: on ? "try" : null, label: null, ...(on ? {} : { waiting_on: "Deploy a model first." }) },
      this.connectStepData(),
    ];
  }

  /** Both models are on the endpoint: this one, and the one it is compared with. */
  both() {
    const st = this.staged();
    return this.running() && Boolean(st?.arms?.baseline) && Boolean(st?.arms?.candidate) && st.how !== "image";
  }

  connectStepData() {
    const on = this.running(), k = this.conn;
    const text = !on ? "After it is deployed, point your site at it and watch the first request arrive."
      : !k ? "Looking for requests from your site..."
      : k.connected ? k.live
      : k.blocked ?? "Waiting for the first request. Copy the code below into your site.";
    return { n: 3, id: "connect", title: "Use it from your site", done: Boolean(on && k?.connected), text, action: on && !k?.connected ? "connect" : null, label: null,
      ...(on ? {} : { waiting_on: "Deploy a model first." }) };
  }

  // ---- drawing ---------------------------------------------------------------------------------------------------

  mark(c, st) {
    return st.done
      ? div().id(`ship-done-${st.id}`).size(22).flex_none().flex().items_center().justify_center().rounded_full().bg(c.goodLight).text_color(c.good).child(svg(icon("check")).size(13))
      : div().size(22).flex_none().flex().items_center().justify_center().rounded_full().border_1().border_color(c.grey300).text_size(12.5).font_semibold().text_color(c.grey900).child(String(st.n));
  }

  row(c, st, action, below) {
    return v_flex().id(`ship-step-${st.id}`).w_full()
      .child(h_flex().p(12).gap(12).items_center().flex_wrap()
        .child(this.mark(c, st))
        .child(v_flex().flex_1().min_w(280).gap(1)
          .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(st.title))
          .child(div().id(`ship-text-${st.id}`).text_size(12.5).line_height("18px").text_color(c.grey500).child(this.w(st.text))))
        .when(Boolean(action), (e) => e.child(h_flex().flex_none().gap(6).items_center().child(action))))
      .when(Boolean(below), (e) => e.child(below));
  }

  stopBox(c) {
    return h_flex().id("ship-stop-ask").p(10).gap(10).items_center().bg(c.surface2).border_1().border_color(c.rule)
      .child(div().flex_1().min_w(0).text_size(12.5).text_color(c.grey900).child("Stop it? The address goes away. What it answered is kept."))
      .child(Button(c, "ship-stop-yes", "Stop it", (_e, cx) => this.stop(cx), { small: true }))
      .child(Button(c, "ship-stop-no", "Keep it running", (_e, cx) => { this.stopAsk = false; cx.notify(); }, { small: true }));
  }

  resultCard(c, x, id) {
    const f = x.image ? this.thumbs.get(x.image) : null;
    const pic = f ? image(`cache/thumbs/${f}`).w(40).h(40).flex_none() : div().w(40).h(40).flex_none().bg(c.surface2);
    return h_flex().id(id).gap(8).py(5).items_center().border_t(1).border_color(c.rule)
      .child(div().w(18).flex_none().text_size(12.5).text_color(c.grey500).child(String(x.position)))
      .when(Boolean(x.image), (e) => e.child(pic))
      .child(v_flex().flex_1().min_w(0)
        .child(div().truncate().text_size(12.5).text_color(c.navy).child(x.title))
        .when(x.price != null, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child(`$${Number(x.price).toFixed(2)}`)))
        .when(Boolean(x.why), (e) => e.child(div().id(`${id}-why`).text_size(12.5).text_color(c.blue).cursor_pointer()
          .on_click((_e, cx) => { if (this.whyOpen.has(id)) this.whyOpen.delete(id); else this.whyOpen.add(id); cx.notify(); }).child("Why is this here?")))
        .when(Boolean(x.why) && this.whyOpen.has(id), (e) => e.child(v_flex().id(`${id}-signals`).gap(1).pt(2)
          .children(x.why.map((w, k) => div().id(`${id}-signal-${k}`).text_size(12.5).line_height("18px").text_color(c.grey900)
            .child(`${Math.round(w.weight * 100)}% of what the model learned: ${w.label} (${Number(w.value).toFixed(2)})`))))))
      // Against the baseline: where it was. Words, so it reads the same in a screen reader and a picture.
      .when(Boolean(x.move) && x.move !== "same", (e) => e.child(div().id(`${id}-move`).flex_none().px(6).h(18).flex().items_center().text_size(12.5)
        .bg(x.move === "down" ? c.failLight : c.goodLight).text_color(x.move === "down" ? c.fail : c.good)
        .child(x.move === "new" ? "new" : `${x.move} ${Math.abs(x.was - x.position)}`)));
  }

  side(c, side, name, id) {
    return v_flex().id(id).flex_1().min_w(240).p(10).gap(2).bg(c.surface).border_1().border_color(c.rule)
      .child(h_flex().gap(6).items_center()
        .child(div().text_size(12.5).font_semibold().child(name))
        .child(Badge(c, short(side.model_version)))
        .child(div().flex_1())
        .child(div().id(`${id}-ms`).text_size(12.5).text_color(c.grey500).child(`${Number(side.ms).toFixed(side.ms < 10 ? 1 : 0)} ms`)))
      .children(side.results.map((x, i) => this.resultCard(c, x, `${id}-r${i}`)))
      .when(!side.results.length, (e) => e.child(div().py(8).text_size(12.5).text_color(c.grey500).child("Nothing found for this query.")));
  }

  // ---- connect your site, and shadow mode -----------------------------------------------------------------------

  connectStep(c, st) {
    if (!this.running() || !this.conn) return this.row(c, st, null, null);
    const k = this.conn, names = Object.keys(k.snippets ?? {});
    const open = this.code && names.includes(this.code) ? this.code : null;
    const below = v_flex().pl(46).pr(12).pb(12).gap(8)
      .child(h_flex().gap(10).items_start().text_size(12.5)
        .child(div().w(70).flex_none().text_color(c.grey500).child("Address"))
        .child(div().id("ship-connect-address").flex_1().min_w(0).font_family(MONO).text_size(12.5).text_color(c.navy).child(String(k.address ?? ""))))
      .child(div().id("ship-connect-where").text_size(12.5).line_height("18px").text_color(c.grey900).child(k.text))
      .when(Boolean(k.blocked), (e) => e.child(h_flex().id("ship-connect-blocked").gap(6).items_start().p(8).bg(c.warnLight)
        .child(svg(icon("triangle-alert")).size(13).mt(2).flex_none().text_color(c.warn))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.navy).child(k.blocked))))
      .child(h_flex().id("ship-connect-live").gap(6).items_center()
        .child(svg(icon(k.connected ? "circle-check" : "clock")).size(13).flex_none().text_color(k.connected ? c.good : c.grey500))
        .child(div().text_size(12.5).font_medium().text_color(k.connected ? c.good : c.grey900).child(k.live))
        .when(k.test + k.simulated > 0, (e) => e.child(div().id("ship-connect-other").text_size(12.5).text_color(c.grey500)
          .child(`Not counted: ${[k.real && k.unlabelled ? `${k.unlabelled} unlabelled` : null, k.test ? `${k.test} test` : null, k.simulated ? `${k.simulated} simulated` : null].filter(Boolean).join(", ")}.`))))
      .child(h_flex().flex_wrap().gap(6).items_center()
        .child(Button(c, "ship-connect-key", "Copy the key", (_e, cx) => this.copyKey(cx), { small: true }))
        .child(Button(c, "ship-connect-test", "Send a test request", (_e, cx) => this.act(cx, "/staging/test", {}, (d) => d.text), { small: true }))
        .children(names.map((n) => this.pick1(c, `ship-connect-code-${n.replace(/ /g, "-")}`, n === "javascript" ? "JavaScript" : n === "python" ? "Python" : n, open === n, () => { this.code = open === n ? null : n; }))))
      .child(div().id("ship-connect-key-note").text_size(12.5).line_height("18px").text_color(c.grey500).child(k.key_note))
      .when(Boolean(open), (e) => e.child(v_flex().id("ship-connect-snippet").gap(4)
        .child(div().p(10).bg(c.surface2).border_1().border_color(c.rule).font_family(MONO).text_size(12.5).line_height("18px").text_color(c.navy)
          .children(String(k.snippets[open]).split("\n").map((ln) => div().child(ln === "" ? " " : ln))))
        .child(h_flex().gap(8).items_center()
          .child(Button(c, "ship-connect-copy", "Copy this code", (_e, cx) => { cx.write_to_clipboard?.(k.snippets[open]); this.note = { tone: "good", text: "The code is on your clipboard. It reads the key from CAPTAIN_STAGING_KEY, so the key is never in your code." }; cx.notify(); }, { small: true }))
          .child(div().id("ship-connect-fallback").flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.grey900).child(k.fallback)))))
      // One address a model, so a team's own testing tool can send each group of shoppers to its model.
      .when((k.addresses ?? []).length > 0, (e) => e.child(v_flex().id("ship-addresses").gap(4)
        .child(div().id("ship-own-tests").text_size(12.5).line_height("18px").text_color(c.grey900).child(k.own_tests ?? ""))
        .children(k.addresses.map((a) => h_flex().id(`ship-address-${a.model}`).gap(10).items_center().flex_wrap()
          .child(div().w(150).flex_none().text_size(12.5).text_color(c.grey500).child(`${a.role}, ${short(a.model_version)}`))
          .child(div().id(`ship-address-${a.model}-value`).flex_1().min_w(0).font_family(MONO).text_size(12.5).text_color(c.navy).child(a.address))
          .child(Button(c, `ship-address-${a.model}-copy`, "Copy", (_e, cx) => this.copy(cx, a.address, "The address is on your clipboard."), { small: true }))))))
      .when(this.both(), (e) => e.child(this.shadowBox(c)));
    return this.row(c, st, null, below);
  }

  /** Shadow mode: shoppers keep the baseline, the candidate ranks the same requests silently, and this is what it
   *  would have changed. Comes before any split. */
  shadowBox(c) {
    const r = this.shadow ?? { status: "none" }, on = r.status === "running";
    return v_flex().id("ship-shadow").p(10).gap(6).bg(c.surface2).border_1().border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(div().text_size(12.5).font_semibold().child("Test quietly with a copy of your site's requests"))
        .when(on, (e) => e.child(Badge(c, "on", "good")))
        .when(Boolean(r.label) && r.status !== "none", (e) => e.child(Badge(c, r.label, r.evidence === "REAL_TRAFFIC" ? "good" : "warn")))
        .child(div().flex_1())
        .when(!on, (e) => e.child(Button(c, "ship-shadow-start", r.status === "none" ? "Start the quiet test" : "Start it again", (_e, cx) => this.act(cx, "/shadow/start", {}, this.w("The quiet test is on. Shoppers keep getting the baseline's results.")), { small: true })))
        .when(on, (e) => e.child(Button(c, "ship-shadow-stop", "End the quiet test", (_e, cx) => this.act(cx, "/shadow/stop", {}, "The quiet test is off."), { small: true }))))
      .child(div().id("ship-shadow-text").text_size(12.5).line_height("18px").text_color(c.grey900)
        .child(this.w(r.status === "none" ? "Your site keeps showing the baseline's results. The candidate ranks a copy of the same requests silently, and Captain shows what it would have changed. No shopper sees the candidate, nobody is assigned to anything, and nothing a shopper does is collected." : r.text)))
      .when(Boolean(r.ms), (e) => e.child(div().id("ship-shadow-ms").text_size(12.5).text_color(c.grey900)
        .child(`Response time, 95 in 100 requests under: ${r.ms.baseline.p95} ms baseline, ${r.ms.candidate.p95} ms candidate.`)))
      .when((r.queries ?? []).length > 0, (e) => e.child(v_flex().id("ship-shadow-queries").gap(2)
        .child(div().text_size(12.5).font_semibold().text_color(c.navy).child("Queries that differ most. Click one to open it in Try it."))
        .children(r.queries.slice(0, 6).map((q, i) => h_flex().id(`ship-shadow-q${i}`).gap(8).items_center().cursor_pointer().hover((x) => x.bg(c.surface))
          .on_click((_e, cx) => this.tryQuery(cx, q.query))
          .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.blue).child(q.query))
          .child(div().flex_none().text_size(12.5).text_color(c.grey500).child(`${this.w(q.text ?? `${q.moved} of the top 10 in another place.`)} ${q.requests} request${q.requests === 1 ? "" : "s"}.`))))));
  }

  pick1(c, id, label, on, onPick) {
    return div().id(id).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
      .on_click((_e, cx) => { onPick(); cx.notify(); }).child(label);
  }

  /** Change what the deployed model accepts (its size and rate limits, its time budget). */
  saveProtect(cx, body) {
    this.protNote = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/protect`, { method: "PUT", body, scoped: false });
      if (r.ok) { this.protD = r.data; this.protNote = { tone: "good", text: "Saved. It takes effect in a few seconds." }; }
      else this.protNote = { tone: "fail", text: plainError(r.error, "That couldn't be saved.") };
      cx.notify();
    });
  }

  /** Make a new key. The old one keeps working for the time chosen, so a site can switch with no downtime. */
  rotateKey(cx, graceMinutes) {
    this.protNote = null; this.keyAsk = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/key/rotate`, { body: { grace_minutes: graceMinutes }, scoped: false, timeout: 60 });
      if (r.ok) { if (this.protD) this.protD = { ...this.protD, key: r.data }; this.protNote = { tone: "good", text: "The key was changed. Copy the new one and give it to your site." }; }
      else this.protNote = { tone: "fail", text: plainError(r.error, "The key couldn't be changed.") };
      cx.notify();
    });
  }

  /** A person asking for the newest requests or log lines now. Only this asks a model in a cloud to write its log:
   *  the page's own reads never call it. */
  refreshNow(cx, which) {
    if (this.refreshing) return;
    this.refreshing = which;
    cx.notify();
    cx.spawn(async (cx) => {
      const q = which === "logs" && this.logBox.value().trim() ? `&q=${enc(this.logBox.value().trim())}` : "";
      const r = await api(`/api/projects/${enc(this.pid())}/staging/${which}?refresh=true&limit=${which === "logs" ? 300 : 50}${q}`, { scoped: false, timeout: 60 });
      this.refreshing = null;
      if (r.ok) { if (which === "logs") this.logsD = r.data; else this.reqs = r.data; }
      cx.notify();
    });
  }

  /** What a tab needs, asked when it is opened and again while it stays open. */
  loadTab(cx) {
    const pid = this.pid();
    const get = (path, set) => cx.spawn(async (cx) => { const r = await api(`/api/projects/${enc(pid)}${path}`, { scoped: false, timeout: 60 }); if (this.pid() === pid && r.ok) { set(r.data); cx.notify(); } });
    if (!this.staged()) return;
    loadPlain(this, cx); // the plain screen: the state, the last hour and the cost, and whatever "More" has open
    return;
    get("/staging/live", (d) => { this.liveD = d; });
    get(`/staging/metrics?range=${this.range}`, (d) => { this.met = d; });
    if (this.tab === "requests") get("/staging/requests?limit=50", (d) => { this.reqs = d; });
    if (this.tab === "versions") { get("/staging/versions", (d) => { this.vers = d; }); get("/staging/rollbacks", (d) => { this.backs = d; }); }
    if (this.tab === "copies") get("/staging/copies", (d) => { this.copiesD = d; });
    if (this.tab === "logs") get(`/staging/logs?limit=300${this.logBox.value().trim() ? `&q=${enc(this.logBox.value().trim())}` : ""}`, (d) => { this.logsD = d; });
    if (this.tab === "settings") get("/staging/settings", (d) => { this.setD = d; });
    if (this.tab === "cost") get("/staging/cost", (d) => { this.costD = d; });
  }

  /** Change how long an idle endpoint stays up. */
  saveIdle(cx, minutes) {
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/settings`, { method: "PUT", body: { idle_minutes: minutes }, scoped: false });
      if (r.ok) { this.setD = r.data; this.setEdit = false; this.setNote = { tone: "good", text: r.data.saved ?? "Saved." }; this.load(cx); }
      else this.setNote = { tone: "fail", text: plainError(r.error, "The idle time couldn't be changed.") };
      cx.notify();
    });
  }

  /** The list of deployments across projects. */
  openList(cx) { this.listOpen = true; this.listD = null; this.loadList(cx); cx.notify(); }

  loadList(cx) {
    const which = this.listWhich;
    cx.spawn(async (cx) => {
      const r = await api(`/api/staging/deployments?which=${which}`, { scoped: false, timeout: 120 });
      if (this.listWhich !== which) return;
      this.listD = r.ok ? r.data : { rows: [], text: plainError(r.error, "The deployments couldn't be read.") };
      cx.notify();
    });
  }

  /** Open a deployment picked from the list: its project's own deployment page. */
  openFromList(cx, r) {
    this.listOpen = false; this.tab = "requests";
    this.page.deployOpen = true;
    this.page.pickProject(cx, { id: r.project, name: r.name });
  }

  deploymentsList(c) { return DeploymentsList(this, c); }

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
    const cell = (w, t, mono) => div().w(w).flex_none().truncate().text_size(12.5).child(String(t ?? ""));
    const list = v_flex().id("dep-requests").flex_1().min_w(360)
      .child(h_flex().gap(8).items_center().pb(6)
        .child(div().id("dep-requests-text").flex_1().min_w(0).text_size(12.5).text_color(c.grey500).child(d ? d.text : "Reading the request log..."))
        .when(this.running(), (e) => e.child(RefreshMark(c, "dep-requests-refresh", this.refreshing === "requests", (_e, cx) => this.refreshNow(cx, "requests")))))
      .when(Boolean(d?.rows?.length), (e) => e
        .child(h_flex().gap(8).py(4).text_color(c.grey500).border_b(1).border_color(c.rule)
          .child(cell(92, "Time")).child(div().flex_1().min_w(80).text_size(12.5).child("Query")).child(cell(80, "Answered by")).child(cell(64, "Took")).child(cell(74, "Traffic")).child(cell(70, "Status")))
        .children(d.rows.map((r, i) => h_flex().id(`dep-request-${i}`).gap(8).py(5).items_center().cursor_pointer().border_b(1).border_color(c.rule)
          .when(open?.id === r.request_id, (x) => x.bg(c.blueLight)).hover((x) => x.bg(c.surface2))
          .on_click((_e, cx) => this.openRequest(cx, r.request_id))
          .child(cell(92, clock(r.ts), true)).child(div().flex_1().min_w(80).truncate().text_size(12.5).text_color(c.navy).child(r.query ?? r.summary ?? ""))
          .child(cell(80, r.arm === "baseline" ? "Baseline" : "Candidate")).child(cell(64, `${Number(r.ms).toFixed(1)} ms`)).child(cell(74, r.traffic))
          .child(div().w(70).flex_none().text_size(12.5).text_color(c.good).child("Answered")))));
    const x = open?.data;
    const panel = !open ? null : v_flex().id("dep-request-panel").w(360).flex_none().p(10).gap(6).bg(c.surface2).border_1().border_color(c.rule)
      .child(h_flex().gap(6).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_semibold().child(x?.query ?? x?.summary ?? "Opening..."))
        .child(Button(c, "dep-request-close", "Close", (_e, cx) => { this.reqOpen = null; cx.notify(); }, { small: true })))
      .when(Boolean(open.problem), (e) => e.child(div().text_size(12.5).text_color(c.fail).child(open.problem)))
      .when(Boolean(x), (e) => e
        .child(div().id("dep-request-meta").text_size(12.5).line_height("18px").text_color(c.grey900)
          .child(`${x.arm === "baseline" ? "Baseline" : "Candidate"} ${short(x.model_version)} answered. ${x.time_text ?? `${Number(x.ms).toFixed(1)} ms at the endpoint.`} ${x.traffic === "real" ? "Real traffic." : x.traffic === "test" ? "Test traffic." : x.traffic === "simulated" ? "Simulated." : "Unlabelled: it didn't say it was a shopper."}`))
        .child(h_flex().gap(6).items_center().child(div().text_size(12.5).text_color(c.grey500).child("Request")).child(div().id("dep-request-id").font_family(MONO).text_size(12.5).child(x.request_id)))
        .children(x.results.map((p, i) => v_flex().id(`dep-request-r${i}`).py(4).border_t(1).border_color(c.rule)
          .child(h_flex().gap(6).child(div().w(18).flex_none().text_size(12.5).text_color(c.grey500).child(String(p.position))).child(div().flex_1().min_w(0).truncate().text_size(12.5).child(p.title))
            .when((p.events ?? []).length > 0, (y) => y.child(Badge(c, p.events[p.events.length - 1], "good"))))
          .when(Boolean(p.signals), (y) => y.child(div().pl(24).text_size(12.5).line_height("18px").text_color(c.grey500)
            .child(p.signals.filter((g) => g.value).slice(0, 4).map((g) => `${g.label} ${Number(g.value).toFixed(2)}`).join(", "))))))
        .when(Boolean(x.shadow), (e2) => e2.child(div().id("dep-request-shadow").text_size(12.5).text_color(c.grey900)
          .child(this.w(x.shadow.same ? "Shadowed: the candidate would have returned the same top 10." : "Shadowed: the candidate would have returned a different top 10."))))
        .when((x.lines ?? []).length > 0, (e2) => e2.child(v_flex().id("dep-request-lines").pt(6).gap(1).border_t(1).border_color(c.rule)
          .child(div().pb(2).text_size(12.5).text_color(c.grey500).child("Log lines of this request"))
          .children(x.lines.map((ln, i) => h_flex().id(`dep-request-line-${i}`).gap(8).items_start()
            .child(div().w(84).flex_none().font_family(MONO).text_size(12.5).text_color(c.grey500).child(clock(ln.ts)))
            .child(div().flex_1().min_w(0).font_family(MONO).text_size(12.5).line_height("18px").text_color(ln.level === "error" ? c.fail : c.grey900).child(ln.text)))))));
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
      const r = await api(`/api/projects/${enc(this.pid())}/staging/versions/${n}/rollback`, { body: { reason: this.verWhy ?? "" }, scoped: false });
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
      .child(div().pb(6).text_size(12.5).text_color(c.grey500).child(d ? d.text : "Reading the versions..."))
      .children((d?.rows ?? []).map((r) => v_flex().id(`dep-version-${r.n}`).py(6).border_t(1).border_color(c.rule)
        .child(h_flex().gap(10).items_center().flex_wrap()
          .child(div().w(36).flex_none().text_size(12.5).font_semibold().child(`v${r.n}`))
          .child(div().flex_1().min_w(160).truncate().text_size(12.5).text_color(c.navy).child(short(r.model_version)))
          .when(r.live, (e) => e.child(div().flex_none().text_size(12.5).text_color(c.good).child("Running now")))
          .child(div().flex_none().text_size(12.5).text_color(c.grey500).child(`${String(r.at).slice(0, 16).replace("T", " ")} by ${r.by ?? "someone"}`))
          .when(Boolean(r.can_roll_back) && !this.busy, (e) => e.child(Button(c, `dep-version-${r.n}-back`, "Roll back", (_e, cx) => { this.verAsk = r.n; cx.notify(); }, { small: true }))))
        .when(this.verAsk === r.n, (e) => e.child(h_flex().id("dep-version-ask").mt(6).p(8).gap(8).items_center().bg(c.surface2).border_1().border_color(c.rule)
          .child(v_flex().flex_1().min_w(0).gap(6)
            .child(div().text_size(12.5).text_color(c.grey900).child(`Put ${short(r.model_version)} back? What runs now keeps answering until it does. Say why, so the next person knows.`))
            .child(h_flex().gap(6).flex_wrap().children(["It gives worse results", "It is too slow", "It is failing requests", "It was deployed by mistake", "Another reason"].map((why, i) =>
              div().id(`dep-version-why-${i}`).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
                .border_color(this.verWhy === why ? c.blue : c.grey300).bg(this.verWhy === why ? c.blueLight : c.surface).text_color(this.verWhy === why ? c.blue : c.navy)
                .on_click((_e, cx) => { this.verWhy = why; cx.notify(); }).child(why)))))
          .child(Button(c, "dep-version-yes", "Roll back", (_e, cx) => this.rollBackTo(cx, r.n), { small: true, disabled: !this.verWhy }))
          .child(Button(c, "dep-version-no", "Not now", (_e, cx) => { this.verAsk = null; this.verWhy = null; cx.notify(); }, { small: true }))))))
      .child(this.rollbacksBox(c));
  }

  /** Who rolled back, when and why, and whether Captain does it by itself in a version's first minutes. */
  rollbacksBox(c) {
    const d = this.backs, auto = d?.auto ?? {};
    const set = (minutes) => (_e, cx) => cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(this.pid())}/staging/rollbacks/auto`, { method: "PUT", body: { minutes }, scoped: false });
      if (r.ok && this.backs) this.backs.auto = r.data;
      cx.notify();
    });
    const chip = (id, label, on, click) => div().id(id).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).on_click(click).child(label);
    return v_flex().id("dep-rollbacks").w_full().mt(10).pt(10).gap(6).border_t(1).border_color(c.rule)
      .child(div().text_size(12.5).font_medium().text_color(c.navy).child("Roll back by itself when a new version is unhealthy"))
      .child(h_flex().gap(6).flex_wrap()
        .child(chip("dep-auto-off", "Off", !auto.on, set(0)))
        .children([[10, "In its first 10 minutes"], [30, "In its first 30 minutes"], [60, "In its first hour"]].map(([m, label]) => chip(`dep-auto-${m}`, label, auto.minutes === m, set(m)))))
      .child(div().id("dep-auto-text").text_size(12.5).text_color(c.grey500).child(auto.text ?? "Reading..."))
      .children((d?.rows ?? []).slice(0, 5).map((r, i) => div().id(`dep-rollback-${i}`).text_size(12.5).text_color(c.grey900)
        .child(`${String(r.at).slice(0, 16).replace("T", " ")} UTC, ${r.by} put ${short(r.to)} back in place of ${short(r.from)}. ${r.reason}`)));
  }

  /** The bottom of the project page: only where to deploy, or what is deployed (ship-view.js). */
  section(c) { return ShipSection(this, c); }

  /** The deployed model's own screen, plain, with the rest behind "More" (ship-view.js). */
  deployed(c) { return DeployedView(this, c); }

}
