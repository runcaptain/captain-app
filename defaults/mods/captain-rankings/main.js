// CAPTAIN: Compare your own rankings (spec S2, backend/docs/api.md "Bring your own rankings"). A team's own system's
// ranked results, imported and compared like any backtest:
//   1. The file: dropped from Finder, typed, or a dataset already in Captain. One plain sentence says what it holds;
//      when its column names differ, a mapping row per needed column, picked from the file's own columns.
//   2. The check: POST /api/rankings/validate, run on every change. Stats, errors (red, Import off), warnings (amber),
//      what the data supports, the baseline arm, and where labels come from when there are labels (it sets the
//      evidence label). Optional: a judgments dataset, a catalog for titles, a hypothesis.
//   3. Import: POST /api/rankings/import as a job, a toast, then the new run opens in Comparisons.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, notify, run, setShared } from "herdr";
import { palette, isDark, FONT, MONO, RADIUS } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int } from "./kit/num.js";

const NEEDED = [["query_id", "Query id"], ["arm", "System or version"], ["item_id", "Result id"]];
const RANKING = [["rank", "Rank (1 is the top)"], ["score", "Score (higher is better)"]];
const OPTIONAL = [["query", "Query text"], ["label", "Label, 0 to 3"], ["segment", "Segment"], ["weight", "Weight"]];
const LABELS_FROM = [["human", "People"], ["llm", "An LLM"], ["clicks", "Clicks"], ["heuristic", "A heuristic"]];
const sleep = (cx, ms) => new Promise((r) => cx.timer.after(ms, () => r()));
const stem = (p) => String(p ?? "").split("/").pop().replace(/\.[a-z0-9]+$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

export default class Rankings extends View {
  init(_props, cx) {
    this.from = "file";
    this.path = InputState.new({ placeholder: "e.g. ~/Downloads/rankings.csv" });
    this.name = InputState.new({ placeholder: "e.g. prod-vs-candidate" });
    this.hypothesis = InputState.new({ placeholder: "e.g. The candidate finds more relevant products for long queries." });
    this.path.on("change", (_e, ecx) => { this.schedule(ecx ?? cx); });
    this.name.on("change", () => { this.named = true; this.error = null; });
    this.mapping = {};
    this.dataset = null; this.judgments = null; this.catalog = null;
    this.baseline = null; this.labelsFrom = null;
    this.check = null; this.checking = false; this.busy = false; this.error = null;
    cx.spawn(async (cx) => { const r = await api("/api/datasets"); this.datasets = r.ok ? r.data ?? [] : []; cx.notify(); });
  }

  source() {
    if (this.from === "dataset") return this.dataset ? { dataset: this.dataset } : null;
    const p = this.path.value().trim();
    return p ? { path: p } : null;
  }

  // Check again shortly after the last change (typing a path, picking a column).
  schedule(cx) {
    this.due = Date.now() + 500;
    cx.timer.after(520, (cx) => { if (Date.now() >= this.due) this.validate(cx); });
  }

  validate(cx) {
    const src = this.source();
    if (!src) { this.check = null; cx.notify(); return; }
    const seq = (this.seq = (this.seq ?? 0) + 1);
    this.checking = true; cx.notify();
    cx.spawn(async (cx) => {
      const mapping = Object.fromEntries(Object.entries(this.mapping).filter(([ours, theirs]) => theirs && theirs !== ours));
      const r = await api("/api/rankings/validate", { body: { ...src, ...(Object.keys(mapping).length ? { mapping } : {}), ...(this.judgments ? { judgments: this.judgments } : {}) } });
      if (seq !== this.seq) return;
      this.checking = false;
      this.check = r.ok ? r.data : { ok: false, errors: [plainError(r.error, "Captain couldn't read that file.")], warnings: [], stats: {}, available: {}, columns: [] };
      const arms = this.check.stats?.arms ?? [];
      if (!arms.includes(this.baseline)) this.baseline = arms[0] ?? null;
      if (!this.named && this.name.value().trim() === "") {
        const guess = stem(src.path ?? src.dataset);
        if (guess) this.name.set_value(guess);
      }
      cx.notify();
    });
  }

  canImport() {
    const c = this.check;
    if (!c?.ok || this.busy) return false;
    if (!this.name.value().trim() || !this.baseline) return false;
    if (c.stats?.labels && !this.labelsFrom) return false;
    return true;
  }

  importIt(cx) {
    if (!this.canImport()) return;
    const src = this.source();
    const name = this.name.value().trim();
    const mapping = Object.fromEntries(Object.entries(this.mapping).filter(([ours, theirs]) => theirs && theirs !== ours));
    const body = { name, ...src, baseline: this.baseline,
      ...(Object.keys(mapping).length ? { mapping } : {}), ...(this.check.stats?.labels ? { labels_from: this.labelsFrom } : {}),
      ...(this.judgments ? { judgments: this.judgments } : {}), ...(this.catalog ? { catalog: this.catalog } : {}),
      ...(this.hypothesis.value().trim() ? { hypothesis: this.hypothesis.value().trim() } : {}) };
    this.busy = true; this.error = null; this.progress = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/rankings/import", { body });
      if (!r.ok || !r.data?.job) { this.busy = false; this.error = plainError(r.error, "Captain couldn't start the import."); cx.notify(); return; }
      for (let i = 0; i < 900; i++) {
        const j = await api(`/api/jobs/${encodeURIComponent(r.data.job)}`);
        const st = j.ok ? j.data?.status : null;
        if (st === "done") {
          const res = j.data?.result ?? {};
          const [backtest, v] = String(res.run ?? `${name}@v1`).split("@v");
          notify({ title: `Imported ${name}`, body: "It opens in Compare, like any offline evaluation.", level: "info" });
          setShared("captain.open", { section: "comparisons", backtest: res.backtest ? String(res.backtest).split("@")[0] : backtest, version: Number(v) || 1, at: Date.now() });
          run("layout.select", { stack: "sections", child: "comparisons" });
          this.busy = false; this.done = res;
          closeOverlay(props()?.overlay);
          return;
        }
        if (st === "failed") { this.busy = false; this.error = plainError(j.data?.message ?? j.data?.error, "The import didn't finish."); cx.notify(); return; }
        if (j.ok) { this.progress = j.data?.progress ?? null; cx.notify(); }
        await sleep(cx, 600);
      }
      this.busy = false; this.error = "This took too long."; cx.notify();
    });
  }

  // ---- drawing --------------------------------------------------------------------------------------------

  label(c, text, optional) {
    return h_flex().gap(6).items_center().child(div().text_size(12.5).font_semibold().text_color(c.navy).child(text))
      .when(Boolean(optional), (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("optional")));
  }

  input(c, id, st) {
    return div().id(id).h(32).flex().items_center().px(10).bg(c.surface).border_1().border_color(c.grey300).cursor_text()
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(st).w_full().text_size(13).text_color(c.navy));
  }

  chips(c, id, opts, value, pick, { none } = {}) {
    return h_flex().flex_wrap().gap(6)
      .when(Boolean(none), (e) => e.child(this.chip(c, `${id}-none`, none, value == null, (cx) => pick(null, cx))))
      .children(opts.map(([v, label]) => this.chip(c, `${id}-${v}`, label, value === v, (cx) => pick(v, cx))));
  }

  chip(c, id, label, on, onPick) {
    return div().id(id).h(28).px(10).flex().items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
      .hover((s) => (on ? s : s.border_color(c.blue)))
      .on_click((_e, cx) => { onPick(cx); cx.notify(); }).child(label);
  }

  fileStep(c) {
    const ds = (this.datasets ?? []).map((d) => [d.name, d.name]);
    return v_flex().gap(10)
      .child(this.chips(c, "rk-from", [["file", "A file"], ["dataset", "A dataset in Captain"]], this.from, (v, cx) => { this.from = v; this.check = null; this.schedule(cx); }))
      .when(this.from === "file", (e) => e
        .child(v_flex().id("rk-drop").h(64).items_center().justify_center().gap(4).border_1().border_color(c.grey300).bg(c.surface2)
          .drag_over((s) => s.border_color(c.blue).bg(c.blueLight))
          .on_drop((ev, cx) => { const p = (ev.paths ?? [])[0]; if (p) { this.path.set_value(p); this.validate(cx); } })
          .child(h_flex().gap(8).items_center().child(svg(icon("upload")).size(16).text_color(c.blue))
            .child(div().text_size(13).text_color(c.navy).child("Drop the file here, or type its path below"))))
        .child(this.input(c, "rk-path", this.path)))
      .when(this.from === "dataset", (e) => e.child(!this.datasets ? div().h(28).w(200).bg(c.surface2)
        : ds.length ? v_flex().id("rk-ds").max_h(120).overflow_y_scroll().child(this.chips(c, "rk-ds", ds, this.dataset, (v, cx) => { this.dataset = v; this.validate(cx); }))
        : div().text_size(12.5).text_color(c.grey500).child("No datasets yet.")))
      .child(div().text_size(12.5).line_height("19px").text_color(c.grey900)
        .child("One row per query, system and result, with a rank or a score. Optional: query text, a label from 0 to 3, a segment, a weight. CSV, TSV, Parquet, JSON or JSON lines."));
  }

  // When the file's names differ: a row per needed column, picked from the file's own. The mapping the services take
  // is {our name: their column}, like `cst rankings validate --map query_id=qid`.
  mappingStep(c) {
    const ch = this.check;
    const cols = ch?.columns ?? [];
    const has = (k) => cols.includes(k) || Boolean(this.mapping[k]);
    const needed = NEEDED.some(([k]) => !has(k)) || !RANKING.some(([k]) => has(k));
    if (!cols.length || (!needed && !Object.keys(this.mapping).length)) return null;
    const row = ([k, label], optional) => h_flex().gap(10).items_center()
      .child(div().w(150).flex_none().text_size(12.5).text_color(c.navy).child(label))
      .child(div().flex_1().min_w(0).child(this.chips(c, `rk-map-${k}`, cols.map((x) => [x, x]), this.mapping[k] ?? (cols.includes(k) ? k : null),
        (v, cx) => { if (v && v !== k) this.mapping[k] = v; else delete this.mapping[k]; this.schedule(cx); }, { none: optional ? "None" : null })));
    return v_flex().gap(8).p(12).border_1().border_color(c.rule)
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child("Which of your columns is which?"))
      .children(NEEDED.map((f) => row(f, false)))
      .children(RANKING.map((f) => row(f, true)))
      .children(OPTIONAL.map((f) => row(f, true)));
  }

  checkStep(c) {
    const ch = this.check;
    if (!ch) return this.checking ? div().text_size(12.5).text_color(c.grey500).child("Checking...") : null;
    const s = ch.stats ?? {};
    const tick = (on, text) => h_flex().gap(6).items_center()
      .child(svg(icon(on ? "circle-check" : "circle-x")).size(13).text_color(on ? c.good : c.grey500))
      .child(div().text_size(12.5).text_color(on ? c.navy : c.grey500).child(text));
    const avail = ch.available ?? {};
    return v_flex().gap(10)
      .when(s.rows != null, (e) => e.child(h_flex().gap(16)
        .child(div().text_size(13).text_color(c.navy).child(`${int(s.rows)} rows`))
        .child(div().text_size(13).text_color(c.navy).child(`${s.queries} queries`))
        .child(div().text_size(13).text_color(c.navy).child(`${(s.arms ?? []).length} systems`))))
      .children((ch.errors ?? []).map((t, i) => h_flex().id(`rk-err-${i}`).gap(8).items_start()
        .child(svg(icon("circle-x")).size(13).flex_none().mt(2).text_color(c.fail))
        .child(div().flex_1().text_size(12.5).line_height("18px").text_color(c.fail).child(t))))
      .children((ch.warnings ?? []).map((t, i) => h_flex().id(`rk-warn-${i}`).gap(8).items_start()
        .child(svg(icon("triangle-alert")).size(13).flex_none().mt(2).text_color(c.warn ?? "#b45309"))
        .child(div().flex_1().text_size(12.5).line_height("18px").text_color(c.warn ?? "#b45309").child(t))))
      .when(Object.keys(avail).length > 0, (e) => e.child(v_flex().gap(4)
        .child(tick(avail["relevance metrics"], avail["relevance metrics"] ? "Relevance metrics: nDCG, P@10, R@50, MRR" : "Relevance metrics: unavailable, no labels"))
        .child(tick(avail["per-query side by side"], "Each query side by side"))
        .child(tick(avail["slices"], avail["slices"] ? "Slices by segment" : "Slices: unavailable, no segment column"))
        .child(tick(avail["traffic-weighted nDCG"], avail["traffic-weighted nDCG"] ? "Traffic-weighted nDCG" : "Traffic-weighted nDCG: unavailable, needs weights and labels"))))
      .when(ch.ok, (e) => e
        .child(v_flex().gap(6).child(this.label(c, "Compare against"))
          .child(this.chips(c, "rk-base", (s.arms ?? []).map((a) => [a, a]), this.baseline, (v) => { this.baseline = v; })))
        .when(Boolean(s.labels), (x) => x.child(v_flex().gap(6).child(this.label(c, "Where do the labels come from?"))
          .child(this.chips(c, "rk-lf", LABELS_FROM, this.labelsFrom, (v) => { this.labelsFrom = v; })))));
  }

  extras(c) {
    if (!this.check?.ok) return null;
    const ds = (this.datasets ?? []).map((d) => [d.name, d.name]);
    return v_flex().gap(12)
      .child(v_flex().gap(6).child(this.label(c, "Name")).child(this.input(c, "rk-name", this.name)))
      .when(!this.check.stats?.labels, (e) => e.child(v_flex().gap(6).child(this.label(c, "Judgments instead of labels", true))
        .child(v_flex().id("rk-judg").max_h(90).overflow_y_scroll().child(this.chips(c, "rk-judg", ds, this.judgments, (v, cx) => { this.judgments = v; this.validate(cx); }, { none: "None" })))))
      .child(v_flex().gap(6).child(this.label(c, "Catalog, for titles side by side", true))
        .child(v_flex().id("rk-cat").max_h(90).overflow_y_scroll().child(this.chips(c, "rk-cat", ds, this.catalog, (v) => { this.catalog = v; }, { none: "None" }))))
      .child(v_flex().gap(6).child(this.label(c, "What you expect", true)).child(this.input(c, "rk-hyp", this.hypothesis)));
  }

  render() {
    const c = palette(isDark(theme()));
    const why = !this.check ? "" : !this.check.ok ? "Fix the errors above to import." : !this.baseline ? "Pick what to compare against."
      : this.check.stats?.labels && !this.labelsFrom ? "Say where the labels come from." : !this.name.value().trim() ? "Give it a name." : "";
    return v_flex().id("rankings").size_full().bg(c.surface).rounded(RADIUS).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(56).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
        .child(div().size(28).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("git-compare")).size(15)))
        .child(div().flex_1().text_size(15).font_semibold().child("Compare your own rankings"))
        .child(div().id("rk-cancel").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => closeOverlay(props()?.overlay)).child("Cancel")))
      .child(div().id("rk-body").flex_1().min_h(0).overflow_y_scroll().child(v_flex().px(20).py(16).gap(16)
        .child(div().text_size(12.5).line_height("19px").text_color(c.grey900).child("Bring your own system's ranked results, and compare them like any offline evaluation: metrics with intervals, slices, and each query side by side."))
        .child(this.fileStep(c))
        .child(this.mappingStep(c) ?? div())
        .child(this.checkStep(c) ?? div())
        .child(this.extras(c) ?? div())
        .when(Boolean(this.error), (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.error)))))
      .child(h_flex().h(60).flex_none().px(20).gap(10).items_center().border_t(1).border_color(c.rule)
        .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(c.grey500).child(this.busy ? (this.progress != null ? `Importing... ${Math.round(this.progress * 100)}%` : "Importing...") : why))
        .child(Button(c, "rk-cancel-b", "Cancel", () => closeOverlay(props()?.overlay)))
        .child(Button(c, "rk-import", this.busy ? "Importing..." : "Import", (_e, cx) => this.importIt(cx), { kind: "primary", disabled: !this.canImport() })));
  }
}
