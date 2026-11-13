// The Weights page against answers recorded from Captain's services' own weights routes (fixtures/captain-weights/
// recorded-tiny.json: what captain_search on origin/feat/weights answered through its test client, in an empty
// workspace). It checks that the page reads those shapes as they are, and that the page's arithmetic agrees with
// the services' numpy: the cost the page works out for an input is the cost the services measured for it.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-recorded.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { harness } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { forward, backward, costOf } = await import('../../defaults/mods/captain-weights/net.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

const REC = JSON.parse(fs.readFileSync(new URL('./fixtures/captain-weights/recorded-tiny.json', import.meta.url), 'utf8'));
const data = (k) => REC[k].data;
const answer = (k) => (REC[k].status === 200 ? { ok: true, data: data(k) } : { ok: false, error: data(k).error, status: REC[k].status });
const personal = data('session:start').id, agents = data('session:end').id;

let stage = 'start', asked = [], feed;
weightsTransport(async (path, opts) => {
  asked.push([path, opts?.method ?? 'GET', opts?.body]);
  const [p] = path.split('?');
  if (p === '/api/weights/models') return answer('GET /api/weights/models');
  if (p === '/api/weights/models/nope/session' || p === '/api/weights/models/nope') return answer('missing');
  if (p === '/api/weights/models/tiny/session') return answer(stage === 'start' ? 'session:start' : stage === 'edited' ? 'session:after-edit' : 'session:end');
  if (p === '/api/weights/models/tiny') return answer(stage === 'start' ? 'whole:start' : stage === 'edited' ? 'whole:after-edit' : 'whole:end');
  if (p.endsWith('/evaluate')) return answer('evaluate');
  if (p.endsWith('/edits')) return answer('edit:nothing');
  return { ok: false, error: 'Not Found' };
});
async function page(model) {
  localReset(); liveReset(); asked = []; stage = 'start';
  feed = fakeFeed(liveTransport);
  globalThis.__props = model ? { model } : {};
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const near = (a, b, tol) => assert.ok(Math.abs(a - b) <= tol * Math.max(1e-6, Math.abs(a), Math.abs(b)), `${a} against ${b}`);

test('the list is the services\' own: their two starters, with their words', async () => {
  const h = await page(null);
  const text = h.text();
  assert.ok(text.includes('tiny') && text.includes('digits'));
  assert.ok(text.includes('Two inputs, three hidden neurons, one output. Small enough to see every number.'));
  assert.ok(!text.includes('Tiny network'), 'the page\'s own starters step aside when the services answer');
  assert.equal(h.byIdPrefix('wt-open-btn-').length, 2);
});

test('their tiny network opens: cross-entropy, 24 practice inputs, the test set scored by them', async () => {
  const h = await page('tiny');
  const net = data('whole:start');
  assert.equal(net.cost, 'cross_entropy');
  assert.equal(textOf(h, 'wt-facts'), '2 in, 1 out, 13 numbers (9 weights and 4 biases). Made-up data');
  assert.ok(h.text().includes('Input 1 of 24') && h.text().includes('practice 0'));
  assert.equal(h.byIdPrefix('wt-n-').length, 6);
  // The page's arithmetic against the services' numpy. Their default target is practice example 0, so the cost
  // they measured for it is the cost the page works out for the first input. The inputs are sent rounded to four
  // places, hence the tolerance.
  const e = net.examples[0];
  const mine = costOf(net, forward(net, e.x).a.at(-1), e.y);
  near(mine, data('evaluate').sets.target.before, 0.02);
  assert.equal(textOf(h, 'wt-cost-input'), mine.toFixed(3));
  // The test set is theirs to score: 300 inputs the page never sees.
  const held = data('evaluate').sets.held_out;
  assert.equal(textOf(h, 'wt-cost-test'), held.after.toFixed(3));
  assert.ok(h.text().includes('Test set (300 inputs)') && h.text().includes('300 of 300 right'));
  assert.deepEqual(asked.find(([p]) => p.endsWith('/evaluate')).slice(1), ['POST', { by: 'you' }], 'asked as the person, not the agent');
  // A person's own session: no sandbox, nothing run.
  assert.equal(textOf(h, 'wt-alone'), 'From the weights alone');
  assert.ok(textOf(h, 'wt-ledger').includes('Forward passes: 0 of 50 used'));
  // Nudges work on their network too: gradients for cross-entropy, worked out in the page.
  await h.click('wt-n-2-0');
  await h.click('wt-mode-nudges');
  const g = backward(net, e.x, e.y);
  assert.ok(Number.isFinite(g.dW[1][0][0]) && h.byIdPrefix('wt-inspect-nudge').length === 1);
});

test('an agent opens a newer session and edits: the page moves to it and says what it did', async () => {
  const h = await page('tiny');
  assert.equal(h.mod.session.id, personal);
  stage = 'edited';
  feed.tell('weights:tiny');
  await h.layoutLists({});
  assert.equal(h.mod.session.id, data('session:after-edit').id, 'the newest session on the model');
  assert.equal(h.mod.pulsed ?? null, null, 'another session\'s history does not pulse as if it just happened');
  const ev = data('edit:scale');
  const text = h.text();
  assert.ok(text.includes(`Agent scaled 2 weights into layer 1, neuron 2, by 0.5. Cost on the test set: ${ev.cost.test.before.toFixed(3)} to ${ev.cost.test.after.toFixed(3)}.`), text);
  assert.ok(text.includes('Agent read 6 weights of layer 1.'));
  assert.ok(text.includes('Agent opened a session on this network.'));
  // Its three numbers, in the services' shape. The edit named input 0 as its target.
  assert.ok(text.includes('Target: practice 0') && text.includes(`${ev.cost.target.before.toFixed(3)} to ${ev.cost.target.after.toFixed(3)}`));
  assert.ok(text.includes('Should not change (23 inputs)') && text.includes('4 answers changed'));
  assert.ok(text.includes('Test set (300 inputs)') && text.includes('269 of 300 right'));
  assert.equal(textOf(h, 'wt-cost-test'), ev.cost.test.after.toFixed(3));
  assert.equal(asked.filter(([p]) => p.endsWith('/evaluate')).length, 1, 'no second scoring: the edit carries the number');
  // The numbers were read again as that session has them, and the page's cost for input 0 is the services'.
  assert.ok(asked.some(([p]) => p === `/api/weights/models/tiny?session=${data('session:after-edit').id}`));
  const net = data('whole:after-edit'), e = net.examples[0];
  near(costOf(net, forward(net, e.x).a.at(-1), e.y), ev.cost.target.after, 0.02);
  assert.equal(textOf(h, 'wt-cost-input'), costOf(net, forward(net, e.x).a.at(-1), e.y).toFixed(3));
  assert.equal(textOf(h, 'wt-ledger-changed'), '2 of 13 numbers changed (15.4%)');
  assert.equal(textOf(h, 'wt-alone'), 'From the weights alone', 'no sandbox, and the agent has not run it yet');
  // The edit opens to the reason and the old and new values.
  await h.click(`wt-ev-line-${ev.id}`);
  assert.ok(h.text().includes('Why: this unit shouts') && h.text().includes('The weight from x1') && h.text().includes('+9.83 to +4.92'));

  // Then the agent runs the network once, and a person takes its edit back.
  stage = 'end';
  feed.tell(`weights-session:${agents}`);
  await h.layoutLists({});
  const end = h.text();
  assert.equal(h.byIdPrefix('wt-alone').length, 0, 'one forward pass was used: no longer from the weights alone');
  assert.ok(textOf(h, 'wt-ledger').includes('Forward passes: 1 of 20 used'));
  assert.ok(end.includes('Agent ran the network on "practice 0".'));
  assert.ok(end.includes('You took back an edit to layer 1, neuron 2.'));
  assert.ok(end.includes('Agent scaled 2 weights into layer 1, neuron 2, by 0.5.') && end.includes('Taken back.'));
  assert.ok(end.includes('You set the bias of layer 1, neuron 2 from'));
  const p = h.mod.pulsed;
  assert.ok(p && p.neurons.has('1:1'), 'what the new events touched pulsed');
  assert.equal(h.byIdPrefix('wt-cost-mark-').length, 3, 'a mark for the edit, the hand edit and the undo');
});

test('a refusal from the services is shown in their own sentence, and a missing model in plain words', async () => {
  const h = await page('tiny');
  await h.click('wt-n-1-1');
  h.mod.handInput.set_value('3');
  await h.click('wt-hand-set');
  assert.equal(textOf(h, 'wt-hand-note'), 'This edit changes nothing: every number it names already has that value.');
  const gone = await page('nope');
  assert.ok(gone.text().includes('There is no weights model named nope.'), gone.text());
  assert.equal(gone.byIdPrefix('wt-retry').length, 1);
});
