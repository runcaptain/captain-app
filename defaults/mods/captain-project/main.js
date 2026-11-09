// CAPTAIN: a project's page, and Captain's home (Edgar, 2026-11-09: "make this flow nicely for ml engineers building
// search"). Three parts, from the project's state (GET /api/projects/{id}/stages and /experiments):
//   Inputs       one card per pinned input (catalog, events, queries, labels, sealed holdout), each with its exact
//                version, what changed since ("Newer version available"), and one pre-filled action.
//   Experiments  each trained version judged against the champion on the sealed holdout: nDCG@10 with its interval,
//                recall, judged share, evidence, label independence and the verdict. A model trained on the labeler
//                that grades the holdout reads "agrees with its own labeler", never as a win.
//   Ship         the registry place, staging, the live handoff and the online check.
// Below them, everything in the project (GET /api/projects/{id}/home): datasets, runs, pipelines, models, notebooks.
// The project is the Explorer's pick (shared "captain.scope" {kind: "project"}), else the last one opened here.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, shared, setShared, run, openOverlay, props as layoutProps } from "herdr";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { api, watchScope, watchWorkspace, watchRecords } from "./kit/cst.js";
import { Badge, Button, Empty, Skeleton, Problem, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int } from "./kit/num.js";

const day = (iso) => (iso ? String(iso).slice(0, 10) : "");
const go = (section, extra) => { setShared("captain.open", { section, ...extra, at: Date.now() }); run("layout.select", { stack: "sections", child: section }); };
const form = (task, values = {}, args) => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task, values, ...(args ? { args } : {}) } });
const enc = encodeURIComponent;

export const SLOTS = {
  catalog: { title: "Catalog", icon: "table", about: "The products your search ranks.", noun: "a catalog" },
  events: { title: "Events", icon: "activity", about: "Searches, clicks and orders, for training.", noun: "events" },
  queries: { title: "Queries", icon: "search", about: "The queries models train on.", noun: "queries" },
  labels: { title: "Labels", icon: "tag", about: "The judgments that grade the holdout.", noun: "labels" },
  holdout: { title: "Sealed holdout", icon: "square-check", about: "Queries no model trains on, kept to judge them.", noun: "a sealed holdout" },
};
export const EVIDENCE = { SIMULATED: "simulated", LLM_GRADED: "LLM graded", LOGGED_CLICKS: "logged clicks", HUMAN_GRADED: "human graded", REAL_TRAFFIC: "real traffic" };
const INDEPENDENCE = { independent: ["Independent labels", "good"], shared: ["Same labeler", "warn"], unknown: ["Label source unknown", "neutral"] };
const OWN = "agrees with its own labeler";

/** "name@v3" as name and version. */
export function parseRef(ref) {
  const m = String(ref ?? "").match(/^(.*?)@v(\d+)(?::(\w+))?$/);
  return m ? { name: m[1], version: Number(m[2]), split: m[3] ?? null } : { name: String(ref ?? ""), version: null, split: null };
}
const signed = (n, d = 4) => (n == null ? "" : `${n >= 0 ? "+" : ""}${Number(n).toFixed(d)}`);

/** How an experiment's result reads: a short verdict, its tone and icon. Shared labels never read as a win. */
export function verdictOf(ev) {
  if (!ev) return { text: "Not judged yet", tone: "neutral", icon: "clock" };
  if (ev.error) return { text: "Result can't be read", tone: "bad", icon: "circle-x" };
  if (ev.verdict === OWN || ev.label_independence?.status === "shared") return { text: "Agrees with its own labeler", tone: "warn", icon: "triangle-alert" };
  if (ev.comparison === "win") return { text: "Beats the champion", tone: "good", icon: "circle-check" };
  if (ev.comparison === "loss") return { text: "Loses to the champion", tone: "bad", icon: "circle-x" };
  if (ev.comparison === "noise") return { text: "No clear difference", tone: "neutral", icon: "activity" };
  return { text: "Not judged yet", tone: "neutral", icon: "clock" };
}

export default class ProjectHome extends View {
  init(_props, cx) {
    this.stages = null; this.exps = null; this.home = null; this.problem = null; this.projects = null;
    this.open = null; this.everything = false; this.note = null; this.busy = false;
    this.choosing = false; this.allModels = null; this.owners = {};
    try { this.last = localStorage.getItem("project") || null; } catch { this.last = null; }
    this.load(cx);
    if ((layoutProps?.() ?? {}).chooseModels) { this.everything = true; this.toggleChoosing(cx); }
    this.openFirst = Boolean((layoutProps?.() ?? {}).openExperiment); // a layout, or the real-window check: open a row
    watchScope(cx, (cx) => this.load(cx));
    watchWorkspace(cx, (cx) => { this.last = null; this.load(cx); });
    watchRecords(cx, (cx) => this.load(cx));
    let rev = shared("captain.explorer.rev") ?? null;
    cx.timer.every(1000, (cx) => { const r = shared("captain.explorer.rev") ?? null; if (r !== rev) { rev = r; this.load(cx); } });
    // While an experiment is being judged, read again until its run lands.
    cx.timer.every(4000, (cx) => { if ((this.exps?.experiments ?? []).some((e) => e.status === "backtesting")) this.load(cx); });
  }

  /** The project shown: the Explorer's pick, else the last one opened here. */
  pid() {
    const s = shared("captain.scope");
    if (s?.kind === "project") return s.id;
    return this.last;
  }

  pickProject(cx, p) {
    this.last = p.id;
    try { localStorage.setItem("project", p.id); } catch {}
    setShared("captain.scope", { kind: "project", id: p.id, label: p.name });
    this.load(cx);
  }

  allProjects(cx) {
    this.last = null;
    try { localStorage.removeItem("project"); } catch {}
    if (shared("captain.scope")?.kind === "project") setShared("captain.scope", null);
    this.load(cx);
  }

  load(cx) {
    const pid = this.pid();
    const seq = (this.seq = (this.seq ?? 0) + 1);
    this.problem = null;
    if (!pid) {
      this.stages = null; this.exps = null; this.home = null;
      cx.spawn(async (cx) => {
        const r = await api("/api/projects", { scoped: false });
        if (seq !== this.seq) return;
        this.projects = r.ok ? (Array.isArray(r.data) ? r.data : r.data?.projects ?? []) : [];
        cx.notify();
      });
      cx.notify();
      return;
    }
    if (pid !== this.shown) {
      this.shown = pid;
      try { localStorage.setItem("project", pid); } catch {}
      this.last = pid;
      this.stages = null; this.exps = null; this.home = null; this.open = null; this.note = null;
    }
    cx.spawn(async (cx) => {
      const [st, ex, ps] = await Promise.all([
        api(`/api/projects/${enc(pid)}/stages`, { scoped: false }),
        api(`/api/projects/${enc(pid)}/experiments`, { scoped: false }),
        api("/api/projects", { scoped: false }),
      ]);
      if (seq !== this.seq) return;
      if (ps.ok) this.projects = Array.isArray(ps.data) ? ps.data : ps.data?.projects ?? [];
      if (!st.ok && st.status === 404 && shared("captain.scope")?.kind !== "project") {
        this.allProjects(cx); // the remembered project is gone (another workspace, or removed)
        return;
      }
      if (st.ok && ex.ok) { this.stages = st.data; this.exps = ex.data; } else this.problem = st.ok ? ex : st;
      if (this.openFirst && this.exps) { this.open = this.exps.experiments.find((e) => !e.is_champion)?.id ?? null; this.openFirst = false; }
      cx.notify();
    });
    if (this.everything) this.loadHome(cx);
  }

  loadHome(cx) {
    const pid = this.pid();
    if (!pid) return;
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${enc(pid)}/home`, { scoped: false });
      if (this.pid() !== pid) return;
      this.home = r.ok ? r.data : { problem: r };
      cx.notify();
    });
  }

  project() { return (this.projects ?? []).find((p) => p.id === this.pid()) ?? null; }

  /** One write, then read the project again. */
  act(cx, path, body, done, after) {
    if (this.busy) return;
    this.busy = true; this.note = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(path, { body, scoped: false });
      this.busy = false;
      if (!r.ok) { this.note = { tone: "fail", text: plainError(r.data?.error ?? r.error, "That didn't work. Try again.") }; cx.notify(); return; }
      this.note = done ? { tone: "good", text: done } : null;
      after?.(r.data);
      setShared("captain.explorer.rev", Date.now());
      this.load(cx);
    });
  }

  backtest(cx, e) {
    this.act(cx, `/api/projects/${enc(this.pid())}/experiments/${enc(e.id)}/backtest`, {}, `Judging ${e.pipeline} against the champion. It's open in Runs, live.`,
      (d) => { if (d?.live) { setShared("captain.open", { section: "runs", live: d.live, at: Date.now() }); run("layout.select", { stack: "sections", child: "runs" }); } });
  }

  makeChampion(cx, e) {
    this.act(cx, `/api/projects/${enc(this.pid())}/champion`, { experiment: e.id, expect: this.exps?.champion ?? null }, `${e.pipeline} is the champion now.`);
  }

  usePin(cx, slot, ref) {
    this.act(cx, `/api/projects/${enc(this.pid())}/pin`, { [slot]: ref }, `${SLOTS[slot].title} now uses ${ref.replace(/:holdout$/, "")}.`);
  }

  values() {
    const pins = this.stages ? Object.fromEntries(this.stages.inputs.map((i) => [i.slot, i.pinned])) : {};
    const reg = this.stages?.ship?.registry ?? {};
    return { project: this.pid(), pins, champion: this.exps?.champion ?? null, arch: reg.arch ?? undefined, brand: reg.brand ?? undefined, registry: reg.uri ?? undefined };
  }

  // ---- inputs ---------------------------------------------------------------------------------------------------

  inputCard(c, inp) {
    const meta = SLOTS[inp.slot] ?? { title: inp.slot, icon: "database", about: "", noun: inp.slot };
    const r = parseRef(inp.pinned);
    const newer = inp.next?.action === "pin" && inp.next.ref ? parseRef(inp.next.ref) : null;
    const missing = inp.status === "missing";
    const action = missing
      ? Button(c, `in-${inp.slot}-pick`, `Choose ${meta.noun}`, () => form("pin", { ...this.values(), slot: inp.slot }), { kind: "primary", small: true })
      : newer
        ? Button(c, `in-${inp.slot}-newer`, `Use version ${newer.version}`, (_e, cx) => this.usePin(cx, inp.slot, inp.next.ref), { kind: "primary", small: true, disabled: this.busy })
        : Button(c, `in-${inp.slot}-change`, "Change", () => form("pin", { ...this.values(), slot: inp.slot }), { small: true });
    return v_flex().id(`in-${inp.slot}`).w(212).min_h(150).flex_none().p(12).gap(8).bg(c.surface).border_1().border_color(missing ? c.grey300 : c.rule)
      .child(h_flex().gap(8).items_center()
        .child(div().size(26).flex_none().flex().items_center().justify_center().bg(missing ? c.surface2 : c.blueLight).text_color(missing ? c.grey500 : c.blue)
          .child(svg(icon(meta.icon)).size(14)))
        .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().child(meta.title))
        .child(Badge(c, missing ? "missing" : "pinned", missing ? "warn" : "good")))
      .child(missing
        ? div().text_size(12).line_height("16px").text_color(c.grey500).child(meta.about)
        : h_flex().h(20).gap(6).items_center().min_w(0)
          .child(div().min_w(0).truncate().font_family(MONO).text_size(12).child(r.name))
          .when(r.version != null, (e) => e.child(Badge(c, `v${r.version}`)))
          .when(Boolean(r.split), (e) => e.child(Badge(c, r.split, "blue"))))
      .children((inp.info ?? []).slice(0, 2).map((i, k) => h_flex().id(`in-${inp.slot}-info-${k}`).gap(6).items_start()
        .child(svg(icon(i.code === "newer-version" ? "sparkles" : "triangle-alert")).size(12).mt(2).flex_none().text_color(i.code === "newer-version" ? c.blue : c.warn))
        .child(div().flex_1().min_w(0).text_size(11.5).line_height("16px").text_color(c.grey900).child(i.text))))
      .child(div().flex_1())
      .child(h_flex().child(action));
  }

  inputs(c) {
    const st = this.stages;
    return this.part(c, "inputs", "Inputs", "Each pinned to an exact version, so every experiment trains and is judged on the same data.", [],
      v_flex().gap(8)
        .child(h_flex().flex_wrap().items_start().gap(10).children(st.inputs.map((i) => this.inputCard(c, i))))
        .children((st.notes ?? []).map((n, k) => div().id(`note-${k}`).text_size(12).text_color(c.grey500)
          .child(n.code === "index-incomplete" ? "Lineage for versions made before this release isn't indexed yet, so a newer version may not show here." : n.text))));
  }

  // ---- experiments ----------------------------------------------------------------------------------------------

  metric(c, label, value, sub) {
    return v_flex().flex_none().gap(1)
      .child(div().text_size(10.5).text_color(c.grey500).child(label))
      .child(h_flex().gap(4).items_baseline()
        .child(div().font_family(MONO).text_size(12.5).child(value))
        .when(Boolean(sub), (e) => e.child(div().font_family(MONO).text_size(11).text_color(c.grey500).child(sub))));
  }

  expRow(c, e, i) {
    const ev = e.evaluation;
    const v = verdictOf(ev);
    const own = v.tone === "warn";
    const open = this.open === e.id;
    const r = parseRef(e.pipeline);
    const ind = ev?.label_independence ? INDEPENDENCE[ev.label_independence.status] ?? INDEPENDENCE.unknown : null;
    const toneColor = { good: c.good, bad: c.fail, warn: c.warn, neutral: c.grey500 }[v.tone];
    const sub = e.status === "backtesting" ? "Being judged against the champion" : e.status === "failed" ? "Judging failed. Open it to try again." : e.trainer ? `Trained with ${e.trainer}` : `Added ${day(e.created_at)}`;
    // Two lines, so nothing spills at any width: who and the verdict, then the numbers and badges, which wrap.
    const head = v_flex().id(`exp-${i}`).px(14).py(8).gap(6).cursor_pointer().hover((s) => s.bg(c.surface2))
      .on_click((_e, cx) => { this.open = open ? null : e.id; cx.notify(); })
      .child(h_flex().gap(10).items_center()
        .child(svg(icon(e.is_champion ? "trophy" : own ? "triangle-alert" : "flask-conical")).size(15).flex_none().text_color(e.is_champion ? c.blue : own ? c.warn : c.grey500))
        .child(v_flex().flex_1().min_w(0).gap(1)
          .child(h_flex().gap(6).items_center().min_w(0)
            .child(div().min_w(0).truncate().font_family(MONO).text_size(12.5).font_medium().child(r.name))
            .when(r.version != null, (x) => x.child(Badge(c, `v${r.version}`)))
            .when(e.is_champion, (x) => x.child(Badge(c, "champion", "blue"))))
          .child(div().truncate().text_size(11.5).text_color(c.grey500).child(sub)))
        .when(!e.is_champion, (x) => x.child(h_flex().flex_none().gap(6).items_center()
          .child(svg(icon(v.icon)).size(13).flex_none().text_color(toneColor))
          .child(div().text_size(12.5).font_medium().text_color(toneColor).child(v.text))))
        .child(svg(icon(open ? "chevron-down" : "chevron-right")).size(12).flex_none().text_color(c.grey300)))
      .child(h_flex().pl(25).flex_wrap().gap(16).items_center()
        .when(Boolean(ev && ev.value != null), (x) => x
          .child(this.metric(c, "nDCG@10", Number(ev.value).toFixed(4), ev.delta != null ? `${signed(ev.delta)} [${signed(ev.ci_low)}, ${signed(ev.ci_high)}]` : null))
          .when(Boolean(ev.recall), (y) => y.child(this.metric(c, `Recall@${String(ev.recall.metric).replace(/^r@/, "")}`, Number(ev.recall.value).toFixed(3))))
          .when(ev.judged_at_10 != null, (y) => y.child(this.metric(c, "Judged@10", `${Math.round(ev.judged_at_10 * 100)}%`))))
        .when(Boolean(ev?.evidence), (x) => x.child(Badge(c, EVIDENCE[ev.evidence] ?? String(ev.evidence).toLowerCase())))
        .when(Boolean(ind), (x) => x.child(Badge(c, ind[0], ind[1])))
        .when(Boolean(ev), (x) => x.child(div().text_size(11.5).text_color(c.grey500).child("not yet confirmed online")))
        .when(e.is_champion && !ev, (x) => x.child(div().text_size(12).text_color(c.grey500).child("The others are judged against this one."))));
    return v_flex().id(`exp-wrap-${i}`).w_full().border_b(1).border_color(c.rule)
      .when(own, (x) => x.bg(c.warnLight))
      .child(h_flex().w_full().when(own, (x) => x.child(div().w(3).flex_none().self_stretch().bg(c.warn))).child(v_flex().flex_1().min_w(0).child(head)
        .when(open, (x) => x.child(this.expDetail(c, e, ev, i)))));
  }

  expDetail(c, e, ev, i) {
    const champ = this.exps?.champion;
    const ind = ev?.label_independence;
    const name = parseRef(e.pipeline).name;
    const evWord = ev?.evidence ? EVIDENCE[ev.evidence] ?? String(ev.evidence).toLowerCase() : "not judged yet";
    const lines = [
      e.error ? plainError(e.error, "Judging it didn't finish.") : null,
      ev?.against && !ev.against_is_champion ? `Judged against ${ev.against}, which is no longer the champion. Judge it again to compare with today's.` : null,
      ind?.text ?? null,
      ind?.status === "shared" ? "So the higher score shows the model learned that labeler, not that it ranks better. Judge it on labels from another source to know." : null,
      ev?.click_grades ? "Click grades only exist for products the old ranker showed, which favors the old ranker." : null,
      ev ? "Not yet confirmed online: an offline result until your team's own A/B or shadow readout agrees." : e.is_champion ? null : "Not judged against the champion yet.",
    ].filter(Boolean);
    return v_flex().px(14).pl(39).pb(12).gap(8)
      .children(lines.map((t, k) => div().id(`exp-${i}-line-${k}`).text_size(12).line_height("17px").text_color(c.grey900).child(t)))
      .child(h_flex().flex_wrap().gap(6)
        .when(Boolean(ev?.run), (x) => x.child(Button(c, `exp-${i}-open`, "Open the comparison", () => {
          const r = parseRef(ev.run);
          go("comparisons", { backtest: r.name, version: r.version ?? undefined });
        }, { small: true })))
        .child(Button(c, `exp-${i}-rerun`, "Rerun with changes", () => form("experiment", { ...this.values(), base: name, labels: e.labels ?? undefined }), { small: true }))
        .when(Boolean(champ) && !e.is_champion, (x) => x.child(Button(c, `exp-${i}-judge`, ev ? "Judge again against the champion" : "Judge against the champion", (_e, cx) => this.backtest(cx, e), { small: true, kind: ev ? "secondary" : "primary", disabled: this.busy || e.status === "backtesting" })))
        .when(!e.is_champion, (x) => x.child(Button(c, `exp-${i}-champion`, `Make champion (${evWord})`, (_e, cx) => this.makeChampion(cx, e), { small: true, disabled: this.busy })))
        .child(Button(c, `exp-${i}-publish`, "Publish to the registry", () => form("publish", { ...this.values(), pipeline: e.pipeline }), { small: true, kind: "quiet" })));
  }

  experiments(c) {
    const xs = this.exps?.experiments ?? [];
    const champ = this.exps?.champion;
    const actions = [
      Button(c, "exp-new", "New experiment", () => form("experiment", { ...this.values(), base: champ ? parseRef(champ).name : undefined }), { small: true, kind: "primary" }),
      Button(c, "exp-import", "Import a model version", () => form("import-version", this.values()), { small: true }),
    ];
    const body = !xs.length
      ? v_flex().w_full().border_1().border_color(c.rule).bg(c.surface).child(Empty(c, "No experiments yet", "An experiment is one trained version of your ranker. Train one from the pinned inputs, or bring a version trained in MLflow or your own registry."))
      : v_flex().w_full().border_1().border_color(c.rule).bg(c.surface).children(xs.map((e, i) => this.expRow(c, e, i)));
    const hint = !champ && xs.length ? "Make one experiment the champion. Every other one is judged against it on the sealed holdout." : "Each one is judged against the champion on the sealed holdout and the pinned labels.";
    return this.part(c, "experiments", "Experiments", hint, actions, body);
  }

  // ---- ship -----------------------------------------------------------------------------------------------------

  tile(c, id, ic, title, lines, action) {
    return v_flex().id(id).w(212).min_h(150).flex_none().p(12).gap(6).bg(c.surface).border_1().border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(svg(icon(ic)).size(14).flex_none().text_color(c.blue))
        .child(div().text_size(13).font_semibold().child(title)))
      .children(lines.filter(Boolean).map((t, k) => div().id(`${id}-l${k}`).text_size(12).line_height("17px").text_color(c.grey900).child(t)))
      .child(div().flex_1())
      .when(Boolean(action), (e) => e.child(h_flex().child(action)));
  }

  ship(c) {
    const st = this.stages, champ = this.exps?.champion;
    const reg = st.ship?.registry ?? {};
    const where = reg.arch && reg.brand ? `${reg.arch} / ${reg.brand}` : null;
    return this.part(c, "ship", "Ship", "Where the champion goes once it wins.", [],
      h_flex().flex_wrap().items_start().gap(10)
        .child(this.tile(c, "ship-registry", "package", "Model registry",
          [where ? `Publishes to ${where}${reg.uri ? "" : " in the default registry"}.` : "Not set yet. Publishing asks where, once.", champ ? null : "Make a champion first."],
          champ ? Button(c, "ship-publish", "Publish the champion", () => form("publish", { ...this.values(), pipeline: champ }), { small: true }) : null))
        .child(this.tile(c, "ship-staging", "server", "Staging",
          ["Serving a trained ranker on a staging endpoint comes in a later release.", "Until then, hand a candidate to your own staging."], null))
        .child(this.tile(c, "ship-live", "send", "Live handoff",
          ["Readiness checks, then a bundle for your deployment process. Captain deploys nothing itself."],
          champ ? Button(c, "ship-handoff", "Hand off the champion", () => form("handoff", { pipeline: champ }), { small: true }) : null))
        .child(this.tile(c, "ship-online", "activity", "Online check",
          ["Not yet confirmed online.", "Bring in your team's A/B or shadow readout from Handoff once it runs."],
          Button(c, "ship-open-handoff", "Open Handoff", () => go("handoff"), { small: true, kind: "quiet" }))));
  }

  // ---- everything in the project (the earlier home) --------------------------------------------------------------

  loadChoices(cx) {
    cx.spawn(async (cx) => {
      const [m, p] = await Promise.all([api("/api/models", { scoped: false }), api("/api/projects", { scoped: false })]);
      this.allModels = m.ok && Array.isArray(m.data) ? m.data : [];
      this.owners = {};
      for (const pr of p.ok ? (Array.isArray(p.data) ? p.data : p.data?.projects ?? []) : []) for (const b of pr.brands ?? []) this.owners[b] = pr;
      cx.notify();
    });
  }

  toggleChoosing(cx) {
    this.choosing = !this.choosing;
    if (this.choosing) this.loadChoices(cx);
    cx.notify();
  }

  change(cx, req, note) {
    if (this.busy) return;
    this.busy = true;
    cx.spawn(async (cx) => {
      for (const [path, opts] of req) {
        const r = await api(path, { ...opts, scoped: false });
        if (!r.ok) { this.busy = false; this.note = { text: plainError(r.data?.error ?? r.error, "Couldn't save that."), tone: "fail" }; cx.notify(); return; }
      }
      this.busy = false;
      this.note = { text: note, tone: "good" };
      setShared("captain.explorer.rev", Date.now());
      this.loadHome(cx);
      this.loadChoices(cx);
    });
    cx.notify();
  }

  box(c, on) { return svg(icon(on ? "square-check" : "square")).size(15).flex_none().text_color(on ? c.blue : c.grey500); }

  chooser(c) {
    const h = this.home, all = this.allModels;
    if (!all) return div().px(14).py(10).text_size(12.5).text_color(c.grey500).child("Reading the registries...");
    if (!all.length) return div().px(14).py(10).text_size(12.5).text_color(c.grey500).child("No registry models yet. Attach a registry in Data and signals, Models.");
    const mine = new Set((h.models ?? []).map((m) => m.id));
    const byBrand = new Map();
    for (const m of all) { if (!byBrand.has(m.brand)) byBrand.set(m.brand, []); byBrand.get(m.brand).push(m); }
    const brands = [...byBrand.keys()].sort();
    const p = h.project;
    return v_flex().id("ph-choose").max_h(420).overflow_y_scrollbar().border_b(1).border_color(c.rule).bg(c.surface2)
      .child(div().px(14).py(8).text_size(12).text_color(c.grey900).child("Tick a brand to include all its models, and the ones trained later. Or tick models one by one."))
      .children(brands.map((b, bi) => {
        const ms = byBrand.get(b).sort((x, y) => String(x.version).localeCompare(String(y.version), undefined, { numeric: true }));
        const on = (p.brands ?? []).includes(b);
        const other = !on && this.owners[b] && this.owners[b].id !== p.id ? this.owners[b].name : null;
        const brandsNext = on ? (p.brands ?? []).filter((x) => x !== b) : [...(p.brands ?? []), b];
        const req = [[`/api/projects/${enc(p.id)}`, { method: "PUT", body: { brands: brandsNext } }]];
        // Turning a brand on brings back every model of it, including ones left out one by one.
        if (!on) req.push(["/api/projects/assign", { body: { project: p.id, clear: true, items: ms.map((m) => ({ kind: "model", name: m.id })) } }]);
        return v_flex().id(`ph-cb-${bi}`)
          .child(h_flex().id(`ph-brand-${bi}`).h(32).px(14).gap(8).items_center().cursor_pointer().hover((s) => s.bg(c.surface))
            .on_click((_e, cx) => this.change(cx, req, on ? `${b}'s models are no longer included by brand. Models you added one by one stay.` : `Every ${b} model is in ${p.name}, and new ones will be too.`))
            .child(this.box(c, on))
            .child(div().text_size(12.5).font_semibold().child(`All ${b} models`))
            .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(
              `${ms.filter((m) => mine.has(m.id)).length} of ${ms.length} in this project${other ? `. Now in ${other}, ticking moves the brand here` : ""}`)))
          .children(ms.map((m, i) => {
            const isIn = mine.has(m.id);
            return h_flex().id(`ph-pick-${bi}-${i}`).h(28).pl(36).pr(14).gap(8).items_center().cursor_pointer().hover((s) => s.bg(c.surface))
              .on_click((_e, cx) => this.change(cx, [["/api/projects/assign", { body: { project: isIn ? null : p.id, items: [{ kind: "model", name: m.id }] } }]],
                isIn ? `Left ${m.brand} ${m.version} out of ${p.name}.` : `Added ${m.brand} ${m.version} to ${p.name}.`))
              .child(this.box(c, isIn))
              .child(div().w(180).flex_none().truncate().font_family(MONO).text_size(12).child(`${m.brand} ${m.version}`))
              .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child([m.architecture, m.status_kind ?? m.status].filter(Boolean).join(", ")));
          }));
      }));
  }

  row(c, id, title, sub, chips, ic, onClick, mono = true) {
    return h_flex().id(id).min_h(40).px(14).py(6).gap(10).items_center().border_b(1).border_color(c.rule).cursor_pointer()
      .hover((s) => s.bg(c.surface2)).on_click(onClick)
      .child(svg(icon(ic)).size(14).flex_none().text_color(c.grey500))
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(div().truncate().text_size(13).font_medium().text_color(c.navy).when(mono, (e) => e.font_family(MONO).text_size(12.5)).child(title))
        .when(Boolean(sub), (e) => e.child(div().truncate().text_size(12).text_color(c.grey500).child(sub))))
      .children((chips ?? []).filter(Boolean).map((ch) => Badge(c, ch, "neutral")))
      .child(svg(icon("chevron-right")).size(12).flex_none().text_color(c.grey300));
  }

  section(c, key, title, items, rowOf, empty, action = null, extra = null) {
    return v_flex().id(`ph-${key}`).w_full().border_1().border_color(c.rule).bg(c.surface)
      .child(h_flex().h(34).px(14).gap(8).items_center().border_b(1).border_color(c.rule)
        .child(div().text_size(12).font_semibold().text_color(c.grey500).child(title.toUpperCase()))
        .child(div().flex_1().text_size(12).font_family(MONO).text_color(c.grey500).child(int(items.length)))
        .when(Boolean(action), (e) => e.child(action)))
      .when(Boolean(extra), (e) => e.child(extra))
      .children(items.length ? items.slice(0, 12).map((x, i) => rowOf(x, i)) : [div().px(14).py(10).text_size(12.5).text_color(c.grey500).child(empty)]);
  }

  everythingBody(c) {
    const h = this.home;
    if (!h) return Skeleton(c, 4);
    if (h.problem) return Problem(c, h.problem, (cx) => this.loadHome(cx));
    const id = h.project?.id ?? "";
    const joins = `Assign it to the project, or name it starting with ${id}.`;
    const models = [...(h.models ?? []).map((m) => ({ ...m, from: "registry" })), ...(h.mlflow_models ?? []).map((m) => ({ ...m, from: "MLflow" }))];
    return v_flex().gap(14)
      .child(this.section(c, "datasets", "Datasets", h.datasets ?? [],
        (d, i) => this.row(c, `ph-ds-${i}`, d.name, d.description || `${int(d.rows)} rows, by ${d.created_by ?? "?"} ${day(d.created_at)}`, [d.version != null ? `v${d.version}` : null], "database", () => go("data", { dataset: d.name })),
        `No datasets yet. ${joins}`))
      .child(this.section(c, "runs", "Recent runs", h.runs ?? [],
        (r, i) => this.row(c, `ph-run-${i}`, r.backtest, `${(r.arms ?? []).join(" vs ")}, ${day(r.created_at)}`, [String(r.run ?? "").split("@")[1] ?? null], "git-compare",
          () => go("comparisons", { backtest: r.backtest, version: Number(String(r.run ?? "").split("@v")[1]) || undefined })),
        "No runs yet. Judge an experiment against the champion."))
      .child(this.section(c, "pipelines", "Pipelines", h.pipelines ?? [],
        (p, i) => this.row(c, `ph-pl-${i}`, `${p.name}@v${p.version ?? 1}`, `Backtest other settings (by ${p.created_by ?? "?"} ${day(p.created_at)})`, [], "layers",
          () => openOverlay({ mod: "captain-sandbox", kind: "dialog", width: 0.9, height: 0.9, props: { pipeline: p.name } })),
        `No pipelines yet. ${joins}`))
      .child(this.section(c, "models", "Models", models,
        (m, i) => this.row(c, `ph-md-${i}`, m.model ?? m.name ?? m.brand ?? m.id ?? "", m.pipeline ? `${m.from}, served by ${m.pipeline}` : [m.from, m.architecture, m.status_kind].filter(Boolean).join(", "), [m.version != null ? (/^\d+$/.test(String(m.version)) ? `v${m.version}` : String(m.version)) : null], "package", () => go("data", { view: "models" })),
        "No models yet. Choose models, or tick a registry brand to bring all of its models.",
        Button(c, "ph-choose-models", this.choosing ? "Done" : "Choose models", (_e, cx) => this.toggleChoosing(cx), { small: true, kind: this.choosing ? "primary" : "secondary" }),
        this.choosing ? this.chooser(c) : null))
      .child(this.section(c, "notebooks", "Notebooks", h.notebooks ?? [],
        (n, i) => this.row(c, `ph-nb-${i}`, n.name ?? n.title ?? "", n.description ?? "", [], "file-text", () => go("data", { view: "notebooks", notebook: n.name })),
        "No notebooks yet. Assign a saved notebook to the project."));
  }

  everythingPart(c) {
    const toggle = h_flex().id("ph-everything").h(36).gap(8).items_center().cursor_pointer().text_color(c.grey900).hover((s) => s.text_color(c.blue))
      .on_click((_e, cx) => { this.everything = !this.everything; if (this.everything) this.loadHome(cx); cx.notify(); })
      .child(svg(icon(this.everything ? "chevron-down" : "chevron-right")).size(13))
      .child(div().text_size(13).font_semibold().child("Everything in this project"))
      .child(div().min_w(0).truncate().text_size(12).text_color(c.grey500).child("datasets, runs, pipelines, models and notebooks"));
    return v_flex().gap(8).child(toggle).when(this.everything, (e) => e.child(this.everythingBody(c)));
  }

  // ---- frame ----------------------------------------------------------------------------------------------------

  part(c, key, title, sub, actions, body) {
    return v_flex().id(`part-${key}`).w_full().gap(10)
      .child(h_flex().gap(10).items_center()
        .child(v_flex().flex_1().min_w(0).gap(1)
          .child(div().text_size(15).font_semibold().child(title))
          .child(div().truncate().text_size(12).text_color(c.grey500).child(sub)))
        .children(actions))
      .child(body);
  }

  list(c) {
    const ps = this.projects;
    if (ps === null) return Skeleton(c, 4);
    const newBtn = Button(c, "ph-empty-new", "New project", () => form("project"), { kind: "primary" });
    if (!ps.length) return Empty(c, "Start with a project", "A project holds one search you're improving: its catalog, the labels that grade it, the experiments you train and the champion you ship.", newBtn);
    return this.section(c, "all", "Projects", ps, (p, i) => this.row(c, `ph-p-${i}`, p.name, p.description ?? "", [], "workflow", (_e, cx) => this.pickProject(cx, p), false), "");
  }

  render() {
    const c = palette(isDark(theme()));
    const pid = this.pid();
    const p = this.project();
    const head = h_flex().h(64).flex_none().px(24).gap(10).items_center().border_b(1).border_color(c.rule)
      .child(div().size(32).flex_none().flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon(pid ? "workflow" : "house")).size(16)))
      .child(v_flex().flex_1().min_w(0)
        .child(div().text_size(18).font_semibold().truncate().child(p?.name ?? (pid ? shared("captain.scope")?.label ?? pid : "Home")))
        .child(div().text_size(12).text_color(c.grey500).truncate().child(p?.description || (pid ? "Pinned inputs, experiments against the champion, and where it ships." : "Pick a project, or start one."))))
      .child(Button(c, "ph-workflows", "Workflows", () => go("runs", { tab: "workflows" }), { small: true, kind: "quiet" }))
      .when(Boolean(pid), (e) => e.child(Button(c, "ph-all", "All projects", (_e, cx) => this.allProjects(cx), { small: true, kind: "quiet" })))
      .child(Button(c, "ph-new", "New project", () => form("project"), { small: true }));
    let body;
    if (!pid) body = this.list(c);
    else if (this.problem) body = Problem(c, this.problem, (cx) => this.load(cx));
    else if (!this.stages || !this.exps) body = Skeleton(c, 6);
    else body = v_flex().gap(22)
      .when(Boolean(this.note), (e) => e.child(div().id("ph-note").px(12).py(8).text_size(12.5).bg(this.note.tone === "fail" ? c.failLight : c.goodLight)
        .text_color(this.note.tone === "fail" ? c.fail : c.good).child(this.note.text)))
      .child(this.inputs(c))
      .child(this.experiments(c))
      .child(this.ship(c))
      .child(this.everythingPart(c));
    return v_flex().id("project-home").size_full().bg(c.canvas).font_family(FONT).text_color(c.navy)
      .child(head)
      .child(div().id("ph-body").flex_1().min_h(0).overflow_y_scroll().child(v_flex().px(24).py(16).child(body)));
  }
}
