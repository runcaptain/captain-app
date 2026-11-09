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
await post('/api/projects', { name: 'Gifts' });

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
await until((t) => t.includes('Outdoor') && t.includes('Gifts') && t.includes('2 experiments'), 'project cards');
const t = h.text();
assert.ok(!t.includes('Start with a project'), 'no empty state when projects exist');
assert.ok(t.includes('demo-bm25') && t.includes('demo-tuned: agrees with its own labeler'), 'champion and the verdict worth showing');
assert.ok(t.includes('No champion yet') && t.includes('0 experiments') && t.includes('Nothing judged yet'), 'a new project reads as new');
assert.ok(t.includes('Choose a catalog') && t.includes('New experiment'), 'each card has its next action');
assert.equal(h.byIdPrefix('ph-new').length, 1, 'New project stays, as a secondary button');

// Clicking a card picks it, as the Explorer does, and the page opens on it.
const i = h.mod.projects.findIndex((p) => p.id === 'outdoor');
await h.click(`ph-p-${i}`);
assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'project', id: 'outdoor', label: 'Outdoor' });
await until((x) => x.includes('Inputs') && x.includes('Train and compare') && x.includes('Ship'), 'the project page opens');

// The next action on the new project picks it and opens the pin form for its catalog.
globalThis.__shared['captain.scope'] = null;
h.mod.allProjects(h.cx);
await until((x) => x.includes('Gifts') && x.includes('Choose a catalog'), 'back to the list');
globalThis.__overlays = [];
const g = h.mod.projects.findIndex((p) => p.id === 'gifts');
await h.click(`ph-p-${g}-next`);
assert.equal(globalThis.__shared['captain.scope']?.id, 'gifts');
assert.equal(globalThis.__overlays.at(-1)?.props?.task, 'pin');
assert.equal(globalThis.__overlays.at(-1)?.props?.values?.slot, 'catalog');
console.log('project home list render test: ok');
process.exit(0);
