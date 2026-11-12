// A project with a catalog and simulated events but no ranker, against a real backend: the Train and compare part is
// a guided start (1 set a keyword-search baseline, 2 train a starter ranker) instead of "No candidates yet", the top
// line says "Next: Set a baseline to compare against", and the New candidate form opens on "How do you want to
// train?" with three cards. When the trainer's library is installed beside cst, the Starter ranker card trains for
// real and the candidate is judged against the baseline, with SIMULATED evidence and no "beats" verdict.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/starter-ranker-live.test.mjs
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
if (!cst) { console.log(`starter-ranker render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-starter-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'starter', '--storage', path.join(work, 'store')], { env });
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
  for (let i = 0; i < 80 && !h.text().includes('Models'); i++) await h.idle(150);
  assert.ok(h.text().includes('Models'), `the page loads: ${h.text().slice(0, 800)}`);
  return h;
};
// What the page tells agents (kit/view.js publishes on a timer, so wait for it).
const published = async (h, ok) => {
  for (let i = 0; i < 60 && !ok(globalThis.__shared['captain.view.project']); i++) { await h.layoutLists({}); await h.idle(150); }
  return globalThis.__shared['captain.view.project'];
};
const clean = (t, where) => {
  for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /CST_[A-Z]/, /[\u2190-\u21ff\u27f0-\u27ff\u2900-\u297f]/, /\u2014/])
    assert.ok(!bad.test(t), `no ${bad} ${where}`);
};

// A catalog and nothing else: the guided start, with the baseline as the step that can be taken.
await post('/api/projects/outdoor/inputs/catalog', { source: { from: 'dataset', ref: 'demo-catalog' }, mode: 'snapshot' });
let h = await page();
let t = h.text();
assert.ok(!t.includes('No candidates yet'), 'the dead end is gone');
assert.equal(h.byIdPrefix('exp-start').length, 1, 'the guided start');
assert.ok(t.includes('Set a baseline (keyword search)') && t.includes('Train a starter ranker'), 'both steps are named');
assert.equal(h.byIdPrefix('start-baseline').length, 1, 'the baseline button');
assert.equal(h.byIdPrefix('start-done-baseline').length, 0, 'the baseline step is open');
assert.ok(t.includes('Start with keyword search as the baseline'), 'its button');
assert.equal(h.byIdPrefix('start-train').length, 0, 'nothing to learn from yet, so no Train button');
assert.ok(/Choose events first/.test(t), `step 2 says what it needs: ${t.slice(0, 1500)}`);
assert.ok(/Next: Set the .*events input\./i.test(t), 'a required input that is missing still comes first on the top line');
clean(t, 'on the page');

// With events: step 2 can be taken too.
await post('/api/projects/outdoor/inputs/events', { source: { from: 'dataset', ref: 'logs/demo-search-sessions' }, mode: 'snapshot' });
h = await page();
assert.equal(h.byIdPrefix('start-train').length, 1, 'Train a starter ranker is offered');
// The same steps, for agents: what `cst app context` reads from this page.
let steps = (await published(h, (v) => v?.steps?.[1]?.action === 'starter-train'))?.steps;
assert.deepEqual(steps?.map((x) => [x.label, x.done, x.action]), [['1 Set a baseline (keyword search)', false, 'baseline-start'], ['2 Train a starter ranker', false, 'starter-train']], `steps are published: ${JSON.stringify(globalThis.__shared['captain.view.project'])}`);
assert.ok(h.text().includes('Next: Set a baseline to compare against.'), `the top line starts with the baseline: ${h.text().slice(0, 500)}`);
assert.ok(h.text().includes('Start with keyword search'), 'and its button');

// Step 1, one click: keyword search over the catalog is the baseline, and the step is ticked.
await h.click('start-baseline');
let st = {};
for (let i = 0; i < 80 && !st.champion; i++) { st = await get('/api/projects/outdoor/state'); if (!st.champion) await h.idle(150); }
assert.equal(st.champion, 'outdoor/keyword-baseline@v1', 'the baseline is set');
h = await page();
for (let i = 0; i < 40 && !h.byIdPrefix('start-done-baseline').length; i++) await h.idle(150);
t = h.text();
assert.equal(h.byIdPrefix('start-done-baseline').length, 1, 'a done tick on step 1');
assert.ok(!t.includes('Start with keyword search as the baseline'), 'its button is gone');
assert.ok(t.includes('Next: Train a starter ranker.'), `the top line moves on: ${t.slice(0, 600)}`);
steps = (await published(h, (v) => v?.steps?.[0]?.done))?.steps;
assert.deepEqual(steps?.map((x) => [x.done, x.action ?? null]), [[true, null], [false, 'starter-train']], 'step 1 reads done to an agent');
assert.ok(t.includes('simulated'), 'the events read as simulated');
clean(t, 'on the page');

// The form: "How do you want to train?", three cards, the starter one chosen.
const fdir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), fdir, { recursive: true });
const pins = st.pins;
globalThis.__props = { task: 'experiment', values: { project: 'outdoor', pins } };
const { default: Forms } = await import(path.join(fdir, 'main.js'));
const f = harness(Forms);
await f.start();
for (let i = 0; i < 60 && !f.text().includes('kept back to judge'); i++) await f.idle(150);
t = f.text();
assert.ok(t.includes('How do you want to train?'), 'the question');
for (const id of ['f-how-starter', 'f-how-agent', 'f-how-own']) assert.equal(f.byIdPrefix(id).length, 1, `${id} card`);
assert.ok(t.includes('Starter ranker') && t.includes('A first trained candidate to judge against keyword search. It may not win.') && !t.includes('Good first candidate'), 'the starter card');
assert.ok(t.includes('Write one with an agent') && t.includes('My own training command'), 'the other two cards');
assert.equal(f.mod.values.how, 'starter', 'the starter card is chosen first');
assert.ok(t.includes('What it will use') && t.includes('demo-catalog@v1') && t.includes('logs/demo-search-sessions@v1'), 'what it reads, from the pins');
assert.ok(/Labels come from clicks after that, up to \d{4}-\d\d-\d\d/.test(t) && t.includes('kept back to judge'), `the cutoff and why: ${t}`);
assert.ok(/These events are simulated/.test(t), 'the simulated warning');
assert.ok(t.includes('The simulated events were generated from keyword search, so a ranker trained on them mostly reproduces keyword search.'), 'and what that means for the ranker, before training');
assert.ok(!t.includes('Training command'), 'no command to type for the starter ranker');
assert.ok(/\bTrain\b/.test(t) && !t.includes('Start training'), 'one button: Train');
clean(t, 'in the form');

// The other cards bring their own fields and their own button.
await f.click('f-how-own');
for (let i = 0; i < 20 && !f.text().includes('Training command'); i++) await f.idle(100);
t = f.text();
assert.ok(t.includes('Training command') && t.includes('Training labels') && t.includes('Start training'), 'my own command: the fields as before');
assert.ok(!t.includes('What it will use'), 'the starter plan is put away');
await f.click('f-how-agent');
for (let i = 0; i < 20 && !f.text().includes('What should it train?'); i++) await f.idle(100);
t = f.text();
assert.ok(t.includes('What should it train?') && t.includes('Open an agent') && !t.includes('Training command'), 'the agent card');
clean(t, 'in the form');

// A project with nothing to learn from: the card says why, and Train is off.
await post('/api/projects', { name: 'Bare', kind: 'search-events' });
const { TASKS, starterLines } = await import(path.join(fdir, 'tasks.js'));
const task = TASKS.find((x) => x.id === 'experiment');
const bare = await task.load({ project: 'bare' });
assert.equal(bare.starter.ready, false);
assert.equal(starterLines(bare.starter)[0].tone, 'bad');
assert.ok(task.blocked({ how: 'starter' }, bare), 'Train is disabled');
assert.ok(!task.blocked({ how: 'own' }, bare), 'the other cards still work');

// The real thing, when LightGBM is installed beside cst (the train-ltr extra). Otherwise training would download it.
const py = path.join(path.dirname(cst), 'python');
const haveTrainer = fs.existsSync(py) && spawnSync(py, ['-c', 'import lightgbm']).status === 0;
if (!haveTrainer) {
  console.log('starter-ranker render test: ok (training itself skipped: the train-ltr extra is not installed beside cst)');
  process.exit(0);
}
const ctx = await task.load({ project: 'outdoor', pins });
const r = await task.submit({ project: 'outdoor', pins, how: 'starter' }, ctx, {});
assert.ok(r.ok, r.error);
assert.match(r.message, /^Trained outdoor\/starter-ranker@v2\./);
assert.ok(/simulated/.test(r.message) && !/beats/i.test(r.message), 'the message keeps to what is known');
assert.equal(r.next?.label, 'Judge it against the baseline');
let exps = (await get('/api/projects/outdoor/experiments')).experiments;
assert.equal(exps.length, 1, 'the trained version is a candidate');
assert.equal(exps[0].trainer, 'lightgbm-ltr');
await r.next.run();
let ev = null;
for (let i = 0; i < 240 && !ev; i++) { ev = (await get('/api/projects/outdoor/experiments')).experiments[0].evaluation; if (!ev) await new Promise((res) => setTimeout(res, 250)); }
assert.ok(ev, 'the candidate is judged');
assert.equal(ev.evidence, 'SIMULATED');
assert.ok(!/beats/.test(ev.verdict), `no winner the rules don't allow: ${ev.verdict}`);
assert.equal(ev.recall.value, ev.recall.champion_value, 'a reranker finds what its first stage finds');
h = await page();
for (let i = 0; i < 40 && !h.text().includes('starter-ranker'); i++) await h.idle(150);
t = h.text();
assert.ok(t.includes('starter-ranker') && !h.byIdPrefix('exp-start').length, 'the candidate row replaces the guided start');
// Once a candidate exists the guided start is done, and the page publishes the Ship path's steps instead.
assert.deepEqual((await published(h, (v) => v?.steps?.[0]?.label === '1 Stage'))?.steps?.map((x) => x.label), ['1 Stage', '2 Try it', '3 Connect your site', '4 A/B test', '5 Launch review'], 'the Ship steps once a candidate exists');
const row = (await get('/api/projects/outdoor/experiments')).experiments[0];
assert.match(row.feature_timing, /^Click features: counted once, up to \d{4}-\d\d-\d\d\./, 'how the features were timed');
assert.ok(/Agrees with its own labeler|Too few judged results to call/.test(t), `the honest verdict shows: ${t.slice(0, 1200)}`);
clean(t, 'on the page');
console.log('starter-ranker render test: ok');
process.exit(0);
