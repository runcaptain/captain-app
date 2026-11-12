// Synthetic events on the project page, against a real backend: an empty events input offers "Generate synthetic
// events" only once the project has a catalog, the form says the results are simulated and checks its numbers, and
// after generating the card carries a "simulated" badge and the evaluation reads as simulated.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/synthetic-events-live.test.mjs
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
if (!cst) { console.log(`synthetic-events render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-synth-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'synth', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });

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

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token }, 'captain.scope': { kind: 'project', id: 'outdoor', label: 'Outdoor' } };
globalThis.__exec = async (cmd, args = []) => { const r = spawnSync(cmd, args, { encoding: 'utf8' }); return { code: r.status ?? 1, stdout: r.stdout ?? '', stderr: r.stderr ?? '' }; };
const pdir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-project-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-project'), pdir, { recursive: true });
const { default: Page } = await import(path.join(pdir, 'main.js'));
const page = async () => {
  const h = harness(Page);
  await h.start();
  for (let i = 0; i < 80 && !h.text().includes('Search events'); i++) await h.idle(150);
  assert.ok(h.text().includes('Search events'), `the page loads: ${h.text().slice(0, 800)}`);
  return h;
};

// No catalog yet: the events card only asks to choose events. Shoppers have nothing to search.
let h = await page();
assert.equal(h.byIdPrefix('in-events-pick').length, 1, 'choose events');
assert.equal(h.byIdPrefix('in-events-generate').length, 0, 'nothing to generate from without a catalog');
let r = await fetch(`${base}/api/projects/outdoor/inputs/events/generate`, { method: 'POST', headers: H, body: '{}' });
assert.equal(r.status, 400, 'the services refuse too');

// With a catalog: the secondary action, which opens the form for this project and slot.
await post('/api/projects/outdoor/inputs/catalog', { source: { from: 'dataset', ref: 'demo-catalog' }, mode: 'snapshot' });
h = await page();
assert.equal(h.byIdPrefix('in-events-pick').length, 1, 'choosing real events is still the first action');
assert.equal(h.byIdPrefix('in-events-generate').length, 1, 'generate is offered');
assert.equal(h.byIdPrefix('in-catalog-generate').length, 0, 'only on an events input');
assert.ok(h.text().includes('Generate simulated events'), 'its words');
globalThis.__overlays = [];
await h.click('in-events-generate');
const opened = (globalThis.__overlays ?? []).at(-1);
if (opened) assert.deepEqual(opened.props, { task: 'generate-events', values: { project: 'outdoor', slot: 'events' } }, 'opens the form');

// The form: two numbers and the warning, drawn. Bad numbers are refused in plain words before anything runs.
const fdir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), fdir, { recursive: true });
const { TASKS } = await import(path.join(fdir, 'tasks.js'));
const task = TASKS.find((t) => t.id === 'generate-events');
assert.ok(task && task.hidden, 'a task opened from the card, not from the New menu');
globalThis.__props = { task: 'generate-events', values: { project: 'outdoor', slot: 'events' } };
const { default: Forms } = await import(path.join(fdir, 'main.js'));
const f = harness(Forms);
await f.start();
for (let i = 0; i < 20 && !f.text().includes('Shopper sessions'); i++) await f.idle(100);
const ft = f.text();
assert.ok(ft.includes('Shopper sessions') && ft.includes('Days to spread them over'), `fields: ${ft.slice(0, 600)}`);
assert.ok(ft.includes('every result judged on them is simulated'), 'the warning');
assert.equal(f.mod.values.sessions, '20000');
assert.equal(f.mod.values.days, '30');
for (const bad of [/\bcst /, /CST_[A-Z]/, /\bdaemon\b/i]) assert.ok(!bad.test(ft), `no ${bad} in the form`);
r = await task.submit({ project: 'outdoor', slot: 'events', sessions: 'lots', days: '30' }, {});
assert.ok(!r.ok && /whole number from 1 to 200,000/.test(r.error), JSON.stringify(r));
r = await task.submit({ project: 'outdoor', slot: 'events', sessions: '500', days: '0' }, {});
assert.ok(!r.ok && /Days is a whole number/.test(r.error), JSON.stringify(r));
assert.equal((await get('/api/projects/outdoor/stages')).inputs.find((x) => x.slot === 'events').pinned, null, 'nothing was made');

// Generate: the job runs, the input is set, and everything says simulated.
r = await task.submit({ project: 'outdoor', slot: 'events', sessions: '1,500', days: '14' }, {});
assert.ok(r.ok, r.error);
assert.ok(/simulated/.test(r.message), r.message);
const st = await get('/api/projects/outdoor/stages');
const ev = st.inputs.find((x) => x.slot === 'events');
assert.equal(ev.pinned, 'outdoor/synthetic-events@v1');
assert.equal(ev.simulated, true);
assert.equal(st.evaluation.label, 'Replay of logged searches, graded from clicks');
assert.equal(st.evaluation.evidence, 'SIMULATED');
const ds = await get('/api/datasets/outdoor/synthetic-events');
assert.equal(ds.tags?.synthetic, 'true', `dataset tags: ${JSON.stringify(ds.tags)}`);

h = await page();
const t = h.text();
// "Simulated" is a word in the row's one status, said only when it is true.
const statusOf = (slot) => (h.byIdPrefix(`in-${slot}-status`)[0]?.kids ?? []).join('');
assert.ok(/, simulated$/.test(statusOf('events')), `the events row says simulated: ${statusOf('events')}`);
assert.ok(!statusOf('catalog').includes('simulated'), 'the catalog is not simulated');
assert.equal(h.byIdPrefix('in-events-generate').length, 0, 'no generate once it is set');
assert.ok(t.includes('synthetic-events') && t.includes('simulated'), 'the card names the dataset and says simulated');
assert.ok(t.includes('graded from clicks, on simulated events'), 'how candidates are judged says simulated');
for (const arrow of ['→', '←', '↑', '↓']) assert.ok(!t.includes(arrow) && !ft.includes(arrow), 'no Unicode arrows');
console.log('synthetic-events render test: ok');
stop();
process.exit(0);
