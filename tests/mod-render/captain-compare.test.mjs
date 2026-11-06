// Render test for the captain-compare mod against fixtures from a real `cst serve` (demo seed, costs set, and a
// backtest with a surface whose guardrail fails). It drives the overview (warnings above the winner, cost, every
// comparison, the histogram), the paged query table with its filters and sort, one query side by side, the
// segments, the surfaces, and a run handed over from Runs.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/captain-compare.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-compare');
globalThis.__routes = [
  [/^\/api\/backtests$/, 'backtests'],
  [/\/compatibility$/, 'compat'],
  [/\/histogram/, 'hist'],
  [/\/queries\/[^/?]+\?/, 'query2'],
  [/\/queries\?.*only=regressions/, 'regressions'],
  [/\/queries\?/, 'queries0'],
  [/^\/api\/runs\/[^?]+\?v=/, 'run'],
];
const fx = (n) => JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, `${n}.json`), 'utf8'));
// Runs handed this run over.
globalThis.__shared = { 'captain.compare': { backtest: 'surf-e2e', version: 1 } };

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-compare-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-compare'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();
const run = fx('run');

// Overview: what limits the result comes before the winner.
let text = h.text();
assert.ok(globalThis.__asked.some((p) => /^\/api\/runs\/surf-e2e\?v=1/.test(p)), 'opened the handed-over run');
assert.ok(text.includes('Read with care') && text.includes('small-slices'), 'warnings');
assert.ok(text.indexOf('Read with care') < text.indexOf('B minus A'), 'warnings above the winner');
assert.ok(text.includes('WORTH IT'), 'cost flag');
assert.ok(/gain per \$1k a month \+4\.9121/.test(text), 'gain per $1k');
// Shown as the name plus a version chip, never "name@v1".
assert.ok(text.includes(run.arms[1].arm.replace(/@v(\d+)$/, ' | v$1')), 'arm table: name plus version chip');
assert.ok(!text.includes(run.arms[1].arm), 'no raw name@v ref');
assert.equal(h.byIdPrefix('cmp-').length, run.comparisons.length, 'every comparison');
assert.equal(h.byIdPrefix('bin-').length, fx('hist').counts.length, 'histogram bins');

// Queries: paged from the daemon, filtered and sorted there.
await h.click('tab-queries');
await h.layoutLists({ 'compare-queries': [0, 30] });
text = h.text({ 'compare-queries': [0, 30] });
assert.ok(text.includes(fx('queries0').rows[0].query), 'first query row');
assert.ok(text.includes('127 of 127 queries'), 'totals');
for (const r of h.byIdPrefix('q-', { 'compare-queries': [0, 30] })) assert.ok(h.has(r, 'svg'), 'icon on a query row');
await h.click('only-regressions');
await h.layoutLists({ 'compare-queries': [0, 30] });
assert.ok(globalThis.__asked.some((p) => /only=regressions/.test(p)), 'regressions asked of the daemon');
assert.ok(h.text({ 'compare-queries': [0, 30] }).includes('4 of 127 queries'), 'filtered total');
await h.click('qcol-ndcg_a');
await h.layoutLists({ 'compare-queries': [0, 30] });
assert.ok(globalThis.__asked.some((p) => /sort=ndcg_a/.test(p) && /desc=true/.test(p)), 'sorted on the daemon');

// One query side by side: grade chips, movement icons, new and dropped.
const row = fx('regressions').rows[0];
await h.click(`q-${row.query_id}`, { 'compare-queries': [0, 30] });
text = h.text({ 'compare-queries': [0, 30] });
const q2 = fx('query2');
assert.ok(text.includes(q2.a.items[0].title), 'A column');
assert.ok(text.includes('new') && text.includes('dropped'), 'new and dropped markers');
const side = h.byIdPrefix('side-by-side', { 'compare-queries': [0, 30] })[0];
const icons = [];
(function walk(e) { if (e && typeof e === 'object') { if (e.kind === 'svg') icons.push(e.args[0]); for (const k of e.kids ?? []) walk(k); } })(side);
assert.ok(icons.some((p) => /moved-up/.test(p)) && icons.some((p) => /moved-down/.test(p)), 'movement icons both ways');
assert.ok(!/[←-⇿⟰-⟿]/.test(text), 'no Unicode arrows');

// Segments: small ones greyed and marked too few. Surfaces: the failed guardrail.
await h.click('tab-segments');
text = h.text();
assert.ok(text.includes('too few'), 'too few');
await h.click('tab-surfaces');
text = h.text();
assert.ok(/failed: related ndcg@10 at least 0\.99|failed: /.test(text), 'failed guardrail');
assert.ok(text.includes('1 GUARDRAIL FAILED'), 'surface badge');

// Runs hands over another run while this copy is open.
globalThis.__shared['captain.compare'] = { backtest: 'demo-tuned-vs-bm25', version: 1 };
await h.layoutLists({});
assert.ok(globalThis.__asked.some((p) => /^\/api\/runs\/demo-tuned-vs-bm25\?v=1/.test(p)), 'followed the hand-over');

const sorted = [...h.timings].sort((a, b) => a - b);
console.log(`renders: ${h.timings.length}, median ${sorted[Math.floor(sorted.length / 2)].toFixed(2)} ms, slowest ${sorted.at(-1).toFixed(1)} ms (V8)`);
assert.ok(sorted.at(-1) < 50, 'a render took 50 ms or more');
console.log('captain-compare render test: ok');
