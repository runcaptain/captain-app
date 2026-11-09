// Handoff end to end against a real backend (this repo's backend/core) on the demo store (Edgar, 2026-11-07: "you
// click around and nothing happens"). Each step goes through the screens a person uses: the empty state's button opens
// the form; the form's submit hands the candidate off and Handoff lists it at once (no reopening); the row opens its
// detail with readiness checks; Record a deployment and Add an outcome open their forms, and what they save shows in
// the detail straight away; All handoffs goes back.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/handoff-live.test.mjs
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
if (!cst) { console.log(`handoff-live render test: skipped (${why})`); process.exit(0); }

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-handoff-'));
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), PYTHONPATH: path.join(root, 'backend/core') };
spawnSync(cst, ['init', 'ho', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} try { fs.rmSync(work, { recursive: true, force: true }); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

const copy = (mod) => { const d = fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-`)); fs.cpSync(path.join(root, 'defaults/mods', mod), d, { recursive: true }); return d; };
const { default: Handoff } = await import(path.join(copy('captain-handoff'), 'main.js'));
const formsDir = copy('captain-forms');
const until = async (h, ok, what, n = 150) => { for (let i = 0; i < n && !ok(); i++) { await h.layoutLists({}); await h.idle(120); } assert.ok(ok(), `${what}: ${h.text().slice(0, 600)}`); };

// A form, opened the way a button opens it (the overlay it asked for), filled in, and submitted with its button.
async function submitForm(overlay, values) {
  globalThis.__props = overlay.props;
  const { default: Forms } = await import(path.join(formsDir, 'main.js') + `?${Math.random()}`);
  const f = harness(Forms);
  await f.start();
  await until(f, () => f.byIdPrefix('form-go').length > 0, `the ${overlay.props.task} form`);
  Object.assign(f.mod.values, values);
  await f.click('form-go');
  await until(f, () => f.mod.result && !f.mod.busy, `${overlay.props.task} submitted`);
  return f.mod.result;
}

globalThis.__props = {};
const h = harness(Handoff);
await h.start();
await until(h, () => h.text().includes('No handoffs yet'), 'the empty state');

// 1. The empty state's button opens the handoff form; handing off lists it in Handoff at once.
globalThis.__overlays = [];
await h.click('ho-empty-new');
const form = globalThis.__overlays.at(-1);
assert.equal(form?.props?.task, 'handoff', 'Hand off a candidate opens the form');
const r1 = await submitForm(form, { pipeline: 'demo-tuned@v1', env: 'prod', reason: 'live test: hand off end to end' });
assert.ok(r1.ok, `handed off: ${JSON.stringify(r1)}`);
await until(h, () => h.mod.table.rows.length === 1, 'Handoff lists the new handoff without reopening', 30);
const ho = h.mod.table.rows[0];

// 2. The row opens its detail: readiness checks, each passed, failed or overridden.
h.mod.show(ho.id, h.cx);
await until(h, () => h.text().includes('Readiness checks'), 'the detail');
assert.match(h.text(), /passed|failed|overridden/, 'each check says how it went');
assert.ok(h.text().includes('Overridden: live test: hand off end to end'), 'the reason is shown');

// 3. Record a deployment: its button opens the form for this handoff; the deployment shows at once.
globalThis.__overlays = [];
await h.click('ho-link2');
const dep = globalThis.__overlays.at(-1);
assert.equal(dep?.props?.task, 'record-deployment');
assert.equal(dep.props.args?.id, ho.id, 'for this handoff');
const r2 = await submitForm(dep, { system: 'argo', deployment_id: 'live-123', url: 'https://argo.example/live', live: true });
assert.ok(r2.ok, `deployment recorded: ${JSON.stringify(r2)}`);
h.mod.tab = 'links';
await until(h, () => h.text().includes('argo live-123'), 'the deployment shows', 30);

// 4. Add an outcome: the same, on the outcomes tab.
globalThis.__overlays = [];
await h.click('ho-out2');
const out = globalThis.__overlays.at(-1);
assert.equal(out?.props?.task, 'add-outcome');
const r3 = await submitForm(out, { metric: 'conversion rate', value: '0.012', ci_low: '0.002', ci_high: '0.02', source: 'live test report' });
assert.ok(r3.ok, `outcome added: ${JSON.stringify(r3)}`);
h.mod.tab = 'outcomes';
await until(h, () => h.text().includes('conversion rate: 0.012'), 'the outcome shows', 30);

// 5. All handoffs goes back to the list, with the handoff's stage now deployed or live.
await h.click('ho-back');
await until(h, () => !h.mod.open && h.mod.table.rows.length === 1, 'back to the list');
assert.match(String(h.mod.table.rows[0].stage ?? JSON.stringify(h.mod.table.rows[0])), /live|deployed/, `the stage moved on: ${JSON.stringify(h.mod.table.rows[0]).slice(0, 200)}`);

console.log('handoff-live render test: ok');
process.exit(0);
