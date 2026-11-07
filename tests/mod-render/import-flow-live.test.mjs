// Edgar's two bugs, against a real backend (backend/core): Data and signals and the import wizard run side by side.
// 1. Tables opened before any database was connected: connecting one (the forms' signal) lists it at once, and one
//    connected without the signal shows the next time the tab is opened. Never a stale "No database connected".
// 2. An import closes the wizard, posts a toast, and Data opens on the dataset, selected in its folder. "Hello World"
//    is saved as hello-world, in the kind's folder. A name already used by another query says so and offers a fresh
//    name. A query with no rows asks first. Fewer rows than the limit says it's the whole table.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/import-flow-live.test.mjs
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
if (!cst) { console.log(`import-flow-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-flow-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'flow', '--storage', path.join(work, 'store')], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
// A GET straight to the backend, asked once more if an idle connection was reset under it (as kit/cst.js does).
const get = async (p) => { const go = () => fetch(`http://127.0.0.1:${j.port}${p}`, { headers: { Authorization: `Bearer ${j.token}` } }); let r; try { r = await go(); } catch { r = await go(); } return r.json(); };
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const copy = (mod) => { const d = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-`))); fs.cpSync(path.join(root, 'defaults/mods', mod), d, { recursive: true }); return d; };
const { default: Data } = await import(path.join(copy('captain-data'), 'main.js'));
const { default: Import } = await import(path.join(copy('captain-import'), 'main.js'));
const until = async (h, ok, what, others = []) => {
  for (let i = 0; i < 80 && !ok(h.text()); i++) { for (const o of [h, ...others]) { await o.layoutLists({}); await o.idle(200); } }
  assert.ok(ok(h.text()), `${typeof what === 'function' ? what() : what}: ${h.text().slice(0, 900)}`);
};

// ---- 1. Tables, opened with nothing connected ----------------------------------------------------------------------
globalThis.__props = { tab: 'tables' };
const data = harness(Data);
await data.start();
await until(data, (t) => t.includes('No database connected'), 'nothing connected yet');
// Connected from the forms: they save, then signal. Tables lists it without a restart.
spawnSync(cst, ['demo', 'seed'], { env });
globalThis.__shared['captain.connections.changed'] = Date.now();
await until(data, (t) => t.includes('demo-store') && !t.includes('No database connected'), () => `Tables lists the new connection (state: ${JSON.stringify({ seq: data.mod.tables.loadSeq, fresh: data.mod.tables.fresh, conns: data.mod.tables.conns, sig: globalThis.__shared['captain.connections.changed'] })})`);
// Connected with no signal (another app, the command line): opening the tab again relists.
const db2 = path.join(work, 'two.duckdb');
spawnSync(path.join(path.dirname(cst), 'python'), ['-c', `import duckdb; c=duckdb.connect(${JSON.stringify(db2)}); c.execute("create table t as select 1 as a"); c.close()`]);
spawnSync(cst, ['conn', 'add', 'second-db', '--engine', 'duckdb', '--url', `duckdb:///${db2}`], { env });
const listed = await get('/api/connections');
if (listed.some((k) => k.name === 'second-db')) {
  await data.click('tab-datasets');
  await data.click('tab-tables');
  await until(data, (t) => t.includes('second-db'), 'opening the tab relists');
} else assert.fail(`second-db wasn't added: ${JSON.stringify(listed)}`);

// ---- 2. Import "Hello World" ---------------------------------------------------------------------------------------
const wizard = async () => {
  globalThis.__props = { overlay: `o${Date.now()}` };
  globalThis.__closed = [];
  globalThis.__toasts = [];
  const w = harness(Import);
  await w.start();
  await until(w, (t) => t.includes('demo-store'), 'sources');
  await w.click('src-demo-store');
  await until(w, (t) => t.includes('Suggested queries') && (t.includes("hasn't read") || t.includes('products')), 'step 2');
  if (w.text().includes("hasn't read")) { await w.click('read-tables'); await until(w, (t) => t.includes('Top searches'), 'suggestions'); }
  return w;
};
// Step 3 prepares the query for training: the services' guess of what it is, the column mapping, the cleaned rows.
const preview = async (w, sql) => {
  await w.click('write-own');
  w.mod.sql.set_value(sql);
  await w.click('preview-run');
  await until(w, (t) => / kept/.test(t) && !w.mod.prep.loading, 'prepared');
};

let w = await wizard();
await preview(w, 'select * from products limit 1000');
assert.equal(w.mod.prep.role, 'catalog', 'the services guess a catalog');
w.mod.name.set_value('Hello World');
await until(w, (t) => t.includes('catalogs/hello-world') && t.includes('(from "Hello World")'), 'the name and folder it will get');
await w.click('import-go');
await until(w, () => globalThis.__closed.length > 0, 'the wizard closes itself');
const toast = globalThis.__toasts[0];
assert.equal(toast.title, 'Imported catalogs/hello-world, version 1');
assert.match(toast.body, /^480 rows in, 480 kept: .*Ready for training as catalog .*\. In catalogs\.$/, toast.body);
assert.equal(toast.action.name, 'layout.select');
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'data' }], 'Data and signals comes forward');
// Data opens on it: the Datasets tab, the dataset selected, its folder open and current.
await until(data, () => data.mod.tab === 'datasets' && data.mod.sel === 'catalogs/hello-world' && data.mod.lib.cur === 'catalogs', 'selected in the tree');
assert.ok(data.mod.lib.open.has('catalogs'), 'its folder is open');

// ---- 3. The same name for other data: said, with a fresh name offered ---------------------------------------------
w = await wizard();
await preview(w, "select * from products where category = 'Camping'");
w.mod.name.set_value('Hello World');
await until(w, (t) => t.includes('catalogs/hello-world already exists (version 1, from demo-store'), 'the clash');
assert.ok(w.text().includes('Name it hello-world-2') && w.text().includes('Import as version 2'), 'both ways out');
await w.click('clash-rename');
assert.equal(w.mod.name.value(), 'hello-world-2');
await until(w, (t) => !t.includes('already exists'), 'the clash is gone');

// ---- 4. What it is, which column is which, and the cleaning ----------------------------------------------------------
// Relevance labels need query_id, product_id and grade: unmapped ones are red, and Import waits.
await w.click('role-judgments');
await until(w, () => !w.mod.prep.loading && w.mod.prep.role === 'judgments' && w.mod.prep.result?.role === 'judgments', 'role changed');
let t = w.text();
assert.ok(t.includes('needs') && t.includes('required'), `the missing columns, in words: ${t.slice(0, 300)}`);
assert.ok(w.mod.blocker, 'Import waits for them');
// Pick product_id from the column list.
await w.click('map-btn-product_id');
const idIdx = w.mod.prep.result.columns.indexOf('id');
await w.click(`map-product_id-c${idIdx}`);
await until(w, () => !w.mod.prep.loading && w.mod.prep.mapping.product_id === 'id' && w.mod.prep.result.mapping.product_id === 'id', 'mapped');
assert.ok(w.text().includes('id becomes product_id'), 'the report says what was renamed');
// Back to a catalog; dedupe off, and the report drops that step.
await w.click('role-catalog');
await until(w, () => !w.mod.prep.loading && w.mod.prep.result?.role === 'catalog' && !w.mod.blocker, 'a catalog again');
assert.ok(w.text().includes('duplicate id keep their first row'), 'dedupe is on by default');
await w.click('clean-dedupe');
await until(w, () => !w.mod.prep.loading && w.mod.prep.options.dedupe === false && !w.text().includes('duplicate id keep their first row'), 'dedupe off');

// ---- 5. No rows: Import waits, and a time window is explained -------------------------------------------------------
w.mod.sql.set_value("select * from products where 1 = 0 and current_date - interval 7 day < current_date");
await w.click('preview-run');
await until(w, (t) => t.includes('Cleaning left no rows') && t.includes('recent time window'), 'nothing to import, and why');
assert.equal(globalThis.__closed.length, 0, 'nothing imported');

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('import-flow-live render test: ok');
process.exit(0);
