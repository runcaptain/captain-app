// Ship against a real backend, on this computer (no cloud): the project page offers where to deploy, one Deploy
// button runs it with its steps named, and the deployed model has one plain screen: the state in words, the
// address, a search box with the results beside the model it is compared with, how much it has answered, the cost,
// and Stop. Everything else is behind More: the requests, where the two models disagree, the check that it uses the
// numbers it was trained on, sending it real traffic, earlier versions, the log and the settings. With the starter
// trainer installed beside cst a trained model is deployed beside keyword search; without it, keyword search alone.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/deployed-live.test.mjs
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
if (!cst) { console.log(`deployed render test: skipped (${why})`); process.exit(0); }
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
const { MONO } = await import(path.join(pdir, 'kit/tokens.js'));
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const until = async (h, ok, what, tries = 240) => { for (let i = 0; i < tries && !ok(); i++) await h.idle(150); assert.ok(ok(), `${what}: ${h.text().slice(-1500)}`); };
const clean = (t, where) => {
  for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /CST_[A-Z]/, /[\u2190-\u21ff]/, /\u2014/, /\bpipeline/i, /\bchampion\b/i, /render-test-key/, /[Ss]taging\b/, /\bStage (it|the)\b/, /\bstaged\b/])
    assert.ok(!bad.test(t), `no ${bad} ${where}: ${t.match(bad)?.[0]} in ...${t.slice(Math.max(0, t.search(bad) - 80), t.search(bad) + 60)}`);
};
const more = async (h, id, ready = () => true) => { await h.click(`dep-more-${id}`); await until(h, () => byId(h, `dep-open-${id}`).length === 1 && ready(h.mod.shipPath), `More opens ${id}`); };
const mine = haveTrainer ? /starter-ranker@v\d+/ : /keyword-baseline@v\d+/;

// The project page: Ship holds only where to deploy. No cloud is connected here, so: this computer, and one prompt.
let h = harness(Page);
await h.start();
await until(h, () => byId(h, 'ship-deploy-local').length === 1, 'the project page offers this computer');
let t = h.text();
assert.ok(mine.test(t.match(/Where should (\S+) run\?/)?.[1] ?? ''), `the question names the model: ${t.slice(-700)}`);
assert.ok(t.includes('This computer') && t.includes('Only you can reach it.') && t.includes('Connect a cloud to deploy where a site can reach it'), 'this computer, and the prompt to connect a cloud');
assert.deepEqual(h.byIdPrefix('ship-deploy-').map((e) => e.call('id')[0]), ['ship-deploy-local'], 'no card for a cloud that is not connected');
if (haveTrainer) assert.ok(/Next: Deploy starter-ranker@v\d+ to try it live\./.test(t), `the next step is to deploy it, named without its project: ${t.slice(0, 500)}`);
clean(t.split('Ship').slice(-1)[0], 'in the Ship section');

// One button. On this computer there is nothing to confirm: it runs, and then the page says what is deployed.
await h.click('ship-deploy-local');
await until(h, () => byId(h, 'ship-deployed').length === 1 && h.mod.shipPath.running(), 'deployed on this computer');
assert.equal(byId(h, 'ship-confirm').length, 0, 'nothing to confirm for this computer');
t = h.text();
assert.ok(/Running on this computer\./.test(t) && /http:\/\/127\.0\.0\.1:\d+/.test(t) && t.includes('Open it'), `the project page says it is deployed, and where: ${t.slice(-600)}`);
const staged = (await get('/api/projects/outdoor/staging')).staged;
assert.equal(staged.status, 'running'); assert.equal(staged.kind, 'local');
assert.ok(staged.host === 'local', 'the record says which place it runs on');

// Open it: one plain screen.
await h.click('ship-open');
await until(h, () => byId(h, 'deployed').length === 1 && Boolean(h.mod.shipPath.met) && Boolean(h.mod.shipPath.conn), 'the deployed screen opens with its numbers');
t = h.text();
assert.equal(byId(h, 'ship-name')[0].kids.join(''), staged.model_version.replace(/^.*\//, ''), 'the model names the screen');
assert.ok(/Running on this computer\. (Answering requests|Ready, with nothing to do right now)\./.test(t), `the state in words: ${t.slice(0, 700)}`);
assert.match(byId(h, 'ship-address')[0].kids.join(''), /^http:\/\/127\.0\.0\.1:\d+$/, 'the address');
assert.equal(byId(h, 'ship-address')[0].call('font_family')?.[0], MONO, 'the address is in the code font: a person copies it');
assert.equal(byId(h, 'ship-name')[0].call('font_family'), undefined, 'a name is not');
// The deploy's own last steps sent five test searches through it, so it has already answered those.
assert.ok(t.includes('Only this computer can reach it.') && t.includes('Runs on this computer. No cloud cost.') && /It has answered 5 requests in the last hour\./.test(t), `who can reach it, the cost, and how much it has answered: ${t.slice(0, 900)}`);
for (const id of ['ship-copy-address', 'ship-copy-test', 'ship-copy-key', 'ship-stop']) assert.equal(byId(h, id).length, 1, id);
assert.equal(h.byIdPrefix('ship-send').length, 1, 'Send to agent');
assert.equal(h.byIdPrefix('dep-tab-').length, 0, 'no tabs');
let copied = null;
h.cx.write_to_clipboard = (x) => { copied = x; };
await h.click('ship-copy-address');
assert.match(copied, /^http:\/\/127\.0\.0\.1:\d+$/, 'Copy puts the address on the clipboard');
await h.click('ship-copy-test');
assert.ok(/^curl -s http:\/\/127\.0\.0\.1:\d+\/rank -H "Authorization: Bearer \$CAPTAIN_STAGING_KEY"/.test(copied) && !copied.includes('render-test-key'), `a test request that reads the key from the shell: ${copied}`);
await h.click('ship-copy-key');
await until(h, () => copied === 'render-test-key', 'Copy its key reads it from the keychain');
assert.ok(!h.text().includes('render-test-key'), 'and never draws it');
const labels = () => h.byIdPrefix('dep-more-').map((e) => e.kids[0].kids[0]);
assert.deepEqual(labels(), [...['Requests it has answered'], ...(haveTrainer ? ['Where the two models disagree'] : []), 'Is it healthy?', 'Check it uses the same numbers it was trained on', 'Use it from your site', 'Earlier versions', 'Logs', 'Settings'], 'More: labels that say what each is for, and no review before there is traffic');
clean(t, 'on the deployed screen');

// Try a search: the results, beside the model it is compared with, by that model's own name.
await h.click('ship-try-go');
assert.ok(h.text().includes('Type a query to try.'), 'an empty search is refused in words');
h.mod.shipPath.box.set_value('waterproof jacket');
await h.click('ship-try-go');
await until(h, () => h.byIdPrefix('ship-try-results').length === 1, 'results come back');
t = h.text();
const res = h.mod.shipPath.result;
assert.equal(res.staged.results.length, 8);
for (const x of res.staged.results.slice(0, 3)) assert.ok(t.includes(x.title), `a product on screen: ${x.title}`);
if (haveTrainer) {
  assert.equal(byId(h, 'ship-try-baseline').length, 1, 'the other model beside it');
  assert.ok(t.includes('This model') && t.includes('Compared with keyword-baseline') && /differ from keyword-baseline|same results in the same order/.test(t), `side by side, by its own name: ${t.slice(-900)}`);
}
assert.ok(t.includes('Searches typed here are tests. They never count as shoppers.'), 'searches typed here are labelled tests');
clean(t, 'with results');
h.mod.shipPath.result = null;

// Is it healthy? A real check against the real endpoint, recorded, with the target a person sets.
await more(h, 'health', (x) => Boolean(x.healthD));
await h.click('dep-health-check');
await until(h, () => h.mod.shipPath.healthD?.status === 'healthy' && Boolean(h.mod.shipPath.healthD?.checked_at), 'a check runs and is recorded');
assert.ok(/Healthy: it is answering\. Only \d+ requests? in the last 15 minutes, too few to judge/.test(h.text()), `it says what it could judge: ${h.text().slice(-700)}`);
await h.click('dep-health-p95-200');
await until(h, () => h.mod.shipPath.healthD?.target?.p95_ms === 200, 'the target is saved');
assert.equal((await get('/api/projects/outdoor/staging/health')).target.set, true);
await h.click('dep-open-close');

// The check that it uses the numbers it was trained on: behind More with its last result.
await more(h, 'check');
assert.ok(h.text().includes('It should pass before you put it in production.'), 'it opens with what it is for');
await h.click('dep-check-now');
await until(h, () => Boolean(h.mod.shipPath.checkResult), 'the check answers');
t = h.text();
assert.ok(/Served features match training: 100% of \d+ checked|Not checked: the staged version ranks without trained features|nothing/i.test(h.mod.shipPath.checkResult.text ?? ''), `the result: ${JSON.stringify(h.mod.shipPath.checkResult).slice(0, 300)}`);
if (haveTrainer) assert.ok(/Served features match training: 100% of \d+ checked/.test(t), `a trained model's numbers match: ${t.slice(-500)}`);
assert.equal(byId(h, 'dep-check-failed').length, 0, 'nothing is wrong, so nothing is said at the top');
await h.click('dep-open-close');

// Where the two models disagree: the searches worth typing, and picking one runs it.
if (haveTrainer) {
  await until(h, () => Boolean(h.mod.shipPath.diff), 'the comparison is read');
  assert.ok(h.text().includes('Not sure what to type? See where this model and keyword-baseline disagree'), 'a way to it under the search box');
  await more(h, 'differ');
  const d = h.mod.shipPath.diff;
  assert.ok(d.checked >= 5, JSON.stringify(d).slice(0, 300));
  assert.ok(h.text().includes('Searches where this model and keyword-baseline return the most different results.'), 'it opens with its sentence');
  if (d.queries.length) { await h.click('dep-differ-0'); await until(h, () => h.mod.shipPath.result?.query === d.queries[0].query, 'picking one runs that search'); h.mod.shipPath.result = null; }
  else { assert.ok(byId(h, 'dep-differ-none').length === 1, 'none differing is said, not left blank'); await h.click('dep-open-close'); }
}

// What it has answered: the sentence on the screen, and each request behind More with its own log lines.
h.mod.shipPath.load(h.cx); h.mod.shipPath.loadTab(h.cx);
await until(h, () => /It has answered \d+ requests? in the last hour\. Usually in (under 1|\d+) ms at the endpoint, almost always within \d+ ms\./.test(h.text()), 'how much and how fast, in a sentence');
await more(h, 'requests', (s) => Boolean(s.reqs?.rows?.length));
t = h.text();
assert.ok(t.includes('waterproof jacket') && t.includes('Each request it answered'), `the requests, under their sentence: ${t.slice(-700)}`);
await h.click('dep-request-0');
await until(h, () => byId(h, 'dep-request-id').length === 1 && byId(h, 'dep-request-lines').length === 1, 'a request opens beside the list, with its own log lines');
assert.ok(/Ranked '.+' with the (candidate|baseline) in [\d.]+ ms: \d+ results\./.test(h.text()), 'what the model wrote for that request');
await h.click('dep-open-close');

// Use it from your site: on this computer the page says a site can't reach it, and gives an address a model.
await more(h, 'traffic', (s) => Boolean(s.conn));
t = h.text();
assert.ok(t.includes('Use it from your site') && t.includes("A site can't reach a model on this computer. Deploy it to a cloud, so your site can call it."), `the honest line: ${t.slice(-900)}`);
assert.ok(t.includes("To compare models on real shoppers, split traffic in your own testing tool and send each group to its model's address.") && t.includes('The deployed model') && /http:\/\/127\.0\.0\.1:\d+\/rank/.test(t), `an address a model, and the paragraph: ${t.slice(-900)}`);
assert.ok(!t.includes('A/B') && !t.includes('simulated shoppers'), 'nothing about an A/B test');
assert.ok(/Not counted: \d+ test\./.test(t), 'searches typed here are shown as not counted');
assert.ok(!t.includes('render-test-key'), 'the key is never on screen');
for (const id of ['ship-connect-code-curl', 'ship-connect-code-javascript', 'ship-connect-code-python']) assert.equal(byId(h, id).length, 1, id);
await h.click('ship-connect-code-python');
t = h.text();
assert.ok(t.includes('def rank(query, session_id):') && t.includes('"traffic": "real"') && t.includes('/timing') && !t.includes('/outcome') && t.includes('CAPTAIN_STAGING_KEY'), 'the code a site uses: it labels its shoppers and reports its own time, and nothing a shopper did');
await h.click('dep-open-close');

// Earlier versions, the log and the settings, each opening with what it is for.
await more(h, 'versions', (s) => Boolean(s.vers));
assert.ok(h.text().includes('Every version deployed here, newest first') && byId(h, 'dep-version-1').length === 1, 'earlier versions');
await until(h, () => Boolean(h.mod.shipPath.backs), 'its roll backs are read');
assert.ok(h.text().includes('Off: an unhealthy version stays up until you roll it back.'), `automatic roll back is off until asked for: ${h.text().slice(-500)}`);
await h.click('dep-auto-30');
await until(h, () => h.mod.shipPath.backs?.auto?.on === true, 'and a click turns it on for real');
assert.equal((await get('/api/projects/outdoor/staging/rollbacks')).auto.minutes, 30);
await h.click('dep-open-close');
await more(h, 'logs', (s) => Boolean(s.logsD));
assert.ok(/Ready in [\d.]+ s: /.test(h.text()) && h.text().includes('Streaming'), 'the log, growing');
await h.click('dep-open-close');
await more(h, 'settings', (s) => Boolean(s.setD) && Boolean(s.copiesD));
t = h.text();
assert.ok(t.includes('Where it runs') && t.includes('Stopping when idle') && t.includes('4 h with no requests') && t.includes('One copy is running.'), `settings and the running copy: ${t.slice(-900)}`);
await h.click('dep-set-edit');
await h.click('dep-set-idle-60');
await until(h, () => h.mod.shipPath.setNote?.tone === 'good', 'the idle time changes while it runs');
assert.equal((await get('/api/projects/outdoor/staging/settings')).idle_minutes, 60);
await h.click('dep-open-close');

// What an agent is told when the deployment is pinned: no key, no secret.
const ctxOut = spawnSync(cst, ['--json', 'context', 'add', 'staging:outdoor'], { env, encoding: 'utf8' });
assert.equal(ctxOut.status, 0, ctxOut.stderr);
const shown = spawnSync(cst, ['context', 'show'], { env, encoding: 'utf8' }).stdout;
assert.ok(shown.includes(staged.model_version) && !shown.includes('render-test-key') && !/KEY/.test(shown), `agent context: ${shown.slice(0, 600)}`);

// Stop asks first. Stopped is said in words, and it can be deployed again from the same screen.
await h.click('ship-stop');
assert.ok(h.text().includes('Stop it? The address goes away. What it answered is kept.'), 'Stop asks');
await h.click('ship-stop-yes');
await until(h, () => !h.mod.shipPath.running() && h.text().includes('Stopped. Nothing is running and nothing is billing.'), 'stopped, in words');
t = h.text();
assert.ok(byId(h, 'ship-address').length === 0 && byId(h, 'ship-deploy-local').length === 1, `no address, and Deploy is offered again: ${t.slice(-600)}`);
assert.equal((await get('/api/projects/outdoor/staging')).status, 'stopped');
clean(t, 'after stopping');
console.log('deployed render test: ok');
process.exit(0);
