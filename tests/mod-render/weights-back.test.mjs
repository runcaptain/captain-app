// Backpropagation, stepped, on the Weights page: the output's error first, then each layer's nudges walking
// backward; lines show the cost's slope (checked against measured slopes) only for the layers reached; the chain
// rule for a picked weight is three named factors whose product is the measured slope; and Play moves on the
// page's animation clock.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-back.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { measured, fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { walkedFrom, stepsOf, walkWords, chainRows, steepestWeight } = await import('../../defaults/mods/captain-weights/back.js');
const { lineValue, layoutOf, shapesFor } = await import('../../defaults/mods/captain-weights/draw.js');
const { signed, plain } = await import('../../defaults/mods/captain-weights/inspect.js');
const { forward, backward } = await import('../../defaults/mods/captain-weights/net.js');
const { starter, TINY, DIGITS } = await import('../../defaults/mods/captain-weights/starters.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');
const { palette } = await import('../../defaults/mods/captain-weights/kit/tokens.js');

weightsTransport(async () => ({ ok: false, error: 'Not Found' }));
async function page(model) {
  localReset(); liveReset(); fakeFeed(liveTransport);
  globalThis.__props = { model };
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const near = (a, b, tol = 1e-6) => assert.ok(Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b)), `${a} against ${b}`);
const marks = (s, c) => [...s.groups].filter(([k]) => (k.startsWith(c.blue) || k.startsWith(c.fail)) && (k.endsWith('ff') || k.endsWith('99'))).reduce((n, [, v]) => n + v.filter((p) => p.length === 3).length, 0);

test('the walk has a step for the output and one per layer, and reaches one layer further back each step', () => {
  const tiny = starter(TINY), digits = starter(DIGITS);
  assert.deepEqual([stepsOf(tiny), stepsOf(digits)], [3, 4]);
  assert.deepEqual([0, 1, 2, 3].map((s) => walkedFrom(tiny, s)), [null, 2, 1, 0]);
  assert.deepEqual([1, 2, 3, 4].map((s) => walkedFrom(digits, s)), [3, 2, 1, 0]);
});

test('stepping on the tiny network: what is drawn at each step matches the reference', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3'); // "Both on", the one it gets wrong
  const net = starter(TINY), e = net.examples[3], c = palette(false), L = layoutOf(net);
  const run = forward(net, e.x), grad = backward(net, e.x, e.y, run);
  assert.equal(h.byIdPrefix('wt-walk-says').length, 0, 'no walk yet');
  assert.equal(h.mod.drawing().grad, null, 'no gradients until they are asked for');

  await h.click('wt-walk-next');
  let d = h.mod.drawing();
  assert.equal(d.walkedFrom, 2);
  assert.equal(textOf(h, 'wt-walk-says'), `Step 1 of 3. The output's error: the network said ${plain(run.a[2][0])} and 0.00 was wanted, so the cost wants the output dimmer.`);
  // Only the output has its mark, and every line still shows its weight.
  assert.equal(marks(shapesFor(L, d, c), c), 1);
  assert.equal(lineValue(d, 1, 0, 1), -8);
  assert.equal(lineValue(d, 0, 1, 0), 6);

  await h.click('wt-walk-next');
  d = h.mod.drawing();
  assert.equal(d.walkedFrom, 1);
  assert.ok(textOf(h, 'wt-walk-says').startsWith('Step 2 of 3. The nudges reach the weights into the output.'));
  assert.ok(textOf(h, 'wt-walk-says').includes('each mark there is a wish'));
  // The lines into the output now show the nudge the cost wants: the negative of a slope measured by hand.
  for (let i = 0; i < 3; i++) near(lineValue(d, 1, 0, i), -measured(net, e.x, e.y, 1, 0, i));
  assert.equal(lineValue(d, 0, 1, 0), 6, 'the layer not reached still shows its weights');
  assert.ok(marks(shapesFor(L, d, c), c) >= 2, 'the hidden neurons have their wishes');

  await h.click('wt-walk-next');
  d = h.mod.drawing();
  assert.equal(d.walkedFrom, 0);
  assert.ok(textOf(h, 'wt-walk-says').includes('The input cannot be changed, so the walk ends here.'));
  for (let j = 0; j < 3; j++) for (let i = 0; i < 2; i++) near(lineValue(d, 0, j, i), -measured(net, e.x, e.y, 0, j, i));
  // The numbers on the lines are the nudges too.
  assert.ok(h.text().includes(plain(-grad.dW[0][1][0])));
  // Next does nothing more; Previous goes back a layer; the last Previous leaves the walk.
  await h.click('wt-walk-prev');
  assert.equal(h.mod.drawing().walkedFrom, 1);
  await h.click('wt-walk-stop');
  assert.equal(h.mod.drawing().walkedFrom, null);
  assert.equal(h.mod.drawing().grad, null);
  assert.equal(h.byIdPrefix('wt-walk-says').length, 0);
});

test('the chain rule for a picked weight: three named factors, and their product is the measured slope', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3');
  await h.click('wt-walk-next');
  const net = starter(TINY), e = net.examples[3];
  const run = forward(net, e.x), grad = backward(net, e.x, e.y, run);
  // The walk picked the weight with the largest slope into the output, so the block is there from step 1.
  const at = steepestWeight(grad, 1);
  assert.deepEqual(h.mod.selW, at);
  const r = chainRows(net, run, grad, at);
  assert.deepEqual(r.rows.map((x) => x.id), ['before', 'steep', 'behind']);
  assert.equal(r.product, signed(measured(net, e.x, e.y, at.k, at.j, at.i)));
  const text = h.text();
  assert.ok(text.includes('The chain rule for the picked weight'));
  for (const row of r.rows) assert.ok(text.includes(row.label) && text.includes(row.value), row.label);
  assert.ok(text.includes("The cost's slope for this weight (its gradient)") && text.includes(r.product));
  assert.ok(r.rows[0].label.startsWith('How active hidden neuron'));
  assert.ok(r.rows[1].label.startsWith('How steep the squish is at the output'));
  assert.equal(r.rows[2].label, 'How far off the output "Exactly one is on" is');
  // The three numbers really multiply to the slope.
  const [a, b, d] = [run.a[1][at.i], run.a[2][0] * (1 - run.a[2][0]), 2 * (run.a[2][0] - 0)];
  near(a * b * d, measured(net, e.x, e.y, at.k, at.j, at.i));
  // A weight further back: the third factor is everything behind the neuron it feeds.
  await h.click('wt-w-0-1-0');
  const far = chainRows(net, run, grad, { k: 0, j: 1, i: 0 });
  assert.equal(far.rows[2].label, 'How far off hidden neuron 2 is, added up over the 1 path from it to the cost');
  assert.equal(far.product, signed(measured(net, e.x, e.y, 0, 1, 0)));
  assert.ok(h.text().includes(far.rows[2].label));
  // On the digit reader a first-layer weight has 16 x 10 paths behind it.
  const dn = starter(DIGITS), de = dn.examples[2], drun = forward(dn, de.x), dg = backward(dn, de.x, de.y, drun);
  const lit = de.x.findIndex((v) => v > 0.5);
  const big = chainRows(dn, drun, dg, { k: 0, j: 3, i: lit });
  assert.equal(big.paths, 160);
  assert.equal(big.product, signed(measured(dn, de.x, de.y, 0, 3, lit)));
});

test('with no weight picked and no walk, there is no chain rule block', async () => {
  const h = await page(TINY);
  await h.click('wt-n-1-1');
  assert.equal(h.byIdPrefix('wt-chain').length, 0);
  await h.click('wt-mode-nudges');
  await h.click('wt-w-1-0-1');
  assert.ok(h.byIdPrefix('wt-chain').length > 0, 'with nudges showing, a picked weight has it too');
});

test('Play walks by itself on the animation clock and stops at the input', async () => {
  const h = await page(DIGITS);
  await h.click('wt-walk-play');
  assert.equal(h.mod.walk.step, 1);
  assert.equal(h.mod.walk.playing, true);
  assert.ok(h.text().includes('Pause'));
  assert.ok(textOf(h, 'wt-walk-says').includes('was wanted, so the cost wants'), textOf(h, 'wt-walk-says'));
  await h.layoutLists({}); // the harness runs the page's timers a few times
  assert.equal(h.mod.walk.step, 4, 'the last step');
  assert.equal(h.mod.walk.playing, false);
  assert.ok(h.text().includes('Walk the error back again'));
  // Words for many outputs name the answers.
  const net = starter(DIGITS), e = net.examples[0], run = forward(net, e.x);
  assert.match(walkWords(net, run, backward(net, e.x, e.y, run), 1, e.y), /^Step 1 of 4\. The output's error: the network said "\d" and "0" was wanted, so the cost wants "0" brighter and the others dimmer\./);
});
