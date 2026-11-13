// What the agent is doing, live, on the Weights page: an event told on the change feed makes its line appear in
// words and what it touched pulse, with no click and no timer that reads data; an edit opens to its old and new
// values and can be taken back; the header is the session's ledger; and "From the weights alone" shows only when
// the ledger says no sandbox was allowed and no forward pass was used.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-live.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset, localEdit, localNote, localSession, localNetwork } = await import('../../defaults/mods/captain-weights/local.js');
const { eventWords, ledgerFacts } = await import('../../defaults/mods/captain-weights/timeline.js');
const { starter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { liveTransport, liveReset, liveState } = await import('../../defaults/mods/captain-weights/kit/live.js');

let feed, served = {}, asked = [];
weightsTransport(async (path, opts) => { asked.push([path, opts?.method ?? 'GET', opts?.body]); return served[path.split('?')[0]] ?? { ok: false, error: 'Not Found' }; });
async function page(model) {
  localReset(); liveReset(); asked = [];
  feed = fakeFeed(liveTransport);
  globalThis.__props = { model };
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');

test('the sentence for an edit reads as a person would say it', () => {
  const weights = Array.from({ length: 12 }, (_v, i) => ({ k: 1, j: 6, i, old: 1, new: 0.5 }));
  assert.equal(eventWords({ by: 'agent', kind: 'edit', op: 'scale', layer: 2, neuron: 6, factor: 0.5, changes: weights, cost: { test: { before: 0.412, after: 0.388 } } }),
    'Agent scaled 12 weights into layer 2, neuron 7, by 0.5. Cost on the test set: 0.412 to 0.388.');
  assert.equal(eventWords({ by: 'you', kind: 'edit', op: 'set', layer: 1, neuron: 1, changes: [{ k: 0, j: 1, i: null, old: -12, new: -9 }] }), 'You set the bias of layer 1, neuron 2 from -12.00 to -9.00.');
  assert.equal(eventWords({ by: 'agent', kind: 'edit', op: 'zero', layer: 1, neuron: 2, changes: [{ k: 0, j: 2, i: 0, old: -2, new: 0 }, { k: 0, j: 2, i: 1, old: 3, new: 0 }, { k: 0, j: 2, i: null, old: -1, new: 0 }] }),
    'Agent set 2 weights and the bias of layer 1, neuron 3 to zero.');
  assert.equal(eventWords({ by: 'agent', kind: 'read', layer: 1, touched: { weights: [[0, 0, 0], [0, 0, 1]] } }), 'Agent read 2 weights of layer 1.');
  assert.equal(eventWords({ by: 'agent', kind: 'run', input: 'Both on' }), 'Agent ran the network on "Both on".');
  assert.equal(eventWords({ by: 'agent', kind: 'undo', layer: 1, neuron: 1 }), 'Agent took back an edit to layer 1, neuron 2.');
  // More numbers changed than the record lists: the true count is said.
  assert.equal(eventWords({ by: 'agent', kind: 'edit', op: 'add', tensor: 'layer1.weight', changed: 12544, changes: weights }), 'Agent changed 12,544 numbers in layer1.');
  for (const kind of ['edit', 'undo', 'read', 'run', 'gradients', 'evaluate', 'open', 'save', 'something-new']) {
    const w = eventWords({ by: 'agent', kind, layer: 1, neuron: 0 });
    assert.ok(/^Agent .+\.$/.test(w) && !/undefined|NaN|null/.test(w), w);
  }
});

test('an agent\'s read, run and edit appear as lines the moment the feed tells, and what they touched pulses', async () => {
  const h = await page(TINY);
  const sid = localSession(TINY).id;
  assert.ok(h.text().includes('Nothing yet.'), 'an empty timeline says what will show');
  assert.equal(h.mod.drawing().pulse, null);

  localNote(sid, { kind: 'read', layer: 1, touched: { neurons: [[1, 1]], weights: [[0, 1, 0], [0, 1, 1]] } });
  // Nothing changes until the feed tells: the page has no timer that reads data.
  await h.idle(10);
  assert.ok(h.text().includes('Nothing yet.'));
  feed.tell(`weights-session:${sid}`);
  await h.layoutLists({});
  assert.equal(liveState().state, 'live');
  assert.ok(h.text().includes('Agent read 2 weights of layer 1.'), h.text());
  // What it touched pulsed. The harness runs the page's timers to their end, so the pulse has also finished:
  // its clock beat a fixed number of times and then armed nothing more.
  let p = h.mod.pulsed;
  assert.deepEqual([[...p.neurons], [...p.weights].sort()], [['1:1'], ['0:1:0', '0:1:1']], 'the neuron and the two weights it read pulsed');
  assert.equal(h.mod.drawing().pulse, null, 'and the pulse ended by itself');
  const beats = h.mod.beats;
  await h.layoutLists({});
  assert.equal(h.mod.beats, beats, 'no timer is armed while nothing pulses');

  localNote(sid, { kind: 'run', input: 'Both on', touched: { neurons: [[2, 0]], weights: [] } });
  const ev = localEdit(sid, { op: 'set', layer: 1, neuron: 1, from: null, value: -9, target: 3, by: 'agent', why: 'the bias keeps this neuron half off when both inputs are on' });
  feed.tell(`weights:${TINY}`);
  await h.layoutLists({});
  const text = h.text();
  assert.ok(text.includes('Agent ran the network on "Both on".'));
  assert.ok(text.includes(`Agent set the bias of layer 1, neuron 2 from -12.00 to -9.00. Cost on the test set: ${ev.cost.test.before.toFixed(3)} to ${ev.cost.test.after.toFixed(3)}.`), text);
  p = h.mod.pulsed;
  assert.ok(p.neurons.has('1:1') && p.neurons.has('2:0'), 'the edited neuron and the output that was run pulsed');
  assert.ok(h.mod.beats > beats, 'the clock ran again for the new pulse');
  // Newest first.
  const lines = h.byIdPrefix('wt-ev-line-').map((e) => e.call('id')[0]);
  assert.deepEqual(lines, ['wt-ev-line-e3', 'wt-ev-line-e2', 'wt-ev-line-e1']);
  // The ledger moved with it.
  assert.equal(textOf(h, 'wt-ledger'), 'Raw weights: 2 read. Sandbox: allowed. Forward passes: 1 of 50 used.');
  // The short line's terms are spelled out on hover.
  assert.ok(h.byIdPrefix('wt-ledger').find((e) => e.call('id')[0] === 'wt-ledger').call('tooltip')[0].includes('A sandbox (code run on the numbers): allowed. Forward passes (runs of the network): 1 of 50 used'));
  assert.equal(textOf(h, 'wt-ledger-changed'), '1 of 13 numbers changed (7.7%)');
  assert.equal(h.byIdPrefix('wt-alone').length, 0, 'a session with a sandbox and a run is not "from the weights alone"');
});

test('an edit opens to its old and new values, and can be taken back', async () => {
  const h = await page(TINY);
  const sid = localSession(TINY).id;
  const ev = localEdit(sid, { op: 'scale', layer: 2, neuron: 0, factor: 0.5, target: 3, by: 'agent', why: 'the output leans too hard on the hidden neurons' });
  feed.tell(`weights-session:${sid}`);
  await h.layoutLists({});
  assert.ok(h.text().includes('Agent scaled 3 weights into layer 2, neuron 1, by 0.5.'));
  assert.equal(h.byIdPrefix('wt-ev-undo-').length, 0, 'closed: no values, no undo');
  await h.click(`wt-ev-line-${ev.id}`);
  const text = h.text();
  assert.ok(text.includes('Why: the output leans too hard on the hidden neurons'));
  assert.ok(text.includes('The weight from hidden neuron 1') && text.includes('+7.00 to +3.50'));
  assert.ok(text.includes('The weight from hidden neuron 2') && text.includes('-8.00 to -4.00'));
  assert.ok(text.includes('The weight from hidden neuron 3') && text.includes('+0.40 to +0.20'));
  assert.ok(text.includes('The edit that is open'), 'its three numbers show under the cost');
  assert.equal(localNetwork(TINY).layers[1].W[0][1], -4);
  await h.click(`wt-ev-undo-${ev.id}`);
  assert.equal(localNetwork(TINY).layers[1].W[0][1], -8, 'the old number is back');
  const after = h.text();
  assert.ok(after.includes('Agent scaled 3 weights into layer 2, neuron 1, by 0.5.') && after.includes('Taken back.'), 'the edit stays in the record, marked');
  assert.ok(after.includes('You took back an edit to layer 2, neuron 1.'));
  assert.equal(h.byIdPrefix('wt-ev-undo-').length, 0, 'an edit taken back has no undo');
  assert.equal(textOf(h, 'wt-ledger-changed'), '0 of 13 numbers changed (0%)');
  assert.ok(after.includes('-8.00'), 'the drawing shows the old weight again');
});

// A network from Captain's services, in the shapes their guide gives (origin/feat/weights, docs/weights.md).
function serve(ledger, events = []) {
  const net = starter(TINY);
  served = {
    '/api/weights/models/tiny': { ok: true, data: { id: 'tiny', name: 'tiny', version: 1, sizes: net.sizes, squish: 'sigmoid', cost: 'cross_entropy', layers: net.layers.map((l) => ({ ...l, activation: 'sigmoid' })),
      inputNames: ['x1', 'x2'], outputNames: ['exactly one is high'], examples: [{ name: 'practice 0', x: [0.9, 0.1], y: [1] }], test: [], synthetic: true } },
    '/api/weights/models/tiny/session': { ok: true, data: { id: 'ws-1', model: 'tiny', version: 1, who: { kind: 'agent', name: 'demo' }, ledger, edits: [], events } },
    '/api/weights/sessions/ws-1/evaluate': { ok: true, data: { sets: { held_out: { name: 'held out', defined: true, of: 200, before: 0.19, after: 0.19, right_before: 179, right_after: 179, answers_changed: 0 } } } },
  };
}
const LEDGER = { raw_weights: true, weights_read: 13, sandbox: false, sandbox_used: false, forward_allowed: 20, forward_used: 0, gradients_computed: false, changed: 0, total: 13, share: 0, weights_alone: true, label: 'from the weights alone' };

test('"From the weights alone" shows only when the ledger says no sandbox and no forward pass', async () => {
  serve(LEDGER);
  let h = await page('tiny');
  assert.equal(textOf(h, 'wt-alone'), 'From the weights alone');
  assert.equal(textOf(h, 'wt-ledger'), 'Raw weights: 13 read. Sandbox: not allowed. Forward passes: 0 of 20 used.');
  // The services' own network drew: cross-entropy, their names, and the test set scored by them.
  assert.ok(textOf(h, 'wt-title') === 'tiny' && h.text().includes('exactly one is high'));
  assert.equal(textOf(h, 'wt-cost-test'), '0.190');
  assert.ok(h.text().includes('Test set (200 inputs)') && h.text().includes('179 of 200 right'));
  assert.ok(textOf(h, 'wt-cost-says').includes('cross-entropy'));
  assert.ok(asked.some(([p]) => p === '/api/weights/models/tiny?session=ws-1'), 'the numbers are asked for as the session has edited them');
  assert.equal(asked.filter(([p]) => p.endsWith('/evaluate')).length, 1, 'the test set is scored once');
  // A record that already holds a score for these numbers is not scored again.
  serve(LEDGER, [{ id: 'e1', seq: 1, by: 'you', kind: 'evaluate', touched: { neurons: [], weights: [] }, cost: { test: { name: 'held out', of: 200, before: 0.19, after: 0.17, right_before: 179, right_after: 181 } } }]);
  h = await page('tiny');
  assert.equal(textOf(h, 'wt-cost-test'), '0.170');
  assert.equal(asked.filter(([p]) => p.endsWith('/evaluate')).length, 0, 'the last score in the record is used');
  assert.ok(!h.text().includes('You scored the network on the test set.'), 'the page\'s own scoring is not listed as something done to the network');
  serve(LEDGER);
  h = await page('tiny');
  const no = [
    ['one forward pass was used', { ...LEDGER, forward_used: 1 }],
    ['a sandbox was allowed', { ...LEDGER, sandbox: true }],
    ['a sandbox was used', { ...LEDGER, sandbox_used: true }],
    ['the services say no', { ...LEDGER, weights_alone: false }],
    ['the ledger does not say whether there was a sandbox', (({ sandbox, ...rest }) => rest)(LEDGER)],
    ['the ledger does not say how many passes were used', (({ forward_used, ...rest }) => rest)(LEDGER)],
    ['there is no ledger', undefined],
  ];
  for (const [why, ledger] of no) {
    assert.equal(ledgerFacts(ledger).alone, false, why);
    serve(ledger);
    h = await page('tiny');
    assert.equal(h.byIdPrefix('wt-alone').length, 0, why);
    assert.ok(!h.text().includes('From the weights alone'), why);
  }
  // A label the services send is not trusted by itself: the facts decide.
  assert.equal(ledgerFacts({ ...LEDGER, forward_used: 3, label: 'from the weights alone' }).alone, false);
  served = {};
});

test('a network that cannot be read says so in plain words, with a way to try again', async () => {
  served = { '/api/weights/models/tiny/session': { ok: false, error: 'HTTP 500' } };
  const h = await page('tiny');
  const text = h.text();
  assert.ok(text.includes("Captain couldn't read that network."), text);
  assert.ok(!/HTTP|500|Not Found/.test(text), 'never the raw error');
  assert.equal(h.byIdPrefix('wt-retry').length, 1);
});

test('a network with many inputs shows ten at a time', async () => {
  const net = starter(TINY);
  const examples = Array.from({ length: 23 }, (_v, n) => ({ name: `practice ${n}`, x: [n % 2, (n >> 1) % 2], y: [(n % 2) ^ ((n >> 1) % 2)] }));
  served = {
    '/api/weights/models/tiny': { ok: true, data: { ...net, id: 'tiny', name: 'tiny', examples, test: [], synthetic: true } },
    '/api/weights/models/tiny/session': { ok: true, data: { id: 'ws-1', model: 'tiny', ledger: LEDGER, events: [] } },
  };
  const h = await page('tiny');
  assert.equal(h.byIdPrefix('wt-in-').filter((e) => /^wt-in-\d+$/.test(e.call('id')[0])).length, 10);
  assert.ok(h.text().includes('Input 1 of 23') && h.text().includes('Later inputs') && !h.text().includes('Earlier inputs'));
  assert.ok(h.text().includes('Made-up data'), 'a network that says its data is made up is labelled so');
  await h.click('wt-in-later');
  assert.ok(h.text().includes('Input 11 of 23') && h.text().includes('practice 10') && h.text().includes('Earlier inputs'));
  await h.click('wt-in-later');
  assert.equal(h.byIdPrefix('wt-in-').filter((e) => /^wt-in-\d+$/.test(e.call('id')[0])).length, 3, 'the last three');
  await h.click('wt-in-earlier');
  assert.ok(h.text().includes('Input 20 of 23'));
  served = {};
});

test('while a pulse lasts, the touched neuron is ringed and its lines are drawn in the pulse colour', async () => {
  const { layoutOf, shapesFor, tint } = await import('../../defaults/mods/captain-weights/draw.js');
  const { palette } = await import('../../defaults/mods/captain-weights/kit/tokens.js');
  const { forward } = await import('../../defaults/mods/captain-weights/net.js');
  const net = starter(TINY), c = palette(false);
  const pulse = { neurons: new Set(['1:1']), weights: new Set(['0:1:0', '0:1:1']), left: 10, phase: 1 };
  const s = shapesFor(layoutOf(net), { net, run: forward(net, net.examples[0].x), sel: null, mode: 'values', pulse }, c);
  assert.equal(s.groups.get(tint(c.warn, 'ff')).length, 2, 'the two weights that were read');
  const plain = shapesFor(layoutOf(net), { net, run: forward(net, net.examples[0].x), sel: null, mode: 'values', pulse: null }, c);
  assert.ok(!plain.groups.has(tint(c.warn, 'ff')));
});
