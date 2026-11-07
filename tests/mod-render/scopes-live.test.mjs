// The Explorer, its scope actions and Settings > Scope model, against a real backend (this repo's backend/core) and
// the real kit: no fixtures. Adds a brand, a site in it and an engine in the site through captain-scope, reads them
// back in the Explorer, moves, renames and archives, then edits the scope model: a loop is refused on screen, a
// change the services refuse shows their reason as it is, and a new type saves.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/scopes-live.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { cstBin, tidy, startScopeModel } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { bin: cst, why } = cstBin();
if (!cst) { console.log(`scopes-live render test: skipped (${why})`); process.exit(0); }

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-scopes-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'scopes', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
// A new workspace's scope model is empty: start it with Brand, then Site, as the one-click start does.
await startScopeModel(`http://127.0.0.1:${j.port}`, j.token);
const get = async (p) => (await fetch(base + p, { headers: { Authorization: `Bearer ${j.token}` } })).json();

// Each mod from its own copy, as the app loads them.
const copy = async (name) => {
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${name}-`)));
  fs.cpSync(path.join(root, 'defaults/mods', name), dir, { recursive: true });
  return dir;
};
const navDir = await copy('captain-nav');
const { Explorer } = await import(path.join(navDir, 'explorer.js'));
const { palette } = await import(path.join(navDir, 'kit/tokens.js'));
class ExplorerView { init(_p, cx) { this.ex = new Explorer(); this.ex.start(cx); } render() { return this.ex.render(palette(false)); } }
const { default: ScopeMod } = await import(path.join(await copy('captain-scope'), 'main.js'));
const { default: ModelMod } = await import(path.join(await copy('captain-scope-model'), 'main.js'));

const findScope = (tree, name) => {
  let hit = null;
  const walk = (n) => { if ((n.kind === 'scope' || n.kind === 'engine') && n.name === name) hit = n; (n.children ?? []).forEach(walk); };
  tree.forEach(walk);
  return hit;
};

/** Let the harness run until `ok()` holds (a cold backend answers slower than one settle). */
async function until(h, ok, what) {
  for (let i = 0; i < 60 && !ok(); i++) await h.idle(100);
  assert.ok(ok(), what);
}

/** One action through captain-scope, as the Explorer's menu opens it. */
async function act(props, fill) {
  globalThis.__props = props;
  globalThis.__shared['captain.explorer.rev'] = null;
  const h = harness(ScopeMod);
  globalThis.props = props;
  await h.start();
  await until(h, () => Boolean(h.mod.model), 'the dialog loaded the model');
  await fill?.(h);
  await h.click('scope-save');
  await until(h, () => !h.mod.busy, 'the action finished');
  return h;
}

// 1. A new workspace: only what every workspace has, from the services (live).
let ex = harness(ExplorerView);
await ex.start();
await until(ex, () => Boolean(ex.mod.ex.tree), 'the Explorer loaded');
let t = ex.text();
// Edgar, 2026-11-07: everything through projects. Projects come first; no All engines or All experiments views; and
// no bare Brands or Sites groups while the scope model has no levels with anything in them.
assert.ok(t.includes('Projects') && t.includes('Scope model'), `base tree: ${t}`);
for (const gone of ['All engines', 'All experiments', 'Brands']) assert.ok(!t.includes(gone), `no ${gone}: ${t}`);
assert.ok(t.indexOf('Projects') < t.indexOf('Scope model'), 'Projects first');
assert.equal(ex.mod.ex.live, true, 'the Explorer reads the services');
assert.ok(t.includes('No projects yet: add one'), 'an empty Projects says so');
await new Promise((r) => setTimeout(r, 1300)); // past the Explorer's first moments, when a click is ignored
globalThis.__overlays = [];
await ex.click('ex-projects/none');
assert.equal(globalThis.__overlays.at(-1)?.props?.task, 'project', 'No projects yet: add one opens New project');

// All engines with none yet: what an engine is, an example, and a button that opens the add dialog.
const { default: EngineMod } = await import(path.join(await copy('captain-engine'), 'main.js'));
const none = harness(EngineMod);
await none.start();
await until(none, () => none.text().includes('No engines yet'), 'the engines page explains an empty workspace');
assert.ok(none.text().includes('AptDeco product search'), 'it gives an example');
globalThis.__overlays = [];
await none.click('eng-add-first');
assert.equal(globalThis.__overlays.at(-1)?.mod, 'captain-scope', 'Add your first engine opens the add dialog');
assert.equal(globalThis.__overlays.at(-1)?.props?.kind, 'engine');

// 2. Add a brand, a site in it, and an engine in the site.
let h = await act({ action: 'add', kind: 'scope', type: 'brand', parent: null }, (h) => h.mod.name.set_value('Acme Outdoors'));
assert.ok(!h.mod.error, `add brand: ${h.mod.error}`);
let tree = (await get('/api/explorer')).tree;
const brand = findScope(tree, 'Acme Outdoors');
assert.ok(brand, 'the brand is in the tree');
h = await act({ action: 'add', kind: 'scope', type: 'site', parent: { id: brand.id, type: 'brand', name: brand.name } }, (h) => h.mod.name.set_value('US storefront'));
assert.ok(!h.mod.error, `add site: ${h.mod.error}`);
tree = (await get('/api/explorer')).tree;
const site = findScope(tree, 'US storefront');
assert.ok(site, 'the site is in the tree');
h = await act({ action: 'add', kind: 'engine', type: 'engine', parent: { id: site.id, type: 'site', name: site.name } }, (h) => h.mod.name.set_value('Product search'));
assert.ok(!h.mod.error, `add engine: ${h.mod.error}`);

// 3. The Explorer shows them, nested.
ex = harness(ExplorerView);
await ex.start();
await until(ex, () => ex.text().includes('Product search'), 'the Explorer shows the new engine');
t = ex.text();
for (const name of ['Brands', 'Acme Outdoors', 'Sites', 'US storefront', 'Engines', 'Product search']) assert.ok(t.includes(name), `${name} in the Explorer: ${t}`);
assert.ok(t.indexOf('Acme Outdoors') < t.indexOf('US storefront') && t.indexOf('US storefront') < t.lastIndexOf('Product search'), 'nested in order');

// 4. A click in the Explorer's first moments is ignored (the click that brings the window forward lands on whatever
//    is under the pointer): Scope model doesn't open by itself. A click after that does.
globalThis.__overlays = [];
await ex.click('ex-scope-model');
assert.equal(globalThis.__overlays.length, 0, 'an early click opened nothing');
await new Promise((r) => setTimeout(r, 1300));
await ex.click('ex-scope-model');
assert.equal(globalThis.__overlays.at(-1)?.mod, 'captain-scope-model', 'a later click opens Scope model');

// A project: made, given the demo's catalog, tuned pipeline and backtest, then picked in the Explorer, it opens its
// home, where each item opens where it lives.
const postJ = async (p, body) => (await fetch(base + p, { method: 'POST', headers: { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
const proj = await postJ('/api/projects', { name: 'Acme search' });
await postJ('/api/projects/assign', { project: proj.id, items: [{ kind: 'dataset', name: 'demo-catalog' }, { kind: 'pipeline', name: 'demo-tuned' }, { kind: 'backtest', name: 'demo-tuned-vs-bm25' }] });
globalThis.__shared['captain.explorer.rev'] = Date.now(); // what New project does; the Explorer re-reads its tree
ex.mod.ex.load(ex.cx);
await until(ex, () => ex.text().includes('Acme search'), 'the Explorer lists the project');
t = ex.text();
assert.ok(t.indexOf('Acme search') < t.indexOf('Acme Outdoors'), 'projects come before brands');
await new Promise((r) => setTimeout(r, 500)); // the rows just moved: a click right away is ignored
globalThis.__runs = [];
await ex.click(`ex-${proj.id}`);
assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'project', id: proj.id, label: 'Acme search' }, 'clicking it picks the project');
assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'project' }], 'and opens its home');
const { default: ProjectMod } = await import(path.join(await copy('captain-project'), 'main.js'));
const home = harness(ProjectMod);
await home.start();
await until(home, () => home.text().includes('demo-catalog'), 'the home lists its dataset');
for (const thing of ['demo-tuned@v1', 'demo-tuned-vs-bm25', 'DATASETS', 'RECENT RUNS', 'PIPELINES', 'MODELS', 'NOTEBOOKS']) assert.ok(home.text().includes(thing), `the home shows ${thing}: ${home.text().slice(0, 500)}`);
await home.click('ph-ds-0');
assert.equal(globalThis.__shared['captain.open']?.section, 'data');
assert.equal(globalThis.__shared['captain.open']?.dataset, 'demo-catalog', 'a dataset opens in Data');
await home.click('ph-run-0');
assert.equal(globalThis.__shared['captain.open']?.section, 'comparisons');
assert.equal(globalThis.__shared['captain.open']?.backtest, 'demo-tuned-vs-bm25', 'a run opens in Comparisons');
globalThis.__overlays = [];
await home.click('ph-pl-0');
assert.equal(globalThis.__overlays.at(-1)?.mod, 'captain-sandbox', 'a pipeline opens What if');
globalThis.__shared['captain.scope'] = null;

// 5. Picking the engine sets the scope the views filter to.
tree = (await get('/api/explorer')).tree;
const engine = findScope(tree, 'Product search');
await ex.click(`ex-${engine.id}`);
assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'engine', id: engine.id, label: 'Product search' });

// 5b. Picking a brand narrows Data and Runs on screen: one dataset and one experiment linked to it show, the rest
//     of the demo workspace doesn't (the kit sends X-CST-Scope; the services filter, 4d6a983d).
const post = async (p, body) => (await fetch(base + p, { method: 'POST', headers: { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
await post('/api/scopes/link', { items: [{ kind: 'dataset', name: 'demo-catalog' }, { kind: 'backtest', name: 'demo-historic-replay' }], scope: brand.id });
globalThis.__shared['captain.scope'] = { kind: 'scope', id: brand.id, label: 'Acme Outdoors' };
const { default: DataMod } = await import(path.join(await copy('captain-data'), 'main.js'));
const data = harness(DataMod);
await data.start();
await until(data, () => data.text().includes('demo-catalog'), 'Data shows the brand\'s dataset');
for (const other of ['demo-queries', 'demo-judgments']) assert.ok(!data.text().includes(other), `Data hides ${other}, which isn't the brand's`);
const { default: RunsMod } = await import(path.join(await copy('captain-runs'), 'main.js'));
const runs = harness(RunsMod);
await runs.start();
await until(runs, () => runs.text().includes('demo-historic-replay'), 'Runs shows the brand\'s experiment');
assert.ok(!runs.text().includes('demo-tuned-vs-bm25'), 'Runs hides the experiment that isn\'t the brand\'s');
globalThis.__shared['captain.scope'] = null;

// 6. Move the site to the top level (the first target), rename the engine, archive the brand.
h = await act({ action: 'move', node: { kind: 'scope', id: site.id, type: 'site', name: site.name } }, async (h) => { await h.click('scope-t-0'); });
assert.ok(!h.mod.error, `move: ${h.mod.error}`);
h = await act({ action: 'rename', node: { kind: 'engine', id: engine.id, type: 'search', name: engine.name } }, (h) => h.mod.name.set_value('Site search'));
assert.ok(!h.mod.error, `rename: ${h.mod.error}`);
h = await act({ action: 'archive', node: { kind: 'scope', id: brand.id, type: 'brand', name: brand.name } });
assert.ok(!h.mod.error, `archive: ${h.mod.error}`);
tree = (await get('/api/explorer')).tree;
assert.equal(findScope(tree, 'Acme Outdoors'), null, 'the archived brand left the tree');
assert.ok(findScope(tree, 'US storefront'), 'the moved site is still there');
assert.ok(findScope(tree, 'Site search'), 'the engine has its new name');

// 7. The scope model: a loop is refused on screen, before anything is sent.
let m = harness(ModelMod);
await m.start();
await until(m, () => Boolean(m.mod.model) && m.mod.live, 'the scope model loaded');
await m.click('sm-tabs-hierarchy');
await m.click('h-brand-site');
assert.match(String(m.mod.error), /would make a loop/, `loop refused: ${m.mod.error}`);

// 8. A change the services refuse shows their reason as it is: removing Site while US storefront is one.
await m.click('sm-tabs-types');
await m.click('type-rm-site');
await m.click('sm-save');
await until(m, () => !m.mod.busy, 'the save finished');
assert.match(String(m.mod.error), /US storefront/, `the services' reason: ${m.mod.error}`);

// 9. A new type saves, and the model's revision moves.
m = harness(ModelMod);
await m.start();
await until(m, () => Boolean(m.mod.model) && m.mod.live, 'the scope model loaded again');
const before = (await get('/api/scope-model')).revision;
m.mod.newType.set_value('Region');
await m.click('type-add');
await m.click('sm-save');
await until(m, () => !m.mod.busy, 'the second save finished');
assert.ok(!m.mod.error, `save: ${m.mod.error}`);
const after = await get('/api/scope-model');
assert.ok(after.types.some((x) => x.key === 'region' && x.singular === 'Region'), 'Region is a type');
assert.ok(after.revision > before, 'the revision moved');

console.log('scopes-live render test: ok');
process.exit(0);
