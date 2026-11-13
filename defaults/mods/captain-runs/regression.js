// CAPTAIN: Runs, Compare: regression testing (Edgar, 2026-11-12: "do offline evaluations of models, compare for
// regression, do regression testing"). Every evaluation run, newest first, one row each: when, which evaluation,
// the model judged, the model it is compared with, the measure, the change with its interval, and the verdict in
// one of three words (kit/verdict.js). Pick a model to see only its runs: has it got better or worse over time.
// Tick two rows to put those two runs side by side; a row's name opens that run in Runs, with its comparison.
//
// GET /api/regression?model= answers the rows and the models there are. GET /api/compare-runs?a=&b= says what makes
// two runs' numbers not comparable (different queries, judgments, measure or mode), and that is said above the two.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { api } from "./kit/cst.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { metricWords } from "./kit/words.js";
import { runVerdict, modelName } from "./kit/verdict.js";
import { openForm } from "./kit/agent.js";

const fmtD = (x) => (x === null || x === undefined ? "" : `${x >= 0 ? "+" : ""}${Number(x).toFixed(4)}`);
const fmt4 = (x) => (x === null || x === undefined ? "not measured" : Number(x).toFixed(4));
const day = (iso) => (iso ? String(iso).slice(0, 10) : "");
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
const ref = (r) => `${r.name}@v${r.version}`;

/** The verdict of one row of GET /api/regression: {word, tone, sentence}. */
export function rowVerdict(r) {
  return runVerdict({ comparison: r.comparison, evidence: r.evidence, model: r.model, against: r.against, metric: r.metric, too_few_judged: r.too_few_judged, own_labeler: r.own_labeler });
}

/** Two runs side by side, in one sentence: how the newer model's score differs, and why that difference has no
 *  interval. Null when the two can't be put on one scale. */
export function pairSentence(a, b, comparable) {
  if (!comparable || a.metric !== b.metric || a.value == null || b.value == null) return null;
  const [older, newer] = a.created <= b.created ? [a, b] : [b, a];
  const d = newer.value - older.value;
  const same = modelName(older.model) === modelName(newer.model);
  const who = same ? `${modelName(newer.model)} scored ${fmt4(newer.value)} in the newer run and ${fmt4(older.value)} in the older one`
    : `${modelName(newer.model)} scored ${fmt4(newer.value)} and ${modelName(older.model)} scored ${fmt4(older.value)}`;
  return `On ${metricWords(a.metric)}, ${who}: a difference of ${fmtD(d)}. The two runs were scored separately, so this difference has no interval.`;
}

export class Regression {
  constructor(host) {
    this.host = host;
    this.rows = null;
    this.models = [];
    this.model = null; // one model's runs, or null for every model
    this.error = null;
    this.picked = []; // up to two rows, by "name@vN"
    this.pair = null; // {a, b, comparable, issues} for the two picked | {loading} | {error}
  }

  load(cx) {
    const seq = (this.seq = (this.seq ?? 0) + 1);
    cx.spawn(async (cx) => {
      const r = await api(`/api/regression?limit=100${this.model ? `&model=${encodeURIComponent(this.model)}` : ""}`);
      if (seq !== this.seq) return;
      if (r.ok) {
        this.rows = r.data?.rows ?? [];
        this.models = r.data?.models ?? [];
        this.error = null;
        this.picked = this.picked.filter((p) => this.rows.some((x) => ref(x) === p));
        if (this.picked.length < 2) this.pair = null;
      } else if (!r.starting) this.error = r;
      else cx.timer.after(1000, (cx) => { if (seq === this.seq) this.load(cx); });
      cx.notify();
    });
  }

  /** What this page shows, for agents (cst app context). */
  slice() {
    return { model: this.model, compared: this.picked.length === 2 ? [...this.picked] : null };
  }

  pickModel(cx, m) {
    this.model = m;
    this.picked = [];
    this.pair = null;
    this.load(cx);
  }

  /** Tick or untick a run. With two ticked, ask what makes them comparable or not. */
  pick(cx, r) {
    const id = ref(r);
    if (this.picked.includes(id)) this.picked = this.picked.filter((p) => p !== id);
    else this.picked = [...this.picked.slice(-1), id]; // a third tick replaces the older of the two
    this.pair = null;
    if (this.picked.length === 2) {
      const [a, b] = this.picked;
      this.pair = { loading: true };
      cx.spawn(async (cx) => {
        const res = await api(`/api/compare-runs?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
        if (this.picked[0] !== a || this.picked[1] !== b) return;
        this.pair = res.ok ? res.data : { error: "Couldn't check whether these two runs can be compared." };
        cx.notify();
      });
    }
    cx.notify();
  }

  /** Open a row's run in Runs, with its comparison in place. */
  open(cx, r) {
    this.host.wantRun = { name: r.name, version: r.version };
    this.host.pick(cx, "backtests", r.name);
  }

  render(c, w) {
    const rows = this.rows ?? [];
    const narrow = w < 1040;
    const tone = (t) => (t === "good" ? c.good : t === "bad" ? c.fail : c.grey900);
    const chip = (id, label, on, onClick) => div().id(id).h(26).px(8).flex().flex_none().items_center().cursor_pointer().text_size(12)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900).on_click(onClick).child(label);
    const cell = (t, width, right) => div().w(width).flex_none().px(8).truncate().when(!!right, (e) => e.text_right()).child(t);
    const two = this.picked.map((p) => rows.find((x) => ref(x) === p)).filter(Boolean);
    return v_flex().id("regression").flex_1().min_h(0).overflow_y_scrollbar()
      .child(h_flex().id("reg-models").flex_none().flex_wrap().gap(6).px(20).py(10).items_center().border_b(1).border_color(c.rule)
        .child(div().flex_none().text_size(12).text_color(c.grey500).child("Model"))
        .child(chip("reg-model-all", "Every model", !this.model, (_e, cx) => this.pickModel(cx, null)))
        .children(this.models.map((m, i) => chip(`reg-model-${i}`, modelName(m), this.model === m, (_e, cx) => this.pickModel(cx, m)))))
      .when(this.rows === null && !this.error, (e) => e.child(Skeleton(c, 8)))
      .when(!!this.error, (e) => e.child(div().p(20).child(this.host.troubleView(c, "regression", this.error, "Couldn't load the evaluation runs.", (cx) => this.load(cx)))))
      .when(this.rows !== null && !rows.length && !this.error, (e) => e.child(v_flex().id("reg-empty").p(20).gap(8)
        .child(div().text_size(13).font_medium().child(this.model ? `No evaluation has judged ${modelName(this.model)} yet` : "No evaluation has been run yet"))
        .child(div().max_w(520).text_size(12.5).line_height("19px").text_color(c.grey900).child("Each run of an evaluation is a row here, with how the model did against the one it is compared with. Run the same evaluation on a new model to see whether it got better or worse."))
        .child(h_flex().child(Button(c, "reg-new", "New evaluation", () => openForm("backtest"), { small: true })))))
      .when(rows.length > 0, (e) => e
        .child(h_flex().flex_none().h(30).px(12).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().text_color(c.grey900).items_center()
          .child(div().w(28).flex_none())
          .child(cell("When", 96))
          .child(div().flex_1().min_w(140).px(8).child("Evaluation"))
          .child(cell("Model", narrow ? 130 : 160))
          .child(cell("Compared with", narrow ? 130 : 160))
          .when(!narrow, (x) => x.child(cell("Measure", 120)))
          .child(cell("Change", narrow ? 84 : 200, true))
          .child(cell("Verdict", 150)))
        .children(rows.map((r, i) => {
          const v = rowVerdict(r);
          const on = this.picked.includes(ref(r));
          return h_flex().id(`reg-row-${i}`).h(36).flex_none().px(12).items_center().border_b(1).border_color(c.rule).text_size(12.5)
            .bg(on ? c.blueLight : c.surface)
            .child(div().id(`reg-pick-${i}`).w(28).flex_none().flex().items_center().justify_center().cursor_pointer().tooltip(on ? "Take it out of the side by side" : "Put it side by side with another run")
              .on_click((_e, cx) => this.pick(cx, r))
              .child(svg(icon(on ? "square-check" : "square")).size(14).text_color(on ? c.blue : c.grey500)))
            .child(cell(day(r.created), 96).text_color(c.grey900))
            .child(h_flex().flex_1().min_w(140).px(8).gap(6).items_center()
              .child(div().id(`reg-open-${i}`).min_w(0).truncate().text_color(c.blue).cursor_pointer().hover((s) => s.underline()).tooltip("Open this run, with its comparison")
                .on_click((_e, cx) => this.open(cx, r)).child(r.name))
              .child(div().flex_none().text_size(11).text_color(c.grey500).child(`run ${r.version}`)))
            .child(cell(modelName(r.model), narrow ? 130 : 160))
            .child(cell(modelName(r.against), narrow ? 130 : 160).text_color(c.grey900))
            .when(!narrow, (x) => x.child(cell(metricWords(r.metric), 120).text_color(c.grey900)))
            .child(cell(narrow ? fmtD(r.delta) : `${fmtD(r.delta)}  [${fmtD(r.ci_low)}, ${fmtD(r.ci_high)}]`, narrow ? 84 : 200, true))
            .child(div().id(`reg-verdict-${i}`).w(150).flex_none().px(8).truncate().text_color(tone(v.tone)).tooltip(v.sentence).child(v.word ?? "Not compared yet"));
        }))
        // Under the table: the two ticked runs side by side, or what to do to get there.
        .child(v_flex().id("reg-pair").flex_none().px(20).py(14).gap(10)
          .when(two.length < 2, (x) => x.child(div().id("reg-pair-hint").text_size(12.5).text_color(c.grey500)
            .child(two.length === 1 ? "Tick one more run to put the two side by side." : "Tick two runs to put them side by side.")))
          .when(two.length === 2, (x) => x.child(this.pairView(c, two[0], two[1], tone)))));
  }

  /** Two runs side by side: what stops them being compared (if anything), each one's verdict and scores, and the
   *  plain difference when they are on one scale. */
  pairView(c, a, b, tone) {
    const p = this.pair;
    const issues = p?.issues ?? [];
    const comparable = !!p && !p.loading && !p.error && p.comparable !== false;
    const one = (r, k) => {
      const v = rowVerdict(r);
      const fact = (label, value) => h_flex().gap(8).text_size(12.5).child(div().w(120).flex_none().text_color(c.grey500).child(label)).child(div().flex_1().min_w(0).child(value));
      return v_flex().id(`reg-side-${k}`).flex_1().min_w(260).p(12).gap(8).border_1().border_color(c.rule)
        .child(h_flex().gap(8).items_center()
          .child(div().min_w(0).truncate().text_size(13).font_medium().child(r.name))
          .child(div().flex_none().text_size(11.5).text_color(c.grey500).child(`run ${r.version}, ${day(r.created)}`)))
        .child(h_flex().flex_wrap().gap(8).items_center()
          .when(!!v.word, (e) => e.child(div().id(`reg-side-word-${k}`).flex_none().h(22).px(7).flex().items_center().border_1().border_color(tone(v.tone)).text_size(12).font_medium().text_color(tone(v.tone)).child(v.word)))
          .child(div().id(`reg-side-sentence-${k}`).flex_1().min_w(200).text_size(12.5).line_height("18px").child(v.sentence)))
        .child(fact(`${modelName(r.model)} scored`, fmt4(r.value)))
        .child(fact(`${modelName(r.against)} scored`, fmt4(r.against_value)))
        .child(fact("Change", `${fmtD(r.delta)}  [${fmtD(r.ci_low)}, ${fmtD(r.ci_high)}] on ${metricWords(r.metric)}`));
    };
    const line = pairSentence(a, b, comparable);
    return v_flex().gap(10)
      .child(div().text_size(13).font_medium().child("Side by side"))
      .when(!!p?.loading, (e) => e.child(Skeleton(c, 2)))
      .when(!!p?.error, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(p.error)))
      // What stops the two being read against each other comes before either verdict.
      .when(issues.length > 0, (e) => e.child(v_flex().id("reg-pair-issues").p(10).gap(4).border_1().border_color(c.fail)
        .child(div().text_size(12.5).font_medium().text_color(c.fail).child("These two runs can't be compared number for number"))
        .children(issues.map((x, i) => div().id(`reg-issue-${i}`).text_size(12).text_color(c.grey900).child(sentence(x.text))))))
      .child(h_flex().flex_wrap().gap(12).items_start().child(one(a, 0)).child(one(b, 1)))
      .when(!!line, (e) => e.child(div().id("reg-pair-line").text_size(12.5).line_height("19px").text_color(c.navy).child(line)));
  }
}
