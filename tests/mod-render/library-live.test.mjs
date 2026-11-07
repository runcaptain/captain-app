// Data and signals as a directory, against a real backend (this repo's backend/core, or `cst` on PATH) and the real
// kit: no fixtures. An empty workspace shows the drop zone. Finder drops import files (a .txt is skipped with its
// reason), a changed file comes back as a new version, a dataset is dragged onto a folder, and folders are made,
// renamed and removed.
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
await until((t) => t.includes('All datasets') && t.includes('shop | v1'), 'the tree shows the new dataset');

// 3. The same file, changed: a new version, shown with its version chip.
fs.writeFileSync(path.join(drop, 'catalogs', 'shop.csv'), 'id,title,price\n1,Sofa,100\n2,Lamp,20\n3,Rug,55\n');
// Dropped onto its own folder (open the folders first): the same name there, so a new version.
await h.click('folder-drop');
await h.click('folder-drop/catalogs');
await h.fire('folder-drop/catalogs', 'on_drop', { paths: [path.join(drop, 'catalogs', 'shop.csv')] });
await until((t) => /updated 1 \(new version\)/.test(t), 'update summary');
await until((t) => t.includes('shop | v2'), 'the tree shows v2');

// 4. A new folder, then drag the catalog onto it.
await h.click('crumb-root');
assert.ok(h.text().includes('drop/catalogs/shop | v2') || h.mod.detail?.version === 2, 'the detail shows the new version');
await h.click('lib-new');
h.mod.lib.newFolder.set_value('Catalogs');
await h.click('lib-new-name-go');
await until((t) => t.includes('Made Catalogs.'), 'folder made');
const shop = h.mod.list.find((d) => d.name === 'drop/catalogs/shop');
await h.fire(`ds-${shop.name}`, 'on_mouse_down', {});
await h.fire('folder-Catalogs', 'on_mouse_up', {});
await until((t) => t.includes('Moved 1 dataset to Catalogs.'), 'moved by dragging');
const inCatalogs = (h.mod.lib.tree.folders.find((f) => f.path === 'Catalogs')?.datasets ?? []).map((d) => d.leaf);
assert.ok(inCatalogs.includes('shop'), `shop is in Catalogs: ${JSON.stringify(inCatalogs)}`);

// 5. Rename the folder; a new empty one can be removed.
h.mod.lib.cur = 'Catalogs';
await h.click('lib-rename');
h.mod.lib.renameTo.set_value('Product catalogs');
await h.click('lib-rename-name-go');
await until((t) => t.includes('Renamed Catalogs to Product catalogs.'), 'renamed');
h.mod.lib.cur = '';
await h.click('lib-new');
h.mod.lib.newFolder.set_value('Scratch');
await h.click('lib-new-name-go');
await until((t) => t.includes('Made Scratch.'), 'second folder');
await until((t) => t.includes('Remove folder'), 'an empty folder can be removed');
await h.click('lib-remove');
await until((t) => t.includes('Removed Scratch.'), 'removed');

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('library-live render test: ok');
process.exit(0);
