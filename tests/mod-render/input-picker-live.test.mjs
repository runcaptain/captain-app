// Choosing a project's input, against a real backend on the demo store: three tabs (a connection, this computer,
// already in Captain); a connection's table, prepared, then "take a snapshot now" or "keep it live"; and each ends by
// saving the input on the project (POST /api/projects/{id}/inputs).
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/input-picker-live.test.mjs
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
if (!cst) { console.log(`input picker render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-picker-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'pk', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
spawnSync(cst, ['tables', 'pull', 'demo-store'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const post = async (p, body) => { const r = await fetch(base + p, { method: 'POST', headers: H, body: JSON.stringify(body) }); assert.ok(r.ok, `${p}: ${r.status} ${await r.clone().text()}`); return r.json(); };
const get = async (p) => (await fetch(base + p, { headers: H })).json();
await post('/api/projects', { name: 'Outdoor', kind: 'search-events' });

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-import-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-import'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const open = async (input) => {
  globalThis.__props = { input: { project: 'outdoor', ...input } };
  const h = harness(Mod);
  await h.start();
  for (let i = 0; i < 40 && !h.mod.conns; i++) await h.idle(100);
  return h;
};
const until = async (h, ok, what, n = 120) => {
  for (let i = 0; i < n && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(150); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 1200)}`);
};

// The three tabs, with the connection's mark and name first.
let h = await open({ slot: 'catalog', label: 'Catalog', kinds: ['catalog'], role: 'catalog' });
let t = h.text();
assert.ok(t.includes('Choose catalog') && t.includes('From a connection') && t.includes('From this computer') && t.includes('Already in Captain'), 'tabs');
assert.ok(t.includes('demo-store'), 'the connection is listed');
// Already in Captain: only datasets that can be a catalog; using one saves it as the input.
await h.click('input-tab-captain');
t = h.text();
assert.ok(t.includes('demo-catalog') && !t.includes('demo-judgments'), 'datasets that fit');
const i = h.mod.datasets.filter((d) => d.kind === 'catalog').findIndex((d) => d.name === 'demo-catalog');
await h.click(`input-ds-${i}`);
await h.click('input-use-dataset');
await until(h, () => !h.mod.busy, 'saved');
let st = await get('/api/projects/outdoor/stages');
const cat = (st.inputs ?? []).find((x) => x.slot === 'catalog');
assert.equal(cat?.pinned, 'demo-catalog@v1', `catalog input: ${JSON.stringify(cat)}`);

// From a connection, kept live: the products table, prepared, then saved as a live input.
h = await open({ slot: 'catalog', label: 'Catalog', kinds: ['catalog'], role: 'catalog' });
await h.click('src-demo-store');
await until(h, () => Boolean(h.mod.tree?.schemas), 'its tables');
const sch = h.mod.tree.schemas.find((s) => (s.tables ?? []).some((x) => x.name === 'products'));
await h.click(`tbl-${sch.name}-products`);
await until(h, () => Boolean(h.mod.prep?.result), 'prepared');
t = h.text();
assert.ok(t.includes('Take a snapshot now') && t.includes('Keep it live'), 'asks snapshot or live');
await h.click('mode-live');
await h.click('input-use-conn');
await until(h, () => !h.mod.busy, 'saved live');
st = await get('/api/projects/outdoor/stages');
const live = (st.inputs ?? []).find((x) => x.slot === 'catalog');
assert.equal(live?.mode, 'live', `live catalog: ${JSON.stringify(live)} note: ${JSON.stringify(h.mod.note)}`);
assert.equal(live?.status, 'live');
const state = await get('/api/projects/outdoor/state');
assert.equal(state.inputs.catalog.source.connection, 'demo-store');
assert.equal(state.inputs.catalog.source.table.split('.').pop(), 'products');
assert.equal(state.pins.catalog, null, 'a live input pins no version');

for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad}`);
console.log('input picker render test: ok');
process.exit(0);
