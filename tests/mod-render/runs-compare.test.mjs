// One evaluation run read in depth, in place in Runs (it was the Compare screen), against fixtures from a real
// `cst serve` (demo seed, costs set, and an evaluation with a surface whose guardrail fails). No tabs: one page
// with what limits the result above the verdict, the verdict in one of three words and a sentence, the numbers,
// and four sections that open in place: query by query (paged and sorted by the services, one query side by
// side), by slice, by surface, and where the time goes. A link written for the Compare screen still lands here.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/runs-compare.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-runs-compare');
globalThis.__routes = [
  [/^\/api\/backtests$/, 'backtests'],
  [/^\/api\/(workflows|grading|replays)$/, 'empty'],
  [/^\/api\/(jobs|live)\?/, 'empty'],
  [/\/compatibility$/, 'compat'],
  [/\/histogram/, 'hist'],
  [/\/queries\/[^/?]+\?/, 'query2'],
  [/\/queries\?.*only=regressions/, 'regressions'],
  [/\/queries\?/, 'queries0'],
  [/^\/api\/runs\/[^?]+\?v=/, 'run'],
  [/^\/api\/backtests\/[^?]+$/, 'backtest'],
];
const fx = (n) => JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, `${n}.json`), 'utf8'));
// Something asked for this run by name, with a link written for the Compare screen (a project page from before
// Compare moved into Runs): Runs comes forward and opens it.
globalThis.__runs = [];
globalThis.__shared = { 'captain.open': { section: 'comparisons', backtest: 'surf-e2e', version: 1, at: Date.now() } };

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-runs-cmp-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-runs'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();
for (let i = 0; i < 8 && !(h.mod.sel.backtests === 'surf-e2e' && h.mod.cmp.run && h.mod.cmp.hist); i++) await h.layoutLists({});
const run = fx('run');

// What limits the result comes before the verdict.
let text = h.text();
assert.ok(globalThis.__asked.some((p) => /^\/api\/runs\/surf-e2e\?v=1/.test(p)), 'opened the run that was asked for');
assert.ok(globalThis.__runs.some(([n, a]) => n === 'layout.select' && a.child === 'runs'), 'a link to the Compare screen brings Runs forward');
assert.equal(h.mod.kind, 'backtests');
assert.equal(h.mod.sel.backtests, 'surf-e2e');
assert.ok(text.includes('Read with care') && text.includes('1 slice result rests on fewer than 5 queries.'), 'warnings as sentences');
assert.ok(!/small-slices|\boffline \|/.test(text), 'no warning codes on screen');
assert.ok(text.includes('nDCG@10') && !/\| ndcg@10 \|/.test(text), 'metric names in words');
assert.ok(text.includes('Ask an agent about it') && text.includes('Copy prompt'), 'agent actions');
// The verdict: one of three words, then one sentence with both models, the measure and the evidence.
assert.ok(text.includes('Better') && text.includes('demo-tuned is better than demo-bm25 on nDCG@10. Simulated events.'), `the verdict: ${text.slice(0, 600)}`);
assert.ok(text.indexOf('Read with care') < text.indexOf('demo-tuned is better than'), 'warnings above the verdict');
assert.ok(!/\| (WIN|NOISE|LOSS) \|/.test(text), 'no WIN, NOISE or LOSS badge');
assert.ok(text.includes('WORTH IT'), 'cost flag');
assert.ok(/gain per \$1k a month \+4\.9121/.test(text), 'gain per $1k');
// Shown as the name plus a version chip, never "name@v1".
assert.ok(text.includes(run.arms[1].arm.replace(/@v(\d+)$/, ' | v$1')), 'arm table: name plus version chip');
assert.ok(!text.includes(run.arms[1].arm), 'no raw name@v ref');
assert.equal(h.byIdPrefix('cmp-').filter((e) => /^cmp-\d+$/.test(e.call('id')[0])).length, run.comparisons.length, 'every measure');
assert.equal(h.byIdPrefix('bin-').length, fx('hist').counts.length, 'histogram bins');
// Experiments and Launch review left the screens: the comparison no longer offers either.
assert.ok(!/Record a decision|launch review/i.test(text), 'no decision or launch review buttons');

// No tabs: four sections that open in place. Nothing is asked for a section until it opens.
assert.equal(h.byIdPrefix('tab-').length, 0, 'no tabs');
assert.deepEqual(h.byIdPrefix('csec-').map((e) => e.call('id')[0]).filter((id) => !id.endsWith('-wrap')), ['csec-queries', 'csec-segments', 'csec-surfaces', 'csec-profile']);
assert.ok(!globalThis.__asked.some((p) => /\/queries\?/.test(p)), 'the queries are not asked for until their section opens');

// Queries: paged from the daemon, filtered and sorted there.
await h.click('csec-queries');
for (let i = 0; i < 4; i++) await h.layoutLists({ 'compare-queries': [0, 30] });
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
const loaded = [...h.mod.cmp.q.pages.entries()].flatMap(([pg, rows]) => rows.map((r, i) => [pg * 200 + i, r]));
const at = loaded.find(([, r]) => r.query_id === row.query_id)?.[0] ?? 0;
await h.itemClick('compare-queries', at, { 'compare-queries': [0, 30] });
text = h.text({ 'compare-queries': [0, 30] });
const q2 = fx('query2');
assert.ok(text.includes(q2.a.items[0].title), 'A column');
assert.ok(text.includes('new') && text.includes('dropped'), 'new and dropped markers');
const side = h.byIdPrefix('side-by-side', { 'compare-queries': [0, 30] })[0];
const icons = [];
(function walk(e) { if (e && typeof e === 'object') { if (e.kind === 'svg') icons.push(e.args[0]); for (const k of e.kids ?? []) walk(k); } })(side);
assert.ok(icons.some((p) => /moved-up/.test(p)) && icons.some((p) => /moved-down/.test(p)), 'movement icons both ways');
assert.ok(!/[←-⇿⟰-⟿]/.test(text), 'no Unicode arrows');

// By slice: small ones greyed and marked. By surface: the failed guardrail. Both open under the queries.
await h.click('csec-segments');
text = h.text();
assert.ok(text.includes('Too few queries'), 'too few');
assert.ok(/Better|No clear difference|Worse/.test(text.slice(text.indexOf('By slice'))), 'a slice reads in the same three words');
await h.click('csec-surfaces');
text = h.text();
assert.ok(/failed: related ndcg@10 at least 0\.99|failed: /.test(text), 'failed guardrail');
assert.ok(text.includes('1 GUARDRAIL FAILED'), 'surface badge');

// The Compare screen's tab names still open the matching section (`cst app open comparison:<name> --tab queries`).
h.mod.cmp.sections.clear();
globalThis.__shared['captain.open'] = { section: 'runs', backtest: 'surf-e2e', version: 1, tab: 'segments', at: Date.now() + 1 };
await h.layoutLists({});
assert.ok(h.mod.cmp.sections.has('segments'), 'tab: "segments" opens By slice');

// Another run asked for while this one is open.
globalThis.__shared['captain.open'] = { section: 'runs', backtest: 'demo-tuned-vs-bm25', version: 1, at: Date.now() + 2 };
await h.layoutLists({});
assert.ok(globalThis.__asked.some((p) => /^\/api\/runs\/demo-tuned-vs-bm25\?v=1/.test(p)), 'opened the other run');

const sorted = [...h.timings].sort((a, b) => a - b);
console.log(`renders: ${h.timings.length}, median ${sorted[Math.floor(sorted.length / 2)].toFixed(2)} ms, slowest ${sorted.at(-1).toFixed(1)} ms (V8)`);
assert.ok(sorted.at(-1) < 50, 'a render took 50 ms or more');
console.log('runs-compare render test: ok');
