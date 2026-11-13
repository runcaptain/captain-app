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
// The app's own folder for this test, where the pins file lives: the services look at it for what a terminal pins.
const appHome = path.join(work, 'app-home');
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), CAPTAIN_APP_HOME: appHome };
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
assert.equal(cur.screen, 'Projects');
assert.equal(cur.view.project.id, 'gifts');
let snap = viewSnapshot();
assert.match(snap, /^What the person has open in Captain right now:/);
assert.ok(snap.includes('- Workspace: viewtest') && snap.includes('- Project: Gifts (id gifts)') && snap.includes('- Screen: Projects'), snap);
assert.ok(snap.includes(`- Next: ${v.next.text}`), snap);
assert.ok(snap.includes('cst app context') && snap.includes('cst app shot'), 'says how to look again');
assert.ok(!/[←-⇿—]/.test(snap), 'no arrows or em dashes');

// --- Switch sections: Data publishes its own part, and the snapshot changes screen ---
// (The whole workspace, as after "Show the whole workspace": the project's own list of datasets may be empty.)
globalThis.__shared['captain.scope'] = null;
const data = await load('captain-data');
const hd = harness(data.Mod);
await hd.start();
// Data opens on the one list of tables; opening a row's page is what selects a dataset.
for (let i = 0; i < 80 && !(hd.mod.catalog.rows ?? []).some((r) => r.kind === 'dataset'); i++) { await hd.layoutLists({}); await hd.idle(150); }
assert.equal(shared('captain.view.data')?.dataset ?? null, null, 'nothing is selected on the list');
hd.mod.catalog.openRow(hd.cx, hd.mod.catalog.rows.find((r) => r.kind === 'dataset'));
for (let i = 0; i < 80 && !shared('captain.view.data')?.dataset; i++) { await hd.layoutLists({}); await hd.idle(150); }
const dv = shared('captain.view.data');
assert.equal(dv?.tab, 'tables', JSON.stringify(dv));
assert.ok(dv.dataset, `a dataset is selected and published: ${JSON.stringify(dv)}`);
globalThis.__layout = sections('data');
cur = currentView();
assert.equal(cur.section, 'data');
assert.equal(cur.screen, 'Data');
assert.equal(cur.view.dataset, dv.dataset);
snap = viewSnapshot();
assert.ok(snap.includes('- Screen: Data, tab tables') && snap.includes(`- Dataset: ${dv.dataset}`), snap);
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
assert.ok(text.includes('cst app context') && text.includes('Project: Gifts (id gifts)') && text.includes('Screen: Projects'), text);
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

// --- Send to agent: a dataset is pinned for the agent and a reference lands in the open agent tab's input ---
const pinFile = '/tmp/captain-app/context/viewtest.json';
const pinned = () => JSON.parse(globalThis.__files?.[pinFile] ?? '{"items":[]}').items;
globalThis.__layout = sections('data');
let clip = null;
hd.cx.write_to_clipboard = (t) => { clip = t; };
globalThis.__shared['captain.scope'] = null; // the whole workspace again, so Data lists the demo datasets
for (let i = 0; i < 80 && hd.byIdPrefix('send-dataset').length === 0; i++) { await hd.layoutLists({}); await hd.idle(150); }
assert.equal(hd.byIdPrefix('send-dataset').length, 1, 'the dataset page offers Send to agent');
globalThis.__agentList = [{ address: 't7', status: 'idle', tab_name: 'Claude Code', focused: true }];
globalThis.__runs = []; globalThis.__hc = [];
// The data screen publishes the dataset's version on a timer: wait for it, so the expected ref isn't "@vundefined".
for (let i = 0; i < 80 && shared('captain.view.data')?.version == null; i++) { await hd.layoutLists({}); await hd.idle(150); }
await hd.click('send-dataset');
await hd.idle(50);
assert.deepEqual(pinned().map((i) => [i.ref, i.kind]), [[`dataset:${dv.dataset}@v${shared('captain.view.data').version}`, 'dataset']], 'pinned in the app home, per workspace');
assert.ok(pinned()[0].added_at && pinned()[0].added_by, 'with who and when');
assert.deepEqual(globalThis.__runs.map(([n]) => n), ['layout.show', 'agent.focus', 'terminal.paste'], 'pasted into the open tab, not sent');
const pasted = globalThis.__runs.at(-1)[1].text;
assert.ok(pasted.includes(`dataset ${dv.dataset}@v${shared('captain.view.data').version}`) && pasted.includes('cst context show') && !pasted.includes('\n'), `one line of reference: ${pasted}`);
assert.equal(clip, null, 'the clipboard is not touched: what the person copied is still there');
assert.equal(globalThis.__hc.length, 0, 'nothing is submitted for the person');
// The same thing sent twice is pinned once.
await hd.click('send-dataset');
await hd.idle(50);
assert.equal(pinned().length, 1);
// No agent tab open: one opens, with the reference as its task.
globalThis.__agentList = []; globalThis.__runs = [];
const ctxKit = await import(path.join(data.dir, 'kit/context.js'));
await ctxKit.sendToAgent(hd.cx, { ref: 'run:demo-tuned-vs-bm25', kind: 'run', label: 'demo-tuned-vs-bm25' });
opened = globalThis.__runs.find(([n]) => n === 'tab.new');
assert.ok(opened && opened[1].task.includes('run demo-tuned-vs-bm25') && opened[1].task.includes('cst context show'), 'a new agent tab gets it');
assert.equal(pinned().length, 2);

globalThis.__shared['captain.scope'] = { kind: 'project', id: 'gifts', label: 'Gifts' };
// --- The sidebar's Agent context strip: the screen, always, and each pinned thing with an x ---
const nav = await load('captain-nav');
globalThis.__nextCommand = () => new Promise(() => {}); // no command arrives
globalThis.__props = {};
globalThis.__layout = sections('project');
const hn = harness(nav.Mod);
await hn.start();
for (let i = 0; i < 20 && !hn.text().includes('demo-tuned-vs-bm25'); i++) { await hn.layoutLists({}); await hn.idle(30); }
let nt = hn.text();
assert.ok(nt.includes('AGENT CONTEXT') && nt.includes('Projects, Gifts'), `the current screen is in the strip: ${nt.slice(-600)}`);
assert.ok(nt.includes(`${dv.dataset}@v${shared('captain.view.data').version}`) && nt.includes('demo-tuned-vs-bm25') && nt.includes('Clear'), 'the pinned things are listed');
assert.ok(!/[←-⇿—]/.test(nt), 'no arrows or em dashes in the sidebar');
// Something pinned from the command line (cst context add) shows up without a click.
// The sidebar does not read the file on a clock: the services see it change on disk and say so on the live channel
// ("pins"). So the file changes where the services look, as `cst context add` would change it, and in the
// window's own view of it.
for (let i = 0; i < 8; i++) { await hn.layoutLists({}); await hn.idle(120); } // the sidebar is listening
globalThis.__files[pinFile] = JSON.stringify({ items: [...pinned(), { ref: 'project:gifts', kind: 'project', label: 'Gifts', added_by: 'cli', added_at: 'now' }] });
fs.mkdirSync(path.join(appHome, 'context'), { recursive: true });
fs.writeFileSync(path.join(appHome, 'context', 'viewtest.json'), globalThis.__files[pinFile]);
for (let i = 0; i < 40 && hn.byIdPrefix('ctx-pin-2').length === 0; i++) { await hn.layoutLists({}); await hn.idle(30); }
assert.ok(hn.byIdPrefix('ctx-pin-2').length > 0, 'a pin added by cst appears');
await hn.click('ctx-pin-0-x');
await hn.idle(50);
assert.deepEqual(pinned().map((i) => i.ref), ['run:demo-tuned-vs-bm25', 'project:gifts'], 'x removes that one');
await hn.click('ctx-clear');
await hn.idle(50);
assert.deepEqual(pinned(), [], 'Clear removes them all');
assert.ok(hn.text().includes('AGENT CONTEXT') && hn.text().includes('Projects, Gifts') && !hn.text().includes('Clear'), 'the screen stays: it is always included');

// --- cst app open: the sidebar shows what an agent asked to show; a form opens filled and is never submitted ---
// (The sidebar also runs commands of its own, such as making a first space: look for ours among them.)
const ran = (name, args) => assert.ok(globalThis.__runs.some(([n, x]) => n === name && JSON.stringify(x) === JSON.stringify(args)), `${name} ${JSON.stringify(args)} in ${JSON.stringify(globalThis.__runs)}`);
const openWith = async (t) => { globalThis.__runs = []; globalThis.__overlays = []; globalThis.__shared['captain.agent.open'] = { ...t, at: Date.now() + Math.random() }; await hn.layoutLists({}); };
await openWith({ section: 'runs' });
ran('layout.select', { stack: 'sections', child: 'runs' });
await openWith({ section: 'data', dataset: 'demo-catalog', tab: 'sample' });
assert.equal(shared('captain.open').dataset, 'demo-catalog');
assert.equal(shared('captain.open').tab, 'sample');
assert.equal(shared('captain.scope'), null, 'a record outside the picked project still opens: the whole workspace is shown');
assert.ok(globalThis.__toasts.at(-1).title === 'Showing the whole workspace', 'and the person is told');
ran('layout.select', { stack: 'sections', child: 'data' });
await openWith({ project: 'outdoor', label: 'Outdoor' });
assert.deepEqual(shared('captain.scope'), { kind: 'project', id: 'outdoor', label: 'Outdoor' });
ran('layout.select', { stack: 'sections', child: 'project' });
await openWith({ settings: 'structure' });
ran('app.settings', { section: 'structure' });
await openWith({ form: 'investigation', values: { title: 'Do long queries regress?' } });
assert.deepEqual(globalThis.__overlays.at(-1).props, { task: 'investigation', values: { title: 'Do long queries regress?' } });
assert.equal(globalThis.__overlays.at(-1).mod, 'captain-forms');
const before = globalThis.__overlays.length;
await hn.layoutLists({});
assert.equal(globalThis.__overlays.length, before, 'acted on once');
globalThis.__shared['captain.scope'] = { kind: 'project', id: 'gifts', label: 'Gifts' };

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
