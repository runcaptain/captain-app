// Home with projects but none picked (the Explorer's "All"), against a real backend on the demo store. Home is the
// first screen, so it asks before Captain's services are up: that must not read as "no projects". Once they answer,
// each project is a card: its champion and the verdict worth showing, how many experiments, and its next action.
// Clicking a card picks it, as the Explorer does, and the page opens on it.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/project-home-list-live.test.mjs
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
if (!cst) { console.log(`project home list render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-homelist-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'hl', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const post = async (p, body) => { const r = await fetch(base + p, { method: 'POST', headers: H, body: JSON.stringify(body) }); assert.ok(r.ok, `${p}: ${r.status}`); return r.json(); };

// Two projects: one with a champion and a judged experiment, one just made.
await post('/api/projects', { name: 'Outdoor', description: 'Product search for the outdoor store' });
await post('/api/projects/outdoor/pin', { catalog: 'demo-catalog', queries: 'demo-queries', holdout: 'demo-queries', labels: 'demo-judgments' });
await post('/api/projects/outdoor/experiments', { pipeline: 'demo-bm25' });
const tuned = await post('/api/projects/outdoor/experiments', { pipeline: 'demo-tuned', labels: 'demo-judgments' });
await post('/api/projects/outdoor/champion', { pipeline: 'demo-bm25' });
assert.equal(spawnSync(cst, ['projects', 'experiment-backtest', 'outdoor', tuned.id], { env }).status, 0);
await post('/api/projects', { name: 'Gifts', kind: 'search-judgments' });

// The services aren't up yet when Home first asks: no captain.daemon, nothing picked.
globalThis.__shared = {};
const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-project-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-project'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
assert.ok(!h.text().includes('Start with a project'), `still starting is not "no projects": ${h.text().slice(0, 300)}`);
// Now they answer: the retry finds both projects.
globalThis.__shared['captain.daemon'] = { state: 'ready', base, token: j.token };
const until = async (ok, what, n = 80) => {
  for (let i = 0; i < n && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(150); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 1500)}`);
};
await until((t) => t.includes('Outdoor') && t.includes('Gifts') && t.includes('2 models'), 'project cards');
const t = h.text();
const byId = (id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
assert.ok(!t.includes('Start with a project'), 'no empty state when projects exist');
// The page is "Projects", with no sentence under it. Each card: the name, what it is, how many models, its state in
// one plain sentence that is not cut off, and where it is deployed. One "Open" button. No code font, no tags.
assert.ok(byId('ph-name')[0].kids.join('') === 'Projects' && !t.includes('Home'), 'the page is called Projects');
assert.ok(!t.includes('Each one is a search') && !t.includes('search you'), 'no description under the title, and nothing that assumes search');
assert.ok(t.includes('2 models') && t.includes('demo-tuned: ') && t.includes('agrees with its own labeler'), `the model count and its state in a sentence: ${t}`);
assert.ok(t.includes('0 models') && t.includes('No model yet.') && t.includes('Not deployed'), 'a new project reads as new');
for (const word of ['candidate', 'No baseline yet', 'Nothing judged yet', 'New candidate', 'Choose a catalog']) assert.ok(!t.includes(word), `"${word}" is gone from the cards`);
{
  const { MONO } = await import(path.join(dir, 'kit/tokens.js'));
  const mono = [];
  const walk = (e) => { if (!e || typeof e !== 'object' || typeof e.call !== 'function') return; if (e.call('font_family')?.[0] === MONO) mono.push(e.call('id')?.[0] ?? '?'); for (const k of e.kids ?? []) walk(k); };
  walk(h.mod.render());
  assert.deepEqual(mono, [], 'no code font on the Projects list');
  const state = h.byIdPrefix('ph-p-').filter((e) => /-state$/.test(e.call('id')?.[0] ?? ''));
  assert.ok(state.length >= 2 && state.every((e) => !e.call('truncate')), 'the state sentence wraps: it is never cut off');
  const cards = h.byIdPrefix('ph-p-').filter((e) => /^ph-p-\d+$/.test(e.call('id')?.[0] ?? ''));
  for (const k of cards.keys()) assert.equal(h.byIdPrefix(`ph-p-${k}-open`).length, 1, 'one Open button a card');
  assert.equal(h.byIdPrefix('ph-p-').filter((e) => /-next$/.test(e.call('id')?.[0] ?? '')).length, 0, 'and no other button');
}
assert.equal(h.byIdPrefix('ph-new').length, 1, 'New project stays, as a secondary button');

// Clicking a card picks it, as the Explorer does, and the page opens on it.
const i = h.mod.projects.findIndex((p) => p.id === 'outdoor');
await h.click(`ph-p-${i}`);
assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'project', id: 'outdoor', label: 'Outdoor' });
await until((x) => x.includes('Data') && x.includes('Models') && x.includes('Ship'), 'the project page opens');

// The Open button on a card opens that project.
globalThis.__shared['captain.scope'] = null;
h.mod.allProjects(h.cx);
await until((x) => x.includes('Gifts') && x.includes('No model yet.'), 'back to the list');
globalThis.__overlays = [];
const g = h.mod.projects.findIndex((p) => p.id === 'gifts');
await h.click(`ph-p-${g}-open`);
assert.equal(globalThis.__shared['captain.scope']?.id, 'gifts', 'Open opens the project');
// The list is live. A model deployed from a terminal shows on its card at the next read, with no manual refresh,
// and so does stopping it. (The page reads again every five seconds: here the read is asked for directly.)
globalThis.__shared['captain.scope'] = null;
h.mod.allProjects(h.cx);
await until((x) => x.includes('Outdoor') && x.includes('Not deployed') && !x.includes('Running on'), 'nothing is deployed yet');
assert.equal(spawnSync(cst, ['deploy', 'outdoor', '--on', 'local'], { env }).status, 0, 'deployed from a terminal');
h.mod.load(h.cx);
await until((x) => x.includes('Running on this computer'), 'the card says where it runs, from the next read');
assert.equal(spawnSync(cst, ['deployments', 'stop', 'outdoor'], { env }).status, 0, 'stopped from a terminal');
h.mod.load(h.cx);
await until((x) => !x.includes('Running on'), 'and that it is no longer deployed');
console.log('project home list render test: ok');
process.exit(0);
