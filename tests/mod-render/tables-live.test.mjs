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
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
// A query through Captain, so the table has insights.
spawnSync(cst, ['sql', 'demo-store', '-e', 'select category, count(*) as n from products group by 1'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__props = { tab: 'sources' };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-tables-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 60 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 700)}`);
};

// Sources: the connection's card says its tables haven't been read, and reads them.
await until((t) => t.includes('demo-store') && t.includes("Its tables haven't been read yet"), 'the connection, not read yet');
// Until then the one list has the datasets only, and says the database is waiting to be read.
assert.ok(!(h.mod.catalog.rows ?? []).some((r) => r.kind === 'table'), 'no database tables listed before the read');
await h.click('pull-demo-store');
await until((t) => /Read 2 tables in 1 schema of demo-store\./.test(t), 'read');
await until((t) => t.includes('2 tables, last read'), 'the card says how many and when');

// Tables: the database's tables are in the one list with the datasets, each saying where it lives.
await h.click('see-demo-store');
await until(() => h.mod.tab === 'tables' && (h.mod.catalog.rows ?? []).filter((r) => r.kind === 'table').length === 2, 'its two tables are listed');
// The database is a group of its own, open because "See its tables" asked for it: its name, its kind, a count.
assert.ok(h.mod.catalog.isOpen('db:demo-store'), 'its group is open');
let listed = h.text();
assert.ok(listed.includes('demo-store') && listed.includes('2 tables') && listed.includes('products') && listed.includes('search_log') && !listed.includes('"'), `the group: ${listed.slice(0, 500)}`);
assert.ok(listed.includes('Read where it lives'), 'Captain keeps no versions of a database table');
assert.ok(h.mod.catalog.groups.some((g) => g.id === 'kept'), 'the tables Captain keeps are one group beside it');

// A row opens the table's page: Rows first, then About (its columns and how it is queried).
await h.click(`cat-row-table-demo-store/${String(h.mod.catalog.rows.find((r) => r.name === 'products').schema).replace(/"/g, '')}/products`);
await until(() => !!h.mod.tables.page?.columns, 'the table page');
assert.deepEqual(h.byIdPrefix('ttab-').map((e) => e.call('id')[0]), ['ttab-rows', 'ttab-about'], 'two views');
assert.ok(h.text().includes('Read where it lives: Captain keeps no versions of it.'), 'what it is, in one line');
await h.click('ttab-about');
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

await h.click('ttab-about');
await until((t) => t.includes('Who queries it') && t.includes('Example queries') && t.includes('Open in a notebook'), 'insights');
await h.click('ex-nb-0');
// Notebooks is its own view: Data hands it the query, and it starts a notebook with that query as its first cell.
const asked = globalThis.__shared['captain.open'];
assert.equal(asked.section, 'notebooks');
assert.ok(asked.seedSql.sql.includes('count(*)') && asked.seedSql.conn === 'demo-store' && asked.seedSql.name === 'products-query', JSON.stringify(asked));
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'notebooks' }]);
{
  const nbDir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-notebooks-seed-')));
  fs.cpSync(path.join(root, 'defaults/mods/captain-notebooks'), nbDir, { recursive: true });
  const { default: NbMod } = await import(path.join(nbDir, 'main.js'));
  globalThis.__shared['captain.open'] = { ...asked, at: Date.now() };
  const n = harness(NbMod);
  await n.start();
  for (let i = 0; i < 80 && !(n.mod.nb.cells[0]?.source?.includes('count(*)')); i++) await n.layoutLists({});
  assert.ok(n.mod.nb.cells[0]?.source?.includes('count(*)') && n.mod.nb.cells[0]?.meta?.conn === 'demo-store', `the example query as its first cell: ${n.text().slice(0, 300)}`);
  assert.ok(n.text().includes('products-query'), 'named after the table');
}

// The list's search finds the table by a column.
await h.click('crumb-tables');
h.mod.catalog.find.set_value('price');
h.mod.catalog.load(h.cx);
await until(() => (h.mod.catalog.rows ?? []).some((r) => r.kind === 'table' && r.name === 'products' && r.matched_column === 'price'), 'search finds the table by a column');

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('tables-live render test: ok');
process.exit(0);
