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
import { api, retry, workspace, watchWorkspace } from "./kit/cst.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { isDark, palette } from "./kit/tokens.js";
import { getAppearance, setAppearance } from "./kit/appearance.js";
import { Explorer, projectNode } from "./explorer.js";
import { ProjectIcon } from "./kit/projecticon.js";
import { TeamViews } from "./teamviews.js";

const ITEMS = [
  // Home: the current project's page (captain-project), or New project when there is none.
  { id: "project", label: "Home", icon: icon("house") },
  { id: "investigations", label: "Experiments", was: "investigations", icon: "search", badge: "/api/proposals?status=proposed", badgeLabel: "recommendations to review" },
  { id: "data", label: "Data and signals", icon: "database" },
  { id: "runs", label: "Runs", was: "offline evaluations backtests rating grading off-policy replays", icon: "play" },
  { id: "comparisons", label: "Compare", was: "comparisons side by side", icon: "git-compare" },
  { id: "handoff", label: "Launch review", was: "handoff hand off", icon: "send" },
  // Team collaboration was only in the palette, so people who built it couldn't find it (Edgar, 2026-11-09).
  { id: "team", label: "Team", icon: icon("users") },
];
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
  ["investigation", "New experiment", "investigation question hypothesis start", "search"],
  ["dataset", "Import a dataset", "file csv parquet sql upload catalog queries judgments", "database"],
  ["database", "Connect a database", "postgres snowflake bigquery mysql redshift sql", "server"],
  ["cloud", "Connect a cloud account", "aws gcp azure login credentials", "cloud"],
  ["mlflow", "Connect MLflow", "mlflow tracking experiments models registry", "flask-conical"],
  ["compute", "Add a place to run work", "compute cloud run offline evaluations backtests serve", "boxes"],
  ["handoff", "Start a launch review", "handoff hand off candidate promote deploy ship release readiness", "send"],
  ["backtest", "Run an offline evaluation", "backtest compare evaluate offline ndcg what if settings tune knobs sweep sandbox", "flask-conical"],
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

export function staticEntries(sections) {
  return [
    ...TASK_ENTRIES.map(([task, title, words, ic]) => ({ group: "Captain", title, words, icon: icon(ic),
      overlay: task === "dataset" ? { mod: "captain-import", kind: "dialog", width: 900, height: 640, props: {} }
        : task === "rankings" ? { mod: "captain-rankings", kind: "dialog", width: 760, height: 660 }
        : task === "explain" ? { mod: "captain-explain", kind: "dialog", width: 0.9, height: 0.9 }
        : task === "sandbox" ? { mod: "captain-sandbox", kind: "dialog", width: 0.9, height: 0.9 } : { mod: "captain-forms", kind: "dialog", width: 560, props: { task } } })),
    ...sections.map((s) => ({ group: "Captain", title: `Go to ${s.label}`, words: `section ${s.label} ${s.was ?? ""}`, icon: s.icon && !String(s.icon).includes("/") ? icon(s.icon) : undefined, overlay: go(s.id) })),
    { name: "captain-nav.theme-light", args: {}, group: "Captain", icon: icon("sun"), title: "Switch to light", words: "appearance theme light mode" },
    { name: "captain-nav.theme-dark", args: {}, group: "Captain", icon: icon("moon"), title: "Switch to dark", words: "appearance theme dark mode night" },
    { name: "captain-nav.theme-system", args: {}, group: "Captain", icon: icon("monitor"), title: "Match the system appearance", words: "appearance theme system auto" },
    { group: "Captain", icon: icon("folder-open"), title: "New workspace...", words: "workspace storage folder bucket s3 gcs azure create start import drop", overlay: SETUP },
    { name: "app.welcome", args: {}, group: "Captain", icon: icon("compass"), title: "Show the Captain tour", words: "onboarding welcome checklist start setup" },
    { group: "Captain", icon: icon("workflow"), title: "Open workflows", words: "workflow schedule nightly steps chain automate", overlay: go("runs", { tab: "workflows" }) },
    { group: "Captain", icon: icon("users"), title: "Team collaboration", words: "company sync login shared records drafts", overlay: { mod: "captain-team", kind: "dialog", width: 960, height: 680 } },
    // The scope model, under the name people use for it; it lives in Settings (Edgar, 2026-11-09).
    { name: "app.settings", args: { section: "structure" }, group: "Captain", icon: icon("waypoints"), title: "Project Structure", words: "scope model levels brand site hierarchy terminology types explorer metadata fields settings" },
    { group: "Captain", icon: icon("cloud"), title: "Cloud and compute", words: "aws gcp azure login profile region compute", overlay: { mod: "captain-cloud", kind: "dialog", width: 760, height: 640 } },
    { group: "Captain", icon: icon("terminal"), title: "Run a cst command...", words: "cli advanced power command", overlay: { mod: "captain-commands", kind: "dialog", width: 560, height: 560, props: { menu: "all" } } },
  ];
}

/** "Open <name>" for datasets and investigations, from the workspace. */
export function openEntries(datasets, investigations, backtests = []) {
  return [
    ...backtests.slice(0, 200).flatMap((b) => [
      { group: "Captain", icon: icon("flask-conical"), title: `Open ${b.name}`, words: "offline evaluation backtest run", overlay: go("runs", { backtest: b.name }) },
      { group: "Captain", icon: icon("git-compare"), title: `Compare ${b.name}`, words: "offline evaluation backtest comparison results side by side", overlay: go("comparisons", { backtest: b.name }) },
    ]),
    ...datasets.slice(0, 200).map((d) => ({ group: "Captain", icon: icon("table"), title: `Open ${d.name}`, words: `dataset ${d.kind ?? ""}`, overlay: go("data", { dataset: d.name }) })),
    ...investigations.slice(0, 200).map((i) => ({ group: "Captain", icon: icon("search"), title: `Open experiment: ${i.title ?? i.id}`, words: `experiment investigation ${i.state ?? ""}`, overlay: go("investigations", { investigation: i.id }) })),
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
    this.explorer = new Explorer();
    this.explorer.start(cx);
    // Team views from the project's captain/views/ (teamviews.js).
    this.teamViews = new TeamViews();
    this.teamViews.start(cx);
    this.refresh(cx);
    watchWorkspace(cx, (cx) => { this.cloud = null; setShared("captain.scope", null); this.refresh(cx); this.explorer.load(cx); });
    // Every 3 s until Captain's services answer, then every 15 s.
    this.tick = 0;
    cx.timer.every(3000, (cx) => { this.tick++; if (this.state !== "ready" || this.tick % 5 === 0) this.refresh(cx); });
  }

  items() {
    return props()?.items ?? ITEMS;
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
          const cloud = await api("/api/cloud/summary");
          this.cloud = cloud.ok ? cloud.data : null;
        }
        await this.ensureSpace();
        for (const it of this.items().filter((i) => i.badge)) {
          const r = await api(it.badge);
          this.counts[it.id] = r.ok && Array.isArray(r.data) ? r.data.length : 0;
        }
        if (ws.ok && (this.tick ?? 0) % 10 === 0) {
          const [ds, inv, bt] = await Promise.all([api("/api/datasets"), api("/api/investigations"), api("/api/backtests")]);
          const extra = { ...(shared("palette.extra") ?? {}) };
          const arr = (r) => (Array.isArray(r.data) ? r.data : []);
          extra.captain = openEntries(arr(ds), arr(inv), arr(bt));
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
  cloudChip(c, rail) {
    const s = this.cloud;
    if (!s && this.state !== "ready") return null;
    const a = s?.active;
    const short = { aws: "AWS", gcp: "GCP", azure: "Azure" }[a?.provider] ?? a?.label ?? a?.provider;
    const dot = !a ? c.grey300 : a.last_test?.ok ? c.good : a.last_test ? c.fail : c.grey300;
    const text = a ? [short, a.account_hint, a.region].filter(Boolean).join(" \u00b7 ") : "No cloud connected";
    const tip = a ? (a.last_test ? (a.last_test.ok ? `Signed in as ${a.last_test.identity ?? "?"}` : `Last check failed: ${a.last_test.error ?? ""}`) : "Not checked yet") : "Connect a cloud login";
    const open = () => (a ? openOverlay({ mod: "captain-cloud", kind: "dialog", width: 760, height: 640 })
      : openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "cloud" } }));
    return h_flex().id("cloud-chip").mx(rail ? 0 : 14).my(4).h(30).gap(8).items_center().cursor_pointer()
      .when(rail, (e) => e.w_full().justify_center()).when(!rail, (e) => e.px(8).border_1().border_color(c.grey300))
      .hover((x) => x.bg(c.surface2)).tooltip(`${text}. ${tip}`).on_click(open)
      .child(a ? Brand(c, a.provider, 14) : svg(icon("cloud")).size(14).text_color(c.grey500))
      .when(!rail, (e) => e.child(div().flex_1().min_w(0).truncate().text_size(12).text_color(a ? c.navy : c.grey500).child(text)))
      .child(div().size(7).rounded_full().bg(dot));
  }

  agentRows(c, rail) {
    const list = agents();
    if (props()?.agents === false || !list.length) return null;
    const dot = (s) => (s === "blocked" ? c.fail : s === "working" ? c.blue : s === "done" ? "#12b76a" : c.grey300);
    return v_flex().w_full().flex_none().pt(6).border_t(1).border_color(c.rule)
      .when(!rail, (e) => e.child(h_flex().id("nav-agents").h(26).px(16).items_center().cursor_pointer()
        .text_size(11).font_medium().text_color(c.grey500).on_click((_e, cx) => { this.agentsOpen = !this.agentsOpen; cx.notify(); })
        .child(div().flex_1().child("AGENTS")).child(div().child(this.agentsOpen ? "hide" : `${list.length}`))))
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
        .child(image("mods/captain-nav/captain.png").size(28).flex_none())
        .when(!rail, (e) => e.child(div().flex_1()).child(div().id("nav-collapse").size(24).mr(8).flex().items_center().justify_center()
          .cursor_pointer().text_color(c.grey500).hover((s) => s.text_color(c.navy)).tooltip("Collapse").on_click(() => this.toggle())
          .child(svg("mods/captain-nav/panel-left.svg").size(15))));
    }
    const stack = props()?.stack ?? "sections";
    const node = find(layout(), stack);
    const active = node?.active ?? node?.children?.[0]?.id;
    const go = (it) => () => (it.run ? run(it.run, it.args ?? {}) : run("layout.select", { stack, child: it.id }));
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
      .child(v_flex().w_full().flex_none().py(8).border_t(1).border_color(c.rule)
        .child(this.cloudChip(c, rail) ?? div())
        .child(this.themeSwitch(c, rail))
        .child(this.row(c, { id: "terminals", label: "Terminals", icon: "terminal" }, !find(layout(), "work")?.hidden, rail, () => run("layout.toggle", { id: "work" })))
        .child(this.row(c, { id: "settings", label: "Settings", icon: "settings" }, false, rail, () => run("app.settings"))));
  }
}
