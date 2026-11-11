// The Ship part of a project's page against a real backend, staged on this computer (no cloud): step 1 Stage with
// one button, step 2 Try it with a query box that shows the staged results beside the baseline's, the plain words
// (where it runs, what it costs, when it stops by itself), the details behind a disclosure, and Stop. The steps are
// published for agents. With the starter trainer installed beside cst, a trained candidate is staged beside the
// baseline; without it, the baseline alone.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/staging-live.test.mjs
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
if (!cst) { console.log(`staging render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-staging-')));
// No keychain here: the endpoint key comes from the environment, under the name staging would store it as.
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring', STAGING_OUTDOOR_STAGING_KEY: 'render-test-key' };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'staging', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { spawnSync(cst, ['projects', 'unstage', 'outdoor'], { env }); } catch {} try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const post = async (p, body) => { const r = await fetch(base + p, { method: 'POST', headers: H, body: JSON.stringify(body) }); assert.ok(r.ok, `${p}: ${r.status} ${await r.clone().text()}`); return r.json(); };
const get = async (p) => (await fetch(base + p, { headers: H })).json();
const job = async (id, tries = 1200) => { for (let i = 0; i < tries; i++) { const r = await get(`/api/jobs/${id}`); if (r.status === 'done') return r.result; if (r.status === 'failed') throw new Error(r.message); await new Promise((res) => setTimeout(res, 250)); } throw new Error('job timed out'); };
await post('/api/projects', { name: 'Outdoor', kind: 'search-events' });
await post('/api/projects/outdoor/inputs/catalog', { source: { from: 'dataset', ref: 'demo-catalog' }, mode: 'snapshot' });
await post('/api/projects/outdoor/inputs/events', { source: { from: 'dataset', ref: 'logs/demo-search-sessions' }, mode: 'snapshot' });
await post('/api/projects/outdoor/baseline-start', {});
const py = path.join(path.dirname(cst), 'python');
const haveTrainer = fs.existsSync(py) && spawnSync(py, ['-c', 'import lightgbm']).status === 0;
let exp = null;
if (haveTrainer) {
  exp = (await job((await post('/api/projects/outdoor/starter/train', {})).job)).experiment;
  await job((await post(`/api/projects/outdoor/experiments/${exp}/backtest`, {})).job);
  for (let i = 0; i < 240; i++) { if ((await get('/api/projects/outdoor/experiments')).experiments[0].evaluation) break; await new Promise((res) => setTimeout(res, 250)); }
}

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token }, 'captain.scope': { kind: 'project', id: 'outdoor', label: 'Outdoor' } };
// The page waits on jobs with `sleep`: let it be a real one, so the job has time to finish.
globalThis.__exec = async (cmd, args = []) => { const r = spawnSync(cmd, args, { encoding: 'utf8' }); return { code: r.status ?? 1, stdout: r.stdout ?? '', stderr: r.stderr ?? '' }; };
const pdir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-project-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-project'), pdir, { recursive: true });
const { default: Page } = await import(path.join(pdir, 'main.js'));
const { span } = await import(path.join(pdir, 'ship.js'));
const page = async () => {
  const h = harness(Page);
  await h.start();
  for (let i = 0; i < 80 && !(h.text().includes('Ship') && h.byIdPrefix('ship-step-stage').length); i++) await h.idle(150);
  assert.equal(h.byIdPrefix('ship-step-stage').length, 1, `the page loads with the Ship path: ${h.text().slice(0, 800)}`);
  return h;
};
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const until = async (h, ok, what, tries = 200) => { for (let i = 0; i < tries && !ok(); i++) await h.idle(150); assert.ok(ok(), `${what}: ${h.text().slice(-1500)}`); };
const published = async (h, ok) => {
  for (let i = 0; i < 60 && !ok(globalThis.__shared['captain.view.project']); i++) { await h.layoutLists({}); await h.idle(150); }
  return globalThis.__shared['captain.view.project'];
};
const clean = (t, where) => {
  for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /CST_[A-Z]/, /[←-⇿⟰-⟿⤀-⥿]/, /—/, /\bpipeline/i, /\bchampion\b(?!\\?" alias)/i, /render-test-key/])
    assert.ok(!bad.test(t.replace('"champion" alias', '')), `no ${bad} ${where}`);
};
assert.equal(span(59), 'under a minute'); assert.equal(span(12 * 60), '12 min'); assert.equal(span(4 * 3600), '4 h'); assert.equal(span(3 * 3600 + 42 * 60), '3 h 42 min');

// Nothing staged: step 1 says what staging is in one sentence and offers one button. Step 2 waits.
let h = await page();
let t = h.text();
assert.ok(t.includes('Staging runs a model version as a live endpoint you can try before launch review.'), `the empty state says what staging is: ${t.slice(-1200)}`);
assert.equal(byId(h, 'ship-stage').length, 1, 'one primary button');
assert.ok(t.includes(haveTrainer ? 'Stage it' : 'Stage the baseline'), 'its words');
assert.equal(h.byIdPrefix('ship-done-').length, 0, 'no step is ticked');
assert.equal(h.byIdPrefix('ship-try-box').length, 0, 'no query box until something is staged');
assert.ok(t.includes('After it is staged, type a query and see the results.'), 'step 2 says what it waits for');
assert.ok(!t.includes('comes in a later release'), 'the placeholder card is gone');
if (haveTrainer) assert.ok(/Next: Stage outdoor\/starter-ranker@v\d+ to try it live\./.test(t), `after a candidate is judged, the next step is to stage it: ${t.slice(0, 600)}`);
let steps = (await published(h, (v) => v?.steps?.[0]?.label === '1 Stage'))?.steps;
assert.deepEqual(steps?.map((x) => [x.label, x.done, x.action ?? null]), [['1 Stage', false, haveTrainer ? 'stage' : 'stage-baseline'], ['2 Try it', false, null]], `agents see the steps: ${JSON.stringify(steps)}`);
assert.equal(steps[1].waiting_on, 'Stage a model version first.');
clean(t, 'before staging');

// One click stages it on this computer (the workspace has no cloud target): no cost to confirm.
await h.click('ship-stage');
await until(h, () => h.byIdPrefix('ship-done-stage').length === 1, 'step 1 is ticked once it runs');
t = h.text();
const staged = (await get('/api/projects/outdoor/staging')).staged;
assert.equal(staged.status, 'running'); assert.equal(staged.kind, 'local');
assert.ok(t.includes(`${staged.model_version} is running on this computer.`), `where it runs, in plain words: ${t.slice(-1500)}`);
assert.ok(/Stops by itself in (3 h 5\d min|4 h) if nobody uses it\./.test(t), 'the countdown to the automatic stop');
assert.ok(t.includes('Runs on this computer. It costs nothing and stops when idle.'), 'the cost note');
assert.equal(h.byIdPrefix('ship-confirm').length, 0, 'nothing to confirm for this computer');
assert.equal(h.byIdPrefix('ship-details-rows').length, 0, 'the endpoint and its log stay behind Details');
assert.ok(!t.includes('127.0.0.1'), 'no address on the page until Details is opened');
assert.equal(byId(h, 'ship-stop').length, 1, 'Stop');
assert.equal(h.byIdPrefix('ship-send').length, 1, 'Send to agent');
if (haveTrainer) assert.equal(h.byIdPrefix('ship-restage-baseline').length, 1, 'Restage with the baseline');
await h.click('ship-details-toggle');
t = h.text();
assert.ok(t.includes('http://127.0.0.1:') && t.includes('This computer (only reachable from here)') && t.includes("kept in this computer's keychain"), `details: ${t.slice(-900)}`);
clean(t, 'while staged');

// Step 2: a query, and the ranked products, beside the baseline's for the same query.
assert.equal(h.byIdPrefix('ship-try-box').length, 1, 'the query box');
await h.click('ship-try-go');
assert.ok(h.text().includes('Type a query to try.'), 'an empty query is refused in words');
h.mod.shipPath.box.set_value('waterproof jacket');
await h.click('ship-try-go');
await until(h, () => h.byIdPrefix('ship-try-results').length === 1, 'results come back');
t = h.text();
const res = h.mod.shipPath.result;
assert.equal(res.staged.results.length, 8);
for (const x of res.staged.results.slice(0, 3)) assert.ok(t.includes(x.title), `a product title on screen: ${x.title}`);
assert.ok(/\d+(\.\d)? ms/.test(t), 'latency');
if (haveTrainer) {
  assert.equal(byId(h, 'ship-try-baseline').length, 1, 'the baseline beside it');
  assert.ok(t.includes('Candidate') && t.includes('Baseline') && /differ from the baseline|same results in the same order/.test(t), 'side by side, with a one line comparison');
}
assert.ok(t.includes('Queries typed here are test traffic. They never count as shoppers.'), 'console queries are labelled test traffic');
assert.equal(h.byIdPrefix('ship-done-try').length, 1, 'step 2 is ticked once something was tried');
steps = (await published(h, (v) => v?.steps?.[1]?.done))?.steps;
assert.deepEqual(steps?.map((x) => x.done), [true, true], 'both steps read done to an agent');
clean(t, 'with results');

// What an agent is told when staging is pinned: no key, no secret.
const ctxOut = spawnSync(cst, ['--json', 'context', 'add', 'staging:outdoor'], { env, encoding: 'utf8' });
assert.equal(ctxOut.status, 0, ctxOut.stderr);
const shown = spawnSync(cst, ['context', 'show'], { env, encoding: 'utf8' }).stdout;
assert.ok(shown.includes('staging') && shown.includes(staged.model_version) && !shown.includes('render-test-key') && !/KEY/.test(shown), `agent context: ${shown.slice(0, 900)}`);

if (haveTrainer) {
  // Restage with the baseline: the same endpoint step, now serving the baseline alone.
  await h.click('ship-restage-baseline');
  await until(h, () => /keyword-baseline@v1 is running on this computer/.test(h.text()), 'the baseline is staged');
  assert.equal((await get('/api/projects/outdoor/staging')).staged.serve, 'baseline');
  assert.ok(h.text().includes('Stage the candidate'), 'and the candidate is one click away again');
}

// Stop asks first, then nothing is left running.
await h.click('ship-stop');
assert.equal(h.byIdPrefix('ship-stop-ask').length, 1, 'a confirmation');
assert.equal((await get('/api/projects/outdoor/staging')).status, 'running', 'nothing stopped yet');
await h.click('ship-stop-yes');
await until(h, () => h.text().includes('Staging is stopped. Nothing is left running.'), 'it says so');
assert.equal((await get('/api/projects/outdoor/staging')).status, 'stopped');
assert.equal(h.byIdPrefix('ship-done-').length, 0, 'the ticks are gone');
assert.equal(h.byIdPrefix('ship-try-box').length, 0, 'and the query box');
assert.ok(h.text().includes('Last staged:'), 'what was staged last stays on the card');
clean(h.text(), 'after stopping');
console.log(`staging render test: ok${haveTrainer ? '' : ' (baseline only: the train-ltr extra is not installed beside cst)'}`);
process.exit(0);
