// A neuron, opened, on the Weights page: its numbers against the reference network (fixtures/captain-weights/
// reference.mjs), its one sentence, and with nudges showing, its desired nudge and each weight's gradient against
// a measured slope.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-inspect.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { refForward, measured } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { neuronFacts, neuronSentence, weightSentence, signed, plain } = await import('../../defaults/mods/captain-weights/inspect.js');
const { forward, backward } = await import('../../defaults/mods/captain-weights/net.js');
const { starter: tinyStarter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { DIGITS, digitsNet, digitsRoutes } = await import('./fixtures/captain-weights/digits.mjs');
const starter = (id) => (id === DIGITS ? digitsNet() : tinyStarter(id));

weightsTransport(async (path) => digitsRoutes(path) ?? { ok: false, error: 'Not Found' });
async function page(model) {
  localReset();
  globalThis.__props = { model };
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const near = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b)), `${a} against ${b}`);

test('the tiny network: hidden neuron 2 on "Both on", number by number', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3');
  await h.click('wt-n-1-1');
  const net = starter(TINY), x = net.examples[3].x;
  // Worked by hand: 6 x 1 + 6 x 1 - 12 = 0, and the sigmoid of 0 is a half.
  assert.equal(textOf(h, 'wt-inspect-name'), 'Hidden neuron 2');
  assert.equal(textOf(h, 'wt-inspect-z'), '0.00');
  assert.equal(textOf(h, 'wt-inspect-a'), '0.50');
  assert.equal(textOf(h, 'wt-inspect-b'), '-12.00');
  const text = h.text();
  assert.ok(text.includes('Weights coming in (2)') && text.includes('Weights going out (1)'), text);
  assert.ok(text.includes('x 1.00 = +6.00'), 'each incoming weight shows what it multiplies and adds');
  assert.ok(text.includes('-8.00'), 'the outgoing weight, with its sign');
  assert.equal(textOf(h, 'wt-inspect-says'),
    'Its largest weight is positive (+6.00, from First input): when First input is bright, this neuron lights up. Its bias is -12.00: the weights must add up to more than 12.00 before it turns on.');
  // The same numbers from the facts, against the reference's own forward pass.
  const f = neuronFacts(net, forward(net, x), null, { k: 1, j: 1 });
  near(f.sum, 0); near(f.activation, 0.5);
  near(forward(net, x).a[2][0], refForward(net, x)[0]);
  assert.equal(f.nudge, null, 'no nudge until gradients are showing');
  // The open neuron's lines are the ones at full strength: the others are dimmed.
  assert.deepEqual(h.mod.drawing().sel, { k: 1, j: 1 });
});

test('with nudges showing: the desired nudge, and each weight\'s gradient equals a measured slope', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3');
  await h.click('wt-n-1-1');
  assert.equal(h.byIdPrefix('wt-inspect-nudge').length, 0);
  await h.click('wt-mode-nudges');
  const net = starter(TINY), e = net.examples[3];
  const g = backward(net, e.x, e.y);
  const f = neuronFacts(net, forward(net, e.x), g, { k: 1, j: 1 });
  f.incoming.forEach((r) => near(r.gradient, measured(net, e.x, e.y, 0, 1, r.i), 1e-6));
  near(f.outgoing[0].gradient, measured(net, e.x, e.y, 1, 0, 1), 1e-6);
  near(f.biasGradient, measured(net, e.x, e.y, 0, 1, null), 1e-6);
  // "Both on" comes out too high, and hidden 2 pulls the output down (-8): the cost wants hidden 2 brighter.
  assert.ok(f.nudge > 0, `${f.nudge}`);
  assert.equal(textOf(h, 'wt-inspect-nudge'), `For this input the cost wants this neuron brighter (desired nudge ${signed(f.nudge)}).`);
  const text = h.text();
  for (const r of f.incoming) assert.ok(text.includes(signed(r.gradient)), `the gradient ${signed(r.gradient)} is shown`);
  assert.ok(text.includes(`The cost's slope for the bias: ${signed(f.biasGradient)}`));
  // The output neuron wants to be dimmer, and says so.
  await h.click('wt-n-2-0');
  assert.ok(textOf(h, 'wt-inspect-nudge').includes('wants this neuron dimmer'));
  // Back to the weights: the gradients go away.
  await h.click('wt-mode-values');
  assert.equal(h.byIdPrefix('wt-inspect-nudge').length, 0);
});

test('picking a weight says what it does in one sentence', async () => {
  const h = await page(TINY);
  await h.click('wt-w-1-0-1');
  assert.equal(textOf(h, 'wt-inspect-says'), 'This weight is large and negative (-8.00): when hidden neuron 2 is bright, the output "Exactly one is on" goes dark.');
  assert.equal(textOf(h, 'wt-inspect-name'), 'The output "Exactly one is on"', 'the neuron the weight feeds is open');
  const net = starter(TINY);
  assert.equal(weightSentence(net, { k: 0, j: 0, i: 0 }), 'This weight is large and positive (+6.00): when First input is bright, hidden neuron 1 lights up.');
  // An input has no weights of its own.
  await h.click('wt-n-0-0');
  assert.ok(textOf(h, 'wt-inspect-says').startsWith('This is First input.'));
  assert.ok(h.text().includes('Weights going out (3)'));
});

test('the digit reader: a first-column neuron lists its strongest 8 of 784 and counts the rest', async () => {
  const h = await page(DIGITS);
  await h.click('wt-in-8');
  await h.click('wt-n-1-4');
  const text = h.text();
  assert.ok(text.includes('Weights coming in (784)'), text);
  assert.ok(text.includes('and 776 more, smaller for this input'));
  assert.ok(/the pixel in row \d+, column \d+/.test(text), 'a pixel is named by its row and column');
  assert.equal(h.byIdPrefix('wt-in-w-4-').length, 8);
  assert.ok(text.includes('Weights going out (16)'));
  const net = starter(DIGITS), x = net.examples[8].x, run = forward(net, x);
  assert.equal(textOf(h, 'wt-inspect-a'), plain(run.a[1][4]));
  assert.equal(textOf(h, 'wt-inspect-z'), plain(run.z[1][4]));
  // The rows are the ones that add most for THIS input: all from lit pixels.
  const f = neuronFacts(net, run, null, { k: 1, j: 4 });
  const top = f.incoming.slice().sort((p, q) => Math.abs(q.adds) - Math.abs(p.adds)).slice(0, 8);
  assert.ok(top.every((r) => r.a > 0), 'lit pixels');
  // The picture's label promises nothing about what it looks like.
  assert.ok(text.includes('What neuron 5 responds to: its 784 weights'));
  const label = h.byIdPrefix('wt-heat').length && text.split(' | ').find((t) => t.startsWith('What neuron 5 responds to'));
  assert.equal(label, 'What neuron 5 responds to: its 784 weights', 'no word about what the picture looks like');
});

test('numbers read the same everywhere: a sign, two places, four for small ones', () => {
  assert.deepEqual([signed(6), signed(-8), signed(0.0312), signed(0), signed(-0.00001)], ['+6.00', '-8.00', '+0.0312', '0.00', '0.00']);
  assert.deepEqual([plain(0.5), plain(-12), plain(0.0312)], ['0.50', '-12.00', '0.0312']);
  const net = starter(TINY);
  assert.ok(neuronSentence(net, neuronFacts(net, null, null, { k: 1, j: 2 })).includes('from Second input'), 'hidden 3 leans on the second input (+3.00)');
});
