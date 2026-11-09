// Runs, Live tab, against a real backend (backend/core, or `cst` on PATH) on the demo store: Run backtest jumps to the
// run's live view, which fills in as it goes (stages, progress, both arms' conversion, the same simulated shoppers on
// each arm), and ends with a link to the result. A run a team's own tool streams through `cst live pipe` shows too.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/runs-live.test.mjs
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
if (!cst) { console.log(`runs-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-live-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'live', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
// A team's own trainer, streaming through cst live pipe: a log line and a falling loss.
const trainer = 'import json\nprint("epoch 1")\nfor i in range(5): print(json.dumps({"type": "metric", "name": "loss", "value": 1/(i+1), "step": i}))';
spawnSync(cst, ['live', 'pipe', '--title', 'house-trainer', '--kind', 'training', '--', 'python3', '-c', trainer], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-runs-live-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-runs'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what, n = 160) => {
  for (let i = 0; i < n && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(250); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 1200)}`);
};

// Run backtest: Runs switches to Live, on this run.
await until((t) => t.includes('demo-tuned-vs-bm25'), 'backtests listed');
await h.click('backtests-demo-tuned-vs-bm25');
await until(() => !!h.mod.detail.backtest, 'backtest open');
await h.click('run-backtest');
await until(() => h.mod.tab === 'live' && !!h.mod.live.run, 'switched to the live view');
const id = h.mod.live.sel;
await until((t) => t.includes('Simulated shoppers'), 'shoppers stream in');
let t = h.text();
assert.ok(t.includes('Simulate 4,000 shoppers on 2 arms') || t.includes('Compare the arms'), 'stages shown');
assert.ok(t.includes('conversion') || t.includes('Conversion'), 'conversion tile');
const run = h.mod.live.run;
const arms = run.head.arms;
const firstPair = run.shoppers.filter((s) => s.shopper === run.shoppers[0].shopper).map((s) => s.arm);
assert.deepEqual([...new Set(firstPair)].sort(), [...arms].sort(), 'the same shopper on each arm');
assert.ok(/clicked #\d|bought|left/.test(t), 'what a shopper did, in words');
await until(() => h.mod.live.run?.head.status === 'done', 'run finishes');
await until((t) => t.includes('Open the result'), 'link to the result');
assert.ok([...run.metrics.values()].some((m) => m.name === 'conversion' && m.points.length > 3), 'conversion line fills in');
await h.click('live-open-result');
assert.equal(h.mod.tab, 'backtests');

// The team's trainer, from cst live pipe: in the list, its loss and its log.
await h.click('tab-live');
await until((t) => t.includes('house-trainer'), 'piped run listed');
const piped = h.mod.live.list.find((x) => x.title === 'house-trainer');
await h.click(`live-${piped.id}`);
await until((t) => t.includes('epoch 1') && t.includes('loss'), 'piped log and metric');
assert.notEqual(piped.id, id);
console.log('runs-live render test: ok');
process.exit(0);
