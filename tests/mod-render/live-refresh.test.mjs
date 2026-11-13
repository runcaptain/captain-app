// The owner: "I think everything needs to refresh live." A view's lists reload by themselves: the moment the live
// channel says something they show changed, at once when the view comes back into view, and at once when a form
// changes something. Nothing is read on a clock. The
// reload is quiet: nothing drawn goes blank and the person's place does not move.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/live-refresh.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { keepFresh, viewShown, recordsChanged, connectionsChanged, api: realApi } = await import('../../defaults/captain/kit/cst.js');
const { liveTransport, liveReset } = await import('../../defaults/captain/kit/live.js');

// ---- the one helper every view uses -------------------------------------------------------------------------------
{
  const stack = (active) => ({ id: 'root', children: [{ id: 'sections', active, children: [{ id: 'data' }, { id: 'runs' }] }] });
  globalThis.__layout = stack('data');
  assert.deepEqual([viewShown('data'), viewShown('runs')], [true, false]);
  const timers = [], why = [];
  const cx = { timer: { every: (_ms, f) => timers.push(f) }, spawn: (f) => f(cx), notify() {} };
  // The live channel, held by the test: each request is answered when the test says something changed.
  let now = 1_000_000, held = null;
  const asked = [];
  liveReset();
  liveTransport((p) => { asked.push(p); return p.includes('since=') ? new Promise((r) => { held = r; }) : Promise.resolve({ ok: true, data: { seq: 10, changed: [], reset: false } }); }, () => now);
  const tick = async (ms = 300) => { now += ms; for (const f of timers) f(cx); await new Promise((r) => setImmediate(r)); };
  const say = async (...topics) => { const h = held; held = null; h({ ok: true, data: { seq: (now += 1), changed: topics.map((topic) => ({ topic, seq: now })), reset: false } }); await new Promise((r) => setImmediate(r)); await tick(); };
  try {
    keepFresh(cx, 'data', (_cx, w) => why.push(w), ['data', 'table:']);
    await tick(); await tick(1200);
    assert.ok(decodeURIComponent(asked.at(-1)).includes('topics=data,table:'), `it listens for what it shows: ${asked.at(-1)}`);
    for (let i = 0; i < 40; i++) await tick(1000);
    assert.deepEqual(why, [], 'nothing is read on a clock, however long nothing changes');
    // Something changed, from anywhere: read at once.
    await say('data');
    assert.deepEqual(why, ['live'], 'the moment the feed says so');
    await say('table:demo-catalog');
    assert.deepEqual(why, ['live', 'live'], 'a table of any name counts');
    await say('notebooks');
    assert.equal(why.length, 2, 'what it does not show is not its news');
    // A form changed something: at once.
    recordsChanged('backtest'); await tick();
    assert.deepEqual(why.at(-1), 'changed');
    await new Promise((r) => setTimeout(r, 5));
    connectionsChanged(); await tick();
    assert.deepEqual(why.slice(-2), ['changed', 'changed'], 'a database or cloud connected counts too');
    // Hidden behind another view: nothing is read, even when the feed says something changed. Back in view: at once.
    globalThis.__layout = stack('runs');
    await tick();
    const n = why.length;
    await say('data');
    assert.equal(why.length, n, 'a view that is not shown does not read');
    globalThis.__layout = stack('data');
    await tick();
    assert.deepEqual(why.at(-1), 'shown', 'back in view: fresh at once, which reads what it missed');
    // A change made by a form while hidden still reaches it, so it is never older than the person's last action.
    globalThis.__layout = stack('runs');
    await new Promise((r) => setTimeout(r, 5)); // a later moment than the last form's change
    recordsChanged('dataset'); await tick();
    assert.equal(why.at(-1), 'changed');
  } finally { globalThis.__layout = undefined; liveReset(); liveTransport((path, opts) => realApi(path, opts), () => Date.now()); }
}

const mount = async (mod, fixtures, routes) => {
  globalThis.__fixtures = path.join(here, 'fixtures', fixtures);
  globalThis.__routes ??= [];
  globalThis.__routes.length = 0;
  globalThis.__routes.push(...routes);
  const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-fresh-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), work, { recursive: true });
  fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
  fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
  const { default: Mod } = await import(path.join(work, 'main.js'));
  const h = harness(Mod);
  await h.start();
  await h.idle(40);
  return h;
};
const swap = (file, to) => { globalThis.__routes.find((r) => r[1] === file)[1] = to; };

// ---- Data: a table added from a terminal shows up, and the person's place does not move --------------------------
{
  const big = JSON.parse(fs.readFileSync(path.join(here, 'fixtures/captain-data/data-tables-big.json'), 'utf8'));
  const more = { ...big, rows: [...big.rows, { ...big.rows.find((r) => r.kind === 'dataset' && !r.folder), name: 'made-in-a-terminal' }] };
  fs.writeFileSync(path.join(here, 'fixtures/captain-data/.fresh-more.json'), JSON.stringify(more));
  try {
    const h = await mount('captain-data', 'captain-data', [
      [/^\/api\/dataset-classes/, 'classes'], [/\/rows\?/, 'rows0'], [/^\/api\/data\/tables/, 'data-tables-big'],
      [/^\/api\/datasets(\?|$)/, 'datasets'], [/^\/api\/datasets\/[^?]+$/, 'detail'],
    ]);
    assert.ok(/keepFresh\(cx, "data"/.test(fs.readFileSync(path.join(root, 'defaults/mods/captain-data/main.js'), 'utf8')));
    await h.click('cat-group-db:warehouse');
    await h.click('cat-group-db:warehouse/main.sales');
    await h.click('cat-next-db:warehouse/main.sales');
    const before = h.byIdPrefix('cat-range-db:warehouse/main.sales')[0];
    const n = h.mod.catalog.rows.length;
    swap('data-tables-big', '.fresh-more');
    h.mod.refreshLists(h.cx);
    await h.idle(40);
    assert.equal(h.mod.catalog.rows.length, n + 1, 'the new table is listed, with no button pressed');
    assert.ok(h.text().includes(`${n + 1} tables`));
    assert.ok(h.mod.catalog.isOpen('db:warehouse') && h.mod.catalog.isOpen('db:warehouse/main.sales'), 'the open groups stay open');
    assert.equal(h.mod.catalog.page.get('db:warehouse/main.sales'), 5, 'and on the same page');
    assert.ok(before && h.text().includes('6 to 10 of 14'));
    // While a search is being typed, nothing is asked: the answer would land under the person's fingers.
    const asked = h.asked().length;
    h.mod.catalog.find.set_value('ord'); h.mod.catalog.typed = Date.now();
    h.mod.refreshLists(h.cx);
    await h.idle(20);
    assert.equal(h.asked().length, asked, 'no request while typing');
    h.mod.catalog.find.set_value(''); h.mod.catalog.typed = null;
    // A page that is open: only its facts are asked for, gently (every third time), and never its rows.
    h.mod.openDataset(h.cx, 'demo-catalog');
    await h.idle(40);
    const grid = h.mod.page.grid ?? h.mod.page.rows ?? null;
    const detail = h.mod.detail;
    let during = h.asked().length;
    h.mod.gentle = 0;
    h.mod.refreshLists(h.cx); h.mod.refreshLists(h.cx);
    await h.idle(20);
    assert.equal(h.asked().length, during, 'two of three times nothing is asked');
    h.mod.refreshLists(h.cx);
    await h.idle(40);
    const got = h.asked().slice(during);
    assert.ok(got.length > 0 && got.every((p) => /^\/api\/datasets(\?|$|\/demo-catalog\?v=)/.test(p)), `only the list and its record: ${got}`);
    assert.ok(!got.some((p) => p.includes('/rows')), 'never its rows');
    assert.equal(h.mod.detail, detail, 'the page keeps its record: nothing is redrawn from scratch');
    assert.equal(h.mod.page.grid ?? h.mod.page.rows ?? null, grid, 'and its grid');
    assert.equal(h.mod.shown, 'dataset');
    // A database table's page: its facts are merged in place; its columns and rows stay as drawn.
    const src = fs.readFileSync(path.join(root, 'defaults/mods/captain-data/tables.js'), 'utf8');
    const body = src.slice(src.indexOf('  refreshFacts(cx) {'), src.indexOf('  /** The Rows tab'));
    assert.ok(body.includes('typeof v === "object"') && !body.includes('loadRows') && !body.includes('this.page ='), 'facts only');
  } finally { fs.rmSync(path.join(here, 'fixtures/captain-data/.fresh-more.json'), { force: true }); }
}

// ---- Runs: its lists are asked for again, and what is open stays open ------------------------------------------------
{
  const main = fs.readFileSync(path.join(root, 'defaults/mods/captain-runs/main.js'), 'utf8');
  assert.ok(/keepFresh\(cx, "runs"/.test(main), 'Runs keeps its lists fresh');
  const ev = fs.readFileSync(path.join(root, 'defaults/mods/captain-evaluation/main.js'), 'utf8');
  assert.ok(ev.includes('recordsChanged("backtest")'), 'a new evaluation tells the lists at once');
}

// ---- Notebooks: the list only, never the open notebook's text -----------------------------------------------------
{
  const { Notebooks } = await import('../../defaults/mods/captain-notebooks/notebooks.js');
  const src = fs.readFileSync(path.join(root, 'defaults/mods/captain-notebooks/notebooks.js'), 'utf8');
  const body = src.slice(src.indexOf('  refreshList(cx) {'), src.indexOf('  load(cx) {'));
  assert.ok(body.includes('api("/api/notebooks")') && !body.includes('this.open =') && !body.includes('openNotebook'), 'it asks for the list and leaves the open notebook alone');
  assert.ok(typeof Notebooks.prototype.refreshList === 'function');
  assert.ok(/keepFresh\(cx, "notebooks"/.test(fs.readFileSync(path.join(root, 'defaults/mods/captain-notebooks/main.js'), 'utf8')));
}

// ---- the sidebar: its chips and counts, at once on a change ---------------------------------------------------------
{
  const nav = fs.readFileSync(path.join(root, 'defaults/mods/captain-nav/main.js'), 'utf8');
  assert.ok(nav.includes('watchRecords(cx, (cx) => { this.refresh(cx); this.explorer.load(cx); });') && nav.includes('watchConnections(cx, (cx) => this.refresh(cx));'));
}
console.log('live refresh: ok');
process.exit(0);
