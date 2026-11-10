// "The agent sees what I'm seeing": each screen publishes what it shows (shared "captain.view.<section>", kit/view.js),
// and the prompts Captain hands an agent start with it. Against a real backend on the demo store:
// - picking a project publishes it with the page's Next line, and picking another replaces it;
// - switching sections changes what currentView() and the prompt snapshot say;
// - a form over the page publishes its filled fields with every secret hidden (a token field, a URL's login);
// - "Ask an agent about this screen" sends the screen to the open agent tab, or opens one when there is none.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/view-context-live.test.mjs
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
if (!cst) { console.log(`view-context render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-view-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'viewtest', '--storage', path.join(work, 'store')], { env });
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

// Two projects: one with nothing chosen yet (the page asks for its first input), one with a catalog pinned.
await post('/api/projects', { name: 'Outdoor' });
await post('/api/projects', { name: 'Gifts' });
await post('/api/projects/gifts/pin', { catalog: 'demo-catalog' });

const sections = (active) => ({ id: 'root', children: [{ id: 'sections', active, children: ['project', 'data', 'runs'].map((id) => ({ id })) }] });
globalThis.__layout = sections('project');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token, cst }, 'captain.workspace': 'viewtest',
  'captain.scope': { kind: 'project', id: 'outdoor', label: 'Outdoor' } };
const shared = (k) => globalThis.__shared[k];
const load = async (mod) => {
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), dir, { recursive: true });
  return { dir, Mod: (await import(path.join(dir, 'main.js'))).default };
};
const until = async (h, ok, what, n = 80) => {
  for (let i = 0; i < n && !ok(); i++) { await h.layoutLists({}); await h.idle(150); }
  assert.ok(ok(), `${what}: ${JSON.stringify(shared('captain.view.project'))} | ${h.text().slice(0, 600)}`);
};

// --- A project picked: the page publishes it, with its Next line ---
const project = await load('captain-project');
const hp = harness(project.Mod);
await hp.start();
await until(hp, () => shared('captain.view.project')?.next?.text, 'the project page publishes its view');
let v = shared('captain.view.project');
assert.equal(v.project.id, 'outdoor');
assert.equal(v.project.name, 'Outdoor');
assert.equal(v.workspace, 'viewtest');
assert.ok(typeof v.at === 'number', 'stamped');
assert.ok(hp.text().includes(`Next: ${v.next.text}`), `the published Next line is the one on the page: ${v.next.text}`);
const firstNext = v.next.text;
const firstAt = v.at;

// Nothing changed: the timers run and nothing is written again.
await hp.layoutLists({});
assert.equal(shared('captain.view.project').at, firstAt, 'no write while nothing changes');

// --- Another project picked: the view follows ---
globalThis.__shared['captain.scope'] = { kind: 'project', id: 'gifts', label: 'Gifts' };
await until(hp, () => shared('captain.view.project')?.project?.id === 'gifts' && shared('captain.view.project')?.project?.name === 'Gifts' && shared('captain.view.project')?.next?.text, 'the view follows the pick');
v = shared('captain.view.project');
assert.ok(hp.text().includes(`Next: ${v.next.text}`), 'the new page\'s Next line');
assert.ok(v.at >= firstAt);

// --- The prompt snapshot: workspace, project, screen, the Next line ---
const { currentView, viewSnapshot, withView, safeFields, safeValue } = await import(path.join(project.dir, 'kit/view.js'));
let cur = currentView();
assert.equal(cur.section, 'project');
assert.equal(cur.screen, 'Home');
assert.equal(cur.view.project.id, 'gifts');
let snap = viewSnapshot();
assert.match(snap, /^What the person has open in Captain right now:/);
assert.ok(snap.includes('- Workspace: viewtest') && snap.includes('- Project: Gifts (id gifts)') && snap.includes('- Screen: Home'), snap);
assert.ok(snap.includes(`- Next: ${v.next.text}`), snap);
assert.ok(snap.includes('cst app context') && snap.includes('cst app shot'), 'says how to look again');
assert.ok(!/[←-⇿—]/.test(snap), 'no arrows or em dashes');

// --- Switch sections: Data and signals publishes its own part, and the snapshot changes screen ---
// (The whole workspace, as after "Show the whole workspace": the project's own list of datasets may be empty.)
globalThis.__shared['captain.scope'] = null;
const data = await load('captain-data');
const hd = harness(data.Mod);
await hd.start();
for (let i = 0; i < 80 && !shared('captain.view.data')?.dataset; i++) { await hd.layoutLists({}); await hd.idle(150); }
const dv = shared('captain.view.data');
assert.equal(dv?.tab, 'datasets', JSON.stringify(dv));
assert.ok(dv.dataset, `a dataset is selected and published: ${JSON.stringify(dv)}`);
globalThis.__layout = sections('data');
cur = currentView();
assert.equal(cur.section, 'data');
assert.equal(cur.screen, 'Data and signals');
assert.equal(cur.view.dataset, dv.dataset);
snap = viewSnapshot();
assert.ok(snap.includes('- Screen: Data and signals, tab datasets') && snap.includes(`- Dataset: ${dv.dataset}`), snap);
assert.ok(!snap.includes('- Next:'), 'the project page\'s Next line is not this screen\'s');
// Back to Home: the project again.
globalThis.__shared['captain.scope'] = { kind: 'project', id: 'gifts', label: 'Gifts' };
globalThis.__layout = sections('project');
assert.equal(currentView().view.project.id, 'gifts');
// A part published for another workspace is not this window's.
globalThis.__shared['captain.workspace'] = 'elsewhere';
assert.equal(currentView().view, null, 'another workspace\'s part is dropped');
globalThis.__shared['captain.workspace'] = 'viewtest';

// --- Prompts Captain builds start with the screen; the cst line stays first ---
const { withCst, askAgent, askAboutScreen } = await import(path.join(project.dir, 'kit/agent.js'));
const built = withCst(withView('Look at this run.'));
assert.equal(built.split('\n')[0], `Use this cst: ${cst}`);
assert.ok(built.indexOf('What the person has open') < built.indexOf('Look at this run.'), 'the screen comes before the task');
const service = withCst(withView(`Use this cst: ${cst}\nRun it.\n\nHelp set up the project.`));
assert.equal(service.split('\n')[0], `Use this cst: ${cst}`, 'a prompt the services wrote keeps its first line');
assert.ok(service.indexOf('What the person has open') < service.indexOf('Help set up the project.'));
assert.equal(service.split('What the person has open').length, 2, 'once');
assert.equal(withView(service), service, 'never twice');

// --- Ask an agent about this screen: no agent tab opens one, an open one gets the message ---
globalThis.__agents = [{ id: 'claude', name: 'Claude Code', installed: true, run: ['claude'] }];
globalThis.__runs = [];
await askAboutScreen(hp.cx);
let opened = globalThis.__runs.find(([n]) => n === 'tab.new');
assert.ok(opened, 'a new agent tab');
assert.ok(opened[1].task.includes('- Project: Gifts (id gifts)') && opened[1].task.includes('cst app context'), opened[1].task);
globalThis.__agentList = [{ address: 't7', status: 'idle', tab_name: 'Claude Code', focused: true }];
globalThis.__runs = []; globalThis.__hc = [];
await askAboutScreen(hp.cx);
assert.equal(globalThis.__hc.length, 1, 'one message to the open tab');
const [verb, addr, text] = globalThis.__hc[0];
assert.deepEqual([verb, addr], ['send', 't7']);
assert.ok(!text.includes('\n'), 'one line: a newline would send half of it');
assert.ok(text.includes('cst app context') && text.includes('Project: Gifts (id gifts)') && text.includes('Screen: Home'), text);
assert.ok(!globalThis.__runs.some(([n]) => n === 'tab.new'), 'no second tab');
assert.ok(globalThis.__runs.some(([n, a]) => n === 'agent.focus' && a.terminal === 't7'), 'the tab is shown');
globalThis.__agentList = [];

// --- A form over the page: its fields are published, secrets never ---
const TOKEN = 'ghp_FAKEfakeFAKEfakeFAKEfake0123456789';
const forms = await load('captain-forms');
globalThis.__props = { task: 'mlflow', overlay: 'o1' };
const hf = harness(forms.Mod);
await hf.start();
Object.assign(hf.mod.values, { from: 'uri', uri: 'https://mlops:hunter2secret@mlflow.example.test/api?token=abc123def&x=1', token: TOKEN });
await hf.layoutLists({});
const dlg = shared('captain.view.dialog');
assert.equal(dlg.mod, 'captain-forms');
assert.equal(dlg.form, 'mlflow');
assert.equal(dlg.fields.token, '(hidden)', 'a secret field says it was filled, never what with');
assert.equal(dlg.fields.uri, 'https://(hidden)@mlflow.example.test/api?token=(hidden)&x=1');
const everything = JSON.stringify(globalThis.__shared, (k, val) => (k === 'captain.daemon' ? undefined : val));
for (const leak of [TOKEN, 'hunter2secret', 'abc123def']) assert.ok(!everything.includes(leak), `${leak} is nowhere in what the screens published`);
globalThis.__layout = sections('project');
assert.ok(viewSnapshot().includes('- Open form: Connect MLflow') || viewSnapshot().includes('- Open form:'), viewSnapshot());
assert.ok(!viewSnapshot().includes(TOKEN));
hf.mod.close();
assert.equal(shared('captain.view.dialog'), null, 'closing the form clears it');
globalThis.__props = {};

// --- The helpers alone ---
assert.deepEqual(safeFields({ name: 'prod', password: 'p', api_key: 'k', empty: '', note: 'fine' }), { name: 'prod', password: '(hidden)', api_key: '(hidden)', note: 'fine' });
assert.deepEqual(safeFields({ 'af:pat': 'x', plain: 'y' }, ['af:pat']), { 'af:pat': '(hidden)', plain: 'y' });
assert.equal(safeValue('postgresql://app:s3cret@db.example.test:5432/shop'), 'postgresql://(hidden)@db.example.test:5432/shop');
assert.equal(safeValue('AKIA' + 'ABCDEFGHIJKLMNOP'), '(hidden)');
assert.equal(safeValue('patagonia-catalog-2026'), 'patagonia-catalog-2026', 'an ordinary name is not a secret');
assert.equal(safeValue('demo-catalog@v2'), 'demo-catalog@v2');

console.log('view-context render test: ok');
stop();
process.exit(0);
