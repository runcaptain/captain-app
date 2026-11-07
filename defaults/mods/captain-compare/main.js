// CAPTAIN: Comparisons, native. One backtest run, read in depth: what can be trusted (compatibility warnings, shown
// above any winner), the arms with their cost, every comparison, the per-query delta distribution, the queries
// themselves (paged and sorted by the backend) with any query side by side, the segments, the surfaces, and the
// profile: where each arm's time goes (profile.js).
// Runs hands a run over through shared "captain.compare" = {backtest, version}.
//
// Per-query rows come in pages of 200 from GET /api/runs/{run}/queries; the histogram comes pre-binned. The mod never
// holds or sorts a whole result set (docs/captain/RISKS.md).
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar, InputState, Input } from "gpui-base";
import { theme, shared, openOverlay, props as layoutProps } from "herdr";
import { api, retry, details, watchWorkspace, watchScope } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Loading, Skeleton, Button } from "./kit/ui.js";
import { askAgent, copyPrompt, openForm } from "./kit/agent.js";
import { metricWords, warningIcon } from "./kit/words.js";
import { icon as kitIcon } from "./kit/icons.js";
import { int } from "./kit/num.js";
import { Profiler } from "./profile.js";

const PAGE = 200;
const ROW = 28;
const ICON = (n) => `mods/captain-compare/icons/${n}.svg`;
const enc = (name) => name.split("/").map(encodeURIComponent).join("/");
const fmtD = (x) => (x === null || x === undefined ? "" : `${x >= 0 ? "+" : ""}${Number(x).toFixed(4)}`);
const fmt4 = (x) => (x === null || x === undefined ? "none" : Number(x).toFixed(4));
const pct = (x) => (x === null || x === undefined ? "none" : `${Math.round(x * 100)}%`);
const usd = (x, d = 4) => (x === null || x === undefined ? null : `$${Number(x).toFixed(d)}`);
const GRADE_TONE = { 0: "grey", 1: "partial", 2: "relevant", 3: "exact" };

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

/** A versioned name as people read it ("name@v2" becomes the name and a small version chip). */
function named(c, ref, { size = 12.5, color } = {}) {
  const m = /^(.*)@v(\d+)$/.exec(String(ref ?? ""));
  const name = m ? m[1] : String(ref ?? "");
  return h_flex().min_w(0).gap(6).items_center()
    .child(div().min_w(0).truncate().text_size(size).font_family(MONO).text_color(color ?? c.navy).child(name))
    .when(!!m, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10.5).font_family(MONO).text_color(c.grey900).child(`v${m[2]}`)));
}
const bare = (ref) => String(ref ?? "").replace(/@v\d+$/, "");

/** What an agent should look into for one query that moved. */
function queryPrompt(run, row, d) {
  return [
    `In Captain, in the backtest run ${run}, look at the query "${row.query || d?.query || row.query_id}".`,
    d ? `Under A (${bare(d.a?.arm)}) its nDCG@10 is ${d.a?.ndcg10}, under B (${bare(d.b?.arm)}) it is ${d.b?.ndcg10}.` : "",
    "Compare the two ranked lists, say why B ranks the way it does, and whether this is a real regression or a judging gap",
    "(unjudged products count as not relevant). Use the cst CLI to read the run. Change nothing without asking me.",
  ].filter(Boolean).join(" ");
}

/** A backend sentence as a sentence: capital first, full stop at the end. */
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };

/** What an agent should look into for a whole comparison. */
function runPrompt(ref, r, compat) {
  const p = (r.comparisons ?? []).find((x) => x.primary) ?? r.comparisons?.[0];
  return [
    `In Captain, look at the backtest run ${ref}${r.mode === "imported" ? ", imported from a readout" : ""}: it compares ${(r.arms ?? []).map((a) => bare(a.arm)).join(" against ")}.`,
    p ? `The main result is ${metricWords(p.metric)}: B minus A is ${p.delta}, 95% interval [${p.ci_low}, ${p.ci_high}], verdict ${p.verdict}.` : "",
    (compat?.warnings ?? []).length ? `Captain warns: ${compat.warnings.map((w) => w.text).join("; ")}.` : "",
    "Tell me whether to trust it, what would make it conclusive, and what to try next.",
    "Use the cst CLI to read the run. Change nothing without asking me.",
  ].filter(Boolean).join(" ");
}

export default class CaptainCompare extends View {
  init(_p, cx) {
    const props = { ...(layoutProps?.() ?? {}), ...(_p ?? {}) };
    // Props: {backtest, version, tab, query} open that run on that tab, with that query side by side.
    this.wantProps = props ?? {};
    this.tab = props?.tab ?? "overview";
    this.autoProfile = !!props?.autoProfile; // start a profile as soon as the run opens
    this.width = 1000;
    this.backtests = null;
    this.sel = null; // {backtest, version}
    this.run = null;
    this.compat = null;
    this.hist = null;
    this.q = this.freshQueries();
    this.side = null;
    this.qSearch = InputState.new({ placeholder: "Search queries" });
    this.qSearch.on("submit", (_e, cx) => this.queries(cx, { q: this.qSearch.value().trim() }));
    this.handed = null;
    this.profiler = new Profiler(this);
    this.loadBacktests(cx);
    cx.timer.every(150, (cx) => this.pump(cx));
    // Until the backend answers, ask again every 3 s.
    // Until the list loads, ask again every 3 s: while the services start, and after any failure that wasn't the
    // backend answering (they stopped or restarted). A run record lost the same way is asked for again too.
    cx.timer.every(3000, (cx) => {
      if (this.starting || (this.error && !this.errorR?.status)) this.loadBacktests(cx);
      else if (this.sel && this.run?.error && !this.runR?.status) this.open(cx, this.sel.backtest, this.sel.version);
    });
    // Opened by name (palette): shared "captain.open" {section: "comparisons", backtest, version, at}.
    cx.timer.every(500, (cx) => this.profiler.tick(cx));
    cx.timer.every(500, (cx) => {
      const o = shared("captain.open");
      if (o?.section === "comparisons" && o.backtest && o.at !== this.opened) {
        this.opened = o.at;
        const v = o.version ?? this.backtests?.find((b) => b.name === o.backtest)?.latest ?? 1;
        this.open(cx, o.backtest, v);
      }
    });
    // The Explorer's pick filters the backtests: reload them, keeping the open run if it's still listed.
    watchScope(cx, (cx) => { this.backtests = null; this.loadBacktests(cx); cx.notify(); });
    // Another space, another workspace: back to its first run.
    watchWorkspace(cx, (cx) => {
      this.sel = null; this.run = null; this.compat = null; this.hist = null; this.side = null; this.backtests = null; this.error = null;
      this.q = this.freshQueries();
      this.loadBacktests(cx);
      cx.notify();
    });
  }

  loadBacktests(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/backtests");
      this.starting = !r.ok && !!r.starting;
      if (this.starting) { cx.notify(); return; }
      if (r.ok) this.backtests = r.data ?? [];
      this.error = r.ok ? null : say(r, "Couldn't load backtests.");
      this.errorR = r;
      const h = shared("captain.compare") ?? (this.wantProps.backtest ? { backtest: this.wantProps.backtest, version: this.wantProps.version ?? 1 } : null);
      const first = h?.backtest ? h : this.backtests?.[0] ? { backtest: this.backtests[0].name, version: this.backtests[0].latest } : null;
      if (h) this.handed = `${h.backtest}@${h.version}`;
      // The run on screen stays when it's still listed (the list reloads when the Explorer's pick changes).
      const kept = this.sel && (this.backtests ?? []).some((b) => b.name === this.sel.backtest);
      if (first && !kept) this.open(cx, first.backtest, first.version);
      else if (!first && !kept) { this.sel = null; this.run = null; }
      cx.notify();
    });
  }

  freshQueries(over = {}) {
    return { pages: new Map(), inflight: null, total: null, counts: null, want: [0, 0], seq: (this.q?.seq ?? 0) + 1, sort: "delta", desc: false, only: null, q: "", error: null, ...over };
  }

  runRef() {
    return this.sel ? `${this.sel.backtest}@v${this.sel.version}` : null;
  }

  open(cx, backtest, version) {
    this.sel = { backtest, version };
    this.run = null;
    this.compat = null;
    this.hist = null;
    this.side = null;
    this.q = this.freshQueries();
    const ref = this.runRef();
    // The run record first, on its own: the overview draws from it alone. The warnings and the histogram fill in
    // when they come, and a failing or slow one never holds the screen (imported runs have no histogram at all).
    cx.spawn(async (cx) => {
      const c = await api(`/api/runs/${enc(ref)}/compatibility`);
      if (this.runRef() !== ref) return;
      this.compat = c.ok ? c.data : null;
      cx.notify();
    });
    cx.spawn(async (cx) => {
      const r = await api(`/api/runs/${enc(backtest)}?v=${version}`);
      if (this.runRef() !== ref) return;
      this.run = r.ok ? r.data : { error: say(r, "Couldn't load this run.", "This run's record is missing from the workspace.") };
      this.runR = r;
      cx.notify();
      if (!r.ok || r.data?.mode === "imported") return;
      const h = await api(`/api/runs/${enc(ref)}/histogram?bins=21`);
      if (this.runRef() !== ref) return;
      this.hist = h.ok ? h.data : null;
      if (this.wantProps.query) {
        const q = this.wantProps.query;
        this.wantProps.query = null;
        this.openSide(cx, { query_id: q, query: "" });
      }
      cx.notify();
    });
    cx.notify();
  }

  queries(cx, change) {
    const q = this.q;
    this.q = this.freshQueries({ sort: q.sort, desc: q.desc, only: q.only, q: q.q, ...change });
    cx.notify();
  }

  url(offset) {
    const q = this.q;
    const p = new URLSearchParams({ sort: q.sort, offset: String(offset), limit: String(PAGE) });
    if (q.desc) p.set("desc", "true");
    if (q.only) p.set("only", q.only);
    if (q.q) p.set("q", q.q);
    return `/api/runs/${enc(this.runRef())}/queries?${p}`;
  }

  /** Fetch one missing page near the visible query rows; drop pages far from them. */
  pump(cx) {
    // A run handed over from Runs while this copy was already open.
    const h = shared("captain.compare");
    if (h?.backtest && `${h.backtest}@${h.version}` !== this.handed && this.backtests) {
      this.handed = `${h.backtest}@${h.version}`;
      this.tab = "overview";
      this.open(cx, h.backtest, h.version);
    }
    const q = this.q;
    if (this.tab !== "queries" || !this.sel || q.inflight !== null || q.error || this.run?.mode === "imported") return;
    const [a, b] = q.want;
    const first = Math.floor(a / PAGE), last = Math.floor(Math.max(a, b - 1) / PAGE);
    for (const k of [...q.pages.keys()]) if (k < first - 3 || k > last + 3) q.pages.delete(k);
    let page = null;
    for (let p = first; p <= last; p++) if (!q.pages.has(p) && (q.total === null || p * PAGE < q.total)) { page = p; break; }
    if (page === null) return;
    const seq = q.seq;
    q.inflight = page;
    cx.spawn(async (cx) => {
      const r = await api(this.url(page * PAGE));
      if (this.q.seq !== seq) return;
      this.q.inflight = null;
      if (!r.ok) this.q.error = say(r, "Couldn't load the queries.");
      else {
        this.q.pages.set(page, r.data.rows ?? []);
        this.q.total = r.data.total ?? 0;
        this.q.counts = r.data.counts ?? this.q.counts;
        this.q.unfiltered = r.data.total_unfiltered;
      }
      cx.notify();
    });
  }

  openSide(cx, row) {
    this.side = { row, data: null };
    const ref = this.runRef();
    cx.spawn(async (cx) => {
      const r = await api(`/api/runs/${enc(ref)}/queries/${encodeURIComponent(row.query_id)}?k=20`);
      if (this.side?.row !== row) return;
      this.side.data = r.ok ? r.data : { error: say(r, "Couldn't load this query.") };
      if (r.ok && !row.query) row.query = r.data?.query ?? row.query_id;
      cx.notify();
    });
    cx.notify();
  }

  // ---- pieces -------------------------------------------------------------------------------------------------

  icon(n, size = 14, color) {
    // "kit:activity": the shared icon set; anything else ships with this mod.
    return svg(String(n).startsWith("kit:") ? kitIcon(n.slice(4)) : ICON(n)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  badge(c, text, tone) {
    const fg = tone === "win" || tone === "blue" ? c.blue : tone === "loss" || tone === "fail" ? c.fail : c.grey900;
    return div().flex_none().h(20).px(6).flex().items_center().border_1().border_color(fg).text_size(10.5).font_medium().text_color(fg).child(String(text).toUpperCase());
  }

  gradeChip(c, g) {
    const label = g === null || g === undefined ? "?" : String(g);
    const bg = g === 3 ? c.blue : g === 2 ? c.blueLight : c.surface;
    const fg = g === 3 ? c.onBlue : g === null || g === undefined ? c.grey500 : c.navy;
    return div().flex_none().w(22).h(20).flex().items_center().justify_center().border_1()
      .border_color(g === null || g === undefined ? c.grey300 : g >= 2 ? c.blue : c.grey300).bg(bg).text_color(fg)
      .text_size(11).font_family(MONO).child(label).tooltip(g === null || g === undefined ? "not judged: counts as not relevant" : `grade ${g}`);
  }

  header(c) {
    const tabs = [["overview", "Overview", "overview"], ["queries", "Queries", "queries"], ["segments", "Segments", "segments"], ["surfaces", "Surfaces", "surfaces"], ["profile", "Profile", "kit:activity"]];
    return h_flex().h(52).flex_none().px(20).gap(14).items_center().border_b(1).border_color(c.rule)
      .child(div().flex_none().text_size(18).font_semibold().child("Comparisons"))
      .child(h_flex().flex_none().border_1().border_color(c.grey300).children(tabs.map(([id, label, icon]) =>
        h_flex().id(`tab-${id}`).h(28).px(12).gap(6).items_center().cursor_pointer().border_r(1).border_color(c.grey300).text_size(12.5)
          .bg(this.tab === id ? c.navy : c.surface).text_color(this.tab === id ? c.surface : c.grey900)
          .on_click((_e, cx) => { this.tab = id; cx.notify(); })
          .child(this.icon(icon, 13)).child(div().child(label)))))
      .child(div().flex_1());
  }

  /** The runs to pick from, as a short list of backtests with their versions. */
  picker(c, w) {
    return v_flex().id("compare-runs").w(w).h_full().flex_none().border_r(1).border_color(c.rule).overflow_y_scrollbar()
      .when(!!this.error, (e) => e.child(div().p(10).child(trouble(c, "backtests-load", this.errorR, this.error, (cx) => this.loadBacktests(cx)))))
      .when(this.backtests === null && !this.error, (e) => e.child(div().child(Skeleton(c, 8))))
      .when(Array.isArray(this.backtests) && !this.backtests.length, (e) => e.child(v_flex().p(12).gap(8)
        .child(div().text_size(12.5).text_color(c.grey500).child("No backtests yet."))
        // Results from your own system compare the same way (captain-rankings).
        .child(Button(c, "empty-rankings", "Compare your own rankings", () => openOverlay({ mod: "captain-rankings", kind: "dialog", width: 760, height: 660 }), { small: true }))))
      .children((this.backtests ?? []).map((b) => {
        const active = this.sel?.backtest === b.name;
        return v_flex().w_full().flex_none().border_b(1).border_color(c.rule)
          .child(h_flex().id(`bt-${b.name}`).h(40).px(10).gap(8).items_center().cursor_pointer()
            .border_l(2).border_color(active ? c.blue : "#00000000").bg(active ? c.blueLight : c.surface).hover((s) => (active ? s : s.bg(c.surface2)))
            .tooltip(`${b.name}: ${(b.arms ?? []).map(bare).join(" vs ")}`)
            .on_click((_e, cx) => this.open(cx, b.name, b.latest))
            .child(this.icon("backtest", 14, active ? c.blue : c.grey500))
            .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_family(MONO).text_color(active ? c.blue : c.navy).child(b.name))
            .child(div().flex_none().text_size(11).text_color(c.grey500).child(`v${b.latest}`)))
          .when(active && b.latest > 1, (e) => e.child(h_flex().flex_wrap().gap(4).px(10).pb(8).children(Array.from({ length: b.latest }, (_, k) => b.latest - k).map((v) =>
            div().id(`v-${b.name}-${v}`).h(22).px(6).flex().items_center().cursor_pointer().border_1().text_size(11).font_family(MONO)
              .border_color(this.sel.version === v ? c.blue : c.grey300).text_color(this.sel.version === v ? c.blue : c.navy)
              .on_click((_e, cx) => this.open(cx, b.name, v)).child(`v${v}`)))));
      }));
  }

  /** What limits this result. Always above any winner. */
  warnings(c) {
    const w = this.compat?.warnings ?? [];
    if (!w.length) return div();
    return v_flex().flex_none().mx(20).mt(12).p(10).gap(4).border_1().border_color(this.compat.trustworthy ? c.grey300 : c.fail).bg(this.compat.trustworthy ? c.surface2 : c.surface)
      .child(h_flex().gap(6).items_center().text_size(12.5).font_medium().text_color(this.compat.trustworthy ? c.navy : c.fail)
        .child(this.icon("alert", 14)).child(div().child(this.compat.trustworthy ? "Read with care" : "Not a trustworthy comparison")))
      .children(w.map((x) => h_flex().id(`warn-${x.code}`).gap(8).items_start().text_size(12).text_color(c.grey900)
        .child(div().flex_none().pt(2).child(svg(kitIcon(warningIcon(x.code))).size(13).text_color(this.compat.trustworthy ? c.grey500 : c.fail)))
        .child(div().flex_1().min_w(0).child(sentence(x.text)))));
  }

  overview(c, w) {
    const r = this.run;
    const cmps = r.comparisons ?? [];
    const p = cmps.find((x) => x.primary) ?? cmps[0];
    const cost = p?.cost;
    return v_flex().id("overview").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(14).gap(14)
      .child(h_flex().gap(8)
        .child(Button(c, "run-agent", "Open an agent in this space", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, runPrompt(this.runRef(), r, this.compat))), { small: true }))
        .child(Button(c, "run-prompt", "Copy prompt", (_e, cx) => copyPrompt(cx, runPrompt(this.runRef(), r, this.compat)), { kind: "quiet", small: true })))
      .when(!!p, (e) => e.child(h_flex().flex_wrap().gap(10).items_center().p(12).border_1().border_color(c.rule)
        .child(this.icon(p.delta > 0 ? "up" : p.delta < 0 ? "down" : "flat", 18, p.verdict === "win" ? c.blue : p.verdict === "loss" ? c.fail : c.grey500))
        .child(div().id("primary-metric").text_size(13).font_medium().tooltip(p.metric).child(`${metricWords(p.metric)}, B minus A`))
        .child(div().text_size(15).font_family(MONO).child(fmtD(p.delta)))
        .child(div().text_size(12).font_family(MONO).text_color(c.grey900).child(`[${fmtD(p.ci_low)}, ${fmtD(p.ci_high)}]`))
        .child(this.badge(c, p.verdict, p.verdict))
        .when(!!p.evidence, (e) => e.child(this.badge(c, p.evidence)))))
      .when(!!cost, (e) => e.child(h_flex().flex_wrap().gap(14).items_center().text_size(12.5)
        .child(this.icon("cost", 14, c.navy))
        .child(this.badge(c, cost.flag ? cost.flag.replace(/-/g, " ") : "add a cost", cost.flag === "retire" ? "fail" : cost.flag ? "blue" : null))
        .when(cost.gain_per_1k_usd != null, (e) => e.child(div().child(`gain per $1k a month ${fmtD(cost.gain_per_1k_usd)}`)))
        .when(cost.extra_monthly_usd != null, (e) => e.child(div().text_color(c.grey900).child(`extra ${usd(cost.extra_monthly_usd, 0)} a month`)))
        .when(cost.extra_serve_usd_per_1k != null, (e) => e.child(div().text_color(c.grey900).child(`B costs ${usd(Math.abs(cost.extra_serve_usd_per_1k))} per 1k ${cost.extra_serve_usd_per_1k < 0 ? "less" : "more"}`)))
        .when(!!cost.needs, (e) => e.child(div().text_color(c.grey500).child(cost.needs)))))
      .when(r.mode === "imported", (e) => e.child(this.importedCard(c, r)))
      .when(r.mode !== "imported", (e) => e.child(this.armTable(c, r)))
      .child(div().text_size(13).font_medium().child("Every comparison"))
      .child(div().id("cmps-x").w_full().overflow_x_scrollbar().child(v_flex().min_w(520).border_1().border_color(c.rule)
        .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
          .child(div().flex_1().min_w(160).px(10).child("Metric")).child(div().w(90).flex_none().px(8).text_right().child("Delta"))
          .child(div().w(170).flex_none().px(8).text_right().child("95% CI")).child(div().w(90).flex_none().px(8).text_right().child("Verdict")))
        .children(cmps.map((x, i) => h_flex().id(`cmp-${i}`).h(30).items_center().border_b(1).border_color(c.rule).text_size(12.5).whitespace_nowrap()
          .child(h_flex().flex_1().min_w(160).px(10).gap(6).items_center()
            .child(this.icon(x.delta > 0 ? "up" : x.delta < 0 ? "down" : "flat", 13, x.verdict === "win" ? c.blue : x.verdict === "loss" ? c.fail : c.grey500))
            .child(div().id(`metric-${i}`).min_w(0).truncate().tooltip(x.metric).child(metricWords(x.metric))).when(!!x.primary, (e) => e.child(div().text_size(10.5).text_color(c.blue).child("primary"))))
          .child(div().w(90).flex_none().px(8).text_right().font_family(MONO).child(fmtD(x.delta)))
          .child(div().w(170).flex_none().px(8).text_right().font_family(MONO).text_color(c.grey900).child(`[${fmtD(x.ci_low)}, ${fmtD(x.ci_high)}]`))
          .child(h_flex().w(90).flex_none().px(8).justify_end().child(this.badge(c, x.verdict, x.verdict)))))))
      .child(this.histogram(c, w - 40));
  }

  /** An imported run: only its readout came in. Where from, and which arms, without empty metric columns. */
  importedCard(c, r) {
    return v_flex().gap(8).p(12).border_1().border_color(c.rule).bg(c.surface2)
      .child(h_flex().gap(6).items_center().text_size(13).font_medium().child(this.icon("backtest", 14)).child(div().child("Imported from a readout")))
      .child(div().text_size(12.5).text_color(c.grey900).child("Captain kept the readout's intervals and verdicts. Per-query results weren't part of it."))
      .when(!!r.source, (e) => e.child(h_flex().id("imp-source").gap(8).text_size(12).child(div().flex_none().w(60).text_color(c.grey500).child("Source"))
        .child(div().flex_1().min_w(0).truncate().font_family(MONO).child(r.source)).tooltip(r.source)))
      .children((r.arms ?? []).map((a, i) => h_flex().gap(8).items_center().text_size(12.5)
        .child(div().flex_none().w(60).text_color(c.grey500).child(i === 0 ? "A" : "B"))
        .child(named(c, a.arm))));
  }

  /** What there is to see on a tab an imported run can't fill. */
  importedEmpty(c, what) {
    return v_flex().flex_1().items_center().justify_center().gap(8).p(24)
      .child(this.icon("backtest", 20, c.grey500))
      .child(div().text_size(13.5).font_medium().child(`No ${what} for this run`))
      .child(div().max_w(420).text_center().text_size(12.5).text_color(c.grey900)
        .child(`This run was imported from a readout, so ${what} aren't available. The Overview has its intervals and verdicts.`))
      .child(Button(c, "imp-overview", "Go to Overview", (_e, cx) => { this.tab = "overview"; cx.notify(); }, { small: true }));
  }

  armTable(c, r) {
    const metrics = ["ndcg@10", "p@10", "mrr"].filter((m) => (r.arms ?? []).some((a) => a.offline?.[m] !== undefined));
    const width = 200 + metrics.length * 104 + 84 + 96 + 110;
    return div().id("arms-x").w_full().overflow_x_scrollbar().child(v_flex().min_w(width).border_1().border_color(c.rule)
      .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
        .child(h_flex().flex_1().min_w(200).px(10).gap(6).child(this.icon("arms", 13, c.grey500)).child(div().child("Arm")))
        .children(metrics.map((m) => div().id(`armh-${m}`).w(104).flex_none().px(8).text_right().truncate().tooltip(m).child(metricWords(m))))
        .child(div().w(84).flex_none().px(8).text_right().child("judged@10"))
        .child(div().w(96).flex_none().px(8).text_right().child("per 1k"))
        .child(div().w(110).flex_none().px(8).text_right().child("p50 / p99 ms")))
      .children((r.arms ?? []).map((a, i) => h_flex().id(`arm-${i}`).h(30).items_center().border_b(1).border_color(c.rule).text_size(12.5).whitespace_nowrap()
        .child(h_flex().flex_1().min_w(200).px(10).gap(6).child(div().text_color(c.grey500).child(String.fromCharCode(65 + i))).child(named(c, a.arm)))
        .children(metrics.map((m) => div().w(104).flex_none().px(8).text_right().font_family(MONO).child(fmt4(a.offline?.[m]))))
        .child(div().w(84).flex_none().px(8).text_right().font_family(MONO).text_color((a.judged_at_10 ?? 1) < 0.9 ? c.fail : c.navy).child(pct(a.judged_at_10)))
        .child(div().w(96).flex_none().px(8).text_right().truncate().font_family(MONO).text_color(a.cost?.serve_usd_per_1k != null ? c.navy : c.grey500).child(usd(a.cost?.serve_usd_per_1k) ?? "none"))
        .child(div().w(110).flex_none().px(8).text_right().truncate().font_family(MONO).text_color(c.grey900)
          .child(a.cost?.p50_ms != null ? `${Number(a.cost.p50_ms).toFixed(a.cost.p50_ms < 1 ? 2 : 0)} / ${Number(a.cost.p99_ms ?? 0).toFixed(a.cost.p99_ms < 1 ? 2 : 0)}` : "")))));
  }

  /** The per-query delta distribution, from the backend's bins: bars around zero. */
  histogram(c, w) {
    const h = this.hist;
    if (!h?.counts?.length) return div();
    const max = Math.max(1, ...h.counts);
    const H = 110;
    const zero = h.edges.findIndex((e, i) => e <= 0 && (h.edges[i + 1] ?? Infinity) > 0);
    return v_flex().id("histogram").gap(6)
      .child(h_flex().gap(6).items_center().child(this.icon("histogram", 14, c.navy)).child(div().text_size(13).font_medium().child(`Per-query change, ${h.n} queries`)))
      .child(h_flex().w(Math.max(240, Math.min(w, 760))).h(H).items_end().gap(2).border_b(1).border_color(c.grey300)
        .children(h.counts.map((n, i) => {
          const mid = (h.edges[i] + h.edges[i + 1]) / 2;
          return div().id(`bin-${i}`).flex_1().h(Math.max(n ? 2 : 0, (n / max) * (H - 4)))
            .bg(i === zero ? c.grey500 : mid > 0 ? c.blue : c.fail).opacity(i === zero ? 0.6 : 1)
            .tooltip(`${fmtD(h.edges[i])} to ${fmtD(h.edges[i + 1])}: ${n} queries`);
        })))
      .child(h_flex().w(Math.max(240, Math.min(w, 760))).text_size(10.5).font_family(MONO).text_color(c.grey500)
        .child(div().child(fmtD(h.edges[0]))).child(div().flex_1()).child(div().child("B worse | B better")).child(div().flex_1()).child(div().child(fmtD(h.edges.at(-1)))));
  }

  // ---- queries ------------------------------------------------------------------------------------------------

  queryPane(c, w) {
    const q = this.q;
    const side = this.side;
    // Narrow (beside a terminal): the query opens in place of the list, and its close button brings the list back.
    const narrow = !!side && w < 980;
    const listW = side ? Math.max(360, Math.floor(w * 0.5)) : w;
    const chip = (id, label, n) => h_flex().id(`only-${id ?? "all"}`).h(26).px(8).gap(6).items_center().cursor_pointer().text_size(12)
      .border_1().border_color(q.only === id ? c.blue : c.grey300).bg(q.only === id ? c.blueLight : c.surface).text_color(q.only === id ? c.blue : c.navy)
      .on_click((_e, cx) => this.queries(cx, { only: id }))
      .when(id === "regressions", (e) => e.child(this.icon("down", 12, c.fail))).when(id === "improvements", (e) => e.child(this.icon("up", 12, c.blue)))
      .child(div().child(label)).when(n !== undefined, (e) => e.child(div().font_family(MONO).text_size(10.5).text_color(c.grey500).child(String(n))));
    const counts = q.counts ?? {};
    const all = (counts.regressions ?? 0) + (counts.improvements ?? 0) + (counts.unchanged ?? 0);
    const cols = [["query", "Query", 0], ["class", "Class", 96], ["ndcg_a", "A nDCG", 74], ["ndcg_b", "B nDCG", 74], ["delta", "Delta", 78], ["overlap", "Overlap", 70]];
    const sortable = new Set(["query", "ndcg_a", "ndcg_b", "delta", "overlap"]);
    const head = h_flex().h(30).flex_none().bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium()
      .children(cols.map(([k, label, wd]) => {
        const on = q.sort === k;
        return h_flex().id(`qcol-${k}`).h_full().px(8).gap(4).items_center().when(!wd, (e) => e.flex_1().min_w(0)).when(!!wd, (e) => e.w(wd).flex_none().justify_end())
          .when(sortable.has(k), (e) => e.cursor_pointer().hover((s) => s.text_color(c.blue)).tooltip(`Sort by ${label}`)
            .on_click((_e, cx) => this.queries(cx, on ? { desc: !q.desc } : { sort: k, desc: k !== "delta" && k !== "query" })))
          .child(div().truncate().text_color(on ? c.blue : c.navy).child(label))
          .when(on, (e) => e.child(this.icon(q.desc ? "moved-down" : "moved-up", 11, c.blue)));
      }));
    const total = q.total ?? 0;
    const rowAt = (i) => q.pages.get(Math.floor(i / PAGE))?.[i % PAGE];
    const list = v_virtual_list("compare-queries", q.total === null ? 30 : total, ROW, (i) => `${q.seq}:${i}`, (range) => {
      q.want = [range.start, range.end];
      const out = [];
      for (let i = range.start; i < range.end; i++) {
        const r = rowAt(i);
        if (!r) { out.push(h_flex().h(ROW).px(8).items_center().when(q.total !== 0, (e) => e.child(div().h(10).w("40%").bg(c.surface2)))); continue; }
        const on = side?.row?.query_id === r.query_id;
        out.push(h_flex().id(`q-${r.query_id}`).h(ROW).border_b(1).border_color(c.rule).text_size(12.5).items_center().cursor_pointer()
          .bg(on ? c.blueLight : c.surface).hover((s) => (on ? s : s.bg(c.surface2)))
          .child(h_flex().flex_1().min_w(0).px(8).gap(6).items_center()
            .child(this.icon(r.delta > 0 ? "up" : r.delta < 0 ? "down" : "flat", 12, r.delta > 0 ? c.blue : r.delta < 0 ? c.fail : c.grey500))
            .child(div().min_w(0).truncate().child(r.query)).tooltip(r.query))
          .child(div().w(96).flex_none().px(8).truncate().text_size(11.5).text_color(c.grey900).child(r.class ?? ""))
          .child(div().w(74).flex_none().px(8).text_right().font_family(MONO).child(fmt4(r.ndcg_a)))
          .child(div().w(74).flex_none().px(8).text_right().font_family(MONO).child(fmt4(r.ndcg_b)))
          .child(div().w(78).flex_none().px(8).text_right().font_family(MONO).text_color(r.delta > 0 ? c.blue : r.delta < 0 ? c.fail : c.grey500).child(fmtD(r.delta)))
          .child(div().w(70).flex_none().px(8).text_right().font_family(MONO).text_color(c.grey900).child(r.overlap10 === null || r.overlap10 === undefined ? "" : `${r.overlap10}/10`)));
      }
      return out;
    });
    return v_flex().flex_1().min_h(0)
      .child(h_flex().flex_none().flex_wrap().gap(6).px(20).py(8).items_center()
        .child(chip(null, "All", all || undefined)).child(chip("regressions", "Regressions", counts.regressions)).child(chip("improvements", "Improvements", counts.improvements)).child(chip("unchanged", "Unchanged", counts.unchanged))
        .child(h_flex().id("q-search").w(220).h(28).px(8).gap(6).items_center().border_1().border_color(c.grey300)
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
          .child(this.icon("search", 13, c.grey500)).child(div().flex_1().min_w(0).child(Input.new(this.qSearch).w_full().text_size(12).text_color(c.navy))))
        .child(div().flex_1())
        .child(div().text_size(12).text_color(c.grey900).child(q.total === null ? "" : `${int(total)} of ${int(q.unfiltered ?? total)} queries`)))
      .when(!!q.error, (e) => e.child(div().px(20).text_size(12.5).text_color(c.fail).child(q.error)))
      .child(h_flex().flex_1().min_h(0).px(20).pb(10).gap(12)
        .when(!narrow, (e) => e.child(v_flex().w(side ? listW : "100%").flex_none().when(!side, (e) => e.flex_1()).min_w(0).h_full().border_1().border_color(c.rule)
          .child(head)
          .child(div().flex_1().min_h(0).relative()
            // A click on a row opens it side by side. Registered on the list: the app refuses on_click inside an item renderer.
            .child(list.size_full().on_item_click((key, cx) => { const r = rowAt(Number(String(key).split(":")[1])); if (r) this.openSide(cx, r); }))
            .child(Scrollbar.vertical("compare-queries").viewport_from_layout().absolute().inset_0()))))
        .when(!!side, (e) => e.child(this.sideBySide(c, side))));
  }

  /** One query, both arms' ranked lists: grade chips, how each item moved, new and dropped. */
  sideBySide(c, side) {
    const d = side.data;
    const column = (arm, items, other, label) => v_flex().flex_1().min_w(0).border_1().border_color(c.rule)
      .child(h_flex().h(32).flex_none().px(10).gap(6).items_center().bg(c.surface2).border_b(1).border_color(c.rule).text_size(12.5)
        .child(div().font_medium().child(label)).child(named(c, arm.arm, { color: c.grey900 }))
        .child(div().flex_1()).child(div().font_family(MONO).text_size(11.5).child(`nDCG ${fmt4(arm.ndcg10)}`)))
      .when(!items.length, (e) => e.child(div().p(10).text_size(12).text_color(c.grey500).child("No results.")))
      .children(items.map((it) => {
        // Up means it ranks higher under B than under A, whichever column it is in.
        const up = it.other_rank === null || it.other_rank === undefined ? null : label === "B" ? it.other_rank - it.rank : it.rank - it.other_rank;
        const mark = it.other_rank === null || it.other_rank === undefined ? (label === "B" ? "new" : "dropped") : null;
        return h_flex().id(`${label}-${it.rank}`).h(30).flex_none().px(8).gap(6).items_center().border_b(1).border_color(c.rule).text_size(12)
          .tooltip(`${it.title ?? it.product_id}${it.brand ? `, ${it.brand}` : ""}${it.price != null ? `, $${Number(it.price).toFixed(2)}` : ""}`)
          .child(div().w(22).flex_none().text_right().font_family(MONO).text_color(c.grey500).child(String(it.rank)))
          .child(this.gradeChip(c, it.grade))
          .child(div().flex_1().min_w(0).truncate().child(it.title ?? it.product_id))
          .child(h_flex().w(66).flex_none().gap(3).justify_end().items_center()
            .when(mark === "new", (e) => e.child(this.icon("new", 12, c.blue)).child(div().text_size(11).text_color(c.blue).child("new")))
            .when(mark === "dropped", (e) => e.child(this.icon("dropped", 12, c.fail)).child(div().text_size(11).text_color(c.fail).child("dropped")))
            .when(up !== null && up > 0, (e) => e.child(this.icon("moved-up", 12, c.blue)).child(div().text_size(11).font_family(MONO).text_color(c.blue).child(String(up))))
            .when(up !== null && up < 0, (e) => e.child(this.icon("moved-down", 12, c.fail)).child(div().text_size(11).font_family(MONO).text_color(c.fail).child(String(-up))))
            .when(up === 0, (e) => e.child(this.icon("flat", 12, c.grey500))));
      }));
    return v_flex().id("side-by-side").flex_1().min_w(0).h_full().gap(8)
      .child(h_flex().flex_none().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(14).font_medium().child(side.row.query))
        .when(!!d && !d.error, (e) => e
          // Stage by stage, where this query's results changed (captain-explain, the run's own explain route).
          .child(Button(c, "q-explain", "Explain why", () => openOverlay({ mod: "captain-explain", kind: "dialog", width: 0.9, height: 0.9, props: { run: this.runRef(), query_id: side.row.query_id } }), { kind: "primary", small: true }))
          .child(Button(c, "q-agent", "Ask an agent why", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, queryPrompt(this.runRef(), side.row, d))), { small: true }))
          .child(Button(c, "q-prompt", "Copy prompt", (_e, cx) => copyPrompt(cx, queryPrompt(this.runRef(), side.row, d)), { kind: "quiet", small: true })))
        .child(div().id("side-close").cursor_pointer().text_color(c.grey500).hover((s) => s.text_color(c.navy)).tooltip("Close")
          .on_click((_e, cx) => { this.side = null; cx.notify(); }).child(this.icon("x", 14))))
      .when(!d, (e) => e.child(Skeleton(c, 6)))
      .when(!!d?.error, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(d.error)))
      .when(!!d && !d.error, (e) => e
        .child(h_flex().flex_1().min_h(0).gap(8)
          .child(v_flex().id("side-a").flex_1().min_w(0).h_full().overflow_y_scrollbar().child(column(d.a, d.a.items ?? [], d.b, "A")))
          .child(v_flex().id("side-b").flex_1().min_w(0).h_full().overflow_y_scrollbar().child(column(d.b, d.b.items ?? [], d.a, "B"))))
        .child(div().flex_none().text_size(11.5).text_color(c.grey500).child([`${(d.new ?? []).length} new in B, ${(d.dropped ?? []).length} dropped from A`, ...(d.notes ?? [])].join(". "))));
  }

  // ---- segments and surfaces ----------------------------------------------------------------------------------

  segments(c) {
    const rows = this.run.segments ?? [];
    const kinds = [["class", "By query class"], ["traffic", "By traffic: head, torso and tail"]];
    return v_flex().id("segments").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(14).gap(14)
      .when(!rows.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("This run has no segments.")))
      .children(kinds.map(([kind, title]) => {
        const segs = [...new Set(rows.filter((r) => r.kind === kind).map((r) => r.segment))];
        if (!segs.length) return kind === "traffic" && rows.length ? div().text_size(12).text_color(c.grey500).child("No traffic segments: the queries carry no weights.") : div();
        const ds = rows.filter((r) => r.kind === kind && r.delta !== null);
        const R = Math.max(0.01, ...ds.map((r) => Math.max(Math.abs(r.ci_low ?? 0), Math.abs(r.ci_high ?? 0))));
        return v_flex().border_1().border_color(c.rule)
          .child(h_flex().h(32).px(10).gap(6).items_center().bg(c.surface2).border_b(1).border_color(c.rule).child(this.icon("segments", 13, c.navy)).child(div().text_size(12.5).font_medium().child(title)))
          .children(segs.map((seg) => {
            const rs = rows.filter((r) => r.kind === kind && r.segment === seg);
            const d = rs.find((r) => r.delta !== null) ?? null;
            const few = (rs[0]?.n ?? 0) < 5;
            const x = (v) => `${((Math.max(-R, Math.min(R, v)) + R) / (2 * R)) * 100}%`;
            return h_flex().id(`seg-${kind}-${seg}`).h(32).px(10).gap(8).items_center().border_b(1).border_color(c.rule).text_size(12.5)
              .when(few, (e) => e.opacity(0.55))
              .child(div().w(150).flex_none().truncate().child(seg))
              .child(div().w(50).flex_none().text_right().font_family(MONO).text_color(c.grey900).child(String(rs[0]?.n ?? 0)))
              .child(div().w(80).flex_none().text_right().font_family(MONO).child(few ? "" : fmtD(d?.delta)))
              .child(div().flex_1().min_w(80).h(12).relative()
                .when(!few && d && d.ci_low !== null, (e) => e
                  .child(div().absolute().top(0).bottom(0).left("50%").w(1).bg(c.grey300))
                  .child(div().absolute().top(5).h(2).left(x(d.ci_low)).w(`${((Math.min(R, d.ci_high) - Math.max(-R, d.ci_low)) / (2 * R)) * 100}%`).bg(d.verdict === "win" ? c.blue : d.verdict === "loss" ? c.fail : c.grey500))))
              .child(h_flex().w(90).flex_none().justify_end().child(few ? div().text_size(11.5).text_color(c.grey500).child("too few") : d?.verdict ? this.badge(c, d.verdict, d.verdict) : div()));
          }));
      }));
  }

  surfaces(c) {
    const ss = this.run.surfaces ?? [];
    return v_flex().id("surfaces").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(14).gap(12)
      .when(!ss.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("This run has no surfaces. Add one, with its guardrail, when you plan a backtest.")))
      .children(ss.map((s) => {
        const failed = (s.guardrails ?? []).filter((g) => !g.ok).length;
        return v_flex().id(`surface-${s.surface}`).border_1().border_color(failed ? c.fail : c.rule)
          .child(h_flex().h(34).px(10).gap(8).items_center().border_b(1).border_color(c.rule)
            .child(this.icon("surfaces", 14, c.navy)).child(div().flex_1().min_w(0).truncate().font_family(MONO).text_size(13).font_medium().child(s.surface))
            .child(failed ? this.badge(c, `${failed} guardrail${failed === 1 ? "" : "s"} failed`, "fail") : this.badge(c, "guardrails ok", "blue")))
          .child(div().px(10).py(6).text_size(12).text_color(c.grey900).child(`queries ${s.queries}, judgments ${s.judgments}`))
          .children((s.comparisons ?? []).map((x, i) => h_flex().id(`scmp-${s.surface}-${i}`).px(10).py(4).gap(8).items_center().text_size(12.5)
            .child(div().id(`scmp-name-${s.surface}-${i}`).tooltip(x.metric).child(metricWords(x.metric))).child(div().font_family(MONO).child(fmtD(x.delta)))
            .child(div().font_family(MONO).text_color(c.grey900).child(`[${fmtD(x.ci_low)}, ${fmtD(x.ci_high)}]`)).child(this.badge(c, x.verdict, x.verdict))))
          .children((s.guardrails ?? []).map((g, i) => h_flex().id(`guard-${s.surface}-${i}`).px(10).py(5).gap(8).items_center().border_t(1).border_color(c.rule).text_size(12.5)
            .child(this.icon(g.ok ? "ok" : "failed", 14, g.ok ? c.blue : c.fail))
            .child(div().flex_1().min_w(0).truncate().text_color(g.ok ? c.navy : c.fail).child(`${g.ok ? "ok" : "failed"}: ${g.rule}`))
            .child(div().flex_none().font_family(MONO).text_color(c.grey900).child(`${g.arm} ${g.value === null ? "" : Number(g.value).toFixed(4)}`))));
      }));
  }

  render() {
    const c = palette(isDark(theme()));
    const w = this.width;
    const pw = Math.max(200, Math.min(280, Math.floor(w * 0.22)));
    const r = this.run;
    // Never blank: the header always shows, and the body says what is happening.
    const body = this.error ? v_flex().flex_1() : this.starting ? v_flex().flex_1().items_center().justify_center().child(Loading(c, "", true)) : !this.sel ? (this.backtests?.length === 0 ? v_flex().flex_1().items_center().justify_center().gap(10).p(24)
        .child(this.icon("compare", 22, c.grey500))
        .child(div().text_size(15).font_medium().child("Nothing to compare yet"))
        .child(div().max_w(460).text_center().text_size(12.5).line_height("19px").text_color(c.grey900)
          .child("A comparison shows whether one version of your search really beats another: the difference, how sure Captain is, and query by query why. Run a backtest to get one."))
        .child(h_flex().gap(8)
          .child(Button(c, "first-backtest", "Run a backtest", () => openForm("backtest"), { kind: "primary" }))
          .child(Button(c, "first-demo", "Try the demo store", () => openForm("demo"))))
        : v_flex().flex_1().child(Skeleton(c, 8)))
      : !r ? v_flex().flex_1().child(Skeleton(c, 8))
      : r.error ? v_flex().flex_1().p(24).child(v_flex().gap(10).p(14).bg(c.failLight).border_1().border_color(c.fail)
          .child(div().text_size(13).text_color(c.fail).child(r.error))
          .child(h_flex().gap(8)
            .child(Button(c, "run-retry", "Retry", (_e, cx) => this.open(cx, this.sel.backtest, this.sel.version), { kind: "primary", small: true }))
            .child(Button(c, "run-copy", "Copy details", (_e, cx) => cx.write_to_clipboard(details(this.runR?.error ?? null)), { small: true }))))
      : this.tab === "overview" ? this.overview(c, w - pw)
      : this.tab === "profile" ? this.profiler.render(c, w - pw)
      : r.mode === "imported" ? this.importedEmpty(c, this.tab === "queries" ? "per-query results" : this.tab === "segments" ? "segment results" : "surface results")
      : this.tab === "queries" ? this.queryPane(c, w - pw - 40)
      : this.tab === "segments" ? this.segments(c)
      : this.surfaces(c);
    return v_flex().id("root").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      .on_size_change((size, cx) => { this.width = size.width; cx.notify(); })
      .child(this.header(c))
      .child(h_flex().flex_1().min_h(0)
        .child(this.picker(c, pw))
        .child(v_flex().flex_1().min_w(0).h_full().child(this.warnings(c)).child(body)));
  }
}
