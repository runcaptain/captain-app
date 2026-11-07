// Render test for the Notebooks tab in captain-data, against fixtures and a fake kernel socket (stubs/websocket.js).
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

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data-notebook');
globalThis.__routes = [
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

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-nb-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();
await h.click('tab-notebooks');
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
assert.ok(text.includes('2,500 rows, 2 columns') && text.includes('showing the first 1,000'), 'grid footer');
assert.ok(text.includes('Download CSV') && text.includes('Copy TSV'), 'grid exports');
assert.ok(text.includes('sofas'), 'grid rows');
assert.ok(text.includes('Python ready'), 'kernel ready');

// The result grid's column menu: filter conv > 20, stats, then sort.
await h.click('gm-sql1-0-1');
text = h.text();
assert.ok(text.includes('sum') && text.includes('median') && text.includes('unique'), 'column stats');
await h.click('gop-sql1-0-1-gt');
h.mod.nb.grids.get('sql1-0').inputs.get('1:a').set_value('20');
await h.click('gapply-sql1-0-1');
assert.ok(/500 of 1,000 rows match/.test(h.text()), `filter applied: ${h.text().match(/\d[\d,]* of [\d,]* rows match/)?.[0]}`);
await h.click('gclose-sql1-0');
assert.ok(!h.text().includes('median'), 'menu closed');
assert.ok((h.mod.nb.cells[1].meta.limit ?? 1000) === 1000, 'default row limit');
await h.click('lim-sql1-10000');
assert.equal(h.mod.nb.cells[1].meta.limit, 10000, 'row limit picked');

// Shift-Enter on the Python cell runs it: stdout shows.
await h.key('cell-py1', 'shift-enter');
await h.idle();
assert.ok(h.text().includes('2500'), 'stdout');

// The chart draws from df_1 (bars), with its pickers.
text = h.text();
assert.ok(text.includes('conv by category'), 'chart drawn from the SQL result');
await h.click('ch-m-ch1-point');
assert.ok(h.text().includes('scatter'), 'chart kinds');

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

// Import: reads the file, posts it, says what it was.
globalThis.__files = { '/tmp/Downloads/analysis.py': 'import marimo\n' };
h.mod.nb.importPath.set_value('~/Downloads/analysis.py');
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
