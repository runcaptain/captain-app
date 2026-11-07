// Data and signals, Tables tab, against a real backend (backend/core, or `cst` on PATH) on the demo store's DuckDB:
// read a connection's tables, open the schema, a table's columns, sample rows ordered by a column, search, and an
// example query (logged by running it) that opens as the first cell of a new notebook.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/tables-live.test.mjs
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
if (!cst) { console.log(`tables-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-tables-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'tables', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed'], { env });
// A query through Captain, so the table has insights.
spawnSync(cst, ['sql', 'demo-store', '-e', 'select category, count(*) as n from products group by 1'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__props = { tab: 'tables' };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-tables-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 60 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 700)}`);
};

await until((t) => t.includes('demo-store') && t.includes("Its tables haven't been read yet"), 'the connection, not read yet');
await h.click('pull-demo-store');
await until((t) => /Read 2 tables in 1 schema of demo-store\./.test(t), 'read');
await until((t) => t.includes('products') && t.includes('search_log') && /[a-z]+-store\.main/.test(t) && !t.includes('"'), 'schema tree, schema in words');
const tableId = h.mod.tables.trees.get('demo-store').schemas[0].name;
await h.click(`table-demo-store/${tableId}/products`);
await until((t) => t.includes('Columns 8') && t.includes('price') && /double|float/.test(t), 'columns');

// Rows: the whole table, paged, sorted on the services by a header click.
await h.click('ttab-rows');
await until((t) => /SELECT \* FROM/.test(t) && /1 to 100 of 480/.test(t), 'every row, paged');
const price = h.mod.tables.rows.table.columns.findIndex((c) => c.name === 'price');
await h.click(`gh-table-rows-${price}`);
await until((t) => /ORDER BY "?price"?/.test(t), 'ordered');
await h.click(`gh-table-rows-${price}`);
await until((t) => /ORDER BY "?price"? DESC/.test(t) && t.includes('Order by price, largest first'), 'ordered, largest first');
await h.click('gnext-table-rows');
await until((t) => /101 to 200 of 480/.test(t), 'next page');
assert.ok(h.text().includes('Download CSV'), 'CSV export from the grid');

await h.click('ttab-insights');
await until((t) => t.includes('Who queries it') && t.includes('Example queries') && t.includes('Open in a notebook'), 'insights');
await h.click('ex-nb-0');
await until((t) => t.includes('Notebooks') && t.includes('products-query'), 'a new notebook');
await until(() => h.mod.nb.cells[0]?.source?.includes('count(*)') && h.mod.nb.cells[0]?.meta?.conn === 'demo-store', 'the example query as its first cell');

h.mod.tab = 'tables';
h.mod.tables.search.set_value('price');
h.mod.tables.find(h.cx);
await until((t) => /demo-store, [a-z]+-store\.main/.test(t) && t.includes('products'), 'search finds the table by a column');

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('tables-live render test: ok');
process.exit(0);
