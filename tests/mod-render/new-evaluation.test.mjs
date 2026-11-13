// New evaluation: a person sets an offline evaluation up themselves (Edgar, 2026-11-12: "I don't think that a
// customer who's working could run one without data like QA pairs and such... They may have to put it together
// themselves"). Against fixtures captured from the real backend (the options are the runner's own; the preview is
// testsets.preview on a small CSV). It drives: the models, their own questions with the column mapper and the
// preview, the measures with one line each, the limits as editable fields with their defaults, what is sent, and
// starting from a saved test so "run it again on the new model" is one pick.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/new-evaluation.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-evaluation');
globalThis.__routes = [
  [/^\/api\/evaluations\/options$/, 'options'],
  [/^\/api\/pipelines$/, 'pipelines'],
  [/^\/api\/datasets$/, 'datasets'],
  [/^\/api\/dataset-classes$/, 'classes'],
  [/^\/api\/backtests$/, 'plan', 'POST'],
  [/^\/api\/backtests$/, 'backtests'],
  [/^\/api\/backtests\/[^?]+\/run$/, 'started', 'POST'],
  [/^\/api\/backtests\/deep-test$/, 'backtest-deep'],
  [/^\/api\/backtests\/[^?]+$/, 'backtest'],
  [/^\/api\/testsets\/preview$/, 'preview', 'POST'],
  [/^\/api\/testsets$/, 'made', 'POST'],
  [/^\/api\/compute$/, 'compute'],
  [/^\/api\/settings$/, 'settings'],
];
const routes = (re, file, method) => { const r = globalThis.__routes.find((x) => String(x[0]) === String(re) && x[2] === method); r[1] = file; };
const fx = (n) => JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, `${n}.json`), 'utf8'));
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-evaluation-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-evaluation'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod, missing, measureOk, ROLES } = await import(path.join(work, 'main.js'));
const posted = (p) => globalThis.__bodies.filter(([q, o]) => q === p && (o.method === 'POST' || o.body !== undefined)).map(([, o]) => o.body);
const mount = async (values) => {
  globalThis.__props = { overlay: 'o1', ...(values ? { values } : {}) };
  globalThis.__shared = {}; globalThis.__runs = []; globalThis.__bodies = []; globalThis.__asked.length = 0;
  const h = harness(Mod);
  await h.start();
  await h.idle(30);
  return h;
};

let h = await mount();
let t = h.text();
const opts = fx('options');

// Six steps, in the order a person thinks: models, what to test on, what decides it, limits, where, the name.
for (const s of ['Which models', 'What to test on', 'What decides it', 'Limits', 'Where it runs', 'Save it as a test']) assert.ok(t.includes(s), `step: ${s}`);
assert.ok(t.indexOf('Which models') < t.indexOf('What to test on') && t.indexOf('What to test on') < t.indexOf('What decides it'), 'in order');

// 1. Models: every model version, to test and to compare with.
assert.equal(h.byIdPrefix('ev-model-').length, fx('pipelines').length);
assert.equal(h.byIdPrefix('ev-against-').length, fx('pipelines').length);

// 2. What to test on: three ways, their own questions first.
assert.deepEqual(h.byIdPrefix('ev-on-').map((e) => e.call('id')[0]), ['ev-on-own', 'ev-on-logs', 'ev-on-saved']);
assert.ok(t.includes('My own questions and answers') && t.includes('Logged searches') && t.includes('A saved test set'));
assert.equal(h.mod.v.on, 'own', 'it opens on their own questions');
// From a file on this computer: the table is read, the mapping is guessed, and nothing is written.
await h.click('ev-src-file');
h.mod.inputs.file.set_value('~/Downloads/my-questions.csv');
h.mod.v.file = '~/Downloads/my-questions.csv';
await h.click('ev-file-read');
await h.idle(30);
t = h.text();
const pv = fx('preview');
assert.equal(posted('/api/testsets/preview').at(-1).source.file, '/tmp/Downloads/my-questions.csv', 'the path is expanded and sent as a file');
assert.equal(posted('/api/testsets').length, 0, 'a preview makes nothing');
// The mapper: one row per thing the test needs, each a pick among the table's own columns.
for (const [role, label] of ROLES) assert.equal(h.byIdPrefix(`ev-map-${role}-`).length, pv.columns.length, `${label}: a pick among the ${pv.columns.length} columns`);
assert.deepEqual(h.mod.v.mapping, { query: 'question', product: 'wanted', grade: 'relevance', slice: 'kind' }, 'guessed from the column names');
assert.ok(t.includes('Which column is which, in my-questions.csv') && t.includes('Question') && t.includes('Wanted product') && t.includes('Grade (optional)') && t.includes('Group (optional)'));
// The preview: what it makes, in a sentence, the first rows mapped, and every row left out with its reason.
assert.ok(t.includes(`This makes ${pv.questions} questions with ${pv.answers} wanted answers, from ${pv.total} rows.`), `the count: ${t.slice(t.indexOf('This makes'), t.indexOf('This makes') + 120)}`);
assert.ok(t.includes("Left out: 1 with no question, 1 with a grade that isn't 0 to 3."), 'what was left out, and why');
assert.equal(h.byIdPrefix('ev-sample-').length, Math.min(5, pv.sample.length), 'the first rows, mapped');
assert.ok(t.includes('warm tent for two') && t.includes('3, Exact') && t.includes('camping'), 'a mapped row: question, grade in words, group');
// A row with no grade: the default is a field, not a hidden rule.
assert.ok(t.includes('A row with no grade counts as') && h.byIdPrefix('ev-grade-').length === 3);
await h.click('ev-grade-2');
await h.idle(30);
assert.equal(posted('/api/testsets/preview').at(-1).default_grade, 2, 'changing it asks again with that grade');
assert.deepEqual(posted('/api/testsets/preview').at(-1).mapping, h.mod.v.mapping, 'and keeps the mapping');
await h.click('ev-grade-3');
// Unmapping the question says what is missing rather than guessing.
routes(/^\/api\/testsets\/preview$/, 'preview-unmapped', 'POST');
await h.click('ev-map-query-0');
await h.idle(30);
assert.ok(h.text().includes('Say which column holds the question and which holds the product you want back.'));
routes(/^\/api\/testsets\/preview$/, 'preview', 'POST');
await h.click('ev-map-query-0');
await h.idle(30);

// 3. What decides it: every measure the runner has, one line each, one of them picked.
assert.equal(h.byIdPrefix('ev-measure-').length, opts.measures.length);
t = h.text();
for (const m of opts.measures) assert.ok(t.includes(m.label) && t.includes(m.line), `${m.label} with its line`);
assert.equal(h.mod.v.measure, 'ndcg@10');
assert.ok(t.includes('Every measure below is computed and shown either way.'));
// A measure this test can't decide on is said so, with the reason, and can't be picked.
const ctr = opts.measures.find((m) => m.id === 'ctr');
assert.equal(measureOk(ctr, 'own'), false);
assert.ok(t.includes('Not for this test: it comes from simulated shoppers, which only a test on logged searches has.'));
assert.equal(h.byIdPrefix('ev-measure-ctr')[0].call('on_click'), undefined, 'not clickable');
await h.click('ev-measure-mrr');
assert.equal(h.mod.v.measure, 'mrr');
await h.click('ev-measure-ndcg@10');
// Slices are fixed by the runner: said, never drawn as a control that does nothing.
assert.ok(t.includes('first 10') && t.includes('first 50'), 'each measure says how deep it looks');
assert.ok(t.includes('Each run also reports its results by group') && t.includes('by traffic'), 'the slices a run reports');
assert.equal(h.byIdPrefix('ev-slice-').length, 0, 'no slice control');

// How many results deep each measure looks: a field per measure that has a depth, starting at the usual one.
assert.deepEqual(h.byIdPrefix('ev-depth-in-').map((e) => e.call('id')[0]), opts.depths.map((d) => `ev-depth-in-${d.id}`));
assert.deepEqual(opts.depths.map((d) => h.mod.depthInputs[d.id].value()), ['10', '10', '50', '50'], 'the usual depths');
assert.ok(t.includes('nDCG looks this many results deep') && t.includes('Usually 10. Up to 200.') && t.includes('Usually 50. Up to 200.'));
assert.equal(h.byIdPrefix('ev-depth-note').length, 0, 'nothing to say while the depths are the usual ones');
h.mod.depthInputs['ndcg@10'].set_value('20'); h.mod.v.depths['ndcg@10'] = '20';
await h.layoutLists({});
t = h.text();
assert.ok(t.includes('These are not the usual depths. The result will say so, and it can only be compared with runs measured at the same depths.'), 'it says what a changed depth means');
assert.ok(t.includes('nDCG@20') && t.includes('near the top of the first 20'), `the measure is named and described at its depth: ${t.slice(t.indexOf('What decides it'), t.indexOf('What decides it') + 300)}`);
// A depth that is not a whole number from 1 to 200 is said, and nothing starts.
h.mod.v.depths['r@50'] = '0';
assert.ok(missing({ ...h.mod.v, name: 'x', models: ['a@v1'], against: 'b@v1' }, { preview: pv, opts }).includes('Recall looks this many results deep: a whole number from 1 to 200.'));
h.mod.v.depths['r@50'] = '2.5';
assert.ok(missing({ ...h.mod.v, name: 'x', models: ['a@v1'], against: 'b@v1' }, { preview: pv, opts }).some((m) => m.startsWith('Recall looks')));
h.mod.v.depths['r@50'] = '50';

// 4. Limits: fields a person can change, each with its default said. Nothing is capped out of sight.
assert.equal(h.mod.inputs.mde.value(), '0.003');
assert.ok(t.includes('Smallest gain worth acting on') && t.includes('Starts at 0.003.'));
assert.equal(h.byIdPrefix('ev-limit-population_size').length, 0, 'simulated shoppers is a limit of a logged-searches test only');
await h.click('ev-on-logs');
assert.equal(h.byIdPrefix('ev-limit-population_size').length, 1);
assert.ok(h.text().includes('Simulated shoppers') && h.text().includes('Starts at 4000.'));
assert.equal(measureOk(ctr, 'logs'), true, 'and on logged searches the shopper measures can decide');
await h.click('ev-on-own');

// 5. Where it runs: the workspace's place, with this computer free.
assert.ok(h.text().includes('Runs on') && h.byIdPrefix('ev-runs-on-aws-batch').length === 1);

// Nothing starts while something is missing: it says what, in the form's order.
await h.click('ev-run');
t = h.text();
for (const m of ['Give the test a name.', 'Pick the model to test.', 'Pick the model to compare it with.']) assert.ok(t.includes(m), `says: ${m}`);
assert.equal(posted('/api/backtests').length, 0, 'nothing was sent');
assert.deepEqual(missing({ ...h.mod.v, name: 'x', models: ['a@v1'], against: 'a@v1' }, { preview: pv, opts }), ["A model can't be compared with itself."]);

// Filled in: the test set is made from their table, the test is saved under its name, and it runs.
h.mod.inputs.name.set_value('shop-test'); h.mod.v.name = 'shop-test';
await h.click('ev-model-1');
await h.click('ev-model-2');
await h.click('ev-against-0');
h.mod.inputs.mde.set_value('0.01'); h.mod.v.mde = '0.01';
await h.click('ev-run');
await h.idle(40);
assert.deepEqual(posted('/api/testsets').at(-1), { name: 'shop-test', source: { file: '/tmp/Downloads/my-questions.csv' }, mapping: { query: 'question', product: 'wanted', grade: 'relevance', slice: 'kind' }, default_grade: 3 });
const plan = posted('/api/backtests').at(-1);
assert.deepEqual(plan.arms, ['demo-bm25@v1', 'demo-tuned@v1', 'demo-ranker@v1'], 'the model compared with first, then every model to test');
assert.deepEqual([plan.name, plan.mode, plan.queries, plan.judgments, plan.primary, plan.mde], ['shop-test', 'simulated', 'shop-test/questions@v1', 'shop-test/answers@v1', 'ndcg@10', 0.01]);
assert.equal(plan.hypothesis, 'Nothing was written down before the run.', 'an empty expectation is said to be empty, not invented');
assert.equal(plan.population_size, undefined, 'a limit that does not apply is not sent');
assert.deepEqual(plan.depths, { 'ndcg@10': 20, 'p@10': 10, 'r@50': 50, mrr: 50 }, 'the depth of every measure is sent, the changed one too');
assert.equal(posted('/api/backtests/shop-test/run').length, 1, 'and it runs');
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'runs' }]);
assert.equal(globalThis.__shared['captain.open'].live, 'live-9', 'Runs opens on it, live');

// Save without running: the test is saved, nothing starts, and Runs opens on the test.
h = await mount();
await h.click('ev-on-saved');
assert.ok(h.byIdPrefix('ev-queries-').length > 0 && h.byIdPrefix('ev-judgments-').length > 0, 'sets of questions and of answers from Data');
await h.click('ev-queries-0');
await h.click('ev-judgments-0');
h.mod.inputs.name.set_value('saved-only'); h.mod.v.name = 'saved-only';
await h.click('ev-model-1');
await h.click('ev-against-0');
await h.click('ev-save');
await h.idle(40);
assert.equal(posted('/api/backtests').length, 1);
assert.equal(posted('/api/backtests/saved-only/run').length, 0, 'nothing started');
assert.equal(posted('/api/testsets').length, 0, 'a saved test set is used as it is');
assert.equal(globalThis.__shared['captain.open'].backtest, 'saved-only');

// A refusal from the services shows in their words, and the form stays.
h = await mount();
routes(/^\/api\/backtests$/, 'plan-refused', 'POST');
await h.click('ev-on-saved');
await h.click('ev-queries-0'); await h.click('ev-judgments-0');
h.mod.inputs.name.set_value('refused'); h.mod.v.name = 'refused';
await h.click('ev-model-1'); await h.click('ev-against-0');
await h.click('ev-run');
await h.idle(40);
assert.ok(h.text().includes('Sealed test set check failed: the holdout changed.'), `their sentence: ${h.text().slice(-300)}`);
assert.equal(posted('/api/backtests/refused/run').length, 0);
routes(/^\/api\/backtests$/, 'plan', 'POST');

// Regression testing: from a saved test, everything is filled in. Pick the new model and run.
h = await mount({ test: 'demo-tuned-vs-bm25' });
const saved = fx('backtest');
t = h.text();
assert.ok(t.includes('Run demo-tuned-vs-bm25 on another model'), 'the title says what this is');
assert.equal(h.mod.v.name, 'demo-tuned-vs-bm25', 'the same name: the run joins that test\'s history');
assert.deepEqual([h.mod.v.against, h.mod.v.models, h.mod.v.on, h.mod.v.queries, h.mod.v.judgments], [saved.arms[0], saved.arms.slice(1), 'saved', saved.queries, saved.judgments]);
assert.equal(h.mod.v.measure, saved.preregistration.primary_metric);
assert.equal(h.mod.inputs.mde.value(), String(saved.preregistration.mde));
// A saved test's depths come with it, so the new model is measured the same way.
{
  const withDepth = await mount({ test: 'deep-test' });
  assert.deepEqual([withDepth.mod.v.depths['ndcg@10'], withDepth.mod.depthInputs['ndcg@10'].value()], ['20', '20'], 'the saved depth fills its field');
  assert.equal(withDepth.mod.v.depths['r@50'], '50', 'the rest stay at the usual depth');
}
assert.ok(t.includes('A test with this name exists. This run joins its history'), 'and says so');
await h.click('ev-model-1'); // the old model off
await h.click('ev-model-2'); // the new one on
await h.click('ev-run');
await h.idle(40);
const again = posted('/api/backtests').at(-1);
assert.deepEqual(again.arms, [saved.arms[0], 'demo-ranker@v1'], 'the same comparison model, the new model');
assert.deepEqual([again.name, again.queries, again.judgments, again.primary, again.mde], ['demo-tuned-vs-bm25', saved.queries, saved.judgments, saved.preregistration.primary_metric, saved.preregistration.mde], 'everything else is the saved test, sealed split and all');
assert.equal(posted('/api/backtests/demo-tuned-vs-bm25/run').length, 1);

// A file dropped on the form is their own questions.
h = await mount();
await h.fire('new-evaluation', 'on_drop', { paths: ['/tmp/drop/pairs.csv'] });
await h.idle(30);
assert.deepEqual([h.mod.v.on, h.mod.v.src, h.mod.v.file], ['own', 'file', '/tmp/drop/pairs.csv']);
assert.equal(posted('/api/testsets/preview').at(-1).source.file, '/tmp/drop/pairs.csv');

assert.ok(!/[←-⇿—]/.test(h.text()), 'no arrows or em dashes');
const worst = Math.max(...h.timings);
assert.ok(worst < 50, `a render took ${worst.toFixed(1)} ms`);
console.log('new evaluation render test: ok');
process.exit(0);
