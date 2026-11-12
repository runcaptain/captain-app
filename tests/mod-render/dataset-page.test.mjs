// Data and signals' dataset page (Unity Catalog's table page shape), against fixtures: the tabs, Details (versions,
// provenance, the SQL that made it, files), Lineage drawn from /api/lineage when /api/lineage/graph isn't there, and
// Quality from /api/profile, which hides its tab when the backend has no profile route.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/dataset-page.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data');
const routes = (profile) => [
  [/^\/api\/dataset-classes/, 'classes'],
  [/\/rows\?/, 'rows0'],
  [/^\/api\/data\/tables/, 'data-tables'],
  [/^\/api\/datasets(\?|$)/, 'datasets'],
  [/^\/api\/datasets\/[^?]+(\?v=\d+)?$/, 'detail'],
  [/^\/api\/lineage\/graph/, 'no-route'],
  [/^\/api\/lineage$/, 'lineage'],
  [/^\/api\/profile/, profile],
];
async function mount(profile) {
  globalThis.__routes = routes(profile);
  globalThis.__asked = [];
  globalThis.__props = { dataset: 'demo-catalog' }; // its page, opened by name: the screen starts on the list of tables
  const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-page-')));
  fs.cpSync(path.join(root, 'defaults/mods/captain-data'), work, { recursive: true });
  fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
  fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
  const { default: Mod } = await import(path.join(work, 'main.js'));
  const h = harness(Mod);
  await h.start();
  await h.idle();
  return h;
}

const h = await mount('profile');
let t = h.text();
// Two views and a button: no strip of six tabs.
assert.deepEqual(h.byIdPrefix('dtab-').map((e) => e.call('id')[0]), ['dtab-sample', 'dtab-about']);
for (const gone of ['Overview', 'Sample data', 'Details', 'Insights']) assert.ok(!t.split(' | ').includes(gone), `no ${gone} tab`);
assert.ok(t.includes('Where this came from'), 'lineage is a button');

// About: what was Overview and Details, on one page.
await h.click('dtab-about');
t = h.text();
assert.ok(t.includes('ROWS') && t.includes('price'), 'about: facts and schema');
assert.ok(t.includes('Versions') && t.includes('v1 latest'), 'versions');
assert.ok(t.includes('A SQL query') && t.includes('demo-store') && t.includes('select * from products') && t.includes('Copy SQL'), 'what made it, and its SQL');
assert.ok(/Files 1/.test(t) && t.includes('data/part-0.parquet'), 'files');

// Quality is a section of About: it loads when opened.
assert.ok(!globalThis.__asked.some((p) => p.startsWith('/api/profile')), 'not asked for yet');
await h.click('dsec-quality');
await h.idle();
t = h.text();
assert.ok(t.includes('480 rows profiled') && t.includes('25%') && t.includes('402') && t.includes('499'), `quality: ${t.slice(-400)}`);

// "Where this came from" opens the graph, and "Back to the table" leaves it.
await h.click('ds-lineage-open');
await h.idle();
t = h.text();
assert.ok(globalThis.__asked.some((p) => p.startsWith('/api/lineage/graph')) && globalThis.__asked.includes('/api/lineage'), 'falls back to /api/lineage');
for (const n of ['demo-store', 'demo-catalog', 'demo-tuned', 'demo-tuned-vs-bm25']) assert.ok(t.includes(n), `lineage node ${n}`);
assert.ok(!/demo-catalog@v1/.test(t), 'versions as chips, not @v');
// The graph draws arrows with arrowheads, as paths.
{
  const kinds = [];
  (function walk(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'path') kinds.push(e.args[0].kind); for (const k of e.kids ?? []) walk(k); })(h.byIdPrefix('ds-lineage-canvas')[0]);
  assert.ok(kinds.includes('stroke') && kinds.includes('fill'), `lines and arrowheads: ${kinds}`);
}
// Clicking a box opens it: an evaluation opens in Runs, with its comparison.
const evalBox = h.byIdPrefix('ds-lineage-node-').map((e) => e.call('id')[0]).find((id) => /node-(backtest|run):/.test(id));
assert.ok(evalBox, 'an evaluation is on the graph');
globalThis.__runs = [];
await h.click(evalBox);
assert.equal(globalThis.__shared['captain.open']?.section, 'runs', 'the evaluation opens in Runs');
assert.ok(globalThis.__shared['captain.open'].backtest, JSON.stringify(globalThis.__shared['captain.open']));
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'runs' }]);
// A model version has no page in Data: its details show on the graph instead.
const modelBox = h.byIdPrefix('ds-lineage-node-').map((e) => e.call('id')[0]).find((id) => /node-pipeline:/.test(id));
await h.click(modelBox);
assert.equal(h.byIdPrefix('ds-lineage-details').length >= 1, true, 'its details show');
await h.click('ds-lineage-close');
assert.ok(h.byIdPrefix('ds-lineage-open').length === 1, 'back on the table');

// An earlier tab's name still opens the right place (`cst app open dataset:<name> --tab insights`, an older link).
h.mod.page.open(h.cx, 'insights');
assert.equal(h.mod.page.tab, 'about');
assert.ok(h.mod.page.sections.has('insights'), 'the section is open');
h.mod.page.open(h.cx, 'overview');
assert.equal(h.mod.page.tab, 'about');
h.mod.page.open(h.cx, 'sample');
assert.equal(h.mod.page.tab, 'sample');

const h2 = await mount('no-route');
await h2.click('dtab-about');
await h2.click('dsec-quality');
await h2.idle();
assert.ok(h2.text().includes('Quality needs a newer Captain backend.') || h2.byIdPrefix('dsec-quality').length === 0, 'no profile route: says so, then the section hides');
await h2.idle();
assert.equal(h2.byIdPrefix('dsec-quality').length, 0, 'and the section is gone');
console.log('dataset-page render test: ok');
