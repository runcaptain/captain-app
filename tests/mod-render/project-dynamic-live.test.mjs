// The project page driven by its slots, against a real backend on the demo store: the next step on top, then 1 What
// you're building, 2 Inputs (required first, optional ones removable, Add an input), 3 Train and compare (no
// evaluation set says so), 4 Ship. Set up with an agent: the agent gets a prompt in a tab, files a proposal, and the
// page shows it to accept per input; a connection it named asks snapshot or live. The prompt names the cst to run by
// its path. An agent session can't apply its own open proposal by hand, and Accept goes through over inputs someone
// added by hand since. Delete project keeps the data.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/project-dynamic-live.test.mjs
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
if (!cst) { console.log(`project dynamic render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-dyn-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
for (const k of ['CLAUDECODE', 'CST_AGENT', 'CODEX_SANDBOX', 'CODEX_THREAD_ID', 'CURSOR_AGENT']) delete env[k];
spawnSync(cst, ['init', 'dyn', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
spawnSync(cst, ['tables', 'pull', 'demo-store'], { env });
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
await post('/api/projects/outdoor/slots', { label: 'Embeddings', accepts: ['any'] });

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token, cst }, 'captain.scope': { kind: 'project', id: 'outdoor', label: 'Outdoor' } };
globalThis.__knownAgents = [];
const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-project-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-project'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
h.cx.write_to_clipboard = (t) => { globalThis.__clipboard = t; };
await h.start();
const until = async (ok, what, n = 80) => {
  for (let i = 0; i < n && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(150); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 1600)}`);
};
await until((t) => t.includes('Train and compare') && t.includes('Embeddings'), 'page loads');
let t = h.text();
// The workflow, top to bottom, with the next step first.
let at = 0;
for (const x of ['Next:', '1', "What you're building", 'Search ranking from events', '2', 'Inputs', '3', 'Train and compare', '4', 'Ship']) {
  const i = t.indexOf(x, at);
  assert.ok(i >= 0, `reads top to bottom: ${x} after ${at}`);
  at = i;
}
assert.ok(/Next: .*catalog/i.test(t), 'next names the first required input');
// Inputs: required first, the optional one removable, and Add an input.
const slots = h.mod.stages.inputs.map((i) => h.mod.slotOf(i));
const req = slots.filter((s) => s.required).map((s) => s.label);
assert.ok(req.length >= 2 && t.indexOf(req[0]) < t.indexOf('Embeddings'), 'required inputs come first');
assert.equal(h.byIdPrefix('in-embeddings-remove').length, 1, 'an optional input can be removed');
assert.equal(h.byIdPrefix(`in-${slots.find((s) => s.required).slot}-remove`).length, 0, 'a required one cannot');
assert.equal(h.byIdPrefix('in-add').length, 1, 'Add an input');
// No evaluation set: Train and compare says so and offers to add one.
// This recipe judges by replaying logged searches: it needs the events input, and the button says so.
assert.ok(h.byIdPrefix('exp-add-eval').length === 1 && t.includes('Replay of logged searches') && t.includes('Choose events'), `not ready to judge: ${JSON.stringify(h.mod.stages.evaluation)}`);
// Remove the optional input: gone from the project.
await h.click('in-embeddings-remove');
await until(() => h.byIdPrefix('in-embeddings').length === 0, 'removed');
assert.ok(!(await get('/api/projects/outdoor/state')).slots.some((s) => s.id === 'embeddings'));

// Set up with an agent: a prompt for the agent (here, on the clipboard: no agent installed), then its proposal.
await h.click('ph-assist');
await until(() => Boolean(h.mod.assist?.id), 'assist started');
assert.ok(String(globalThis.__clipboard ?? '').includes('cst projects propose outdoor'), 'the agent gets its prompt');
// The prompt names the cst to run by its path, once (an older cst can be first on the agent's PATH), and no file the
// agent's folder doesn't have.
const asked = String(globalThis.__clipboard);
assert.match(asked.split('\n')[0], /^Use this cst: \/.*\/cst$/, 'the first line is the cst path');
assert.equal(asked.split('Use this cst:').length, 2, 'named once');
assert.ok(!asked.includes('docs/trainer-contract.md') && asked.includes('docs trainer-contract') && asked.includes('CST_TRAIN_INPUTS'), 'the contract is a command, with its outline inline');
assert.ok(asked.includes('press\nAccept in Captain'), 'the agent is told it cannot apply the proposal');
// A prompt a screen wrote itself gets the same first line.
const { withCst } = await import(path.join(dir, 'kit/agent.js'));
assert.equal(withCst('Look at this run.').split('\n')[0], `Use this cst: ${cst}`);
assert.ok(withCst('Look at this run.').endsWith('\n\nLook at this run.'));
const prop = path.join(work, 'proposal.yaml');
fs.writeFileSync(path.join(work, 'train.py'), "print('train')\n");
fs.writeFileSync(prop, [
  'note: the products and the search log are in the demo store',
  'slots:',
  '  - {slot: catalog, source: {from: dataset, ref: demo-catalog@v1}, why: the only catalog}',
  '  - {slot: events, source: {from: connection, connection: demo-store, table: main.search_log}, mode: live, why: one row per search}',
  'trainer: {command: python train.py, script: {path: train.py, file: train.py}, why: a small ranker}',
].join('\n'));
const filed = spawnSync(cst, ['projects', 'propose', 'outdoor', '--file', prop, '--assist', h.mod.assist.id, '--agent', 'claude'], { env: { ...env, CST_AGENT: 'claude' }, encoding: 'utf8' });
assert.equal(filed.status, 0, filed.stdout + filed.stderr);
h.mod.load(h.cx);
await until((x) => x.includes('Claude Code proposes') && x.includes('the only catalog'), 'the proposal shows');
await until((x) => x.includes("print('train')"), 'the script text shows');
// Leave the script out, accept the rest: the catalog is pinned at once; the connection asks snapshot or live.
await h.click('assist-script-check');
assert.ok(h.text().includes('Accept the checked ones'));
await h.click('assist-accept-all');
await until(() => (h.mod.state?.pins?.catalog ?? null) === 'demo-catalog@v1', 'catalog pinned from the proposal');
await until(() => (h.mod.assist?.toConnect ?? []).length === 1, 'the connection asks snapshot or live');
await h.click('assist-connect-0-live');
await until(() => h.mod.state?.inputs?.events?.mode === 'live', 'events kept live');
t = h.text();
assert.ok(t.includes('search_log') && t.includes('live'), 'the card shows the connection and live');

// "Put that in", said to the agent: it files a proposal, then tries to apply it by hand. Refused, with what to tell
// the person. Then inputs get added by hand anyway (a person can), and Accept still goes through: the one that
// matches counts as done, the one with another definition is left alone and named, and the rest goes in.
const again = await post('/api/projects/outdoor/assist', {});
fs.writeFileSync(prop, [
  'note: three more inputs',
  'slots:',
  '  - {slot: sessions, add: true, label: Sessions, role: custom, why: for features}',
  '  - {slot: promos, add: true, label: Promos, role: catalog, accepts: [catalog], why: promoted products}',
  '  - {slot: stock, add: true, label: Stock, role: custom, why: availability}',
].join('\n'));
const agentEnv = { ...env, CLAUDECODE: '1' };
const second = spawnSync(cst, ['projects', 'propose', 'outdoor', '--file', prop, '--assist', again.assist], { env: agentEnv, encoding: 'utf8' });
assert.equal(second.status, 0, second.stdout + second.stderr);
const byHand = spawnSync(cst, ['projects', 'slot-add', 'outdoor', 'Sessions'], { env: agentEnv, encoding: 'utf8' });
assert.equal(byHand.status, 1, 'an agent session cannot apply its open proposal by hand');
assert.ok((byHand.stdout + byHand.stderr).includes('Tell the person to press Accept in Captain (Home, the proposal panel).'), byHand.stdout + byHand.stderr);
assert.ok(!(await get('/api/projects/outdoor/state')).slots.some((s) => s.id === 'sessions'), 'nothing was added');
await post('/api/projects/outdoor/slots', { label: 'Sessions' });
await post('/api/projects/outdoor/slots', { label: 'Promos', role: 'events', accepts: ['events'] });
h.mod.load(h.cx);
await until((x) => x.includes('Claude Code proposes') && x.includes('promoted products'), 'the second proposal shows');
await h.click('assist-accept-all');
await until((x) => x.includes('Accepted, except one input'), 'accept goes through and names the input it left');
t = h.text();
assert.ok(t.includes('Promos already exists with a different definition') && t.includes('role events here, catalog proposed'), t.slice(0, 600));
const after = await get('/api/projects/outdoor/state');
assert.ok(['sessions', 'promos', 'stock'].every((id) => after.slots.some((s) => s.id === id)), 'the inputs are all there');
assert.equal(after.slots.find((s) => s.id === 'promos').role, 'events', 'the one with another definition is untouched');
assert.equal((await get('/api/projects/outdoor/proposals'))[0].status, 'accepted');

// Delete project: the data is kept.
const fdir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), fdir, { recursive: true });
const { TASKS } = await import(path.join(fdir, 'tasks.js'));
const del = TASKS.find((x) => x.id === 'delete-project');
assert.equal((await del.submit({ project: 'outdoor', name: 'Outdoor', confirm: 'outdoor' }, {}, {})).ok, false, 'the name must match');
const r = await del.submit({ project: 'outdoor', name: 'Outdoor', confirm: 'Outdoor' }, {}, {});
assert.ok(r.ok && /Kept .*datasets/.test(r.message), r.error ?? r.message);
assert.ok(!(await get('/api/projects')).some((p) => p.id === 'outdoor'), 'the project is gone');
assert.ok((await get('/api/datasets')).some((d) => d.name === 'demo-catalog'), 'its data is kept');
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad} on the page`);
console.log('project dynamic render test: ok');
process.exit(0);
