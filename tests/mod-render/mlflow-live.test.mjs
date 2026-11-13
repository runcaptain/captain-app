// The MLflow bridge, end to end against a real backend (backend/core) and a fake MLflow tracking server (the same
// answers as backend/core/tests/test_mlflow_bridge.py). "Connect MLflow" against a server that refuses the login shows
// the services' sentence; against one that answers, it connects. The kit's MLflow list shows experiments, a run's
// metrics and params, a model's versions, and links a version to a pipeline. Then a dataset's Lineage tab draws the
// MLflow run and model upstream of that pipeline.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/mlflow-live.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { cstBin, tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { bin: cst, why } = cstBin();
if (!cst) { console.log(`mlflow-live render test: skipped (${why})`); process.exit(0); }

// The fake tracking server: search endpoints only. One port answers, the other refuses every login.
const ANSWERS = {
  'experiments/search': { experiments: [{ experiment_id: '1', name: '/rankers', lifecycle_stage: 'active', last_update_time: 1762400000000 }] },
  'runs/search': { runs: [{ info: { run_id: 'abc123def456', run_name: 'lgbm', status: 'FINISHED', start_time: 1762400000000 },
    data: { metrics: [{ key: 'ndcg_10', value: 0.71 }], params: [{ key: 'lr', value: '0.05' }], tags: [{ key: 'mlflow.user', value: 'x' }, { key: 'team', value: 's' }] } }] },
  'registered-models/search': { registered_models: [{ name: 'ranker' }] },
  'model-versions/search': { model_versions: [{ version: '3', run_id: 'abc123def456', status: 'READY' }] },
};
const seen = [];
const serve = (refuse) => new Promise((res) => {
  const s = http.createServer((req, rsp) => {
    seen.push(`${req.method} ${req.url}`);
    const key = (req.url.split('/api/2.0/mlflow/')[1] ?? '').split('?')[0];
    if (refuse) { rsp.writeHead(403, { 'Content-Type': 'application/json' }); rsp.end(JSON.stringify({ error_code: 'PERMISSION_DENIED', message: 'Invalid scope' })); return; }
    rsp.writeHead(ANSWERS[key] ? 200 : 404, { 'Content-Type': 'application/json' });
    rsp.end(JSON.stringify(ANSWERS[key] ?? {}));
  });
  s.listen(0, '127.0.0.1', () => res(s));
});
const good = await serve(false), bad = await serve(true);
const goodUri = `http://127.0.0.1:${good.address().port}`, badUri = `http://127.0.0.1:${bad.address().port}`;

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-mlflow-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
spawnSync(cst, ['init', 'ml', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const back = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { back.kill('SIGTERM'); } catch {} good.close(); bad.close(); };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; back.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); back.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: `http://127.0.0.1:${j.port}`, token: j.token } };

// 1. Connect MLflow: a refused login is said in the services' words; a server that answers connects.
const forms = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-forms-ml-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-forms'), forms, { recursive: true });
const { TASKS } = await import(path.join(forms, 'tasks.js'));
const task = TASKS.find((t) => t.id === 'mlflow');
let r = await task.submit({ from: 'uri', uri: badUri });
assert.equal(r.ok, false);
assert.match(r.error, /^Saved, but Captain couldn't read MLflow\. MLflow refused the login/, r.error);
r = await task.submit({ from: 'uri', uri: goodUri });
assert.deepEqual(r, { ok: true, message: 'Connected MLflow: 1 experiment.' });
assert.ok(globalThis.__shared['captain.mlflow.changed'], 'an open MLflow list is told to read again');

// 2. The MLflow list: experiments and their runs, models and their versions, and "Served by pipeline...".
const kit = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-kit-ml-')));
fs.cpSync(path.join(root, 'defaults/captain/kit'), kit, { recursive: true });
const { MlflowList } = await import(path.join(kit, 'mlflow.js'));
const { palette } = await import(path.join(kit, 'tokens.js'));
class ListMod { init(_p, cx) { this.ml = new MlflowList({ id: 'ml', onSetup: () => {} }); this.ml.load(cx); } render() { return this.ml.render(palette(true), 360); } }
const h = harness(ListMod);
await h.start();
const until = async (hh, ok, what) => {
  for (let i = 0; i < 80 && !ok(hh.text()); i++) { await hh.layoutLists({}); await hh.idle(150); }
  assert.ok(ok(hh.text()), `${what}: ${hh.text().slice(0, 600)}`);
};
await until(h, (t) => t.includes('/rankers') && t.includes('ranker'), 'experiments and models');
await h.click('ml-exp-1');
await until(h, (t) => t.includes('lgbm') && t.includes('ndcg_10 0.71') && t.includes('lr=0.05') && t.includes('finished'), 'a run with its metrics and params');
// The models are read behind the answer: the list says so, then shows them the moment the read lands, by itself.
for (let i = 0; i < 80 && h.byIdPrefix('ml-model-ranker').length === 0; i++) { await h.layoutLists({}); await h.idle(150); }
assert.ok(h.byIdPrefix('ml-model-ranker').length > 0, `the registered models arrive without a click: ${h.text().slice(0, 500)}`);
await h.click('ml-model-ranker');
await until(h, (t) => t.includes('v3') && t.includes('from run abc123de') && t.includes('Served by model version...'), 'a model version');
await h.click('ml-link-ranker@3');
await until(h, () => Array.isArray(h.mod.ml.pipelines) && h.mod.ml.pipelines.includes('demo-tuned'), 'the pipelines to pick from');
await h.click(`ml-pick-ranker@3-${h.mod.ml.pipelines.indexOf('demo-tuned')}`);
await until(h, (t) => t.includes('Served by demo-tuned') && t.includes('ranker version 3 is served by demo-tuned'), 'linked');
assert.ok(seen.every((s) => /\/search/.test(s)), `MLflow is only searched, never written: ${seen.join(', ')}`);

// 3. Lineage: the MLflow run and model version upstream of the pipeline, on a dataset that feeds it.
globalThis.__props = { dataset: 'demo-catalog', pageTab: 'lineage' };
const data = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-ml-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), data, { recursive: true });
const { default: Data } = await import(path.join(data, 'main.js'));
const d = harness(Data);
await d.start();
await until(d, () => (d.mod.list ?? []).some((x) => x.name === 'demo-catalog'), 'datasets');
d.mod.select(d.cx, 'demo-catalog');
await until(d, () => (d.mod.page.lineage.graph?.nodes ?? []).some((n) => n.kind === 'mlflow-model'), 'MLflow in the lineage graph');
const g = d.mod.page.lineage.graph;
const edges = new Set(g.edges.map((e) => `${e.src} > ${e.dst}`));
assert.ok(edges.has('mlflow-run:abc123def456 > mlflow-model:ranker@3'), 'the run made the model');
assert.ok(edges.has('mlflow-model:ranker@3 > pipeline:demo-tuned@v1'), 'the model is upstream of the pipeline it serves');
assert.ok(g.nodes.some((n) => n.kind === 'mlflow-run' && n.label === 'run abc123de'));

stop();
console.log('mlflow-live render test: ok');
process.exit(0);
