// The kit's lineage graph against a made-up graph: levels around the focus within the chosen depth, edges only among
// shown nodes, how many are left out each way, focusing a node by clicking it, and a column's lineage lighting up.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/lineage.test.mjs
import assert from 'node:assert/strict';
import { levels, LineageGraph } from '../../defaults/captain/kit/lineage.js';
import { palette } from '../../defaults/captain/kit/tokens.js';

const graph = {
  nodes: [
    { id: 't:orders', kind: 'table', label: 'warehouse.orders' },
    { id: 't:events', kind: 'table', label: 'warehouse.events' },
    { id: 'd:catalog', kind: 'dataset', label: 'catalog', version: 3, columns: ['id', 'title', 'price'] },
    { id: 'd:queries', kind: 'dataset', label: 'queries', version: 1 },
    { id: 'p:ranker', kind: 'pipeline', label: 'ranker', version: 2 },
    { id: 'm:shop-v2', kind: 'model', label: 'shop v2' },
    { id: 'b:ab', kind: 'backtest', label: 'tuned-vs-current' },
    { id: 'h:prod', kind: 'handoff', label: 'production' },
  ],
  edges: [
    { from: 't:orders', to: 'd:catalog' }, { from: 't:events', to: 'd:queries' },
    { from: 'd:catalog', to: 'p:ranker' }, { from: 'm:shop-v2', to: 'p:ranker' },
    { src: 'p:ranker', dst: 'b:ab' }, { from: 'd:queries', to: 'b:ab' }, { from: 'b:ab', to: 'h:prod' },
  ],
};

const l = levels(graph, 'p:ranker', 2, 1);
assert.equal(l.get('p:ranker'), 0);
assert.equal(l.get('d:catalog'), -1);
assert.equal(l.get('t:orders'), -2);
assert.equal(l.get('b:ab'), 1);
assert.ok(!l.has('h:prod'), 'beyond one step downstream');
assert.ok(!l.has('d:queries'), 'not upstream of the focus');

let focused = null, traced = null;
const g = new LineageGraph({ id: 'lin', onFocus: (id) => { focused = id; }, onColumn: (col) => { traced = col; } });
g.set(graph, 'd:catalog');
g.up = 1; g.down = 1;
let L = g.layout();
assert.deepEqual([...L.pos.keys()].sort(), ['d:catalog', 'p:ranker', 't:orders'].sort(), 'one step each way');
assert.equal(L.edges.length, 2, 'edges among shown nodes only');
assert.deepEqual(L.hidden, { up: 0, down: 2 }, 'two further downstream');
g.down = 3; g.cache = null;
L = g.layout();
assert.ok(L.pos.has('h:prod') && L.hidden.down === 0, 'deeper shows the handoff');
const xs = ['t:orders', 'd:catalog', 'p:ranker', 'b:ab', 'h:prod'].map((id) => L.pos.get(id).x);
assert.deepEqual([...xs].sort((a, b) => a - b), xs, 'upstream to downstream, left to right');

// Render: every shown node, focus by click, trace a column.
const c = palette(true);
const tree = g.render(c);
const all = [];
const walk = (e) => { if (!e || typeof e !== 'object') return; all.push(e); for (const k of e.kids ?? []) walk(k); };
walk(tree);
const text = all.flatMap((e) => (e.kids ?? []).filter((k) => typeof k === 'string')).join(' | ');
for (const n of ['warehouse.orders', 'catalog', 'ranker', 'tuned-vs-current', 'production']) assert.ok(text.includes(n), `node ${n}`);
assert.ok(text.includes('Upstream') && text.includes('Downstream') && text.includes('Trace a column'), 'controls');
const node = all.find((e) => e.call?.('id')?.[0] === 'lin-node-p:ranker');
node.call('on_click')[0]({}, { notify() {} });
assert.equal(focused, 'p:ranker', 'click focuses');
const col = all.find((e) => e.call?.('id')?.[0] === 'lin-col-price');
col.call('on_click')[0]({}, { notify() {} });
assert.equal(traced, 'price', 'a column is traced');
g.setColumns([{ from: { node: 't:orders', column: 'amount' }, to: { node: 'd:catalog', column: 'price' } }]);
assert.equal(g.columnAt('t:orders'), 'amount');
console.log('lineage test: ok');
