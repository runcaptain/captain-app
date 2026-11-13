// Every screen is live. The owner: "Everything that you see on every screen should be live updating every second...
// instant sync, instant awareness." Each screen is mounted against the real services, something is changed behind
// its back (through the service from another client, and through a second process), and the screen must show it
// within a second, with no click and no timer of its own re-reading anything.
//
// A cloud's own state change (a model stopped by another process, which the services learn by asking where it
// runs) is allowed two seconds on the services' stand-in feed.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/live-sync-live.test.mjs
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
if (!cst) { console.log(`live sync test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-livesync-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring', STAGING_OUTDOOR_STAGING_KEY: 'render-test-key' };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'shop', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { spawnSync(cst, ['projects', 'unstage', 'outdoor'], { env }); } catch {} try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const send = async (method, p, body) => { const r = await fetch(base + p, { method, headers: H, body: body === undefined ? undefined : JSON.stringify(body) }); assert.ok(r.ok, `${method} ${p}: ${r.status} ${await r.clone().text()}`); return r.json(); };
const post = (p, body) => send('POST', p, body ?? {});
// A read is asked again once: the services close a connection after a few idle seconds, and a request sent on it
// just then is reset.
const get = async (p) => { let r; try { r = await fetch(base + p, { headers: H }); } catch { r = await fetch(base + p, { headers: H }); } return r.json(); };
const job = async (id, tries = 1200) => { for (let i = 0; i < tries; i++) { const r = await get(`/api/jobs/${id}`); if (r.status === 'done') return r.result; if (r.status === 'failed') throw new Error(`job failed: ${r.message}`); await new Promise((res) => setTimeout(res, 100)); } throw new Error('job never ended'); };
globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };

const mount = async (mod) => {
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-livesync-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), dir, { recursive: true });
  const { default: Mod } = await import(path.join(dir, 'main.js'));
  const h = harness(Mod);
  await h.start();
  return h;
};
// The app runs a screen's timers by itself. Here they run every 100 ms for as long as it takes `ok` to hold, and
// the time that took is returned: null if it never did.
const within = async (h, ms, ok) => {
  const t0 = Date.now();
  while (Date.now() - t0 <= ms) {
    await h.layoutLists({});
    if (ok(h.text())) return Date.now() - t0;
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
};
const settle = async (h, ms = 1500) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { await h.layoutLists({}); await new Promise((r) => setTimeout(r, 100)); } };
const results = [];
const check = (screen, what, took, limit) => {
  results.push(`${screen}: ${what}: ${took === null ? 'never' : `${took} ms`} (limit ${limit} ms)`);
  assert.ok(took !== null, `${screen} did not show it within ${limit} ms: ${what}`);
};

// ---- Deployments ------------------------------------------------------------------------------------------------
{
  await post('/api/projects', { name: 'Outdoor', kind: 'search-events' });
  await post('/api/projects/outdoor/inputs/catalog', { source: { from: 'dataset', ref: 'demo-catalog' }, mode: 'snapshot' });
  await post('/api/projects/outdoor/inputs/events', { source: { from: 'dataset', ref: 'logs/demo-search-sessions' }, mode: 'snapshot' });
  await post('/api/projects/outdoor/baseline-start', {});
  const h = await mount('captain-deployments');
  await settle(h);
  assert.ok(!h.text().includes('Outdoor'), `nothing is deployed yet: ${h.text().slice(0, 200)}`);
  assert.ok(!h.text().includes('Reconnecting'), 'the live channel is up');
  const src = fs.readFileSync(path.join(root, 'defaults/mods/captain-deployments/main.js'), 'utf8');
  assert.ok(src.includes('watchLive(cx, ["deployed", "projects", "logins"]'), 'it watches what is deployed');

  // 1. Through the service, from another client: a model is deployed. The row shows it deploying, then running.
  const started = await post('/api/projects/outdoor/deploy', { host: 'local', confirm: true });
  check('Deployments', 'a deploy started from elsewhere shows on the list', await within(h, 1000, (t) => t.includes('Outdoor')), 1000);
  await job(started.job);
  const isUp = async () => (await get('/api/deployed')).rows.some((r) => r.project === 'outdoor' && r.state === 'running');
  for (let i = 0; i < 100 && !(await isUp()); i++) await new Promise((r) => setTimeout(r, 100));
  check('Deployments', 'the model is running, as the services say', await within(h, 2000, (t) => t.includes('| Running. |') && t.includes('1 model running.')), 2000);

  // 2. Through a second process: `cst projects unstage` stops it. No request reaches the service for this.
  const out = spawnSync(cst, ['projects', 'unstage', 'outdoor'], { env, encoding: 'utf8' });
  assert.equal(out.status, 0, out.stderr);
  check('Deployments', 'a model stopped by another process leaves the running list', await within(h, 2000, (t) => !t.includes('| Running. |') && !t.includes('1 model running.')), 2000);
  assert.ok(/Stopped/.test(h.text()), `and it says it is stopped: ${h.text().slice(0, 300)}`);
  assert.ok(!h.text().includes('Reconnecting'));
}

// ---- Data -------------------------------------------------------------------------------------------------------
{
  const h = await mount('captain-data');
  await settle(h);
  const n = (await get('/api/data/tables')).rows.length;
  assert.ok(h.text().includes(`${n} tables`), `the list is up: ${h.text().slice(0, 200)}`);
  // Through the service, from another client: a file is imported (a job). The count goes up by itself.
  const csv = path.join(work, 'added-behind-its-back.csv');
  fs.writeFileSync(csv, 'id,title\n1,Tent\n2,Stove\n');
  const imp = await post('/api/datasets/import', { paths: [csv], folder: '' });
  await job(imp.job);
  check('Data', 'a table imported from elsewhere is counted', await within(h, 1000, (t) => t.includes(`${n + 1} tables`)), 1000);
  // A table moved to a folder elsewhere: the open group shows the folder.
  await h.click('cat-group-kept');
  await post('/api/library/move', { names: ['added-behind-its-back'], folder: 'made-elsewhere' });
  await post('/api/library/move', { names: ['demo-catalog'], folder: 'made-elsewhere' });
  check('Data', 'a folder made from elsewhere shows in the open group', await within(h, 1000, (t) => t.includes('made-elsewhere')), 1000);
}

// ---- Runs -------------------------------------------------------------------------------------------------------
{
  const h = await mount('captain-runs');
  await settle(h);
  assert.ok(h.text().includes('demo-tuned-vs-bm25') && !h.text().includes('made-behind-its-back'), `the list is up: ${h.text().slice(0, 200)}`);
  await post('/api/backtests', { name: 'made-behind-its-back', arms: ['demo-bm25', 'demo-tuned'], queries: 'demo-queries', judgments: 'demo-judgments', hypothesis: 'x' });
  check('Runs', 'an evaluation saved from elsewhere is listed', await within(h, 1000, (t) => t.includes('made-behind-its-back')), 1000);
  // It is run from elsewhere. When the run is over, the list's own record of that evaluation has its result, with
  // nothing pressed: what the screen holds is what the services hold.
  const before = JSON.stringify((await get('/api/backtests')).find((x) => x.name === 'made-behind-its-back'));
  const run = await post('/api/backtests/made-behind-its-back/run', {});
  if (run.job) await job(run.job);
  let after = before;
  for (let i = 0; i < 100 && after === before; i++) { after = JSON.stringify((await get('/api/backtests')).find((x) => x.name === 'made-behind-its-back')); if (after === before) await new Promise((r) => setTimeout(r, 100)); }
  assert.notEqual(after, before, 'the services record the run');
  check('Runs', 'a run that finished elsewhere is on the list as the services have it', await within(h, 1000, () => JSON.stringify(h.mod.data.backtests.find((x) => x.name === 'made-behind-its-back')) === after), 1000);
}

// ---- Notebooks --------------------------------------------------------------------------------------------------
{
  const h = await mount('captain-notebooks');
  await settle(h);
  assert.ok(h.text().includes('demo-zero-results'), `the list is up: ${h.text().slice(0, 200)}`);
  await post('/api/notebooks/made-behind-its-back/save', { notebook: { nbformat: 4, nbformat_minor: 5, cells: [], metadata: {} }, message: 'x' });
  check('Notebooks', 'a notebook saved from elsewhere is listed', await within(h, 1000, (t) => t.includes('made-behind-its-back')), 1000);
  await post('/api/notebooks/made-behind-its-back/rename', { to: 'Named from elsewhere' });
  check('Notebooks', 'a rename made elsewhere shows', await within(h, 1000, (t) => t.includes('Named from elsewhere')), 1000);
}

console.log(results.join('\n'));
console.log('live sync: ok');
process.exit(0);
