// The Ship part of a project's page against a real backend, staged on this computer (no cloud): step 1 Stage with
// one button, step 2 Try it with a query box that shows the staged results beside the baseline's, the plain words
// (where it runs, what it costs, when it stops by itself), the details behind a disclosure, step 3 the A/B test
// (start, a readout labelled by where its traffic came from, simulated shoppers, ramp and roll back, each with a
// confirmation), Connect your site, shadow mode, the gated A/B form and its three endings, Launch review, and Stop. The steps are published for agents. With the starter trainer installed beside cst, a trained candidate is staged beside the
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
// The project page, then (unless `project`) the staged model's own page, on the tab asked for.
const page = async (tabId = 'try', project = false) => {
  const h = harness(Page);
  await h.start();
  for (let i = 0; i < 80 && !h.byIdPrefix('ship-summary').length; i++) await h.idle(150);
  assert.equal(byId(h, 'ship-open').length, 1, `the project page loads with the deployment summary: ${h.text().slice(0, 800)}`);
  if (project) return h;
  await h.click('ship-open');
  h.mod.shipPath.tab = tabId;
  for (let i = 0; i < 40 && !h.byIdPrefix('ship-header').length; i++) await h.idle(100);
  assert.equal(h.byIdPrefix('ship-header').length, 1, 'the deployment page opens from the project page in one click');
  await h.idle(50);
  return h;
};
const tab = async (h, id) => { await h.click(`dep-tab-${id}`); };
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
// The project page's Ship part is a short card that links to the deployment page.
let h = await page('try', true);
let t = h.text();
assert.ok(t.includes('Deployment') && t.includes('Not staged') && t.includes('Staging runs a model version as a live endpoint'), `the summary card: ${t.slice(-900)}`);
assert.equal(h.byIdPrefix('ship-handoff').length, 1, 'launch review stays on the project page');
h = await page();
t = h.text();
assert.ok(t.includes('Projects') && t.includes('Outdoor') && t.includes('Deployment'), 'the breadcrumb');
// No cloud is connected in this workspace: this computer is the choice, with one line on how to add a cloud.
assert.equal(h.byIdPrefix('ship-choice-local').length > 0, true, 'this computer is offered');
assert.ok(t.includes('On this computer') && t.includes('Free. Only you can reach it.'), 'and says what it is');
assert.equal(h.byIdPrefix('ship-choice-cloud').length, 0, 'no cloud choice without a cloud login');
assert.ok(t.includes('To stage where a site can reach it, connect a cloud login'), 'how to get one');
assert.ok(t.includes('Staging runs a model version as a live endpoint you can try before launch review.'), `the empty state says what staging is: ${t.slice(-1200)}`);
assert.equal(byId(h, 'ship-stage').length, 1, 'one primary button');
assert.ok(t.includes(haveTrainer ? 'Stage it' : 'Stage the baseline'), 'its words');
assert.equal(h.byIdPrefix('ship-done-').length, 0, 'no step is ticked');
assert.equal(h.byIdPrefix('ship-try-box').length, 0, 'no query box until something is staged');
assert.ok(t.includes('After it is staged, type a query and see the results.'), 'step 2 says what it waits for');
assert.ok(!t.includes('comes in a later release'), 'the placeholder card is gone');
if (haveTrainer) assert.ok(/Next: Stage outdoor\/starter-ranker@v\d+ to try it live\./.test(t), `after a candidate is judged, the next step is to stage it: ${t.slice(0, 600)}`);
let steps = (await published(h, (v) => v?.steps?.[0]?.label === '1 Stage'))?.steps;
assert.deepEqual(steps?.map((x) => [x.label, x.done, x.action ?? null]), [['1 Stage', false, haveTrainer ? 'stage' : 'stage-baseline'], ['2 Try it', false, null], ['3 Connect your site', false, null], ['4 A/B test', false, null], ['5 Launch review', false, 'handoff']], `agents see the steps: ${JSON.stringify(steps)}`);
assert.equal(steps[2].waiting_on, 'Stage a model version first.');
assert.equal(steps[3].waiting_on, 'Stage a candidate beside the baseline first.');
await tab(h, 'traffic');
assert.equal(byId(h, 'ship-handoff').length, 1, 'launch review stays the gate before production, as the last step');
assert.ok(h.text().includes('Captain deploys nothing itself.'), 'and says Captain deploys nothing');
assert.equal(byId(h, 'ship-ab-start').length, 0, 'no A/B test to start before a candidate is staged beside the baseline');
await tab(h, 'try');
assert.equal(steps[1].waiting_on, 'Stage a model version first.');
clean(t, 'before staging');

// One click stages it on this computer (the workspace has no cloud target): no cost to confirm.
await h.click('ship-stage');
await until(h, () => byId(h, 'ship-chip-state').length === 1 && h.text().includes('1 copy live'), 'the header says it is live once it runs');
assert.equal(byId(h, 'ship-busy').length, 0, 'no progress label on a finished step');
// A progress label never outlives its step, even when the job that reported it can no longer answer (the services
// were restarted): once staging says the endpoint is running, the label goes.
h.mod.shipPath.busy = 'Packaging the model version'; h.mod.shipPath.busyFor = { what: 'stage', was: null };
h.mod.shipPath.load(h.cx);
await until(h, () => byId(h, 'ship-busy').length === 0, 'the label is cleared when staging reports the endpoint running');
assert.ok(!h.text().includes('Packaging the model version'), 'and its words are gone');
t = h.text();
const staged = (await get('/api/projects/outdoor/staging')).staged;
assert.equal(staged.status, 'running'); assert.equal(staged.kind, 'local');
assert.equal(byId(h, 'ship-name')[0].kids.join(''), staged.model_version.replace(/^.*\//, ''), 'the model version names the page');
assert.ok(/Stops by itself in (3 h 5\d min|4 h) if nobody uses it\./.test(t), 'the countdown to the automatic stop');
// The cost line is the shared one (kit/cost.js, POST /api/costs/estimate): the same words every form uses.
const est = await post('/api/costs/estimate', { kind: 'staging', where: 'local', size: {} });
assert.ok(est.free && est.text, JSON.stringify(est));
await until(h, () => byId(h, 'ship-cost-text').length === 1 && h.text().includes(est.text), `the shared cost line: ${est.text}`);
assert.equal(byId(h, 'ship-cost-billing').length, 0, 'nothing bills on this computer');
assert.equal((await published(h, (v) => v?.staging_cost?.free === true))?.staging_cost?.place, 'This computer', 'agents see where it runs and that it is free');
assert.equal(h.byIdPrefix('ship-confirm').length, 0, 'nothing to confirm for this computer');
// The header answers without a click: is it up, where, the address with Copy, the key, a ready test request.
assert.match(byId(h, 'ship-address')[0].kids.join(''), /^http:\/\/127\.0\.0\.1:\d+$/, 'the address is in the header, not under Details');
for (const id of ['ship-copy-address', 'ship-copy-key', 'ship-copy-test', 'ship-chip-where', 'ship-chip-key', 'ship-chip-running']) assert.equal(byId(h, id).length, 1, id);
assert.ok(t.includes('This computer') && t.includes('Key required') && t.includes('0 requests running') && t.includes('Only this computer can reach it.'), `the chips and the reach line: ${t.slice(0, 1500)}`);
assert.equal(byId(h, 'ship-chip-traffic').length, 0, 'no traffic label before any request: never "real" by default');
let copied = null;
h.cx.write_to_clipboard = (x) => { copied = x; };
await h.click('ship-copy-address');
assert.match(copied, /^http:\/\/127\.0\.0\.1:\d+$/, 'Copy puts the address on the clipboard');
await until(h, () => byId(h, 'ship-copy-test').length === 1, 'a ready test request');
await h.click('ship-copy-test');
assert.ok(/^curl -s http:\/\/127\.0\.0\.1:\d+\/rank -H "Authorization: Bearer \$CAPTAIN_STAGING_KEY"/.test(copied) && !copied.includes('render-test-key'), `a curl that reads the key from the environment: ${copied}`);
await h.click('ship-copy-key');
await until(h, () => copied === 'render-test-key', 'Copy the key reads it from the keychain');
assert.ok(!h.text().includes('render-test-key'), 'and never draws it');
assert.equal(h.byIdPrefix('ship-details-rows').length, 0, 'the instance and the log stay behind Details');
// Where it runs, and who can reach it, is on the Stage row itself, not only under Details.
assert.equal(byId(h, 'ship-where')[0].kids.join(''), 'Only this computer can reach it.', 'one line under the address says who can reach it');
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
assert.deepEqual(steps?.map((x) => x.done), [true, true, false, false, false], 'steps 1 and 2 read done to an agent');
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

// Try it, beside the baseline: what moved, why a result is there, and the queries where the two differ most.
if (haveTrainer) {
  await until(h, () => byId(h, 'ship-differ').length === 1, 'the list of queries where the two differ');
  const d = h.mod.shipPath.diff;
  assert.ok(d.checked >= 5, JSON.stringify(d));
  if (d.mostly_same) {
    assert.equal(byId(h, 'ship-same').length, 1, 'a candidate that reproduces the baseline is called that');
    assert.ok(/This candidate mostly reproduces the baseline: .+\. It returned the same top results on \d+ of \d+ queries checked\./.test(h.text()), `one plain sentence with the reason: ${d.text}`);
    assert.ok(d.text.includes('it learned from events that keyword search generated'), `the reason for a ranker trained on simulated events: ${d.text}`);
  }
  if (!d.queries.length) assert.ok(h.text().includes(`None of the ${d.checked} queries checked rank differently.`), 'no differing queries is said, not left blank');
  else { await h.click('ship-differ-0'); await until(h, () => h.mod.shipPath.result?.query === d.queries[0].query, 'a differing query opens in Try it'); }
  const withWhy = h.mod.shipPath.result.staged.results.findIndex((x) => x.why?.length);
  if (withWhy >= 0) {
    await h.click(`ship-try-staged-r${withWhy}-why`);
    assert.ok(/\d+% of what the model learned: /.test(h.text()), `why a result is there, from the model's own signals: ${h.text().slice(-600)}`);
  }
  const moved = h.mod.shipPath.result.staged.results.filter((x) => x.move && x.move !== 'same');
  assert.equal(h.byIdPrefix('ship-try-staged-r').filter((e) => /-move$/.test(e.call('id')[0])).length, moved.length, 'each result that moved is marked');
  // The candidate's own row says it too, before anyone stages it.
  const row = (await get('/api/projects/outdoor/experiments')).experiments.find((e) => !e.is_champion);
  if (row.sameness?.mostly_same) assert.ok(h.text().includes('Mostly reproduces the baseline. Open it to see why.'), 'on the candidate row');
}

// Step 3, Connect your site. On this computer no site can reach it, and the page says so with the ways forward.
await tab(h, 'traffic');
await until(h, () => byId(h, 'ship-connect-live').length === 1, 'the Connect step shows');
t = h.text();
assert.ok(t.includes('Connect your site') && t.includes('Waiting for the first request.'), 'a live line, waiting');
assert.ok(t.includes("A site can't reach an endpoint on this computer. Two ways forward: stage it in your cloud, so your site can call it, or keep testing here with simulated shoppers."), `the blocked sentence: ${t.slice(-1500)}`);
assert.ok(/http:\/\/127\.0\.0\.1:\d+/.test(t), 'the address');
assert.ok(/Not counted: \d+ test\./.test(t), 'console queries are shown as not counted');
assert.equal(byId(h, 'ship-chip-traffic')[0]?.kids.join(''), 'Test traffic only', 'the header says what the traffic so far is');
assert.ok(t.includes("The key is kept in this computer's keychain.") && !t.includes('render-test-key'), 'where the key lives, never the key');
let clip = null;
h.cx.write_to_clipboard = (x) => { clip = x; };
await h.click('ship-connect-key');
await until(h, () => clip !== null, 'Copy the key puts it on the clipboard');
assert.equal(clip, 'render-test-key');
assert.ok(h.text().includes('The key is on your clipboard.') && !h.text().includes('render-test-key'), 'and it is still not on screen');
for (const id of ['ship-connect-code-curl', 'ship-connect-code-javascript', 'ship-connect-code-python']) assert.equal(byId(h, id).length, 1, id);
assert.equal(byId(h, 'ship-connect-snippet').length, 0, 'code stays closed until asked for');
await h.click('ship-connect-code-python');
t = h.text();
assert.ok(t.includes('def rank(query, session_id):') && t.includes('def outcome(request_id, event') && t.includes('fall back to your own search') && t.includes('CAPTAIN_STAGING_KEY'), `the two calls, with the fallback: ${t.slice(-1200)}`);
assert.ok(t.includes('Give the endpoint 300 ms.'), 'the timeout is said');
await h.click('ship-connect-copy');
assert.ok(clip.includes('def rank(query, session_id)') && !clip.includes('render-test-key'), 'the code is copied without the key in it');
await h.click('ship-connect-test');
await until(h, () => /The endpoint answered ".+" in \d+ ms with 5 results\. This was test traffic/.test(h.mod.shipPath.note?.text ?? ''), `a test request proves the path: ${JSON.stringify(h.mod.shipPath.note)}`);
assert.ok(h.text().includes(h.mod.shipPath.note.text), 'and the page says so');
assert.equal(h.byIdPrefix('ship-done-connect').length, 0, 'and still does not count as a site');
if (haveTrainer) assert.ok((await page('traffic')).text().includes("Next: A site can't reach this computer. Stage it in your cloud, or practice here with simulated shoppers (Ship, step 3)."), 'the top line says the same');
clean(h.text(), 'in the Connect step');

if (haveTrainer) {
  // Step 4 is gated: no real request has arrived, so the only way in says what it is.
  await until(h, () => byId(h, 'ship-ab-practice-go').length === 1, 'practice is offered');
  assert.equal(byId(h, 'ship-ab-start').length, 0, 'no Start an A/B test before a site is calling');
  assert.ok(h.text().includes('No real request has reached this endpoint yet, so a split would divide nothing.'), `and it says why: ${h.text().slice(-1400)}`);
  await h.click('ship-ab-practice-go');
  t = h.text();
  assert.equal(byId(h, 'ship-ab-practice').length, 1);
  assert.ok(t.includes("Practice with simulated shoppers. Everything it shows is SIMULATED, and a practice test can't go to launch review."), 'the practice form says what it is');
  for (const w of ["Captain's staging endpoint", 'My own A/B system', "The candidate's share of traffic", 'What decides it', 'Sessions with a click', 'Response time: must not get worse', 'Captain pauses the ramp and says why'])
    assert.ok(t.includes(w), `setup says: ${w}`);
  for (const id of ['ship-ab-split-1', 'ship-ab-split-5', 'ship-ab-split-25', 'ship-ab-split-50']) assert.equal(byId(h, id).length, 1, id);
  await h.click('ship-ab-guard-revenue');
  await h.click('ship-ab-primary-clicks');
  await h.click('ship-ab-start-go');
  await until(h, () => byId(h, 'ship-ab-readout').length === 1, 'the readout appears');
  assert.ok(/Not enough data yet: 0 baseline and 0 candidate sessions\. Each arm needs 200/.test(h.text()), 'no winner before there is data');
  for (const id of ['ship-ab-ramp-1', 'ship-ab-ramp-5', 'ship-ab-ramp-25', 'ship-ab-ramp-50', 'ship-ab-ramp-100', 'ship-ab-rollback', 'ship-ab-simulate', 'ship-ab-ship', 'ship-ab-keep', 'ship-ab-more']) assert.equal(byId(h, id).length, 1, id);
  // An empty readout is never called real, and on this computer it says no shopper can arrive.
  assert.ok(h.text().includes('No traffic yet') && !/Real traffic/.test(h.text()), `no "Real traffic" label on zero sessions: ${h.text().slice(-1200)}`);
  assert.equal(byId(h, 'ship-ab-unreachable').length, 1, "and it says a site can't reach this endpoint");
  await h.click('ship-ab-simulate');
  // The job's progress shows on the step that started it, the A/B test, never on the header.
  assert.equal(byId(h, 'ship-ab-busy').length, 1, 'the progress label is on the A/B step');
  assert.equal(byId(h, 'ship-busy').length, 0, 'and not on the header');
  await until(h, () => h.byIdPrefix('ship-done-ab').length === 1, 'step 4 is ticked once both arms have enough sessions', 2400);
  assert.equal(byId(h, 'ship-ab-busy').length + byId(h, 'ship-busy').length, 0, 'and it is gone when the job ends');
  t = h.text();
  let ro = await get('/api/projects/outdoor/ab');
  assert.equal(ro.evidence, 'SIMULATED'); assert.ok(ro.practice && ro.enough && !ro.can_ship);
  assert.ok(t.includes('Simulated shoppers') && t.includes('SIMULATED: these shoppers are generated. Nothing here says what real shoppers will do.') && !/Real traffic/.test(t), 'labelled simulated, never real');
  assert.ok(t.includes("This is a practice test on simulated shoppers. It can't go to launch review."), 'Ship it is refused, with the reason');
  assert.ok(t.includes(ro.primary.text) && /between [+-]\d+\.\d and [+-]\d+\.\d points \(95% interval\)/.test(t), 'the difference with its interval');
  assert.ok(t.includes('Must not get worse. Response time:') && t.includes('Must not get worse. Revenue per session:') && t.includes('Watch. Sessions with a purchase:'), 'guardrails with their marks');
  assert.ok(ro.segments.length >= 1 && ro.segments.every((x) => t.includes(x.text)) && /(Head|Torso|Tail) queries: /.test(t), 'by segment');
  if (ro.needed) assert.ok(t.includes(ro.needed.text) && /About [\d,]+ more sessions needed in the smaller arm to tell a difference of \d+\.\d points\./.test(ro.needed.text), `sessions still needed: ${ro.needed.text}`);
  assert.equal(byId(h, 'ship-ab-ship')[0].call('on_click'), undefined, 'Ship it is switched off for a practice test');
  // Ramp, with a confirmation.
  // When a "must not get worse" guardrail has clearly dropped, the ramp is paused: up is refused, down still works.
  const to = ro.hard_stop ? 25 : 100;
  if (ro.hard_stop) {
    assert.equal(byId(h, 'ship-ab-paused').length, 1, 'the pause is on the page, with why');
    await h.click('ship-ab-ramp-100');
    assert.equal(byId(h, 'ship-ab-ask').length, 0, 'ramping up is refused while paused');
  }
  await h.click(`ship-ab-ramp-${to}`);
  assert.ok(h.text().includes(`Give the candidate ${to}% of traffic? It gets 50% now.`));
  await h.click('ship-ab-ask-yes');
  await until(h, () => h.text().includes(`The candidate now gets ${to}% of traffic.`), 'ramped');
  // Roll back: one line says what, why and what next. The old readout is filed, not left on screen as current.
  await h.click('ship-ab-rollback');
  assert.ok(h.text().includes('Roll back? Every shopper gets the baseline again, and this test ends.'));
  await h.click('ship-ab-ask-yes');
  await until(h, () => byId(h, 'ship-ab-filed').length === 1, 'the rolled back test is filed');
  t = h.text();
  assert.ok(/Rolled back outdoor\/starter-ranker@v\d+ from (100|25)% of traffic: .+\. Every shopper gets outdoor\/keyword-baseline@v1 again\. Next: fix what went wrong and start a new test, or keep the baseline\./.test(t), `what, why and what next: ${t.slice(-1400)}`);
  assert.equal(byId(h, 'ship-ab-readout').length, 0, 'its readout is not shown as if current');
  assert.equal(byId(h, 'ship-ab-rollback').length, 0, 'no ramp or roll back on an ended test');

  // A site starts calling. Real requests are counted, the step is ticked, and the form opens for real.
  const ep = (await get('/api/projects/outdoor/staging')).staged.endpoint.url;
  const KH = { Authorization: 'Bearer render-test-key', 'Content-Type': 'application/json' };
  const site = async (path, body) => (await fetch(ep + path, { method: 'POST', headers: KH, body: JSON.stringify(body) })).json();
  const queries = ['waterproof jacket', 'hiking boots', 'tent', 'wool socks', 'headlamp', 'trail shoes'];
  for (let i = 0; i < 24; i++) await site('/rank', { query: queries[i % queries.length], k: 10, session_id: `shopper-${i}` });
  h = await page('traffic');
  await until(h, () => h.byIdPrefix('ship-done-connect').length === 1, 'step 3 is ticked once a site calls it');
  t = h.text();
  assert.ok(/Received 24 requests, the last one \d+ s ago\./.test(t), `the live line counts real requests: ${t.slice(-1500)}`);
  await until(h, () => h.text().includes('Next: Your site is connected. Start shadow mode to see what the candidate would change, before any shopper sees it.'), 'the top line follows, and offers shadow mode');
  t = h.text();

  // Shadow mode: shoppers keep the baseline, the candidate ranks the same requests, and this is what it would change.
  assert.ok(t.includes('No shopper sees the candidate.'), 'what shadow mode is, before it starts');
  await h.click('ship-shadow-start');
  await until(h, () => byId(h, 'ship-shadow-stop').length === 1, 'shadow mode is on');
  const seen = [];
  for (let i = 0; i < 30; i++) seen.push(await site('/rank', { query: queries[i % queries.length], k: 10, session_id: `shadowed-${i}` }));
  assert.ok(seen.every((r) => r.arm === 'baseline'), 'every shopper gets the baseline');
  h = await page('traffic');
  const sh = await get('/api/projects/outdoor/shadow');
  assert.equal(sh.requests, 30); assert.equal(sh.evidence, 'REAL_TRAFFIC');
  await until(h, () => h.text().includes(sh.text), `the shadow readout: ${sh.text}`);
  t = h.text();
  assert.ok(/Real traffic\. On 30 requests /.test(t) && t.includes('No shopper saw the candidate.') && /Response time, 95 in 100 requests under: [\d.]+ ms baseline, [\d.]+ ms candidate\./.test(t), 'requests, how often it differs, and both response times');
  if (sh.queries.length) { await h.click('ship-shadow-q0'); await until(h, () => h.mod.shipPath.result?.query === sh.queries[0].query && h.mod.shipPath.tab === 'try', 'a query that differs opens in Try it'); await tab(h, 'traffic'); }
  assert.equal(byId(h, 'ship-chip-traffic')[0]?.kids.join(''), 'Real traffic', 'now that real requests have arrived, the header says so');

  // The A/B form, for real: shares in this endpoint's own sessions a day.
  await until(h, () => byId(h, 'ship-ab-start').length === 1, 'Start an A/B test is offered now');
  const o = await get('/api/projects/outdoor/ab/options');
  assert.ok(o.ready && o.per_day > 0);
  await h.click('ship-ab-start');
  assert.equal(byId(h, 'ship-ab-practice').length, 0, 'not a practice test');
  assert.ok(h.text().includes(o.shares[1].text) && /5%: about [\d,]+ of the [\d,]+ sessions a day this endpoint sees/.test(o.shares[1].text), `the share in real numbers: ${o.shares[1].text}`);
  await h.click('ship-ab-split-50');
  assert.ok(h.text().includes(o.shares[3].text), 'and it follows the share picked');
  await h.click('ship-ab-primary-clicks');
  await h.click('ship-ab-start-go');
  await until(h, () => byId(h, 'ship-ab-readout').length === 1, 'the real test runs');
  assert.equal((await get('/api/projects/outdoor/shadow')).status, 'ended', 'a split ends shadow mode');
  assert.equal(byId(h, 'ship-ab-simulate').length, 0, 'no simulated shoppers in a real test');
  // Shoppers arrive. Those on the candidate click more.
  for (let i = 0; i < 520; i++) {
    const r = await site('/rank', { query: queries[i % queries.length], k: 10, session_id: `ab-${i}` });
    if (r.arm === 'candidate' ? i % 10 < 6 : i % 10 < 2) await site('/outcome', { request_id: r.request_id, event: 'click', product_id: r.ids[0] });
  }
  h = await page('traffic');
  ro = await get('/api/projects/outdoor/ab');
  assert.equal(ro.evidence, 'REAL_TRAFFIC'); assert.ok(ro.enough && ro.can_ship, JSON.stringify(ro.arms));
  await until(h, () => h.text().includes(ro.primary.text), 'the real readout');
  t = h.text();
  assert.ok(t.includes('Real traffic') && !t.includes('SIMULATED:') && t.includes('The candidate is ahead.'), `labelled real: ${t.slice(-1600)}`);
  await until(h, () => h.text().includes('Next: The A/B test has enough sessions. Decide: ship it, keep the baseline, or keep testing.'), 'the top line asks for a decision');
  // Ship it: recorded, and launch review opens with the candidate.
  globalThis.__overlays = [];
  await h.click('ship-ab-ship');
  await until(h, () => (globalThis.__overlays ?? []).some((x) => x.props?.task === 'handoff'), 'launch review opens');
  assert.equal(globalThis.__overlays.find((x) => x.props?.task === 'handoff').props.values.pipeline, ro.ab.arms.candidate);
  const dec = (await get('/api/projects/outdoor/ab')).decision;
  assert.equal(dec.choice, 'ship'); assert.equal(dec.evidence, 'REAL_TRAFFIC');
  h = await page('traffic');
  await until(h, () => /Decided: Ship it, by .+ on \d{4}-\d\d-\d\d\./.test(h.text()), 'the decision is on the page');
  assert.ok(/Next: Send outdoor\/starter-ranker@v\d+ to launch review\./.test(h.text()), 'and the top line moves on to launch review');
  const hist = (await get('/api/projects/outdoor/state')).history.map((x) => x.what).join(' | ');
  for (const w of ['started an A/B test', 'simulated shoppers', 'ramped the A/B test', 'rolled back the A/B test', 'the first real request reached staging', 'started shadow mode', 'decided on the A/B test: ship it']) assert.ok(hist.includes(w), `the project history has: ${w}`);
  steps = (await published(h, (v) => v?.steps?.[3]?.done))?.steps;
  assert.deepEqual(steps?.map((x) => [x.label, x.done]), [['1 Stage', true], ['2 Try it', true], ['3 Connect your site', true], ['4 A/B test', true], ['5 Launch review', false]], 'the five steps, for agents');
  clean(h.text(), 'after a real test');

  // Keep the baseline: recorded, rolled back, and said in one line.
  await h.click('ship-ab-keep');
  await until(h, () => byId(h, 'ship-ab-filed').length === 1 && h.text().includes('the decision was to keep the baseline'), 'kept the baseline');

  // My own A/B system: an address per version, and the readout the team brings back, labelled by what they say it was.
  await h.click('ship-ab-start');
  await h.click('ship-ab-mode-own');
  assert.ok(h.text().includes('Split traffic in your system, then bring the readout in here.'));
  await h.click('ship-ab-start-go');
  await until(h, () => byId(h, 'ship-ab-own').length === 1, 'the own-system view');
  t = h.text();
  assert.ok(t.includes('/rank/baseline') && t.includes('/rank/candidate') && t.includes('Bring in the readout'), `each version's address: ${t.slice(-900)}`);
  await post('/api/projects/outdoor/ab/result', { metric: 'conversion', baseline: 0.031, candidate: 0.034, ci_low: 0.001, ci_high: 0.005, source: 'our experiment system, test 12', traffic: 'real' });
  h = await page('traffic');
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
assert.ok(h.text().includes('Stopped') && h.byIdPrefix('ship-done-try').length === 0 && byId(h, 'ship-address').length === 0, 'the header says stopped, and the address is gone');
assert.equal(h.byIdPrefix('ship-choice-local').length > 0, true, 'and it can be staged again from here');
assert.equal(h.byIdPrefix('ship-try-box').length, 0, 'and the query box');
assert.ok(h.text().includes('Last staged:'), 'what was staged last stays on the card');
clean(h.text(), 'after stopping');
console.log(`staging render test: ok${haveTrainer ? '' : ' (baseline only: the train-ltr extra is not installed beside cst)'}`);
process.exit(0);
