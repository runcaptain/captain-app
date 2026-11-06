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
// It also publishes the daemon's commands for the palette (shared "palette.extra")
// and the New menu (shared "captain.commands"): this mod is always shown, so it's
// the one that stays up to date.
import { View, div, image, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, run, layout, props, agents, shared, setShared, openOverlay, window as win } from "herdr";
import { api } from "./kit/cst.js";
import { isDark, palette } from "./kit/tokens.js";
import { getAppearance, setAppearance } from "./kit/appearance.js";

const ITEMS = [
  { id: "investigations", label: "Investigations", icon: "search", badge: "/api/proposals?status=proposed", badgeLabel: "proposals to review" },
  { id: "data", label: "Data and signals", icon: "database" },
  { id: "runs", label: "Runs", icon: "play" },
  { id: "comparisons", label: "Comparisons", icon: "git-compare" },
  { id: "handoff", label: "Handoff", icon: "send" },
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
const MAKES = /^(new|create|add|snapshot|import|gen|generate|sweep|publish|seed|init|link|propose)$/;

/** Daemon commands as palette entries: each opens the native command form. */
export function paletteEntries(list) {
  return list.map((c) => ({
    name: "captain.command", group: "Captain", title: `cst ${c.path.join(" ")}`,
    words: `${c.help ?? ""} ${c.path.join(" ")}`,
    overlay: { mod: "captain-commands", kind: "dialog", width: 560, props: { path: c.path } },
  }));
}

export default class CaptainNav extends View {
  init(_props, cx) {
    this.workspace = null;
    this.counts = {};
    this.agentsOpen = true;
    this.state = "starting";
    this.appearance = "system";
    cx.spawn(async (cx) => { this.appearance = await getAppearance(); cx.notify(); });
    // The onboarding, in the palette from the start (the commands join when the engine is up).
    if (props()?.part !== "brand") {
      setShared("palette.extra", { ...(shared("palette.extra") ?? {}), "captain-app": [
        { name: "app.welcome", args: {}, group: "Captain", title: "Captain: onboarding", words: "welcome tour checklist start setup" },
      ] });
    }
    this.refresh(cx);
    // Every 3 s until the engine answers, then every 15 s.
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
        this.state = ws.ok ? "ready" : ws.starting ? "starting" : "error";
        if (ws.ok) this.workspace = ws.data?.name ?? null;
        for (const it of this.items().filter((i) => i.badge)) {
          const r = await api(it.badge);
          this.counts[it.id] = r.ok && Array.isArray(r.data) ? r.data.length : 0;
        }
        if (ws.ok && !this.published) {
          const r = await api("/api/commands");
          if (r.ok && Array.isArray(r.data)) {
            const extra = { ...(shared("palette.extra") ?? {}), captain: paletteEntries(r.data) };
            setShared("palette.extra", extra);
            setShared("captain.commands", r.data.filter((c) => MAKES.test(c.path[c.path.length - 1])).map((c) => ({ path: c.path, help: c.help })));
            this.published = true;
          }
        }
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
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
    const ws = this.workspace ?? (this.state === "starting" ? "Starting Captain's engine..." : "No workspace");
    const go = (it) => () => (it.run ? run(it.run, it.args ?? {}) : run("layout.select", { stack, child: it.id }));
    return v_flex().size_full().bg(c.surface).font_family("Inter").border_r(1).border_color(c.rule)
      .when(!rail, (e) => e.child(h_flex().h(40).flex_none().px(16).gap(8).items_center().border_b(1).border_color(c.rule)
        .child(div().flex_1().min_w(0).truncate().text_size(13).font_medium().text_color(this.workspace ? c.navy : c.grey500).child(ws))
        .child(div().id("nav-new").h(24).px(8).flex().items_center().gap(4).border_1().border_color(c.grey300).cursor_pointer()
          .text_size(12).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue)).tooltip("New")
          .on_click(() => openOverlay({ mod: "captain-commands", kind: "popup", width: 300, height: 420, props: { menu: "new" } }))
          .child(svg("mods/captain-nav/plus.svg").size(12)).child("New"))))
      .when(rail, (e) => e.child(h_flex().id("nav-expand").h(40).flex_none().justify_center().items_center().cursor_pointer()
        .text_color(c.grey500).hover((s) => s.text_color(c.navy)).tooltip("Expand").on_click(() => this.toggle())
        .child(svg("mods/captain-nav/panel-left.svg").size(15))))
      .child(v_flex().w_full().flex_1().min_h(0).py(8)
        .children(this.items().map((it) => this.row(c, it, it.id === active, rail, go(it)))))
      .child(this.agentRows(c, rail) ?? div())
      .child(v_flex().w_full().flex_none().py(8).border_t(1).border_color(c.rule)
        .child(this.themeSwitch(c, rail))
        .child(this.row(c, { id: "terminals", label: "Terminals", icon: "terminal" }, !find(layout(), "work")?.hidden, rail, () => run("layout.toggle", { id: "work" })))
        .child(this.row(c, { id: "settings", label: "Settings", icon: "settings" }, false, rail, () => run("app.settings"))));
  }
}
