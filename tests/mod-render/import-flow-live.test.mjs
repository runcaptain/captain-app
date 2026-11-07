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
const preview = async (w, sql) => {
  await w.click('write-own');
  w.mod.sql.set_value(sql);
  await w.click('preview-run');
  await until(w, (t) => /[\d,]+ rows in \d+ ms/.test(t), 'preview');
};

let w = await wizard();
await preview(w, 'select * from products limit 1000');
w.mod.name.set_value('Hello World');
await until(w, (t) => t.includes('imports/hello-world') && t.includes('(from "Hello World")'), 'the name and folder it will get');
await w.click('import-go');
await until(w, () => globalThis.__closed.length > 0, 'the wizard closes itself');
const toast = globalThis.__toasts[0];
assert.equal(toast.title, 'Imported hello-world, version 1');
assert.match(toast.body, /^480 rows, the whole table\. In imports\.$/, toast.body);
assert.equal(toast.action.name, 'layout.select');
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'data' }], 'Data and signals comes forward');
// Data opens on it: the Datasets tab, the dataset selected, its folder open and current.
await until(data, () => data.mod.tab === 'datasets' && data.mod.sel === 'hello-world' && data.mod.lib.cur === 'imports', 'selected in the tree');
assert.ok(data.mod.lib.open.has('imports'), 'its folder is open');
assert.ok(data.text().includes('hello-world'), 'shown in the tree');

// ---- 3. The same name for another query: said, with a fresh name offered --------------------------------------------
w = await wizard();
await preview(w, 'select * from products where 1 = 0');
w.mod.name.set_value('Hello World');
await until(w, (t) => t.includes('hello-world already exists (version 1, from demo-store: select * from products limit 1000)'), 'the clash');
assert.ok(w.text().includes('Name it hello-world-2') && w.text().includes('Import as version 2'), 'both ways out');
await w.click('clash-rename');
assert.equal(w.mod.name.value(), 'hello-world-2');

// ---- 4. No rows: asked first -------------------------------------------------------------------------------------
await until(w, (t) => !t.includes('already exists'), 'the clash is gone');
await w.click('import-go');
await until(w, (t) => t.includes('This query returned no rows. Import anyway?'), 'asked before an empty import');
assert.equal(globalThis.__closed.length, 0, 'nothing imported yet');
w.mod.sql.set_value("select * from events where ts > current_date - interval 7 day and 1 = 0");
await until(w, (t) => t.includes('recent time window'), 'a time window is explained');
w.mod.sql.set_value('select * from products where 1 = 0');
await w.click('import-go');
await until(w, () => globalThis.__closed.length > 0, 'imported anyway');
assert.match(globalThis.__toasts[0].title, /^Imported hello-world-2, version 1$/);
assert.match(globalThis.__toasts[0].body, /^0 rows/);

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('import-flow-live render test: ok');
process.exit(0);
