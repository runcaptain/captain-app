// The kit's charts without a window: shaping rows (pivot, aggregate, swap, sort, multiple y), formats, and every one of
// the nine kinds drawing something, with hidden series left out and the legend toggling.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/charts.test.mjs
import assert from 'node:assert/strict';
import { shape, fmt, ChartView, MARKS } from '../../defaults/captain/kit/charts.js';
import { palette } from '../../defaults/captain/kit/tokens.js';

const table = {
  columns: [{ name: 'day', type: 'string' }, { name: 'device', type: 'string' }, { name: 'searches', type: 'int' }, { name: 'buys', type: 'int' }],
  rows: [['mon', 'mobile', 10, 1], ['mon', 'desktop', 20, 4], ['tue', 'mobile', 30, 3], ['tue', 'desktop', 5, 1], ['tue', 'mobile', 2, 0]],
};

// Pivot by device, summed: tue/mobile is 30 + 2.
let d = shape(table, { x: 'day', y: ['searches'], series: 'device', agg: 'sum' });
assert.deepEqual(d.categories, ['mon', 'tue']);
assert.deepEqual(d.series.map((s) => s.name), ['mobile', 'desktop']);
assert.deepEqual(d.series[0].values, [10, 32]);
// Two measures as two series, averaged.
d = shape(table, { x: 'day', y: ['searches', 'buys'], agg: 'avg' });
assert.deepEqual(d.series.map((s) => s.name), ['searches', 'buys']);
assert.equal(d.series[1].values[0], 2.5);
// Count rows per category.
assert.deepEqual(shape(table, { x: 'device', agg: 'count' }).series[0].values, [3, 2]);
// Median.
assert.deepEqual(shape(table, { x: 'device', y: ['searches'], agg: 'median' }).series[0].values, [10, 12.5]);
// Swap: devices become categories.
d = shape(table, { x: 'day', y: ['searches'], series: 'device', agg: 'sum', swap: true });
assert.deepEqual(d.categories, ['mobile', 'desktop']);
assert.deepEqual(d.series.map((s) => s.name), ['mon', 'tue']);
// Sort by total, largest first.
d = shape(table, { x: 'device', y: ['searches'], agg: 'sum', sort: 'value-desc' });
assert.deepEqual(d.categories, ['mobile', 'desktop']);
d = shape(table, { x: 'device', y: ['searches'], agg: 'sum', sort: 'value-asc' });
assert.deepEqual(d.categories, ['desktop', 'mobile']);
// Scatter and bubble keep points.
d = shape(table, { mark: 'bubble', x: 'searches', y: ['buys'], z: 'searches', series: 'device' });
assert.equal(d.points.length, 5);
assert.deepEqual(d.series.map((s) => s.name).sort(), ['desktop', 'mobile']);
// Formats.
assert.equal(fmt(1234567, '$'), '$1.2M');
assert.equal(fmt(0.256, '%'), '25.6%');
assert.equal(fmt(42), '42');

// Every kind draws, hidden series are left out, and the legend toggles.
const c = palette(true);
let toggled = null;
const all = (e, out = []) => { if (e && typeof e === 'object') { out.push(e); for (const k of e.kids ?? []) all(k, out); } return out; };
for (const [mark] of MARKS) {
  const spec = { mark, x: mark === 'scatter' || mark === 'bubble' ? 'searches' : 'day', y: [mark === 'scatter' || mark === 'bubble' ? 'buys' : 'searches'], series: 'device', agg: mark === 'scatter' || mark === 'bubble' ? 'none' : 'sum', z: 'searches', labels: true, legend: 'right' };
  const view = ChartView(c, table, spec, { width: 560, height: 220, onToggle: (name) => { toggled = name; } });
  const els = all(view);
  assert.ok(els.length > 8, `${mark} draws (${els.length} elements)`);
  const text = els.flatMap((e) => (e.kids ?? []).filter((k) => typeof k === 'string')).join(' | ');
  assert.ok(!text.includes('Nothing to draw'), `${mark}: not empty`);
}
const view = ChartView(c, table, { mark: 'bar', x: 'day', y: ['searches'], series: 'device', agg: 'sum', hidden: ['mobile'], legend: 'right' }, { width: 560, onToggle: (n) => { toggled = n; } });
const legend = all(view).find((e) => e.call?.('id')?.[0] === 'legend-mobile');
legend.call('on_click')[0]({}, {});
assert.equal(toggled, 'mobile', 'the legend toggles a series');
const hiddenText = all(view).flatMap((e) => (e.kids ?? []).filter((k) => typeof k === 'string')).join(' | ');
assert.ok(hiddenText.includes('mobile'), 'a hidden series stays in the legend');
console.log('charts test: ok');
