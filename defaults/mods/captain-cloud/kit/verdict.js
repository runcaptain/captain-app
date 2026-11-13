// captain-ui: how one evaluation reads, in one of three words and one sentence.
//
// The words are Better, No clear difference and Worse (Edgar, 2026-11-12: "a verdict of win, noise, or bad").
// The sentence says which model, against which, on what, and then two things whenever they apply, because they
// change what the result means and are never dropped for style (defaults/AGENTS.md, "Screen rules"):
//   - where the evidence comes from (Simulated events, LLM graded, From logged clicks, Human graded, Real traffic);
//   - that the model agrees with its own labeler, which is never read as Better;
//   - that too few of its results were judged to call it, which is never read as Better or Worse.
//
//   const v = runVerdict({ comparison: "win", evidence: "HUMAN_GRADED", model: "tuned", against: "keyword-baseline", metric: "ndcg@10" });
//   v.word      "Better"
//   v.sentence  "tuned is better than keyword-baseline on nDCG@10. Human graded."
import { metricWords } from "./words.js";

export const VERDICT_WORDS = { win: "Better", noise: "No clear difference", loss: "Worse" };
export const EVIDENCE_SENTENCE = { SIMULATED: "Simulated events.", LLM_GRADED: "LLM graded.", LOGGED_CLICKS: "From logged clicks.", HUMAN_GRADED: "Human graded.", REAL_TRAFFIC: "Real traffic." };
const TONE = { Better: "good", Worse: "bad", "No clear difference": "neutral" };

/** A model's name without its version and its project's prefix: "shop/ranker@v2" reads "ranker". */
export const modelName = (ref) => String(ref ?? "").replace(/@v\d+$/, "").replace(/^.*\//, "");

/** The facts of a run that decide its verdict, from GET /api/runs/<name> and its evaluation (GET /api/backtests/<name>).
 *  The model judged is the comparison's B arm; the one it is compared with is A. */
export function runFacts(run, backtest) {
  const p = (run?.comparisons ?? []).find((x) => x.primary) ?? run?.comparisons?.[0] ?? null;
  return {
    comparison: p?.verdict ?? null, evidence: p?.evidence ?? run?.arms?.[0]?.evidence ?? null,
    model: p?.b ?? run?.arms?.[1]?.arm ?? null, against: p?.a ?? run?.arms?.[0]?.arm ?? null, metric: p?.metric ?? backtest?.preregistration?.primary_metric ?? null,
    delta: p?.delta ?? null, ci_low: p?.ci_low ?? null, ci_high: p?.ci_high ?? null,
    too_few_judged: run?.complete === false,
    own_labeler: (backtest?.tags ?? {}).label_independence === "shared" || run?.own_labeler === true,
  };
}

/** {word, tone, sentence} for those facts. `word` is null before there is a comparison. */
export function runVerdict(f) {
  const model = modelName(f?.model) || "This model", other = modelName(f?.against) || "the model it's compared with";
  const on = f?.metric ? ` on ${metricWords(f.metric)}` : "";
  const evidence = EVIDENCE_SENTENCE[f?.evidence] ?? "";
  if (!f?.comparison) return { word: null, tone: "neutral", sentence: `${model} has not been compared with ${other} yet.` };
  let word = VERDICT_WORDS[f.comparison] ?? "No clear difference";
  let first = word === "Better" ? `${model} is better than ${other}${on}.` : word === "Worse" ? `${model} is worse than ${other}${on}.` : `No clear difference between ${model} and ${other}${on}.`;
  let also = "";
  if (f.too_few_judged) {
    // Unjudged results count as wrong, so the score is understated: neither Better nor Worse can be said.
    word = "No clear difference";
    first = `Too few judged results to say whether ${model} is better than ${other}${on}.`;
    if (f.own_labeler) also = " It also agrees with its own labeler.";
  } else if (f.own_labeler) {
    // It learned from the labeler that grades it: a higher score is agreement, never a win.
    if (word === "Better") word = "No clear difference";
    first = `${model} agrees with its own labeler, so its score doesn't show it is better than ${other}${on}.`;
  }
  return { word, tone: TONE[word], sentence: `${first}${also} ${evidence}`.trim() };
}
