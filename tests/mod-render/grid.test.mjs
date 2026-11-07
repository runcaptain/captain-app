// The kit's result grid, without a window: type detection, typed filters, sort, column stats and CSV/TSV.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/grid.test.mjs
import assert from 'node:assert/strict';
import { detectType, columnStats, passes, ResultGrid } from '../../defaults/captain/kit/grid.js';
import { int, num } from '../../defaults/captain/kit/num.js';

// Numbers group their own digits: the app's JavaScript has no locale data for toLocaleString.
assert.deepEqual([int(12480), int(-1234567), int(999), int(null), num(1234.5678), num(0.1, 4), num(2, 2, 2), num(-0.004)], ['12,480', '-1,234,567', '999', '', '1,234.57', '0.1', '2.00', '0']);

assert.equal(detectType('price', 'double', [1.5, 2]), 'number');
assert.equal(detectType('n', '', ['1', '2', '3']), 'number');
assert.equal(detectType('product_id', 'string', ['p1', 'p2']), 'id');
assert.equal(detectType('ok', '', [true, false]), 'boolean');
assert.equal(detectType('meta', '', ['{"a":1}', '[1]']), 'json');
assert.equal(detectType('page', '', ['https://a.example/x']), 'url');
assert.equal(detectType('title', 'string', ['Sofa', 'Lamp']), 'string');

const s = columnStats([1, 2, 3, 4, null], 'number');
assert.deepEqual([s.sum, s.avg, s.median, s.min, s.max, s.unique, s.empty], [10, 2.5, 2.5, 1, 4, 4, 1]);
assert.deepEqual(columnStats(['a', 'b', 'a'], 'string').common[0], ['a', 2]);

assert.ok(passes(5, { op: 'gt', value: '3' }, 'number') && !passes(2, { op: 'gt', value: '3' }, 'number'));
assert.ok(passes(5, { op: 'between', value: '6', value2: '4' }, 'number'));
assert.ok(passes('Big Sofa', { op: 'contains', value: 'sofa' }, 'string'));
assert.ok(passes('Sofa', { op: 'starts', value: 'so' }, 'string') && !passes('Sofa', { op: 'ends', value: 'so' }, 'string'));
assert.ok(!passes('Sofa', { op: 'not_contains', value: 'of' }, 'string'));

const g = new ResultGrid({ id: 't', table: { columns: [{ name: 'name', type: 'string' }, { name: 'price', type: 'double' }], rows: [['b, "x"', 10], ['a', 2], ['c', 30]], row_count: 3 } });
g.sort = { col: 1, desc: true };
assert.deepEqual(g.view(), [2, 0, 1], 'sorted by number, largest first');
g.filters[1] = { op: 'lt', value: '20' };
assert.deepEqual(g.view(), [0, 1], 'filtered and still sorted');
assert.equal(g.csv(','), 'name,price\n"b, ""x""",10\na,2', 'CSV quoting');
g.hidden.add(0);
assert.equal(g.csv('\t'), 'price\n10\n2', 'TSV without hidden columns');
g.formats[1] = 'dollar';
assert.equal(g.show(1234.5, 1), '$1,234.50');
g.formats[1] = 'human';
assert.equal(g.show(1234567, 1), '1.2M');
console.log('grid test: ok');
