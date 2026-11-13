// A dataset's Insights tab against a real backend (backend/core) on the demo store: what uses demo-catalog (pipelines,
// runs, an environment, datasets made from it), a saved notebook that names it, who works with it and recent activity.
// A run opens in Runs, a dataset here, a notebook in the Notebooks view.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/insights-live.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { cstBin, tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { bin: cst, why } = cstBin();
if (!cst) { console.log(`insights-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-ins-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'ins', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
// A saved notebook that reads the catalog.
const nb = { nbformat: 4, nbformat_minor: 5, metadata: {}, cells: [{ id: 'a', cell_type: 'code', metadata: {}, source: "df = cst.dataset('demo-catalog')", outputs: [], execution_count: null }] };
await fetch(`http://127.0.0.1:${j.port}/api/notebooks/catalog-checks/save`, { method: 'POST', headers: { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ notebook: nb, message: 't' }) });

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-ins-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 80 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(200); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 700)}`);
};

await until(() => (h.mod.list ?? []).some((d) => d.name === 'demo-catalog'), 'datasets');
h.mod.select(h.cx, 'demo-catalog');
await until(() => h.mod.detail?.name === 'demo-catalog', 'the catalog open');
// "What uses it" is a section of About (it was the Insights tab).
const openInsights = async () => { h.mod.page.open(h.cx, 'insights'); await h.idle(50); };
await openInsights();
assert.equal(h.mod.page.tab, 'about');
// Reads: the page's own Sample data (the app), and an agent at the CLI.
spawnSync(cst, ['data', 'head', 'demo-catalog'], { env: { ...env, CST_AGENT: 'claude' } });
await h.click('dtab-sample');
await until(() => h.mod.page.rows && !h.mod.page.rows.loading && h.mod.page.rows.table.rows.length > 0, 'sample rows read');
await openInsights();
h.mod.page.insights.reset();
h.mod.page.insights.load(h.cx, h.mod.detail);
await until((t) => t.includes('Used by 2 model versions, 2 runs, 1 environment, 3 datasets. Named in 1 notebook. Read 2 times in the last 90 days.'), 'the summary');
assert.ok(h.text().includes('Frequent readers') && h.text().includes('claude (agent, for') && h.text().includes('1 read, as an agent') && h.text().includes('1 read, in the app'), `frequent readers: ${h.text().slice(0, 900)}`);
const t = h.text();
for (const s of ['Model versions built on it', 'demo-bm25@v1', 'Runs scored with it', 'demo-tuned-vs-bm25@v1', 'Environments it reached', 'staging',
  'Datasets made from it', 'demo-queries@v1', 'uses it directly', '2 steps downstream', 'Notebooks that name it', 'catalog-checks', 'named in 1 cell',
  'Who works with it', 'Recent activity', "From Captain's own records"]) assert.ok(t.includes(s), `shows ${s}`);
// A run opens in Comparisons.
globalThis.__runs = [];
await h.click('ins-run-0');
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'runs' }]);
assert.equal(globalThis.__shared['captain.open'].section, 'runs');
// A dataset opens here.
const ds = h.mod.page.insights.data.used_by.dataset[0].name;
await h.click('ins-dataset-0');
await until(() => h.mod.sel === ds, 'the derived dataset open');
// Back to the catalog: a notebook opens in Notebooks.
h.mod.select(h.cx, 'demo-catalog');
await until(() => h.mod.detail?.name === 'demo-catalog', 'the catalog again');
await openInsights();
await until((t) => t.includes('catalog-checks'), 'insights again');
await h.click('ins-nb-0');
// Notebooks is its own view: the row asks it to open that notebook.
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'notebooks' }]);
assert.equal(globalThis.__shared['captain.open'].section, 'notebooks');
assert.equal(globalThis.__shared['captain.open'].notebook, 'catalog-checks');

// The one list: a column's name finds its table and says which column; a folder's name finds what is in it.
await h.click('crumb-tables');
const search = async (q) => { h.mod.catalog.find.set_value(q); h.mod.catalog.load(h.cx); await h.idle(100); };
await search('price');
await until((t) => t.includes('column price') && (h.mod.catalog.rows ?? []).some((r) => r.name === 'demo-catalog' && r.matched_column === 'price'), 'demo-catalog, by its price column');
await search('historic judgments');
await until((t) => t.includes('Captain, folder demo-historic-replay') && h.mod.catalog.rows.length === 1, 'a foldered dataset found, with its folder');
await search('');
await until(() => h.mod.catalog.rows.filter((r) => r.kind === 'dataset').length === h.mod.list.length, 'every dataset, folders aside');
assert.ok(h.mod.catalog.groups.some((g) => g.id === 'folder:logs'), 'each folder is a group');

stop();
console.log('insights-live render test: ok');
process.exit(0);
