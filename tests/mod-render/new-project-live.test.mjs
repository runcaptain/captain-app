// New project starts with what you're building: one card per recipe, each saying what you'll need, then the name, then
// the project page. Against a real backend on the demo store.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/new-project-live.test.mjs
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
if (!cst) { console.log(`new-project render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-newproj-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'np', '--storage', path.join(work, 'store')], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__props = { task: 'project' };
globalThis.__runs = [];

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), dir, { recursive: true });
const { default: Forms } = await import(path.join(dir, 'main.js'));
const h = harness(Forms);
await h.start();
for (let i = 0; i < 30 && !h.text().includes('Recommendations'); i++) await h.idle(100);
let t = h.text();
// The name comes first, then what you are building: a card a kind, each with what you'll need, and "Something else".
for (const r of ['Search ranking', 'Recommendations', 'Something else']) assert.ok(t.includes(r), `kind card ${r}`);
assert.ok(h.byIdPrefix('f-recipe-').length >= 5, 'the four kinds and Something else');
assert.ok((t.match(/You'll need/g) ?? []).length >= 5, 'each card says what you will need');
assert.ok(t.indexOf('Name') < t.indexOf('What are you building?'), 'the name comes first');
assert.equal(h.byIdPrefix('f-inputs').length, 0, 'no inputs are asked for before a kind is picked');
const get = async (p) => (await fetch(`http://127.0.0.1:${j.port}${p}`, { headers: { Authorization: `Bearer ${j.token}` } })).json();
const wait = async (ok, what) => { for (let i = 0; i < 40 && !ok(); i++) await h.idle(100); assert.ok(ok(), what); };
const byId = (id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);

// A search project: the kind fills the inputs, and they are the person's to rename, remove and add to.
await h.click('f-recipe-search-events');
await wait(() => (h.mod.values.inputs ?? []).length > 0, 'the kind fills the inputs');
const seeded = h.mod.values.inputs.map((x) => x.label);
assert.ok(seeded.includes('Catalog') && seeded.length >= 2, `filled from the kind: ${seeded}`);
assert.equal(h.mod.values.trainer, 'builtin', 'a search project starts on the built-in ranker');
t = h.text();
assert.ok(t.includes('Inputs') && t.includes('How is it trained?') && t.includes("Captain's built-in ranker") && t.includes('My own training command') && t.includes('My own repo'), `the inputs and the trainer: ${t.slice(-900)}`);
for (const k of ['Products', 'Events', 'Queries', 'Labels', 'A table', 'Something else']) assert.ok(t.includes(k), `an input can be ${k}`);
h.mod.rowStates[h.mod.values.inputs[0].uid].set_value('Products we sell');   // rename
const n0 = h.mod.values.inputs.length;
await h.click(`f-inputs-row-${n0 - 1}-remove`);                                // remove the last
assert.equal(h.mod.values.inputs.length, n0 - 1);
await h.click('f-inputs-add');                                                 // add one of their own
const added = h.mod.values.inputs.at(-1);
h.mod.rowStates[added.uid].set_value('Product embeddings');
await h.click(`f-inputs-row-${h.mod.values.inputs.length - 1}-kind-generic`);
assert.equal(added.kind, 'generic');
h.mod.values.name = 'Store search';
await h.click('form-go');
await wait(() => Boolean(globalThis.__shared['captain.scope']), `the project is made: ${h.text().slice(-500)}`);
assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'project', id: 'store-search', label: 'Store search' }, 'the new project is picked');
assert.ok(globalThis.__runs.some((r) => r[0] === 'layout.select' && r[1]?.child === 'project'), 'and its page opens');
let stages = await get('/api/projects/store-search/stages');
const labels = stages.inputs.map((x) => x.label ?? x.title ?? x.slot);
assert.ok(JSON.stringify(stages.inputs).includes('Products we sell') && JSON.stringify(stages.inputs).includes('Product embeddings'), `renamed and added inputs are the project's: ${labels}`);
assert.equal(stages.inputs.length, n0, 'one removed, one added');

// Something else: no kind is forced on it. The person says what it is, lists what it reads, and gives a command.
globalThis.__shared['captain.scope'] = null;
const h2 = harness(Forms);
await h2.start();
for (let i = 0; i < 30 && !h2.text().includes('Something else'); i++) await h2.idle(100);
await h2.click('f-recipe-other');
for (let i = 0; i < 20 && h2.mod.values.trainer === undefined; i++) await h2.idle(50);
t = h2.text();
assert.ok(t.includes('What is it?') && t.includes('No inputs yet. Add what your training reads'), `it asks what it is, and starts with no inputs: ${t.slice(-700)}`);
assert.equal(h2.mod.values.trainer, 'command', 'and on their own training command');
h2.mod.values.name = 'Fraud scores';
await h2.click('form-go');
assert.ok(h2.text().includes('What is it? is needed.'), 'it must be said what it is');
h2.mod.values.what = 'A fraud score for each payment';
h2.mod.values.command = 'python train_fraud.py';
await h2.click('f-inputs-add');
h2.mod.rowStates[h2.mod.values.inputs[0].uid].set_value('Payments');
await h2.click('f-inputs-add');                                                // left without a name
await h2.click('form-go');
await h2.idle(200);
assert.ok(h2.text().includes('Every input needs a name. Name it, or remove it.'), `an unnamed input is said, and nothing is made: ${h2.text().slice(-400)}`);
await h2.click('f-inputs-row-1-remove');
await h2.click('form-go');
for (let i = 0; i < 40 && !globalThis.__shared['captain.scope']; i++) await h2.idle(100);
assert.equal(globalThis.__shared['captain.scope']?.id, 'fraud-scores');
stages = await get('/api/projects/fraud-scores/stages');
assert.equal(stages.inputs.length, 1);
assert.ok(JSON.stringify(stages.inputs).includes('Payments'));
// Its page's three parts all answer, whatever the inputs: data, models, and where it can be deployed.
for (const p of ['/experiments', '/staging', '/deploy/hosts']) assert.ok(await get(`/api/projects/fraud-scores${p}`), p);
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad}`);
console.log('new-project render test: ok');
process.exit(0);
