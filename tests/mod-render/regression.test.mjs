// Runs, Compare: regression testing, and the verdict's three words.
//
// 1. The verdict (kit/verdict.js). One of three words: Better, No clear difference, Worse. One sentence. And the
//    honesty rules, which never bend to style: the evidence word ends every judged verdict; a model that agrees
//    with its own labeler is never Better; too few judged results is never Better or Worse; when both are true,
//    both are said.
// 2. The regression table (regression.js): every evaluation run with the change against the model it is compared
//    with, one model's runs alone, and two runs side by side with what stops them being compared said first.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/regression.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { runVerdict, runFacts, VERDICT_WORDS, EVIDENCE_SENTENCE, modelName } = await import(path.join(root, 'defaults/captain/kit/verdict.js'));

// ---- 1. the verdict ------------------------------------------------------------------------------------------------
const run = JSON.parse(fs.readFileSync(path.join(here, 'fixtures/captain-runs/run.json'), 'utf8'));
assert.deepEqual(VERDICT_WORDS, { win: 'Better', noise: 'No clear difference', loss: 'Worse' });
const facts = (over) => ({ comparison: 'win', evidence: 'HUMAN_GRADED', model: 'shop/ranker@v2', against: 'keyword-baseline@v1', metric: 'ndcg@10', ...over });

// Three words, and only three.
assert.equal(runVerdict(facts({ comparison: 'win' })).word, 'Better');
assert.equal(runVerdict(facts({ comparison: 'noise' })).word, 'No clear difference');
assert.equal(runVerdict(facts({ comparison: 'loss' })).word, 'Worse');
for (const comparison of ['win', 'noise', 'loss']) for (const too_few_judged of [false, true]) for (const own_labeler of [false, true]) {
  const v = runVerdict(facts({ comparison, too_few_judged, own_labeler }));
  assert.ok(['Better', 'No clear difference', 'Worse'].includes(v.word), `${comparison}: ${v.word}`);
  assert.ok(!/\b(win|noise|loss)\b/i.test(v.sentence) && !/[←-⇿—]/.test(v.sentence), `plain words: ${v.sentence}`);
}
// The sentence names both models (by their own names, without the version or the project), the measure, the evidence.
assert.equal(runVerdict(facts({})).sentence, 'ranker is better than keyword-baseline on nDCG@10. Human graded.');
assert.equal(runVerdict(facts({ comparison: 'loss', evidence: 'REAL_TRAFFIC' })).sentence, 'ranker is worse than keyword-baseline on nDCG@10. Real traffic.');
assert.equal(runVerdict(facts({ comparison: 'noise', evidence: 'SIMULATED' })).sentence, 'No clear difference between ranker and keyword-baseline on nDCG@10. Simulated events.');
assert.equal(modelName('shop/ranker@v2'), 'ranker');

// The evidence word ends every judged verdict, whatever else is true.
for (const [evidence, words] of Object.entries(EVIDENCE_SENTENCE)) {
  for (const comparison of ['win', 'noise', 'loss']) for (const too_few_judged of [false, true]) for (const own_labeler of [false, true]) {
    const s = runVerdict(facts({ comparison, evidence, too_few_judged, own_labeler })).sentence;
    assert.ok(s.endsWith(words), `${evidence}, ${comparison}, too few ${too_few_judged}, own labeler ${own_labeler}: ${s}`);
  }
}

// Its own labeler: said whenever it applies, and never read as Better, even when the run recorded a win.
const own = runVerdict(facts({ comparison: 'win', evidence: 'LLM_GRADED', own_labeler: true }));
assert.equal(own.word, 'No clear difference');
assert.equal(own.sentence, "ranker agrees with its own labeler, so its score doesn't show it is better than keyword-baseline on nDCG@10. LLM graded.");
for (const comparison of ['win', 'noise', 'loss']) {
  const v = runVerdict(facts({ comparison, own_labeler: true }));
  assert.ok(/agrees with its own labeler/.test(v.sentence), `said for ${comparison}: ${v.sentence}`);
  assert.notEqual(v.word, 'Better', `never Better for ${comparison}`);
}
// A loss stays a loss: agreeing with the labeler can only flatter a model.
assert.equal(runVerdict(facts({ comparison: 'loss', own_labeler: true })).word, 'Worse');

// Too few judged results: said whenever it applies, and never Better or Worse.
const few = runVerdict(facts({ comparison: 'win', evidence: 'SIMULATED', too_few_judged: true }));
assert.equal(few.word, 'No clear difference');
assert.equal(few.sentence, 'Too few judged results to say whether ranker is better than keyword-baseline on nDCG@10. Simulated events.');
assert.equal(runVerdict(facts({ comparison: 'loss', too_few_judged: true })).word, 'No clear difference');

// Both at once: the labeler is not dropped.
const both = runVerdict(facts({ comparison: 'win', evidence: 'SIMULATED', too_few_judged: true, own_labeler: true }));
assert.equal(both.sentence, 'Too few judged results to say whether ranker is better than keyword-baseline on nDCG@10. It also agrees with its own labeler. Simulated events.');
assert.equal(both.word, 'No clear difference');

// Not the usual settings: said in the sentence, and the measure is named at the depth it really looked.
const { metricAt } = await import(path.join(root, 'defaults/captain/kit/verdict.js'));
assert.equal(metricAt('ndcg@10', { 'ndcg@10': 20 }), 'ndcg@20');
assert.equal(metricAt('ndcg@10_tw', { 'ndcg@10': 20 }), 'ndcg@20_tw');
assert.equal(metricAt('ndcg@10', { 'ndcg@10': 10, 'r@50': 100 }), 'ndcg@10');
assert.equal(metricAt('mrr', { mrr: 5 }), 'mrr');
assert.equal(metricAt('ndcg@10', null), 'ndcg@10');
const deep = facts({ depths: { 'ndcg@10': 20, 'p@10': 10, 'r@50': 50, mrr: 50 }, not_usual: ['nDCG looks 20 results deep, not the usual 10.'] });
assert.equal(runVerdict(deep).sentence, 'ranker is better than keyword-baseline on nDCG@20. nDCG looks 20 results deep, not the usual 10. Human graded.');
assert.equal(runVerdict(deep).word, 'Better', 'a different depth does not change the word, only what it is a verdict on');
// It joins the other honesty rules, never replaces them, and the evidence still ends the sentence.
assert.equal(runVerdict({ ...deep, too_few_judged: true, own_labeler: true, evidence: 'SIMULATED' }).sentence,
  'Too few judged results to say whether ranker is better than keyword-baseline on nDCG@20. It also agrees with its own labeler. nDCG looks 20 results deep, not the usual 10. Simulated events.');
// The usual depths say nothing extra.
assert.equal(runVerdict(facts({ depths: { 'ndcg@10': 10, 'p@10': 10, 'r@50': 50, mrr: 50 }, not_usual: [] })).sentence, 'ranker is better than keyword-baseline on nDCG@10. Human graded.');
assert.deepEqual(runFacts({ ...run, depths: { 'ndcg@10': 20 }, not_usual: ['nDCG looks 20 results deep, not the usual 10.'] }, null).not_usual, ['nDCG looks 20 results deep, not the usual 10.']);

// Before there is a comparison it claims nothing.
assert.deepEqual([runVerdict(facts({ comparison: null })).word, runVerdict(facts({ comparison: null })).sentence], [null, 'ranker has not been compared with keyword-baseline yet.']);

// The facts come from the run's record and its evaluation's.
const f = runFacts(run, { tags: {} });
assert.deepEqual([f.comparison, f.evidence, f.model, f.against, f.metric, f.too_few_judged, f.own_labeler], ['win', 'SIMULATED', 'demo-tuned@v1', 'demo-bm25@v1', 'ndcg@10', false, false]);
assert.equal(runFacts({ ...run, complete: false }, null).too_few_judged, true, 'an arm with under 90% of its top 10 judged');
assert.equal(runFacts(run, { tags: { label_independence: 'shared' } }).own_labeler, true, 'trained on the labeler that grades the test set');

// ---- 2. the regression table ---------------------------------------------------------------------------------------
globalThis.__fixtures = path.join(here, 'fixtures/captain-runs');
globalThis.__routes = [
  [/^\/api\/regression\?.*model=/, 'regression-one'],
  [/^\/api\/regression/, 'regression'],
  [/^\/api\/compare-runs\?.*demo-historic-replay/, 'pair-not'],
  [/^\/api\/compare-runs/, 'pair-ok'],
  [/^\/api\/backtests$/, 'backtests'],
  [/^\/api\/backtests\/[^?]+$/, 'backtest'],
  [/^\/api\/runs\//, 'run'],
  [/^\/api\/(workflows|grading|replays)$/, 'empty'],
  [/^\/api\/(jobs|live)\?/, 'empty'],
  [/\/(compatibility|histogram)/, 'empty'],
];
const fx = (n) => JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, `${n}.json`), 'utf8'));
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-runs-reg-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-runs'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const { rowVerdict, pairSentence } = await import(path.join(work, 'regression.js'));
const h = harness(Mod);
await h.start();
await h.click('submenu-compare');
await h.idle(30);
const rows = fx('regression').rows;
let t = h.text();

// One row per run, with the model judged, the model it is compared with, the change and the verdict in a word.
for (const col of ['When', 'Evaluation', 'Model', 'Compared with', 'Change', 'Verdict']) assert.ok(t.includes(col), `column ${col}`);
assert.equal(h.byIdPrefix('reg-row-').length, rows.length);
assert.ok(t.includes('demo-tuned') && t.includes('demo-bm25') && t.includes('+0.0458') && t.includes('2026-11-06'), `a row's facts: ${t.slice(0, 500)}`);
assert.ok(!/@v\d/.test(t), 'no raw name@v ref');
const words = h.byIdPrefix('reg-verdict-').map((e) => e.kids.filter((k) => typeof k === 'string').join(''));
assert.deepEqual(words, ['Better', 'Worse', 'No clear difference', 'No clear difference'], 'win, loss, too few judged, own labeler');
// The honesty rules reach the table: the two rows that can't be called say why, on the word itself.
assert.equal(rowVerdict(rows[2]).sentence, 'Too few judged results to say whether demo-tuned is better than demo-bm25 on nDCG@10, traffic-weighted. From logged clicks.');
assert.equal(rowVerdict(rows[3]).sentence, "ranker agrees with its own labeler, so its score doesn't show it is better than keyword-baseline on nDCG@10. LLM graded.");
assert.equal(h.byIdPrefix('reg-verdict-2')[0].call('tooltip')[0], rowVerdict(rows[2]).sentence, 'the sentence is on the word');
assert.ok(!/\b(WIN|NOISE|LOSS)\b/.test(t), 'no WIN, NOISE or LOSS');

// One model's runs alone.
assert.ok(t.includes('Every model') && t.includes('ranker'), 'the models to pick from');
await h.click('reg-model-0');
await h.idle(30);
assert.ok(globalThis.__asked.some((p) => /\/api\/regression\?.*model=demo-tuned/.test(p)), 'asks the services for that model');
assert.equal(h.byIdPrefix('reg-row-').length, fx('regression-one').rows.length);
await h.click('reg-model-all');
await h.idle(30);

// Two runs side by side. One ticked: it says to tick another.
assert.ok(h.text().includes('Tick two runs to put them side by side.'));
await h.click('reg-pick-0');
assert.ok(h.text().includes('Tick one more run to put the two side by side.'));
await h.click('reg-pick-1');
await h.idle(30);
t = h.text();
assert.ok(globalThis.__asked.some((p) => p.startsWith('/api/compare-runs?a=demo-tuned-vs-bm25%40v2&b=demo-tuned-vs-bm25%40v1')), 'asks whether the two can be compared');
assert.ok(t.includes('Side by side'), 'the two, side by side');
assert.deepEqual(h.byIdPrefix('reg-side-word-').map((e) => e.kids.join('')), ['Better', 'Worse'], 'each with its own verdict');
assert.ok(t.includes('demo-tuned is better than demo-bm25 on nDCG@10. Simulated events.') && t.includes('demo-tuned is worse than demo-bm25 on nDCG@10. Simulated events.'), 'and its sentence');
// Comparable, same measure: the plain difference, and that it has no interval.
assert.equal(pairSentence(rows[0], rows[1], true), 'On nDCG@10, demo-tuned scored 0.8187 in the newer run and 0.7519 in the older one: a difference of +0.0668. The two runs were scored separately, so this difference has no interval.');
assert.ok(t.includes('The two runs were scored separately, so this difference has no interval.'), 'said on screen');
assert.equal(h.byIdPrefix('reg-pair-issues').length, 0, 'nothing stops these two');
// What the page shows, for agents (cst app context): the model picked and the two runs side by side.
assert.deepEqual(h.mod.reg.slice(), { model: null, compared: ['demo-tuned-vs-bm25@v2', 'demo-tuned-vs-bm25@v1'] });

// Two that can't be compared number for number: that is said first, and no difference is drawn.
await h.click('reg-pick-2'); // a third tick replaces the older of the two
await h.idle(30);
t = h.text();
assert.deepEqual(h.mod.reg.picked, ['demo-tuned-vs-bm25@v1', 'demo-historic-replay@v1']);
assert.ok(t.includes("These two runs can't be compared number for number") && t.includes('Different query sets') && t.includes('Different modes (simulated vs historic).'), `the reasons, as sentences: ${t.slice(-700)}`);
assert.ok(t.indexOf("can't be compared number for number") < t.indexOf('Too few judged results to say'), 'said before either verdict');
assert.equal(h.byIdPrefix('reg-pair-line').length, 0, 'no difference is drawn across runs that are not on one scale');
assert.equal(pairSentence(rows[1], rows[2], false), null);
assert.equal(pairSentence(rows[0], { ...rows[1], metric: 'mrr' }, true), null, 'nor across two measures');

// A row's name opens that run in Runs, with its comparison in place.
await h.click('reg-open-1');
await h.idle(30);
assert.equal(h.mod.tab, 'runs');
assert.equal(h.mod.kind, 'backtests');
assert.equal(h.mod.detail.runV, 1, 'the run that was clicked, not the newest');
assert.ok(h.byIdPrefix('cmp-verdict').length >= 1, 'its comparison is on the page');

const worst = Math.max(...h.timings);
assert.ok(worst < 50, `a render took ${worst.toFixed(1)} ms`);
console.log('regression render test: ok');
process.exit(0);
