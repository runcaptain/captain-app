// A trained model on the Weights page: a "model of parts" (a small transformer with tree rankers), read and never
// edited or run. The list says its kind in words, it opens as a diagram of blocks, a block opens to a sentence, a
// picture of its matrix, its largest rows and (for a table) the rows most like one, and the rankers open to
// inputs by share of gain, the unused inputs and one tree. Against a made-up model (fixtures/captain-weights/
// parts.mjs): every name and number in it is invented.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-parts.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness, shapesIn } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';
import { PARTS, partsModel, partsRoutes, matrix } from './fixtures/captain-weights/parts.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');
const { partsLayout, treeLayout, reduceGrid, splitWords, leafWords, HEAT_MAX } = await import('../../defaults/mods/captain-weights/partsdraw.js');
const { partSentence, partsFacts, gainWords, READ_ONLY } = await import('../../defaults/mods/captain-weights/parts.js');
const { MAX_SHAPES } = await import('../../defaults/mods/captain-weights/draw.js');

let asked = [], wide = false, feed;
weightsTransport(async (path, opts) => { asked.push([path, opts?.method ?? 'GET']); return partsRoutes(path, { wide }) ?? { ok: false, error: 'Not Found' }; });
async function page(model, size = null) {
  localReset(); liveReset(); feed = fakeFeed(liveTransport); asked = []; wide = false;
  globalThis.__props = model ? { model } : {}; globalThis.__files = {}; globalThis.__toasts = [];
  const h = harness(Weights);
  await h.start();
  if (size) { h.mod.width = size[0]; h.mod.height = size[1]; await h.idle(1); }
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const M = partsModel();

test('the list says what a trained model is, in words, and where it came from', async () => {
  const h = await page(null);
  assert.equal(partsFacts(M), `Session model with rankers, ${M.parameters.toLocaleString('en-US')} numbers`);
  assert.equal(textOf(h, `wt-open-kind-${PARTS}`), partsFacts(M));
  assert.equal(textOf(h, `wt-open-source-${PARTS}`), 'Imported from a made-up training run.');
  await h.click(`wt-open-btn-${PARTS}`);
  assert.equal(h.mod.kind, 'parts');
  assert.equal(textOf(h, 'wt-title'), 'Demo session model');
  assert.equal(textOf(h, 'wt-facts'), partsFacts(M));
  assert.equal(textOf(h, 'wt-source'), 'Imported from a made-up training run.');
});

test('it opens as blocks left to right, read only: no session, no editing, no running, no deploy', async () => {
  const h = await page(PARTS, [1280, 800]);
  // One block per part, each with its shape and its count.
  for (const p of M.parts) assert.equal(h.byIdPrefix(`wp-part-${p.id}`).filter((e) => e.call('id')[0] === `wp-part-${p.id}`).length, 1, p.id);
  const text = h.text();
  assert.ok(text.includes('Item table') && text.includes('12 by 8') && text.includes('96 numbers'), 'a table with its shape and count');
  assert.ok(text.includes('Head 1') && text.includes('Head 2'), 'an attention block shows its heads');
  assert.ok(text.includes('Block 1') && text.includes('Block 2') && text.includes('Lookup tables') && text.includes('Heads'));
  assert.equal(h.byIdPrefix('wp-n-').length + h.byIdPrefix('wt-n-').length, 0, 'no neuron circles');
  // The layout: tables stacked in the first column and sized by their rows, the rest to the right in order.
  const L = partsLayout(M), box = (id) => L.boxes.find((b) => b.part.id === id);
  assert.equal(box('emb-item').x, box('emb-kind').x);
  assert.ok(box('emb-item').h > box('emb-pos').h && box('emb-pos').h > box('emb-kind').h, 'more rows, taller');
  const order = ['emb-item', 'proj', 'attn-1', 'norm-1a', 'ff-1', 'norm-1b', 'attn-2', 'ff-2', 'head-next'];
  for (let n = 1; n < order.length; n++) assert.ok(box(order[n]).x > box(order[n - 1]).x, `${order[n]} is right of ${order[n - 1]}`);
  assert.equal(box('head-next').x, box('head-buy').x, 'the heads share the last column');
  assert.equal(L.lines.filter((l) => l.to.part.id === 'attn-1').length, 4, 'a line from everything attention reads');
  // Read only, and said once.
  assert.equal(textOf(h, 'wt-read-only'), READ_ONLY);
  assert.equal(text.split(READ_ONLY).length - 1, 1, 'said once');
  for (const id of ['wt-hand', 'wt-walk', 'wt-modes', 'wt-inputs', 'wt-deploy-go', 'wt-ask', 'wt-cost', 'wt-band']) assert.equal(h.byIdPrefix(id).length, 0, `${id} is hidden`);
  assert.ok(asked.every(([, m]) => m === 'GET'), 'nothing is written');
  assert.ok(!asked.some(([p]) => /\/session|\/sessions/.test(p)), 'no session is opened on it');
  assert.ok(shapesIn(h.mod.render(h.cx)) < MAX_SHAPES);
});

test('a block opens to a sentence, a picture of its matrix and its largest rows', async () => {
  const h = await page(PARTS, [1280, 800]);
  assert.ok(h.text().includes('Open a part'));
  await h.click('wp-part-attn-1');
  assert.equal(textOf(h, 'wt-part-name'), 'Attention 1');
  assert.equal(textOf(h, 'wt-part-says'), 'Attention: which earlier events this one looks at. 2 heads, each looking for something of its own.');
  assert.equal(textOf(h, 'wt-part-shape'), '8 by 8');
  assert.ok(asked.some(([p]) => p === `/api/weights/models/${PARTS}/parts/attn-1/heatmap?tensor=qk&head=0&max=64`), JSON.stringify(asked));
  assert.ok(textOf(h, 'wt-part-tensor-says').startsWith('Query-key: how strongly'));
  assert.equal(h.byIdPrefix('wt-part-heat').filter((e) => e.call('id')[0] === 'wt-part-heat').length, 1);
  assert.ok(textOf(h, 'wt-part-heat-says').includes('blue is positive, red is negative') && !textOf(h, 'wt-part-heat-says').includes('reduced'));
  // The largest rows, largest first, as the services sent them.
  const rows = partsRoutes(`/api/weights/models/${PARTS}/parts/attn-1/rows?tensor=qk&head=0&top=8`).data;
  assert.equal(h.byIdPrefix('wt-part-row-').filter((e) => /^wt-part-row-\d+$/.test(e.call('id')[0])).length, rows.length);
  assert.ok(h.text().includes(`Row ${rows[0].row + 1}`));
  // The second head, from its box in the drawing; then the output-value matrix.
  await h.click('wp-part-attn-1-head-1');
  assert.equal(h.mod.pm.head, 1);
  assert.ok(asked.some(([p]) => p.includes('/parts/attn-1/heatmap?tensor=qk&head=1')));
  await h.click('wt-part-tensor-ov');
  assert.ok(asked.some(([p]) => p.includes('/parts/attn-1/heatmap?tensor=ov&head=1')));
  assert.ok(textOf(h, 'wt-part-tensor-says').startsWith('Output-value: what is written'));
  // A matrix that is not per head has no head to pick.
  await h.click('wt-part-tensor-query');
  assert.equal(h.byIdPrefix('wt-part-head-').length, 0);
  assert.ok(asked.at(-1)[0].includes('tensor=query') && !asked.at(-1)[0].includes('head='));
  // Every kind of part has its sentence, with plain words beside the technical one.
  for (const p of M.parts) assert.ok(partSentence(p).length > 30 && !/undefined|NaN/.test(partSentence(p)), p.id);
  assert.ok(partSentence(M.parts[0]).startsWith('A lookup table (an embedding): one row of 8 numbers for each of 12 items.'));
  // A click on the open block closes it.
  await h.click('wp-part-attn-1');
  assert.equal(h.mod.pm.sel, null);
});

test('an embedding table has a row picker and the rows most like the row', async () => {
  const h = await page(PARTS, [1280, 800]);
  await h.click('wp-part-emb-item');
  assert.equal(textOf(h, 'wt-part-row-at'), 'Row 1 of 12: Blue kettle');
  const near = partsRoutes(`/api/weights/models/${PARTS}/parts/emb-item/nearest?row=0&top=8`).data.nearest;
  assert.ok(h.text().includes('Rows most like this one') && h.text().includes(near[0].name));
  assert.equal(h.byIdPrefix('wt-part-near-').filter((e) => /^wt-part-near-\d+$/.test(e.call('id')[0])).length, near.length);
  assert.equal(h.byIdPrefix('wt-part-heat-mark').length, 1, 'the picked row is outlined in the picture');
  await h.click('wt-part-row-next');
  assert.equal(textOf(h, 'wt-part-row-at'), 'Row 2 of 12: Red kettle');
  assert.ok(asked.some(([p]) => p.includes('/parts/emb-item/nearest?row=1')));
  // A row in either list picks it.
  await h.click(`wt-part-near-${partsRoutes(`/api/weights/models/${PARTS}/parts/emb-item/nearest?row=1&top=8`).data.nearest[0].row}`);
  assert.notEqual(h.mod.pm.row, 1);
  assert.ok(!h.byIdPrefix('wt-part-row-prev')[0].call('on_click') === (h.mod.pm.row === 0));
});

test('a large matrix is drawn reduced, says so, and stays under the shape ceiling', async () => {
  const big = matrix('big', 96, 96), g = reduceGrid(big);
  assert.deepEqual([g.rows, g.cols, g.by, g.reduced], [HEAT_MAX, HEAT_MAX, [3, 3], true]);
  assert.ok(Math.abs(g.values[0][0] - (big[0][0] + big[0][1] + big[0][2] + big[1][0] + big[1][1] + big[1][2] + big[2][0] + big[2][1] + big[2][2]) / 9) < 1e-12, 'a cell is the average of the numbers it stands for');
  assert.equal(reduceGrid(matrix('small', 8, 8)).reduced, false);
  const h = await page(PARTS, [1280, 800]);
  wide = true;
  await h.click('wp-part-ff-1');
  assert.ok(textOf(h, 'wt-part-heat-says').endsWith('Shown reduced: 96 by 96 drawn as 32 by 32, each cell an average.'), textOf(h, 'wt-part-heat-says'));
  assert.ok(h.text().includes('The numbers as a picture (96 by 96)'));
  assert.ok(shapesIn(h.mod.render(h.cx)) < MAX_SHAPES, `${shapesIn(h.mod.render(h.cx))} shapes`);
});

test('the rankers: inputs by share of gain, the unused ones said plainly, and one tree drawn', async () => {
  const h = await page(PARTS, [1280, 800]);
  await h.click('submenu-rankers');
  assert.equal(textOf(h, 'wt-ranker-name'), 'First ranker');
  assert.equal(textOf(h, 'wt-ranker-unused'), 'Never used by this ranker: 1 input (hour of day).');
  assert.equal(gainWords(M.rankers[0].inputs[0]), '46% of gain, 31 splits');
  const bars = h.byIdPrefix('wt-ranker-input-').map((e) => e.kids);
  assert.equal(bars.length, 4);
  assert.deepEqual([0, 1, 2, 3].map((n) => h.byIdPrefix(`wt-ranker-input-${n}`)[0].kids[0].kids[0]), ['price', 'days since last visit', 'items seen', 'hour of day'], 'largest share first');
  // The tree: its splits as sentences, its leaves as what they add, yes and no on its lines.
  const text = h.text();
  for (const words of ['price above 12.5', 'items seen above 3', 'days since last visit above 7', 'adds +0.21', 'adds -0.05', 'yes', 'no']) assert.ok(text.includes(words), words);
  assert.equal(splitWords({ input: 'price', threshold: 12.5 }), 'price above 12.5');
  assert.equal(leafWords(-0.18), 'adds -0.18');
  assert.equal(h.byIdPrefix('wr-node-').length, 7);
  const L = treeLayout(M.rankers[0].shown[0].nodes), at = (id) => L.boxes.find((b) => b.node.id === id);
  assert.ok(at(0).y < at(1).y && at(1).y < at(3).y, 'the root on top');
  assert.ok(at(1).x < at(2).x && at(3).x < at(4).x && at(4).x < at(5).x, 'yes to the left, no to the right, leaves in order');
  for (const a of L.boxes) for (const b of L.boxes) if (a !== b && a.y === b.y) assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x, 'no two boxes overlap');
  assert.equal(textOf(h, 'wt-ranker-tree-at'), 'Tree 1 of the first 2 is drawn (the ranker has 40).');
  await h.click('wt-ranker-tree-next');
  assert.ok(h.text().includes('price above 30'));
  // The other ranker uses every input.
  await h.click('wt-ranker-rank-second');
  assert.equal(textOf(h, 'wt-ranker-unused'), 'Every input is used by this ranker.');
  assert.ok(h.text().includes('model score above 0.5'));
  await h.click('submenu-model');
  assert.equal(h.byIdPrefix('wp-part-').length > 0, true);
});

test('beside a terminal the open part takes the drawing\'s place, with a way back', async () => {
  const h = await page(PARTS, [600, 760]);
  assert.equal(h.byIdPrefix('wt-side').length, 0, 'nothing open: the drawing has the room');
  await h.click('wp-part-proj');
  assert.equal(h.byIdPrefix('wp-canvas').length, 0);
  assert.equal(textOf(h, 'wt-part-name'), 'Text projection');
  await h.click('wt-side-close');
  assert.equal(h.byIdPrefix('wp-canvas').length, 1);
  // The rankers there: the inputs over the tree.
  await h.click('submenu-rankers');
  assert.ok(h.byIdPrefix('wt-ranker-unused').length === 1 && h.byIdPrefix('wr-canvas').length === 1);
});

test('the model is read again when the change feed says it changed, and never on a clock', async () => {
  const h = await page(PARTS, [1280, 800]);
  const before = asked.filter(([p]) => p === `/api/weights/models/${PARTS}`).length;
  await h.idle(60);
  assert.equal(asked.filter(([p]) => p === `/api/weights/models/${PARTS}`).length, before, 'no re-read while nothing changes');
  feed.tell(`weights:${PARTS}`);
  await h.idle(700);
  assert.ok(asked.filter(([p]) => p === `/api/weights/models/${PARTS}`).length > before, 'read again on the topic');
});
