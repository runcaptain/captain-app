// The Weights page's arithmetic (defaults/mods/captain-weights/net.js) against a reference written apart from it
// (fixtures/captain-weights/reference.mjs): the forward pass, the cost, and every gradient of backpropagation
// against a slope measured by nudging that one number. On the tiny 2-3-1 network every number is checked; on the
// 784-16-16-10 digit reader a spread of them.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-net.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { forward, backward, costOf, costOver, chain, countOf, seededNet, steepness, answerOf } from '../../defaults/mods/captain-weights/net.js';
import { starter as tinyStarter, TINY } from '../../defaults/mods/captain-weights/starters.js';
import { DIGITS, digitsNet, digitSet } from './fixtures/captain-weights/digits.mjs';
const starter = (id) => (id === DIGITS ? digitsNet() : tinyStarter(id));
import { refForward, refCost, measured } from './fixtures/captain-weights/reference.mjs';

const close = (a, b, tol, what) => assert.ok(Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b)), `${what}: ${a} against ${b}`);

test('the tiny network has 13 numbers and the digit reader 13,002', () => {
  assert.deepEqual(countOf(starter(TINY)), { weights: 9, biases: 4, total: 13 });
  assert.deepEqual(countOf(starter(DIGITS)), { weights: 12544 + 256 + 160, biases: 42, total: 13002 });
});

test('the forward pass and the cost match the reference', () => {
  for (const id of [TINY, DIGITS]) {
    const net = starter(id);
    for (const e of net.examples.slice(0, 4)) {
      const run = forward(net, e.x), ref = refForward(net, e.x);
      run.a.at(-1).forEach((v, j) => close(v, ref[j], 1e-12, `${id} output ${j}`));
      close(costOf(net, run.a.at(-1), e.y), refCost(net, e.x, e.y), 1e-12, `${id} cost`);
    }
  }
});

test('on the tiny network every gradient matches a measured slope', () => {
  const net = starter(TINY);
  for (const e of net.examples) {
    const g = backward(net, e.x, e.y);
    net.layers.forEach(({ W, b }, k) => {
      for (let j = 0; j < b.length; j++) {
        close(g.dB[k][j], measured(net, e.x, e.y, k, j, null), 1e-6, `${e.name}: bias ${k}.${j}`);
        for (let i = 0; i < W[j].length; i++) close(g.dW[k][j][i], measured(net, e.x, e.y, k, j, i), 1e-6, `${e.name}: weight ${k}.${j}.${i}`);
      }
    });
  }
});

test('on the digit reader a spread of gradients matches measured slopes, with sigmoid and with relu', () => {
  for (const squish of ['sigmoid', 'relu']) {
    const net = { ...starter(DIGITS), squish };
    const e = net.examples[3];
    const g = backward(net, e.x, e.y);
    const lit = e.x.findIndex((v) => v > 0.5);
    for (const [k, j, i] of [[0, 0, lit], [0, 7, lit + 1], [0, 15, 0], [1, 3, 4], [1, 15, 15], [2, 0, 0], [2, 9, 15], [0, 2, null], [1, 5, null], [2, 9, null]]) {
      const mine = i == null ? g.dB[k][j] : g.dW[k][j][i];
      close(mine, measured(net, e.x, e.y, k, j, i), 1e-5, `${squish} ${k}.${j}.${i}`);
    }
  }
});

test('a neuron\'s nudge is the measured slope of the cost against its activation', () => {
  // Nudging a hidden activation is the same as nudging the bias of a neuron after a straight-through squish, so it
  // is checked the other way: dZ = dA times the squish's steepness, and dB = dZ.
  const net = starter(TINY), e = net.examples[3];
  const run = forward(net, e.x), g = backward(net, e.x, e.y, run);
  for (let k = 1; k <= 2; k++) g.dA[k].forEach((d, j) => close(d * steepness(net.squish, run.z[k][j], run.a[k][j]), measured(net, e.x, e.y, k - 1, j, null), 1e-6, `column ${k} neuron ${j}`));
});

test('the chain rule: three factors whose product is the gradient', () => {
  const net = starter(TINY), e = net.examples[3];
  const run = forward(net, e.x), g = backward(net, e.x, e.y, run);
  // A weight into the output: how active the neuron before is, how steep the squish is, how far off the output is.
  const c = chain(net, run, g, 1, 0, 1);
  assert.equal(c.last, true);
  close(c.before, run.a[1][1], 1e-12, 'the neuron before');
  close(c.behind, 2 * (run.a[2][0] - e.y[0]), 1e-12, 'how far off');
  close(c.product, g.dW[1][0][1], 1e-12, 'the product');
  close(c.product, measured(net, e.x, e.y, 1, 0, 1), 1e-6, 'the product, measured');
  // A weight further back: the third factor is everything behind the neuron it feeds.
  const d = chain(net, run, g, 0, 1, 0);
  assert.equal(d.last, false);
  close(d.product, measured(net, e.x, e.y, 0, 1, 0), 1e-6, 'an earlier weight');
});

test('cross-entropy, softmax, tanh and linear layers: every gradient matches a measured slope', () => {
  const kinds = [
    { cost: 'cross_entropy', acts: ['tanh', 'sigmoid'], sizes: [3, 4, 2], y: [1, 0] },
    { cost: 'cross_entropy', acts: ['relu', 'softmax'], sizes: [3, 5, 3], y: [0, 1, 0] },
    { cost: 'squared_error', acts: ['linear', 'softmax'], sizes: [3, 4, 3], y: [0, 0, 1] },
    { cost: 'squared_error', acts: ['tanh', 'linear'], sizes: [3, 4, 2], y: [0.3, -0.2] },
  ];
  kinds.forEach((kd, n) => {
    const net = seededNet(kd.sizes, 40 + n);
    net.cost = kd.cost;
    net.layers.forEach((l, k) => { l.activation = kd.acts[k]; });
    const x = [0.2, -0.7, 0.9];
    const run = forward(net, x), g = backward(net, x, kd.y, run);
    close(g.cost, refCost(net, x, kd.y), 1e-10, `${kd.cost} ${kd.acts} cost`);
    net.layers.forEach(({ W, b }, k) => {
      for (let j = 0; j < b.length; j++) {
        close(g.dB[k][j], measured(net, x, kd.y, k, j, null), 1e-5, `${kd.acts} bias ${k}.${j}`);
        for (let i = 0; i < W[j].length; i++) close(g.dW[k][j][i], measured(net, x, kd.y, k, j, i), 1e-5, `${kd.acts} weight ${k}.${j}.${i}`);
      }
    });
    // Softmax ties the outputs together: the three factors do not multiply to the gradient there, and say so.
    const c = chain(net, run, g, 1, 0, 0);
    assert.equal(c.exact, kd.acts[1] !== 'softmax');
    if (c.exact) close(c.product, c.gradient, 1e-10, 'the product is the gradient');
    close(c.gradient, measured(net, x, kd.y, 1, 0, 0), 1e-5, 'the gradient, measured');
  });
});

test('the tiny network gets "Both on" wrong and the other three right', () => {
  const net = starter(TINY);
  const { right, of } = costOver(net, net.test);
  assert.deepEqual([right, of], [3, 4]);
  const out = forward(net, net.examples[3].x).a.at(-1)[0];
  assert.ok(out > 0.5, `Both on should come out wrong (above a half): ${out}`);
});

test('the made-up digits are repeatable, differ from each other, and are not blank', () => {
  const a = digitSet(23, 2), b = digitSet(23, 2);
  assert.deepEqual(a, b, 'the same seed gives the same pictures');
  assert.equal(a.length, 20);
  for (const e of a) { const lit = e.x.filter((v) => v > 0).length; assert.ok(lit > 15 && lit < 400, `${e.name}: ${lit} lit pixels`); assert.equal(e.y.reduce((s, v) => s + v, 0), 1); }
  assert.notDeepEqual(a[1].x, a[11].x, 'two pictures of a 1 differ');
  assert.deepEqual(seededNet([3, 2], 9), seededNet([3, 2], 9));
});
