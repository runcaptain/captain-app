// CAPTAIN: Deployments: every deployed model across projects and clouds, as each cloud reports it. The place to
// see what is deployed, where, how much traffic it takes and what it costs, and to stop, start, go back a version
// or remove one. GET /api/deployed asks each connected cloud and corrects Captain's records from the answer, so
// this screen and the cloud cannot disagree. Opening a row shows that model's page (model.js).
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, props, run, shared, setShared } from "herdr";
import { api, watchWorkspace } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Button, Problem, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { ago, money, ms, count, Spark, Updated, deployingWords, spanWords } from "./words.js";
import { ModelPage } from "./model.js";
import { ModelCard } from "./card.js";
import { watchLive, liveState } from "./kit/live.js";

const UP = ["running", "asleep", "starting", "unknown"];

export default class CaptainDeployments extends View {
  init(_props, cx) {
    this.d = null;            // GET /api/deployed
    this.problem = null;
    this.created = null;      // GET /api/deployed/created
    this.nums = {};           // project -> the last 24 hours of numbers
    this.shownEarlier = false; // whether the models stopped earlier are opened up
    this.cloud = null;        // a cloud to show only, or null for all
    this.project = null;
    this.ask = null;          // an open question: {kind, id, text, yes}
    this.busy = {};           // row id -> what is being done to it, in words
    this.following = {};      // row id -> the job doing it: {job, words, kind, read, heard, polling}
    // For the picture check only, which can't click: do one action as if its button were pressed, once the list
    // is read. "stop" or "remove", on the first model that is in a cloud or on this computer. Honoured only in the
    // app's test window (the app sets `app.testWindow` at each start): in a person's app it does nothing, so a
    // layout or a link can never stop or remove a model by naming this.
    this.press = shared("app.testWindow") === true ? (props()?.press ?? null) : null;
    this.note = null;         // what the last action said
    this.projects = null;     // for "Link to a project"
    this.page = null;         // the model page, when a row is open
    this.menu = null;         // which picker is open: "cloud", "project" or none
    this.shownSetup = {};     // cloud -> whether what Captain created there is opened up
    this.wanted = props()?.open ?? null;  // "<project>:<env>": open that model's page once the list is read
    // The picture check hands the screen its rows (props.demo), so every state can be drawn with nothing deployed
    // and no cloud asked. A screen drawn this way does nothing: its buttons reach no service.
    this.demo = props()?.demo ?? null;
    // The line to the change feed. A quiet stand-in until it is opened below, so a page opened while the screen
    // is still being set up (a sample does that, and so can a link from another screen) has something to call.
    this.live = { up: () => Boolean(this.demo), kick() {} };
    if (this.demo) {
      this.d = this.demo.deployed;
      this.created = this.demo.created ?? null;
      this.nums = this.demo.numbers ?? {};
      this.shownEarlier = Boolean(this.demo.earlier);
      this.busy = { ...(this.demo.busy ?? {}) };
      this.readAt = Date.now();
      if (this.demo.open) this.openRow(this.d.rows.find((r) => r.id === this.demo.open), cx, this.demo.part ?? null);
      return;
    }
    this.asked = 0;           // the `at` of the last request from another screen that was handled
    this.reads = 0;           // how many times the list has been asked for
    this.floor = 0;           // answers to asks numbered below this are older than the person's last action
    this.readAt = 0;          // when the list on screen was read
    this.numsAt = 0;          // when the numbers were last read
    this.takeOpen(cx);
    this.load(cx);
    watchWorkspace(cx, (cx) => { this.page = null; this.load(cx); });
    // Live: the screen listens to the app's change feed and reads again the moment something it shows changes:
    // the list, an open model's page, a job it started. Whoever made the change, here, on another screen, in a
    // terminal or in the cloud's own console. It asks nothing on a clock while the channel is up.
    this.live = this.demo ? this.live  // a sample has nothing to listen to
      : this.listen(cx);
    // The page's own clock, once a second: a deploying row's seconds and "Not updating". It reads nothing.
    cx.timer.every(1000, (cx) => { if (this.deployingNow() || Object.keys(this.following).length || !this.live.up()) cx.notify(); this.giveUp(cx); });
    // An open model's numbers are paid reads with no topic of their own: once a minute, as before.
    cx.timer.every(60000, (cx) => this.page?.refresh(cx));
    // A link on another screen can't reach this screen's props: it sets the shared "open" value and selects this
    // place. Looked at a few times a second, as the other screens do, so the link lands without a wait.
    cx.timer.every(300, (cx) => this.takeOpen(cx));
  }

  /** What another screen asked this one to show: one model's page ("Open in Deployments" on a project's page), or
   *  the list ("All deployments"). Each request carries the time it was made, and is handled once. */
  takeOpen(cx) {
    const o = shared("captain.open");
    if (!o || o.section !== "deployments" || !o.at || o.at === this.asked) return;
    this.asked = o.at;
    // The sidebar's entry asks for the view's home, every click, the entry already shown included. Home is the
    // list: any model's page is closed and forgotten, with whatever was open on it, and so is a model waiting to be
    // opened, an open question and an open picker. A request for home wins over a page opened a moment before.
    if (o.home || !o.open) { this.page = null; this.wanted = null; this.ask = null; this.menu = null; return cx.notify(); }
    const row = this.d?.rows.find((x) => x.id === o.open);
    if (row) this.openRow(row, cx);
    else { this.wanted = o.open; this.load(cx, true); }  // not read yet, or made a moment ago: ask, then open it
  }

  /** The kit's one live channel (kit/live.js), in the shape this screen uses: `up()` says whether it is alive.
   *  It watches the list, and every deployed model and every job by kind, and `heard` picks its own from them, so
   *  a page opened or a job started is listened to already. While the channel is down the kit calls every two
   *  seconds ("fallback"), which reads the list and the jobs again. */
  listen(cx) {
    watchLive(cx, ["deployed", "deployment:", "job:"], (cx, why, topics) => this.heard(cx, why === "changed" ? topics : [], why !== "changed", why === "fallback"));
    return { up: () => liveState().state === "live", kick() {} };
  }

  /** Something this screen shows changed. `reset` is the first answer, or a feed that lost track: read it all. */
  heard(cx, changed, reset, fallback = false) {
    if (reset || changed.includes("deployed")) this.load(cx);
    if (reset || changed.some((t) => t.startsWith("job:"))) this.follow(cx, reset ? null : changed);
    // An open model's page is five reads, one of them to the cloud. While the feed is down this is called every
    // two seconds: the list and the jobs are read then, and the page keeps to its own once-a-minute read.
    if (this.page && !fallback && (reset || changed.includes(`deployment:${this.page.row.project}`))) this.page.refresh(cx);
  }

  deployingNow() {
    return Boolean(this.d?.rows?.some((r) => r.state === "deploying" || r.deploying));
  }

  /** Read what is deployed, and the numbers beside it. The list is read every few seconds: Captain's records and
   *  the clouds' own listings cost nothing to ask. The numbers are read once a minute, because a cloud charges for
   *  each read of those. An answer that set out before the person's last action is dropped: the screen never
   *  shows a state older than the last thing they did. */
  load(cx, fresh = false) {
    if (this.demo) return;
    const mine = ++this.reads;
    cx.spawn(async (cx) => {
      const r = await api(`/api/deployed${fresh ? "?fresh=true" : ""}`, { timeout: 60 });
      if (mine < this.floor) return;  // asked before the last action finished: older than what the person did
      if (r.ok) { this.d = r.data; this.problem = null; this.readAt = Date.now(); this.settle(cx, r.data.rows, mine); } else if (!this.d) this.problem = r;
      if (r.ok && this.press) {
        const row = r.data.rows.find((x) => x.in_cloud && x.project && x.state !== "deploying");
        const what = this.press;
        if (row) {
          this.press = null;
          if (what === "stop") this.stop(row, cx);
          if (what === "remove") { this.remove(row, cx); this.ask?.go(cx); }
        }
      }
      if (r.ok && this.wanted) {  // asked for by another screen ("Open in Deployments" on a project's page)
        const row = r.data.rows.find((x) => x.id === this.wanted);
        this.wanted = null;
        if (row) this.openRow(row, cx);
      }
      // A model's page open on a row keeps to the row as it is now, or closes when the row is gone.
      if (r.ok && this.page) {
        const now = r.data.rows.find((x) => x.id === this.page.row.id);
        if (now) this.page.row = now; else this.page = null;
      }
      cx.notify();
      if (!r.ok) return;
      const made = await api("/api/deployed/created");
      if (made.ok && mine >= this.floor) this.created = made.data;
      cx.notify();
      if (!fresh && Date.now() - this.numsAt < 60000) return;
      this.numsAt = Date.now();
      // Traffic and cost for each row that belongs to a project.
      for (const row of r.data.rows.filter((x) => x.link !== "unlinked" && x.project)) {
        const n = await api(`/api/projects/${encodeURIComponent(row.project)}/deployed/numbers?range=24h&env=${encodeURIComponent(row.env ?? "staging")}`, { timeout: 60 });
        if (n.ok) this.nums[row.id] = n.data;
        cx.notify();
      }
    });
  }

  // ---- doing things to a deployment -----------------------------------------------------------------

  /** Do something to a deployment and show it happening. While it runs the row says what is being done, with the
   *  step it is on, and offers no button that would do it again. Work the services do as a job (stopping,
   *  starting, removing) is followed by `follow`, on the screen's timer. `kind` says what the list will show when
   *  it is over ("gone" for a stop or a removal), so the row can be settled from the list alone. */
  async act(cx, row, words, call, kind = "other") {
    this.ask = null;
    if (this.demo) return cx.notify();
    this.busy[row.id] = words;
    this.note = null;
    cx.notify();
    let r;
    try { r = await call(); } catch (e) { r = { ok: false, error: String(e?.message ?? e) }; }
    if (r.ok && r.data?.job) {
      this.following[row.id] = { job: r.data.job, words, kind, read: this.reads, heard: Date.now(), polling: false };
      this.live.kick(cx);  // listen to this job from now
      this.follow(cx);     // and read it once: it may be over already
      return cx.notify();
    }
    this.finish(cx, row.id, r.ok ? (r.data?.text ?? null) : plainError(r.error, "That did not work."));
  }

  /** An action is over: the row gets its buttons back, what was said is shown, and the list is read at once. */
  finish(cx, id, said) {
    delete this.busy[id];
    delete this.following[id];
    this.note = said ?? null;
    this.floor = this.reads + 1;  // anything read before now is older than this action
    this.load(cx, true);
  }

  /** Ask after each job being followed, once a second. Nothing here waits in a loop: the script engine has no
   *  timer of its own to wait on, and a loop that tried one died after its first look, leaving the row "Stopping"
   *  for ever. A job that can't be asked after for two minutes is given up on, and the list says where it stands. */
  giveUp(cx) {
    for (const [id, f] of Object.entries(this.following))
      if (Date.now() - f.heard > 120000) this.finish(cx, id, `${f.words}: Captain could not tell how that ended. The list shows where it stands now.`);
  }

  follow(cx, changed = null) {
    for (const [id, f] of Object.entries(this.following)) {
      if (changed && !changed.includes(`job:${f.job}`)) continue;
      if (f.polling) { f.again = true; continue; }  // told again while reading: read once more after
      f.polling = true;
      cx.spawn(async (cx) => {
        let j;
        try { j = await api(`/api/jobs/${encodeURIComponent(f.job)}`, { timeout: 10 }); } catch (e) { j = { ok: false }; }
        f.polling = false;
        if (this.following[id] !== f) return;  // settled meanwhile
        if (f.again) { f.again = false; this.follow(cx, [`job:${f.job}`]); }
        if (!j.ok) return;  // not heard: the next change, or the fallback tick, asks again
        f.heard = Date.now();
        const log = j.data?.log ?? [];
        // The job's own title is not a step ("stopping the deployed model of <id>"): the row already says what is being done.
        const step = [j.data?.message, log[log.length - 1]].find((x) => x && x !== log[0] && x !== "done" && !String(x).startsWith("done: "));
        this.busy[id] = step ? `${f.words}: ${String(step).replace(/[.]+$/, "")}` : f.words;
        // What the job said it did, when it said anything. Otherwise the list, read at once, is what there is to say.
        if (j.data?.status === "done") return this.finish(cx, id, j.data?.result?.text ?? null);
        if (j.data?.status === "failed") return this.finish(cx, id, plainError(j.data?.error ?? j.data?.message, "That did not work."));
        cx.notify();
      });
    }
  }

  /** The list is the truth. A stop or a removal is over the moment the list, read after it began, no longer has
   *  the model in a cloud, whatever the job says or fails to say. */
  settle(cx, rows, mine) {
    for (const [id, f] of Object.entries(this.following)) {
      if (f.kind !== "gone" || mine <= f.read) continue;
      const row = rows.find((x) => x.id === id);
      if (row && row.in_cloud && !["stopped", "gone"].includes(row.state)) continue;
      delete this.busy[id];
      delete this.following[id];
      this.note = row?.state_text ?? `${f.words}: done.`;
    }
  }

  stop(row, cx) {
    cx.spawn((cx) => this.act(cx, row, row.cloud === "local" ? "Stopping" : `Stopping on ${row.cloud_label}`,
      () => api(`/api/projects/${encodeURIComponent(row.project)}/unstage`, { method: "POST", body: { env: row.env }, timeout: 600 }), "gone"));
  }

  /** Start a stopped model again, in the place it was. The services are asked for the plan first, so the question
   *  names the place, says when that place has to be set up again, how long it takes and what it costs. It never
   *  starts anywhere else: with no plan for that place, nothing is asked and the reason is shown. */
  start(row, cx) {
    const where = { host: row.cloud, login: row.login, candidate: row.model_version, env: row.env };
    this.busy[row.id] = "Checking where it will run";
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${encodeURIComponent(row.project)}/deploy`, { method: "POST", body: { ...where, dry_run: true }, timeout: 60 });
      delete this.busy[row.id];
      if (!r.ok) { this.note = plainError(r.error, `It can't be started again on ${row.cloud_label} just now.`); return cx.notify(); }
      const p = r.data;
      this.ask = { id: row.id, yes: p.first_time && row.cloud !== "local" ? `Set up ${p.label} and start it` : "Start it again",
        text: `Start ${row.model_label ?? row.model_version} again on ${p.label ?? row.cloud_label}? `
          + (p.first_time && row.cloud !== "local" ? `This will set up ${p.label} again. ` : "")
          + [p.minutes?.text, p.cost?.text].filter(Boolean).join(" "),
        go: (cx) => cx.spawn((cx) => this.act(cx, row, `Starting on ${p.label ?? row.cloud_label}`, () => api(`/api/projects/${encodeURIComponent(row.project)}/deploy`,
          { method: "POST", body: { ...where, confirm: true }, timeout: 60 }))) };
      cx.notify();
    });
  }

  remove(row, cx) {
    const found = row.link === "unlinked";
    this.ask = { id: row.id, yes: "Remove it from the cloud",
      text: found ? `Remove ${row.name} from your ${row.cloud_label} account? It cannot be undone.`
        : `Remove ${row.project_name}'s model from ${row.cloud_label}? It stops answering, and what Captain made for it there is deleted. It cannot be undone.`,
      go: (cx) => cx.spawn((cx) => this.act(cx, row, `Removing from ${row.cloud_label}`, () => found
        ? api("/api/deployed/remove", { method: "POST", body: { id: row.id, confirm: true }, timeout: 600 })
        : api(`/api/projects/${encodeURIComponent(row.project)}/deploy?env=${encodeURIComponent(row.env ?? "staging")}`, { method: "DELETE", timeout: 600 }), "gone")) };
    cx.notify();
  }

  forgetIt(row, cx) {
    this.ask = { id: row.id, yes: "Forget it",
      text: `Forget ${row.project_name}'s stopped model? Captain removes its own record of it. Nothing in the cloud changes, because nothing of it is there.`,
      go: (cx) => cx.spawn((cx) => this.act(cx, row, "Forgetting", () => api("/api/deployed/forget", { method: "POST", body: { project: row.project, env: row.env, confirm: true } }))) };
    cx.notify();
  }

  linkTo(row, cx) {
    this.ask = { id: row.id, pick: true, text: `Which project should ${row.name} belong to?` };
    cx.notify();
    if (!this.projects) cx.spawn(async (cx) => { const r = await api("/api/projects"); this.projects = r.ok ? (r.data.projects ?? r.data.rows ?? r.data ?? []) : []; cx.notify(); });
  }

  removeSetup(place, cx) {
    const row = { id: `setup-${place.cloud}` };
    this.ask = { id: row.id, yes: "Remove everything",
      text: `Remove everything Captain created in your ${place.cloud_label} account? The next deploy there makes it again, which takes a few minutes.`,
      go: (cx) => cx.spawn((cx) => this.act(cx, row, "Removing", () => api(`/api/serving/setup?cloud=${place.cloud}`, { method: "DELETE", timeout: 600 }))) };
    cx.notify();
  }

  /** "Deploy a model": a model is deployed from its project's page. With one project, go there. With several, ask
   *  which. With none, go to the projects, where one is made. */
  deployFrom(cx) {
    const go = (list) => {
      if (list.length === 1) return this.openProject({ project: list[0].id, project_name: list[0].name });
      if (!list.length) return run("layout.select", { stack: props()?.stack ?? "sections", child: "project" });
      this.ask = { id: "deploy", pick: "deploy", text: "Which project's model do you want to deploy?" };
      cx.notify();
    };
    if (this.projects) return go(this.projects);
    cx.spawn(async (cx) => { const r = await api("/api/projects"); this.projects = r.ok ? (r.data.projects ?? r.data.rows ?? r.data ?? []) : []; go(this.projects); cx.notify(); });
  }

  openProject(row) {
    setShared("captain.scope", { kind: "project", id: row.project, label: row.project_name ?? row.project });
    return run("layout.select", { stack: props()?.stack ?? "sections", child: "project" });
  }

  // ---- drawing ---------------------------------------------------------------------------------------

  // An open question takes the screen's one filled button.
  question(c, cx) {
    const a = this.ask;
    const box = v_flex().id("dep-ask").gap(8).p(12).border_1().border_color(c.rule).bg(c.surface)
      .child(div().text_size(13).line_height("19px").text_color(c.navy).child(a.text));
    if (a.pick) {
      const list = this.projects;
      return box.child(h_flex().gap(8).flex_wrap()
        // Picking a project either gives it something found in a cloud, or goes to it to deploy from there.
        .children((list ?? []).map((p) => (a.pick === "deploy"
          ? Button(c, `dep-deploy-in-${p.id}`, p.name ?? p.id, (_e, cx) => { this.ask = null; cx.notify(); this.openProject({ project: p.id, project_name: p.name }); }, { small: true })
          : Button(c, `dep-link-${p.id}`, p.name ?? p.id, (_e, cx) => cx.spawn((cx) => this.act(cx, { id: a.id }, "Linking",
            () => api("/api/deployed/link", { method: "POST", body: { id: a.id, project: p.id } }))), { small: true }))))
        .when(list && !list.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("There are no projects yet. Make one first.")))
        .when(!list, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("Reading your projects...")))
        .child(Button(c, "dep-ask-no", "Cancel", (_e, cx) => { this.ask = null; cx.notify(); }, { small: true })));
    }
    return box.child(h_flex().gap(8)
      .child(Button(c, "dep-ask-yes", a.yes, (_e, cx) => a.go(cx), { kind: "primary", small: true }))
      .child(Button(c, "dep-ask-no", "Cancel", (_e, cx) => { this.ask = null; cx.notify(); }, { small: true })));
  }

  /** One model as its card (card.js): the one action that matters now, and the rest behind Details. `page` is the
   *  card as the header of the model's own page, where the rest is shown and Details is not offered. */
  card(c, r, page = false) {
    const up = UP.includes(r.state), found = r.link === "unlinked", inCloud = r.in_cloud !== false;
    const deploying = r.state === "deploying" && r.deploying;
    const a = (id, label, go, kind) => ({ id: `dep-${id}-${r.id}`, label, go, kind });
    const details = a("open", "Details", (_e, cx) => this.openRow(r, cx));
    const remove = a("remove", "Remove from the cloud", (_e, cx) => this.remove(r, cx));
    const start = (label) => (r.model_version ? a("start", label, (_e, cx) => this.start(r, cx)) : null);
    let action = null, more = [];
    if (deploying) { /* nothing can be done to a model still being made */ }
    else if (found) { action = a("link", "Link to a project", (_e, cx) => this.linkTo(r, cx)); more = [remove]; }
    else if (r.link === "unknown") more = [details];
    else if (up) { action = a("stop", "Stop", (_e, cx) => this.stop(r, cx), "secondary"); more = [details]; }
    // In a cloud and not answering: the one thing to do is deploy it again. It can also be removed.
    // Removing it is behind Details, on its own page.
    else if (inCloud) { action = start("Deploy again"); more = page ? [remove] : [details]; }
    // Stopped earlier: nothing of it is in a cloud. It can be started again, or forgotten.
    else { action = start("Start again"); more = [a("forget", "Forget it", (_e, cx) => this.forgetIt(r, cx))]; }
    if (page) more = more.filter((x) => x !== details);
    const copied = this.copied?.startsWith(`${r.id}:`) ? this.copied.slice(r.id.length + 1) : null;
    return ModelCard(c, r, {
      numbers: this.nums[r.id] ?? null, busy: this.busy[r.id] ?? null, action, more, copied,
      question: this.ask?.id === r.id ? this.question(c) : null,
      // What Captain corrected in its own record is bookkeeping: on the model's page, not on its card in the list.
      notes: [...(page ? (r.corrected ?? []) : []), ...(r.deploying && !deploying ? [`A new version is on its way. ${deployingWords(r.deploying)}`] : [])],
      raw: page,
      onCopy: (what, _e, cx) => this.copy(r, what, cx),
    });
  }

  /** Copy the address, or a request that tries it. The request is read from the model's own facts when asked for. */
  copy(r, what, cx) {
    const done = (text) => { if (text) cx.write_to_clipboard?.(String(text)); this.copied = `${r.id}:${what}`; cx.notify(); };
    if (what === "address" || this.demo) return done(what === "address" ? r.address : this.demo?.model?.where?.facts?.find((f) => f.id === "call")?.value);
    cx.spawn(async (cx) => {
      const w = await api(`/api/projects/${encodeURIComponent(r.project)}/deployed/where?env=${encodeURIComponent(r.env ?? "staging")}`, { timeout: 60 });
      const call = w.ok ? w.data?.facts?.find((f) => f.id === "call")?.value : null;
      if (call) done(call); else { this.note = "A test request could not be made for this model just now."; cx.notify(); }
    });
  }

  row(c, r) {
    return this.card(c, r);
  }

  openRow(r, cx, part = null) {
    this.page = new ModelPage(r, part, () => { this.page = null; }, this.demo?.model ?? null);
    this.page.isLive = () => !this.demo && this.live.up();  // its title says "Live" while the channel is up
    this.page.load(cx);
    this.live.kick(cx);  // listen to this model from now
    cx.notify();
  }

  // What the first deploy made in one account. Closed at first, as one line: it is there to be found and removed,
  // not the thing this screen is about.
  setup(c, place) {
    const id = `setup-${place.cloud}`;
    const open = Boolean(this.shownSetup[place.cloud]) || this.ask?.id === id;
    const n = place.things.length;
    const line = h_flex().id(`dep-setup-${place.cloud}`).gap(10).items_center().py(8)
      .child(Brand(c, place.cloud, 14))
      .child(div().flex_1().min_w(0).text_size(13).line_height("19px").text_color(c.grey900)
        .child(`Set up in your ${place.cloud_label} account: ${n} thing${n === 1 ? "" : "s"} Captain created so it can deploy there.`))
      .child(Button(c, `dep-setup-show-${place.cloud}`, open ? "Hide" : "Show", (_e, cx) => { this.shownSetup[place.cloud] = !open; cx.notify(); }, { small: true }));
    if (!open) return line;
    const where = [place.region, "login_shown" in place ? place.login_shown : place.login].filter(Boolean).join(", ");
    return v_flex().gap(6).pb(10).border_b(1).border_color(c.rule)
      .child(line)
      .when(Boolean(where), (e) => e.child(div().pl(24).text_size(12.5).text_color(c.grey500).child(where)))
      .children(place.things.map((t, i) => h_flex().id(`dep-thing-${place.cloud}-${i}`).pl(24).gap(8).items_start()
        .child(div().w(150).flex_none().truncate().text_size(12.5).text_color(c.grey500).child(t.kind_label ?? ""))
        .child(div().w(230).flex_none().truncate().text_size(12.5).text_color(c.navy).child(t.short ?? t.name))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.grey500).child(t.what_for))))
      .when(Boolean(place.in_use_by?.length), (e) => e.child(div().pl(24).text_size(12.5).text_color(c.grey500)
        .child(`In use by ${place.in_use_by.join(", ")}. Stop that model first to remove these.`)))
      .when(this.ask?.id === id, (e) => e.child(div().pl(24).child(this.question(c))))
      .when(this.ask?.id !== id && place.can_remove, (e) => e.child(h_flex().pl(24).child(this.busy[id]
        ? div().text_size(12.5).text_color(c.grey500).child("Removing...")
        : Button(c, `dep-setup-remove-${place.cloud}`, "Remove everything", (_e, cx) => this.removeSetup(place, cx), { small: true }))));
  }

  // Above the list, at the right: the cloud and the project as two small pickers, each shown only when there is
  // more than one to pick from. An open picker lists its choices on a line below. There is no filter by state: what
  // is in a cloud is the list, and what was stopped earlier has its own line under it.
  filters(c, rows) {
    const clouds = [...new Map(rows.map((r) => [r.cloud, r.cloud_label])).entries()];
    const projects = [...new Map(rows.filter((r) => r.project && r.link !== "unlinked").map((r) => [r.project, r.project_name ?? r.project])).entries()];
    const chip = (id, label, on, click) => div().id(id).h(26).px(10).flex().items_center().text_size(12.5).cursor_pointer()
      .border_1().border_color(on ? c.navy : c.rule).text_color(on ? c.navy : c.grey500).when(on, (e) => e.font_semibold())
      .hover((x) => x.text_color(c.navy)).on_click(click).child(label);
    const set = (k, v) => (_e, cx) => { this[k] = v; this.menu = null; cx.notify(); };
    const picker = (key, every, list) => div().id(`dep-pick-${key}`).h(26).px(10).flex().items_center().gap(6).text_size(12.5).cursor_pointer()
      .border_1().border_color(this.menu === key || this[key] ? c.navy : c.rule).text_color(c.navy)
      .on_click((_e, cx) => { this.menu = this.menu === key ? null : key; cx.notify(); })
      .child(div().child(this[key] ? (list.find(([id]) => id === this[key])?.[1] ?? every) : every))
      .child(svg(icon(this.menu === key ? "chevron-up" : "chevron-down")).size(12).text_color(c.grey500));
    const open = this.menu === "cloud" ? ["cloud", "Every cloud", clouds] : this.menu === "project" ? ["project", "Every project", projects] : null;
    // The pickers sit on the title's line. An open one lists its choices on a line under the title.
    if (clouds.length < 2 && projects.length < 2) return { pickers: null, list: null };
    return {
      pickers: h_flex().id("dep-filters").flex_none().gap(6).items_center()
        .when(clouds.length > 1, (e) => e.child(picker("cloud", "Every cloud", clouds)))
        .when(projects.length > 1, (e) => e.child(picker("project", "Every project", projects))),
      list: open ? h_flex().id("dep-pick-list").gap(6).flex_wrap().justify_end()
        .child(chip(`dep-${open[0]}-all`, open[1], this[open[0]] === null, set(open[0], null)))
        .children(open[2].map(([id, name]) => chip(`dep-${open[0]}-${id}`, name, this[open[0]] === id, set(open[0], id)))) : null,
    };
  }

  render(cx) {
    const c = palette(isDark(theme()));
    const frame = (body) => v_flex().id("deployments").size_full().bg(c.canvas).font_family(FONT).text_color(c.navy).child(body);
    if (this.page) return frame(this.page.render(c, cx, this.card(c, this.page.row, true)));
    const d = this.d;
    if (!d) return frame(div().p(24).child(Problem(c, this.problem ?? { starting: true }, (cx) => this.load(cx))));
    const picked = (r) => (!this.cloud || r.cloud === this.cloud) && (!this.project || r.project === this.project);
    // The list is what exists in a cloud now: running, starting, asleep or failed. A model that was stopped is not
    // in a cloud: Captain only remembers it, and it is listed apart.
    const here = d.rows.filter((r) => r.in_cloud !== false), earlier = d.rows.filter((r) => r.in_cloud === false);
    const rows = here.filter(picked);
    const notAsked = d.asked.filter((a) => !a.ok);
    const places = this.created?.places ?? [];
    const none = !here.length;
    // Nothing at all: no model, none stopped earlier, nothing set up in any account. The page is one quiet card
    // that says what this screen is for and offers the one thing to do.
    const bare = none && !earlier.length && !places.length;
    const filters = none ? { pickers: null, list: null } : this.filters(c, here);
    const head = h_flex().h(56).flex_none().px(24).gap(12).items_center().border_b(1).border_color(c.rule)
      .child(div().text_size(18).font_semibold().text_color(c.navy).child("Deployments"))
      // With nothing deployed the page itself says so: the line beside the title would only repeat it.
      .child(div().id("dep-summary").flex_1().min_w(0).truncate().text_size(13).text_color(c.grey500).child(d.text))
      // The cloud and the project to show, at the right of the title's line. Each only when there is a choice.
      .when(Boolean(filters.pickers), (e) => e.child(filters.pickers))
      .when(!here.length && !bare && !this.ask, (e) => e.child(Button(c, "dep-go-projects", "Deploy a model", (_e, cx) => this.deployFrom(cx), { kind: "primary", small: true })))
      // It keeps itself up to date, and says when it last was, or that a cloud could not be reached. No button to
      // press: a small refresh icon is there for someone who wants to ask now.
      .child(Updated(c, "dep-updated", { readAt: this.readAt, icon: svg(icon("refresh-cw")).size(14), onRefresh: (_e, cx) => this.load(cx, true),
        // The list is read every 5 seconds. If the last good read is older than 15, the screen is not keeping up,
        // and it says so: "Updated 3 min ago" beside a row that is no longer true reads as if all were well.
        problem: notAsked.length ? `${[...new Set(notAsked.map((a) => a.cloud_label))].join(" and ")} could not be reached. Trying again.`
          : this.readAt && !this.demo && !this.live.up() && Date.now() - this.readAt > 15000 ? "Not updating. Refresh" : null,
        live: this.live.up() }));
    const deploy = (small) => Button(c, "dep-go-projects", "Deploy a model", (_e, cx) => this.deployFrom(cx), { kind: "primary", small });
    const n = earlier.length;
    const body = v_flex().gap(18)
      .when(Boolean(filters.list), (e) => e.child(filters.list))
      .when(Boolean(this.note), (e) => e.child(div().id("dep-note").text_size(13).line_height("19px").text_color(c.navy).child(this.note)))
      // Why a cloud could not be reached, once, in words a person can act on. What is shown for it is Captain's record.
      .children(notAsked.map((a, i) => div().id(`dep-not-asked-${i}`).text_size(12.5).line_height("18px").text_color(c.grey500)
        .child(`${a.cloud_label}${a.login && a.login.toLowerCase() !== a.cloud ? ` (${a.login})` : ""}: ${plainError(a.error, "no reason given")}. What is shown for it is Captain's own record.`)))
      // The list of models is what this screen is: it is always here, under its own heading, even with nothing in it.
      .child(v_flex().id("dep-models").gap(10)
        .when(this.ask?.id === "deploy", (e) => e.child(this.question(c)))
        // With nothing at all (no model, none stopped earlier, nothing set up in any account), one line says what
        // this screen is for. With anything else on the page, the page says it.
        .when(bare, (e) => e.child(v_flex().id("dep-empty").w_full().items_center().gap(12).px(24).py(56).bg(c.surface).border_1().border_color(c.rule)
          .child(svg(icon("rocket")).size(28).text_color(c.grey500))
          .child(div().text_size(17).font_semibold().text_color(c.navy).child("Nothing is deployed yet"))
          .child(div().id("dep-what-for").max_w(520).text_center().text_size(12.5).line_height("19px").text_color(c.grey500)
            .child("A model you deploy from a project's page shows here: where it runs, the address to call, its traffic and what it costs. You can stop it, start it again or remove it from here."))
          .when(!this.ask, (x) => x.child(div().pt(6).child(deploy(false))))))
        .child(v_flex().id("dep-list").gap(14)
          .children(rows.map((r) => this.row(c, r)))
          .when(!none && !rows.length, (e) => e.child(div().id("dep-none-shown").py(16).text_size(13).text_color(c.grey500).child("No model matches. Change the cloud or the project above.")))))
      // Stopped earlier: one closed line. Nothing of these is in a cloud. Captain remembers them so they can be
      // started again, or forgotten.
      .when(n > 0, (e) => e.child(v_flex().id("dep-earlier")
        .child(h_flex().gap(10).items_center().py(8)
          .child(div().id("dep-earlier-line").flex_1().text_size(13).line_height("19px").text_color(c.grey900).child(`Stopped earlier: ${n} model${n === 1 ? "" : "s"}.`))
          .child(Button(c, "dep-earlier-show", this.shownEarlier || earlier.some((r) => this.ask?.id === r.id) ? "Hide" : "Show",
            (_e, cx) => { this.shownEarlier = !this.shownEarlier; cx.notify(); }, { small: true })))
        .when(this.shownEarlier || earlier.some((r) => this.ask?.id === r.id), (x) => x.child(v_flex().gap(14).pt(6).children(earlier.map((r) => this.row(c, r)))))))
      // Last and small: what the first deploy to each cloud made, one line each until it is opened.
      .when(Boolean(places.length), (e) => e.child(v_flex().id("dep-created").pt(6)
        .children(places.map((p) => this.setup(c, p)))));
    return frame(v_flex().size_full().child(head).child(div().id("dep-body").flex_1().min_h(0).overflow_y_scroll().px(24).py(18).child(body)));
  }
}
