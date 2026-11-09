// Settings > Connected services and the scope model's one-click start, against a real backend (backend/core) on the
// demo store: every section draws, Check all asks each service, Add compute lists every kind with which are untried,
// a default chip shows, and no secret is drawn.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/services-live.test.mjs
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
if (!cst) { console.log(`services-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-svc-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'svc', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const load = async (mod) => {
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), dir, { recursive: true });
  return (await import(path.join(dir, 'main.js'))).default;
};
const until = async (h, ok, what) => {
  for (let i = 0; i < 120 && !ok(h.text()); i++) await h.idle(150);
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 600)}`);
};

globalThis.__props = {};
const h = harness(await load('captain-services'));
await h.start();
await until(h, (t) => t.includes('Database connections') && t.includes('demo-store'), 'the list');
for (const s of ['Storage', 'Database connections', 'Compute', 'Model registries', 'MLflow', 'Model keys']) assert.ok(h.text().includes(s), s);
assert.ok(h.text().includes('DuckDB'), "a connection's database by name");
assert.ok(h.text().includes('default for backtests'), 'the default chip');
assert.ok(h.text().includes('Backtests run on') && h.text().includes('Pipelines are served on'), 'both pickers');
assert.ok(!/token|password|secret:/i.test(h.text().replace(/no secret is ever shown/i, '')), 'no secret drawn');
await h.click('add-compute-toggle');
await until(h, (t) => t.includes('Not yet tried on a real account'), 'untried kinds say so');
const kinds = h.mod.data.compute.kinds;
assert.ok(kinds.some((k) => k.kind === 'databricks-jobs') && kinds.some((k) => k.kind === 'snowflake-spcs'), 'the warehouse kinds');
for (const k of kinds) assert.ok(h.byIdPrefix(`add-${k.kind}`).length, `Add ${k.kind}`);
await h.click('check-all');
await until(h, (t) => t.includes('Answered'), 'Check all asks each one');
assert.ok(h.mod.checked && !h.mod.checking);

// Scope model: empty, with the suggested start, which isn't saved until Save.
const sm = harness(await load('captain-scope-model'));
await sm.start();
await until(sm, (t) => t.includes('Start with Brand, then Site'), 'the empty model offers a start');
await sm.click('types-suggested');
await until(sm, () => sm.mod.model.types.map((t) => t.key).join() === 'brand,site', 'Brand, then Site');
assert.ok(sm.mod.dirty, 'waits for Save');
stop();
console.log('services-live render test: ok');
process.exit(0);
