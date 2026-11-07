// CAPTAIN: Captain's onboarding, for a search or ML engineer. It opens once,
// over the live window (a movable window overlay that blocks nothing), and
// records finished or skipped in shared "frame.welcomed". The palette's
// "Captain: onboarding" (app.welcome) opens it again.
//
// Page 1: where this workspace keeps its work, then a drop zone for their files, then an optional model registry
// (kit/setup.js, shared with New workspace). Pages 2-4: the interface. Page 5: their work, a live checklist read from the
// Captain Search daemon (GET /api/onboarding): each step done or next, the one
// command behind "Show the command", and a Do it button that opens a designed form.

import { View, div, svg, image } from "gpui-kit";
import { v_flex, h_flex, Scrollbar } from "gpui-base";
import { theme, props, closeOverlay, setShared, keys, openOverlay, spaces, run } from "herdr";
import { api } from "./kit/cst.js";
import { Skeleton, ServiceError } from "./kit/ui.js";
import { isDark, palette } from "./kit/tokens.js";
import { StorageSetup } from "./kit/setup.js";

const PAGES = 5;
const I = (name) => `mods/captain-nav/${name}.svg`;

const SECTIONS = [
  ["search", "Investigations", "Home. Each question you're working on: the hypothesis and its revisions, evidence, signals, runs, and the decision."],
  ["database", "Data and signals", "Datasets (classes, grid, labels as code) and the signals you rank with."],
  ["play", "Runs", "Every execution with its manifest, logs, artifacts and how reproducible it is: backtests, replays, grading jobs, workflows."],
  ["git-compare", "Comparisons", "Metrics, slices, queries side by side, and the limits of what a result shows."],
  ["send", "Handoff", "Candidates you export, serving contracts, linked deployments and their online outcomes."],
];

// What each checklist step's "Do it" opens: a designed form (captain-forms), or the
// screen where it's done. The command stays behind "Show the command".
const STEP_DO = {
  cloud: { mod: "captain-forms", kind: "dialog", width: 560, props: { task: "cloud" } },
  data: { mod: "captain-forms", kind: "dialog", width: 560, props: { task: "dataset" } },
  investigation: { mod: "captain-forms", kind: "dialog", width: 560, props: { task: "investigation" } },
  run: { mod: "captain-go", kind: "popup", width: 1, height: 1, props: { section: "investigations" } },
  decision: { mod: "captain-go", kind: "popup", width: 1, height: 1, props: { section: "investigations" } },
};

// The key bound to an action in the captain layout or globally, as people press it.
function bound(match) {
  const k = keys().find((b) => (b.section === "layout:captain" || b.section === "global") && match(b.action));
  if (!k) return null;
  const parts = k.keys.endsWith("--") ? [...k.keys.slice(0, -2).split("-"), "-"] : k.keys.split("-");
  const sym = { cmd: "⌘", shift: "⇧", alt: "⌥", ctrl: "⌃" };
  return parts.map((p) => sym[p] ?? p.toUpperCase()).join("");
}

export default class Welcome extends View {
  init(_props, cx) {
    this.page = props()?.page ?? 1;
    this.steps = [];
    this.state = "starting";
    this.error = null;
    this.copied = null;
    this.seeding = false;
    // Storage first (Edgar: "I'd rather this storage also be connected as well during onboarding").
    this.setup = new StorageSetup({ first: true, done: () => { this.page = 2; } });
    this.setup.start(cx);
    this.refresh(cx);
    cx.timer.every(4000, (cx) => { if (this.page === PAGES) this.refresh(cx); });
  }

  finish(outcome) {
    // The shells start with a space to type in, not "No space open".
    if (!spaces().length) run("space.new", { name: "Captain" });
    setShared("frame.welcomed", outcome);
    closeOverlay(props().overlay);
  }

  refresh(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        const r = await api("/api/onboarding");
        if (r.ok) {
          this.state = "ready"; this.error = null;
          this.steps = r.data?.steps ?? [];
          this.next = r.data?.next ?? null;
          this.workspace = r.data?.workspace?.name ?? null;
        } else {
          this.state = r.starting ? "starting" : r.failed ? "failed" : "error";
          this.error = r.starting ? null : r.error;
        }
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  seed(cx) {
    if (this.seeding) return;
    this.seeding = true;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/commands/run", { body: { path: ["demo", "seed"], args: {} }, timeout: 590 });
      this.seeding = false;
      const failed = !r.ok || r.data?.exit_code || r.data?.error;
      this.error = failed ? `cst demo seed failed: ${r.data?.error ?? r.error ?? "exit " + r.data?.exit_code}` : null;
      this.refresh(cx);
      cx.notify();
    });
  }

  para(c, text) {
    return div().text_size(13.5).line_height("21px").text_color(c.grey900).child(text);
  }

  title(c, head, sub) {
    return v_flex().gap(6).pb(14)
      .child(div().text_size(20).font_semibold().text_color(c.navy).child(head))
      .when(Boolean(sub), (e) => e.child(this.para(c, sub)));
  }

  code(c, id, text) {
    return h_flex().id(id).gap(8).items_center().px(10).py(6).bg(c.surface2).border_1().border_color(c.rule).cursor_pointer()
      .hover((s) => s.border_color(c.blue)).tooltip("Copy")
      .on_click((_e, cx) => { cx.write_to_clipboard(text); this.copied = id; cx.notify(); })
      .child(div().flex_1().min_w(0).font_family("JetBrains Mono").text_size(12).text_color(c.navy).child(text))
      .child(div().text_size(11).text_color(this.copied === id ? c.blue : c.grey500).child(this.copied === id ? "Copied" : "Copy"));
  }

  button(c, id, label, primary, action) {
    return div().id(id).h(32).px(14).flex().items_center().cursor_pointer().text_size(13).font_medium()
      .when(primary, (e) => e.bg(c.blue).text_color(c.onBlue).hover((s) => s.opacity(0.9)))
      .when(!primary, (e) => e.border_1().border_color(c.grey300).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue)))
      .on_click(action).child(label);
  }

  pageNav(c) {
    return v_flex()
      // Edgar's words, as he wrote them (the em dash is a colon by the UI rule).
      .child(div().pb(16).text_size(18).line_height("26px").font_semibold().text_color(c.navy).child("Captain is the open-source platform for building better search and recommendation models: from signal discovery to reproducible experiments and measured production improvements."))
      .child(div().pb(12).child(this.para(c, "The left side is where you go:")))
      .child(v_flex().gap(10).children(SECTIONS.map(([icon, name, about]) => h_flex().gap(12).items_start()
        .child(div().size(28).flex_none().flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(I(icon)).size(15)))
        .child(v_flex().flex_1().min_w(0).gap(2).child(div().text_size(13.5).font_semibold().text_color(c.navy).child(name)).child(div().text_size(12.5).line_height("18px").text_color(c.grey900).child(about))))))
      .child(div().pt(14).child(this.para(c, "The main area shows the section you pick. It's where the evidence lives: results, slices, queries and decisions.")));
  }

  pageShells(c) {
    const shells = bound((a) => a.includes('"id":"work"') || a === "layout.toggle:work");
    const pal = bound((a) => a === "app.palette");
    const keyRow = (k, text) => h_flex().gap(10).items_center()
      .child(div().w(84).flex_none().font_family("JetBrains Mono").text_size(13).text_color(c.blue).child(k))
      .child(div().flex_1().min_w(0).child(this.para(c, text)));
    return v_flex()
      .child(this.title(c, "Shells on the right, when you want them", "The right side holds real terminals: zsh, Claude Code, Codex or any agent. Agents there report their status, and the sidebar's Agents list shows who needs you."))
      .child(v_flex().gap(10)
        .child(keyRow(shells ?? "Terminals", shells ? "Shows or hides the shells. So does Terminals in the sidebar." : "Terminals in the sidebar shows or hides the shells."))
        .child(keyRow(pal ?? "Palette", "The palette: everything you can do and every place you can go, by name.")))
      .child(div().pt(16).child(this.para(c, "Hiding the shells keeps every session running, so an agent keeps working while you read results. Closing a pane ends the program in it.")));
  }

  pageYours(c) {
    const idea = (title, text) => v_flex().gap(6)
      .child(div().text_size(15).font_semibold().text_color(c.navy).child(title))
      .child(this.para(c, text));
    return v_flex().gap(16)
      .child(this.title(c, "Make Captain your own", "Your coding agent can customize the interface and the data models behind it."))
      .child(idea("Your interface", "Rearrange your workspace and build custom panels for the way you work."))
      .child(idea("Your data models", "Define and extend datasets, signals and experiments in code to fit your project."))
      .child(div().pt(8).child(this.para(c, "Describe what you need to your agent. Keep the changes in code, alongside your work.")));
  }

  step(c, s, i) {
    const todo = STEP_DO[s.id];
    this.shownCmd ??= {};
    const first = this.steps.findIndex((x) => !x.done);
    const next = !s.done && (this.next ? this.next === s.id : i === first);
    const command = String(s.command ?? "").split("  (or:")[0];
    return v_flex().gap(6).py(10).border_b(1).border_color(c.rule)
      .child(h_flex().gap(10).items_center()
        .child(div().size(18).flex_none().flex().items_center().justify_center().border_1()
          .border_color(s.done || next ? c.blue : c.grey300).bg(s.done ? c.blue : c.surface)
          .text_color(c.onBlue).text_size(11).child(s.done ? "✓" : ""))
        .child(div().flex_1().min_w(0).text_size(13.5).font_semibold().text_color(s.done ? c.grey500 : c.navy).child(s.title))
        .when(next, (e) => e.child(div().px(6).text_size(11).bg(c.blueLight).text_color(c.blue).child("next")))
        .when(Boolean(s.optional) && !s.done, (e) => e.child(div().text_size(11).text_color(c.grey500).child("optional")))
        .when(!s.done && Boolean(todo), (e) => e.child(this.button(c, `do-${s.id}`, "Do it", next, () => openOverlay(todo)))))
      .child(div().pl(28).text_size(12.5).text_color(c.grey900).child(s.detail ?? ""))
      .when(!s.done && Boolean(command), (e) => e.child(div().pl(28).child(this.shownCmd[s.id]
        ? this.code(c, `cmd-${s.id}`, command)
        : div().id(`show-${s.id}`).text_size(12).text_color(c.blue).cursor_pointer().on_click((_e, cx) => { this.shownCmd[s.id] = true; cx.notify(); }).child("Show the command"))))
      .when(s.id === "agent", (e) => e.child(div().pl(28).child(this.para(c,
        "Every investigation has Copy prompt and Open an agent in this space. An agent in a terminal here, or in Cursor, Devin or any terminal, can do everything Captain can. Records it makes say which agent made them."))));
  }

  pageWork(c) {
    if (this.state !== "ready") {
      // Quiet while Captain's services start; one plain error with Retry if they don't.
      return v_flex()
        .child(this.title(c, "Your work", null))
        .child(this.state === "starting" ? Skeleton(c, 5) : this.state === "failed" ? ServiceError(c, this.error, (cx) => this.refresh(cx)) : div().text_size(12.5).text_color(c.fail).child(String(this.error ?? "")));
    }
    const empty = !this.steps.some((s) => s.id === "data" && s.done);
    return v_flex()
      .child(this.title(c, "Your work", `${this.workspace ? `Workspace ${this.workspace}. ` : ""}From a dataset to a decision, read from your real records: a step is done only when it is.`))
      // The synthetic store is a small secondary link, never the default: your own data comes first (page 1).
      .when(empty, (e) => e.child(h_flex().gap(6).items_center().pb(8)
        .child(div().text_size(12).text_color(c.grey500).child("Just looking around?"))
        .child(div().id("demo-seed").text_size(12).text_color(c.blue).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click((_e, cx) => this.seed(cx)).child(this.seeding ? "Setting up the demo store..." : "Try the demo store"))))
      .when(Boolean(this.error), (e) => e.child(div().pb(6).text_size(12.5).text_color(c.fail).child(this.error)))
      .children(this.steps.map((s, i) => this.step(c, s, i)));
  }

  render() {
    const c = palette(isDark(theme()));
    const go = (page, cx) => {
      this.page = Math.max(1, Math.min(PAGES, page));
      if (this.page === PAGES) this.refresh(cx);
      cx.notify();
    };
    const pages = [(c) => this.setup.body(c), this.pageNav, this.pageShells, this.pageYours, this.pageWork];
    const customizing = pages[this.page - 1] === this.pageYours;
    return v_flex().id("welcome").size_full().bg(c.surface).text_color(c.navy).font_family("Inter")
      .child(h_flex().h(52).flex_none().px(20).gap(10).items_center().border_b(1).border_color(c.rule).cursor_grab()
        .child(image("mods/captain-nav/captain.png").size(24).flex_none())
        .child(div().text_size(14).font_semibold().child("Captain"))
        .child(div().flex_1())
        .children([...Array(PAGES)].map((_, i) => div().w(i + 1 === this.page ? 16 : 6).h(6).bg(i + 1 === this.page ? c.blue : c.grey300)))
        .child(div().w(12))
        .when(!customizing, (e) => e.child(div().id("w-skip").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => this.finish("skipped")).child("Skip"))))
      .child(div().flex_1().min_h(0).relative()
        .child(div().id("welcome-body").size_full().overflow_y_scroll()
          .child(div().px(24).pt(22).pb(20).child(pages[this.page - 1].call(this, c))))
        .child(Scrollbar.vertical("welcome-body").absolute().inset_0()))
      .child(h_flex().h(60).flex_none().px(20).gap(8).items_center().border_t(1).border_color(c.rule)
        .when(this.page > 1 && !customizing, (e) => e.child(this.button(c, "w-back", "Back", false, (_e, cx) => go(this.page - 1, cx))))
        .when(this.page === 1 && this.setup.step === "storage", (e) => e.child(div().id("w-later").text_size(12.5).text_color(c.grey500).cursor_pointer()
          .hover((s) => s.text_color(c.navy)).on_click((_e, cx) => go(2, cx)).child("Set this up later")))
        .child(div().flex_1())
        .child(this.page === 1 ? this.setup.footer(c) : this.page < PAGES
          ? this.button(c, "w-next", customizing ? "Continue" : "Next", true, (_e, cx) => go(this.page + 1, cx))
          : this.button(c, "w-done", "Start", true, () => this.finish("finished"))));
  }
}
