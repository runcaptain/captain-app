// What if... (captain-sandbox) against a real backend (this repo's backend/core) on the demo store: one knob draws the
// line with whiskers, two draw the heat grid; full-pass cells carry verdicts; a picked cell shows its ripple and becomes
// the next version, then offers a backtest against the current one. The offline note is on screen.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/sandbox-live.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { cstBin } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { bin: cst, why } = cstBin();
if (!cst) { console.log(`sandbox-live render test: skipped (${why})`); process.exit(0); }

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-sandbox-'));
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), PYTHONPATH: path.join(root, 'backend/core') };
spawnSync(cst, ['init', 'sb', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} try { fs.rmSync(work, { recursive: true, force: true }); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
const call = async (method, p, body) => (await fetch(base + p, { method, headers: { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })).json();

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-sandbox-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-sandbox'), dir, { recursive: true });
globalThis.__props = { pipeline: 'demo-tuned@v1' };
const { default: Sandbox } = await import(path.join(dir, 'main.js'));
const until = async (h, ok, what, n = 400) => { for (let i = 0; i < n && !ok(); i++) { await h.layoutLists({}); await h.idle(150); } assert.ok(ok(), `${what}: ${h.text().slice(0, 700)}`); };
const h = harness(Sandbox);
await h.start();
await until(h, () => Boolean(h.mod.offer), 'the knobs offered');
const paths = h.mod.offer.map((k) => k.path);
assert.ok(paths.includes('boosts.0.weight') && paths.includes('fields.title') && paths.includes('k1'), `numeric settings: ${paths}`);
assert.equal(h.mod.queries, 'demo-queries');
assert.equal(h.mod.judgments, 'demo-judgments');

// One knob: the line, with whiskers and today's value.
await h.click('sb-knob-boosts.0.weight');
h.mod.states['boosts.0.weight'].set_value('1, 1.15, 1.4');
await h.click('sb-run');
await until(h, () => Boolean(h.mod.result), 'the one-knob sweep');
let t = h.text();
assert.ok(t.includes("Offline, from grades: it narrows what's worth an A/B test"), 'the offline note is shown');
assert.ok(h.byIdPrefix('sb-line').length, 'one knob draws the line');
assert.ok(t.includes('now 1.15'), "today's value is marked");
assert.ok(h.byIdPrefix('sb-full-0').length, 'full-pass cells');
assert.match(t, /\b(win|loss|noise)\b/, 'verdict chips');

// Two knobs: the heat grid.
await h.click('sb-knob-fields.title');
h.mod.states['fields.title'].set_value('2, 3');
await h.click('sb-run');
await until(h, () => Boolean(h.mod.result) && Object.keys(h.mod.result.knobs).length === 2, 'the two-knob sweep');
assert.ok(h.byIdPrefix('sb-cell-1.15-3').length, `two knobs draw a grid, today's cell included: ${h.text().slice(0, 300)}`);
assert.ok(h.text().includes(', now'), "today's cell is marked");

// A cell: its ripple, then the next version, then a backtest.
await h.click('sb-full-0');
assert.ok(h.mod.cell, 'a cell is picked');
h.mod.mustNot.set_value('running shoes');
await h.click('sb-ripple');
await until(h, () => Boolean(h.mod.ripple), 'the ripple');
t = h.text();
assert.ok(t.includes('MOVED MOST') && t.includes('REGRESSIONS') && t.includes('MUST NOT BREAK'), 'the ripple lists');
const before = (await call('GET', '/api/pipelines')).find((p) => p.name === 'demo-tuned').latest;
await h.click('sb-promote');
await until(h, () => Boolean(h.mod.promoted), 'promoted');
const after = (await call('GET', '/api/pipelines')).find((p) => p.name === 'demo-tuned').latest;
assert.equal(after, before + 1, 'a new version of demo-tuned');
assert.equal(h.mod.promoted.pipeline, `demo-tuned@v${after}`);
globalThis.__overlays = [];
await h.click('sb-backtest');
const o = globalThis.__overlays.at(-1);
assert.equal(o?.props?.task, 'backtest', 'Run a backtest opens the form');
assert.deepEqual(o.props.values, { a: `demo-tuned@v${before}`, b: `demo-tuned@v${after}` });

console.log('sandbox-live render test: ok');
process.exit(0);
