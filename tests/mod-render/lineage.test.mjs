// The kit's lineage graph ("Where this came from") against a made-up graph: a layered layout left to right, the
// whole chain by default, arrows with arrowheads drawn as paths (never Unicode), everything upstream and downstream
// of a box lighting up on hover, a click opening a box that has a page, columns untangled so arrows cross less,
// dragging, panning, zooming, and a column's lineage lighting up.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/lineage.test.mjs
import assert from 'node:assert/strict';
import { levels, LineageGraph, untangle, crossings, KINDS } from '../../defaults/captain/kit/lineage.js';
import { SHAPES_PER_DRAW } from './harness.mjs';
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
const strokes = paths.filter((p) => p.kind === 'stroke');
// Arrowheads are triangles; the dotted grid is a path of small squares.
const fills = paths.filter((p) => p.kind === 'fill' && p.ops.every(([, a]) => a[0].length === 3));
const shown = g.layout().edges.length;
assert.equal(strokes.reduce((n, p) => n + p.ops.filter(([m]) => m === 'cubic_bezier_to').length, 0), shown, 'a curve for every edge');
assert.equal(fills.reduce((n, p) => n + p.ops.filter(([m]) => m === 'add_polygon').length, 0), shown, 'an arrowhead for every edge');
{
  // A head is a triangle whose point is on the left edge of the box the arrow points to, at mid height.
  const L0 = g.layout();
  const [from, to] = L0.edges[0];
  const tip = g.head(g.at(L0, to))[0];
  assert.deepEqual(tip, [g.at(L0, to).x - 4, g.at(L0, to).y + 26], `the head of ${from} to ${to} ends on the dot at ${to}'s left edge`);
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
// The canvas is as tall as the graph needs at the size it is drawn, within the room it is given: a short chain
// gets a short canvas, not a tall empty box, and a tall graph gets all the room there is.
{
  const find2 = (tree, id) => { const out = []; const w = (e) => { if (!e || typeof e !== 'object') return; out.push(e); for (const k of e.kids ?? []) w(k); }; w(tree); return out.find((e) => e.call?.('id')?.[0] === id); };
  const short = new LineageGraph({ id: 'tall' });
  short.set(graph, null);
  const hShort = find2(short.render(palette(false), { width: 900, height: 640 }), 'tall-canvas').call('h')[0];
  assert.ok(hShort >= 220 && hShort < 640, `a short chain: ${hShort}`);
  const many = { nodes: [{ id: 'a', kind: 'dataset' }, ...Array.from({ length: 14 }, (_x, i) => ({ id: `e${i}`, kind: 'backtest-run' }))], edges: Array.from({ length: 14 }, (_x, i) => ({ from: 'a', to: `e${i}` })) };
  const tall = new LineageGraph({ id: 'tall' });
  tall.set(many, 'a');
  assert.equal(find2(tall.render(palette(false), { width: 900, height: 640 }), 'tall-canvas').call('h')[0], 640, 'a tall graph takes the room it is given');
}

// Fit fills the canvas. A small graph grows until a box's title is the size of body text (never past 125%), is
// never shrunk below 100%, and starts at the left, at mid height. A big one shrinks to fit and is centred.
{
  const small = new LineageGraph({ id: 'fit' });
  small.set({ nodes: [{ id: 'a', kind: 'dataset' }, { id: 'b', kind: 'pipeline' }], edges: [{ from: 'a', to: 'b' }] }, 'a');
  small.render(palette(false), { width: 1200, height: 600 });
  const L = small.layout();
  assert.equal(small.zoom, 1.25, 'a small graph is enlarged, to 125% at most');
  assert.ok(12.5 * small.zoom >= 13, 'a title is at least the size of body text');
  assert.equal(Math.round(small.at(L, 'a').x * small.zoom + small.pan.x), 24, 'it starts at the left margin, not floating in the centre');
  const H = small.view.h, mid = (small.at(L, 'a').y + 26) * small.zoom + small.pan.y;
  assert.ok(Math.abs(mid - H / 2) < 2, 'at mid height');
  const wide = { nodes: Array.from({ length: 9 }, (_x, i) => ({ id: `n${i}`, kind: 'dataset' })), edges: Array.from({ length: 8 }, (_x, i) => ({ from: `n${i}`, to: `n${i + 1}` })) };
  const big = new LineageGraph({ id: 'fit' });
  big.set(wide, 'n0');
  big.up = 9; big.down = 9; big.cache = null;
  big.render(palette(false), { width: 900, height: 600 });
  const LB = big.layout();
  assert.ok(big.zoom < 1 && big.zoom >= 0.3, `a wide graph shrinks: ${big.zoom}`);
  const left = big.at(LB, 'n0').x * big.zoom + big.pan.x, right = (big.at(LB, [...LB.pos.keys()].at(-1)).x + 188) * big.zoom + big.pan.x;
  assert.ok(left >= 23 && right <= 900 - 23, `inside the canvas with a margin: ${left} to ${right}`);
}

// A connector that skips a column runs through a gap between the boxes there, never across one.
{
  const g = { nodes: ['a', 'b1', 'b2', 'b3', 'c'].map((id) => ({ id, kind: 'dataset' })),
    edges: [['a', 'b1'], ['a', 'b2'], ['a', 'b3'], ['b1', 'c'], ['b2', 'c'], ['b3', 'c'], ['a', 'c']].map(([from, to]) => ({ from, to })) };
  const lg = new LineageGraph({ id: 'lane' });
  lg.set(g, 'a');
  const L = lg.layout();
  const lane = L.routes.get('a\u0000c');
  assert.equal(lane.length, 1, 'one lane, in the column it skips');
  const boxes = ['b1', 'b2', 'b3'].map((id) => L.pos.get(id));
  assert.equal(lane[0].x, boxes[0].x, 'in the middle column');
  for (const b of boxes) assert.ok(lane[0].y <= b.y - 3 || lane[0].y >= b.y + 52 + 3, `the lane at ${lane[0].y} clears the box at ${b.y}`);
  assert.deepEqual(L.columns.map((col) => col.length), [1, 3, 1], 'lanes are not boxes');
  assert.equal(L.pos.size, 5);
  // Drawn: the skipping connector bends into the lane, runs straight across the column, and bends out.
  const tree = lg.render(palette(false), { width: 900, height: 500 });
  const strokes = []; (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'path' && e.args?.[0]?.kind === 'stroke') strokes.push(e.args[0]); for (const k of e.kids ?? []) w(k); })(tree);
  const ops = strokes.flatMap((p) => p.ops.map(([m]) => m));
  assert.equal(ops.filter((m) => m === 'line_to').length, 1, 'one straight run, through the lane');
  assert.equal(ops.filter((m) => m === 'cubic_bezier_to').length, 8, 'six one-column curves, and two for the one that skips');
}

// The untangler tries more than three sweeps and keeps the best order it saw.
{
  const cols = [['a', 'b', 'c', 'd'], ['w', 'x', 'y', 'z'], ['p', 'q', 'r', 's']];
  const edges = [['a', 'z'], ['b', 'y'], ['c', 'x'], ['d', 'w'], ['w', 's'], ['x', 'r'], ['y', 'q'], ['z', 'p'], ['a', 'w'], ['d', 'z']];
  const out = untangle(cols, edges);
  assert.ok(crossings(out, edges) <= crossings(cols, edges), 'never worse than it started');
  assert.ok(crossings(out, edges) <= 4, `few crossings: ${crossings(out, edges)}`);
}

// A draw never builds thousands of shapes. That is how the dotted grid broke the graph in a real window: the app
// stops a draw that takes too long, and the tests could not see it. Every painted path in one draw is counted.
{
  const shapes = (tree) => { let n = 0; (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'path') n += e.args?.[0]?.ops?.length ?? 0; for (const k of e.kids ?? []) w(k); })(tree); return n; };
  const nodes = Array.from({ length: 60 }, (_x, i) => ({ id: `n${i}`, kind: i % 3 ? 'backtest-run' : 'dataset' }));
  const edges = nodes.slice(1).flatMap((n, i) => [{ from: `n${Math.floor(i / 3)}`, to: n.id }]);
  const lg = new LineageGraph({ id: 'heavy' });
  lg.set({ nodes, edges }, 'n0');
  lg.up = 9; lg.down = 9; lg.cache = null;
  for (const [w, h, zoom] of [[1300, 800, null], [2400, 1400, null], [1300, 800, 0.45], [1300, 800, 2]]) {
    if (zoom) { lg.zoom = zoom; lg.touched = true; }
    const n = shapes(lg.render(palette(false), { width: w, height: h }));
    assert.ok(n > 0 && n <= SHAPES_PER_DRAW, `${n} shapes in one draw at ${w}x${h}, zoom ${lg.zoom.toFixed(2)}: the ceiling is ${SHAPES_PER_DRAW}`);
  }
}
console.log('lineage graph: ok');

// The graph is fitted to the canvas it really has. A screen first draws at a guessed width and learns its real one
// a moment later: the graph fits again, so its last column is never off the edge. Once the person pans or zooms,
// the view is theirs and stays.
{
  const g2 = new LineageGraph({ id: 'fit' });
  g2.set(graph, null);
  const c2 = palette(true);
  g2.render(c2, { width: 1400, height: 500 });
  const wide = g2.zoom;
  g2.render(c2, { width: 600, height: 500 });
  const L2 = g2.layout();
  assert.ok(g2.zoom < wide, `narrower canvas, smaller graph: ${wide} then ${g2.zoom}`);
  assert.ok(L2.width * g2.zoom <= 600, 'every column is inside the canvas');
  g2.touched = true;
  const mine = g2.zoom;
  g2.render(c2, { width: 1200, height: 500 });
  assert.equal(g2.zoom, mine, 'after the person moved it, a resize leaves their view alone');
}
console.log('lineage fit: ok');

// A node graph, in the way people know one: rounded boxes with an icon, a dot where a connector joins a box, and
// a dotted grid behind them that moves with the pan and spreads with the zoom.
{
  const g3 = new LineageGraph({ id: 'ng' });
  g3.set(graph, null);
  const c3 = palette(true);
  const tree3 = g3.render(c3, { width: 900, height: 500 });
  const all3 = []; (function w(e) { if (!e || typeof e !== 'object') return; all3.push(e); for (const k of e.kids ?? []) w(k); })(tree3);
  const idOf = (e) => String(e.call?.('id')?.[0] ?? '');
  const box = all3.find((e) => idOf(e) === 'ng-node-p:ranker');
  assert.ok(box.call('rounded')?.[0] > 0, 'boxes are rounded');
  const icons = []; (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'svg') icons.push(e); for (const k of e.kids ?? []) w(k); })(box);
  assert.equal(icons.length, 1, 'an icon on the box');
  // A dot on the right edge of every box something reads from, and on the left edge of every box made from something.
  const L3 = g3.layout();
  const outs = new Set(L3.edges.map(([a]) => a)), ins = new Set(L3.edges.map(([, b]) => b));
  assert.equal(all3.filter((e) => idOf(e).startsWith('ng-dot-out-')).length, outs.size);
  assert.equal(all3.filter((e) => idOf(e).startsWith('ng-dot-in-')).length, ins.size);
  assert.ok(all3.find((e) => idOf(e) === 'ng-dot-in-p:ranker').call('rounded_full') !== undefined, 'the dots are round');
  assert.equal(all3.filter((e) => idOf(e) === 'ng-dot-in-t:orders').length, 0, 'a source has nothing joining it from the left');
  // The grid: one path, on the canvas, of small squares at the grid's spacing.
  const grid = all3.find((e) => idOf(e) === 'ng-grid');
  assert.ok(grid, 'a dotted grid');
  const gp = g3.grid(220, 110);
  assert.equal(gp.kind, 'fill');
  // A dot every 22 units, scaled by the zoom: the count depends on where the pan puts the first one.
  const nx = 220 / (22 * g3.zoom), ny = 110 / (22 * g3.zoom);
  assert.ok(gp.ops.length >= Math.floor(nx) * Math.floor(ny) && gp.ops.length <= Math.ceil(nx) * Math.ceil(ny), `a dot every 22 units, scaled by the zoom: ${gp.ops.length}`);
  const first = gp.ops[0][1][0][0];
  g3.pan = { x: g3.pan.x + 5, y: g3.pan.y };
  assert.notDeepEqual(g3.grid(220, 110).ops[0][1][0][0], first, 'the grid moves with the pan');
  // A big canvas never builds more than 500 dots (thousands took longer than a frame is allowed), and the same
  // canvas and offset give back the path already built.
  const big = g3.grid(1300, 800);
  assert.ok(big.ops.length <= 500 && big.ops.length > 100, `a capped grid: ${big.ops.length} dots`);
  assert.equal(g3.grid(1300, 800), big, 'not rebuilt when nothing moved');
  g3.zoom = 0.45;
  assert.ok(g3.grid(1300, 800).ops.length <= 500, 'zoomed out too');
  g3.zoom = 0.3;
  assert.equal(g3.grid(220, 110), null, 'zoomed far out, the dots would touch: no grid');
}
console.log('lineage node graph: ok');

// Every connector runs left to right: a box that reads from one the same distance from the focus is pushed a
// column past it, never left beside it. A loop in the records doesn't hang the layout.
{
  const g = { nodes: ['c', 'q', 'j', 'm', 'e'].map((id) => ({ id, kind: 'dataset' })),
    edges: [['c', 'q'], ['c', 'm'], ['q', 'j'], ['q', 'e'], ['m', 'e'], ['j', 'e']].map(([from, to]) => ({ from, to })) };
  const L = levels(g, 'c', 9, 9);
  assert.deepEqual(['c', 'q', 'm', 'j', 'e'].map((id) => L.get(id)), [0, 1, 1, 2, 3], 'the evaluation sits past the judgments it reads');
  for (const e of g.edges) assert.ok(L.get(e.to) > L.get(e.from), `${e.from} to ${e.to} runs left to right`);
  const up = levels({ nodes: g.nodes, edges: [['a', 'b'], ['b', 'f'], ['a', 'f']].map(([from, to]) => ({ from, to })) }, 'f', 9, 9);
  assert.deepEqual(['a', 'b', 'f'].map((id) => up.get(id)), [-2, -1, 0], 'upstream too');
  const loop = levels({ nodes: g.nodes, edges: [['c', 'q'], ['q', 'j'], ['j', 'q']].map(([from, to]) => ({ from, to })) }, 'c', 9, 9);
  assert.ok(loop.get('q') > 0 && loop.get('j') > 0, 'a loop still lays out');
}
console.log('lineage columns: ok');
