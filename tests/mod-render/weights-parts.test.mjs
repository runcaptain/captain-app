// A trained model on the Weights page: a "model of parts" (a small transformer with tree rankers, the services'
// kind "session-ranker"), read and never edited or run. The list says its kind in words, it opens as a diagram of
// blocks, a block opens to a sentence, a picture of its matrix, its largest rows and (for a table) the rows most
// like one, and the rankers open to inputs by share of gain, the unused inputs and one tree. Against a made-up
// model answered in the services' own shapes (fixtures/captain-weights/parts.mjs): every name and number in it is
// invented.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-parts.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness, shapesIn } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';
import { PARTS, SESSION, partsModel, partsRoutes, matrix } from './fixtures/captain-weights/parts.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport, partsFrom, isParts } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');
const { partsLayout, treeLayout, reduceGrid, splitWords, leafWords, HEAT_MAX } = await import('../../defaults/mods/captain-weights/partsdraw.js');
const { partSentence, partsFacts, gainWords, windowOf, largestRows, READ_ONLY, WINDOW } = await import('../../defaults/mods/captain-weights/parts.js');
const { MAX_SHAPES } = await import('../../defaults/mods/captain-weights/draw.js');

let asked = [], opts = {}, feed;
weightsTransport(async (path, o) => { asked.push([path, o?.method ?? 'GET']); return partsRoutes(path, opts) ?? { ok: false, error: 'Not Found' }; });
async function page(model, size = null, o = {}) {
  localReset(); liveReset(); feed = fakeFeed(liveTransport); asked = []; opts = o;
  globalThis.__props = model ? { model } : {}; globalThis.__files = {}; globalThis.__toasts = [];
  const h = harness(Weights);
  await h.start();
  if (size) { h.mod.width = size[0]; h.mod.height = size[1]; await h.idle(1); }
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const one = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id);
const D = partsModel(), M = partsFrom(D);
const S = `/api/weights/sessions/${SESSION}`;

test('the services\' description becomes the page\'s parts, with plain names', () => {
  assert.ok(isParts(D) && isParts({ kind: 'session-ranker' }) && !isParts({ kind: 'dense', sizes: [2, 3, 1] }) && !isParts(null));
  assert.equal(M.kind_words, 'Session model with rankers');
  assert.equal(M.source, 'Brought in from /models/demo/v1');
  assert.equal(M.parts.reduce((n, p) => n + p.parameters, 0), D.parameters, 'every number belongs to one part');
  const attn = M.parts.find((p) => p.id === 'L0.self_attn');
  assert.deepEqual(attn.tensors.map((t) => t.name), ['Query-key', 'Output-value', 'Query', 'Key', 'Value', 'Output']);
  assert.deepEqual([attn.shape, attn.block, attn.heads, attn.name], [[8, 8], 1, 2, 'Attention, layer 1']);
  assert.deepEqual(attn.tensors[3].how, { tensor: 'L0.self_attn.in_proj_weight', rows: [8, 16] }, 'the key matrix is rows 8 to 16 of the stacked tensor');
  assert.deepEqual(M.parts.find((p) => p.id === 'L0.ff').tensors.map((t) => t.name), ['Into the hidden step', 'Hidden step biases', 'Out of the hidden step', 'Output biases']);
  assert.deepEqual(M.parts.find((p) => p.id === 'ln').tensors.map((t) => t.name), ['Scale', 'Shift']);
  assert.deepEqual(M.parts.find((p) => p.id === 'ln').shape, [1, 8], 'a list of numbers is one row');
  assert.deepEqual(M.rankers.map((r) => r.id), ['full', 'quick']);
  // Every kind of part has its sentence, with plain words beside the technical one.
  for (const p of M.parts) assert.ok(partSentence(p).length > 30 && !/undefined|NaN|null/.test(partSentence(p)), `${p.id}: ${partSentence(p)}`);
  assert.equal(partSentence(M.parts[1]), 'A lookup table (an embedding): one row of 8 numbers for each of 4 values. It holds what kind of event it was (a guess from its name). Rows that are alike stand for things the model treats alike.');
  // A model whose files do not say how many heads: no per-head matrices are offered, and the sentence says so.
  const unsure = partsFrom(partsModel({ heads: null })).parts.find((p) => p.id === 'L0.self_attn');
  assert.deepEqual(unsure.tensors.map((t) => t.id), ['query', 'key', 'value', 'output']);
  assert.ok(partSentence(unsure).endsWith('The files do not say how many heads it has.'));
});

test('the list says what a trained model is, in words, and it opens with where it came from', async () => {
  const h = await page(null);
  assert.equal(partsFacts(M), `Session model with rankers, ${D.parameters.toLocaleString('en-US')} numbers`);
  assert.equal(textOf(h, `wt-open-kind-${PARTS}`), partsFacts(M));
  assert.equal(textOf(h, `wt-open-says-${PARTS}`), 'A session model, width 8, 2 layers, 2 heads, with 2 tree rankers.');
  await h.click(`wt-open-btn-${PARTS}`);
  assert.equal(h.mod.kind, 'parts');
  assert.equal(textOf(h, 'wt-facts'), partsFacts(M));
  assert.equal(textOf(h, 'wt-source'), 'Brought in from /models/demo/v1');
});

test('it opens as blocks left to right, read only: nothing is written, edited, run or deployed', async () => {
  const h = await page(PARTS, [1280, 800]);
  for (const p of M.parts) assert.equal(one(h, `wp-part-${p.id}`).length, 1, p.id);
  const text = h.text();
  assert.ok(text.includes('Item table') && text.includes('12 by 8') && text.includes('96 numbers'), 'a table with its shape and count');
  assert.ok(text.includes('Head 1') && text.includes('Head 2'), 'an attention block shows its heads');
  assert.ok(text.includes('Layer 1') && text.includes('Layer 2') && text.includes('Lookup tables') && text.includes('Head'));
  assert.equal(h.byIdPrefix('wp-n-').length + h.byIdPrefix('wt-n-').length, 0, 'no neuron circles');
  // The layout: tables stacked in the first column and sized by their rows, the rest to the right in order.
  const L = partsLayout(M), box = (id) => L.boxes.find((b) => b.part.id === id);
  assert.equal(box('item').x, box('act').x);
  assert.ok(box('item').h > box('pos').h && box('pos').h > box('act').h, 'more rows, taller');
  const order = ['item', 'text', 'ln', 'L0.self_attn', 'L0.norm1', 'L0.ff', 'L0.norm2', 'L1.self_attn', 'L1.ff', 'buy'];
  for (let n = 1; n < order.length; n++) assert.ok(box(order[n]).x > box(order[n - 1]).x, `${order[n]} is right of ${order[n - 1]}`);
  assert.equal(L.lines.filter((l) => l.to.part.id === 'ln').length, 4, 'a line from everything the first norm reads');
  for (const a of L.boxes) for (const b of L.boxes) if (a !== b) assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y, `${a.part.id} and ${b.part.id} do not overlap`);
  // A candidate side is a second row under the session side.
  const two = partsLayout({ parts: [...M.parts, { ...M.parts[0], id: 'c_item', side: 'candidate' }, { ...M.parts[3], id: 'c_text', side: 'candidate', feeds_from: [] }] });
  assert.ok(two.boxes.find((b) => b.part.id === 'c_item').y > Math.max(...two.boxes.filter((b) => b.part.side !== 'candidate').map((b) => b.y + b.h)));
  assert.ok(two.captions.some((c) => c.text === 'Candidate side: Lookup tables'));
  // Read only, and said once.
  assert.equal(textOf(h, 'wt-read-only'), READ_ONLY);
  assert.equal(text.split(READ_ONLY).length - 1, 1, 'said once');
  for (const id of ['wt-hand', 'wt-walk', 'wt-modes', 'wt-inputs', 'wt-deploy-go', 'wt-ask', 'wt-cost', 'wt-band']) assert.equal(h.byIdPrefix(id).length, 0, `${id} is hidden`);
  assert.deepEqual(asked.map(([p, m]) => `${m} ${p}`), [`GET /api/weights/models/${PARTS}`], 'opening asks for the description and nothing else');
  assert.ok(textOf(h, 'wt-part-wiring').startsWith('The order of the parts is the usual one'), 'how sure the wiring is, in the services\' words');
  assert.ok(shapesIn(h.mod.render(h.cx)) < MAX_SHAPES);
});

test('a block opens to a sentence, a picture of its matrix and its largest rows, read as a screen\'s look', async () => {
  const h = await page(PARTS, [1280, 800]);
  await h.click('wp-part-L0.self_attn');
  assert.equal(textOf(h, 'wt-part-name'), 'Attention, layer 1');
  assert.equal(textOf(h, 'wt-part-says'), 'Attention: which earlier events this one looks at. 2 heads, each looking for something of its own.');
  assert.equal(textOf(h, 'wt-part-shape'), '8 by 8');
  assert.deepEqual(asked.slice(1).map(([p]) => p), [`/api/weights/models/${PARTS}/session`, `${S}/attention?layer=0&head=0&matrix=query_key&watch=1`]);
  assert.ok(textOf(h, 'wt-part-tensor-says').startsWith('Query-key: how strongly'));
  assert.equal(one(h, 'wt-part-heat').length, 1);
  assert.ok(textOf(h, 'wt-part-heat-says').includes('blue is positive, red is negative') && !textOf(h, 'wt-part-heat-says').includes('reduced'));
  // The largest rows, worked out from the numbers on screen: largest first.
  const want = largestRows(matrix('L0/query_key/0', 8, 8));
  assert.deepEqual(h.mod.pm.rows, want);
  assert.ok(want.every((r, n) => n === 0 || r.norm <= want[n - 1].norm) && Math.abs(want[0].norm - Math.hypot(...matrix('L0/query_key/0', 8, 8)[want[0].row])) < 1e-12);
  assert.equal(one(h, `wt-part-row-${want[0].row}`).length, 1);
  // The second head, from its box in the drawing; then the output-value matrix.
  await h.click('wp-part-L0.self_attn-head-1');
  assert.equal(asked.at(-1)[0], `${S}/attention?layer=0&head=1&matrix=query_key&watch=1`);
  await h.click('wt-part-tensor-ov');
  assert.equal(asked.at(-1)[0], `${S}/attention?layer=0&head=1&matrix=output_value&watch=1`);
  assert.ok(textOf(h, 'wt-part-tensor-says').startsWith('Output-value: what is written'));
  // The key matrix is its rows of the stacked tensor, and has no head to pick.
  await h.click('wt-part-tensor-key');
  assert.equal(asked.at(-1)[0], `${S}/heatmap?tensor=L0.self_attn.in_proj_weight&rows=8%3A16&watch=1`);
  assert.equal(h.byIdPrefix('wt-part-head-').length, 0);
  assert.ok(h.text().includes('The numbers as a picture (8 by 8)'));
  // The session is asked for once. Every read of numbers is a screen's look, and nothing is written.
  assert.equal(asked.filter(([p]) => p.endsWith('/session')).length, 1);
  assert.ok(asked.filter(([p]) => p.startsWith(S)).every(([p]) => p.endsWith('watch=1')), 'every read of numbers is marked as the screen\'s');
  assert.ok(asked.every(([, m]) => m === 'GET'));
  // A click on the open block closes it.
  await h.click('wp-part-L0.self_attn');
  assert.equal(h.mod.pm.sel, null);
  // A part with a list of numbers draws it as one row.
  await h.click('wp-part-ln');
  assert.ok(h.text().includes('The numbers as a picture (1 by 8)'));
});

test('an embedding table has a row picker and the rows most like the row', async () => {
  const h = await page(PARTS, [1280, 800]);
  await h.click('wp-part-item');
  assert.equal(textOf(h, 'wt-part-row-at'), 'Row 1 of 12: Blue kettle');
  assert.ok(asked.some(([p]) => p === `${S}/heatmap?tensor=item.weight&watch=1`) && asked.some(([p]) => p === `${S}/nearest?tensor=item.weight&row=0&top=8&watch=1`));
  const near = partsRoutes(`${S}/nearest?tensor=item.weight&row=0&top=8`).data.nearest;
  assert.ok(h.text().includes('Rows most like this one') && h.text().includes(near[0].label));
  assert.equal(h.byIdPrefix('wt-part-near-').filter((e) => /^wt-part-near-\d+$/.test(e.call('id')[0])).length, near.length);
  assert.equal(h.byIdPrefix('wt-part-heat-mark').length, 1, 'the picked row is outlined in the picture');
  await h.click('wt-part-row-next');
  assert.equal(textOf(h, 'wt-part-row-at'), 'Row 2 of 12: Red kettle');
  // A row in either list picks it.
  const to = partsRoutes(`${S}/nearest?tensor=item.weight&row=1&top=8`).data.nearest[0];
  await h.click(`wt-part-near-${to.row}`);
  assert.equal(textOf(h, 'wt-part-row-at'), `Row ${to.row + 1} of 12: ${to.label}`);
  await h.click(`wt-part-row-${h.mod.pm.rows[0].row}`);
  assert.equal(h.mod.pm.row, h.mod.pm.rows[0].row);
});

test('a table too tall to draw whole is read a window at a time, drawn reduced, and says both', async () => {
  assert.equal(windowOf([12, 8], 3), null);
  assert.deepEqual(windowOf([1000, 8], 0), [0, WINDOW]);
  assert.deepEqual(windowOf([1000, 8], 500), [372, 628]);
  assert.deepEqual(windowOf([1000, 8], 999), [744, 1000]);
  assert.deepEqual(windowOf([300, 4000], 0), [0, 50], 'never more numbers than the services send at once');
  const big = matrix('big', 96, 96), g = reduceGrid(big);
  assert.deepEqual([g.rows, g.cols, g.by, g.reduced], [HEAT_MAX, HEAT_MAX, [3, 3], true]);
  assert.ok(Math.abs(g.values[0][0] - [0, 1, 2].flatMap((r) => [0, 1, 2].map((q) => big[r][q])).reduce((a, b) => a + b, 0) / 9) < 1e-12, 'a cell is the average of the numbers it stands for');
  assert.equal(reduceGrid(matrix('small', 8, 8)).reduced, false);
  const h = await page(PARTS, [1280, 800], { tall: true });
  await h.click('wp-part-item');
  assert.equal(asked.find(([p]) => p.includes('/heatmap'))[0], `${S}/heatmap?tensor=item.weight&rows=0%3A256&watch=1`);
  assert.ok(textOf(h, 'wt-part-heat-says').endsWith('Rows 1 to 256 of 1,000 are drawn, around the picked row. Shown reduced: 256 by 8 drawn as 32 by 8, each cell an average.'), textOf(h, 'wt-part-heat-says'));
  assert.ok(h.text().includes('The numbers as a picture (1,000 by 8)') && h.text().includes('Largest rows of those drawn'));
  assert.equal(textOf(h, 'wt-part-row-at'), 'Row 1 of 1,000');
  h.mod.pm.row = 600;
  await h.idle(1);
  await h.click('wt-part-row-next');
  assert.equal(asked.filter(([p]) => p.includes('/heatmap')).at(-1)[0], `${S}/heatmap?tensor=item.weight&rows=473%3A729&watch=1`);
  assert.equal(h.byIdPrefix('wt-part-heat-mark').length, 1);
  assert.ok(shapesIn(h.mod.render(h.cx)) < MAX_SHAPES, `${shapesIn(h.mod.render(h.cx))} shapes`);
});

test('the rankers: inputs by share of gain, the unused ones said plainly, and one tree drawn', async () => {
  const h = await page(PARTS, [1280, 800]);
  assert.ok(!asked.some(([p]) => p.includes('/trees')), 'a ranker is read when it is opened');
  await h.click('submenu-rankers');
  assert.equal(asked.at(-1)[0], `/api/weights/models/${PARTS}/trees?ranker=full&trees=3`);
  assert.equal(textOf(h, 'wt-ranker-name'), 'Ranker full');
  assert.equal(textOf(h, 'wt-ranker-unused'), 'Never used by this ranker: 1 input (hour of day). Changing it cannot change its answer.');
  assert.equal(gainWords({ gain_share: 0.46, splits: 31 }), '46% of gain, 31 splits');
  assert.deepEqual([0, 1, 2, 3].map((n) => h.byIdPrefix(`wt-ranker-input-${n}`)[0].kids[0].kids[0]), ['price', 'days since last visit', 'items seen', 'hour of day'], 'largest share first');
  // The tree: its splits as sentences, its leaves as what they add, yes and no on its lines.
  const text = h.text();
  for (const words of ['price above 12.5', 'items seen above 3', 'days since last visit above 7', 'adds +0.21', 'adds -0.05', 'yes', 'no']) assert.ok(text.includes(words), words);
  assert.equal(splitWords({ input: 'price', threshold: 12.5 }), 'price above 12.5');
  assert.equal(leafWords(-0.18), 'adds -0.18');
  assert.equal(h.byIdPrefix('wr-node-').length, 7);
  // At or below the threshold goes left: "above" is yes, to the right, as on a number line.
  const nodes = h.mod.pm.rankers.full.shown[0].nodes, L = treeLayout(nodes), at = (id) => L.boxes.find((b) => b.node.id === id);
  const root = nodes[0], below = nodes[root.no], above = nodes[root.yes];
  assert.deepEqual([root.input, below.input, above.input], ['price', 'items seen', 'days since last visit']);
  assert.ok(at(root.id).y < at(below.id).y && at(below.id).x < at(above.id).x, 'the root on top, no to the left, yes to the right');
  assert.equal(nodes[above.yes].leaf, -0.18);
  assert.deepEqual(L.lines.filter((l) => l.from.node.id === root.id).map((l) => [l.says, l.to.node.id]), [['no', below.id], ['yes', above.id]]);
  for (const a of L.boxes) for (const b of L.boxes) if (a !== b && a.y === b.y) assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x, 'no two boxes overlap');
  assert.equal(textOf(h, 'wt-ranker-tree-at'), 'Tree 1 of the first 3 is drawn (the ranker has 40).');
  await h.click('wt-ranker-tree-next');
  assert.ok(h.text().includes('price above 30'));
  await h.click('wt-ranker-tree-next');
  assert.ok(h.text().includes('shop section outside the set it lists'), 'a split on a category is not given a threshold it does not have');
  // The other ranker is read when picked, once, and uses every input.
  await h.click('wt-ranker-quick');
  assert.equal(asked.at(-1)[0], `/api/weights/models/${PARTS}/trees?ranker=quick&trees=3`);
  assert.equal(textOf(h, 'wt-ranker-unused'), 'Every input is used by this ranker.');
  assert.ok(h.text().includes('model score above 0.5'));
  await h.click('wt-ranker-full');
  assert.equal(asked.filter(([p]) => p.includes('/trees')).length, 2, 'not read twice');
  await h.click('submenu-model');
  assert.ok(h.byIdPrefix('wp-part-').length > 0);
});

test('beside a terminal the open part takes the drawing\'s place, with a way back', async () => {
  const h = await page(PARTS, [600, 760]);
  assert.equal(h.byIdPrefix('wt-side').length, 0, 'nothing open: the drawing has the room');
  await h.click('wp-part-text');
  assert.equal(h.byIdPrefix('wp-canvas').length, 0);
  assert.equal(textOf(h, 'wt-part-name'), 'Text projection');
  await h.click('wt-side-close');
  assert.equal(h.byIdPrefix('wp-canvas').length, 1);
  // The rankers there: the inputs over the tree.
  await h.click('submenu-rankers');
  assert.ok(h.byIdPrefix('wt-ranker-unused').length === 1 && h.byIdPrefix('wr-canvas').length === 1);
});

test('a refusal is said in plain words, and the model is read again only when the change feed says so', async () => {
  const h = await page(PARTS, [1280, 800]);
  const before = asked.filter(([p]) => p === `/api/weights/models/${PARTS}`).length;
  await h.idle(60);
  assert.equal(asked.filter(([p]) => p === `/api/weights/models/${PARTS}`).length, before, 'no re-read while nothing changes');
  feed.tell(`weights:${PARTS}`);
  await h.idle(700);
  assert.ok(asked.filter(([p]) => p === `/api/weights/models/${PARTS}`).length > before, 'read again on the topic');
  // A part whose numbers cannot be read.
  h.mod.pm.model.parts.push({ ...h.mod.pm.model.parts[0], id: 'gone', name: 'Gone table', tensors: [{ id: 'gone.weight', name: 'The table', how: { tensor: 'gone.weight' } }] });
  h.mod.partsLayout = partsLayout(h.mod.pm.model);
  await h.idle(1);
  await h.click('wp-part-gone');
  assert.equal(textOf(h, 'wt-part-problem'), 'There is no tensor named gone.weight.');
});
