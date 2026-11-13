// Comparisons' Profile tab against a real backend (backend/core) on the demo store: profile a two-arm run (the summary
// sentence, one stage bar per arm, memory labelled as Python allocations, the slowest queries opening the Explain
// view, the "measured offline" label), then an imported run, whose arms are skipped with the reason.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/profile-live.test.mjs
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
if (!cst) { console.log(`profile-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-prof-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'prof', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
// An imported run (rankings from outside): its arms can't be replayed.
const python = fs.readFileSync(cst, 'utf8').split('\n')[0].replace(/^#!/, '');
spawnSync(python, [path.join(here, 'seed-imported.py')], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__props = {};

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-runs-prof-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-runs'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what, n = 120) => {
  for (let i = 0; i < n && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${typeof what === "function" ? what() : what}: ${h.text().slice(0, 300)}`);
};

await until(() => (h.mod.data.backtests ?? []).some((b) => b.name === 'demo-tuned-vs-bm25'), 'runs listed');
// "Where the time goes" is a section of the run's comparison, in Runs (it was Compare's Profile tab).
globalThis.__shared['captain.open'] = { section: 'runs', backtest: 'demo-tuned-vs-bm25', tab: 'profile', at: Date.now() };
await until((t) => t.includes('Profile this run') && h.mod.cmp.sel?.backtest === 'demo-tuned-vs-bm25' && !!h.mod.cmp.run && !h.mod.cmp.run.error, 'the section on the run');
assert.ok(h.text().includes('Measured offline on this computer, not production.'), 'labelled before anything runs');
await h.click('prof-q-50');
await h.click('prof-run');
await until((t) => / at p99 against /.test(t), () => `the summary sentence (${JSON.stringify(h.mod.cmp.profiler.cur)?.slice(0, 400)})`);
const res = h.mod.cmp.profiler.cur.result;
assert.equal(res.arms.length, 2, 'both arms timed');
let t = h.text();
assert.ok(/(adds|saves) [\d.]+ ms at p99 against/.test(t), 'adds or saves, in ms');
assert.ok(h.byIdPrefix('prof-bar-').length === 2, 'one stage bar per arm');
assert.ok(h.byIdPrefix('prof-seg-0-').length >= 1, 'stages in the bar');
assert.ok(t.includes('Memory, Python allocations, not process memory'), 'memory, labelled');
assert.ok(t.includes('Measured offline on this computer, not production.') && t.includes('one query at a time'), 'where it was measured');
assert.ok(t.includes('Slowest queries') && t.includes('Most time in'), 'the slowest queries');
// A slow query opens the Explain view for it.
globalThis.__overlays = [];
await h.click('prof-slow-0');
const ov = globalThis.__overlays.at(-1);
assert.equal(ov.mod, 'captain-explain');
assert.equal(ov.props.run, 'demo-tuned-vs-bm25@v1');
assert.ok(ov.props.query_id, 'with the query');

// An imported run: its arms can't be replayed, and the section says why.
globalThis.__shared['captain.open'] = { section: 'runs', backtest: 'demos/shot/imported-readout', version: 1, tab: 'profile', at: Date.now() };
await until((t) => t.includes('Profile this run') && h.mod.cmp.sel?.backtest === 'demos/shot/imported-readout' && !!h.mod.cmp.run, 'the imported run');
await h.click('prof-run');
await until(() => !!h.mod.cmp.profiler.cur?.result || !!h.mod.cmp.profiler.cur?.error, 'profiled');
t = h.text();
assert.ok(/isn't timed: from imported rankings, so it can't be replayed/.test(t) && t.includes('Nothing in this run can be timed.'), `skipped with the reason: ${t.slice(0, 600)}`);
assert.ok(!h.byIdPrefix('prof-bar-').length, 'no empty bars');

stop();
console.log('profile-live render test: ok');
process.exit(0);
