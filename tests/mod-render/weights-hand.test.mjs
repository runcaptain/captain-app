// Changing a network by hand on the Weights page: type a value, drag a number, halve, double or zero a neuron.
// Each only stages a change, previewed and marked as not saved. Nothing is sent until its save button is pressed.
// Take an edit back; and handing the selection to an agent: a pin for its context and
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

/** Type into the number box, as a person would: the text, then the box's "change". */
async function type(h, text) { h.mod.handInput.set_value(text); h.mod.handInput.handlers.change({}, h.cx); await h.idle(1); }
const sentEdits = () => asked.filter(([p]) => /\/edits$/.test(p)).length;

test('typing a value only stages it: nothing changes until "Set this value", which sends exactly one edit', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3'); // "Both on": wrong
  assert.equal(h.byIdPrefix('wt-hand').length, 0, 'no hand tools until something with a number is open');
  await h.click('wt-n-1-1');
  assert.equal(textOf(h, 'wt-hand-target'), 'The bias of hidden neuron 2 is -12.00.');
  assert.equal(h.mod.handInput.value(), '-12', 'the field starts at the number it would change');
  assert.equal(h.byIdPrefix('wt-hand-set').length, 0, 'no save button while nothing is staged');
  const net = starter(TINY);
  const before = textOf(h, 'wt-cost-input');
  assert.equal(before, cost3(refCost(net, [1, 1], [0])));
  const drawn = () => JSON.stringify(h.mod.drawing().net.layers);
  const drawnBefore = drawn();
  await type(h, '-9');
  // Staged: said in a line, previewed in the drawing and the cost (marked), and not in the network.
  assert.equal(textOf(h, 'wt-hand-unsaved'), 'Not saved. -12.00 will become -9.00.');
  assert.equal(textOf(h, 'wt-hand-target'), 'The bias of hidden neuron 2 is -12.00.', 'the saved value is still what the network holds');
  // The neuron's sentence stays on the saved numbers. Only the figures labelled "if saved" preview the change.
  assert.ok(textOf(h, 'wt-inspect-says').includes('Its bias is -12.00'), textOf(h, 'wt-inspect-says'));
  assert.equal(textOf(h, 'wt-inspect-b'), '-9.00');
  assert.ok(h.text().includes('Bias, if saved'));
  net.layers[0].b[1] = -9;
  assert.equal(textOf(h, 'wt-cost-input'), `if saved: ${cost3(refCost(net, [1, 1], [0]))}`);
  assert.equal(textOf(h, 'wt-cost-input-under'), `not saved. Now ${before}`);
  assert.equal(textOf(h, 'wt-staged-says'), 'Not saved: this is a preview.');
  assert.equal(h.mod.drawing().net.layers[0].b[1], -9, 'the drawing previews it');
  assert.equal(localNetwork(TINY).layers[0].b[1], -12, 'the network is untouched');
  assert.equal(h.mod.net.layers[0].b[1], -12, 'the page\'s own copy is untouched');
  assert.equal(localSession(TINY).events.length, 0, 'nothing in the timeline');
  assert.equal(h.byIdPrefix('wt-cost-mark-').length, 0, 'no mark on the cost curve');
  // Enter in the box does not save.
  h.mod.handInput.handlers.submit({}, h.cx);
  await h.idle(5);
  assert.equal(localSession(TINY).events.length, 0, 'Enter saves nothing');
  assert.equal(localNetwork(TINY).layers[0].b[1], -12);
  assert.equal(textOf(h, 'wt-hand-unsaved'), 'Not saved. -12.00 will become -9.00.');
  // Put it back: the box, the drawing and the cost return, and nothing was sent.
  await h.click('wt-hand-back');
  assert.equal(h.mod.handInput.value(), '-12');
  assert.equal(drawn(), drawnBefore, 'the drawing returns');
  assert.equal(textOf(h, 'wt-cost-input'), before);
  assert.equal(h.byIdPrefix('wt-hand-unsaved').length + h.byIdPrefix('wt-hand-set').length, 0);
  assert.equal(localSession(TINY).events.length, 0);
  // Escape does the same.
  await type(h, '-9');
  assert.equal(h.byIdPrefix('wt-hand-set').length, 1);
  await h.key('weights', 'escape');
  assert.equal(h.mod.staged, null);
  assert.equal(h.mod.handInput.value(), '-12');
  assert.equal(localSession(TINY).events.length, 0);
  // "Set this value" saves it, as one edit.
  await type(h, '-9');
  await h.click('wt-hand-set');
  assert.equal(localNetwork(TINY).layers[0].b[1], -9, 'the edit went through the adapter');
  assert.equal(localSession(TINY).events.filter((e) => e.kind === 'edit').length, 1, 'exactly one edit');
  assert.equal(h.mod.staged, null);
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
  // Something that is not a number stages nothing, and says what to type.
  await type(h, 'lots');
  assert.equal(textOf(h, 'wt-hand-typo'), 'Type a number, such as -1.5.');
  assert.equal(h.byIdPrefix('wt-hand-set').length, 0);
  assert.equal(localSession(TINY).events.length, 2);
});

test('dragging a weight previews it and sends nothing, even when let go', async () => {
  const h = await page(TINY);
  await h.click('wt-in-3');
  await h.click('wt-w-1-0-1'); // the weight from hidden 2 into the output: -8
  assert.equal(textOf(h, 'wt-hand-target'), 'The weight from hidden neuron 2 into the output "Exactly one is on" is -8.00.');
  const before = textOf(h, 'wt-cost-input');
  await h.fire('wt-hand-track', 'on_mouse_down', { position: { x: 100, y: 0 } });
  // A quarter of the track to the left: a quarter of the span lower.
  await h.fire('wt-hand-track', 'on_mouse_move', { position: { x: 100 - TRACK_W / 4, y: 0 } });
  const want = -8 - dragSpan(-8) / 4;
  assert.equal(want, -16);
  assert.equal(h.mod.staged.value, want);
  assert.equal(handTarget(h.mod).value, -8, 'the page\'s network still holds the saved value');
  assert.equal(h.mod.handInput.value(), '-16', 'the box shows the new value');
  assert.equal(textOf(h, 'wt-hand-unsaved'), 'Not saved. -8.00 will become -16.00.');
  const net = starter(TINY); net.layers[1].W[0][1] = -16;
  assert.equal(textOf(h, 'wt-cost-input'), `if saved: ${cost3(refCost(net, [1, 1], [0]))}`, 'the cost for this input previews it');
  await h.fire('wt-hand-track', 'on_mouse_up', {});
  // Let go: still only staged.
  assert.equal(localSession(TINY).events.length, 0, 'a drag sends nothing');
  assert.equal(localNetwork(TINY).layers[1].W[0][1], -8);
  assert.equal(textOf(h, 'wt-hand-unsaved'), 'Not saved. -8.00 will become -16.00.');
  assert.equal(h.byIdPrefix('wt-hand-set').length, 1);
  // A second drag goes on from the staged value.
  await h.fire('wt-hand-track', 'on_mouse_down', { position: { x: 50, y: 0 } });
  await h.fire('wt-hand-track', 'on_mouse_move', { position: { x: 50 + TRACK_W / 8, y: 0 } });
  await h.fire('wt-hand-track', 'on_mouse_up', {});
  assert.equal(h.mod.staged.value, -12);
  assert.equal(localSession(TINY).events.length, 0);
  // Saved by the button, as one edit.
  await h.click('wt-hand-set');
  assert.equal(localNetwork(TINY).layers[1].W[0][1], -12);
  assert.equal(localSession(TINY).events.length, 1);
  assert.ok(h.text().includes('You set one weight of layer 2, neuron 1 from -8.00 to -12.00.'), h.text());
  assert.equal(h.mod.handInput.value(), '-12');
  // A press with no movement stages nothing.
  await h.fire('wt-hand-track', 'on_mouse_down', { position: { x: 50, y: 0 } });
  await h.fire('wt-hand-track', 'on_mouse_up', {});
  assert.equal(h.mod.staged, null);
  assert.equal(localSession(TINY).events.length, 1);
  assert.equal(textOf(h, 'wt-cost-input'), cost3(refCost(localNetwork(TINY), [1, 1], [0])));
  assert.notEqual(textOf(h, 'wt-cost-input'), before);
});

test('halve, double and zero a neuron: each waits for "Save this change"', async () => {
  const h = await page(TINY);
  await h.click('wt-n-1-0');
  for (const [id, says, preview] of [
    ['wt-hand-halve', 'Not saved. 2 weights into this neuron will be halved.', [3, 3]],
    ['wt-hand-double', 'Not saved. 2 weights into this neuron will be doubled.', [12, 12]],
    ['wt-hand-zero', 'Not saved. 2 weights into this neuron and its bias will be set to zero.', [0, 0]],
  ]) {
    await h.click(id);
    assert.equal(textOf(h, 'wt-hand-unsaved'), says);
    assert.deepEqual(h.mod.drawing().net.layers[0].W[0], preview, 'the drawing previews it');
    assert.ok(textOf(h, 'wt-cost-input').startsWith('if saved: '));
    assert.deepEqual(localNetwork(TINY).layers[0].W[0], [6, 6], `${id} sends nothing`);
    assert.equal(localSession(TINY).events.length, 0);
    await h.click('wt-hand-back');
    assert.deepEqual(h.mod.drawing().net.layers[0].W[0], [6, 6], 'put back');
    assert.equal(h.byIdPrefix('wt-hand-save').length, 0);
    assert.equal(localSession(TINY).events.length, 0);
  }
  await h.click('wt-hand-halve');
  await h.click('wt-hand-save');
  assert.deepEqual(localNetwork(TINY).layers[0].W[0], [3, 3]);
  assert.equal(localSession(TINY).events.length, 1, 'one edit');
  assert.ok(h.text().includes('You scaled 2 weights into layer 1, neuron 1, by 0.5.'));
  await h.click('wt-hand-double');
  await h.click('wt-hand-save');
  assert.deepEqual(localNetwork(TINY).layers[0].W[0], [6, 6]);
  await h.click('wt-hand-zero');
  await h.click('wt-hand-save');
  assert.deepEqual([localNetwork(TINY).layers[0].W[0], localNetwork(TINY).layers[0].b[0]], [[0, 0], 0]);
  assert.ok(h.text().includes('You set 2 weights and the bias of layer 1, neuron 1 to zero.'));
  assert.equal(textOf(h, 'wt-ledger-changed'), '3 of 13 numbers changed (23.1%)');
  assert.equal(localSession(TINY).events.length, 3);
  // An input neuron has no number of its own to change.
  await h.click('wt-n-0-0');
  assert.equal(h.byIdPrefix('wt-hand').length, 0);
});

test('leaving with a change staged drops it, says so once, and saves nothing', async () => {
  const h = await page(TINY);
  await h.click('wt-n-1-1');
  // Another neuron.
  await type(h, '-9');
  await h.click('wt-n-1-0');
  assert.equal(h.mod.staged, null);
  assert.equal(textOf(h, 'wt-staged-says'), 'Your unsaved change to the bias of hidden neuron 2 was dropped. Nothing was saved.');
  assert.equal(h.mod.handInput.value(), '-3', 'the box is the newly opened neuron\'s saved bias');
  // Another input. The line about the earlier drop is gone once something else is picked.
  await h.click('wt-hand-zero');
  await h.click('wt-in-2');
  assert.equal(h.mod.staged, null);
  assert.equal(textOf(h, 'wt-staged-says'), 'Your unsaved change to the weights of hidden neuron 1 was dropped. Nothing was saved.');
  await h.click('wt-in-1');
  assert.equal(textOf(h, 'wt-staged-says'), '');
  // Closing the neuron (a click on it again).
  await type(h, '2');
  await h.click('wt-n-1-0');
  assert.equal(h.mod.sel, null);
  assert.equal(textOf(h, 'wt-staged-says'), 'Your unsaved change to the bias of hidden neuron 1 was dropped. Nothing was saved.');
  // Back to the list of networks.
  await h.click('wt-w-1-0-1');
  await type(h, '3');
  await h.click('wt-back');
  assert.equal(textOf(h, 'wt-dropped'), 'Your unsaved change to the weight from hidden neuron 2 into the output "Exactly one is on" was dropped. Nothing was saved.');
  // Another network.
  await h.click(`wt-open-btn-${TINY}`);
  assert.equal(h.mod.dropped, null);
  await h.click('wt-n-1-1');
  await type(h, '-9');
  h.mod.open(TINY, h.cx);
  await h.idle(20);
  assert.equal(h.mod.staged, null);
  assert.equal(textOf(h, 'wt-staged-says'), 'Your unsaved change to the bias of hidden neuron 2 was dropped. Nothing was saved.');
  assert.equal(localSession(TINY).events.length, 0, 'nothing was ever sent');
  assert.deepEqual(localNetwork(TINY).layers, starter(TINY).layers);
  assert.equal(sentEdits(), 0);
});

test('a change the services refuse stays staged, and is said in plain words', async () => {
  const net = starter(TINY);
  served = {
    '/api/weights/models/tiny': { ok: true, data: { ...net, id: 'tiny', name: 'tiny', test: [] } },
    '/api/weights/models/tiny/session': { ok: true, data: { id: 'ws-1', model: 'tiny', ledger: { raw_weights: true, weights_read: 0, sandbox: false, forward_allowed: 20, forward_used: 0, changed: 0, total: 13 }, events: [] } },
    '/api/weights/sessions/ws-1/edits': { ok: false, error: 'HTTP 500' },
  };
  const h = await page('tiny');
  await h.click('wt-n-1-1');
  await type(h, '-9');
  h.mod.handInput.handlers.submit({}, h.cx);
  await h.idle(5);
  assert.equal(sentEdits(), 0, 'typing and Enter ask the services for nothing');
  await h.click('wt-hand-set');
  assert.equal(sentEdits(), 1, 'one request');
  const sent = asked.find(([p]) => p === '/api/weights/sessions/ws-1/edits');
  // `target` is the input that was open, sent whole: the services measure the edit on it.
  assert.deepEqual(sent[1].body, { op: 'set', layer: 1, neuron: 1, from: null, value: -9, target: { inputs: [[0, 0]], targets: [[0]] }, by: 'you' }, 'the edit in the services\' shape');
  assert.equal(textOf(h, 'wt-hand-note'), "Captain couldn't make that change.");
  assert.ok(!/HTTP|500/.test(h.text()));
  assert.equal(handTarget(h.mod).value, -12, 'the page never took the value as saved');
  assert.equal(textOf(h, 'wt-hand-unsaved'), 'Not saved. -12.00 will become -9.00.', 'still staged, to try again or put back');
  await h.click('wt-hand-back');
  // A drag asks for nothing, and neither do the three neuron actions.
  await h.fire('wt-hand-track', 'on_mouse_down', { position: { x: 100, y: 0 } });
  await h.fire('wt-hand-track', 'on_mouse_move', { position: { x: 150, y: 0 } });
  await h.fire('wt-hand-track', 'on_mouse_up', {});
  for (const id of ['wt-hand-halve', 'wt-hand-double', 'wt-hand-zero']) await h.click(id);
  assert.equal(sentEdits(), 1);
  await h.click('wt-hand-save');
  assert.deepEqual(asked.filter(([p]) => /\/edits$/.test(p)).at(-1)[1].body, { op: 'zero', layer: 1, neuron: 1, target: { inputs: [[0, 0]], targets: [[0]] }, by: 'you' });
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
