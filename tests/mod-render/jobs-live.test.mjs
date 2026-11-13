// Runs, Jobs tab, against a real backend (backend/core, or `cst` on PATH) with three local compute jobs: one that
// finishes, one that fails (its reason and its log line show), and a long one stopped from the screen. Names and
// places are in words; no command line shows.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/jobs-live.test.mjs
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
if (!cst) { console.log(`jobs-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-jobs-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'jobs', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
spawnSync(cst, ['job', 'run', 'local', '--', 'data', 'list'], { env });
spawnSync(cst, ['job', 'run', 'local', '--', 'sql', 'nope', '-e', 'select 1'], { env });
spawnSync(cst, ['job', 'run', 'local', '--', 'sql', 'demo-store', '-e', 'select sum(a.range * b.range) from range(200000) a, range(200000) b'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { spawnSync(cst, ['job', 'list'], { env }); for (const l of (spawnSync(cst, ['job', 'list'], { env, encoding: 'utf8' }).stdout ?? '').split('\n')) { const id = /(job-[a-z0-9]+)/.exec(l)?.[1]; if (id && /running|queued/.test(l)) spawnSync(cst, ['job', 'cancel', id], { env }); } serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-runs-jobs-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-runs'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
await h.click('runs-kind-jobs'); // the one list, filtered to jobs
const until = async (ok, what) => {
  for (let i = 0; i < 80 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 800)}`);
};

await until((t) => t.includes('List datasets') && t.includes('Run a query on nope') && t.includes('Run a query on demo-store'), 'jobs named in words');
let t = h.text();
assert.ok(t.includes('This computer'), 'place in words');
assert.ok(!/sql nope -e|select 1|data list/.test(t), 'no command line on screen');

await until(() => (h.mod.jobs.list ?? []).some((x) => x.state === 'failed'), 'the failed job, with its state');
assert.ok(h.text().includes('Job') && h.text().includes('Failed'), 'its kind and its status, in words, on its row');
const failed = h.mod.jobs.list.find((x) => x.state === 'failed');
await h.click(`job-${failed.id}`);
await until((t) => t.includes('cst exited with code 1') || /exited with code 1/.test(t), 'the failure reason');
await until((t) => t.includes('no connection named nope'), 'its log line');

const long = h.mod.jobs.list.find((x) => x.argv.includes('demo-store'));
await h.click(`job-${long.id}`);
await until((t) => t.includes('Running') && t.includes('RUNNING FOR'), 'a running job with its clock');
await h.click(`job-stop-${long.id}`);
assert.ok(h.text().includes('Stop it? Click again'), 'stop asks first');
await h.click(`job-stop-${long.id}`);
await until((t) => t.includes('Stopped') || /stopped/i.test(h.mod.jobs.list.find((x) => x.id === long.id)?.state ?? ''), 'stopped');

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('jobs-live render test: ok');
process.exit(0);
