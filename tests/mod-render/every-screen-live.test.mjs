// Every screen is live. Each is mounted against the real services, something it shows is changed behind its back,
// once through the services and once by a second process (a `cst` command in a terminal), and it must show the
// change by itself: no click, no timer of its own, only the one live channel (kit/live.js).
//
// How soon: a change through the services within one second. A change by another process within two: the
// services look at the workspace's folder once a second for writes they did not make, then the screen reads.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/every-screen-live.test.mjs
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
if (!cst) { console.log(`every screen live: skipped (${why})`); process.exit(0); }
const python = path.join(path.dirname(cst), 'python');
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-everylive-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'el', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const call = async (method, p, body) => { const r = await fetch(base + p, { method, headers: H, body: body === undefined ? undefined : JSON.stringify(body) }); assert.ok(r.ok, `${method} ${p}: ${r.status} ${(await r.clone().text()).slice(0, 200)}`); return r.json(); };
const post = (p, body) => call('POST', p, body ?? {});
const get = (p) => call('GET', p);
const term = (args) => { const r = spawnSync(cst, args, { env, encoding: 'utf8' }); assert.equal(r.status, 0, `cst ${args.join(' ')}: ${r.stderr.slice(-400)}`); return r.stdout; };

await post('/api/projects', { name: 'Outdoor', description: 'Product search for the outdoor store' });
await post('/api/projects/outdoor/pin', { catalog: 'demo-catalog', queries: 'demo-queries', holdout: 'demo-queries', labels: 'demo-judgments' });
await post('/api/projects/outdoor/experiments', { pipeline: 'demo-bm25' });
await post('/api/projects/outdoor/champion', { pipeline: 'demo-bm25' });

const SECTIONS = ['project', 'data', 'notebooks', 'runs', 'deployments', 'team'];
const show = (section) => { globalThis.__layout = { id: 'root', children: [{ id: 'sections', active: section, children: SECTIONS.map((id) => ({ id })) }] }; };
const mount = async (mod, section, shared = {}, props) => {
  globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token }, ...shared };
  globalThis.__props = props;
  show(section);
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-el-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), dir, { recursive: true });
  const { default: Mod } = await import(path.join(dir, 'main.js'));
  const h = harness(Mod);
  await h.start();
  return h;
};
// The app's timers run on every pass (the live channel's own among them). Nothing here calls a screen's load.
const until = async (h, ok, what, ms = 12000) => {
  const t0 = performance.now();
  while (!ok() && performance.now() - t0 < ms) { await h.layoutLists({}); await h.idle(30); }
  assert.ok(ok(), `${what}: ${h.text().slice(0, 900)}`);
  return performance.now() - t0;
};
// The screen has read once and its channel holds a request open: only then is a change "behind its back".
const settled = async (h) => { const t0 = performance.now(); for (let i = 0; i < 12 && (i < 2 || performance.now() - t0 < 2500); i++) { await h.layoutLists({}); await h.idle(120); } };
const took = [];
/** Make a change, and time how long the screen takes to show it. `write` returns when the change is made. */
const shows = async (screen, how, h, write, ok, limit) => {
  assert.ok(!ok(), `${screen}: the change is not there before it is made`);
  await write();
  const ms = await until(h, ok, `${screen}, changed ${how}: it never showed`);
  took.push([screen, how, Math.round(ms), limit]);
  console.log(`  ${screen.padEnd(22)} ${how.padEnd(22)} ${String(Math.round(ms)).padStart(5)} ms  (limit ${limit})`);
  assert.ok(ms < limit, `${screen}, changed ${how}: shown after ${Math.round(ms)} ms, over ${limit}`);
};
const SERVICE = 1000, PROCESS = 2000;
console.log('every screen live: how long each screen took to show a change it was not told about');

// ---- Projects: the list of every project ---------------------------------------------------------------------
{
  const h = await mount('captain-project', 'project');
  await until(h, () => h.text().includes('Outdoor'), 'the list of projects');
  await settled(h);
  await shows('Projects', 'through the services', h, () => post('/api/projects', { name: 'Kids range' }), () => h.text().includes('Kids range'), SERVICE);
  await shows('Projects', 'by a second process', h, async () => { term(['projects', 'new', 'Trail gear']); }, () => h.text().includes('Trail gear'), PROCESS);
}

// ---- A project's page -------------------------------------------------------------------------------------------
{
  const h = await mount('captain-project', 'project', { 'captain.scope': { kind: 'project', id: 'outdoor', label: 'Outdoor' } });
  await until(h, () => h.mod.stages && h.mod.exps, 'the project page');
  await settled(h);
  const n = () => (h.mod.exps?.experiments ?? []).length;
  const before = n();
  await shows('Project page', 'through the services', h, () => post('/api/projects/outdoor/experiments', { pipeline: 'demo-tuned', labels: 'demo-judgments' }),
    () => n() === before + 1 && h.text().includes('demo-tuned'), SERVICE);
  await shows('Project page', 'by a second process', h, async () => { term(['projects', 'slot-add', 'outdoor', 'Store hours']); },
    () => JSON.stringify(h.mod.state ?? {}).includes('Store hours'), PROCESS);
}

// ---- Data: the tables ----------------------------------------------------------------------------------------------
{
  const h = await mount('captain-data', 'data');
  await until(h, () => (h.mod.catalog.rows ?? []).length > 0, 'the tables');
  await settled(h);
  const names = () => (h.mod.catalog.rows ?? []).map((r) => r.name);
  const csv = path.join(work, 'returns.csv');
  fs.writeFileSync(csv, 'order_id,reason\n1,too small\n2,changed mind\n');
  await shows('Data', 'by a second process', h, async () => { term(['data', 'snapshot', csv, '--name', 'made-in-a-terminal']); },
    () => names().includes('made-in-a-terminal') && /\d+ tables/.test(h.text()), PROCESS);
  // Moved into a folder through the services: its row says where it now is.
  await post('/api/library/folders', { path: 'from-the-services' });
  await settled(h);
  const folder = () => JSON.stringify((h.mod.catalog.rows ?? []).find((r) => r.name === 'made-in-a-terminal') ?? {});
  await shows('Data', 'through the services', h, () => post('/api/library/move', { names: ['made-in-a-terminal'], folder: 'from-the-services' }),
    () => folder().includes('from-the-services'), SERVICE);
}

// ---- Notebooks: the list ---------------------------------------------------------------------------------------------
{
  term(['notebook', 'new', 'first-look']);
  const h = await mount('captain-notebooks', 'notebooks');
  await until(h, () => (h.mod.nb.list ?? []).some((n) => n.name === 'first-look'), 'the notebooks');
  await settled(h);
  const has = (name) => (h.mod.nb.list ?? []).some((n) => n.name === name) && h.text().includes(name);
  await shows('Notebooks', 'by a second process', h, async () => { term(['notebook', 'new', 'from-a-terminal']); }, () => has('from-a-terminal'), PROCESS);
  const one = await get('/api/notebooks/first-look');
  await shows('Notebooks', 'through the services', h, () => post('/api/notebooks/from-the-services/save', { notebook: one.notebook ?? one, message: 'a copy' }), () => has('from-the-services'), SERVICE);
}

// ---- Runs: what is running now ---------------------------------------------------------------------------------------
{
  const h = await mount('captain-runs', 'runs', {}, { tab: 'live' });
  await until(h, () => h.mod.live.list !== null && h.mod.live.list !== undefined, 'the runs');
  await settled(h);
  const has = (title) => (h.mod.live.list ?? []).some((r) => r.title === title);
  let rid = null;
  await shows('Runs', 'by a second process', h, async () => {
    const r = spawnSync(python, ['-c', "from captain_search import live as LV; r = LV.start('custom', 'From a terminal', of='outdoor'); r.log('hello'); print(r.id)"], { env, encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr.slice(-400));
    rid = r.stdout.trim().split('\n').at(-1);
  }, () => has('From a terminal') && h.text().includes('From a terminal'), PROCESS);
  // Its end, written by another process, takes it off "running".
  const going = () => (h.mod.live.list ?? []).find((r) => r.id === rid)?.status;
  await shows('Runs, a run ending', 'by a second process', h, async () => {
    const code = `import json, os\nfrom captain_search import live as LV\nh = LV.header('${rid}'); h['status'] = 'done'; h['ended'] = LV._now(); LV._write_header('${rid}', h)`;
    const r = spawnSync(python, ['-c', code], { env, encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr.slice(-400));
  }, () => going() === 'done', PROCESS);
}

// ---- The sidebar: its tree of projects --------------------------------------------------------------------------------
{
  globalThis.__nextCommand = () => new Promise(() => {}); // the sidebar waits for palette commands, as in the app
  const h = await mount('captain-nav', 'project');
  await until(h, () => h.mod.state === 'ready' && h.text().includes('Outdoor'), 'the sidebar');
  await settled(h);
  await shows('Sidebar', 'through the services', h, () => post('/api/projects', { name: 'Gift finder' }), () => h.text().includes('Gift finder'), SERVICE);
  await shows('Sidebar', 'by a second process', h, async () => { term(['projects', 'new', 'Garden tools']); }, () => h.text().includes('Garden tools'), PROCESS);
}

// ---- Deployments: a model deployed from a terminal, stopped through the services ---------------------------------------
{
  const h = await mount('captain-deployments', 'deployments');
  await until(h, () => h.mod.d, 'the deployments');
  await settled(h);
  const row = () => (h.mod.d?.rows ?? []).find((r) => r.project === 'outdoor');
  await shows('Deployments', 'by a second process', h, async () => { term(['deploy', 'outdoor', '--on', 'local']); }, () => row()?.state === 'running' && h.text().includes('Outdoor'), PROCESS);
  await shows('Deployments', 'through the services', h, async () => {
    const r = await post('/api/projects/outdoor/unstage', {});
    for (let i = 0; i < 100; i++) { const jb = await get(`/api/jobs/${r.job}`); if (jb.status === 'done' || jb.status === 'failed') break; await new Promise((res) => setTimeout(res, 50)); }
  }, () => row()?.state !== 'running', SERVICE);
}

console.log('every screen live: ok');
process.exit(0);
