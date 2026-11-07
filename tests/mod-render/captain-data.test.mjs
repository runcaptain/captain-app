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
  [/^\/api\/datasets\/tree$/, 'tree'],
  [/^\/api\/datasets(\?|$)/, 'datasets'],
  [/^\/api\/datasets\/[^?]+$/, 'detail'],
  [/^\/api\/labels\/[^/]+\/preview/, 'preview'],
  [/^\/api\/labels$/, 'labels'],
];

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-'));
// The mod as shipped, with its kit; the kit's daemon client is swapped for the fixtures.
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));

const h = harness(Mod);
// First the engine is still starting: the screen says so instead of drawing nothing, and keeps asking.
globalThis.__starting = true;
await h.start();
// A quiet skeleton: the header, and no words about what is starting.
assert.ok(!/engine|daemon|starting|loading/i.test(h.text()), `starting state shows words: ${h.text().slice(0, 160)}`);
globalThis.__starting = false;
await h.layoutLists({});
const rows0 = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'rows0.json'), 'utf8'));
const rows1 = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'rows1.json'), 'utf8'));
const datasets = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'datasets.json'), 'utf8'));

// The list, the classes, the first dataset's detail.
let text = h.text();
if (process.env.SHOW) console.log(text.slice(0, 1500));
// Versioned names show as the name plus a version chip, never "name@v1".
// A directory: folders (expanded at first), each dataset by its own name inside its folder, with its version chip.
for (const d of datasets) assert.ok(text.includes(`${d.name.split('/').pop()} | v${d.latest}`), `list shows ${d.name}`);
for (const f of ['All datasets', 'demo-historic-replay', 'logs']) assert.ok(text.includes(f), `folder ${f}`);
assert.ok(text.includes('Import files') && text.includes('New folder'), 'directory actions');
assert.ok(!/@v\d/.test(text), 'no raw name@v ref anywhere');
assert.ok(text.includes('catalog'), 'class chips');
assert.ok(text.includes('demo-catalog | v1'), 'detail header');
assert.ok(text.includes('Import a dataset') && text.includes('Open an agent in this space') && text.includes('Copy prompt'), 'tasks and agent actions');
assert.ok(text.includes('price'), 'schema');
// An icon on every dataset row.
for (const row of h.byIdPrefix('ds-')) assert.ok(h.has(row, 'svg'), `icon on ${row.call('id')[0]}`);

// The page opens on Overview (facts and schema). Sample data is the kit grid every screen uses, paged by the services.
await h.click('dtab-sample');
await h.idle(50);
text = h.text({ 'grid-ds-rows': [0, 30] });
assert.ok(text.includes(String(rows0.rows[0][1])), 'first row shows');
assert.ok(/1 to 200 of 480/.test(text), `pager: ${text.match(/\d+ to [^|]*/)?.[0]}`);
assert.ok(rows0.columns.every((c) => text.includes(c.name)), 'every column header');

// Next asks for the next page, and only it.
const before = h.asked().length;
await h.click('gnext-ds-rows');
await h.idle(50);
assert.ok(h.asked().slice(before).some((p) => /offset=200/.test(p)), 'page 2 asked');
text = h.text({ 'grid-ds-rows': [0, 30] });
assert.ok(text.includes(String(rows1.rows[5][1])) && /201 to 400 of 480/.test(text), 'page 2 shows');

// A header click sorts the whole dataset on the services.
const price = h.mod.page.rows.table.columns.findIndex((c) => c.name === 'price');
await h.click(`gh-ds-rows-${price}`);
await h.idle(50);
assert.ok(h.asked().some((p) => /order=price/.test(p) && !/desc=/.test(p)), 'sorted ascending');
await h.click(`gh-ds-rows-${price}`);
await h.idle(50);
assert.ok(h.asked().some((p) => /order=price/.test(p) && /desc=true/.test(p)), 'sorted descending');

// A broken filter shows the services' sentence, without terminal colour codes.
h.mod.page.rows.whereBox.set_value('price <<< 3');
await h.click('gwhere-ds-rows');
await h.idle(50);
text = h.text();
assert.ok(text.includes('The filter is not valid SQL'), 'filter error shows');
assert.ok(!text.includes('\u001b['), 'no ANSI codes');

// Labels: the list, the SQL, and a preview over an events dataset.
await h.click('tab-labels');
text = h.text();
assert.ok(text.includes('deep-search | v1'), 'label listed');
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
