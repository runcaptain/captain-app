// Render test for the captain-data mod, in Node, against fixtures captured from a real `cst serve` (cst demo seed).
// `captain-app check` renders a mod once, before any data arrives; this drives the populated states: the dataset
// one list of tables (datasets and database tables together), a dataset's page with its two views, the first page
// of the grid, page 2, a sort, a broken filter, and Sources. It also times every render against gpui-kit's 50 ms
// limit.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/captain-data.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data');
globalThis.__routes = [
  [/^\/api\/dataset-classes/, 'classes'],
  [/\/rows\?.*where=/, 'bad-where'],
  [/\/rows\?.*offset=200(&|$)/, 'rows1'],
  [/\/rows\?/, 'rows0'],
  [/^\/api\/data\/tables/, 'data-tables'],
  [/^\/api\/datasets(\?|$)/, 'datasets'],
  [/^\/api\/datasets\/[^?]+$/, 'detail'],
];

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-')));
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

const { readWords, versionWords } = await import(path.join(work, 'catalog.js'));
const merged = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'data-tables.json'), 'utf8'));

// Two submenus, Tables first, and no Labels, Datasets, Notebooks or Models tab.
assert.deepEqual(h.byIdPrefix('submenu-').map((e) => e.call('id')[0]), ['submenu-tables', 'submenu-sources']);
assert.equal(h.byIdPrefix('tab-').length, 0, 'no strip of tabs besides the two submenus');
let text = h.text();
if (process.env.SHOW) console.log(text.slice(0, 1500));
assert.ok(!/\bLabels\b/.test(text), 'Labels left the screen');
assert.ok(!h.asked().some((p) => p.startsWith('/api/labels')), 'and nothing asks for labels');

// Tables: one row per dataset and per database table, in one list, with the five columns.
for (const col of ['Name', 'Where it lives', 'Rows', 'Versions', 'Last read']) assert.ok(text.includes(col), `column ${col}`);
assert.equal(h.byIdPrefix('cat-row-').length, merged.rows.length, 'every dataset and every database table has a row');
for (const d of datasets) assert.ok(text.includes(d.name), `the list shows the dataset ${d.name}`);
for (const t of merged.rows.filter((r) => r.kind === 'table')) assert.ok(text.includes(t.name) && text.includes(t.where), `the list shows the database table ${t.name}`);
// A dataset is a table Captain keeps versions of: the Versions column says so. A database table says it is read
// where it lives. There is no separate Datasets screen.
assert.ok(text.includes('Captain keeps 1 version') && text.includes('Captain keeps 2 versions') && text.includes('Read where it lives'), 'the Versions column tells the two apart');
assert.equal(versionWords({ kind: 'dataset', versions: 3 }), 'Captain keeps 3 versions');
assert.equal(versionWords({ kind: 'table', versions: null }), 'Read where it lives');
assert.ok(text.includes('Captain, folder logs') && text.includes('Captain, a saved query on demo-store'), 'where it lives, in words');
assert.ok(text.includes('60,000'), 'rows');
assert.ok(text.includes(`${merged.rows.length} tables`), 'the count');
// Last read, in words.
const now = Date.parse('2026-11-12T12:00:00Z');
assert.equal(readWords('2026-11-12', now), 'Today');
assert.equal(readWords('2026-11-11', now), 'Yesterday');
assert.equal(readWords('2026-11-05T08:00:00+00:00', now), '7 days ago');
assert.equal(readWords('2026-08-01', now), 'Aug 1, 2026');
assert.equal(readWords(null, now), 'Not read yet');
assert.ok(!/@v\d/.test(text), 'no raw name@v ref anywhere');
assert.ok(text.includes('Import a dataset'), 'the one filled button brings data in');
// An icon on every row.
for (const row of h.byIdPrefix('cat-row-')) assert.ok(h.has(row, 'svg') || h.has(row, 'image'), `icon on ${row.call('id')[0]}`);

// The kind filter keeps one of the two.
await h.click('cat-kind-table');
assert.equal(h.byIdPrefix('cat-row-').length, merged.rows.filter((r) => r.kind === 'table').length, 'only the database tables');
await h.click('cat-kind-dataset');
assert.equal(h.byIdPrefix('cat-row-').length, datasets.length, 'only the tables Captain keeps versions of');
await h.click('cat-kind-all');

// A search asks the services (so a column's name finds its table).
h.mod.catalog.find.set_value('price');
h.mod.catalog.find.handlers?.submit?.({}, h.cx);
await h.idle(30);
assert.ok(h.asked().some((p) => p === '/api/data/tables?q=price'), `the search is asked of the services: ${h.asked().filter((p) => p.includes('data/tables'))}`);
h.mod.catalog.find.set_value('');

// A row opens that table's page in place: a crumb back to the list, one line saying what it is, and two views.
const at = merged.rows.findIndex((r) => r.name === 'demo-catalog');
await h.click(`cat-row-${at}`);
await h.idle(50);
text = h.text({ 'grid-ds-rows': [0, 30] });
assert.ok(h.byIdPrefix('crumb-tables').length === 1, 'a crumb back to Tables');
assert.ok(text.includes('demo-catalog | v1'), 'the page header');
assert.ok(text.includes('A table Captain keeps versions of. This is version 1 of 1.'), `what it is, in one line: ${text.slice(0, 400)}`);
assert.deepEqual(h.byIdPrefix('dtab-').map((e) => e.call('id')[0]), ['dtab-sample', 'dtab-about'], 'two views: Rows and About');
assert.ok(text.includes('Rows') && text.includes('About') && text.includes('Where this came from'), 'Rows, About, and the lineage button');
assert.ok(text.includes('Ask an agent about it'), 'the agent action');

// The page opens on Rows: the kit grid every screen uses, paged by the services.
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

// About: the facts, the schema and the details on one page, then sections that load only when opened.
await h.click('dtab-about');
text = h.text();
assert.ok(text.includes('price') && text.includes('Versions') && text.includes('What made it'), `About holds the schema and the details: ${text.slice(0, 300)}`);
for (const sec of ['What uses it', 'Quality of each column']) assert.ok(text.includes(sec), `section ${sec}`);
const beforeQuality = h.asked().length;
assert.ok(!h.asked().some((p) => p.startsWith('/api/profile')), 'quality is not asked for until its section opens');
await h.click('dsec-quality');
await h.idle(30);
assert.ok(h.asked().slice(beforeQuality).some((p) => p.startsWith('/api/profile')), 'opening the section asks for it');

// Back to the one list.
await h.click('crumb-tables');
assert.equal(h.byIdPrefix('cat-row-').length, merged.rows.length, 'the list again');

// Sources: the databases Captain reads tables from, and the model registries.
await h.click('submenu-sources');
await h.idle(30);
text = h.text();
assert.ok(text.includes('Databases') && text.includes('Model registries'), `Sources: ${text.slice(0, 300)}`);
assert.ok(text.includes('Connect a database'), 'the one filled button connects one');

// Every render stayed far under gpui-kit's 50 ms limit.
const sorted = [...h.timings].sort((a, b) => a - b);
console.log(`renders: ${h.timings.length}, median ${sorted[Math.floor(sorted.length / 2)].toFixed(2)} ms, slowest ${sorted.at(-1).toFixed(1)} ms (V8; QuickJS in the app is slower, see RISKS.md)`);
assert.ok(Math.max(...h.timings) < 50, 'a render took 50 ms or more');
console.log('captain-data render test: ok');
