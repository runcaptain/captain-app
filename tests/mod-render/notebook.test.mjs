// Render test for the Notebooks view (captain-notebooks), against fixtures and a fake kernel socket (stubs/websocket.js).
// It opens a notebook, runs a SQL cell into a grid, a Python cell, a failing cell (a sentence, Copy details, the
// traceback only behind it), stops a long cell, draws a chart from the SQL result, autosaves a draft, saves a
// version with Cmd-S, exports, imports, and adds cells. Then it checks the 50 ms render budget.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/notebook.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data-notebook');
globalThis.__routes = [
  [/^\/api\/notebooks\/[^?]+\/rename$/, 'renamed'],
  [/^\/api\/notebooks\/[^?]+\/draft$/, 'draft'],
  [/^\/api\/notebooks\/[^?]+\/save$/, 'save'],
  [/^\/api\/notebooks\/[^?]+\/import$/, 'import'],
  [/^\/api\/notebooks\/[^?]+\/export/, 'export'],
  [/^\/api\/notebooks\/[^?]+$/, 'notebook'],
  [/^\/api\/notebooks$/, 'notebooks'],
  [/^\/api\/notebook-formats$/, 'formats'],
  [/^\/api\/connections$/, 'connections'],
  [/^\/api\/kernels/, 'kernel'],
];
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://127.0.0.1:9', token: 'tok' } };

// The fake kernel: SQL returns 2,500 rows (1,000 sent), "boom" raises, "sleep" waits for an interrupt.
let waiting = null;
globalThis.__kernel = (m, send) => {
  if (m.type === 'interrupt' && waiting) {
    send({ type: 'error', cell_id: waiting, ename: 'KeyboardInterrupt', evalue: '', traceback: [] });
    send({ type: 'done', cell_id: waiting, ms: 900 });
    send({ type: 'status', state: 'idle' });
    waiting = null;
    return;
  }
  // A page of df_1 for the notebook grid: all 2,500 rows live "in Python", sorted and filtered there.
  if (m.type === 'page') {
    globalThis.__pages = [...(globalThis.__pages ?? []), m];
    const cats = ['sofas', 'lamps', 'rugs', 'beds'];
    let all = Array.from({ length: 2500 }, (_, i) => [cats[i % 4], (i % 4) * 10 + 5]);
    const w = /conv\s*>\s*(\d+)/.exec(m.where ?? '');
    if (w) all = all.filter((r) => r[1] > Number(w[1]));
    if (m.order === 'conv') all.sort((a, b) => (a[1] - b[1]) * (m.desc ? -1 : 1));
    const rows = all.slice(m.offset, m.offset + m.limit);
    send({ type: 'page', cell_id: m.req, columns: [{ name: 'category', type: 'VARCHAR', kind: 'text' }, { name: 'conv', type: 'BIGINT', kind: 'integer' }], rows, offset: m.offset, limit: m.limit, has_more: m.offset + m.limit < all.length, total: m.count ? all.length : undefined, ms: 4, filtered: !!m.where });
    send({ type: 'done', cell_id: m.req, ms: 4 });
    return;
  }
  if (m.type !== 'execute') return;
  send({ type: 'status', state: 'busy' });
  if (m.kind === 'sql') {
    const cats = ['sofas', 'lamps', 'rugs', 'beds'];
    const rows = Array.from({ length: 1000 }, (_, i) => [cats[i % 4], (i % 4) * 10 + 5]);
    send({ type: 'table', cell_id: m.cell_id, var: m.var, columns: [{ name: 'category', type: 'string' }, { name: 'conv', type: 'int64' }], rows, row_count: 2500, truncated: false });
  } else if (/boom/.test(m.code)) {
    send({ type: 'error', cell_id: m.cell_id, ename: 'NameError', evalue: "name 'boom' is not defined", traceback: ['Traceback (most recent call last):', '  File "<cell>", line 1', "NameError: name 'boom' is not defined"] });
  } else if (/sleep/.test(m.code)) {
    waiting = m.cell_id;
    return;
  } else {
    send({ type: 'stream', cell_id: m.cell_id, name: 'stdout', text: '2500\n' });
  }
  send({ type: 'done', cell_id: m.cell_id, ms: 420 });
  send({ type: 'status', state: 'idle' });
};

const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-notebooks-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-notebooks'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();
// Notebooks is its own view: nothing to click to get to it.
await h.idle();

// The list and the first notebook, its markdown drawn as text.
let text = h.text();
if (process.env.SHOW) console.log(text.slice(0, 900), globalThis.__asked);
assert.ok(text.includes('weekly-check') && text.includes('scratch') && text.includes('draft'), 'notebook list');
assert.ok(text.includes('Weekly search check') && !text.includes('# Weekly'), 'markdown drawn, not shown as source');
assert.ok(text.includes('Python starts when you run a cell'), 'kernel off until a run');
assert.ok(text.includes('warehouse') && text.includes('local-duck'), 'connection choices on the SQL cell');
assert.ok(!/@v\d/.test(text), 'no raw name@v');

// Run the SQL cell with Cmd-Enter: a grid of what came back, the row count, the result's name.
await h.key('cell-sql1', 'cmd-enter');
await h.idle();
text = h.text();
assert.ok(globalThis.__asked.some((p) => p === '/api/kernels'), 'kernel session started');
assert.ok(globalThis.__sockets?.[0]?.url.startsWith('ws://127.0.0.1:9/api/kernels/abc123?token='), 'socket to the session');
assert.ok(/Done, 420 ms, 2,500 rows/.test(text), `cell status: ${text.slice(0, 400)}`);
// More rows than came back: the grid pages all 2,500 in Python, 100 at a time.
await h.idle();
text = h.text();
assert.ok(/1 to 100 of 2,500/.test(text), `pager: ${text.match(/\d+ to [^|]*/)?.[0]}`);
assert.equal(globalThis.__pages[0].var, 'df_1', 'the page names the result');
assert.ok(text.includes('Download CSV') && text.includes('Copy TSV'), 'grid exports');
assert.ok(text.includes('sofas'), 'grid rows');
assert.ok(text.includes('Python ready'), 'kernel ready');

// The column menu's stats; the Where box filters all 2,500 rows; a header click sorts them all, in Python.
await h.click('gm-sql1-0-1');
text = h.text();
assert.ok(text.includes('sum') && text.includes('median') && text.includes('unique') && text.includes('(this page)'), 'column stats, on this page');
await h.click('gclose-sql1-0');
assert.ok(!h.text().includes('median'), 'menu closed');
h.mod.nb.grids.get('sql1-0').whereBox.set_value('conv > 20');
await h.click('gwhere-sql1-0');
await h.idle();
assert.ok(/1 to 100 of 1,250/.test(h.text()), `filtered over every row: ${h.text().match(/\d+ to [^|]*/)?.[0]}`);
await h.click('gh-sql1-0-1');
await h.click('gh-sql1-0-1');
await h.idle();
assert.deepEqual([globalThis.__pages.at(-1).order, globalThis.__pages.at(-1).desc, globalThis.__pages.at(-1).where], ['conv', true, 'conv > 20'], 'sorted in Python, keeping the filter');
assert.ok((h.mod.nb.cells[1].meta.limit ?? 1000) === 1000, 'default row limit');
await h.click('lim-sql1-10000');
assert.equal(h.mod.nb.cells[1].meta.limit, 10000, 'row limit picked');

// Shift-Enter on the Python cell runs it: stdout shows.
await h.key('cell-py1', 'shift-enter');
await h.idle();
assert.ok(h.text().includes('2500'), 'stdout');

// The chart draws from df_1 (bars), with its pickers.
text = h.text();
assert.ok(text.includes('Stacked area') && text.includes('Horizontal bar') && text.includes('Doughnut') && text.includes('Bubble'), 'the nine kinds');
assert.ok(text.includes('Split by') && text.includes('Combine') && text.includes('Log'), 'chart settings');
assert.ok(text.includes('sofas'), 'categories drawn from the SQL result');
await h.click('ch-m-ch1-scatter');
assert.ok(h.text().includes('Up (y)'), 'scatter asks for x and y');
await h.click('ch-m-ch1-bar');
await h.click('ch-a-ch1-sum');
assert.equal(h.mod.nb.cells.find((x) => x.id === 'ch1').meta.chart.agg, 'sum', 'combine by sum, saved in the cell');

// A failing cell: one sentence, Copy details, an agent; no traceback on screen.
await h.click('nb-add-python');
const added = h.mod.nb.cells[h.mod.nb.cells.length - 1];
// Typed into the cell's editor (created when the cell drew).
h.mod.nb.editors.get(added.id).set_value('boom');
await h.click(`run-${added.id}`);
await h.idle();
text = h.text();
assert.ok(text.includes("Nothing is called boom yet: run the cell that makes it first."), `error sentence: ${text.slice(-500)}`);
assert.ok(!text.includes('Traceback'), 'traceback hidden');
assert.ok(text.includes('Copy details') && text.includes('Ask an agent to fix it'), 'error actions');

// A long cell, stopped.
h.mod.nb.editors.get(added.id).set_value('sleep(60)');
await h.click(`run-${added.id}`);
await h.idle();
if (process.env.SHOW) console.log('RUN::', h.text().slice(-700), [...h.mod.nb.state.entries()]);
assert.ok(/Running, /.test(h.text()), 'running clock');
await h.click(`stop-${added.id}`);
await h.idle();
assert.ok(/Stopped/.test(h.text()), 'stopped');

// Autosave: an edit saves a draft after a pause; Cmd-S saves a version.
h.mod.nb.lastEdit = Date.now() - 5000;
h.mod.nb.dirty = true;
await h.layoutLists({});
assert.ok(globalThis.__asked.some((p) => /\/api\/notebooks\/weekly-check\/draft$/.test(p)), 'draft autosaved');
await h.key('notebooks', 'cmd-s');
await h.idle();
const saved = globalThis.__bodies.filter(([p]) => /\/save$/.test(p)).pop();
assert.ok(saved && saved[1].body.notebook.cells.length === 5, 'saved the whole notebook');
assert.equal(saved[1].body.notebook.cells[1].metadata.captain.conn, 'warehouse', 'cell metadata kept');
assert.ok(h.text().includes('Saved as version 3.'), 'saved note');

// Export: a format, written to Downloads.
await h.click('nb-export');
await h.click('nb-fmt-quarto');
await h.idle();
assert.ok((globalThis.__written ?? []).some((p) => p.endsWith('/Downloads/weekly-check.qmd')), 'exported file written');
assert.ok(h.text().includes('Exported to ~/Downloads/weekly-check.qmd.'), 'export note');

// Rename: a click on the title shows the field with the name in it. Any words do. Only what the notebook is called
// changes: its id, its version, its cells and its Python session are the same before and after.
{
  const nb = h.mod.nb;
  const id = nb.open.name, version = nb.open.version, kernel = nb.kernel, cells = nb.cells, dirty = nb.dirty;
  await h.click('nb-title');
  assert.equal(nb.renameTo.value(), id, 'the field starts with its name');
  assert.equal(h.byIdPrefix('nb-rename-go')[0].call('on_click'), undefined, 'the same name: Rename is off');
  nb.renameTo.set_value('Weekly review, Q4');
  await h.layoutLists({});
  const before = h.asked().length;
  await h.click('nb-rename-go');
  await h.idle(40);
  assert.deepEqual(h.asked().slice(before).filter((p) => !p.includes('/api/kernels')), [`/api/notebooks/${id}/rename`], 'one request, and no draft or save');
  assert.deepEqual([nb.open.name, nb.open.version, nb.kernel, nb.cells, nb.dirty], [id, version, kernel, cells, dirty], 'the id, the version, the cells and the Python session are untouched');
  assert.equal(nb.list.find((n) => n.name === id).label, 'Weekly review, Q4', 'the list has its new name');
  let t = h.text();
  assert.ok(t.includes('Weekly review, Q4') && t.includes('Renamed to Weekly review, Q4.') && h.byIdPrefix('nb-rename-field').length === 0, 'the title and the row show it');
  assert.equal(h.byIdPrefix('nb-id').length, 1, 'its id sits small under the name, for commands');
  // Every row has Rename, and Cancel puts the title back.
  assert.equal(h.byIdPrefix('nb-rename-row-').length, nb.list.length, 'Rename on every row');
  await h.click(`nb-rename-row-${id}`);
  assert.equal(nb.renameTo.value(), 'Weekly review, Q4', 'the field starts with the name it has now');
  await h.click('nb-rename-cancel');
  assert.equal(h.byIdPrefix('nb-title').length, 1);
}

// Import: reads the file, posts it, says what it was.
globalThis.__files = { '/tmp/Downloads/analysis.py': 'import marimo\n' };
// Nothing typed: the button is off. A path typed: it is on, and the line under the field follows the typing.
assert.equal(h.byIdPrefix('nb-import-go')[0].call('on_click'), undefined, 'no path yet: Import is off');
h.mod.nb.importPath.set_value('~/Downloads/analysis.py');
await h.layoutLists({});
await h.click('nb-import-go');
await h.idle();
const imp = globalThis.__bodies.filter(([p]) => /\/import$/.test(p)).pop();
assert.equal(imp?.[1].body.filename, 'analysis.py', 'import posts the file');
assert.ok(/Imported analysis.py \(marimo/.test(h.text()), 'import note');

// Budget: no frame over 50 ms.
const worst = Math.max(...h.timings);
assert.ok(worst < 50, `slowest render ${worst.toFixed(1)} ms`);
for (const s of globalThis.__sockets ?? []) s.close();
console.log(`notebook render test: ok (slowest render ${worst.toFixed(1)} ms)`);
process.exit(0);
