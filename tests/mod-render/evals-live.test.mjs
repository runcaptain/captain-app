// Living evaluation sets against a real backend (this repo's backend/core) on the demo store. The catalog changes (a
// graded product retired, another renamed), so Health (kit/evals.js, the dataset page's tab) finds stale grades; the
// refresh shows its changelog, publishes new versions, and offers grading them. Rubrics: Start grading offers the
// project's rubric, and the grader shows its words beside each pair.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/evals-live.test.mjs
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
if (!cst) { console.log(`evals-live render test: skipped (${why})`); process.exit(0); }

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-evals-'));
// A project with one rubric (captain/rubrics/<domain>.md).
const project = path.join(work, 'project', 'captain');
fs.mkdirSync(path.join(project, 'rubrics'), { recursive: true });
fs.writeFileSync(path.join(project, 'captain.toml'), '');
fs.writeFileSync(path.join(project, 'rubrics', 'apparel.md'), '# Apparel\n\nA result is relevant when it is the garment asked for, in the size and color asked for.\n');
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), CST_PROJECT: project, PYTHONPATH: path.join(root, 'backend/core') };
spawnSync(cst, ['init', 'ev', '--storage', path.join(work, 'store')], { env });
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
const waitJob = async (job) => { for (let i = 0; i < 200; i++) { const r = await call('GET', `/api/jobs/${job}`); if (r.status === 'done' || r.status === 'failed') return r; await new Promise((x) => setTimeout(x, 300)); } throw new Error('job timed out'); };

// The catalog changes: a graded product is retired, another graded one is renamed.
const cat = await call('GET', '/api/datasets/demo-catalog/rows?limit=2000');
const grades = await call('GET', '/api/datasets/demo-judgments/rows?limit=5000');
const gcol = (name) => grades.columns.findIndex((c) => c.name === name);
const graded = [...new Set(grades.rows.map((r) => r[gcol('product_id')]))];
const idCol = cat.columns.findIndex((c) => c.name === 'id'), titleCol = cat.columns.findIndex((c) => c.name === 'title');
const [gone, renamed] = graded.filter((p) => cat.rows.some((r) => r[idCol] === p)).slice(0, 2);
const csv = (v) => { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
const lines = [cat.columns.map((c) => c.name).join(',')];
for (const r of cat.rows) {
  if (r[idCol] === gone) continue;
  const row = [...r];
  if (row[idCol] === renamed) row[titleCol] = `${row[titleCol]} (new model)`;
  lines.push(row.map(csv).join(','));
}
const file = path.join(work, 'demo-catalog.csv');
fs.writeFileSync(file, lines.join('\n') + '\n');
const imp = await waitJob((await call('POST', '/api/datasets/import', { paths: [file], folder: '' })).job);
assert.equal(imp.status, 'done', `catalog imported: ${JSON.stringify(imp).slice(0, 300)}`);

// Health, as the dataset page's tab shows it.
const kitDir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-evals-'));
fs.cpSync(path.join(root, 'defaults/captain/kit'), kitDir, { recursive: true });
const { EvalHealth } = await import(path.join(kitDir, 'evals.js'));
const { palette } = await import(path.join(kitDir, 'tokens.js'));
class HealthView { init(_p, cx) { this.h = new EvalHealth(); this.h.open(cx, { name: 'demo-judgments', kind: 'judgments' }); } render() { return this.h.render(palette(false)); } }
const until = async (h, ok, what) => { for (let i = 0; i < 120 && !ok(); i++) { await h.layoutLists({}); await h.idle(150); } assert.ok(ok(), `${what}: ${h.text().slice(0, 700)}`); };
const h = harness(HealthView);
await h.start();
await until(h, () => Boolean(h.mod.h.health), 'health checked');
assert.equal(h.mod.h.pick.queries, 'demo-queries', 'its partner set is guessed');
assert.equal(h.mod.h.pick.catalog, 'demo-catalog');
const st = h.mod.h.health.staleness;
assert.ok(st.retired.some((x) => x.product_id === gone), `the retired product's grades are found: ${JSON.stringify(st.retired).slice(0, 200)}`);
assert.ok(st.changed.some((x) => x.product_id === renamed), `the renamed product's grades are found: ${JSON.stringify(st.changed).slice(0, 200)}`);
let t = h.text();
assert.ok(t.includes('grades on products that are gone') && t.includes('grades on products changed since grading'), 'staleness, in words');
await h.click('eh-x-retired');
assert.ok(h.text().includes(gone), 'the retired list opens');
assert.ok(t.includes('Head') && t.includes('Tail') && t.includes('SEARCHED OFTEN, NOT IN THE SET'), 'traffic coverage and the top unseen');

// Refresh: the changelog first, then new versions.
const before = Object.fromEntries((await call('GET', '/api/datasets')).map((d) => [d.name, d.latest]));
await h.click('eh-plan');
await until(h, () => Boolean(h.mod.h.plan), 'the refresh plan');
assert.ok(h.text().includes('added') && h.text().includes('removed'), `the changelog: ${h.text().slice(0, 400)}`);
await h.click('eh-publish');
await until(h, () => Boolean(h.mod.h.published) && !h.mod.h.busy, 'published');
const after = Object.fromEntries((await call('GET', '/api/datasets')).map((d) => [d.name, d.latest]));
assert.equal(after['demo-queries'], before['demo-queries'] + 1, 'a new queries version');
assert.equal(after['demo-judgments'], before['demo-judgments'] + 1, 'a new judgments version');
globalThis.__overlays = [];
await h.click('eh-grade');
assert.equal(globalThis.__overlays.at(-1)?.props?.task, 'grading', 'Grade the new and changed pairs opens Start grading');
assert.equal(globalThis.__overlays.at(-1)?.props?.values?.queries, 'demo-queries');

// Rubrics: Start grading offers the project's, and the grader shows its words.
const formsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), formsDir, { recursive: true });
globalThis.__props = { task: 'grading' };
const { default: Forms } = await import(path.join(formsDir, 'main.js'));
const f = harness(Forms);
await f.start();
await until(f, () => (f.mod.options?.rubric ?? []).length > 0, 'the rubric picker');
assert.deepEqual(f.mod.options.rubric.map((o) => o.value), ['apparel']);
const q = await call('POST', '/api/grading', { name: 'apparel-grading', queries: 'demo-queries', pipelines: 'demo-bm25', depth: 3, sample: 2, rubric: 'apparel' });
assert.ok(!q.error, `queue made: ${JSON.stringify(q).slice(0, 200)}`);
const runsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-runs-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-runs'), runsDir, { recursive: true });
globalThis.__props = {};
const { default: Runs } = await import(path.join(runsDir, 'main.js'));
const r = harness(Runs);
await r.start();
const status = await call('GET', '/api/grading/apparel-grading');
assert.match(String(status.rubric?.text ?? ''), /garment asked for/, 'the queue carries the rubric text');
await r.click('tab-grading');
r.mod.tab = 'grading';
r.mod.openGrader(r.cx, 'apparel-grading');
await until(r, () => r.text().includes('the garment asked for'), 'the grader shows the rubric');
assert.ok(r.text().includes('RUBRIC: APPAREL'));

console.log('evals-live render test: ok');
process.exit(0);
