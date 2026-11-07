// The kit grid paged from the services, against a fake rows route: pages ("1 to 100 of 250"), Previous and Next,
// sorting the whole table by a header, the Where box, column kinds (decimals exact and right-aligned, lists short),
// and a clicked row written out in full.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/grid-paged.test.mjs
import assert from 'node:assert/strict';
import { ResultGrid, columnType, shortJson } from '../../defaults/captain/kit/grid.js';
import { palette } from '../../defaults/captain/kit/tokens.js';

const all = Array.from({ length: 250 }, (_, i) => [i + 1, `item ${i + 1}`, (i * 1.1).toFixed(2), JSON.stringify(['a', 'b', String(i)])]);
const columns = [{ name: 'id', type: 'BIGINT', kind: 'integer' }, { name: 'title', type: 'VARCHAR', kind: 'text' }, { name: 'price', type: 'DECIMAL(10,2)', kind: 'decimal' }, { name: 'tags', type: 'VARCHAR[]', kind: 'list' }];
const asked = [];
const load = async (q) => {
  asked.push(q);
  let rows = all;
  if (q.where) { const m = /id\s*<\s*(\d+)/.exec(q.where); if (!m) return { ok: false, status: 400, data: { error: 'that WHERE does not parse' } }; rows = rows.filter((r) => r[0] < Number(m[1])); }
  if (q.order) { const j = columns.findIndex((c) => c.name === q.order); const v = (x) => (j === 2 ? Number(x) : x); rows = [...rows].sort((a, b) => (v(a[j]) < v(b[j]) ? -1 : 1) * (q.desc ? -1 : 1)); }
  const page = rows.slice(q.offset, q.offset + q.limit);
  return { ok: true, data: { columns, rows: page, offset: q.offset, limit: q.limit, has_more: q.offset + q.limit < rows.length, total: q.count ? rows.length : undefined, ms: 3 } };
};
let cx;
const tasks = [];
cx = { spawn: (f) => tasks.push(f(cx)), notify() {} };
const flush = async () => { while (tasks.length) await tasks.shift(); };
const c = palette(true);
const textOf = (e, out = []) => { if (typeof e === 'string') out.push(e); else if (e && typeof e === 'object') for (const k of e.kids ?? []) textOf(k, out); return out; };
const find = (e, id) => { if (!e || typeof e !== 'object') return null; if (e.call?.('id')?.[0] === id) return e; for (const k of e.kids ?? []) { const f = find(k, id); if (f) return f; } return null; };
const click = async (g, id) => { const el = find(g.render(c), id); assert.ok(el, `no ${id}`); el.call('on_click')[0]({}, cx); await flush(); };

assert.equal(columnType(columns[0], []), 'id');
assert.equal(columnType(columns[2], []), 'number');
assert.equal(columnType(columns[3], []), 'json');
assert.equal(shortJson('["a","b","c"]'), '[3] a, b, c');
assert.equal(shortJson('{"k":1,"j":"x"}'), '{2} k: 1, j: x');

const g = new ResultGrid({ id: 'p', name: 'products', load });
g.fetch(cx);
await flush();
let t = textOf(g.render(c)).join(' | ');
assert.ok(t.includes('1 to 100 of 250'), t.slice(0, 300));
assert.equal(g.show(g.table.rows[0][3], 3), '[3] a, b, 0', 'lists shortened');
assert.equal(g.show('1.10', 2), '1.10', 'a decimal exactly as sent');
assert.equal(asked[0].count, 'true', 'counted once');
await click(g, 'gnext-p');
t = textOf(g.render(c)).join(' | ');
assert.ok(t.includes('101 to 200 of 250') && g.table.rows[0][1] === 'item 101', 'next page');
assert.equal(asked[1].count, undefined, 'not counted again');
await click(g, 'gprev-p');
assert.ok(textOf(g.render(c)).join(' | ').includes('1 to 100 of 250'), 'previous page');
// Sort the whole table by price, largest first: two header clicks.
await click(g, 'gh-p-2');
await click(g, 'gh-p-2');
assert.deepEqual([asked.at(-1).order, asked.at(-1).desc, asked.at(-1).offset], ['price', 'true', 0]);
assert.equal(g.table.rows[0][0], 250, 'the largest price of all 250 first');
// Where: the whole table, counted again.
g.whereBox.set_value('id < 31');
await click(g, 'gwhere-p');
t = textOf(g.render(c)).join(' | ');
assert.ok(t.includes('1 to 30 of 30'), t.slice(0, 300));
// A WHERE the services refuse: their sentence, not a crash.
g.whereBox.set_value('nonsense');
await click(g, 'gwhere-p');
assert.ok(textOf(g.render(c)).join(' | ').includes('That WHERE does not parse.'));
// A row clicked: every column in full, the list as JSON.
g.whereBox.set_value('');
await click(g, 'gwhere-p');
g.openRow = 0;
t = textOf(g.render(c)).join(' | ');
assert.ok(t.includes('Row 1') && t.includes('"a",'), 'row in full');
console.log('grid-paged test: ok');
