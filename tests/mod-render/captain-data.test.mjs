// Render test for the captain-data mod, in Node, against fixtures captured from a real `cst serve` (cst demo seed).
// `captain-app check` renders a mod once, before any data arrives; this drives the populated states: the dataset
// list, the first page of the grid, scrolling into page 2, a sort, a broken filter, and the labels tab. It also
// times every render against gpui-kit's 50 ms limit.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/captain-data.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data');
globalThis.__routes = [
  [/^\/api\/dataset-classes/, 'classes'],
  [/\/rows\?.*where=/, 'bad-where'],
  [/\/rows\?.*offset=200(&|$)/, 'rows1'],
  [/\/rows\?/, 'rows0'],
  [/^\/api\/datasets(\?|$)/, 'datasets'],
  [/^\/api\/datasets\/[^?]+$/, 'detail'],
  [/^\/api\/labels\/[^/]+\/preview/, 'preview'],
  [/^\/api\/labels$/, 'labels'],
];

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-'));
fs.copyFileSync(path.join(root, 'defaults/mods/captain-data/main.js'), path.join(work, 'main.js'));
fs.copyFileSync(path.join(root, 'defaults/captain/cst.js'), path.join(work, 'real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));

const h = harness(Mod);
await h.start();
const rows0 = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'rows0.json'), 'utf8'));
const rows1 = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'rows1.json'), 'utf8'));
const datasets = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'datasets.json'), 'utf8'));

// The list, the classes, the first dataset's detail.
let text = h.text();
for (const d of datasets) assert.ok(text.includes(`${d.name}@v${d.latest}`), `list shows ${d.name}`);
assert.ok(text.includes('catalog'), 'class chips');
assert.ok(text.includes('demo-catalog@v1'), 'detail header');
assert.ok(text.includes('price'), 'schema');
// An icon on every dataset row.
for (const row of h.byIdPrefix('ds-')) assert.ok(h.has(row, 'svg'), `icon on ${row.call('id')[0]}`);

// The grid asks for the visible rows, then shows them.
await h.layoutLists({ 'data-grid': [0, 30] });
text = h.text({ 'data-grid': [0, 30] });
assert.ok(text.includes(String(rows0.rows[0][1])), 'first row shows');
assert.ok(/rows 1 to 30 of 480/.test(text), `status line: ${text.match(/rows [^|]*?of [\d,]+/)?.[0]}`);
assert.ok(rows0.columns.every((c) => text.includes(c.name)), 'every column header');

// Scrolling into page 2 fetches it, and only it.
const before = h.asked().length;
await h.layoutLists({ 'data-grid': [195, 225] });
text = h.text({ 'data-grid': [195, 225] });
assert.ok(h.asked().slice(before).some((p) => /offset=200/.test(p)), 'page 2 asked');
assert.ok(text.includes(String(rows1.rows[5][1])), 'a page-2 row shows');

// A header click sorts on the daemon.
await h.click('col-price');
await h.layoutLists({ 'data-grid': [0, 30] });
assert.ok(h.asked().some((p) => /order=price/.test(p) && !/desc=/.test(p)), 'sorted ascending');
await h.click('col-price');
await h.layoutLists({ 'data-grid': [0, 30] });
assert.ok(h.asked().some((p) => /order=price/.test(p) && /desc=true/.test(p)), 'sorted descending');

// A broken filter shows the daemon's sentence, without terminal colour codes.
h.mod.where.set_value('price <<< 3');
h.mod.where.handlers.submit({}, h.cx);
await h.layoutLists({ 'data-grid': [0, 30] });
text = h.text();
assert.ok(text.includes('the filter is not valid SQL'), 'filter error shows');
assert.ok(!text.includes('\u001b['), 'no ANSI codes');

// Labels: the list, the SQL, and a preview over an events dataset.
await h.click('tab-labels');
text = h.text();
assert.ok(text.includes('deep-search@v1'), 'label listed');
assert.ok(text.includes('max(case when clicked'), 'label SQL');
const chip = h.byIdPrefix('chip-logs/demo-search-sessions')[0];
assert.ok(chip, 'events chip for the preview');
await h.click(chip.call('id')[0]);
text = h.text();
assert.ok(/mean 0\.\d+/.test(text), 'preview mean');
assert.ok(text.includes('session_id'), 'preview columns');

// Every render stayed far under gpui-kit's 50 ms limit.
const sorted = [...h.timings].sort((a, b) => a - b);
console.log(`renders: ${h.timings.length}, median ${sorted[Math.floor(sorted.length / 2)].toFixed(2)} ms, slowest ${sorted.at(-1).toFixed(1)} ms (V8; QuickJS in the app is slower, see RISKS.md)`);
assert.ok(Math.max(...h.timings) < 50, 'a render took 50 ms or more');
console.log('captain-data render test: ok');
