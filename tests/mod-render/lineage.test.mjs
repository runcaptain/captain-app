// The kit's lineage graph ("Where this came from") against a made-up graph: a layered layout left to right, the
// whole chain by default, arrows with arrowheads drawn as paths (never Unicode), everything upstream and downstream
// of a box lighting up on hover, a click opening a box that has a page, columns untangled so arrows cross less,
// dragging, panning, zooming, and a column's lineage lighting up.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/lineage.test.mjs
import assert from 'node:assert/strict';
import { levels, LineageGraph, untangle, crossings, KINDS } from '../../defaults/captain/kit/lineage.js';
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
const opened = [];
// Tables and evaluations have a page to open; a model version does not.
const g = new LineageGraph({ id: 'lin', onFocus: (id) => { focused = id; }, onColumn: (col) => { traced = col; },
  onOpen: (node) => { if (['dataset', 'table', 'backtest'].includes(node.kind)) { opened.push(node.id); return true; } return false; } });
g.set(graph, 'd:catalog');
// The whole chain by default: everything upstream and downstream of the focus, nothing left out.
{
  const W = g.layout();
  assert.deepEqual([...W.pos.keys()].sort(), ['b:ab', 'd:catalog', 'h:prod', 'm:shop-v2', 'p:ranker', 't:orders'].filter((id) => id !== 'm:shop-v2').sort(), 'the chain from its source to the last thing that used it');
  assert.deepEqual(W.hidden, { up: 0, down: 0 }, 'nothing is left out');
}
g.up = 1; g.down = 1; g.cache = null;
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
assert.ok(text.includes('Trace a column'), 'a column can be traced');
assert.ok(!text.includes('Upstream') && !text.includes('Downstream'), 'no depth steppers: the whole chain shows');
assert.ok(text.includes('Hover a box to see what feeds it and what it feeds. Click one to open it.'), 'what to do, in one line');
// The words on the boxes are the ones the screens use.
assert.deepEqual([KINDS.table[0], KINDS.dataset[0], KINDS.pipeline[0], KINDS.model[0], KINDS.backtest[0], KINDS.environment[0]], ['Database table', 'Table', 'Model version', 'Model', 'Evaluation', 'Deployment']);
for (const w of ['Database table', 'Table', 'Model version', 'Evaluation']) assert.ok(text.includes(w), `the legend says ${w}`);

// Arrows are drawn: one stroked path of curves and one filled path of arrowheads, a head for every edge shown.
// Never a Unicode arrow.
const paths = all.filter((e) => e.kind === 'path').map((e) => e.args[0]);
const strokes = paths.filter((p) => p.kind === 'stroke'), fills = paths.filter((p) => p.kind === 'fill');
const shown = g.layout().edges.length;
assert.equal(strokes.reduce((n, p) => n + p.ops.filter(([m]) => m === 'cubic_bezier_to').length, 0), shown, 'a curve for every edge');
assert.equal(fills.reduce((n, p) => n + p.ops.filter(([m]) => m === 'add_polygon').length, 0), shown, 'an arrowhead for every edge');
{
  // A head is a triangle whose point is on the left edge of the box the arrow points to, at mid height.
  const L0 = g.layout();
  const [from, to] = L0.edges[0];
  const tip = g.head(g.at(L0, to))[0];
  assert.deepEqual(tip, [g.at(L0, to).x, g.at(L0, to).y + 26], `the head of ${from} to ${to} touches ${to}`);
  assert.equal(g.head(g.at(L0, to)).length, 3, 'a triangle');
}
assert.ok(!/[←-⇿⟰-⟿➔-➾]/.test(text), 'no Unicode arrows');

// Hover: everything upstream and downstream of the box lights up, and the rest dims.
const ncx = { notify() {}, stop_propagation() {} };
const byId = (tree, id) => { const out = []; const w = (e) => { if (!e || typeof e !== 'object') return; out.push(e); for (const k of e.kids ?? []) w(k); }; w(tree); return out.find((e) => e.call?.('id')?.[0] === id); };
all.find((e) => e.call?.('id')?.[0] === 'lin-node-d:queries')?.call('on_hover')?.[0]?.(true, ncx);
byId(tree, 'lin-node-p:ranker').call('on_hover')[0](true, ncx);
assert.equal(g.hover, 'p:ranker');
{
  const hovered = g.render(c);
  const dim = (id) => byId(hovered, `lin-node-${id}`).call('opacity') !== undefined;
  assert.ok(!dim('d:catalog') && !dim('t:orders') && !dim('b:ab') && !dim('h:prod') && !dim('p:ranker'), 'its whole chain stays lit');
  const lit = []; (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'path' && e.args[0].kind === 'stroke' && e.args[0].width === 2.5) lit.push(e.args[0]); for (const k of e.kids ?? []) w(k); })(hovered);
  assert.equal(lit.reduce((n, p) => n + p.ops.filter(([m]) => m === 'cubic_bezier_to').length, 0), 4, 'the four arrows on its chain are the lit ones');
  // A box off the chain dims: focus the queries set, whose chain does not include the catalog's source.
  const side = new LineageGraph({ id: 'side' });
  side.set(graph, null);
  const st = side.render(c);
  byId(st, 'side-node-t:events').call('on_hover')[0](true, ncx);
  const st2 = side.render(c);
  assert.ok(byId(st2, 'side-node-t:orders').call('opacity') !== undefined, 'a box that neither feeds it nor is fed by it dims');
  assert.ok(byId(st2, 'side-node-d:queries').call('opacity') === undefined && byId(st2, 'side-node-b:ab').call('opacity') === undefined, 'what it feeds stays lit');
  byId(st2, 'side-node-t:events').call('on_hover')[0](false, ncx);
  assert.equal(side.hover, null, 'leaving the box clears it');
}
byId(g.render(c), 'lin-node-p:ranker').call('on_hover')[0](false, ncx);

// Click: a box with a page opens it. One without shows its details instead.
byId(g.render(c), 'lin-node-b:ab').call('on_click')[0]({}, ncx);
assert.deepEqual(opened, ['b:ab'], 'the evaluation opens');
assert.equal(g.sel, null, 'and it is not selected');
const node = all.find((e) => e.call?.('id')?.[0] === 'lin-node-p:ranker');
node.call('on_click')[0]({}, ncx);
assert.deepEqual(opened, ['b:ab'], 'a model version has no page');
assert.equal(focused, null, 'a click does not refocus');
assert.equal(g.sel, 'p:ranker', 'so its details show');
const find = (tree, id) => { const out = []; const w = (e) => { if (!e || typeof e !== 'object') return; out.push(e); for (const k of e.kids ?? []) w(k); }; w(tree); return out.find((e) => e.call?.('id')?.[0] === id); };
const words = (tree) => { const out = []; const w = (e) => { if (!e || typeof e !== 'object') return; for (const k of e.kids ?? []) typeof k === 'string' ? out.push(k) : w(k); }; w(tree); return out.join(' | '); };
let t2 = g.render(c);
const det = find(t2, 'lin-details');
assert.ok(det, 'its details show');
assert.ok(words(det).includes('Made from (2)') && words(det).includes('catalog') && words(det).includes('Feeds (1)'), words(det));
// Only the path through the selection lights up: the selected node's chain, not its siblings.
const P = g.pathOf(g.layout().edges, 'p:ranker');
assert.ok(P.up.has('d:catalog') && P.up.has('t:orders') && P.down.has('h:prod'), 'upstream and downstream of it');
// "Put this in the middle" refocuses.
find(t2, 'lin-details-focus').call('on_click')[0]({}, ncx);
assert.equal(focused, 'p:ranker', 'it refocuses');
// Drag a node: press, move, release. It moves by the drag over the zoom, and the release is not a click.
const before = { ...g.at(g.layout(), 'd:catalog') };
find(t2, 'lin-node-d:catalog').call('on_mouse_down')[1]({ position: { x: 100, y: 100 }, click_count: 1 }, ncx);
const canvas = find(t2, 'lin-canvas');
canvas.call('on_mouse_move')[0]({ position: { x: 160, y: 130 }, bounds: { x: 0, y: 0, width: 800, height: 300 } }, ncx);
canvas.call('on_mouse_up')[1]({}, ncx);
assert.deepEqual(g.at(g.layout(), 'd:catalog'), { x: before.x + 60, y: before.y + 30 }, 'the node moved with the drag');
find(g.render(c), 'lin-node-d:catalog').call('on_click')[0]({}, ncx);
assert.equal(g.sel, 'p:ranker', 'the drag did not select');
assert.deepEqual(opened, ['b:ab'], 'nor open anything');
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

// Columns are untangled: a box sits near the boxes it is joined to, so arrows cross less than in name order.
{
  const cols = [['a1', 'a2', 'a3'], ['b1', 'b2', 'b3'], ['c1', 'c2']];
  const edges = [['a1', 'b3'], ['a2', 'b2'], ['a3', 'b1'], ['b3', 'c1'], ['b1', 'c2']];
  const before = crossings(cols, edges);
  const after = crossings(untangle(cols, edges), edges);
  assert.equal(before, 4, 'in name order the arrows cross four times');
  assert.equal(after, 0, 'untangled, none cross');
  assert.deepEqual(untangle(cols, edges).map((c) => [...c].sort()), cols.map((c) => [...c].sort()), 'the same boxes in the same columns');
  // Nothing to untangle stays as it was, so the same graph always draws the same.
  assert.deepEqual(untangle([['x', 'y'], ['z']], []), [['x', 'y'], ['z']]);
}
// The canvas takes the height it is given, so a long chain has room.
{
  const tall = new LineageGraph({ id: 'tall' });
  tall.set(graph, null);
  const find2 = (tree, id) => { const out = []; const w = (e) => { if (!e || typeof e !== 'object') return; out.push(e); for (const k of e.kids ?? []) w(k); }; w(tree); return out.find((e) => e.call?.('id')?.[0] === id); };
  assert.equal(find2(tall.render(palette(false), { width: 900, height: 640 }), 'tall-canvas').call('h')[0], 640);
}
console.log('lineage graph: ok');
