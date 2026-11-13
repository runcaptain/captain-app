// Changing a network by hand on the Weights page: type a value, drag a number and watch the cost move, halve,
// double or zero a neuron, take an edit back; and handing the selection to an agent: a pin for its context and
// three starter prompts written around what is selected.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-hand.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { refCost, fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset, localSession, localNetwork } = await import('../../defaults/mods/captain-weights/local.js');
const { handTarget, selectionPin, starters, dragSpan, TRACK_W } = await import('../../defaults/mods/captain-weights/hand.js');
const { cost3 } = await import('../../defaults/mods/captain-weights/cost.js');
const { starter: tinyStarter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { DIGITS, digitsNet, digitsRoutes } = await import('./fixtures/captain-weights/digits.mjs');
const starter = (id) => (id === DIGITS ? digitsNet() : tinyStarter(id));
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

let served = {}, asked = [];
weightsTransport(async (path, opts) => { asked.push([path, opts]); return served[path.split('?')[0]] ?? digitsRoutes(path) ?? { ok: false, error: 'Not Found' }; });
async function page(model) {
  localReset(); liveReset(); fakeFeed(liveTransport); asked = [];
  globalThis.__props = { model }; globalThis.__runs = []; globalThis.__toasts = []; globalThis.__agents = []; globalThis.__agentList = []; globalThis.__files = {};
  const h = harness(Weights);
  h.cx.write_to_clipboard = (t) => { globalThis.__clipboard = t; };
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const mean = (xs) => xs.reduce((s, v) => s + v, 0) / xs.length;

test('the tiny network\'s mistake is fixed by typing one bias, and the cost moves', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3'); // "Both on": wrong
  assert.equal(h.byIdPrefix('wt-hand').length, 0, 'no hand tools until something with a number is open');
  await h.click('wt-n-1-1');
  assert.equal(textOf(h, 'wt-hand-target'), 'The bias of hidden neuron 2 is -12.00.');
  assert.equal(h.mod.handInput.value(), '-12', 'the field starts at the number it would change');
  const net = starter(TINY);
  const before = textOf(h, 'wt-cost-input');
  assert.equal(before, cost3(refCost(net, [1, 1], [0])));
  h.mod.handInput.set_value('-9');
  await h.click('wt-hand-set');
  net.layers[0].b[1] = -9;
  assert.equal(localNetwork(TINY).layers[0].b[1], -9, 'the edit went through the adapter');
  assert.equal(textOf(h, 'wt-cost-input'), cost3(refCost(net, [1, 1], [0])));
  assert.ok(Number(textOf(h, 'wt-cost-input')) < Number(before) / 5, 'the cost for this input fell');
  assert.equal(textOf(h, 'wt-cost-test'), cost3(mean(net.test.map((e) => refCost(net, e.x, e.y)))));
  assert.equal(textOf(h, 'wt-cost-test-under'), '4 of 4 right');
  const text = h.text();
  assert.ok(text.includes('You set the bias of layer 1, neuron 2 from -12.00 to -9.00.'), 'it is in the timeline like an agent\'s edit');
  assert.ok(text.includes('Target: Both on'), 'measured against the input that was open');
  assert.ok(text.includes('bias -9.00'));
  // Taken back from the hand tools.
  await h.click('wt-hand-undo');
  assert.equal(localNetwork(TINY).layers[0].b[1], -12);
  assert.equal(textOf(h, 'wt-cost-input'), before);
  assert.ok(h.text().includes('You took back an edit to layer 1, neuron 2.'));
  assert.equal(h.byIdPrefix('wt-hand-undo').length, 0, 'nothing of mine left to take back');
  // Something that is not a number is refused in words, and nothing is sent.
  h.mod.handInput.set_value('lots');
  await h.click('wt-hand-set');
  assert.equal(textOf(h, 'wt-hand-note'), 'Type a number, such as -1.5.');
  assert.equal(localSession(TINY).events.length, 2);
});

test('dragging a weight moves the drawing and the cost at once, and is recorded when let go', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3');
  await h.click('wt-w-1-0-1'); // the weight from hidden 2 into the output: -8
  assert.equal(textOf(h, 'wt-hand-target'), 'The weight from hidden neuron 2 into the output "Exactly one is on" is -8.00.');
  const before = textOf(h, 'wt-cost-input');
  await h.fire('wt-hand-track', 'on_mouse_down', { position: { x: 100, y: 0 } });
  // A quarter of the track to the left: a quarter of the span lower.
  await h.fire('wt-hand-track', 'on_mouse_move', { position: { x: 100 - TRACK_W / 4, y: 0 } });
  const want = -8 - dragSpan(-8) / 4;
  assert.equal(handTarget(h.mod).value, want);
  assert.equal(want, -16);
  assert.ok(h.text().includes('Now -16.00. Let go to keep it.'));
  assert.notEqual(textOf(h, 'wt-cost-input'), before, 'the cost for this input moved while dragging');
  const net = starter(TINY); net.layers[1].W[0][1] = -16;
  assert.equal(textOf(h, 'wt-cost-input'), cost3(refCost(net, [1, 1], [0])));
  assert.equal(localSession(TINY).events.length, 0, 'nothing is recorded until the drag ends');
  assert.equal(localNetwork(TINY).layers[1].W[0][1], -8);
  await h.fire('wt-hand-track', 'on_mouse_up', {});
  assert.equal(localNetwork(TINY).layers[1].W[0][1], -16);
  assert.ok(h.text().includes('You set one weight of layer 2, neuron 1 from -8.00 to -16.00.'), h.text());
  assert.equal(h.mod.handInput.value(), '-16');
  // A press with no movement records nothing.
  await h.fire('wt-hand-track', 'on_mouse_down', { position: { x: 50, y: 0 } });
  await h.fire('wt-hand-track', 'on_mouse_up', {});
  assert.equal(localSession(TINY).events.length, 1);
});

test('halve, double and zero a neuron', async () => {
  const h = await page(TINY);
  await h.click('wt-n-1-0');
  await h.click('wt-hand-halve');
  assert.deepEqual(localNetwork(TINY).layers[0].W[0], [3, 3]);
  assert.ok(h.text().includes('You scaled 2 weights into layer 1, neuron 1, by 0.5.'));
  await h.click('wt-hand-double');
  assert.deepEqual(localNetwork(TINY).layers[0].W[0], [6, 6]);
  await h.click('wt-hand-zero');
  assert.deepEqual([localNetwork(TINY).layers[0].W[0], localNetwork(TINY).layers[0].b[0]], [[0, 0], 0]);
  assert.ok(h.text().includes('You set 2 weights and the bias of layer 1, neuron 1 to zero.'));
  assert.equal(textOf(h, 'wt-ledger-changed'), '3 of 13 numbers changed (23.1%)');
  // An input neuron has no number of its own to change.
  await h.click('wt-n-0-0');
  assert.equal(h.byIdPrefix('wt-hand').length, 0);
});

test('a change the services refuse is put back, and said in plain words', async () => {
  const net = starter(TINY);
  served = {
    '/api/weights/models/tiny': { ok: true, data: { ...net, id: 'tiny', name: 'tiny', test: [] } },
    '/api/weights/models/tiny/session': { ok: true, data: { id: 'ws-1', model: 'tiny', ledger: { raw_weights: true, weights_read: 0, sandbox: false, forward_allowed: 20, forward_used: 0, changed: 0, total: 13 }, events: [] } },
    '/api/weights/sessions/ws-1/edits': { ok: false, error: 'HTTP 500' },
  };
  const h = await page('tiny');
  await h.click('wt-n-1-1');
  h.mod.handInput.set_value('-9');
  await h.click('wt-hand-set');
  const sent = asked.find(([p]) => p === '/api/weights/sessions/ws-1/edits');
  // `target` is the input that was open, sent whole: the services measure the edit on it.
  assert.deepEqual(sent[1].body, { op: 'set', layer: 1, neuron: 1, from: null, value: -9, target: { inputs: [[0, 0]], targets: [[0]] }, by: 'you' }, 'the edit in the services\' shape');
  assert.equal(textOf(h, 'wt-hand-note'), "Captain couldn't make that change.");
  assert.ok(!/HTTP|500/.test(h.text()));
  // A drag that is refused goes back to where it was.
  await h.fire('wt-hand-track', 'on_mouse_down', { position: { x: 100, y: 0 } });
  await h.fire('wt-hand-track', 'on_mouse_move', { position: { x: 150, y: 0 } });
  assert.notEqual(handTarget(h.mod).value, -12);
  await h.fire('wt-hand-track', 'on_mouse_up', {});
  assert.equal(handTarget(h.mod).value, -12);
  served = {};
});

test('"Send to agent" pins what is selected, and the three starters are written around it', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3');
  assert.deepEqual(selectionPin(h.mod), { ref: 'weights:tiny', kind: 'weights', label: 'Tiny network: the whole network' });
  let s = starters(h.mod);
  assert.deepEqual(s.map((x) => x.label), ['Find what this neuron detects', 'Fix this wrong answer with the smallest edit', 'Explain this layer']);
  assert.equal(s[0].needs, 'Open a neuron first.');
  // The three things to ask sit with "Send to agent" at the top of the page, behind one button.
  assert.equal(h.byIdPrefix('wt-ask-').length, 0);
  await h.click('wt-ask');
  assert.equal(h.byIdPrefix('wt-ask-').length, 3);
  assert.ok(!h.byIdPrefix('wt-ask-detects')[0].call('on_click'), 'the first starter waits for a neuron');
  await h.click('wt-n-1-1');
  assert.deepEqual(selectionPin(h.mod), { ref: 'weights:tiny@layer=1,neuron=1', kind: 'weights', label: 'Tiny network: hidden neuron 2 (bias -12.00)' });
  assert.equal(textOf(h, 'wt-agent-about'), 'It will be told about hidden neuron 2 (bias -12.00).');
  s = starters(h.mod);
  assert.ok(s[0].prompt.includes('hidden neuron 2 (layer 1, neuron 2)') && s[0].prompt.includes('cst weights heatmap --layer 1 --neuron 2'));
  assert.ok(s[1].prompt.includes('on the input "Both on". It gets it wrong: it answers') && s[1].prompt.includes('0.00 is wanted'));
  assert.ok(s[1].prompt.includes('cst weights undo') && s[1].prompt.includes('ONE `cst weights edit`'));
  assert.ok(s[1].prompt.includes('cst weights open tiny --agent --blind') && s[1].prompt.includes('cst weights sets --target-example 4'), 'a blind session, and the input on screen as the target, counted from 1');
  assert.ok(s[2].prompt.includes('Explain layer 1 (the weights into column 1, 3 neurons fed by 2)'));
  for (const x of s) assert.ok(x.prompt.includes('say "from the weights alone" only if the session\'s record does'), 'every starter carries the honesty rule');
  // With no agent installed the prompt goes to the clipboard, as on every other screen.
  await h.click('wt-ask-fix');
  await h.idle(20);
  assert.ok(String(globalThis.__clipboard).includes('Fix a wrong answer with the smallest edit'), String(globalThis.__clipboard).slice(0, 200));
  // Send to agent: the pin is written to the agent's context file.
  await h.click('wt-send');
  await h.idle(20);
  const file = Object.entries(globalThis.__files).find(([p]) => p.includes('/context/'));
  assert.ok(file, 'a context file was written');
  assert.deepEqual(JSON.parse(file[1]).items.map((i) => [i.ref, i.kind, i.label]), [['weights:tiny@layer=1,neuron=1', 'weights', 'Tiny network: hidden neuron 2 (bias -12.00)']]);
  // A picked weight is what gets sent.
  await h.click('wt-w-1-0-1');
  assert.equal(selectionPin(h.mod).ref, 'weights:tiny@layer=2,neuron=0,from=1');
  // On an input that is right, the fix prompt does not claim a mistake.
  await h.click('wt-in-0');
  assert.ok(starters(h.mod)[1].prompt.includes('Check whether that is right before changing anything.'));
});

test('one filled button at most, on every state of the page', async () => {
  const fs = await import('node:fs');
  for (const f of fs.readdirSync('defaults/mods/captain-weights').filter((x) => x.endsWith('.js'))) {
    assert.ok(!/kind:\s*"primary"/.test(fs.readFileSync(`defaults/mods/captain-weights/${f}`, 'utf8')), `${f} has a filled button`);
  }
  const h = await page(DIGITS);
  await h.click('wt-n-2-3');
  h.mod.pickWeight({ k: 1, j: 3, i: 0 }, h.cx);
  await h.click('wt-walk-next');
  await h.click('wt-ask');
  assert.ok(h.byIdPrefix('wt-hand').length > 0 && h.byIdPrefix('wt-chain').length > 0 && h.byIdPrefix('wt-ask-').length === 3);
});
