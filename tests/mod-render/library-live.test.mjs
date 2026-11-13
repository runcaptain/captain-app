// Bringing files into Data, against a real backend (this repo's backend/core, or `cst` on PATH) and the real kit: no
// fixtures. An empty workspace shows the drop zone. Finder drops import files (a .txt is skipped with its reason)
// and they join the one list of tables. A changed file dropped on its dataset's page comes back as a new version.
// A dataset moves to a folder from its page, and the list's "Where it lives" says so.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/library-live.test.mjs
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
if (!cst) { console.log(`library-live render test: skipped (${why})`); process.exit(0); }

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-lib-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'lib', '--storage', path.join(work, 'store')], { env });
const drop = path.join(work, 'drop');
fs.mkdirSync(path.join(drop, 'catalogs'), { recursive: true });
fs.writeFileSync(path.join(drop, 'catalogs', 'shop.csv'), 'id,title,price\n1,Sofa,100\n2,Lamp,20\n');
fs.writeFileSync(path.join(drop, 'queries.csv'), 'query\nsofa\nlamp\n');
fs.writeFileSync(path.join(drop, 'notes.txt'), 'not a table\n');

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-lib-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 60 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 600)}`);
};

// 1. An empty workspace: one big drop zone, the demo store only as a small link.
await until((t) => t.includes('Drag your catalogs, queries, judgments and logs here'), 'empty drop zone');
assert.ok(h.text().includes('or try the demo store'), 'demo store link');

// 2. Drop a folder from Finder: two tables in, the .txt skipped with its reason.
await h.fire('lib-empty', 'on_drop', { paths: [drop] });
await until((t) => /Added 2, 1 skipped\./.test(t), 'import summary');
let t = h.text();
assert.ok(t.includes("isn't a table"), `skipped reason: ${t.slice(0, 400)}`);
assert.ok(t.includes('shop') && t.includes('queries'), 'imported rows listed');
await until((t) => t.includes('drop/catalogs/shop') && t.includes('Captain, folder drop/catalogs') && t.includes('Captain keeps 1 version'), 'the one list shows the new dataset, its folder and that Captain keeps versions of it');

// 3. The same file, changed, dropped on the dataset's own page: it lands in its folder, so it is a new version.
fs.writeFileSync(path.join(drop, 'catalogs', 'shop.csv'), 'id,title,price\n1,Sofa,100\n2,Lamp,20\n3,Rug,55\n');
const row = () => h.mod.catalog.rows.findIndex((r) => r.name === 'drop/catalogs/shop');
await h.click(`cat-row-${row()}`);
await until(() => h.mod.detail?.name === 'drop/catalogs/shop', 'its page');
assert.equal(h.mod.page.folder(), 'drop/catalogs');
await h.fire('dataset-page', 'on_drop', { paths: [path.join(drop, 'catalogs', 'shop.csv')] });
await until((t) => /updated 1 \(new version\)/.test(t), 'update summary');
await until((t) => t.includes('This is version 2 of 2'), 'the page shows version 2');

// 4. Move it to a new folder, from its page.
await h.click('dtab-about');
assert.ok(h.text().includes('Captain keeps it, in the folder drop/catalogs.'), 'where it lives');
await h.click('ds-move');
await until(() => Array.isArray(h.mod.page.folders), 'the folders there are');
h.mod.page.newFolder.set_value('Catalogs');
await h.click('ds-move-new-go');
await until((t) => t.includes('Moved to Catalogs.'), 'moved');
await until((t) => t.includes('Captain keeps it, in the folder Catalogs.'), 'the page says its new folder');
await h.click('crumb-tables');
await until((t) => t.includes('Captain, folder Catalogs') && t.includes('Captain keeps 2 versions'), 'the list says where it lives now, and that there are two versions');

// 5. And back to the top level, by picking it.
await h.click(`cat-row-${row()}`);
await until(() => h.mod.detail?.name === 'drop/catalogs/shop', 'its page again');
await h.click('dtab-about');
await h.click('ds-move');
await until(() => (h.mod.page.folders ?? []).includes('Catalogs'), 'Catalogs is offered');
await h.click('move-to-0');
await until((t) => t.includes('Moved to the top level.'), 'moved to the top level');

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('library-live render test: ok');
process.exit(0);
