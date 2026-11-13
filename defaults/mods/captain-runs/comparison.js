// CAPTAIN: one evaluation run, read in depth, in place in Runs (Edgar, 2026-11-12: "The compare menu can be gotten
// rid of... It should be under runs if anything, with all these comparisons showing you whether it's a verdict of
// win, noise, or bad"). This was the Compare screen. It is one page now, no tabs:
//   what limits the result (compatibility warnings, always above the verdict), the verdict in one of three words
//   and one sentence (kit/verdict.js), the arms with their cost, every measure, and the per-query change. Then four
//   sections that open in place and load when opened: query by query (paged and sorted by the services, with any
//   query side by side), by slice, by surface, and where each arm's time goes (profile.js).
//
// Per-query rows come in pages of 200 from GET /api/runs/{run}/queries; the histogram comes pre-binned. The mod never
// holds or sorts a whole result set (docs/captain/RISKS.md).
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, v_virtual_list, Scrollbar, InputState, Input } from "gpui-base";
import { openOverlay } from "herdr";
import { api, details } from "./kit/cst.js";
import { SendToAgent } from "./kit/context.js";
import { MONO } from "./kit/tokens.js";
import { Skeleton, Button } from "./kit/ui.js";
import { askAgent, copyPrompt } from "./kit/agent.js";
import { metricWords, warningIcon } from "./kit/words.js";
import { runFacts, runVerdict, metricAt, VERDICT_WORDS } from "./kit/verdict.js";
import { icon as kitIcon } from "./kit/icons.js";
import { int } from "./kit/num.js";
import { Profiler } from "./profile.js";

const PAGE = 200;
const ROW = 28;
const ICON = (n) => `mods/captain-runs/icons/${n}.svg`;
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
    `In Captain, in the offline evaluation run ${run} (cst backtest), look at the query "${row.query || d?.query || row.query_id}".`,
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
    `In Captain, look at the offline evaluation run ${ref} (cst backtest)${r.mode === "imported" ? ", imported from a readout" : ""}: it compares ${(r.arms ?? []).map((a) => bare(a.arm)).join(" against ")}.`,
    p ? `The main result is ${metricWords(p.metric)}: B minus A is ${p.delta}, 95% interval [${p.ci_low}, ${p.ci_high}], verdict ${p.verdict}.` : "",
    (compat?.warnings ?? []).length ? `Captain warns: ${compat.warnings.map((w) => w.text).join("; ")}.` : "",
    "Tell me whether to trust it, what would make it conclusive, and what to try next.",
    "Use the cst CLI to read the run. Change nothing without asking me.",
  ].filter(Boolean).join(" ");
}

/** The sections under the numbers: each opens in place, and loads only then. */
export const SECTIONS = [["queries", "Query by query"], ["segments", "By slice"], ["surfaces", "By surface"], ["profile", "Where the time goes"]];
/** The Compare screen's tab names, which still open the matching section (`cst app open comparison:<name> --tab queries`). */
const EARLIER = { overview: null, queries: "queries", segments: "segments", slices: "segments", surfaces: "surfaces", profile: "profile" };

export class Comparison {
  /** host: the Runs view (its evaluation, for the verdict: host.detail.backtest). */
  constructor(host) {
    this.host = host;
    this.autoProfile = false; // start a profile as soon as the run opens
    this.sel = null; // {backtest, version}
    this.run = null;
    this.compat = null;
    this.hist = null;
    this.q = this.freshQueries();
    this.side = null;
    this.sections = new Set(); // the sections that are open
    this.qSearch = InputState.new({ placeholder: "Search queries" });
    this.qSearch.on("submit", (_e, cx) => this.queries(cx, { q: this.qSearch.value().trim() }));
    this.profiler = new Profiler(this);
  }

  /** Open a section by its name, or by the Compare screen's earlier tab name. */
  show(cx, name) {
    const s = EARLIER[name] ?? (SECTIONS.some(([id]) => id === name) ? name : null);
    if (s) this.sections.add(s);
    cx.notify();
  }

  toggle(cx, id) {
    if (this.sections.has(id)) this.sections.delete(id); else this.sections.add(id);
    cx.notify();
  }

  freshQueries(over = {}) {
    return { pages: new Map(), inflight: null, total: null, counts: null, want: [0, 0], seq: (this.q?.seq ?? 0) + 1, sort: "delta", desc: false, only: null, q: "", error: null, ...over };
  }

  runRef() {
    return this.sel ? `${this.sel.backtest}@v${this.sel.version}` : null;
  }

  /** Read a run. The same run again keeps what is on screen. */
  open(cx, backtest, version) {
    if (this.sel?.backtest === backtest && this.sel?.version === version && this.run && !this.run.error) return;
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
      // A query to open side by side, asked for with the run (a layout, or the real-window check).
      if (this.wantQuery) {
        const q = this.wantQuery;
        this.wantQuery = null;
        this.sections.add("queries");
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
    const q = this.q;
    if (!this.sections.has("queries") || !this.sel || q.inflight !== null || q.error || this.run?.mode === "imported") return;
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

  /** From the screen's timer: the next page of queries, and a running profile. A run record lost while the
   *  services restarted is asked for again. */
  tick(cx) {
    this.pump(cx);
    if ((this.n = (this.n ?? 0) + 1) % 3 === 0) this.profiler.tick(cx);
    if (this.n % 20 === 0 && this.sel && this.run?.error && !this.runR?.status) { const s = this.sel; this.sel = null; this.open(cx, s.backtest, s.version); }
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

  /** What limits this result. Always above any winner. */
  warnings(c) {
    const w = this.compat?.warnings ?? [];
    if (!w.length) return div();
    return v_flex().id("cmp-warnings").flex_none().p(10).gap(4).border_1().border_color(this.compat.trustworthy ? c.grey300 : c.fail).bg(this.compat.trustworthy ? c.surface2 : c.surface)
      .child(h_flex().gap(6).items_center().text_size(12.5).font_medium().text_color(this.compat.trustworthy ? c.navy : c.fail)
        .child(this.icon("alert", 14)).child(div().child(this.compat.trustworthy ? "Read with care" : "Not a trustworthy comparison")))
      .children(w.map((x) => h_flex().id(`warn-${x.code}`).gap(8).items_start().text_size(12).text_color(c.grey900)
        .child(div().flex_none().pt(2).child(svg(kitIcon(warningIcon(x.code))).size(13).text_color(this.compat.trustworthy ? c.grey500 : c.fail)))
        .child(div().flex_1().min_w(0).child(sentence(x.text)))));
  }

  /** The verdict: one of three words, then one sentence that names both models, the measure, where the evidence
   *  comes from, and anything that changes what the result means (kit/verdict.js). The numbers sit beside it. */
  verdict(c) {
    const r = this.run;
    const f = runFacts(r, this.host.detail?.backtest);
    // The services' own warning says it too, when the evaluation's record does not.
    if ((this.compat?.warnings ?? []).some((w) => w.code === "own-labeler")) f.own_labeler = true;
    const v = runVerdict(f);
    const col = v.tone === "good" ? c.good : v.tone === "bad" ? c.fail : c.grey900;
    return v_flex().id("cmp-verdict").flex_none().p(12).gap(6).border_1().border_color(c.rule)
      .child(h_flex().flex_wrap().gap(10).items_center()
        .when(!!v.word, (e) => e.child(div().id("cmp-verdict-word").flex_none().h(24).px(8).flex().items_center().border_1().border_color(col).text_size(12.5).font_medium().text_color(col).child(v.word)))
        .child(div().id("cmp-verdict-sentence").flex_1().min_w(240).text_size(13).line_height("19px").child(v.sentence)))
      .when(f.delta != null, (e) => e.child(h_flex().flex_wrap().gap(10).items_center().text_size(12).text_color(c.grey900)
        .child(this.icon(f.delta > 0 ? "up" : f.delta < 0 ? "down" : "flat", 14, col))
        .child(div().id("primary-metric").tooltip(f.metric).child(`${metricWords(metricAt(f.metric, f.depths))}, B minus A`))
        .child(div().font_family(MONO).text_color(c.navy).child(fmtD(f.delta)))
        .child(div().font_family(MONO).child(`[${fmtD(f.ci_low)}, ${fmtD(f.ci_high)}]`))));
  }

  /** What to do with a finished comparison: make its model (arm B) the one the others are compared with. */
  nextSteps(c, p) {
    const ev = { SIMULATED: "simulated", LLM_GRADED: "LLM graded", LOGGED_CLICKS: "logged clicks", HUMAN_GRADED: "human graded", REAL_TRAFFIC: "real traffic" }[p.evidence] ?? "not judged";
    // A project's experiment backtests are named <project>/<experiment>.
    const project = /^([a-z0-9][a-z0-9-]*)\/exp-/.test(this.sel?.backtest ?? "") ? this.sel.backtest.split("/")[0] : undefined;
    const form = (task, values) => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task, values } });
    return h_flex().id("next-steps").flex_wrap().gap(8).items_center()
      .child(Button(c, "next-champion", `Make it the baseline (${ev})`, () => form("champion", { pipeline: p.b, evidence: ev, project }), { small: true }))
      .child(Button(c, "run-agent", "Ask an agent about it", (_e, cx) => cx.spawn(async (cx) => askAgent(cx, runPrompt(this.runRef(), this.run, this.compat))), { small: true }))
      .child(Button(c, "run-prompt", "Copy prompt", (_e, cx) => copyPrompt(cx, runPrompt(this.runRef(), this.run, this.compat)), { kind: "quiet", small: true }))
      .when(Boolean(this.sel?.backtest), (e) => e.child(SendToAgent(c, "send-comparison", { ref: `run:${this.sel.backtest}`, kind: "run", label: this.sel.backtest })));
  }

  /** Warnings, verdict, what to do next, cost, the arms, every measure, and the per-query change. */
  numbers(c, w) {
    const r = this.run;
    const cmps = r.comparisons ?? [];
    const p = cmps.find((x) => x.primary) ?? cmps[0];
    const cost = p?.cost;
    return v_flex().id("overview").flex_none().gap(14)
      .child(this.warnings(c))
      .child(this.verdict(c))
      .when(!!p && String(p.b ?? "").includes("@v"), (e) => e.child(this.nextSteps(c, p)))
      .when(!!cost, (e) => e.child(h_flex().flex_wrap().gap(14).items_center().text_size(12.5)
        .child(this.icon("cost", 14, c.navy))
        .child(this.badge(c, cost.flag ? cost.flag.replace(/-/g, " ") : "add a cost", cost.flag === "retire" ? "fail" : cost.flag ? "blue" : null))
        .when(cost.gain_per_1k_usd != null, (e) => e.child(div().child(`gain per $1k a month ${fmtD(cost.gain_per_1k_usd)}`)))
        .when(cost.extra_monthly_usd != null, (e) => e.child(div().text_color(c.grey900).child(`extra ${usd(cost.extra_monthly_usd, 0)} a month`)))
        .when(cost.extra_serve_usd_per_1k != null, (e) => e.child(div().text_color(c.grey900).child(`B costs ${usd(Math.abs(cost.extra_serve_usd_per_1k))} per 1k ${cost.extra_serve_usd_per_1k < 0 ? "less" : "more"}`)))
        .when(!!cost.needs, (e) => e.child(div().text_color(c.grey500).child(cost.needs)))))
      .when(r.mode === "imported", (e) => e.child(this.importedCard(c, r)))
      .when(r.mode !== "imported", (e) => e.child(this.armTable(c, r)))
      .child(div().text_size(13).font_medium().child("Every measure"))
      .child(div().id("cmps-x").w_full().overflow_x_scrollbar().child(v_flex().min_w(600).border_1().border_color(c.rule)
        .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
          .child(div().flex_1().min_w(160).px(10).child("Metric")).child(div().w(90).flex_none().px(8).text_right().child("Delta"))
          .child(div().w(170).flex_none().px(8).text_right().child("95% CI")).child(div().w(170).flex_none().px(8).text_right().child("Verdict")))
        .children(cmps.map((x, i) => h_flex().id(`cmp-${i}`).h(30).items_center().border_b(1).border_color(c.rule).text_size(12.5).whitespace_nowrap()
          .child(h_flex().flex_1().min_w(160).px(10).gap(6).items_center()
            .child(this.icon(x.delta > 0 ? "up" : x.delta < 0 ? "down" : "flat", 13, x.verdict === "win" ? c.blue : x.verdict === "loss" ? c.fail : c.grey500))
            .child(div().id(`metric-${i}`).min_w(0).truncate().tooltip(x.metric).child(metricWords(metricAt(x.metric, r.depths)))).when(!!x.primary, (e) => e.child(div().text_size(10.5).text_color(c.blue).child("primary"))))
          .child(div().w(90).flex_none().px(8).text_right().font_family(MONO).child(fmtD(x.delta)))
          .child(div().w(170).flex_none().px(8).text_right().font_family(MONO).text_color(c.grey900).child(`[${fmtD(x.ci_low)}, ${fmtD(x.ci_high)}]`))
          .child(h_flex().w(170).flex_none().px(8).justify_end().child(div().text_color(x.verdict === "win" ? c.good : x.verdict === "loss" ? c.fail : c.grey900).child(VERDICT_WORDS[x.verdict] ?? x.verdict ?? "")))))))
      .child(this.histogram(c, w));
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

  /** A section an imported run can't fill: it only brought its readout. */
  importedEmpty(c, what) {
    return div().id("imp-empty").px(2).pb(10).text_size(12.5).text_color(c.grey900)
      .child(`This run was imported from a readout, so ${what} aren't available. Its intervals and verdicts are above.`);
  }

  armTable(c, r) {
    const metrics = ["ndcg@10", "p@10", "mrr"].filter((m) => (r.arms ?? []).some((a) => a.offline?.[m] !== undefined));
    const width = 200 + metrics.length * 104 + 84 + 96 + 110;
    return div().id("arms-x").w_full().overflow_x_scrollbar().child(v_flex().min_w(width).border_1().border_color(c.rule)
      .child(h_flex().h(28).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().whitespace_nowrap()
        .child(h_flex().flex_1().min_w(200).px(10).gap(6).child(this.icon("arms", 13, c.grey500)).child(div().child("Arm")))
        .children(metrics.map((m) => div().id(`armh-${m}`).w(104).flex_none().px(8).text_right().truncate().tooltip(m).child(metricWords(metricAt(m, r.depths)))))
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
    // A fixed height inside the page: the list scrolls inside itself, the page around it.
    return v_flex().id("cmp-queries").flex_none().h(side ? 560 : 440)
      .child(h_flex().flex_none().flex_wrap().gap(6).py(8).items_center()
        .child(chip(null, "All", all || undefined)).child(chip("regressions", "Regressions", counts.regressions)).child(chip("improvements", "Improvements", counts.improvements)).child(chip("unchanged", "Unchanged", counts.unchanged))
        .child(h_flex().id("q-search").w(220).h(28).px(8).gap(6).items_center().border_1().border_color(c.grey300)
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
          .child(this.icon("search", 13, c.grey500)).child(div().flex_1().min_w(0).child(Input.new(this.qSearch).w_full().text_size(12).text_color(c.navy))))
        .child(div().flex_1())
        .child(div().text_size(12).text_color(c.grey900).child(q.total === null ? "" : `${int(total)} of ${int(q.unfiltered ?? total)} queries`)))
      .when(!!q.error, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(q.error)))
      .child(h_flex().flex_1().min_h(0).pb(10).gap(12)
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

  // ---- slices and surfaces ------------------------------------------------------------------------------------

  segments(c) {
    const rows = this.run.segments ?? [];
    const kinds = [["class", "By query class"], ["traffic", "By traffic: head, torso and tail"]];
    return v_flex().id("segments").flex_none().pb(10).gap(14)
      .when(!rows.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("This run has no slices.")))
      .children(kinds.map(([kind, title]) => {
        const segs = [...new Set(rows.filter((r) => r.kind === kind).map((r) => r.segment))];
        if (!segs.length) return kind === "traffic" && rows.length ? div().text_size(12).text_color(c.grey500).child("No traffic slices: the queries carry no weights.") : div();
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
              .child(h_flex().w(150).flex_none().justify_end().text_size(12).child(few ? div().text_color(c.grey500).child("Too few queries") : d?.verdict ? div().text_color(d.verdict === "win" ? c.good : d.verdict === "loss" ? c.fail : c.grey900).child(VERDICT_WORDS[d.verdict] ?? d.verdict) : div()));
          }));
      }));
  }

  surfaces(c) {
    const ss = this.run.surfaces ?? [];
    return v_flex().id("surfaces").flex_none().pb(10).gap(12)
      .when(!ss.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("This run has no surfaces. Add one, with its guardrail, when you plan an offline evaluation.")))
      .children(ss.map((s) => {
        const failed = (s.guardrails ?? []).filter((g) => !g.ok).length;
        return v_flex().id(`surface-${s.surface}`).border_1().border_color(failed ? c.fail : c.rule)
          .child(h_flex().h(34).px(10).gap(8).items_center().border_b(1).border_color(c.rule)
            .child(this.icon("surfaces", 14, c.navy)).child(div().flex_1().min_w(0).truncate().font_family(MONO).text_size(13).font_medium().child(s.surface))
            .child(failed ? this.badge(c, `${failed} guardrail${failed === 1 ? "" : "s"} failed`, "fail") : this.badge(c, "guardrails ok", "blue")))
          .child(div().px(10).py(6).text_size(12).text_color(c.grey900).child(`queries ${s.queries}, judgments ${s.judgments}`))
          .children((s.comparisons ?? []).map((x, i) => h_flex().id(`scmp-${s.surface}-${i}`).px(10).py(4).gap(8).items_center().text_size(12.5)
            .child(div().id(`scmp-name-${s.surface}-${i}`).tooltip(x.metric).child(metricWords(x.metric))).child(div().font_family(MONO).child(fmtD(x.delta)))
            .child(div().font_family(MONO).text_color(c.grey900).child(`[${fmtD(x.ci_low)}, ${fmtD(x.ci_high)}]`)).child(div().text_size(12).text_color(x.verdict === "win" ? c.good : x.verdict === "loss" ? c.fail : c.grey900).child(VERDICT_WORDS[x.verdict] ?? x.verdict ?? ""))))
          .children((s.guardrails ?? []).map((g, i) => h_flex().id(`guard-${s.surface}-${i}`).px(10).py(5).gap(8).items_center().border_t(1).border_color(c.rule).text_size(12.5)
            .child(this.icon(g.ok ? "ok" : "failed", 14, g.ok ? c.blue : c.fail))
            .child(div().flex_1().min_w(0).truncate().text_color(g.ok ? c.navy : c.fail).child(`${g.ok ? "ok" : "failed"}: ${g.rule}`))
            .child(div().flex_none().font_family(MONO).text_color(c.grey900).child(`${g.arm} ${g.value === null ? "" : Number(g.value).toFixed(4)}`))));
      }));
  }

  /** The whole comparison, as one page for the caller to scroll. `w` is the width it has. */
  render(c, w) {
    const r = this.run;
    if (!this.sel) return div();
    if (!r) return v_flex().id("comparison").flex_none().child(Skeleton(c, 6));
    if (r.error) return v_flex().id("comparison").flex_none().gap(10).p(14).bg(c.failLight).border_1().border_color(c.fail)
      .child(div().text_size(13).text_color(c.fail).child(r.error))
      .child(h_flex().gap(8)
        .child(Button(c, "run-retry", "Retry", (_e, cx) => { const s = this.sel; this.sel = null; this.open(cx, s.backtest, s.version); }, { small: true }))
        .child(Button(c, "run-copy", "Copy details", (_e, cx) => cx.write_to_clipboard(details(this.runR?.error ?? null)), { small: true })));
    const imported = r.mode === "imported";
    const body = (id) => id === "profile" ? this.profiler.render(c, w)
      : imported ? this.importedEmpty(c, id === "queries" ? "per-query results" : id === "segments" ? "slice results" : "surface results")
      : id === "queries" ? this.queryPane(c, w) : id === "segments" ? this.segments(c) : this.surfaces(c);
    // By surface shows only when the run has one: most don't.
    const sections = SECTIONS.filter(([id]) => id !== "surfaces" || (r.surfaces ?? []).length > 0 || this.sections.has("surfaces"));
    return v_flex().id("comparison").flex_none().gap(14)
      .child(this.numbers(c, w))
      .child(v_flex().flex_none().children(sections.map(([id, title]) => {
        const open = this.sections.has(id);
        return v_flex().id(`csec-${id}-wrap`).flex_none().border_t(1).border_color(c.rule)
          .child(h_flex().id(`csec-${id}`).h(40).gap(8).items_center().cursor_pointer().hover((s) => s.bg(c.surface2))
            .on_click((_e, cx) => this.toggle(cx, id))
            .child(this.icon(open ? "chevron-down" : "chevron-right", 12, c.grey500))
            .child(div().text_size(13).font_medium().child(title)))
          .when(open, (e) => e.child(body(id)));
      })));
  }
}
