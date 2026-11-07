// The import wizard (captain-import) against a real backend (backend/core): the demo store is a source, its tables are
// read, a suggested query previews up to 1,000 rows in the kit grid, and imports into a folder as a dataset. A folder
// of files imports too, with a skipped file's reason.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/import-live.test.mjs
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
if (!cst) { console.log(`import-live render test: skipped (${why})`); process.exit(0); }
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-import-'));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'imp', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed'], { env });
spawnSync(cst, ['data', 'mkdir', 'Shop'], { env });
const drop = path.join(work, 'drop');
fs.mkdirSync(drop);
fs.writeFileSync(path.join(drop, 'rugs.csv'), 'id,title\n1,Rug\n');
fs.writeFileSync(path.join(drop, 'readme.txt'), 'hi\n');

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
const call = async (p, body) => (await fetch(`http://127.0.0.1:${j.port}${p}`, { method: body ? 'POST' : 'GET', headers: { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })).json();
await call('/api/projects', { name: 'Acme' });
const brand = await call('/api/scopes', { type: 'brand', name: 'Acme Shop' });
// The Explorer has the brand picked: the import's "For" defaults to it.
globalThis.__shared['captain.scope'] = { kind: 'scope', id: brand.id, label: 'Acme Shop' };
globalThis.__props = { overlay: 'o1' };

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-import-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-import'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 80 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 700)}`);
};

await until((t) => t.includes('demo-store') && t.includes('A file or folder') && t.includes('Connect a new source') && t.includes('Connect PostHog') && t.includes('Connect Google Analytics 4'), 'sources');
await h.click('src-demo-store');
await until((t) => t.includes("hasn't read this database's tables"), 'unread');
await h.click('read-tables');
// The backend's suggestions (from the read tables), each with why it's worth running.
await until((t) => t.includes('products') && t.includes('Top searches, last 7 days'), 'tables and suggestions');
const i = h.mod.suggestions.findIndex((s) => /from\s+\S*products/i.test(s.sql));
assert.ok(i >= 0, `a suggestion on products: ${h.mod.suggestions.map((s) => s.title).join('; ')}`);
await h.click(`sug-${i}`);
await until((t) => /[\d,]+ rows in \d+ ms/.test(t) && t.includes('Download CSV'), 'preview in the grid');
h.mod.name.set_value('shop-products');
await h.click(`folder-${h.mod.folders.indexOf('Shop')}`);
assert.equal(h.mod.target, 'Shop');
await until((t) => t.includes('Shop/shop-products'), 'saved as, before importing');
globalThis.__toasts = [];
globalThis.__closed = [];
await h.click('import-go');
assert.equal(h.mod.dest.id, brand.id, 'defaults to the Explorer pick');
await until(() => globalThis.__toasts.length > 0, 'imported');
assert.equal(globalThis.__toasts[0].title, 'Imported shop-products, version 1');
assert.match(globalThis.__toasts[0].body, /^[\d,]+ rows, the whole table\. In Shop, for Acme Shop\.$/, globalThis.__toasts[0].body);
assert.equal(globalThis.__closed.length, 1, 'the wizard closed itself');
assert.equal(globalThis.__shared['captain.open'].dataset, 'shop-products', 'Data opens on it');
const scoped = await (await fetch(`http://127.0.0.1:${j.port}/api/datasets`, { headers: { Authorization: `Bearer ${j.token}`, 'X-CST-Scope': `scope:${brand.id}` } })).json();
assert.ok(scoped.some((d) => d.name.endsWith('shop-products')), `the brand's datasets include it: ${JSON.stringify(scoped).slice(0, 200)}`);
const tree = await (await fetch(`http://127.0.0.1:${j.port}/api/datasets/tree`, { headers: { Authorization: `Bearer ${j.token}` } })).json();
const shop = tree.folders.find((f) => f.path === 'Shop');
assert.ok(shop && shop.datasets.some((d) => d.leaf === 'shop-products'), `the dataset is in Shop: ${JSON.stringify(shop)?.slice(0, 200)}`);

await h.click('step-1');
await h.click('src-file');
// This time for the project, which the import itself records.
await h.click(`dest-${h.mod.dests.findIndex((d) => d.kind === 'project' && d.id === 'acme')}`);
h.mod.path.set_value(drop);
await h.click('folder-0');
await h.click('file-go');
await until(() => globalThis.__toasts.length > 1, 'file import');
assert.equal(globalThis.__toasts[1].title, 'Added 1 dataset');
assert.match(globalThis.__toasts[1].body, /Skipped 1: .*isn't a table/, globalThis.__toasts[1].body);
const acme = await call('/api/projects/acme');
assert.ok(JSON.stringify(acme).includes('rugs'), `the project holds the imported file: ${JSON.stringify(acme).slice(0, 300)}`);

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('import-live render test: ok');
process.exit(0);
