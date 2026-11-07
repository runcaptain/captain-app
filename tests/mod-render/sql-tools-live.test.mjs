// A notebook SQL cell's tools against a real backend (backend/core) on the demo store's DuckDB: format (upper and
// lower case), explain, the {{name}} values (a cell with an empty one won't run, and its value saves in the notebook),
// translate to Snowflake, save, find, insert and delete a snippet, and query history (a failed query shows as failed).
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/sql-tools-live.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { cstBin } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { bin: cst, why } = cstBin();
if (!cst) { console.log(`sql-tools-live render test: skipped (${why})`); process.exit(0); }

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-sqlt-'));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'sqlt', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed'], { env });
spawnSync(cst, ['sql', 'demo-store', '-e', 'select count(*) from products'], { env });
spawnSync(cst, ['sql', 'demo-store', '-e', 'select nope from products'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__props = { tab: 'notebooks' };

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-sqlt-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 60 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 900)}`);
};
const nb = () => h.mod.nb;

await until(() => nb().conns.length > 0, 'connections loaded');
nb().newName.set_value('tools');
await h.click('nb-new-go');
await until(() => nb().cells.length > 0 && nb().editors.size > 0, 'a new notebook with a SQL cell');
const cell = nb().cells[0];
const id = cell.id;
cell.meta.conn = 'demo-store';
const ed = () => nb().editors.get(id);
ed().set_value("select category, count(*) as n from products where category <> '{{skip}}' group by category");
cell.source = ed().value();

// 1. The tools open under the cell. Format: keywords upper case, then lower case; literals untouched.
await h.click(`tools-${id}`);
await until((t) => t.includes('Format, lower case') && t.includes('Snippets') && t.includes('History'), 'the tools row');
ed().set_value('select category from products where title = \'Big Sofa\'');
await h.click(`sqlt-fmt-${id}`);
await until(() => ed().value().startsWith('SELECT') && ed().value().includes("'Big Sofa'"), 'formatted upper case');
await h.click(`sqlt-low-${id}`);
await until(() => ed().value().startsWith('select') && ed().value().includes('\nfrom products'), 'formatted lower case');
assert.equal(cell.source, ed().value(), 'the cell keeps the formatted text');

// 2. Explain: the plan, nothing run.
await h.click(`sqlt-explain-${id}`);
await until((t) => t.includes('The plan from demo-store. Nothing was run.'), 'the plan');
assert.ok(nb().sqlTools.out.plan.length > 20, 'a real plan');

// 3. {{name}} values: an empty one stops the run with a sentence; filling it saves in the notebook.
ed().set_value("select count(*) as n from products where category <> '{{skip}}' and {{day}} = {{day}}");
cell.source = ed().value();
await h.click(`run-${id}`);
await until((t) => t.includes('Give {{skip}}, {{day}} a value first'), 'a missing value stops the run');
await h.click(`sqlt-filled-${id}`);
await until((t) => t.includes('Still empty: {{skip}}, {{day}}'), 'the values panel');
nb().sqlTools.varInputs.get('skip').set_value('Bags');
nb().sqlTools.varInputs.get('skip').handlers.change({}, h.cx);
nb().sqlTools.varInputs.get('day').set_value("'{{today}}'");
nb().sqlTools.varInputs.get('day').handlers.change({}, h.cx);
await h.idle(50);
assert.ok(h.text().includes("category <> 'Bags'"), 'the filled-in query');
assert.deepEqual(nb().open.base.metadata.captain.vars, { skip: 'Bags', day: "'{{today}}'" }, 'values saved with the notebook');
assert.ok(!h.text().includes('Still empty'), 'nothing left empty');
assert.ok(!nb().sqlTools.varInputs.has('today'), '{{today}} is built in, no input for it');
// Explain uses the filled-in query.
await h.click(`sqlt-explain-${id}`);
await until((t) => t.includes('The plan from demo-store'), 'explain with values');

// 4. Translate to Snowflake, then use it.
ed().set_value('select ifnull(title, \'none\') as t from products limit 3');
await h.click(`sqlt-tr-${id}`);
await h.click(`sqlt-to-${id}-snowflake`);
await until((t) => t.includes('Use the Snowflake version'), 'translated');
await h.click(`sqlt-use-${id}`);
await until((t) => t.includes('This cell is now Snowflake SQL'), 'used');
assert.ok(/COALESCE|IFNULL/i.test(ed().value()) && /LIMIT 3/.test(ed().value()), `snowflake text: ${ed().value()}`);

// 5. Snippets: save this cell, find it, insert it into another cell's text, delete it.
ed().set_value('select * from products where category = {{category}}');
await h.click(`sqlt-save-${id}`);
nb().sqlTools.title.set_value('Products in a category');
await until((t) => t.includes('Inserting it asks for {{category}}'), 'save panel names the values');
await h.click(`sqlt-save-go-${id}`);
await until((t) => t.includes('Saved "Products in a category"'), 'snippet saved');
ed().set_value('');
await h.click(`sqlt-snip-${id}`);
await until((t) => t.includes('Products in a category') && t.includes('Insert'), 'snippet listed');
nb().sqlTools.find.set_value('nothing like it');
await until((t) => t.includes('No snippet matches.'), 'find narrows');
nb().sqlTools.find.set_value('category');
await until((t) => t.includes('Products in a category'), 'found again');
await h.click('sqlt-insert-0');
await until((t) => t.includes('Inserted "Products in a category". Fill {{category}}'), 'inserted');
assert.equal(ed().value(), 'select * from products where category = {{category}}');
await h.click('sqlt-del-0');
await until((t) => t.includes('Deleted "Products in a category".'), 'deleted');

// 6. History on this connection: the failed query shows as failed, Use puts it in the cell.
await h.click(`sqlt-hist-${id}`);
await until((t) => t.includes('select nope from products') && t.includes('failed'), 'history with a failed query');
assert.ok(h.text().includes('select count(*) from products'), 'the earlier query');
const k = nb().sqlTools.history.findIndex((x) => x.sql === 'select count(*) from products');
await h.click(`sqlt-reuse-${k}`);
await until(() => ed().value() === 'select count(*) from products', 'history query in the cell');

// 7. Closing the tools hides them.
await h.click(`tools-${id}`);
assert.ok(!h.text().includes('Format, lower case'), 'tools closed');
console.log('sql-tools-live render test: ok');
process.exit(0);
