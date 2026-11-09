// "Run a backtest": picking Compare and With shows at once, against a real backend (backend/core) on the demo store,
// with more than eight pipelines so the long list draws (as in Edgar's workspace). The click's context goes stale the
// moment its handler returns, as the app's does: a pick that waits on it to redraw never shows.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/backtest-pick-live.test.mjs
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
if (!cst) { console.log(`backtest-pick-live render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-btpick-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'bt', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const python = fs.readFileSync(cst, 'utf8').split('\n')[0].replace(/^#!/, '');
// Ten more pipelines, copies of demo-bm25, so the choice draws as the long list.
spawnSync(python, ['-c', `
from captain_search.workspace import open_context
from captain_search import ops
ctx = open_context()
m = ctx.registry.load("pipeline", "demo-bm25")
for i in range(10):
    ctx.registry.publish(m.model_copy(update={"name": f"ranker-{i}", "version": 0}))
`], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };
globalThis.__props = { task: 'backtest' };

const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-bt-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), dir, { recursive: true });
const { default: Forms } = await import(path.join(dir, 'main.js'));
const h = harness(Forms);
await h.start();
const until = async (ok, what) => {
  for (let i = 0; i < 60 && !ok(h.text()); i++) { await h.idle(150); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 500)}`);
};
await until(() => (h.mod.options?.a ?? []).length > 8 && (h.mod.options?.b ?? []).length > 8, 'more than eight pipelines to pick from');

// A click the way the app delivers it: its cx is good only while the handler runs.
const click = (id) => {
  const el = h.byIdPrefix(id).find((e) => e.call('id')[0] === id);
  assert.ok(el, `no ${id}`);
  let live = true, shown = false, stale = 0;
  const ecx = {
    notify() { if (live) shown = true; else stale++; },
    spawn: (f) => h.cx.spawn(f),
    stop_propagation() {},
  };
  el.call('on_click')[0]({}, ecx);
  live = false;
  return () => ({ shown, stale });
};
const a = h.mod.options.a.find((o) => o.label === 'demo-bm25').value;
const b = h.mod.options.b.find((o) => o.label === 'demo-tuned').value;
let r = click(`f-a-${a}`);
await h.idle(200);
assert.equal(h.mod.values.a, a, 'Compare picked');
assert.deepEqual(r(), { shown: true, stale: 0 }, 'the pick shows at once, nothing waits on the click');
r = click(`f-b-${b}`);
await h.idle(200);
assert.equal(h.mod.values.b, b, 'With picked');
assert.deepEqual(r(), { shown: true, stale: 0 });
// Both picks change what's drawn: each chosen row is the selected one.
const picked = h.byIdPrefix('f-a-').concat(h.byIdPrefix('f-b-')).filter((e) => [`f-a-${a}`, `f-b-${b}`].includes(e.call('id')[0]));
assert.equal(picked.length, 2, 'both chosen rows drawn');
stop();
console.log('backtest-pick-live render test: ok');
process.exit(0);
