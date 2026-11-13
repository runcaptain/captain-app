// CAPTAIN: the Captain sidebar, in the Captain Search desktop app's look
// (its App.tsx NavItem and index.css tokens), built from Herdr's parts:
// a gpui-kit mod a person can clone and change.
//
// Props (all optional), so a layout or a clone can reorder or add items:
//   part: "brand"            the mark only (the layout makes that block drag the window)
//   items: [{id, label, icon, badge?, run?, args?}]
//                            sections; `id` is a child of the "sections" stack unless
//                            `run` names another command. `icon` is an svg in this folder
//                            (or a path in the home); `badge` an API path whose list
//                            length shows as a count.
//   agents: false            hide the Agents section
//   stack: "sections"        the stack the items switch
//
// It also publishes the palette's Captain entries (shared "palette.extra"): tasks,
// places and "Open <name>", in plain words. This mod is always shown, so it's the one
// that keeps them current.
import { View, div, image, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, run, layout, props, notify, agents, spaces, machines, exec, nextCommand, shared, setShared, openOverlay, window as win } from "herdr";
import { api, retry, workspace, watchWorkspace, watchRecords, watchConnections } from "./kit/cst.js";
import { watchLive } from "./kit/live.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { isDark, palette } from "./kit/tokens.js";
import { getAppearance, setAppearance } from "./kit/appearance.js";
import { Explorer, projectNode } from "./explorer.js";
import { ProjectIcon } from "./kit/projecticon.js";
import { TeamViews } from "./teamviews.js";
import { askAboutScreen, EVALUATION_FORM } from "./kit/agent.js";
import { currentView } from "./kit/view.js";
import { loadPins, unpin, clearPins } from "./kit/context.js";
import { LOCAL, placesFrom, placeChanged } from "./kit/cost.js";

// The views, in the order a team works: projects, the data they read, notebooks, runs and their comparisons,
// what is deployed, the team. Six at most (Edgar, 2026-11-12: "there are a lot of views"). Experiments, Compare
// and Launch review left the screens; `cst investigate`, `cst backtest` and `cst handoff` still work in a terminal.
export const ITEMS = [
  // Projects: every project of the workspace (captain-project). One opens from there or from the Explorer.
  { id: "project", label: "Projects", was: "home all projects", icon: icon("folder"), open: { all: true } },
  { id: "data", label: "Data", was: "data and signals sources tables datasets catalogs databases", icon: "database" },
  { id: "notebooks", label: "Notebooks", was: "sql python charts", icon: icon("file-text") },
  { id: "runs", label: "Runs", was: "offline evaluations backtests rating grading off-policy replays compare comparisons regression jobs", icon: "play" },
  // Deployments: every deployed model across projects and clouds, read from the clouds (captain-deployments).
  { id: "deployments", label: "Deployments", was: "deployed models endpoints hosting stop remove traffic cost", icon: icon("server") },
  // Team collaboration was only in the palette, so people who built it couldn't find it (Edgar, 2026-11-09).
  { id: "team", label: "Team", icon: icon("users") },
];
/** Screens that left the app, for something that still asks for one (`cst app open`, an old link): where it goes now. */
/** A click on a sidebar entry. The sidebar's rule: an entry always opens its view's home (every project, the
 *  tables' groups, the list of runs, of deployments, of notebooks), never the page last looked at there, and a
 *  second click on the entry already shown goes home too. Every view honours `home: true` on shared
 *  "captain.open". A detail page opens only from a row or a link, which send the same value without `home`. */
export function openEntry(it, stack = "sections") {
  if (it.run) return run(it.run, it.args ?? {});
  setShared("captain.open", { section: it.id, home: true, ...(it.open ?? {}), at: Date.now() });
  return run("layout.select", { stack, child: it.id });
}

export const MOVED = { comparisons: "runs", investigations: null, handoff: null };
const WIDE = 216, RAIL = 52;
const STATUS = { working: "working", blocked: "needs you", done: "done", idle: "idle" };

function find(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = find(child, id);
    if (found) return found;
  }
  return null;
}

// Commands people make things with, for the New menu.

// The palette speaks in tasks and places, not commands (Edgar: "not a CLI wrapper").
// Raw cst commands sit behind one "Run a cst command..." entry; agents use the CLI.
const TASK_ENTRIES = [
  // [task, title, search words, icon: the New menu's for the same task (captain-forms/tasks.js)]
  ["dataset", "Import a dataset", "file csv parquet sql upload catalog queries judgments", "database"],
  ["database", "Connect a database", "postgres snowflake bigquery mysql redshift sql", "server"],
  ["cloud", "Connect a cloud account", "aws gcp azure login credentials", "cloud"],
  ["mlflow", "Connect MLflow", "mlflow tracking experiments models registry", "flask-conical"],
  ["compute", "Add a place to run work", "compute cloud run offline evaluations backtests serve", "boxes"],
  ["backtest", "New evaluation", "run an offline evaluation backtest compare evaluate offline ndcg qa pairs questions answers test set regression test", "flask-conical"],
  ["grading", "Start rating", "grading grade grader raters judgments labels relevance human", "square-check"],
  ["dataset-class", "New dataset class", "class type role catalog queries judgments", "tag"],
  ["rankings", "Compare your own rankings", "bring import rankings results system offline compare evaluate", "git-compare"],
  ["explain", "Explain a query", "debug why ranking stage score trace query debugger", "layers"],
  ["demo", "Load Sample North Pole", "sample example data santa workshop catalogs", "sparkles"],
  ["remove-sample", "Remove Sample North Pole", "delete sample example data", "trash-2"],
];
// New workspace: where it keeps its work, then a drop zone, then an optional model registry (captain-setup).
const SETUP = { mod: "captain-setup", kind: "dialog", width: 600, height: 640 };
const go = (section, extra) => ({ mod: "captain-go", kind: "popup", width: 1, height: 1, props: { section, ...extra } });

/** What the sidebar's cloud chip says, from GET /api/cloud/summary. It says what is true:
 *  the login that holds the workspace, or the only login, by name; several logins when none of them holds the
 *  workspace, as a count with the list in the tooltip; "No cloud connected" only when there is none.
 *  Returns {text, tip, dot: good | fail | none, provider (for its mark, when one login is shown), count, open}. */
export function cloudChipInfo(summary) {
  const short = (p) => ({ aws: "AWS", gcp: "GCP", azure: "Azure" }[p?.provider] ?? p?.label ?? p?.provider ?? "Cloud");
  const line = (p) => [short(p), p.account_hint, p.region].filter(Boolean).join(" \u00b7 ");
  const checked = (p) => (p.last_test ? (p.last_test.ok ? `signed in as ${p.last_test.identity ?? "?"}` : `last check failed: ${p.last_test.error ?? ""}`.trim()) : "not checked yet");
  const all = summary?.profiles ?? [];
  const a = summary?.active ?? (all.length === 1 ? all[0] : null);
  if (a) {
    const tip = a.last_test ? (a.last_test.ok ? `Signed in as ${a.last_test.identity ?? "?"}` : `Last check failed: ${a.last_test.error ?? ""}`) : "Not checked yet";
    const others = all.filter((p) => p.name !== a.name);
    return { text: line(a), provider: a.provider, count: Math.max(1, all.length), open: "cloud",
      dot: a.last_test?.ok ? "good" : a.last_test ? "fail" : "none",
      tip: `${line(a)}. ${tip}` + (a.used_by_storage && others.length ? `. It holds this workspace. Also connected: ${others.map(line).join(", ")}.` : "") };
  }
  if (all.length > 1) {
    return { text: `${all.length} clouds connected`, provider: null, count: all.length, open: "cloud",
      dot: all.some((p) => p.last_test && !p.last_test.ok) ? "fail" : all.every((p) => p.last_test?.ok) ? "good" : "none",
      tip: `${all.length} clouds connected: ${all.map((p) => `${line(p)} (${checked(p)})`).join(", ")}. This workspace is not kept in any of them. Click for the list.` };
  }
  return { text: "No cloud connected", provider: null, count: 0, open: "connect", dot: "none", tip: "No cloud connected. Connect a cloud login" };
}

export function staticEntries(sections) {
  return [
    ...TASK_ENTRIES.map(([task, title, words, ic]) => ({ group: "Captain", title, words, icon: icon(ic),
      overlay: task === "dataset" ? { mod: "captain-import", kind: "dialog", width: 900, height: 640, props: {} }
        : task === "rankings" ? { mod: "captain-rankings", kind: "dialog", width: 760, height: 660 }
        : task === "backtest" ? EVALUATION_FORM()
        : task === "explain" ? { mod: "captain-explain", kind: "dialog", width: 0.9, height: 0.9 }
        : task === "sandbox" ? { mod: "captain-sandbox", kind: "dialog", width: 0.9, height: 0.9 } : { mod: "captain-forms", kind: "dialog", width: 560, props: { task } } })),
    ...sections.map((s) => ({ group: "Captain", title: `Go to ${s.label}`, words: `section ${s.label} ${s.was ?? ""}`, icon: s.icon && !String(s.icon).includes("/") ? icon(s.icon) : undefined, overlay: go(s.id, s.open) })),
    { name: "captain-nav.theme-light", args: {}, group: "Captain", icon: icon("sun"), title: "Switch to light", words: "appearance theme light mode" },
    { name: "captain-nav.theme-dark", args: {}, group: "Captain", icon: icon("moon"), title: "Switch to dark", words: "appearance theme dark mode night" },
    { name: "captain-nav.theme-system", args: {}, group: "Captain", icon: icon("monitor"), title: "Match the system appearance", words: "appearance theme system auto" },
    { group: "Captain", icon: icon("folder-open"), title: "New workspace...", words: "workspace storage folder bucket s3 gcs azure create start import drop", overlay: SETUP },
    // The agent tab gets what is on screen (workspace, project, section, selection, the Next line): kit/agent.js.
    { name: "captain-nav.ask-screen", args: {}, group: "Captain", icon: icon("bot"), title: "Ask an agent about this screen", words: "agent claude codex context see looking help next this here what am I looking at" },
    { name: "app.welcome", args: {}, group: "Captain", icon: icon("compass"), title: "Show the Captain tour", words: "onboarding welcome checklist start setup" },
    { group: "Captain", icon: icon("workflow"), title: "Open workflows", words: "workflow schedule nightly steps chain automate", overlay: go("runs", { tab: "workflows" }) },
    { group: "Captain", icon: icon("users"), title: "Team collaboration", words: "company sync login shared records drafts", overlay: { mod: "captain-team", kind: "dialog", width: 960, height: 680 } },
    // The scope model, under the name people use for it; it lives in Settings (Edgar, 2026-11-09).
    { name: "app.settings", args: { section: "structure" }, group: "Captain", icon: icon("waypoints"), title: "Project Structure", words: "scope model levels brand site hierarchy terminology types explorer metadata fields settings" },
    { group: "Captain", icon: icon("cloud"), title: "Cloud and compute", words: "aws gcp azure login profile region compute", overlay: { mod: "captain-cloud", kind: "dialog", width: 760, height: 640 } },
    { group: "Captain", icon: icon("terminal"), title: "Run a cst command...", words: "cli advanced power command", overlay: { mod: "captain-commands", kind: "dialog", width: 560, height: 560, props: { menu: "all" } } },
  ];
}

/** "Open <name>" for datasets and offline evaluations, from the workspace. An evaluation opens in Runs, with its
 *  comparison in place. */
export function openEntries(datasets, _investigations = [], backtests = []) {
  return [
    ...backtests.slice(0, 200).map((b) => ({ group: "Captain", icon: icon("flask-conical"), title: `Open ${b.name}`, words: "offline evaluation backtest run compare comparison results side by side", overlay: go("runs", { backtest: b.name }) })),
    ...datasets.slice(0, 200).map((d) => ({ group: "Captain", icon: icon("table"), title: `Open ${d.name}`, words: `dataset table ${d.kind ?? ""}`, overlay: go("data", { dataset: d.name }) })),
  ];
}

export default class CaptainNav extends View {
  init(_props, cx) {
    this.workspace = null;
    this.counts = {};
    this.agentsOpen = true;
    this.state = "starting";
    this.appearance = "system";
    cx.spawn(async (cx) => { this.appearance = await getAppearance(); cx.notify(); });
    // The onboarding, in the palette from the start (the commands join once Captain's services answer).
    if (props()?.part !== "brand") {
      setShared("palette.extra", { ...(shared("palette.extra") ?? {}), "captain-app": staticEntries(this.items()), captain: [] });
      // Commands the palette and keys send this mod (captain-nav.<name>).
      cx.spawn(async (cx) => {
        for (;;) {
          const c = String(await nextCommand()).split(".").pop();
          if (c.startsWith("theme-")) {
            this.appearance = c.slice(6);
            await setAppearance(this.appearance);
          } else if (c === "space-new") {
            await this.newSpace();
          } else if (c === "ask-screen") {
            await askAboutScreen(cx);
          }
          cx.notify();
        }
      });
    }
    // Spaces from before the workspace switcher were bound to a workspace; only an investigation's stays bound, so
    // every other terminal follows the switcher's workspace.
    if (props()?.part !== "brand") {
      const map = shared("captain.spaces") ?? {};
      const kept = Object.fromEntries(Object.entries(map).filter(([, b]) => b?.investigation));
      if (Object.keys(kept).length !== Object.keys(map).length) setShared("captain.spaces", kept);
    }
    if (props()?.part !== "brand") {
      // The agent's context (kit/context.js): what the person pinned for their agent. Read at start, when this
      // window pins or removes something, and every few seconds for what `cst context add` put there.
      this.pins = [];
      const reload = (cx) => cx.spawn(async (cx) => { const next = await loadPins(); if (JSON.stringify(next) !== JSON.stringify(this.pins)) { this.pins = next; cx.notify(); } });
      // The first read is on the timer too: nothing here reads a file while the sidebar starts.
      let changed = shared("captain.context.changed") ?? null, ws = workspace(), n = 0;
      cx.timer.every(500, (cx) => {
        const now = shared("captain.context.changed") ?? null;
        if (now !== changed || workspace() !== ws || n++ % 6 === 0) { changed = now; ws = workspace(); reload(cx); }
      });
      // `cst app open <target>` (an agent showing the person something): the window puts it in shared
      // "captain.agent.open" {…, at}. Only one that arrives while this window runs is acted on.
      let opened = shared("captain.agent.open")?.at ?? null;
      cx.timer.every(300, () => {
        const t = shared("captain.agent.open");
        if (!t?.at || t.at === opened) return;
        opened = t.at;
        this.openTarget(t);
      });
    }
    // Every launch starts on Projects, on the list of every project (Edgar, 2026-11-12: "always default to
    // projects: all projects"), whatever screen the last session ended on.
    if (props()?.part !== "brand") cx.timer.after(1, () => run("layout.select", { stack: props()?.stack ?? "sections", child: "project" }));
    // The compute picker changed where runs go: the chip says so at once.
    if (props()?.part !== "brand") {
      let seen = placeChanged();
      cx.timer.every(400, (cx) => { const now = placeChanged(); if (now !== seen) { seen = now; cx.spawn(async (cx) => { await this.loadPlace(); cx.notify(); }); } });
    }
    this.explorer = new Explorer();
    this.explorer.start(cx);
    // Team views from the project's captain/views/ (teamviews.js).
    this.teamViews = new TeamViews();
    this.teamViews.start(cx);
    this.refresh(cx);
    watchWorkspace(cx, (cx) => { this.cloud = null; setShared("captain.scope", null); this.refresh(cx); this.explorer.load(cx); });
    // Live, by the one channel (kit/live.js): the chips, the counts and the Explorer are read again the moment a
    // login, a place to run work, a setting, a project, a table or a run changes. No timer re-reads them. Until
    // Captain's services answer, the channel's two-second fallback and its "connected" call are what ask again.
    watchLive(cx, ["logins", "compute", "settings", "projects", "data", "runs"], (cx, why, topics) => {
      this.refresh(cx);
      if (why !== "changed" || topics.some((t) => ["projects", "data", "runs"].includes(t))) this.explorer.load(cx);
    });
    // And at once when a form changes something, or a cloud or database is connected or removed.
    watchRecords(cx, (cx) => { this.refresh(cx); this.explorer.load(cx); });
    watchConnections(cx, (cx) => this.refresh(cx));
  }

  items() {
    return props()?.items ?? ITEMS;
  }

  /** Show what an agent asked to show (`cst app open`): a screen, a project, a record on its screen, Settings, or a
   *  form with values filled in. A form only opens: the person reads it and submits it, or doesn't. */
  openTarget(t) {
    const stack = props()?.stack ?? "sections";
    if ("settings" in t) return run("app.settings", t.settings ? { section: t.settings } : {});
    if (t.form) {
      const overlay = t.form === "dataset" ? { mod: "captain-import", kind: "dialog", width: 900, height: 640, props: {} }
        : t.form === "rankings" ? { mod: "captain-rankings", kind: "dialog", width: 760, height: 660 }
        // `cst app open form:backtest --value test=<name>` (or a=, b=, queries=, judgments=, name=): the evaluation's own form.
        : t.form === "backtest" ? EVALUATION_FORM(t.values ?? {})
          : { mod: "captain-forms", kind: "dialog", width: 560, props: { task: t.form, values: t.values ?? {} } };
      return openOverlay(overlay);
    }
    if (t.project) {
      setShared("captain.scope", { kind: "project", id: t.project, label: t.label ?? t.project });
      return run("layout.select", { stack, child: "project" });
    }
    if (!t.section) return null;
    // A screen that left the app: Compare is inside Runs now; Experiments and Launch review are terminal-only.
    if (t.section in MOVED) {
      if (!MOVED[t.section]) return notify({ title: "That screen is no longer in the app", body: `Use ${t.section === "handoff" ? "cst handoff" : "cst investigate"} in a terminal.`, level: "info" });
      t = { ...t, section: MOVED[t.section] };
    }
    const { at: _at, ...rest } = t;
    // A record by name (a dataset, an evaluation, an experiment): the lists are filtered to the Explorer's pick, and
    // a record outside it would not open. Show the whole workspace, and say so.
    if ((t.dataset || t.backtest) && shared("captain.scope")?.id) {
      notify({ title: "Showing the whole workspace", body: `So ${t.dataset ?? t.backtest} can open. Pick your project again in the Explorer to filter.`, level: "info" });
      setShared("captain.scope", null);
    }
    if (Object.keys(rest).length > 1) setShared("captain.open", { ...rest, at: Date.now() });
    return run("layout.select", { stack, child: t.section });
  }

  // Agent context: the screen the person is on (always part of it) and what they pinned, each with an x.
  contextStrip(c, rail) {
    if (rail || props()?.agents === false) return null;
    const v = currentView();
    const pins = this.pins ?? [];
    const on = v.view?.project?.name ?? (v.scope?.kind === "project" ? v.scope.label : null);
    const picked = v.view?.dataset ?? v.view?.backtest ?? v.view?.notebook ?? v.view?.title ?? null;
    const screen = [v.screen, on, picked].filter(Boolean).join(", ");
    return v_flex().id("nav-context").w_full().flex_none().pb(4)
      .child(h_flex().h(24).px(16).items_center().text_size(11).font_medium().text_color(c.grey500)
        .child(div().flex_1().child("AGENT CONTEXT"))
        .when(pins.length > 0, (e) => e.child(div().id("ctx-clear").px(4).cursor_pointer().text_color(c.blue).hover((s) => s.underline())
          .tooltip("Remove every pinned item").on_click((_e, cx) => { cx.spawn(async (cx) => { this.pins = await clearPins(); cx.notify(); }); }).child("Clear"))))
      .child(h_flex().id("ctx-screen").h(24).px(16).gap(7).items_center().text_size(12).text_color(c.grey900)
        .tooltip("The screen you're on is always part of what your agent sees")
        .child(svg(icon("monitor")).size(12).flex_none().text_color(c.grey500))
        .child(div().flex_1().min_w(0).truncate().child(screen || "This screen")))
      .children(pins.slice(-6).map((p, i) => h_flex().id(`ctx-pin-${i}`).h(24).px(16).gap(7).items_center().text_size(12).text_color(c.navy)
        .tooltip(p.ref)
        .child(svg(icon("link")).size(12).flex_none().text_color(c.blue))
        .child(div().flex_1().min_w(0).truncate().child(p.label ?? p.ref))
        .child(div().id(`ctx-pin-${i}-x`).size(16).flex().flex_none().items_center().justify_center().cursor_pointer().text_color(c.grey500)
          .hover((s) => s.text_color(c.fail)).tooltip("Remove from the agent's context")
          .on_click((_e, cx) => { cx.spawn(async (cx) => { this.pins = await unpin(p.ref); cx.notify(); }); })
          .child(svg(icon("x")).size(11)))))
      .when(pins.length > 6, (e) => e.child(div().h(20).px(16).text_size(11).text_color(c.grey500).child(`and ${pins.length - 6} more`)));
  }

  refresh(cx) {
    if (props()?.part === "brand" || this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        const ws = await api("/api/workspace");
        this.state = ws.ok ? "ready" : ws.starting ? "starting" : ws.failed ? "failed" : "error";
        this.error = ws.ok ? null : ws.error;
        if (ws.ok) {
          this.workspace = ws.data?.name ?? null;
          // The window's workspace, until someone picks another in the switcher: the services' current one. It is also
          // the terminals' CST_WORKSPACE.
          if (!workspace() && this.workspace) setShared("captain.workspace", this.workspace);
          const all = await api("/api/workspaces");
          if (all.ok && Array.isArray(all.data)) this.workspaces = all.data.map((w) => w.name);
          // A deployed model that turned unhealthy is said once, wherever the person is in the app. The service
          // posts the same message as a notification of this computer.
          const alerts = await api("/api/alerts");
          for (const u of alerts.ok ? alerts.data?.unhealthy ?? [] : []) {
            const key = `${u.project}:${u.since}`;
            if (this.alerted?.has(key)) continue;
            (this.alerted ??= new Set()).add(key);
            notify({ title: `${u.name}: its deployed model is unhealthy`, body: String(u.text ?? "").slice(0, 240), level: "error" });
          }
          const cloud = await api("/api/cloud/summary");
          this.cloud = cloud.ok ? cloud.data : null;
          await this.loadPlace();
        }
        await this.ensureSpace();
        for (const it of this.items().filter((i) => i.badge)) {
          const r = await api(it.badge);
          this.counts[it.id] = r.ok && Array.isArray(r.data) ? r.data.length : 0;
        }
        // The palette's "Open <table>" and "Open <evaluation>" entries: read with every refresh, which now happens
        // only when something changed, so a table made a moment ago can be opened by name at once.
        if (ws.ok) {
          const [ds, bt] = await Promise.all([api("/api/datasets"), api("/api/backtests")]);
          const extra = { ...(shared("palette.extra") ?? {}) };
          const arr = (r) => (Array.isArray(r.data) ? r.data : []);
          extra.captain = openEntries(arr(ds), [], arr(bt));
          setShared("palette.extra", extra);
        }
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  /** The workspace's agent folder (<CST_HOME>/agent/<workspace>, with its AGENTS.md), if it exists. */
  async agentFolder() {
    const home = shared("captain.daemon")?.home;
    if (!home || !this.workspace) return null;
    const dir = `${home}/agent/${this.workspace}`;
    try { return (await exec("test", ["-d", dir])).code === 0 ? dir : null; } catch { return null; }
  }

  /** A space of terminals ("Terminals"), or an investigation's, bound so its terminals know the investigation. Spaces
   *  are never named after a workspace: the workspace is the switcher's. */
  async newSpace(investigation, ws = this.workspace) {
    const name = investigation?.title ?? "Terminals";
    const folder = await this.agentFolder();
    const before = new Set(spaces().map((s) => s.id));
    run("space.new", folder ? { name, folder } : { name });
    const made = spaces().find((s) => !before.has(s.id));
    if (made) {
      // Only an investigation's space is bound (its terminals get CST_INVESTIGATION, and its workspace).
      if (investigation) {
        const map = { ...(shared("captain.spaces") ?? {}) };
        map[made.id] = { workspace: ws, investigation: investigation.id };
        setShared("captain.spaces", map);
      }
    }
  }

  /** Never zero spaces: once the window's spaces have loaded, make one if there are none. */
  async ensureSpace() {
    if (this.making || spaces().length) return;
    if ((machines()[0]?.layout ?? "ready") !== "ready") return;
    // Wait for the workspace's name (the space is named after it and its terminals get it),
    // but never longer than ~10 s: a window with no space can't open a terminal at all.
    this.waited = (this.waited ?? 0) + 1;
    if (this.state !== "ready" && this.waited < 4) return;
    this.making = true;
    try { await this.newSpace(); } finally { this.making = false; }
  }

  /** The workspace switcher: the window's workspace, every workspace to switch to, and New workspace. */
  switchWorkspace(cx, name) {
    this.spacesOpen = false;
    if (name === workspace()) { cx.notify(); return; }
    setShared("captain.workspace", name); // every screen reads it (kit/cst.js workspace()) and reloads
    cx.spawn(async (cx) => { await api(`/api/workspaces/${encodeURIComponent(name)}/use`, { method: "POST" }); this.refresh(cx); });
    cx.notify();
  }

  spaceSwitcher(c) {
    const current = workspace() ?? this.workspace;
    // No words while Captain's services start: a placeholder bar. One short line if they fail.
    const failed = this.state === "failed";
    const list = this.workspaces?.length ? this.workspaces : current ? [current] : [];
    return v_flex().w_full().flex_none().border_b(1).border_color(c.rule)
      .child(h_flex().h(40).px(16).gap(8).items_center()
        .child(h_flex().id("space-current").flex_1().min_w(0).gap(6).items_center().cursor_pointer().tooltip(failed ? this.error : "Switch workspace")
          .on_click((_e, cx) => { this.spacesOpen = !this.spacesOpen; cx.notify(); })
          .child(current
            ? div().min_w(0).truncate().text_size(13).font_medium().text_color(c.navy).child(current)
            : div().h(12).w(96).bg(c.surface2))
          .child(svg("mods/captain-nav/chevron-down.svg").size(12).text_color(c.grey500)))
        .child(div().id("nav-new").h(24).px(8).flex().items_center().gap(4).border_1().border_color(c.grey300).cursor_pointer()
          .text_size(12).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue)).tooltip("New")
          .on_click(() => openOverlay({ mod: "captain-forms", kind: "popup", width: 340, height: 520, props: { menu: true } }))
          .child(svg("mods/captain-nav/plus.svg").size(12)).child("New")))
      .when(failed && !this.spacesOpen, (e) => e.child(h_flex().px(16).pb(8).gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.fail).tooltip(this.error ?? "").child("Captain couldn't start its services"))
        .child(div().id("svc-retry-nav").text_size(11.5).text_color(c.blue).cursor_pointer().on_click((_e, cx) => { retry(); this.state = "starting"; cx.notify(); }).child("Retry"))))
      .when(Boolean(this.spacesOpen), (e) => e.child(v_flex().pb(6)
        .child(div().h(22).px(16).flex().items_center().text_size(10.5).font_semibold().text_color(c.grey500).child("WORKSPACES"))
        .children(list.map((w) => h_flex().id(`workspace-${w}`).h(28).px(16).gap(8).items_center().cursor_pointer()
          .bg(w === current ? c.blueLight : c.surface).text_color(w === current ? c.blue : c.navy).text_size(12.5)
          .hover((s) => s.bg(c.surface2))
          .on_click((_e, cx) => this.switchWorkspace(cx, w))
          .child(div().flex_1().min_w(0).truncate().child(w))
          .when(w === current, (x) => x.child(svg(icon("check")).size(12).text_color(c.blue)))))
        .child(h_flex().id("workspace-new").h(28).px(16).gap(6).items_center().cursor_pointer().text_size(12.5).text_color(c.blue)
          .hover((s) => s.bg(c.blueLight)).on_click((_e, cx) => { this.spacesOpen = false; cx.notify(); openOverlay(SETUP); })
          .child(svg("mods/captain-nav/plus.svg").size(12)).child("New workspace..."))));
  }


  // The project's own views (captain/views/<name>/), under a "From <project>" label. A view with a problem says what
  // it is instead of opening.
  teamRows(c, rail) {
    const views = shared("captain.team.views") ?? [];
    if (!views.length) return null;
    const project = String(shared("captain.team.project") ?? "").replace(/\/captain\/?$/, "").split("/").pop() || "your project";
    return v_flex().w_full().pt(4)
      .when(!rail, (e) => e.child(div().h(20).px(14).flex().items_center().text_size(10.5).font_semibold().text_color(c.grey500).child(`FROM ${project.toUpperCase()}`)))
      .children(views.map((v) => this.row(c, { id: v.mod, label: v.problem ? `${v.title} (${v.installed ? "last good version" : "can't load"})` : v.title, icon: icon(v.icon) }, false, rail, () => {
        // A view with a problem: its last good version, if there is one, and the check's words either way.
        if (v.problem) notify({ title: v.installed ? `${v.title}: showing the last good version` : `${v.title} can't load`, body: v.problem, level: "error" });
        if (!v.problem || v.installed) openOverlay({ mod: v.mod, kind: "dialog", width: v.size?.width ?? 0.8, height: v.size?.height ?? 0.85 });
      })));
  }

  narrow() {
    return (find(layout(), "nav")?.size?.px ?? WIDE) < 120;
  }

  toggle() {
    run("layout.resize", { id: "nav", px: this.narrow() ? WIDE : RAIL });
  }

  // One nav row: icon, label, badge. Active: a 2px blue edge, blue tint, blue text.
  row(c, it, active, rail, action) {
    const count = this.counts[it.id] ?? 0;
    const icon = it.icon?.includes("/") ? it.icon : `mods/captain-nav/${it.icon ?? "search"}.svg`;
    return h_flex().id(`nav-${it.id}`).relative().w_full().h(36).flex_none().gap(10).items_center().cursor_pointer()
      .border_l(2).border_color(active ? c.blue : "#00000000")
      .when(!rail, (e) => e.px(14)).when(rail, (e) => e.justify_center())
      .bg(active ? c.blueLight : c.surface).text_color(active ? c.blue : c.navy).text_size(14)
      .hover((s) => (active ? s : s.bg(c.surface2)))
      .tooltip(it.label)
      .on_click(action)
      .child(svg(icon).size(16).flex_none())
      .when(!rail, (e) => e.child(div().flex_1().min_w(0).truncate().child(it.label)))
      .when(count > 0, (e) => e.child(div()
        .when(rail, (d) => d.absolute().top(2).right(4).min_w(16).px(4).text_size(9.5).line_height("14px"))
        .when(!rail, (d) => d.min_w(20).px(6).text_size(10.5).line_height("18px"))
        .bg(c.blue).text_color(c.onBlue).font_family("JetBrains Mono").flex().justify_center()
        .tooltip(`${count} ${it.badgeLabel ?? ""}`.trim()).child(String(count))));
  }

  // Light, Dark, System: the Captain Search desktop app's segmented control.
  // In the rail, one icon that steps through them.
  themeSwitch(c, rail) {
    const MODES = [["light", "Light", "sun"], ["dark", "Dark", "moon"], ["system", "System", "monitor"]];
    const set = (v) => (_e, cx) => {
      this.appearance = v;
      cx.notify();
      cx.spawn(async () => { await setAppearance(v); });
    };
    if (rail) {
      const i = MODES.findIndex(([v]) => v === this.appearance);
      const [, label, icon] = MODES[i < 0 ? 2 : i];
      const next = MODES[((i < 0 ? 2 : i) + 1) % MODES.length][0];
      return h_flex().id("theme-cycle").h(32).w_full().justify_center().items_center().cursor_pointer().text_color(c.grey900)
        .hover((s) => s.text_color(c.navy)).tooltip(`${label} (click for ${next})`).on_click(set(next))
        .child(svg(`mods/captain-nav/${icon}.svg`).size(14));
    }
    return h_flex().id("theme-switch").mx(14).my(6).h(28).border_1().border_color(c.grey300).children(MODES.map(([v, label, icon], k) => {
      const on = this.appearance === v;
      return h_flex().id(`theme-${v}`).flex_1().h_full().gap(5).items_center().justify_center().cursor_pointer().text_size(12)
        .when(k < MODES.length - 1, (e) => e.border_r(1).border_color(c.grey300))
        .bg(on ? c.navy : c.surface).text_color(on ? c.surface : c.grey900)
        .when(!on, (e) => e.hover((s) => s.text_color(c.navy)))
        .tooltip(label).on_click(set(v))
        .child(svg(`mods/captain-nav/${icon}.svg`).size(13)).child(label);
    }));
  }

  // Which cloud this workspace works with (GET /api/cloud/summary): provider, account,
  // region, and whether the last check of the login passed. Opens Cloud and compute.
  /** A packaged Captain running a backend that is not its own says so, always in view. */
  devBackend(c, rail) {
    const dev = shared("captain.daemon")?.dev_backend;
    if (!dev) return null;
    return h_flex().id("dev-backend").mx(rail ? 0 : 14).my(4).h(26).gap(6).items_center().when(rail, (e) => e.w_full().justify_center()).when(!rail, (e) => e.px(8))
      .bg(c.warnLight).text_color(c.warn).tooltip(`Development backend: Captain's services run from ${dev}, not from this app.`)
      .child(svg(icon("triangle-alert")).size(13).flex_none())
      .when(!rail, (e) => e.child(div().text_size(12).font_medium().truncate().child("Development backend")));
  }

  /** Where new runs go: the workspace's default place (Settings, or the compute picker this chip opens). */
  async loadPlace() {
    const [t, st] = await Promise.all([api("/api/compute", { scoped: false }), api("/api/settings", { scoped: false })]);
    if (!st.ok) return;
    const name = st.data?.compute?.backtest ?? LOCAL;
    this.place = placesFrom(t.ok ? t.data : []).find((p) => p.value === name) ?? { value: name, label: name };
  }

  cloudChip(c, rail) {
    const s = this.cloud;
    if (!s && this.state !== "ready") return null;
    const k = cloudChipInfo(s);
    const dot = { good: c.good, fail: c.fail, none: c.grey300 }[k.dot];
    // The chip opens the compute picker: every place work can run, the current one checked. Cloud logins are one
    // click further, from the picker.
    const open = () => openOverlay({ mod: "captain-compute", kind: "popup", width: 340, height: 420 });
    const place = this.place?.label ?? "This computer";
    return h_flex().id("cloud-chip").mx(rail ? 0 : 14).my(4).min_h(rail ? 30 : 42).gap(8).items_center().cursor_pointer()
      .when(rail, (e) => e.w_full().justify_center()).when(!rail, (e) => e.px(8).py(4).border_1().border_color(c.grey300))
      .hover((x) => x.bg(c.surface2)).tooltip(`${k.tip}. Runs go to ${place}. Click to pick where runs go`).on_click(open)
      .child(k.provider ? Brand(c, k.provider, 14) : svg(icon("cloud")).size(14).text_color(k.count ? c.navy : c.grey500))
      .when(!rail, (e) => e.child(v_flex().flex_1().min_w(0)
        .child(div().id("cloud-chip-text").truncate().text_size(12).text_color(k.count ? c.navy : c.grey500).child(k.text))
        .child(div().id("cloud-chip-place").truncate().text_size(11).text_color(c.grey500).child(`Runs on ${place}`))))
      .child(div().size(7).rounded_full().flex_none().bg(dot));
  }

  agentRows(c, rail) {
    const list = agents();
    if (props()?.agents === false) return null;
    // One click sends this screen to the open agent tab, or opens one (kit/agent.js askAboutScreen).
    const ask = (id) => div().id(id).size(22).flex().flex_none().items_center().justify_center().cursor_pointer().text_color(c.grey500)
      .hover((s) => s.text_color(c.blue)).tooltip("Ask an agent about this screen")
      .on_click((_e, cx) => { cx.stop_propagation(); cx.spawn(async (cx) => { await askAboutScreen(cx); }); })
      .child(svg(icon("bot")).size(14));
    if (rail && !list.length) return h_flex().w_full().flex_none().h(30).justify_center().items_center().border_t(1).border_color(c.rule).child(ask("nav-ask-screen"));
    const dot = (s) => (s === "blocked" ? c.fail : s === "working" ? c.blue : s === "done" ? "#12b76a" : c.grey300);
    return v_flex().w_full().flex_none().pt(6).border_t(1).border_color(c.rule)
      .when(!rail, (e) => e.child(h_flex().id("nav-agents").h(26).px(16).items_center().cursor_pointer()
        .text_size(11).font_medium().text_color(c.grey500).on_click((_e, cx) => { this.agentsOpen = !this.agentsOpen; cx.notify(); })
        .child(div().flex_1().child("AGENTS")).child(ask("nav-ask-screen")).when(list.length > 0, (x) => x.child(div().ml(6).child(this.agentsOpen ? "hide" : `${list.length}`)))))
      .when(this.agentsOpen || rail, (e) => e.children(list.slice(0, 8).map((a) => h_flex().id(`agent-${a.address}`).h(28).gap(8).items_center()
        .cursor_pointer().when(!rail, (d) => d.px(16)).when(rail, (d) => d.justify_center())
        .text_size(12.5).text_color(c.navy).hover((s) => s.bg(c.surface2))
        .tooltip(`${a.app ?? a.program?.name ?? "agent"}: ${STATUS[a.status] ?? a.status}`)
        .on_click(() => run("agent.focus", { terminal: a.address }))
        .child(div().size(7).rounded_full().flex_none().bg(dot(a.status)))
        .when(!rail, (d) => d.child(div().flex_1().min_w(0).truncate().child(a.tab_name || a.title || a.address))
          .child(div().text_size(11).text_color(a.status === "blocked" ? c.fail : c.grey500).child(STATUS[a.status] ?? ""))))));
  }

  render() {
    const t = theme();
    const c = palette(isDark(t));
    const rail = this.narrow();
    const mac = win()?.platform === "macos" && !win()?.fullscreen;
    if (props()?.part === "brand") {
      // The mark only; on macOS it sits right of the traffic lights.
      return h_flex().size_full().bg(c.surface).border_b(1).border_color(c.rule).items_center()
        .when(rail, (e) => e.justify_center()).when(!rail, (e) => e.pl(mac ? 84 : 16))
        .child(div().id("nav-logo").flex_none().cursor_pointer().tooltip("All projects")
          .on_click(() => { setShared("captain.open", { section: "project", all: true, at: Date.now() }); run("layout.select", { stack: props()?.stack ?? "sections", child: "project" }); })
          .child(image("mods/captain-nav/captain.png").size(28)))
        .when(!rail, (e) => e.child(div().flex_1()).child(div().id("nav-collapse").size(24).mr(8).flex().items_center().justify_center()
          .cursor_pointer().text_color(c.grey500).hover((s) => s.text_color(c.navy)).tooltip("Collapse").on_click(() => this.toggle())
          .child(svg("mods/captain-nav/panel-left.svg").size(15))));
    }
    const stack = props()?.stack ?? "sections";
    const node = find(layout(), stack);
    const active = node?.active ?? node?.children?.[0]?.id;
    const go = (it) => () => openEntry(it, stack);
    return v_flex().size_full().bg(c.surface).font_family("Inter").border_r(1).border_color(c.rule)
      .when(!rail, (e) => e.child(this.spaceSwitcher(c)))
      .when(rail, (e) => e.child(h_flex().id("nav-expand").h(40).flex_none().justify_center().items_center().cursor_pointer()
        .text_color(c.grey500).hover((s) => s.text_color(c.navy)).tooltip("Expand").on_click(() => this.toggle())
        .child(svg("mods/captain-nav/panel-left.svg").size(15))))
      // The Explorer: the workspace's scopes and engines; picking one filters the views below to it.
      .when(!rail, (e) => e.child(v_flex().id("nav-explorer").w_full().flex_1().min_h(0).py(6).overflow_y_scroll().child(this.explorer.render(c))))
      .child(v_flex().w_full().when(rail, (e) => e.flex_1().min_h(0)).when(!rail, (e) => e.flex_none().border_t(1).border_color(c.rule)).py(6)
        .when(!rail, (e) => e.child(h_flex().h(22).px(14).items_center().gap(6)
          .child(div().text_size(11).font_semibold().text_color(c.grey500).child("VIEWS"))
          .when(Boolean(shared("captain.scope")), (x) => {
            const sc = shared("captain.scope");
            const proj = sc?.kind === "project" ? projectNode(this.explorer?.tree, sc.id) : null;
            return x.child(h_flex().id("nav-scope-clear").min_w(0).flex_1().gap(4).items_center().cursor_pointer()
              .tooltip("Show the whole workspace").on_click((_e, cx) => { setShared("captain.scope", null); cx.notify(); })
              .when(Boolean(proj), (y) => y.child(ProjectIcon(c, proj, 12)))
              .child(div().min_w(0).truncate().text_size(11).text_color(c.blue).child(`in ${sc?.label ?? ""}`)));
          })))
        .children(this.items().map((it) => this.row(c, it, it.id === active, rail, go(it))))
        .child(this.teamRows(c, rail) ?? div()))
      .child(this.agentRows(c, rail) ?? div())
      .child(this.contextStrip(c, rail) ?? div())
      .child(v_flex().w_full().flex_none().py(8).border_t(1).border_color(c.rule)
        .child(this.devBackend(c, rail) ?? div())
        .child(this.cloudChip(c, rail) ?? div())
        // Light, dark or system is chosen in Settings (and from the palette): it is not something to look at every day.
        .child(this.row(c, { id: "terminals", label: "Terminals", icon: "terminal" }, !find(layout(), "work")?.hidden, rail, () => run("layout.toggle", { id: "work" })))
        .child(this.row(c, { id: "settings", label: "Settings", icon: "settings" }, false, rail, () => run("app.settings"))));
  }
}
