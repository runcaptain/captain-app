// New project starts with what you're building: one card per recipe, each saying what you'll need, then the name, then
// the project page. Against a real backend on the demo store.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/new-project-live.test.mjs
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
if (!cst) { console.log(`new-project render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-newproj-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'np', '--storage', path.join(work, 'store')], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__props = { task: 'project' };
globalThis.__runs = [];

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), dir, { recursive: true });
const { default: Forms } = await import(path.join(dir, 'main.js'));
const h = harness(Forms);
await h.start();
for (let i = 0; i < 30 && !h.text().includes('Recommendations'); i++) await h.idle(100);
let t = h.text();
// What are you building: four cards, each with what you'll need.
for (const r of ['Search ranking from events', 'Search ranking with judgments', 'Recommendations', 'Bring your own pipeline']) assert.ok(t.includes(r), `recipe card ${r}`);
assert.ok((t.match(/You'll need/g) ?? []).length >= 4, 'each card says what you will need');
assert.ok(t.indexOf('What are you building?') < t.indexOf('Name'), 'what you are building comes before the name');
// Pick one, name it, make it: the project page opens on it.
const ids = h.byIdPrefix('f-recipe-').map((e) => e.call('id')[0]);
assert.ok(ids.includes('f-recipe-recommendations'), `card ids: ${ids}`);
await h.click('f-recipe-recommendations');
assert.equal(h.mod.values.recipe, 'recommendations');
h.mod.values.name = 'Gift picks';
await h.click('form-go');
for (let i = 0; i < 30 && !globalThis.__shared['captain.scope']; i++) await h.idle(100);
assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'project', id: 'gift-picks', label: 'Gift picks' }, 'the new project is picked');
assert.ok(globalThis.__runs.some((r) => r[0] === 'layout.select' && r[1]?.child === 'project'), 'and its page opens');
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad}`);
console.log('new-project render test: ok');
process.exit(0);
