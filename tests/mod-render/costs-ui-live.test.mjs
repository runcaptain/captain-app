// What work cost, wherever it shows after it ran, against a real backend (backend/core) with one training run of 12
// minutes recorded on a place in AWS (seed-costs.py):
//   the project page   the candidate's row carries its cost, Recent changes says what a change cost (or what it was
//                      estimated at), the More menu holds the project's running total with its last runs, and the
//                      total is published for agents (cloud_cost)
//   Runs, Jobs         the job's row and its detail carry the cost; the machine and its price open on a click
//   Runs, Live         a finished run that went out as a job shows the job's cost
//   Runs               an offline evaluation and a workflow show "Runs on" with the estimate; a place in the cloud is
//                      sent with the run, and above the amount set in Settings the first click only repeats the number
//   Settings           "Ask before starting work that costs more than" reads and saves the amount
//   Choose an input    under the snapshot choice: a file costs nothing on the cloud, a connection is billed by its
//                      warehouse
//   Retrain            a model version's Retrain shows "Runs on" first, and sends the place picked
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/costs-ui-live.test.mjs
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
if (!cst) { console.log(`costs-ui render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-costsui-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'costsui', '--storage', path.join(work, 'store')], { env });
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

// A project with a baseline and one candidate, a place in AWS, and a training run recorded there.
await post('/api/projects', { name: 'Outdoor' });
await post('/api/projects/outdoor/pin', { catalog: 'demo-catalog', queries: 'demo-queries@v1', holdout: 'demo-queries', labels: 'demo-judgments' });
await post('/api/projects/outdoor/experiments', { pipeline: 'demo-bm25' });
const tuned = await post('/api/projects/outdoor/experiments', { pipeline: 'demo-tuned' });
await post('/api/projects/outdoor/champion', { pipeline: 'demo-bm25' });
await post('/api/compute', { name: 'aws-batch', kind: 'aws-batch', profile: 'work-aws', region: 'us-west-1', config: { job_queue: 'q', job_definition: 'd' } });
const py = path.join(path.dirname(fs.realpathSync(cst)), 'python');
const seeded = spawnSync(fs.existsSync(py) ? py : 'python3', [path.join(here, 'seed-costs.py'), 'outdoor', tuned.id], { env, encoding: 'utf8' });
assert.equal(seeded.status, 0, seeded.stdout + seeded.stderr);
const seed = JSON.parse(seeded.stdout.trim().split('\n').at(-1));
assert.match(seed.cost, /^Cost about \$\d+\.\d\d \(12 min on aws-batch\)$/, `the services' sentence: ${seed.cost}`);
const total = await get('/api/projects/outdoor/costs');
assert.match(total.text, /^This project has cost about \$\d+\.\d\d on your cloud so far \(1 run\)\.$/, total.text);

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token }, 'captain.scope': { kind: 'project', id: 'outdoor', label: 'Outdoor' } };
globalThis.__props = {};
const load = async (mod) => {
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), dir, { recursive: true });
  return (await import(path.join(dir, 'main.js'))).default;
};
const until = async (h, ok, what, n = 80) => {
  for (let i = 0; i < n && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(120); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 1800)}`);
};
const textOf = (h, id) => {
  const parts = [];
  const walk = (e) => { if (!e || typeof e !== 'object') return; for (const k of e.kids ?? []) { if (typeof k === 'string' || typeof k === 'number') parts.push(String(k)); else walk(k); } };
  for (const e of h.byIdPrefix(id)) walk(e);
  return parts.join(' ');
};
const clean = (t, where) => {
  for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /CST_[A-Z]/, /[←-⇿⟰-⟿⤀-⥿]/, /—/])
    assert.ok(!bad.test(t), `no ${bad} ${where}: ${t}`);
};

// ---- the project page ---------------------------------------------------------------------------------------------
{
  const h = harness(await load('captain-project'));
  await h.start();
  await until(h, (t) => t.includes('Train and compare') && t.includes('demo-tuned') && t.includes(seed.cost), 'the candidate shows what it cost');
  const row = h.mod.exps.experiments.findIndex((e) => e.id === tuned.id);
  assert.equal(textOf(h, `exp-${row}-cost`), seed.cost, "the cost is on the candidate's own row");
  const champ = h.mod.exps.experiments.findIndex((e) => e.is_champion);
  assert.equal(h.byIdPrefix(`exp-${champ}-cost`).length, 0, 'a candidate with no recorded cost shows none');
  // Recent changes: what a change cost, and for one that only has an estimate, the estimate.
  await h.click('ph-everything');
  await until(h, (t) => t.includes('Recent changes') && t.includes('Trained a starter ranker on aws-batch'), 'Recent changes');
  const hist = h.byIdPrefix('ph-history-').map((e) => e.call('id')[0]).filter((id) => /-cost$/.test(id)).map((id) => textOf(h, id));
  assert.ok(hist.includes(seed.cost), `a change's cost: ${hist.join(' / ')}`);
  assert.ok(hist.some((x) => x === 'Estimated before it started: $0.02 to $0.05, 2 to 4 min on aws-batch. Estimate, prices as of 2026-11-01.'), `a change's estimate: ${hist.join(' / ')}`);
  // The More menu: the running total, as a line and not a button, with the last run under it.
  await until(h, () => Boolean(h.mod.costs), 'the total is loaded');
  await h.click('ph-more');
  assert.equal(textOf(h, 'ph-cost-total'), total.text, 'the running total, in the services words');
  assert.ok(textOf(h, 'ph-cost-run-0').includes(seed.cost) && textOf(h, 'ph-cost-run-0').startsWith('Train a starter ranker: '), `its last run: ${textOf(h, 'ph-cost-run-0')}`);
  for (const e of h.byIdPrefix('ph-cost')) assert.ok(!e.call('on_click'), 'the total is not a button');
  assert.ok(h.text().includes('Change icon') && h.text().includes('Delete project'), 'the menu keeps its entries');
  clean([textOf(h, 'ph-cost'), ...hist, textOf(h, `exp-${row}-cost`)].join(' '), 'on the project page');
  // For agents: the total rides the page's published view.
  const view = () => globalThis.__shared['captain.view.project'];
  await until(h, () => view()?.cloud_cost?.text === total.text, 'cloud_cost is published');
  assert.equal(view().cloud_cost.runs, 1);
}

// ---- Runs: Jobs, Live, and "Runs on" ------------------------------------------------------------------------------
{
  globalThis.__shared['captain.scope'] = null;
  const h = harness(await load('captain-runs'));
  await h.start();
  await h.click('tab-jobs');
  await until(h, (t) => t.includes('Retrain outdoor-starter') && t.includes(seed.cost), 'the job with its cost');
  assert.equal(textOf(h, `job-cost-${seed.job}`), seed.cost, "the cost is on the job's row");
  await h.click(`job-${seed.job}`);
  await until(h, () => textOf(h, 'job-cost-text') === seed.cost, 'the cost in the detail');
  assert.equal(h.byIdPrefix('job-cost-detail').length, 0, 'the machine and its price stay closed');
  assert.ok(!h.text().includes('an hour, prices as of'), 'no machine details in the main lines');
  await h.click('job-cost-how');
  assert.equal(textOf(h, 'job-cost-detail'), seed.detail, 'the machine and its price open on a click');
  assert.ok(h.text().includes('How this was worked out'));

  // Live: the finished run names its job, and shows what the job cost.
  await h.click('tab-live');
  await until(h, () => h.byIdPrefix(`live-${seed.live}`).length > 0, 'the live run is listed');
  await h.click(`live-${seed.live}`);
  h.mod.live.tick(h.cx);
  await until(h, () => textOf(h, 'live-cost') === seed.cost, "the live run shows its job's cost");

  // An offline evaluation: this computer first, free. Then the place in AWS, with its estimate and the place sent.
  await h.click('tab-backtests');
  await until(h, (t) => t.includes('Run evaluation') && t.includes('Runs on') && t.includes('Runs on this computer. No cloud cost.'), 'Runs on, this computer');
  assert.ok(h.byIdPrefix('runs-on-backtests-aws-batch').length, 'the place in AWS is offered');
  await h.click('runs-on-backtests-aws-batch');
  await until(h, (t) => /min on aws-batch\. Estimate, prices as of/.test(t), 'the estimate on the cloud, as a range of dollars and minutes');
  const est = h.mod.cost.backtests.estimate;
  assert.equal(textOf(h, 'runs-on-backtests-cost-text'), est.text, "the estimate is the services' sentence");
  await h.click('runs-on-backtests-cost-how');
  assert.ok(h.byIdPrefix('runs-on-backtests-cost-basis-0').length, 'How this is estimated opens the basis');
  await until(h, () => globalThis.__shared['captain.view.runs']?.runs_on?.place === 'aws-batch' && globalThis.__shared['captain.view.runs'].runs_on.estimate === est.text, 'the place and the estimate are published');
  // Above the amount set in Settings (0 asks every time), the first click only repeats the number.
  await send('PUT', '/api/settings/costs', { confirm_above_usd: 0 });
  h.mod.cost.backtests.asked = null;
  await until(h, () => h.mod.cost.backtests.estimate?.confirm === true, 'the estimate now needs a second click');
  const sent = [];
  const start = h.mod.start.bind(h.mod);
  h.mod.start = (cx, label, p, after, body) => { sent.push({ p, body }); };
  await h.click('run-backtest');
  assert.equal(sent.length, 0, 'nothing starts on the first click');
  assert.ok(textOf(h, 'runs-on-backtests-confirm').includes('Start it?') && h.text().includes('Start anyway'), 'the number is repeated');
  await h.click('run-backtest');
  assert.equal(sent.length, 1, 'the second click starts it');
  assert.deepEqual(sent[0].body, { compute: 'aws-batch' }, 'the place in the cloud is sent');
  // Back on this computer nothing is sent, and nothing is asked.
  await h.click('runs-on-backtests-local');
  await until(h, (t) => t.includes('Runs on this computer. No cloud cost.'), 'back on this computer');
  await h.click('run-backtest');
  assert.deepEqual(sent[1].body, {}, 'this computer sends no place');
  h.mod.start = start;
  // A workflow: its steps start from this computer, so the row names no other place and says where a step's cost shows.
  await h.click('tab-workflows');
  await until(h, () => Array.isArray(h.mod.data.workflows), 'the workflows');
  if (h.mod.data.workflows.length) {
    await until(h, () => textOf(h, 'runs-on-workflows-cost-text') === 'Runs on this computer. No cloud cost.', 'a workflow starts from this computer');
    assert.equal(textOf(h, 'runs-on-workflows-note'), 'Each step starts from this computer. A step set to run in your cloud is billed there, and its cost shows in Jobs when it finishes.');
    assert.equal(h.byIdPrefix('runs-on-workflows-aws-batch').length, 0, 'no place to pick: the services run a workflow from here');
  }
  await send('PUT', '/api/settings/costs', { confirm_above_usd: 5 });
  clean([textOf(h, 'runs-on-backtests'), seed.cost].join(' '), 'in Runs');
}

// ---- Settings: the amount to ask above ----------------------------------------------------------------------------
{
  const h = harness(await load('captain-services'));
  await h.start();
  await until(h, (t) => t.includes('Ask before starting work that costs more than') && h.mod.askSaved === 5, 'the setting, at its default of 5');
  assert.ok(h.text().includes('Applies to anything that runs on your cloud. Estimates under this start without asking.'), 'its one line of help');
  assert.equal(h.mod.askAbove.value(), '5');
  h.mod.askAbove.set_value('nope');
  await h.click('ask-above-save');
  assert.equal(textOf(h, 'ask-above-note'), 'Type an amount in dollars, like 5.');
  assert.equal((await get('/api/settings')).costs.confirm_above_usd, 5, 'nothing saved');
  h.mod.askAbove.set_value('$12.50');
  await h.click('ask-above-save');
  await until(h, () => textOf(h, 'ask-above-note') === 'Saved.', 'it saves');
  assert.equal((await get('/api/settings')).costs.confirm_above_usd, 12.5, 'saved with PUT /api/settings/costs');
  assert.equal(h.mod.askSaved, 12.5);
  clean(textOf(h, 'sec-costs'), 'in Settings');
}

// ---- a project's input: what taking a snapshot costs ---------------------------------------------------------------
{
  globalThis.__props = { input: { project: 'outdoor', slot: 'catalog', label: 'Catalog', kinds: ['catalog'], role: 'catalog' } };
  const h = harness(await load('captain-import'));
  await h.start();
  for (let i = 0; i < 40 && !h.mod.conns; i++) await h.idle(100);
  // A file or folder is read on this computer.
  await h.click('input-tab-computer');
  await until(h, () => textOf(h, 'input-cost-text') === 'Runs on this computer. No cloud cost.', 'a file or folder costs nothing on the cloud');
  // Kept live, nothing is copied now, so there is no snapshot to price.
  await h.click('mode-live');
  await until(h, () => h.byIdPrefix('input-cost').length === 0, 'no estimate when it is kept live');
  await h.click('mode-snapshot');
  // From a connection: the warehouse bills its own query, and the line says so, with the reason behind a click.
  await h.click('input-tab-connection');
  await h.click('src-demo-store');
  for (let i = 0; i < 20 && !h.mod.tree?.schemas && !h.byIdPrefix('read-tables').length; i++) { await h.layoutLists({}); await h.idle(100); }
  if (!h.mod.tree?.schemas) await h.click('read-tables');
  await until(h, () => Boolean(h.mod.tree?.schemas), 'its tables', 200);
  const sch = h.mod.tree.schemas.find((x) => (x.tables ?? []).some((y) => y.name === 'products'));
  await h.click(`tbl-${sch.name}-products`);
  await until(h, () => Boolean(h.mod.prep?.result), 'prepared');
  await until(h, () => textOf(h, 'input-cost-text') === "Billed by your warehouse. Captain can't estimate it.", 'a connection is billed by its warehouse');
  assert.equal(h.byIdPrefix('input-cost-basis').length, 0, 'the reason stays closed');
  await h.click('input-cost-how');
  assert.ok(h.byIdPrefix('input-cost-basis-0').length, 'How this is estimated opens the reason');
  const asked = JSON.parse(h.mod.snapCost.asked);
  assert.equal(asked.kind, 'snapshot');
  assert.equal(asked.where, 'demo-store', 'the connection is the place asked about');
  assert.ok(/products/.test(asked.size.sql ?? ''), `the query is sent for the estimate: ${JSON.stringify(asked.size)}`);
  clean(textOf(h, 'input-cost'), 'under the snapshot choice');
  globalThis.__props = {};
}

// ---- Retrain, from a model version's row: where it runs and what that costs, before anything starts ----------------
{
  const eng = await post('/api/engines', { name: 'Product search', type: 'search', scopes: [], pipelines: ['demo-bm25'] });
  globalThis.__shared['captain.scope'] = { kind: 'engine', id: eng.id, label: 'Product search' };
  const h = harness(await load('captain-engine'));
  await h.start();
  await until(h, () => Boolean(h.mod.engine) && Boolean(h.mod.data.versions?.ok), 'the page loaded');
  await h.click('eng-tabs-versions');
  // The demo's model versions declare no training command. Mark one as able to retrain, as the services would.
  h.mod.data.versions.data[0].trains = true;
  const name = h.mod.data.versions.data[0].name;
  await h.idle(10);
  const posts = [];
  const real = globalThis.fetch;
  globalThis.fetch = (url, init) => { if (String(url).includes('/retrain')) { posts.push(JSON.parse(init.body)); return Promise.resolve(new Response(JSON.stringify({ job: 'j1', live: 'live1' }), { status: 200, headers: { 'Content-Type': 'application/json' } })); } return real(url, init); };
  await h.click('eng-retrain-0');
  assert.equal(posts.length, 0, 'Retrain starts nothing: it asks first');
  await until(h, (t) => t.includes('Runs on') && t.includes('Runs on this computer. No cloud cost.') && t.includes('Start retraining'), 'Runs on, this computer');
  await h.click('eng-runs-on-aws-batch');
  await until(h, () => /on aws-batch\./.test(textOf(h, 'eng-runs-on-cost-text')), 'the estimate on the cloud');
  assert.equal(JSON.parse(h.mod.cost.asked).kind, 'training');
  assert.deepEqual(JSON.parse(h.mod.cost.asked).size, { how: 'command' });
  // Above the amount set in Settings the first click only repeats the number.
  h.mod.cost.estimate = { ...h.mod.cost.estimate, confirm: true, high: 9, threshold: 5 };
  await h.click('eng-retrain-go');
  assert.equal(posts.length, 0, 'nothing starts on the first click');
  assert.ok(textOf(h, 'eng-runs-on-confirm').includes('Start it?') && h.text().includes('Start anyway'), 'the number is repeated');
  await h.click('eng-retrain-go');
  for (let i = 0; i < 20 && !posts.length; i++) await h.idle(50);
  assert.deepEqual(posts, [{ compute: 'aws-batch' }], 'the place is sent with the retrain');
  assert.equal(globalThis.__shared['captain.open']?.live, 'live1', 'Runs opens on it, live');
  assert.equal(h.mod.asking, null, 'the row closes');
  globalThis.fetch = real;
  assert.ok(name);
}

stop();
fs.rmSync(work, { recursive: true, force: true });
console.log('costs-ui render test: ok');
process.exit(0);
