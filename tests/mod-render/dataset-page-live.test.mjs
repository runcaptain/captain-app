// The dataset page against a real backend (backend/core) on the demo store: Lineage from /api/lineage/graph (the
// warehouse table the SQL read, upstream of demo-catalog), a traced column back to its table column, and Quality
// from /api/profile.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/dataset-page-live.test.mjs
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
if (!cst) { console.log(`dataset-page-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-page-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'page', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
spawnSync(cst, ['tables', 'pull', 'demo-store'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-page-live-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 60 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 900)}`);
};
await until(() => h.mod.detail && !h.mod.detail.error, 'a dataset open');
h.mod.select(h.cx, 'demo-catalog');
await until(() => h.mod.detail?.name === 'demo-catalog', 'demo-catalog open');

await h.click('dtab-lineage');
await until((t) => t.includes('Trace a column') && t.includes('products'), 'lineage: the warehouse table it was read from');
await h.click('ds-lineage-col-price');
await until((t) => /price comes from .*products\.price/.test(t), 'the column traced to its table column');

await h.click('dtab-quality');
await until((t) => /480 rows profiled/.test(t) && t.includes('price'), 'quality');
stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('dataset-page-live render test: ok');
process.exit(0);
