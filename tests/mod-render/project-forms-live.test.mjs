// The project's forms against a real backend on the demo store, through their real submits (captain-forms/tasks.js):
// New project with a catalog pins it; choosing an unsealed query set as the holdout seals it first; New experiment
// trains a version with your own command on the pinned inputs and it joins the project; Publish saves the registry
// place on the project once; Make champion; a failed readiness check offers its fix; a decision to go on offers the
// handoff. Then the New experiment form, drawn, warns when its training labels are the holdout's own.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/project-forms-live.test.mjs
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
if (!cst) { console.log(`project-forms render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-pforms-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'pf', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__exec = async (cmd, args = []) => { const r = spawnSync(cmd, args, { encoding: 'utf8' }); return { code: r.status ?? 1, stdout: r.stdout ?? '', stderr: r.stderr ?? '' }; };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), dir, { recursive: true });
const { TASKS, fixFor } = await import(path.join(dir, 'tasks.js'));
const { api } = await import(path.join(dir, 'kit/cst.js'));
const task = (id) => TASKS.find((t) => t.id === id);
const submit = async (id, values, args = {}) => { const t = task(id); const ctx = { args, ...(t.load ? await t.load(values) : {}) }; return t.submit(values, ctx, args); };
const state = async () => (await api('/api/projects/trail/state', { scoped: false })).data;
const opts = async (id, key, values = {}) => { const f = task(id).fields.find((x) => x.key === key); return typeof f.options === 'function' ? f.options({}, values) : f.options; };

// New project: what you're building, then the name. The catalog is chosen on its page.
let r = await submit('project', { name: 'Trail', recipe: 'search-judgments' });
assert.ok(r.ok, r.error);
r = await submit('pin', { project: 'trail', slot: 'catalog', ref: 'demo-catalog@v1' });
assert.ok(r.ok, r.error);
assert.equal((await state()).pins.catalog, 'demo-catalog@v1');

// An unsealed query set as the holdout: sealed first, then pinned.
r = await submit('pin', { project: 'trail', slot: 'holdout', ref: 'demo-queries@v1' });
assert.ok(r.ok, r.error);
const hold = (await state()).pins.holdout;
assert.match(hold, /^demo-queries@v\d+:holdout$/);
assert.notEqual(hold, 'demo-queries@v1:holdout');
r = await submit('pin', { project: 'trail', slot: 'labels', ref: 'demo-judgments@v1' });
assert.ok(r.ok, r.error);

// New experiment: your own training command, on the pinned inputs, joins the project when it lands.
const trainer = path.join(work, 'train.py');
fs.writeFileSync(trainer, 'import json, os, pathlib\nseen = json.loads(os.environ["CST_TRAIN_INPUTS"])\n'
  + 'pathlib.Path(os.environ["CST_OUT"], "result.yaml").write_text("metrics:\\n  inputs: %d\\n" % len(seen))\nprint("trained")\n');
const pins = (await state()).pins;
r = await submit('experiment', { project: 'trail', pins, base: 'demo-tuned', command: `python3 ${trainer}`, labels: 'demo-judgments@v1', params: 'trees = 50\nlr = 0.1' });
assert.ok(r.ok, r.error);
let exps = [];
for (let i = 0; i < 120 && !exps.length; i++) { exps = (await state()).experiments; if (!exps.length) await new Promise((res) => setTimeout(res, 250)); }
assert.equal(exps.length, 1, 'the trained version is an experiment');
const ref = exps[0].pipeline;
const pm = (await api(`/api/pipelines/demo-tuned?v=${ref.split('@v')[1]}`, { scoped: false })).data;
assert.deepEqual(pm.train_inputs.map((x) => x.role).sort(), ['catalog', 'labels', 'queries'], 'trained on the pins');
assert.ok(pm.train_inputs.find((x) => x.role === 'queries').ref.endsWith(':train'), 'the holdout trains on its train split only');
assert.deepEqual(pm.train_params, { trees: 50, lr: 0.1 });

// Publish: the registry place is kept on the project.
const reg = path.join(work, 'registry');
fs.mkdirSync(reg);
r = await submit('publish', { project: 'trail', pipeline: ref, arch: 'bm25', brand: 'trail', registry: reg });
assert.ok(r.ok, r.error);
assert.deepEqual((await state()).registry, { arch: 'bm25', brand: 'trail', uri: reg });

// Make champion, from a comparison.
r = await submit('champion', { project: 'trail', pipeline: 'demo-bm25@v1' });
assert.ok(r.ok, r.error);
assert.equal((await state()).champion, 'demo-bm25@v1');

// A failed readiness check's fix, and the handoff after a decision to go on.
assert.equal(fixFor([{ gate: 'evidence', detail: 'SIMULATED, needs HUMAN_GRADED or stronger' }], 'x@v1').task, 'grading');
assert.equal(fixFor([{ gate: 'evidence', detail: 'agrees with its own labeler' }], 'x@v1').label, 'Grade the holdout by hand');
assert.equal(fixFor([{ gate: 'backtest', detail: 'no run' }], 'x@v1').values.b, 'x@v1');
r = await submit('handoff', { pipeline: ref, env: 'prod' });
assert.ok(!r.ok && r.next?.task, `a failed check offers its fix: ${JSON.stringify(r)}`);
const inv = (await api('/api/investigations', { body: { title: 'Does the tuned ranker help?' }, scoped: false })).data;
r = await submit('decision', { investigation: inv.id, run: 'demo-tuned-vs-bm25@v1', candidate: 'demo-tuned@v1', outcome: 'continue', claim: 'offline-improvement', interpretation: 'Better offline.', next_step: 'Hand it off.' });
assert.ok(r.ok, r.error);
assert.equal(r.next?.task, 'handoff');
assert.equal(r.next.values.pipeline, 'demo-tuned@v1');

// The form, drawn: training on the holdout's own labels is called out before anything trains.
globalThis.__props = { task: 'experiment', values: { project: 'trail', pins, base: 'demo-tuned', labels: pins.labels } };
const { default: Forms } = await import(path.join(dir, 'main.js'));
const h = harness(Forms);
await h.start();
for (let i = 0; i < 20 && !h.text().includes('Training command'); i++) await h.idle(100);
const t = h.text();
assert.ok(t.includes('Training command') && t.includes('Training labels') && t.includes('Train on data before'), 'fields');
assert.ok(t.includes('agrees with its own labeler'), 'shared labels called out');
// The ranker's training command is filled in from its current version.
assert.equal(h.mod.values.command, `python3 ${trainer}`, 'training command pre-filled');
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /CST_[A-Z]/]) assert.ok(!bad.test(t), `no ${bad} in the form`);
console.log('project-forms render test: ok');
process.exit(0);
