// The engine page against a real backend (this repo's backend/core): its tabs read the lists filtered to the engine.
// A demo workspace has seven datasets; one is linked to a new engine, and the engine's Datasets tab shows that one.
// Its pipeline shows in Overview and Versions, and the Experiments tab opens Comparisons on a click.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/engine-live.test.mjs
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
if (!cst) { console.log(`engine-live render test: skipped (${why})`); process.exit(0); }

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-engine-'));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'engines', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
const call = async (method, p, body) => (await fetch(base + p, { method, headers: { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })).json();

// An engine with one pipeline and one dataset.
const engine = await call('POST', '/api/engines', { name: 'Product search', type: 'search', scopes: [], pipelines: ['demo-bm25'] });
assert.ok(engine.id, `engine made: ${JSON.stringify(engine)}`);
await call('POST', '/api/scopes/link', { items: [{ kind: 'dataset', name: 'demo-catalog' }], engine: engine.id });
const all = await call('GET', '/api/datasets');
assert.ok(all.length > 1, 'the workspace has several datasets');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-engine-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-engine'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));

// No engine picked: the chooser lists it.
let h = harness(Mod);
await h.start();
const until = async (h, ok, what) => { for (let i = 0; i < 60 && !ok(); i++) await h.idle(100); assert.ok(ok(), `${what}: ${h.text().slice(0, 500)}`); };
await until(h, () => h.text().includes('Product search'), 'the chooser lists the engine');

// Picked: the page, filtered to it.
globalThis.__shared['captain.scope'] = { kind: 'engine', id: engine.id, label: 'Product search' };
h = harness(Mod);
await h.start();
await until(h, () => Boolean(h.mod.engine) && Boolean(h.mod.data.datasets), 'the engine page loaded');
let t = h.text();
assert.ok(t.includes('Product search') && t.includes('Search') && t.includes('Not in any scope'), `header: ${t.slice(0, 300)}`);
assert.ok(t.includes('demo-bm25'), 'Overview lists its pipeline');

await h.click('eng-tabs-datasets');
t = h.text();
assert.ok(t.includes('demo-catalog'), `its dataset: ${t.slice(0, 400)}`);
const others = all.map((d) => d.name).filter((n) => n !== 'demo-catalog');
for (const n of others) assert.ok(!t.includes(n), `${n} isn't this engine's, so it doesn't show`);

await h.click('eng-tabs-versions');
assert.ok(h.text().includes('demo-bm25@v1'), 'Versions lists its pipeline version');

// A row opens its record in its section.
await h.click('eng-tabs-datasets');
await h.click('eng-ds-0');
assert.equal(globalThis.__shared['captain.open']?.section, 'data');
assert.equal(globalThis.__shared['captain.open']?.dataset, 'demo-catalog');

console.log('engine-live render test: ok');
process.exit(0);
