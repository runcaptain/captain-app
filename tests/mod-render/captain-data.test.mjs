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
  [/^\/api\/data\/tables/, 'data-tables-big'],
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
const merged = JSON.parse(fs.readFileSync(path.join(globalThis.__fixtures, 'data-tables-big.json'), 'utf8'));

// Two submenus, Tables first, and no Labels, Datasets, Notebooks or Models tab.
assert.deepEqual(h.byIdPrefix('submenu-').map((e) => e.call('id')[0]), ['submenu-tables', 'submenu-sources']);
assert.equal(h.byIdPrefix('tab-').length, 0, 'no strip of tabs besides the two submenus');
let text = h.text();
if (process.env.SHOW) console.log(text.slice(0, 1500));
assert.ok(!/\bLabels\b/.test(text), 'Labels left the screen');
assert.ok(!h.asked().some((p) => p.startsWith('/api/labels')), 'and nothing asks for labels');

// Tables: grouped by where the tables live, every group closed at first. No wall of tables.
const { groupsOf, plainTip, PAGE, BY_SCHEMA_OVER } = await import(path.join(work, 'catalog.js'));
assert.equal(PAGE, 5);
assert.equal(h.byIdPrefix('cat-row-').length, 0, 'no table shows until a group is opened');
const groups = groupsOf(merged.rows);
assert.deepEqual(groups.map((g) => g.id), ['db:demo-store', 'db:warehouse', 'kept'], 'each database, then ONE group for what Captain keeps: three rows, not a wall of folders');
const kept = groups.at(-1);
for (const g of groups) assert.equal(h.byIdPrefix(`cat-group-${g.id}`).filter((e) => e.call('id')[0] === `cat-group-${g.id}`).length, 1, `a heading for ${g.id}`);
assert.ok(text.includes('warehouse') && text.includes('Databricks') && text.includes('39 tables'), `a database's name, its kind and its count: ${text.slice(0, 400)}`);
assert.ok(text.includes('Kept in Captain') && text.includes('Tables Captain keeps versions of'), 'said once, on its row');
assert.ok(!text.includes('A folder of tables') && !text.includes('places'), 'not repeated on every row, and no count of places');
assert.ok(text.includes(`${merged.rows.length} tables`), 'the count');
assert.equal(h.byIdPrefix('cat-group-folder:').length, 0, 'folders are inside it, not beside it');
// A database's own mark is on its heading.
assert.ok(h.has(h.byIdPrefix('cat-group-db:warehouse')[0], 'svg') || h.has(h.byIdPrefix('cat-group-db:warehouse')[0], 'image'), 'the mark');

// Open a folder: its tables, with the five columns. A dataset is a table Captain keeps versions of: the Versions
// column says so. There is no separate Datasets screen.
await h.click('cat-group-kept');
text = h.text();
// Its folders are a second level, nested by path. A folder with one table shows that table directly.
assert.ok(h.byIdPrefix('cat-group-folder:logs').length === 1, 'a folder with several tables is a group inside it');
assert.ok(kept.parts.every((p) => p.rows.length > 1), 'no folder row for a single table');
for (const col of ['Name', 'Where it lives', 'Rows', 'Versions', 'Last read']) assert.ok(text.includes(col), `column ${col}`);
assert.equal(h.byIdPrefix('cat-row-').length, Math.min(5, kept.own.length), 'the tables directly in it, after its folders');
assert.ok(text.includes('Captain keeps 1 version') && text.includes('Captain keeps 2 versions'), 'the Versions column');
assert.ok(text.includes('Captain, a saved query on demo-store'), 'where it lives, in words');
assert.equal(versionWords({ kind: 'dataset', versions: 3 }), 'Captain keeps 3 versions');
assert.equal(versionWords({ kind: 'table', versions: null }), 'Read where it lives');
// The open groups are remembered for the workspace.
assert.deepEqual(Object.values(globalThis.__shared['captain.data.groups'])[0], ['kept'], 'remembered');

// A big database is grouped again by schema, and each schema shows five tables at a time with a pager.
assert.ok(39 > BY_SCHEMA_OVER);
await h.click('cat-group-db:warehouse');
assert.deepEqual(groups.find((g) => g.id === 'db:warehouse').parts.map((p) => [p.label, p.rows.length]), [['main.finance', 12], ['main.sales', 14], ['main.web', 13]]);
assert.equal(h.byIdPrefix('cat-row-table-warehouse').length, 0, 'its schemas first, closed');
await h.click('cat-group-db:warehouse/main.sales');
assert.equal(h.byIdPrefix('cat-row-table-warehouse').length, 5, 'five at a time');
text = h.text();
assert.ok(text.includes('1 to 5 of 14') && text.includes('Previous') && text.includes('Next'), `the pager: ${text.slice(-400)}`);
assert.equal(h.byIdPrefix('cat-prev-db:warehouse/main.sales')[0].call('on_click'), undefined, 'Previous is off on the first page');
await h.click('cat-next-db:warehouse/main.sales');
assert.ok(h.text().includes('6 to 10 of 14'), 'the next five');
await h.click('cat-next-db:warehouse/main.sales');
assert.ok(h.text().includes('11 to 14 of 14'), 'the last four');
assert.equal(h.byIdPrefix('cat-next-db:warehouse/main.sales')[0].call('on_click'), undefined, 'Next is off on the last page');
// The group's own search narrows it, and starts again at the first page.
h.mod.catalog.finds.get('db:warehouse/main.sales').set_value('order');
h.mod.catalog.finds.get('db:warehouse/main.sales').handlers?.change?.({}, h.cx);
await h.layoutLists({});
assert.deepEqual(h.byIdPrefix('cat-row-table-warehouse').map((e) => e.call('id')[0].split('/').pop()), ['order_items', 'orders'], 'the group\'s own search');
assert.ok(h.text().includes('1 to 2 of 2'));
// A small database is not split by schema: the schema is its "Where it lives".
await h.click('cat-group-db:demo-store');
assert.equal(groups.find((g) => g.id === 'db:demo-store').parts, undefined);
assert.ok(h.text().includes('demo-store.main') && h.text().includes('Read where it lives'), 'the schema, and that Captain keeps no versions');

// A description shows as plain words, about two lines: never raw markdown across the window.
await h.click('cat-group-db:warehouse/main.finance');
await h.click('cat-next-db:warehouse/main.finance');
await h.click('cat-next-db:warehouse/main.finance');
const usage = h.byIdPrefix('cat-row-table-warehouse/main.finance/usage')[0];
assert.ok(usage, 'the usage table is on the last page');
const tip = usage.call('tooltip')[0];
assert.ok(tip.startsWith('Billed usage per team. See the attributed usage table for how it is split.'), `links become their words: ${tip}`);
assert.ok(!/[\[\]()]|https?:/.test(tip) && !tip.includes('\n') && tip.length <= 150, `plain, one string, two lines at most: ${tip.length}`);
assert.equal(plainTip('**Bold** `code` [a link](https://example.com/x) and https://example.com/y'), 'Bold code a link and');
assert.equal(plainTip(null), '');
// Last read, in words.
const now = Date.parse('2026-11-12T12:00:00Z');
assert.equal(readWords('2026-11-12', now), 'Today');
assert.equal(readWords('2026-11-11', now), 'Yesterday');
assert.equal(readWords('2026-11-05T08:00:00+00:00', now), '7 days ago');
assert.equal(readWords('2026-08-01', now), 'Aug 1, 2026');
assert.equal(readWords(null, now), 'Not read yet');
assert.ok(!/@v\d/.test(h.text()), 'no raw name@v ref anywhere');
assert.ok(h.text().includes('Add a table') && !h.text().includes('New class'), 'the one filled button brings data in');

// Beside an open terminal column the list is narrow. The Versions column still shows, in shorter words.
h.mod.width = 700;
await h.layoutLists({});
text = h.text();
assert.ok(text.includes('Versions') && text.includes('2 kept') && text.includes('Live'), `narrow: ${text.slice(0, 500)}`);
h.mod.width = 1000;
await h.layoutLists({});

// The search at the top asks the services (so a column's name finds its table) and opens every group with a match.
h.mod.catalog.find.set_value('price');
h.mod.catalog.find.handlers?.submit?.({}, h.cx);
await h.idle(30);
assert.ok(h.asked().some((p) => p === '/api/data/tables?q=price'), `the search is asked of the services: ${h.asked().filter((p) => p.includes('data/tables'))}`);
assert.ok(h.mod.catalog.groups.every((g) => h.mod.catalog.isOpen(g.id)), 'every group with a match is open');
h.mod.catalog.find.set_value('');
h.mod.catalog.load(h.cx);
await h.idle(30);
assert.ok(h.mod.catalog.isOpen('kept') && !h.mod.catalog.isOpen('folder:logs'), 'cleared: back to the groups the person opened');

// A row opens that table's page in place: a crumb back to the list, one line saying what it is, and two views.
await h.click('cat-row-dataset-demo-catalog');
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
assert.ok(h.byIdPrefix('cat-group-kept').length >= 1 && h.byIdPrefix('crumb-tables').length === 0, 'the groups again');

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
