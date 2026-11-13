// The Weights page against answers recorded from Captain's services' own weights routes (fixtures/captain-weights/
// recorded-tiny.json: what captain_search on origin/feat/weights answered through its test client, in an empty
// workspace, at the commit the fixture names). It checks that the page reads those shapes as they are, and that the page's arithmetic agrees with
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
  const [p, q = ''] = path.split('?');
  if (p === '/api/weights/models') return answer('models');
  if (p === '/api/weights/models/nope/session') return answer('missing');
  if (p === '/api/weights/models/tiny/session') return answer(stage === 'start' ? 'session:start' : stage === 'edited' ? 'session:after-edit' : stage === 'end' ? 'session:end' : 'person:session-after-edit');
  // Without a session the services send the network's shape and no numbers.
  if (p === '/api/weights/models/tiny') return !q.includes('session=') ? answer('whole:no-session') : answer(stage === 'start' || stage === 'person' ? 'whole:start' : stage === 'edited' ? 'whole:after-edit' : 'whole:end');
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
  assert.ok(!text.includes('Tiny network') && !text.includes("needs Captain's services"), 'the page\'s own tiny network steps aside when the services answer');
  assert.equal(h.byIdPrefix('wt-open-btn-').length, 2);
});

test('their tiny network opens through a session, as a screen\'s look, and nothing is asked that changes the record', async () => {
  const h = await page('tiny');
  const net = data('whole:start');
  assert.equal(data('whole:no-session').numbers, false, 'the fixture: no numbers without a session');
  assert.deepEqual(asked.map(([p, m]) => `${m} ${p}`), ['GET /api/weights/models/tiny/session', `GET /api/weights/models/tiny?session=${personal}&watch=1`]);
  assert.equal(textOf(h, 'wt-facts'), '2 in, 1 out, 13 numbers (9 weights and 4 biases). Made-up data');
  assert.ok(h.text().includes('Input 1 of 24') && h.text().includes(net.examples[0].name));
  assert.equal(h.byIdPrefix('wt-n-').length, 6);
  assert.equal(textOf(h, 'wt-cost-says').includes('cross-entropy'), true);
  // Nobody has scored it yet, and the page does not ask.
  assert.equal(textOf(h, 'wt-cost-test'), 'not scored');
  assert.ok(h.text().includes('Test set (300 inputs)'), 'the size of the set the services keep back');
  // A person's own fresh session: no sandbox, nothing run, no score shown.
  assert.equal(textOf(h, 'wt-alone'), 'From the weights alone');
  assert.ok(textOf(h, 'wt-ledger').includes('Forward passes: 0 of 50 used'));
  // Nudges work on their network too: gradients for cross-entropy, worked out in the page.
  await h.click('wt-n-2-0');
  await h.click('wt-mode-nudges');
  const e = net.examples[0], g = backward(net, e.x, e.y);
  assert.ok(Number.isFinite(g.dW[1][0][0]) && h.byIdPrefix('wt-inspect-nudge').length === 1);
});

test('the page\'s arithmetic agrees with the services\' numpy', async () => {
  // A person's edit aimed at the input on screen: the services measured the cost on that input before and after.
  const ev = data('person:edit'), before = data('whole:start'), e = before.examples[0];
  near(costOf(before, forward(before, e.x).a.at(-1), e.y), ev.cost.target.before, 0.02);
  const after = JSON.parse(JSON.stringify(before));
  for (const c of ev.changes) { if (c.i == null) after.layers[c.k].b[c.j] = c.new; else after.layers[c.k].W[c.j][c.i] = c.new; }
  near(costOf(after, forward(after, e.x).a.at(-1), e.y), ev.cost.target.after, 0.02);
  // And the page shows that edit's numbers: the target and the test set. No "should not change" set was named.
  const h = await page('tiny');
  stage = 'person';
  feed.tell(`weights-session:${personal}`);
  await h.layoutLists({});
  const text = h.text();
  assert.ok(text.includes(`You set the bias of layer 1, neuron 1 from +10.18 to +9.00. Cost on the test set: ${ev.cost.test.before.toFixed(3)} to ${ev.cost.test.after.toFixed(3)}.`), text);
  assert.ok(text.includes('Target') && text.includes(`${ev.cost.target.before.toFixed(3)} to ${ev.cost.target.after.toFixed(3)}`));
  assert.ok(!text.includes('Should not change'), 'a set that was not named is left out');
  assert.equal(textOf(h, 'wt-cost-test'), ev.cost.test.after.toFixed(3));
  assert.ok(text.includes('291 of 300 right'));
  // A score was shown in a session with feedback: it is no longer "from the weights alone".
  assert.equal(data('person:session-after-edit').ledger.weights_alone, false);
  assert.equal(h.byIdPrefix('wt-alone').length, 0);
  assert.ok(textOf(h, 'wt-ledger').endsWith('Scores shown.'));
});

test('an agent opens a newer, blind session and edits: the page moves to it, and its scores stay hidden', async () => {
  const h = await page('tiny');
  assert.equal(h.mod.session.id, personal);
  stage = 'edited';
  feed.tell('weights:tiny');
  await h.layoutLists({});
  assert.equal(h.mod.session.id, data('session:after-edit').id, 'the newest session on the model');
  assert.equal(h.mod.pulsed ?? null, null, 'another session\'s history does not pulse as if it just happened');
  const ev = data('edit:scale');
  assert.equal(ev.scores, 'hidden');
  const text = h.text();
  assert.ok(text.includes('Agent scaled 2 weights into layer 1, neuron 2, by 0.5. Its scores are hidden until the session is saved or closed.'), text);
  assert.ok(text.includes('Agent read 6 weights of layer 1.'));
  assert.ok(text.includes('Agent opened a session on this network.'));
  assert.equal(textOf(h, 'wt-cost-test'), 'hidden');
  assert.equal(h.byIdPrefix('wt-cost-edit').length, 0, 'no three numbers for an edit whose scores are hidden');
  assert.ok(asked.some(([p]) => p === `/api/weights/models/tiny?session=${data('session:after-edit').id}&watch=1`), 'the numbers as that session has them');
  assert.equal(textOf(h, 'wt-ledger-changed'), '2 of 13 numbers changed (15.4%)');
  assert.equal(textOf(h, 'wt-alone'), 'From the weights alone', 'blind, no sandbox, and the agent has not run it yet');
  assert.ok(textOf(h, 'wt-ledger').includes('Blind'));
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
  assert.ok(/Agent ran the network on "[^"]+"\./.test(end), end);
  assert.ok(end.includes('You took back an edit to layer 1, neuron 2.'));
  assert.ok(end.includes('Agent scaled 2 weights into layer 1, neuron 2, by 0.5.') && end.includes('Taken back.'));
  assert.ok(end.includes('You set the bias of layer 1, neuron 2 from'));
  assert.ok(h.mod.pulsed && h.mod.pulsed.neurons.has('1:1'), 'what the new events touched pulsed');
  assert.ok(asked.every(([, m]) => m === 'GET'), 'watching an agent\'s session changes nothing in it');
});

test('a refusal from the services is shown in their own sentence, and a missing model in plain words', async () => {
  const h = await page('tiny');
  await h.click('wt-n-1-1');
  h.mod.handInput.set_value('3');
  await h.click('wt-hand-set');
  assert.equal(textOf(h, 'wt-hand-note'), 'This edit changes nothing: every number it names already has that value.');
  const sent = asked.find(([p]) => p.endsWith('/edits'));
  const e = data('whole:start').examples[0];
  assert.deepEqual(sent[2].target, { inputs: [e.x], targets: [e.y] }, 'the input on screen goes with the edit, whole');
  const gone = await page('nope');
  assert.ok(gone.text().includes('There is no weights model named nope.'), gone.text());
  assert.equal(gone.byIdPrefix('wt-retry').length, 1);
});
