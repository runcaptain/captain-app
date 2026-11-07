// Render test for the captain-runs mod against fixtures from a real `cst serve` (demo seed, plus one workflow that
// finished and one that failed). It drives each tab: a backtest and its run, Run backtest as a job, a workflow's
// steps and a failed run's reasons, grading by key, publishing, and the replay curves.
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
  [/^\/api\/jobs\//, 'job'],
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

// Backtests: the list with an icon per row, and the newest run of the first one.
let text = h.text();
for (const b of fx('backtests')) assert.ok(text.includes(b.name), `lists ${b.name}`);
for (const row of h.byIdPrefix('backtests-')) assert.ok(h.has(row, 'svg'), 'icon on a backtest row');
const run = fx('run');
const primary = run.comparisons.find((c) => c.primary);
assert.ok(text.includes(`${metricWords(primary.metric)}, B minus A`), 'primary comparison, in words');
assert.ok(text.includes(primary.verdict.toUpperCase()), 'verdict badge');
// Shown as the name plus a version chip, never "name@v1".
assert.ok(text.includes(run.arms[0].arm.replace(/@v(\d+)$/, ' | v$1')), 'arm table: name plus version chip');
assert.ok(!text.includes(run.arms[0].arm), 'no raw name@v ref');
assert.ok(text.includes(Number(run.arms[0].offline['ndcg@10']).toFixed(4)), 'arm nDCG');

// Compare in detail hands the run to Comparisons.
await h.click('open-compare');
assert.equal(globalThis.__shared['captain.compare'].backtest, 'demo-historic-replay');
assert.ok(globalThis.__runs.some(([n, a]) => n === 'layout.select' && a.child === 'comparisons'), 'switches to Comparisons');

// Run backtest is a job; its line follows the daemon's status.
await h.click('run-backtest');
for (const f of [0, 1]) await h.layoutLists({});
text = h.text();
assert.ok(/Backtest: done/.test(text), 'job finished');

// Workflows: status icons, the steps with command icons, and a failed run's reasons.
await h.click('tab-workflows');
text = h.text();
assert.ok(text.includes('gate-demo') && text.includes('simulated-backtest'), 'workflows listed');
assert.ok(text.includes('at 07:00 UTC on Monday'), 'schedule in plain English');
for (const s of h.byIdPrefix('step-')) assert.ok(h.has(s, 'svg'), `icon on step ${s.call('id')[0]}`);
await h.click('wfrun-1');
text = h.text();
assert.ok(text.includes("no environment 'demo-staging'"), "the failed step's error");
assert.ok(text.includes('an earlier step failed'), "a skipped step's reason");

// Grading: progress, then the grader, graded by key.
await h.click('tab-grading');
text = h.text();
assert.ok(/0 of 98 pairs/.test(text), 'queue progress');
await h.click('grade-demo-grading');
text = h.text();
assert.ok(text.includes(fx('grading-next').pair.query), 'the query, large');
const grader = h.byIdPrefix('grader')[0];
grader.call('on_key_down')[0]({ key: '2' }, h.cx);
await h.layoutLists({});
assert.ok(/1 graded now/.test(h.text()), 'key 2 graded the pair');
assert.ok(globalThis.__asked.some((p) => /\/grade$/.test(p)), 'POST grade');
// The daemon now counts one grade.
globalThis.__routes.find((r) => r[1] === 'grading')[1] = 'grading-1';
await h.click('grader-back');
await h.click('publish-demo-grading');
assert.ok(h.text().includes('Published demo-grading-human@v1: HUMAN_GRADED'), 'published');

// Replays: a line per outcome (dashed when directional), the legend, and the points.
await h.click('tab-replays');
text = h.text();
const rep = fx('replays')[0];
assert.ok(text.includes('directional: which way, not how much'), 'legend');
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

const sorted = [...h.timings].sort((a, b) => a - b);
console.log(`renders: ${h.timings.length}, median ${sorted[Math.floor(sorted.length / 2)].toFixed(2)} ms, slowest ${sorted.at(-1).toFixed(1)} ms (V8)`);
assert.ok(sorted.at(-1) < 50, 'a render took 50 ms or more');
console.log('captain-runs render test: ok');
