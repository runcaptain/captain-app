// Render test against a real, seeded Captain backend (cst serve on a scratch workspace), through the real kit/cst.js:
// no fixtures. Comparisons, Runs and Data load the real lists. Then the backend dies: every screen, Investigations
// included, shows the plain "Captain's services stopped." with Retry, and never the transport's own words ("error
// sending request for url", a host or a port). Then a new backend comes up and each screen recovers by itself.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/live-backend.test.mjs
// Needs `cst` on PATH. Skips (exit 0, says so) without it.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';
import { cstBin } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { bin: cst, why } = cstBin();
if (!cst) { console.log(`live-backend render test: skipped (${why})`); process.exit(0); }
const python = fs.readFileSync(cst, 'utf8').split('\n')[0].replace(/^#!/, '');

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-live-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'live', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed'], { env });
spawnSync(python, [path.join(here, 'seed-imported.py')], { env });

let serve = null;
async function startServe() {
  serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
  const line = await new Promise((resolve, reject) => {
    let buf = '';
    serve.stdout.on('data', (d) => { buf += d; if (buf.includes('\n')) resolve(buf.split('\n')[0]); });
    serve.on('exit', () => reject(new Error('cst serve exited')));
  });
  const j = JSON.parse(line);
  globalThis.__shared['captain.daemon'] = { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token };
}
function stopServe() { serve?.kill('SIGTERM'); serve = null; }
process.on('exit', stopServe);
process.on('uncaughtException', (e) => { stopServe(); console.error(e); process.exit(1); });
process.on('unhandledRejection', (e) => { stopServe(); console.error(e); process.exit(1); });


globalThis.__shared = {};
await startServe();
const FORBIDDEN = /error sending request|fetching http|127\.0\.0\.1|localhost|\bport\b|curl|engine|daemon|Traceback/i;

async function mount(mod) {
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-live-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), dir, { recursive: true });
  const { default: Mod } = await import(path.join(dir, 'main.js'));
  const h = harness(Mod);
  await h.start();
  for (let i = 0; i < 6; i++) await h.idle(150);
  return h;
}
const until = async (h, ok, what) => {
  for (let i = 0; i < 40 && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(150); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 300)}`);
};

// 1. The real lists, from the real backend.
const cmp = await mount('captain-compare');
await until(cmp, (t) => t.includes('demo-historic-replay') && t.includes('demos/shot/imported-readout'), 'Comparisons lists the backtests');
assert.ok(!/Couldn't load backtests/.test(cmp.text()), 'Comparisons: no load error');
await until(cmp, (t) => /B minus A/.test(t), 'Comparisons opens the first run');
const runs = await mount('captain-runs');
await until(runs, (t) => t.includes('demo-tuned-vs-bm25'), 'Runs lists the backtests');
const data = await mount('captain-data');
await until(data, (t) => t.includes('demo-catalog'), 'Data lists the datasets');
const inv = await mount('captain-investigations');

// 2. The backend dies. Each screen's next request fails in transport. (Past the kit's 15 s patience for services
// coming up, which each screen's kit counts from when it loaded, so a failure is a failure.)
const realNow = Date.now;
Date.now = () => realNow() + 60000;
stopServe();
await new Promise((r) => setTimeout(r, 800));
cmp.mod.error = null; cmp.mod.backtests = null; cmp.mod.loadBacktests(cmp.cx);
runs.mod.load(runs.cx, 'backtests');
data.mod.load(data.cx);
inv.mod.table?.load?.(inv.cx);
for (const h of [cmp, runs, data, inv]) await h.idle(300);
if (process.env.SHOW) console.log('CMP', cmp.mod.error, JSON.stringify(cmp.mod.errorR), cmp.mod.starting, (cmp.mod.backtests ?? []).length);
for (const [name, h] of [['Comparisons', cmp], ['Runs', runs], ['Data', data], ['Investigations', inv]]) {
  const t = h.text();
  const bad = t.match(FORBIDDEN);
  assert.ok(!bad, `${name} shows "${bad?.[0]}": ${t.slice(Math.max(0, t.search(FORBIDDEN) - 100), t.search(FORBIDDEN) + 100)}`);
  assert.ok(t.includes("Captain's services stopped."), `${name} says the services stopped: ${t.slice(0, 300)}`);
  assert.ok(t.includes('Retry'), `${name} offers Retry`);
}

// 3. A new backend comes up: each screen recovers on its own.
await startServe();
await until(cmp, (t) => t.includes('demo-historic-replay') && !t.includes("services stopped"), 'Comparisons recovers by itself');
await until(runs, (t) => t.includes('demo-tuned-vs-bm25') && !t.includes("services stopped"), 'Runs recovers by itself');
await until(data, (t) => t.includes('demo-catalog') && !t.includes("services stopped"), 'Data recovers by itself');

stopServe();
fs.rmSync(work, { recursive: true, force: true });
console.log('live-backend render test: ok');
process.exit(0);
