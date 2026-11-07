// Data and signals' dataset page (Unity Catalog's table page shape), against fixtures: the tabs, Details (versions,
// provenance, the SQL that made it, files), Lineage drawn from /api/lineage when /api/lineage/graph isn't there, and
// Quality from /api/profile, which hides its tab when the backend has no profile route.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/dataset-page.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data');
const routes = (profile) => [
  [/^\/api\/dataset-classes/, 'classes'],
  [/\/rows\?/, 'rows0'],
  [/^\/api\/datasets\/tree$/, 'tree'],
  [/^\/api\/datasets(\?|$)/, 'datasets'],
  [/^\/api\/datasets\/[^?]+(\?v=\d+)?$/, 'detail'],
  [/^\/api\/lineage\/graph/, 'no-route'],
  [/^\/api\/lineage$/, 'lineage'],
  [/^\/api\/profile/, profile],
  [/^\/api\/labels$/, 'labels'],
];
async function mount(profile) {
  globalThis.__routes = routes(profile);
  globalThis.__asked = [];
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-page-'));
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
for (const tab of ['Overview', 'Sample data', 'Details', 'Lineage', 'Quality']) assert.ok(t.includes(tab), `tab ${tab}`);
assert.ok(t.includes('ROWS') && t.includes('price'), 'overview: facts and schema');

await h.click('dtab-details');
t = h.text();
assert.ok(t.includes('Versions') && t.includes('v1 latest'), 'versions');
assert.ok(t.includes('A SQL query') && t.includes('demo-store') && t.includes('select * from products') && t.includes('Copy SQL'), 'provenance and its SQL');
assert.ok(/Files 1/.test(t) && t.includes('data/part-0.parquet'), 'files');

await h.click('dtab-lineage');
await h.idle();
t = h.text();
assert.ok(globalThis.__asked.some((p) => p.startsWith('/api/lineage/graph')) && globalThis.__asked.includes('/api/lineage'), 'falls back to /api/lineage');
for (const n of ['demo-store', 'demo-catalog', 'demo-tuned', 'demo-tuned-vs-bm25']) assert.ok(t.includes(n), `lineage node ${n}`);
assert.ok(t.includes('Upstream') && t.includes('Trace a column') && !/demo-catalog@v1/.test(t), 'controls; versions as chips, not @v');

await h.click('dtab-quality');
await h.idle();
t = h.text();
assert.ok(t.includes('480 rows profiled') && t.includes('25%') && t.includes('402') && t.includes('499'), `quality: ${t.slice(-400)}`);

const h2 = await mount('no-route');
await h2.click('dtab-quality');
await h2.idle();
assert.ok(!h2.text().includes('Quality') || h2.text().includes('Quality needs a newer Captain backend.'), 'no profile route: says so');
await h2.click('dtab-overview');
assert.ok(!h2.text().split(' | ').includes('Quality'), 'and the tab hides');
console.log('dataset-page render test: ok');
