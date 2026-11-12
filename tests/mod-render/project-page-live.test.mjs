// The project page against a real backend on the demo store: Inputs show each pinned version and offer one action
// (a newer version is information with "Use version 2", a missing input asks to choose one), Experiments show the
// champion and a model trained on the holdout's own labeler as "Agrees with its own labeler", never as a win, and
// Ship says the result is not yet confirmed online. A model trained on other labels gets a real verdict.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/project-page-live.test.mjs
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
if (!cst) { console.log(`project-page render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-project-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'proj', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });

const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const post = async (p, body) => { const r = await fetch(base + p, { method: 'POST', headers: H, body: JSON.stringify(body) }); assert.ok(r.ok, `${p}: ${r.status} ${await r.clone().text()}`); return r.json(); };

// A project: the sealed holdout and its judgments pinned, queries pinned at v1 (v2 is the sealed one, so it's newer),
// the baseline as champion and the tuned ranker trained on the holdout's own judgments.
await post('/api/projects', { name: 'Outdoor' });
await post('/api/projects/outdoor/pin', { catalog: 'demo-catalog', queries: 'demo-queries@v1', holdout: 'demo-queries', labels: 'demo-judgments' });
await post('/api/projects/outdoor/experiments', { pipeline: 'demo-bm25' });
const tuned = await post('/api/projects/outdoor/experiments', { pipeline: 'demo-tuned', labels: 'demo-judgments' });
await post('/api/projects/outdoor/champion', { pipeline: 'demo-bm25' });
const bt = spawnSync(cst, ['--json', 'projects', 'experiment-backtest', 'outdoor', tuned.id], { env, encoding: 'utf8' });
assert.equal(bt.status, 0, bt.stdout + bt.stderr);

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token }, 'captain.scope': { kind: 'project', id: 'outdoor', label: 'Outdoor' } };
const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-project-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-project'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const until = async (ok, what, n = 80) => {
  for (let i = 0; i < n && !ok(h.text()); i++) { await h.layoutLists({}); await h.idle(150); }
  assert.ok(ok(h.text()), `${what}: ${h.text().slice(0, 1500)}`);
};

await until((t) => t.includes('Models') && t.includes('demo-tuned'), 'page loads');
let t = h.text();
// Inputs: each pinned version, one action each.
for (const s of ['Catalog', 'Events', 'Queries', 'Labels', 'Sealed test set']) assert.ok(t.includes(s), `input card ${s}`);
assert.ok(t.includes('demo-catalog') && t.includes('demo-judgments'), 'pinned names');
assert.ok(t.includes('Newer version available: demo-queries@v2.'), 'newer version as information');
assert.equal(h.byIdPrefix('in-queries-newer').length, 1, 'one action: use the newer version');
assert.equal(h.byIdPrefix('in-events-pick').length, 1, 'a missing input asks to choose one');
// Three sections and nothing numbered: Data, Models, Ship. The header is the name, one line, and More.
for (const id of ['part-inputs', 'part-experiments', 'part-ship']) assert.equal(h.byIdPrefix(id).length >= 1, true, id);
assert.ok(t.includes('Data') && t.includes('Models') && t.includes('Ship') && !t.includes("What you're building") && !t.includes('Train and compare'), `the three sections: ${t.slice(0, 300)}`);
assert.equal(h.byIdPrefix('ph-building').length, 1, 'what it builds is one muted line under the name');
// Data: one row each, one status in words, and no tags.
assert.ok(/480 rows, (taken|last read) \d{4}-\d\d-\d\d/.test(t), 'rows and when, as words');
assert.ok(!/\b(required|optional|snapshot)\b/.test(t.replace('Not chosen. Optional.', '')) && !/version 1\b/.test(t.split('Models')[0]), `no tag rows on the inputs: ${t.split('Models')[0]}`);
assert.equal(h.byIdPrefix('in-add').length, 1, 'one way to add data');
// Models: a verdict sentence per model. The one trained on its own labeler never reads as a win, and the sentence
// says so and says where the evidence comes from. The numbers are under Details.
assert.ok(t.includes('The model the others are compared with.'), 'the compared model, in words');
assert.ok(t.includes("Agrees with its own labeler, so its score doesn't show it ranks better than demo-bm25.") && t.includes('Simulated events.'), `the verdict sentence carries the labeler and the evidence: ${t}`);
assert.ok(!t.includes('Ranks better than') && !t.includes('nDCG@10'), 'no win claimed, and no numbers before Details');
// One filled button on the page: the Next banner's.
// A filled button is the one drawn on the solid colour (kit/ui.js Button, kind "primary").
const solid = (await import(path.join(dir, 'kit/tokens.js'))).palette(false).solid;
const filled = () => h.byIdPrefix('').filter((e) => e.call('bg')?.[0] === solid && e.call('on_click')).map((e) => e.call('id')[0]);
assert.deepEqual(filled(), ['ph-next-go'], 'one filled button: the next step');
// One typeface: the code font is for something a person copies or types. Nothing on this page is.
const MONO_FONT = (await import(path.join(dir, 'kit/tokens.js'))).MONO;
const mono = h.byIdPrefix('').filter((e) => e.call('font_family')?.[0] === MONO_FONT);
assert.equal(mono.length, 0, `no code font on the project page: ${mono.map((e) => (e.kids ?? []).filter((k) => typeof k === 'string').join(' ')).slice(0, 5)}`);
// Ship: only where to deploy. No registry tile, no launch review, none of the old words.
assert.ok(t.includes('Where should demo-tuned@v1 run?') && !/Model registry|Launch review|Not staged|Staging runs/.test(t), `Ship: ${t.slice(-600)}`);

// Open the row: why, and its actions, with the evidence on Make champion.
const row = h.mod.exps.experiments.findIndex((e) => e.id === tuned.id);
await h.click(`exp-${row}`);
t = h.text();
assert.ok(t.includes('same judgment set'), 'independence explained');
assert.ok(t.includes('nDCG@10') && t.includes('Recall@50') && t.includes('Judged@10') && t.includes('simulated') && t.includes('Same labeler'), 'Details opens the numbers, the evidence and the label independence, as before');
assert.ok(t.includes('Make it the baseline (simulated)'), 'evidence on the button');
for (const id of ['open', 'rerun', 'judge', 'champion', 'publish']) assert.equal(h.byIdPrefix(`exp-${row}-${id}`).length, 1, `row action ${id}`);
await h.click(`exp-${row}-rerun`);
const o = globalThis.__overlays.at(-1);
assert.equal(o.props.task, 'experiment');
assert.equal(o.props.values.base, 'demo-tuned');
assert.equal(o.props.values.pins.labels, 'demo-judgments@v1', 'the form gets the pins');

// Use version 2: pinned now, the information gone.
await h.click('in-queries-newer');
await until((t) => t.includes('Queries now uses demo-queries@v2.'), 'pinned the newer version');
await until((t) => !t.includes('Newer version available: demo-queries@v2.'), 'newer-version info cleared');

// Human grades on the holdout: the same model now gets a real verdict.
const graded = path.join(work, 'human.csv');
const py = path.join(path.dirname(cst), 'python');
const dump = spawnSync(py, ['-c', `
import csv, sys
from captain_search import ops
from captain_search.workspace import open_context
_, rows, _ = ops.rows_of(open_context(), "demo-judgments")
w = csv.writer(open(sys.argv[1], "w")); w.writerow(["query_id", "product_id", "grade"])
for r in rows: w.writerow([r["query_id"], r["product_id"], r["grade"]])
`, graded], { env, encoding: 'utf8' });
assert.equal(dump.status, 0, dump.stderr);
assert.equal(spawnSync(cst, ['judge', 'import', graded, '--name', 'human-grades'], { env }).status, 0, 'human grades imported');
await post('/api/projects/outdoor/pin', { labels: 'human-grades' });
assert.equal(spawnSync(cst, ['projects', 'experiment-backtest', 'outdoor', tuned.id], { env }).status, 0, 'judged on human grades');
h.mod.load(h.cx);
await until((x) => /Ranks better than demo-bm25\.|Ranks worse than demo-bm25\.|No clear difference from demo-bm25\./.test(x), 'a real verdict with independent labels');
assert.ok(h.text().includes('Human graded.'), 'and the sentence now says the evidence is human graded');
assert.ok(h.text().includes('Independent labels'));
assert.ok(!h.text().includes('Agrees with its own labeler'));

// Words a person shouldn't see.
t = h.text();
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bcst /, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad} on the page`);
console.log('project-page render test: ok');
process.exit(0);
