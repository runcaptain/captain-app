// Explain (captain-explain) against a real backend (this repo's backend/core) on the demo store: from the palette
// (pick demo-bm25 vs demo-tuned, type "earbuds under 50") every stage renders, unknown terms are flagged and retrieval
// shows its field:term parts, and with no grades the backend's note shows; from a comparison run, the first stage
// where a relevant result was lost is highlighted with its sentence, and Copy prompt carries it. A stage the engine
// can't explain shows its note, not empty bars.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/explain-live.test.mjs
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
if (!cst) { console.log(`explain-live render test: skipped (${why})`); process.exit(0); }

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-explain-'));
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), PYTHONPATH: path.join(root, 'backend/core') };
spawnSync(cst, ['init', 'ex', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} try { fs.rmSync(work, { recursive: true, force: true }); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
const get = async (p) => (await fetch(base + p, { headers: { Authorization: `Bearer ${j.token}` } })).json();

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-explain-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-explain'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const until = async (h, ok, what) => { for (let i = 0; i < 100 && !ok(); i++) { await h.layoutLists({}); await h.idle(150); } assert.ok(ok(), `${what}: ${h.text().slice(0, 700)}`); };

// 1. From the palette: pick the pipelines, type the query.
globalThis.__props = {};
let h = harness(Mod);
await h.start();
await until(h, () => (h.mod.pipelines ?? []).length > 0, 'the pipelines loaded');
await h.click('ex-pipe-demo-bm25');
await h.click('ex-vs-demo-tuned');
h.mod.query.set_value('earbuds under 50');
await h.click('ex-go');
await until(h, () => Boolean(h.mod.trace), 'explained');
let t = h.text();
for (const s of ['query understanding', 'lexical retrieval (BM25)', 'boosts', 'price filter', 'final ranking']) assert.ok(t.includes(s), `the ${s} stage: ${t.slice(0, 500)}`);
assert.ok(t.includes('no product contains: under, 50'), 'unknown terms are said in words');
assert.deepEqual(h.mod.trace.a.stages[0].unknown_terms, ['under', '50'], 'and flagged');
assert.ok(/title:earbuds \d/.test(t), `retrieval shows its field:term parts: ${t.slice(0, 900)}`);
assert.ok(t.includes("No grades for this query"), 'without grades, the backend says why it can\'t point at a stage');
assert.ok(t.includes('A: demo-bm25') && t.includes('B: demo-tuned'), 'both systems, side by side');

// 2. From a comparison run: a query where a relevant result was lost.
const rows = (await get('/api/runs/demo-historic-replay%40v1/queries?sort=delta&limit=60')).rows ?? [];
let hit = null;
for (const r of rows) {
  const d = await get(`/api/runs/demo-historic-replay%40v1/queries/${r.query_id}/explain`);
  if (d.first_divergence) { hit = { row: r, d }; break; }
}
assert.ok(hit, 'the demo run has a query where a relevant result was lost');
globalThis.__props = { run: 'demo-historic-replay@v1', query_id: hit.row.query_id };
h = harness(Mod);
await h.start();
await until(h, () => Boolean(h.mod.trace), 'the run query explained');
t = h.text();
const fd = hit.d.first_divergence;
const lower = (fd.ranked_lower ?? [])[0];
assert.ok(t.includes(`At ${fd.stage}, the relevant ${lower?.id ?? ''}`), `the divergence sentence: ${t.slice(0, 600)}`);
if (lower) assert.ok(t.includes(`dropped from ${lower.rank_a} to ${lower.rank_b}`), 'with its ranks');
assert.ok(h.byIdPrefix('ex-pipe').length === 0, 'opened from a run, it asks for nothing');
let copied = null;
h.cx.write_to_clipboard = (s) => { copied = s; };
await h.click('ex-copy');
assert.ok(copied && copied.includes('First divergence: At'), `Copy prompt carries the trace: ${String(copied).slice(0, 300)}`);

// 3. A stage the engine can't explain: its note, not empty bars.
const { palette } = await import(path.join(dir, 'kit/tokens.js'));
const el = h.mod.stageBody(palette(false), { stage: 'semantic retrieval', kind: 'retrieve', explained: false, notes: ['no explanation provided'], items: [{ id: 'x', rank: 1, score: 1, score_parts: {} }] });
const texts = []; const walk = (e) => { if (!e || typeof e !== 'object') return; for (const k of e.kids ?? []) { if (typeof k === 'string') texts.push(k); else walk(k); } }; walk(el);
assert.deepEqual(texts, ['no explanation provided'], `only the note: ${JSON.stringify(texts)}`);

console.log('explain-live render test: ok');
process.exit(0);
