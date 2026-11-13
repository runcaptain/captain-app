// Compare your own rankings (captain-rankings), against a real backend (this repo's backend/core) with generated
// files: one with errors (Import stays off), one without labels (relevance metrics say they're unavailable, and no
// labels question), and one with the team's own column names and labels, mapped on screen, which imports and opens
// in Comparisons as a normal run.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/rankings-live.test.mjs
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
if (!cst) { console.log(`rankings-live render test: skipped (${why})`); process.exit(0); }

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cst-rankings-'));
// This tree's backend source, even when the venv was installed from another checkout.
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), PYTHONPATH: path.join(root, 'backend/core') };
spawnSync(cst, ['init', 'rk', '--storage', path.join(work, 'store')], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} try { fs.rmSync(work, { recursive: true, force: true }); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
const get = async (p) => { const r = await fetch(base + p, { headers: { Authorization: `Bearer ${j.token}` } }); return { status: r.status, body: await r.json().catch(() => null) }; };

// The files: 4 queries, 2 systems, 5 results each.
const write = (name, header, rowFn) => {
  const rows = [header.join(',')];
  for (let q = 1; q <= 4; q++) for (const arm of ['prod', 'candidate']) for (let r = 1; r <= 5; r++) rows.push(rowFn(q, arm, r).join(','));
  const p = path.join(work, name);
  fs.writeFileSync(p, rows.join('\n') + '\n');
  return p;
};
const broken = write('broken.csv', ['query_id', 'arm', 'item_id', 'rank'], (q, arm, r) => [`q${q}`, arm, `p${r}`, r === 2 ? 1 : r]);
const unlabeled = write('unlabeled.csv', ['query_id', 'arm', 'item_id', 'rank'], (q, arm, r) => [`q${q}`, arm, `p${arm === 'prod' ? r : 6 - r}`, r]);
const theirs = write('team-results.csv', ['qid', 'system', 'doc', 'position', 'grade'], (q, arm, r) => {
  const item = arm === 'prod' ? r : 6 - r;
  return [`q${q}`, arm, `p${item}`, r, item <= 2 ? 3 : item <= 4 ? 1 : 0];
});

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-rankings-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-rankings'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
// Timers (the import's polling) run in layoutLists, as layout would run them.
const until = async (h, ok, what) => { for (let i = 0; i < 120 && !ok(); i++) { await h.layoutLists({}); await h.idle(150); } assert.ok(ok(), `${what}: ${h.text().slice(0, 600)}`); };
const importOff = (h) => !h.byIdPrefix('rk-import').find((e) => e.call('id')?.[0] === 'rk-import')?.call('on_click');

// 1. Errors: shown in words, and Import stays off.
let h = harness(Mod);
await h.start();
h.mod.path.set_value(broken);
h.mod.validate(h.cx);
await until(h, () => h.mod.check && !h.mod.checking, 'the broken file was checked');
assert.equal(h.mod.check.ok, false);
assert.ok(h.text().includes('the same rank'), `the error, in words: ${h.text().slice(0, 500)}`);
assert.ok(importOff(h), 'Import is off');

// 2. No labels: relevance metrics are unavailable (never invented), and there's no labels question.
h = harness(Mod);
await h.start();
h.mod.path.set_value(unlabeled);
h.mod.validate(h.cx);
await until(h, () => h.mod.check && !h.mod.checking, 'the unlabeled file was checked');
let t = h.text();
assert.equal(h.mod.check.ok, true, JSON.stringify(h.mod.check.errors));
assert.ok(t.includes('Relevance metrics: unavailable, no labels'), `relevance unavailable: ${t.slice(0, 500)}`);
assert.ok(!t.includes('Where do the labels come from?'), 'no labels question without labels');
assert.ok(t.includes('40 rows') && t.includes('4 queries') && t.includes('2 systems'), 'the stats');
assert.equal(h.mod.name.value(), 'unlabeled', 'the name comes from the file');

// 3. The team's own column names: mapped on screen, then labelled, imported, and opened in Comparisons.
h = harness(Mod);
await h.start();
h.mod.path.set_value(theirs);
h.mod.validate(h.cx);
await until(h, () => h.mod.check && !h.mod.checking, 'the team file was checked');
assert.equal(h.mod.check.ok, false, 'its names differ');
assert.ok(h.text().includes('Which of your columns is which?'), 'the mapping rows show');
for (const [k, v] of [['query_id', 'qid'], ['arm', 'system'], ['item_id', 'doc'], ['rank', 'position'], ['label', 'grade']]) await h.click(`rk-map-${k}-${v}`);
h.mod.validate(h.cx);
await until(h, () => h.mod.check?.ok && !h.mod.checking, 'mapped, it passes');
assert.ok(h.text().includes('Where do the labels come from?'), 'labels need their source');
assert.ok(importOff(h), 'Import waits for the labels source');
await h.click('rk-lf-human');
assert.equal(h.mod.baseline, 'candidate', 'a baseline is picked (the first system, sorted)');
await h.click('rk-base-prod');
assert.ok(!importOff(h), 'Import is on');
await h.click('rk-import');
await until(h, () => !h.mod.busy, 'the import finished');
assert.ok(!h.mod.error, `no error: ${h.mod.error}`);
await until(h, () => globalThis.__shared['captain.open']?.section === 'runs', 'it opened in Runs, where its comparison shows');
const opened = globalThis.__shared['captain.open'];
assert.equal(opened.backtest, 'team-results', `opens the new run: ${JSON.stringify(opened)}`);
const runRec = await get(`/api/runs/${opened.backtest}?v=${opened.version}`);
assert.equal(runRec.status, 200, 'the run exists');
assert.ok((runRec.body?.arms ?? []).length === 2, 'with both systems');

console.log('rankings-live render test: ok');
process.exit(0);
