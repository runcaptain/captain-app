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
const ncx = { notify() {}, stop_propagation() {} };
node.call('on_click')[0]({}, ncx);
assert.equal(focused, null, 'a click selects, it does not refocus');
assert.equal(g.sel, 'p:ranker', 'a click selects');
const find = (tree, id) => { const out = []; const w = (e) => { if (!e || typeof e !== 'object') return; out.push(e); for (const k of e.kids ?? []) w(k); }; w(tree); return out.find((e) => e.call?.('id')?.[0] === id); };
const words = (tree) => { const out = []; const w = (e) => { if (!e || typeof e !== 'object') return; for (const k of e.kids ?? []) typeof k === 'string' ? out.push(k) : w(k); }; w(tree); return out.join(' | '); };
let t2 = g.render(c);
const det = find(t2, 'lin-details');
assert.ok(det, 'its details show');
assert.ok(words(det).includes('Made from (2)') && words(det).includes('catalog') && words(det).includes('Feeds (1)'), words(det));
// Only the path through the selection lights up: the selected node's chain, not its siblings.
const P = g.pathOf(g.layout().edges, 'p:ranker');
assert.ok(P.up.has('d:catalog') && P.up.has('t:orders') && P.down.has('h:prod'), 'upstream and downstream of it');
// "Show its lineage" refocuses.
find(t2, 'lin-details-focus').call('on_click')[0]({}, ncx);
assert.equal(focused, 'p:ranker', 'Show its lineage focuses');
// Drag a node: press, move, release. It moves by the drag over the zoom, and the release is not a click.
const before = { ...g.at(g.layout(), 'd:catalog') };
find(t2, 'lin-node-d:catalog').call('on_mouse_down')[1]({ position: { x: 100, y: 100 }, click_count: 1 }, ncx);
const canvas = find(t2, 'lin-canvas');
canvas.call('on_mouse_move')[0]({ position: { x: 160, y: 130 }, bounds: { x: 0, y: 0, width: 800, height: 300 } }, ncx);
canvas.call('on_mouse_up')[1]({}, ncx);
assert.deepEqual(g.at(g.layout(), 'd:catalog'), { x: before.x + 60, y: before.y + 30 }, 'the node moved with the drag');
find(g.render(c), 'lin-node-d:catalog').call('on_click')[0]({}, ncx);
assert.equal(g.sel, 'p:ranker', 'the drag did not select');
// Pan the background, then zoom with Command held, and Fit.
canvas.call('on_mouse_down')[1]({ position: { x: 10, y: 10 }, bounds: { x: 0, y: 0, width: 800, height: 300 } }, ncx);
canvas.call('on_mouse_move')[0]({ position: { x: 50, y: 20 } }, ncx);
canvas.call('on_mouse_up')[1]({}, ncx);
assert.deepEqual(g.pan, { x: 40, y: 10 }, 'panned');
canvas.call('on_scroll_wheel')[0]({ delta: { x: 0, y: -100 }, modifiers: { platform: true }, local_position: { x: 0, y: 0 } }, ncx);
assert.ok(g.zoom < 1, 'zoomed');
canvas.call('on_scroll_wheel')[0]({ delta: { x: 0, y: 30 }, modifiers: {} }, ncx);
find(g.render(c), 'lin-zoom-fit').call('on_click')[0]({}, ncx);
assert.equal(g.moved.size, 0, 'Fit puts dragged nodes back');
const col = all.find((e) => e.call?.('id')?.[0] === 'lin-col-price');
col.call('on_click')[0]({}, { notify() {} });
assert.equal(traced, 'price', 'a column is traced');
g.setColumns([{ from: { node: 't:orders', column: 'amount' }, to: { node: 'd:catalog', column: 'price' } }]);
assert.equal(g.columnAt('t:orders'), 'amount');
console.log('lineage test: ok');

// No focus (a project's whole lineage): columns by longest path from the roots.
const { ranks } = await import('../../defaults/captain/kit/lineage.js');
const r = ranks(graph);
assert.equal(r.get('t:orders'), 0);
assert.equal(r.get('d:catalog'), 1);
assert.equal(r.get('p:ranker'), 2, 'after its longest input (catalog), not its model');
assert.equal(r.get('h:prod'), 4);
const whole = new LineageGraph({ id: 'all' });
whole.set(graph, null);
assert.equal(whole.layout().pos.size, graph.nodes.length, 'every node shown');
// MLflow feeds a downstream pipeline from the side: it's placed one column before what it feeds, run before model.
{
  const g = { nodes: [{ id: 'd', kind: 'dataset' }, { id: 'p', kind: 'pipeline' }, { id: 'm', kind: 'mlflow-model' }, { id: 'r', kind: 'mlflow-run' }, { id: 'x', kind: 'dataset' }],
    edges: [{ src: 'd', dst: 'p' }, { src: 'm', dst: 'p' }, { src: 'r', dst: 'm' }, { src: 'x', dst: 'p' }] };
  const lv = levels(g, 'd', 2, 2);
  assert.deepEqual([lv.get('p'), lv.get('m'), lv.get('r')], [1, 0, -1], 'MLflow beside the pipeline it feeds');
  assert.ok(!lv.has('x'), 'other side inputs stay out');
}
console.log('lineage ranks: ok');
