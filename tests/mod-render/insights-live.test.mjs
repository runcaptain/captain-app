// A dataset's Insights tab against a real backend (backend/core) on the demo store: what uses demo-catalog (pipelines,
// runs, an environment, datasets made from it), a saved notebook that names it, who works with it and recent activity.
// A run opens in Comparisons, a dataset here, a notebook in the Notebooks tab.
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
spawnSync(cst, ['demo', 'seed'], { env });

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
await h.click('dtab-insights');
// Reads: the page's own Sample data (the app), and an agent at the CLI.
spawnSync(cst, ['data', 'head', 'demo-catalog'], { env: { ...env, CST_AGENT: 'claude' } });
await h.click('dtab-sample');
await until(() => h.mod.page.rows && !h.mod.page.rows.loading && h.mod.page.rows.table.rows.length > 0, 'sample rows read');
await h.click('dtab-insights');
h.mod.page.insights.reset();
h.mod.page.insights.load(h.cx, h.mod.detail);
await until((t) => t.includes('Used by 2 pipelines, 2 runs, 1 environment, 3 datasets. Named in 1 notebook. Read 2 times in the last 90 days.'), 'the summary');
assert.ok(h.text().includes('Frequent readers') && h.text().includes('claude (agent, for') && h.text().includes('1 read, as an agent') && h.text().includes('1 read, in the app'), `frequent readers: ${h.text().slice(0, 900)}`);
const t = h.text();
for (const s of ['Pipelines built on it', 'demo-bm25@v1', 'Runs scored with it', 'demo-tuned-vs-bm25@v1', 'Environments it reached', 'staging',
  'Datasets made from it', 'demo-queries@v1', 'uses it directly', '2 steps downstream', 'Notebooks that name it', 'catalog-checks', 'named in 1 cell',
  'Who works with it', 'Recent activity', "From Captain's own records"]) assert.ok(t.includes(s), `shows ${s}`);
// A run opens in Comparisons.
globalThis.__runs = [];
await h.click('ins-run-0');
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'comparisons' }]);
assert.equal(globalThis.__shared['captain.open'].section, 'comparisons');
// A dataset opens here.
const ds = h.mod.page.insights.data.used_by.dataset[0].name;
await h.click('ins-dataset-0');
await until(() => h.mod.sel === ds, 'the derived dataset open');
// Back to the catalog: a notebook opens in Notebooks.
h.mod.select(h.cx, 'demo-catalog');
await until(() => h.mod.detail?.name === 'demo-catalog', 'the catalog again');
await h.click('dtab-insights');
await until((t) => t.includes('catalog-checks'), 'insights again');
await h.click('ins-nb-0');
await until(() => h.mod.tab === 'notebooks' && h.mod.nb.open?.name === 'catalog-checks', 'the notebook open');

stop();
console.log('insights-live render test: ok');
process.exit(0);
