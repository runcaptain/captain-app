// "Runs on" and its cost estimate (kit/cost.js), in the forms that can run work on a cloud, against a real backend:
// New candidate (all three cards), Run an offline evaluation, Generate synthetic events and Start rating. With no
// place in a cloud the row says this computer, free, and offers to add one. With one, picking it shows the estimate
// with the date of its prices, "How this is estimated" opens the basis, the place and the estimate are published
// for agents, and above the amount set in Settings the first click only repeats the number.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/cost-row-live.test.mjs
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
if (!cst) { console.log(`cost-row render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-costrow-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'costs', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const send = async (method, p, body) => { const r = await fetch(base + p, { method, headers: H, body: JSON.stringify(body) }); assert.ok(r.ok, `${p}: ${r.status} ${await r.clone().text()}`); return r.json(); };
const post = (p, body) => send('POST', p, body);
const get = async (p) => (await fetch(base + p, { headers: H })).json();
await post('/api/projects', { name: 'Outdoor', kind: 'search-events' });
await post('/api/projects/outdoor/inputs/catalog', { source: { from: 'dataset', ref: 'demo-catalog' }, mode: 'snapshot' });
await post('/api/projects/outdoor/inputs/events', { source: { from: 'dataset', ref: 'logs/demo-search-sessions' }, mode: 'snapshot' });
const pins = (await get('/api/projects/outdoor/state')).pins;

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
globalThis.__exec = async (cmd, args = []) => { const r = spawnSync(cmd, args, { encoding: 'utf8' }); return { code: r.status ?? 1, stdout: r.stdout ?? '', stderr: r.stderr ?? '' }; };
const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), dir, { recursive: true });
const { default: Forms } = await import(path.join(dir, 'main.js'));
const clean = (t, where) => {
  for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /CST_[A-Z]/, /[←-⇿⟰-⟿⤀-⥿]/, /—/, /\bvCPU\b.*\bRuns on\b/])
    assert.ok(!bad.test(t), `no ${bad} ${where}`);
};
/** Open a form and let its timers ask for the estimate until `ready` holds. */
const open = async (task, values, ready) => {
  globalThis.__props = { task, values };
  globalThis.__shared['captain.view.dialog'] = null;
  const f = harness(Forms);
  await f.start();
  for (let i = 0; i < 80 && !ready(f.text()); i++) { await f.layoutLists({}); await f.idle(80); }
  assert.ok(ready(f.text()), `${task} form is ready: ${f.text().slice(0, 1500)}`);
  return f;
};
const until = async (f, ok, what) => {
  for (let i = 0; i < 80 && !ok(f.text()); i++) { await f.layoutLists({}); await f.idle(80); }
  assert.ok(ok(f.text()), `${what}: ${f.text().slice(0, 2000)}`);
  return f.text();
};
const FREE = 'Runs on this computer. No cloud cost.';

// ---- No place in a cloud yet: this computer, free, and the way to add one. ----
let f = await open('experiment', { project: 'outdoor', pins }, (t) => t.includes(FREE));
let t = f.text();
assert.ok(t.includes('Runs on') && t.includes('This computer') && t.includes('free'), `the row: ${t}`);
assert.ok(t.includes('Add a place to run work'), 'the way to add a cloud place');
assert.equal(f.byIdPrefix('f-compute-aws').length, 0, 'no cloud place to pick');
assert.ok(t.includes('How this is estimated'), 'the disclosure is offered');
clean(t, 'in New candidate, local');

// ---- A place on AWS: the estimate, its date, the basis, and what an agent sees. ----
await post('/api/compute', { name: 'aws-batch', kind: 'aws-batch', profile: 'work-aws', region: 'us-west-1', config: { job_queue: 'q', job_definition: 'd' } });
f = await open('experiment', { project: 'outdoor', pins }, (x) => x.includes(FREE));
assert.equal(f.byIdPrefix('f-compute-aws-batch').length, 1, 'the cloud place is offered');
assert.equal(f.byIdPrefix('f-compute-local').length, 1, 'beside this computer');
assert.ok(!f.text().includes('Add a place to run work'), 'nothing to add once there is one');
await f.click('f-compute-aws-batch');
t = await until(f, (x) => /prices as of \d{4}-\d\d-\d\d\./.test(x), 'the estimate for the cloud place');
const est = t.split(' | ').find((x) => /prices as of/.test(x));
assert.match(est, /^(About \$\d+\.\d\d( to \$\d+\.\d\d)?|Up to \$\d+\.\d\d|Less than \$0\.01), .+ on aws-batch\. Estimate, prices as of \d{4}-\d\d-\d\d\.$/, `the main line: ${est}`);
assert.ok(!/vCPU|Fargate|GB\b/.test(est), 'no machine words in the main line');
assert.ok(!t.includes('4 vCPU, 16 GB on AWS Fargate'), 'the basis is closed until asked for');
await f.click('f-compute-cost-how');
t = f.text();
assert.ok(t.includes('4 vCPU, 16 GB on AWS Fargate in us-west-1'), `the machine and its price, in the basis: ${t}`);
assert.ok(/bundled AWS table, as of \d{4}-\d\d-\d\d/.test(t), 'where the price came from');
assert.ok(/scaled to [\d,]+ event rows/.test(t), 'how the time was worked out');
assert.equal(f.mod.values.compute, 'aws-batch', 'the place is what the form submits');
for (let i = 0; i < 30 && globalThis.__shared['captain.view.dialog']?.runs_on?.estimate !== est; i++) { await f.layoutLists({}); await f.idle(60); }
const seen = globalThis.__shared['captain.view.dialog'];
assert.equal(seen?.runs_on?.place, 'aws-batch', `published for agents: ${JSON.stringify(seen)}`);
assert.equal(seen.runs_on.estimate, est, 'with the same estimate the person reads');
assert.ok(typeof seen.runs_on.high === 'number' && seen.runs_on.free === false);
clean(t.replace(/4 vCPU.*$/, ''), 'in New candidate, cloud');

// My own training command: nothing says how long it runs, so the price is per hour.
await f.click('f-how-own');
t = await until(f, (x) => /an hour on aws-batch/.test(x), 'my own command is priced per hour');
assert.ok(t.includes("Captain can't tell how long your own training command runs until it has run once here."), 'and says why');
// The agent card trains nothing itself: this computer, no choice.
await f.click('f-how-agent');
t = await until(f, (x) => x.includes(FREE) && x.includes('The agent writes the script in a terminal on this computer.'), 'the agent card');
assert.equal(f.byIdPrefix('f-compute-aws-batch').length, 0, 'no place to pick on the agent card');
assert.equal(f.mod.values.compute, undefined, 'and nothing is sent to a cloud');

// ---- Above the amount set in Settings: the first click repeats the number and starts nothing. ----
await send('PUT', '/api/settings/costs', { confirm_above_usd: 0.0001 });
f = await open('experiment', { project: 'outdoor', pins }, (x) => x.includes(FREE));
await f.click('f-compute-aws-batch');
await until(f, (x) => /prices as of/.test(x), 'the estimate');
await f.click('form-go');
t = f.text();
assert.ok(/This is estimated at up to (less than \$0\.01|\$\d+\.\d\d) on aws-batch, above the/.test(t) && t.includes('Start it?'), `asked again with the number: ${t}`);
assert.ok(t.includes('Start anyway'), 'the button says what the second click does');
assert.equal((await get('/api/jobs')).length, 0, 'nothing was submitted');
assert.ok(!f.mod.busy, 'and nothing is running');
await send('PUT', '/api/settings/costs', { confirm_above_usd: 5 });

// ---- Run an offline evaluation: the same row, sized by the query set. ----
f = await open('backtest', { name: 'tuned-vs-bm25-again', a: 'demo-bm25@v1', b: 'demo-tuned@v1', queries: 'demo-queries@v2', judgments: 'demo-judgments@v1', hypothesis: 'x' }, (x) => x.includes(FREE));
t = f.text();
assert.ok(t.includes('Runs on') && f.byIdPrefix('f-compute-aws-batch').length === 1, 'the row, with the cloud place');
await f.click('f-compute-aws-batch');
t = await until(f, (x) => /on aws-batch\. Estimate, prices as of/.test(x), 'the evaluation estimate');
await f.click('f-compute-cost-how');
assert.ok(/scaled to [\d,]+ query and version pairs/.test(f.text()), `sized by queries and versions: ${f.text()}`);
assert.equal(f.mod.values.compute, 'aws-batch');
clean(t, 'in Run an offline evaluation');
// Trying settings of one version runs here, so the row goes away.
await f.click('f-kind-settings');
assert.equal(f.byIdPrefix('f-compute').length, 0, 'no row for the settings sweep');

// ---- Generate synthetic events: this computer only. ----
f = await open('generate-events', { project: 'outdoor', slot: 'events' }, (x) => x.includes(FREE));
assert.equal(f.byIdPrefix('f-compute-aws-batch').length, 0, 'no place to pick: it runs here');
assert.ok(f.text().includes('This computer') && f.text().includes('free'));

// ---- Start rating: people rate for free, and the row says what an LLM judge would bill for the same pairs. ----
f = await open('grading', { name: 'r1', queries: 'demo-queries@v2', a: 'demo-bm25@v1' }, (x) => /in model tokens for [\d,]+ pairs/.test(x));
t = f.text();
assert.ok(t.includes('People rate this queue, at no cloud cost.'), 'what the estimate is for');
assert.ok(/Estimate, prices as of \d{4}-\d\d-\d\d\./.test(t), 'with the date of the token prices');
clean(t, 'in Start rating');

console.log('cost-row render test: ok');
process.exit(0);
