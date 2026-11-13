// One table, three screens, the same rows: against a real backend (backend/core) on the demo store. The import wizard
// prepares the products table for training (a catalog: nothing to clean) and imports it, then Tables' Rows tab and the
// new dataset's Sample data show it. All three use the kit grid on the one row shape, so the first 100 rows match
// cell for cell, with the same column kinds and the same total.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/same-rows-live.test.mjs
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
if (!cst) { console.log(`same-rows-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-same-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'same', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
spawnSync(cst, ['tables', 'pull', 'demo-store'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const copy = (mod) => { const d = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-`))); fs.cpSync(path.join(root, 'defaults/mods', mod), d, { recursive: true }); return d; };
const { default: Data } = await import(path.join(copy('captain-data'), 'main.js'));
const { default: Import } = await import(path.join(copy('captain-import'), 'main.js'));
const until = async (h, ok, what) => {
  for (let i = 0; i < 100 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(200); }
  assert.ok(ok(h.text()), `${typeof what === 'function' ? what() : what}: ${h.text().slice(0, 700)}`);
};
// The same order on every screen: by id, through the grid's own header sort (the services sort the whole table).
const byId = async (h, grid) => {
  const j = grid.table.columns.findIndex((c) => c.name === 'id');
  grid.toggleSort(h.cx, j);
  await until(h, () => !grid.loading && grid.order?.name === 'id' && grid.table.rows.length > 0, 'sorted by id');
  return { columns: grid.table.columns.map((c) => [c.name, c.kind]), types: grid.types, rows: grid.firstPage().rows, of: grid.page.total };
};

// 1. The wizard: pick the products table, see it whole, import it.
globalThis.__props = { overlay: 'o1' };
globalThis.__toasts = [];
globalThis.__closed = [];
const w = harness(Import);
await w.start();
await until(w, (t) => t.includes('demo-store'), 'sources');
await w.click('src-demo-store');
await until(w, () => (w.mod.tree?.schemas ?? []).some((s) => (s.tables ?? []).some((x) => x.name === 'products')), 'tables listed');
const schema = w.mod.tree.schemas.find((s) => s.tables.some((x) => x.name === 'products')).name;
await w.click(`tbl-${schema}-products`);
await until(w, (t) => t.includes('480 rows in, 480 kept') && t.includes('Import for training') && !w.mod.prep.loading, 'prepared for training');
// The cleaned preview keeps the table's order, which is by id.
const g0 = w.mod.prep.grid;
const fromWizard = { columns: g0.table.columns.map((c) => [c.name, c.kind]), types: g0.types, rows: g0.firstPage().rows, of: w.mod.prep.result.report.rows_out };
w.mod.name.set_value('products-copy');
await until(w, (t) => t.includes('products-copy'), 'named');
await w.click('import-go');
await until(w, () => globalThis.__toasts.length > 0, 'imported');
assert.match(globalThis.__toasts[0].body, /^480 rows in, 480 kept/, globalThis.__toasts[0].body);
assert.equal(globalThis.__toasts[0].title, 'Imported products-copy, version 1');

// 2. Tables: the same database table's Rows. (The wizard's "open this dataset" was for a Data already running: drop it.)
delete globalThis.__shared['captain.open'];
globalThis.__props = { tab: 'tables' };
const data = harness(Data);
await data.start();
await until(data, () => (data.mod.catalog.rows ?? []).some((r) => r.kind === 'table' && r.name === 'products'), 'the database table is in the one list');
data.mod.catalog.openRow(data.cx, data.mod.catalog.rows.find((r) => r.kind === 'table' && r.name === 'products'));
await until(data, () => !!data.mod.tables.page?.columns, 'the table page');
assert.equal(data.mod.tables.sel.schema, schema);
await data.click('ttab-rows');
await until(data, () => data.mod.tables.rows && !data.mod.tables.rows.loading && data.mod.tables.rows.table.rows.length > 0, 'Rows tab');
await until(data, (t) => /1 to 100 of 480/.test(t), () => `Rows pager: ${JSON.stringify(data.mod.tables.rows.page)}`);
const fromTables = await byId(data, data.mod.tables.rows);

// 3. The dataset it became: its Rows.
globalThis.__shared['captain.open'] = { section: 'data', dataset: 'products-copy', fresh: true, at: Date.now() };
await until(data, () => data.mod.sel === 'products-copy' && !!data.mod.detail?.version, 'the dataset open');
await until(data, (t) => t.includes('A table Captain keeps versions of'), () => `the dataset page (tab ${data.mod.tab}, detail ${JSON.stringify(data.mod.detail)?.slice(0, 200)})`);
await data.click('dtab-sample');
await until(data, () => data.mod.page.rows && !data.mod.page.rows.loading && data.mod.page.rows.table.rows.length > 0, 'Sample data');
const fromDataset = await byId(data, data.mod.page.rows);

// Cell for cell, kind for kind.
// The wizard's cleaned rows are exactly what was saved. Preparing puts the role's fields first (id, title,
// description, ...), so Tables' raw rows hold the same values with the columns in the table's own order.
assert.deepEqual(fromDataset.columns, fromWizard.columns, 'the dataset and the wizard: the same columns and kinds');
assert.deepEqual(fromDataset.types, fromWizard.types, 'formatted the same');
for (let i = 0; i < fromWizard.rows.length; i++) assert.deepEqual(fromDataset.rows[i], fromWizard.rows[i], `dataset row ${i + 1}`);
const byName = (x) => x.rows.map((r) => Object.fromEntries(x.columns.map(([n], j) => [n, r[j]])));
const kinds = (x) => Object.fromEntries(x.columns);
assert.deepEqual(kinds(fromTables), kinds(fromWizard), 'Tables: the same column kinds');
assert.deepEqual(byName(fromTables), byName(fromWizard), 'Tables: the same rows, cell for cell by column');
assert.equal(fromWizard.rows.length, 100);
assert.deepEqual([fromWizard.of, fromTables.of, fromDataset.of], [480, 480, 480], 'the same total everywhere');

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('same-rows-live render test: ok');
process.exit(0);
