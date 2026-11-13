// Render test for the captain-runs mod against fixtures from a real `cst serve` (demo seed, plus one workflow that
// finished and one that failed). Runs has two submenus. This drives the first: the one list of everything that runs,
// with its Kind column and filters, and what each kind opens to: an offline evaluation with its comparison in
// place, a job, a workflow's steps and a failed run's reasons, rating by key and publishing, and the off-policy
// curves. regression.test.mjs drives the second (Compare), and runs-compare.test.mjs the comparison in depth.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/captain-runs.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';
import { metricWords } from '../../defaults/captain/kit/words.js';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-runs');
globalThis.__routes = [
  [/^\/api\/backtests\/[^?]+\/run$/, 'job-start'],
  [/^\/api\/workflows\/[^?]+\/run$/, 'job-start'],
  [/^\/api\/jobs\?/, 'jobs-list'],
  [/^\/api\/jobs\/[^/]+\/logs/, 'job-logs'],
  [/^\/api\/jobs\//, 'job'],
  [/^\/api\/live\?/, 'live'],
  [/\/compatibility$/, 'empty'],
  [/\/histogram/, 'empty'],
  [/^\/api\/backtests$/, 'backtests'],
  [/^\/api\/backtests\/[^?]+$/, 'backtest'],
  [/^\/api\/runs\//, 'run'],
  [/^\/api\/workflows$/, 'workflows'],
  [/gate-demo\/runs$/, 'wf-runs-failed'],
  [/\/runs$/, 'wf-runs-done'],
  [/^\/api\/workflows\/[^?]+$/, 'workflow'],
  [/^\/api\/grading$/, 'grading'],
  [/\/next$/, 'grading-next'],
  [/\/grade$/, 'graded'],
  [/\/publish$/, 'published'],
  [/^\/api\/replays$/, 'replays'],
];
const fx = (n) => JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, `${n}.json`), 'utf8'));

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-runs-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-runs'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();

const { KINDS } = await import(path.join(work, 'main.js'));
const { VERDICT_WORDS } = await import(path.join(work, 'kit/verdict.js'));

// Two submenus and no strip of six tabs.
assert.deepEqual(h.byIdPrefix('submenu-').map((e) => e.call('id')[0]), ['submenu-runs', 'submenu-compare']);
assert.equal(h.byIdPrefix('tab-').length, 0, 'no tabs besides the two submenus');

// One list: every kind of thing that runs, each row with its kind in words and one status in words.
let text = h.text();
for (const head of ['Name', 'Kind', 'Status']) assert.ok(text.includes(head), `column ${head}`);
for (const b of fx('backtests')) assert.ok(text.includes(b.name), `lists the evaluation ${b.name}`);
for (const w of fx('workflows')) assert.ok(text.includes(w.name), `lists the workflow ${w.name}`);
assert.ok(text.includes('demo-grading') && text.includes(fx('replays')[0].name), 'lists the rating queue and the off-policy evaluation');
assert.ok(text.includes('Run offline evaluation demo-tuned-vs-bm25') || h.byIdPrefix('job-job-eval-1').length === 1, 'lists the jobs, in words');
for (const [, word] of KINDS) assert.ok(text.includes(word), `the Kind column says ${word}`);
for (const status of ['2 runs', '0 of 98 rated', 'Last run failed', 'Failed', 'Finished']) assert.ok(text.includes(status), `a status in words: ${status}`);
assert.equal(h.byIdPrefix('backtests-').length, fx('backtests').length);
assert.equal(h.byIdPrefix('job-job-').length, fx('jobs-list').length);
for (const row of [...h.byIdPrefix('backtests-'), ...h.byIdPrefix('job-job-'), ...h.byIdPrefix('workflows-')]) assert.ok(h.has(row, 'svg'), `icon on ${row.call('id')[0]}`);
// What is running now leads the list.
const order = h.byIdPrefix('').map((e) => String(e.call('id')?.[0] ?? '')).filter((id) => /^(live|backtests|job|workflows|grading|replays)-/.test(id));
assert.equal(order[0], 'live-live-1', `the run that is going is first: ${order.slice(0, 3)}`);
assert.ok(text.includes('Running, 42%'), 'and says how far it is');

// A kind the workspace has none of gets no filter chip.
{
  const had = h.mod.data.replays;
  h.mod.data.replays = [];
  await h.layoutLists({});
  h.mod.data.replays = [];
  assert.equal(h.byIdPrefix('runs-kind-replays').length, 0, 'no Off-policy chip with no off-policy evaluations');
  assert.equal(h.byIdPrefix('runs-kind-backtests').length, 1);
  h.mod.data.replays = had;
}

// The filters keep one kind.
await h.click('runs-kind-jobs');
assert.equal(h.byIdPrefix('backtests-').length, 0, 'only jobs');
assert.equal(h.byIdPrefix('job-job-').length, 2);
await h.click('runs-kind-backtests');
assert.equal(h.byIdPrefix('job-job-').length, 0, 'only evaluations');
await h.click('runs-kind-all');

// The first evaluation is open beside the list, with its comparison in place: the verdict in one of three words
// and one sentence, never the old WIN, NOISE or LOSS badge.
const run = fx('run');
const primary = run.comparisons.find((c) => c.primary);
text = h.text();
const word = h.byIdPrefix('cmp-verdict-word')[0];
assert.ok(word, 'a verdict word');
assert.ok(text.includes(VERDICT_WORDS[primary.verdict]), `the verdict reads ${VERDICT_WORDS[primary.verdict]}`);
assert.ok(text.includes('demo-tuned is better than demo-bm25 on nDCG@10. Simulated events.'), `the verdict sentence: ${text.slice(0, 900)}`);
assert.ok(!/\| (WIN|NOISE|LOSS) \|/.test(text), 'no WIN, NOISE or LOSS badge');
assert.ok(text.includes(`${metricWords(primary.metric)}, B minus A`), 'the measure, in words, beside it');
// Shown as the name plus a version chip, never "name@v1".
assert.ok(text.includes(run.arms[0].arm.replace(/@v(\d+)$/, ' | v$1')), 'arm table: name plus version chip');
assert.ok(!text.includes(run.arms[0].arm), 'no raw name@v ref');
assert.ok(text.includes(Number(run.arms[0].offline['ndcg@10']).toFixed(4)), 'arm nDCG');
assert.equal(h.byIdPrefix('cmp-').filter((e) => /^cmp-\d+$/.test(e.call('id')[0])).length, run.comparisons.length, 'every measure has a row');
// Nothing sends a person to another screen for the comparison.
assert.equal(h.byIdPrefix('open-compare').length, 0, 'no "Compare in detail" button');
assert.ok(!(globalThis.__runs ?? []).some(([n, a]) => n === 'layout.select' && a.child === 'comparisons'), 'nothing selects a Compare screen');
// One filled button on the screen: New evaluation.
assert.ok(text.includes('New evaluation'), 'New evaluation');

// Run backtest is a job; its line follows the daemon's status.
await h.click('run-backtest');
for (const f of [0, 1]) await h.layoutLists({});
text = h.text();
assert.ok(/Offline evaluation: done/.test(text), 'job finished');

// A workflow, from the list: the steps with command icons, and a failed run's reasons.
await h.click('workflows-gate-demo');
text = h.text();
assert.ok(text.includes('gate-demo') && text.includes('simulated-backtest'), 'workflows listed');
assert.ok(text.includes('at 07:00 UTC on Monday'), 'schedule in plain English');
for (const s of h.byIdPrefix('step-')) assert.ok(h.has(s, 'svg'), `icon on step ${s.call('id')[0]}`);
await h.click('wfrun-1');
text = h.text();
assert.ok(text.includes("no environment 'demo-staging'"), "the failed step's error");
assert.ok(text.includes('an earlier step failed'), "a skipped step's reason");

// A rating queue, from the list: progress, then the rater, rated by key.
await h.click('grading-demo-grading');
text = h.text();
assert.ok(/0 of 98 pairs/.test(text), 'queue progress');
await h.click('grade-demo-grading');
text = h.text();
assert.ok(text.includes(fx('grading-next').pair.query), 'the query, large');
const grader = h.byIdPrefix('grader')[0];
grader.call('on_key_down')[0]({ key: '2' }, h.cx);
await h.layoutLists({});
assert.ok(/1 rated now/.test(h.text()), 'key 2 graded the pair');
assert.ok(globalThis.__asked.some((p) => /\/grade$/.test(p)), 'POST grade');
// The daemon now counts one grade.
globalThis.__routes.find((r) => r[1] === 'grading')[1] = 'grading-1';
await h.click('grader-back');
await h.click('publish-demo-grading');
assert.ok(h.text().includes('Published demo-grading-human@v1: judgments graded by people.'), 'published');
// Carried on: the new grades can become a project's labels, the pin form pre-filled.
globalThis.__overlays = [];
await h.click('use-labels-demo-grading');
assert.deepEqual(globalThis.__overlays.at(-1)?.props?.values?.ref, 'demo-grading-human@v1');
assert.equal(globalThis.__overlays.at(-1)?.props?.task, 'pin');

// An off-policy evaluation, from the list: a line per outcome (dashed when directional), the legend, the points.
await h.click(`replays-${fx('replays')[0].name}`);
text = h.text();
const rep = fx('replays')[0];
assert.ok(text.includes('directional: which way, not how much'), 'legend');

// A job, from the list: where it ran, how it ended, and its log.
await h.click('job-job-train-2');
await h.idle(30);
assert.ok(h.text().includes('The training command exited with code 1.') && h.text().includes('AWS Batch, us-west-1'), `the job: ${h.text().slice(-400)}`);
await h.click(`replays-${rep.name}`);
const paths = [];
(function walk(e) {
  if (!e || typeof e !== 'object') return;
  if (e.kind === 'path') paths.push(e.args[0]);
  for (const k of e.kids ?? []) walk(k);
})(h.byIdPrefix('replay-detail')[0]);
const lines = paths.filter((p) => p.width === 2);
assert.equal(lines.length, rep.outcomes.length, 'a line per outcome');
assert.ok(lines.some((p) => p.ops.some(([m]) => m === 'dash_array')), 'a dashed directional line');
assert.equal(h.byIdPrefix('pt-').length, rep.curve.length, 'every operating point');

// Beside an open terminal column the pane is narrow: the list alone, then a row's page in its place with a crumb back.
h.mod.width = 600;
h.mod.opened1 = false;
await h.layoutLists({});
assert.ok(h.byIdPrefix('runs-list').length === 1 && h.byIdPrefix('replay-detail').length === 0, 'narrow: the list alone');
await h.click('backtests-demo-tuned-vs-bm25');
await h.layoutLists({});
assert.ok(h.byIdPrefix('runs-list').length === 0 && h.byIdPrefix('backtest-detail').length === 1, 'a row opens its page in place of the list');
assert.ok(h.text().includes('demo-tuned-vs-bm25'), 'the crumb names it');
await h.click('crumb-runs');
assert.equal(h.byIdPrefix('runs-list').length, 1, 'the crumb goes back to the list');
h.mod.width = 1000;
await h.layoutLists({});
assert.ok(h.byIdPrefix('runs-list').length === 1 && h.byIdPrefix('backtest-detail').length === 1, 'wide: the list and the page side by side');

const sorted = [...h.timings].sort((a, b) => a - b);
console.log(`renders: ${h.timings.length}, median ${sorted[Math.floor(sorted.length / 2)].toFixed(2)} ms, slowest ${sorted.at(-1).toFixed(1)} ms (V8)`);
assert.ok(sorted.at(-1) < 50, 'a render took 50 ms or more');
console.log('captain-runs render test: ok');
