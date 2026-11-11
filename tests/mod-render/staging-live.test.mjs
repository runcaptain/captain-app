// The Ship part of a project's page against a real backend, staged on this computer (no cloud): step 1 Stage with
// one button, step 2 Try it with a query box that shows the staged results beside the baseline's, the plain words
// (where it runs, what it costs, when it stops by itself), the details behind a disclosure, step 3 the A/B test
// (start, a readout labelled by where its traffic came from, simulated shoppers, ramp and roll back, each with a
// confirmation), step 4 Launch review, and Stop. The steps are published for agents. With the starter trainer installed beside cst, a trained candidate is staged beside the
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
assert.deepEqual(steps?.map((x) => [x.label, x.done, x.action ?? null]), [['1 Stage', false, haveTrainer ? 'stage' : 'stage-baseline'], ['2 Try it', false, null], ['3 A/B test', false, null], ['4 Launch review', false, 'handoff']], `agents see the steps: ${JSON.stringify(steps)}`);
assert.equal(steps[2].waiting_on, 'Stage a candidate beside the baseline first.');
assert.equal(byId(h, 'ship-handoff').length, 1, 'launch review stays the gate before production, as step 4');
assert.ok(t.includes('Captain deploys nothing itself.'), 'and says Captain deploys nothing');
assert.equal(byId(h, 'ship-ab-start').length, 0, 'no A/B test to start before a candidate is staged beside the baseline');
assert.equal(steps[1].waiting_on, 'Stage a model version first.');
clean(t, 'before staging');

// One click stages it on this computer (the workspace has no cloud target): no cost to confirm.
await h.click('ship-stage');
await until(h, () => h.byIdPrefix('ship-done-stage').length === 1, 'step 1 is ticked once it runs');
assert.equal(byId(h, 'ship-busy').length, 0, 'no progress label on a finished step');
// A progress label never outlives its step, even when the job that reported it can no longer answer (the services
// were restarted): once staging says the endpoint is running, the label goes.
h.mod.shipPath.busy = 'Packaging the model version'; h.mod.shipPath.busyFor = { what: 'stage', was: null };
await h.idle(50);
assert.equal(byId(h, 'ship-busy').length, 1, 'while it is in flight the label shows');
h.mod.shipPath.load(h.cx);
await until(h, () => byId(h, 'ship-busy').length === 0, 'the label is cleared when staging reports the endpoint running');
assert.ok(!h.text().includes('Packaging the model version'), 'and its words are gone');
t = h.text();
const staged = (await get('/api/projects/outdoor/staging')).staged;
assert.equal(staged.status, 'running'); assert.equal(staged.kind, 'local');
assert.ok(t.includes(`${staged.model_version} is running on this computer.`), `where it runs, in plain words: ${t.slice(-1500)}`);
assert.ok(/Stops by itself in (3 h 5\d min|4 h) if nobody uses it\./.test(t), 'the countdown to the automatic stop');
// The cost line is the shared one (kit/cost.js, POST /api/costs/estimate): the same words every form uses.
const est = await post('/api/costs/estimate', { kind: 'staging', where: 'local', size: {} });
assert.ok(est.free && est.text, JSON.stringify(est));
await until(h, () => byId(h, 'ship-cost-text').length === 1 && h.text().includes(est.text), `the shared cost line: ${est.text}`);
assert.equal(byId(h, 'ship-cost-billing').length, 0, 'nothing bills on this computer');
assert.equal((await published(h, (v) => v?.staging_cost?.free === true))?.staging_cost?.place, 'This computer', 'agents see where it runs and that it is free');
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
assert.deepEqual(steps?.map((x) => x.done), [true, true, false, false], 'steps 1 and 2 read done to an agent');
clean(t, 'with results');

// One line says whether serving computed the features training computes, for the pairs just served.
assert.ok(h.text().includes('Not checked yet: does the endpoint compute the same features training did?'), 'the check is offered');
await h.click('ship-check-go');
await until(h, () => /Served features match training: 100% of \d+ checked|Not checked: the staged version ranks without trained features/.test(h.text()), 'the check answers in one line');
if (haveTrainer) assert.ok(/Served features match training: 100% of \d+ checked/.test(h.text()), `a trained ranker's features match: ${h.text().slice(-700)}`);

// What an agent is told when staging is pinned: no key, no secret.
const ctxOut = spawnSync(cst, ['--json', 'context', 'add', 'staging:outdoor'], { env, encoding: 'utf8' });
assert.equal(ctxOut.status, 0, ctxOut.stderr);
const shown = spawnSync(cst, ['context', 'show'], { env, encoding: 'utf8' }).stdout;
assert.ok(shown.includes('staging') && shown.includes(staged.model_version) && !shown.includes('render-test-key') && !/KEY/.test(shown), `agent context: ${shown.slice(0, 900)}`);

if (haveTrainer) {
  // Step 3: an A/B test between the baseline and the candidate, on the endpoint that is already up.
  h = await page();
  await until(h, () => byId(h, 'ship-ab-start').length === 1, 'Start an A/B test is offered once both versions are staged');
  assert.ok(h.text().includes('Next: Start an A/B test: split traffic between the baseline and the candidate.'), `the top line follows: ${h.text().slice(0, 500)}`);
  await h.click('ship-ab-start');
  assert.equal(byId(h, 'ship-ab-setup').length, 1, 'the setup: share, metric, who splits');
  t = h.text();
  for (const w of ["Captain's staging endpoint", 'My own A/B system', "The candidate's share of traffic", 'What decides it', 'Sessions with a click', 'Sessions with a purchase', 'Revenue per session', 'A shopper always sees the same version.'])
    assert.ok(t.includes(w), `setup says: ${w}`);
  for (const id of ['ship-ab-split-1', 'ship-ab-split-5', 'ship-ab-split-25', 'ship-ab-split-50']) assert.equal(byId(h, id).length, 1, id);
  assert.ok(t.includes('Response time: must not get worse') && t.includes('Sessions with a click: watch') && t.includes('Captain pauses the ramp and says why'), `guardrails are marked must or watch: ${t.slice(-900)}`);
  await h.click('ship-ab-guard-revenue');
  assert.ok(h.text().includes('Revenue per session: must not get worse'), 'one click changes a guardrail');
  await h.click('ship-ab-primary-clicks');
  await h.click('ship-ab-split-50');
  await h.click('ship-ab-start-go');
  await until(h, () => byId(h, 'ship-ab-readout').length === 1, 'the readout appears');
  t = h.text();
  assert.ok(t.includes('The A/B test is running: the candidate gets 50% of traffic.'), 'it says what started');
  assert.ok(/Not enough data yet: 0 baseline and 0 candidate sessions\. Each arm needs 200/.test(t), `no winner before there is data: ${t.slice(-1500)}`);
  assert.equal(h.byIdPrefix('ship-done-ab').length, 0, 'step 3 is not ticked yet');
  for (const id of ['ship-ab-ramp-1', 'ship-ab-ramp-5', 'ship-ab-ramp-25', 'ship-ab-ramp-50', 'ship-ab-ramp-100', 'ship-ab-rollback', 'ship-ab-simulate']) assert.equal(byId(h, id).length, 1, id);
  assert.ok(t.includes('Ramp') && t.includes('Roll back') && t.includes('Baseline') && t.includes('Candidate') && t.includes('A/B test'), 'the words on screen');

  // No traffic yet: simulated shoppers drive the endpoint, and everything they produce says so.
  await h.click('ship-ab-simulate');
  await until(h, () => h.byIdPrefix('ship-done-ab').length === 1, 'step 3 is ticked once both arms have enough sessions', 2400);
  t = h.text();
  assert.ok(t.includes('Simulated shoppers') && t.includes('SIMULATED: these shoppers are generated. Nothing here says what real shoppers will do.'), `the readout is labelled simulated: ${t.slice(-1800)}`);
  assert.ok(!/Real traffic/.test(t), 'and never as real traffic');
  const ro = await get('/api/projects/outdoor/ab');
  assert.equal(ro.evidence, 'SIMULATED'); assert.equal(ro.traffic, 'simulated'); assert.ok(ro.enough);
  assert.equal(ro.arms.baseline.sessions + ro.arms.candidate.sessions, 1000);
  assert.ok(t.includes(`${ro.arms.baseline.sessions.toLocaleString('en-US')} sessions`) && t.includes(`${ro.arms.candidate.sessions.toLocaleString('en-US')} sessions`), 'sessions per arm');
  assert.ok(t.includes(ro.primary.text), `the primary metric with its interval: ${ro.primary.text}`);
  assert.ok(/between [+-]\d+\.\d and [+-]\d+\.\d points \(95% interval\)/.test(t), 'the difference comes with an interval');
  assert.ok(t.includes('Must not get worse. Response time:') && t.includes('Must not get worse. Revenue per session:') && t.includes('Watch. Sessions with a purchase:') && t.includes('The split is as asked:'), 'guardrails with their marks, and a check that the split is what was asked for');
  assert.deepEqual(ro.ab.guardrail_modes, { latency: 'must', purchases: 'watch', revenue: 'must' });
  // The same comparison per kind of query (head, torso, tail by how often the project's log searches it) and shopper.
  assert.equal(byId(h, 'ship-ab-segments').length, 1, 'the segment breakdown');
  assert.ok(ro.segments.length >= 1 && ro.segments.every((x) => t.includes(x.text)), `each segment has its own line: ${JSON.stringify(ro.segments.map((x) => x.text))}`);
  assert.ok(/(Head|Torso|Tail) queries: /.test(t), 'named in plain words');
  assert.ok(/Next: Send outdoor\/starter-ranker@v\d+ to launch review\./.test((await page()).text()), 'the top line moves on to launch review');
  clean(t, 'in the readout');

  // Ramp: one click and a confirmation. Nothing changes until the yes.
  await h.click('ship-ab-ramp-100');
  assert.equal(byId(h, 'ship-ab-ask').length, 1, 'a confirmation');
  assert.ok(h.text().includes('Give the candidate 100% of traffic? It gets 50% now.'));
  assert.equal((await get('/api/projects/outdoor/ab')).ab.split, 0.5, 'not ramped yet');
  await h.click('ship-ab-ask-yes');
  await until(h, () => h.text().includes('The candidate now gets 100% of traffic.'), 'ramped');
  assert.equal((await get('/api/projects/outdoor/ab')).ab.split, 1);

  // Roll back: one click and a confirmation, then every shopper gets the baseline and the test ends.
  await h.click('ship-ab-rollback');
  assert.ok(h.text().includes('Roll back? Every shopper gets the baseline again, and this test ends.'));
  await h.click('ship-ab-ask-yes');
  await until(h, () => h.text().includes('Rolled back. Every shopper gets the baseline again.'), 'rolled back');
  const after = await get('/api/projects/outdoor/ab');
  assert.equal(after.ab.status, 'rolled_back');
  assert.equal((await get('/api/projects/outdoor/staging')).staged.serve, 'baseline', 'the endpoint serves the baseline again');
  const hist = (await get('/api/projects/outdoor/state')).history.map((x) => x.what).join(' | ');
  for (const w of ['started an A/B test', 'simulated shoppers', 'ramped the A/B test', 'rolled back the A/B test']) assert.ok(hist.includes(w), `the project history has: ${w}`);
  await until(h, () => byId(h, 'ship-ab-start').length === 1 && h.text().includes('Start another A/B test'), 'another test can be started');
  assert.equal(byId(h, 'ship-ab-rollback').length, 0, 'no ramp or roll back on an ended test');
  assert.ok(h.text().includes('Stage the candidate'), 'and the candidate is one click from serving again');
  steps = (await published(h, (v) => v?.steps?.[2]?.done))?.steps;
  assert.deepEqual(steps?.map((x) => [x.label, x.done]), [['1 Stage', true], ['2 Try it', true], ['3 A/B test', true], ['4 Launch review', false]], 'the four steps, for agents');

  // My own A/B system: an address per version, and the readout the team brings back, labelled by what they say it was.
  await h.click('ship-ab-start');
  await h.click('ship-ab-mode-own');
  assert.ok(h.text().includes('Split traffic in your system, then bring the readout in here.'));
  await h.click('ship-ab-start-go');
  await until(h, () => byId(h, 'ship-ab-own').length === 1, 'the own-system view');
  t = h.text();
  assert.ok(t.includes('/rank/baseline') && t.includes('/rank/candidate') && t.includes('Bring in the readout'), `each version's address: ${t.slice(-900)}`);
  await post('/api/projects/outdoor/ab/result', { metric: 'conversion', baseline: 0.031, candidate: 0.034, ci_low: 0.001, ci_high: 0.005, source: 'our experiment system, test 12', traffic: 'real' });
  h = await page();
  await until(h, () => byId(h, 'ship-ab-brought').length === 1, 'the brought-in readout shows');
  assert.ok(h.text().includes('Real traffic, from our experiment system, test 12.'), 'labelled real because the team said so');
  await h.click('ship-ab-end');
  await until(h, () => h.text().includes('The A/B test is ended.'), 'ended');
  clean(h.text(), 'with a brought-in readout');
}

// Stop asks first, then nothing is left running.
await h.click('ship-stop');
assert.equal(h.byIdPrefix('ship-stop-ask').length, 1, 'a confirmation');
assert.equal((await get('/api/projects/outdoor/staging')).status, 'running', 'nothing stopped yet');
await h.click('ship-stop-yes');
await until(h, () => h.text().includes('Staging is stopped. Nothing is left running.'), 'it says so');
assert.equal((await get('/api/projects/outdoor/staging')).status, 'stopped');
assert.equal(h.byIdPrefix('ship-done-stage').length + h.byIdPrefix('ship-done-try').length, 0, 'the Stage and Try it ticks are gone');
assert.equal(h.byIdPrefix('ship-try-box').length, 0, 'and the query box');
assert.ok(h.text().includes('Last staged:'), 'what was staged last stays on the card');
clean(h.text(), 'after stopping');
console.log(`staging render test: ok${haveTrainer ? '' : ' (baseline only: the train-ltr extra is not installed beside cst)'}`);
process.exit(0);
