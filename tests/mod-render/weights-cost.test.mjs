// The cost on the Weights page: one number for the chosen input and one for the test set, both against the
// reference network; a curve with a mark per edit; and an edit's three numbers (the target, "should not change",
// the test set), each before and after.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-cost.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { refCost, fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset, localEdit, localUndo, localSession } = await import('../../defaults/mods/captain-weights/local.js');
const { costSteps, threeNumbers, cost3 } = await import('../../defaults/mods/captain-weights/cost.js');
const { starter: tinyStarter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { DIGITS, digitsNet, digitsRoutes } = await import('./fixtures/captain-weights/digits.mjs');
const starter = (id) => (id === DIGITS ? digitsNet() : tinyStarter(id));
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

weightsTransport(async (path) => digitsRoutes(path) ?? { ok: false, error: 'Not Found' });
let feed;
async function page(model) {
  localReset(); liveReset();
  feed = fakeFeed(liveTransport);
  globalThis.__props = { model };
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const mean = (xs) => xs.reduce((s, v) => s + v, 0) / xs.length;

test('the two numbers match the reference network, and follow the chosen input', async () => {
  for (const id of [TINY, DIGITS]) {
    const h = await page(id);
    const net = starter(id);
    assert.equal(textOf(h, 'wt-cost-input'), cost3(refCost(net, net.examples[0].x, net.examples[0].y)), `${id}: this input`);
    assert.equal(textOf(h, 'wt-cost-test'), cost3(mean(net.test.map((e) => refCost(net, e.x, e.y)))), `${id}: the test set`);
    await h.click('wt-in-3');
    assert.equal(textOf(h, 'wt-cost-input'), cost3(refCost(net, net.examples[3].x, net.examples[3].y)));
    assert.equal(textOf(h, 'wt-cost-input-under'), net.examples[3].name);
  }
  const h = await page(TINY);
  assert.equal(textOf(h, 'wt-cost-test-under'), '3 of 4 right');
  assert.ok(h.text().includes('Test set (4 inputs)'));
  assert.ok(textOf(h, 'wt-cost-says').startsWith('Cost is one number for how wrong the network is'), 'the term has its plain words beside it');
  assert.ok(h.text().includes('Each edit adds a point to a curve here.'), 'no curve before the first edit');
  assert.equal(h.byIdPrefix('wt-cost-edit').length, 0, 'no three numbers before the first edit');
});

test('an edit adds a mark to the curve and shows its three numbers, before and after', async () => {
  const h = await page(TINY);
  const net = starter(TINY), sid = localSession(TINY).id;
  const before = { target: refCost(net, [1, 1], [0]), keep: mean(net.examples.slice(0, 3).map((e) => refCost(net, e.x, e.y))), test: mean(net.test.map((e) => refCost(net, e.x, e.y))) };
  // The fix: hidden neuron 2's bias from -12 to -9, meant for "Both on" (example 3).
  const ev = localEdit(sid, { op: 'set', layer: 1, neuron: 1, from: null, value: -9, target: 3, by: 'agent', why: 'the bias keeps this neuron half off when both inputs are on' });
  net.layers[0].b[1] = -9;
  const after = { target: refCost(net, [1, 1], [0]), keep: mean(net.examples.slice(0, 3).map((e) => refCost(net, e.x, e.y))), test: mean(net.test.map((e) => refCost(net, e.x, e.y))) };
  // The event carries the three numbers, and they are the reference's.
  for (const k of ['target', 'keep', 'test']) {
    assert.ok(Math.abs(ev.cost[k].before - before[k]) < 1e-12 && Math.abs(ev.cost[k].after - after[k]) < 1e-12, `${k}: ${JSON.stringify(ev.cost[k])}`);
  }
  assert.ok(after.target < before.target / 5, 'the target got much better');
  assert.equal(ev.cost.keep.answers_changed, 0, 'no other answer changed');
  assert.deepEqual([ev.cost.test.right_before, ev.cost.test.right_after], [3, 4]);
  // The page hears it on the change feed and reads again: no timer of its own.
  feed.tell(`weights-session:${sid}`);
  await h.layoutLists({});
  assert.equal(h.byIdPrefix('wt-cost-mark-').length, 1, 'one mark for the edit');
  assert.equal(textOf(h, 'wt-cost-test'), cost3(after.test));
  assert.equal(textOf(h, 'wt-cost-test-under'), '4 of 4 right');
  const text = h.text();
  assert.ok(text.includes('The last edit'));
  assert.ok(text.includes('Target: Both on') && text.includes(`${cost3(before.target)} to ${cost3(after.target)}`), text);
  assert.ok(text.includes('Should not change (3 inputs)') && text.includes(`${cost3(before.keep)} to ${cost3(after.keep)}`) && text.includes('0 answers changed'));
  assert.ok(text.includes('Test set (4 inputs)') && text.includes(`${cost3(before.test)} to ${cost3(after.test)}`) && text.includes('4 of 4 right'));
  // The drawing has the new bias.
  assert.ok(text.includes('bias -9.00') && !text.includes('bias -12.00'));
  // Taking it back adds a second, hollow mark, and the cost returns.
  localUndo(sid, ev.id, 'agent');
  feed.tell(`weights:${TINY}`);
  await h.layoutLists({});
  assert.equal(h.byIdPrefix('wt-cost-mark-').length, 2);
  assert.equal(textOf(h, 'wt-cost-test'), cost3(before.test));
  const steps = costSteps(localSession(TINY), null);
  assert.deepEqual(steps.map((s) => s.kind), ['start', 'edit', 'undo']);
  assert.equal(steps[1].undone, true, 'the edit stays in the record, marked as taken back');
  assert.ok(Math.abs(steps[0].cost - steps[2].cost) < 1e-12);
});

test('three numbers in the services\' shape read the same', () => {
  const rows = threeNumbers({ cost: {
    target: { name: 'target', of: 40, before: 0.61, after: 0.22, right_before: 21, right_after: 37, answers_changed: 16 },
    keep: { name: 'should not change', of: 160, before: 0.08, after: 0.09, right_before: 158, right_after: 158, answers_changed: 0 },
    test: { name: 'held out', of: 200, before: 0.19, after: 0.12, right_before: 179, right_after: 195, answers_changed: 16 } } });
  assert.deepEqual(rows.map((r) => r.name), ['Target (40 inputs)', 'Should not change (160 inputs)', 'Test set (200 inputs)']);
  assert.deepEqual(rows.map((r) => r.says), ['what the edit was meant to fix', '0 answers changed', '195 of 200 right']);
  assert.deepEqual(threeNumbers({ cost: { test: { before: 0.2, after: 0.1, of: 4 } } }).map((r) => r.id), ['test'], 'an undo carries only the test set');
  assert.deepEqual(threeNumbers({}), []);
});
