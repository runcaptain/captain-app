// "Save this version" on the Weights page: changes made in a session stay in the session until a person presses
// it. The page says how many changes are in no version, saves them on the press and on nothing else, and then
// says the version's name and number.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-save.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { starter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { unsavedOf, versionWords } = await import('../../defaults/mods/captain-weights/save.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

const SID = 'ws-1';
let asked = [], events = [], changed = 0, refuse = null, net;
weightsTransport(async (path, opts) => {
  const p = path.split('?')[0];
  asked.push([p, opts?.method ?? 'GET', opts?.body]);
  if (p === '/api/weights/models/tiny') return { ok: true, data: { ...net, id: 'tiny', name: 'tiny', test: [] } };
  if (p === '/api/weights/models/tiny/session') return { ok: true, data: { id: SID, model: 'tiny', ledger: { raw_weights: true, weights_read: 0, sandbox: false, forward_allowed: 20, forward_used: 0, changed, total: 13 }, events } };
  if (p === `/api/weights/sessions/${SID}/edits`) {
    net.layers[0].b[1] = opts.body.value; changed = 1;
    const ev = { id: `e${events.length + 1}`, seq: events.length + 1, by: 'you', kind: 'edit', op: 'set', layer: 1, neuron: 1, changes: [{ k: 0, j: 1, i: null, old: -12, new: opts.body.value }], touched: { neurons: [[1, 1]], weights: [] } };
    events = [...events, ev];
    return { ok: true, data: ev };
  }
  if (p === `/api/weights/sessions/${SID}/save`) {
    if (refuse) return { ok: false, error: refuse };
    events = [...events, { id: `e${events.length + 1}`, seq: events.length + 1, by: 'you', kind: 'save', sentence: 'You saved the network as tiny@v2.' }];
    return { ok: true, data: { model: 'tiny', version: 2, ref: 'tiny@v2', base: 'tiny@v1', patches: 1, changed: 1 } };
  }
  return { ok: false, error: 'Not Found' };
});
async function page(model = 'tiny') {
  localReset(); liveReset(); fakeFeed(liveTransport); asked = []; events = []; changed = 0; refuse = null; net = starter(TINY);
  globalThis.__props = { model }; globalThis.__files = {}; globalThis.__toasts = [];
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const saves = () => asked.filter(([p, m]) => m === 'POST' && p.endsWith('/save')).length;

test('a saved edit stays in the session until "Save this version", which makes the version and says its name', async () => {
  const h = await page();
  assert.equal(h.byIdPrefix('wt-save').length, 0, 'nothing to save: no line');
  await h.click('wt-n-1-1');
  h.mod.handInput.set_value('-9'); h.mod.handInput.handlers.change({}, h.cx); await h.idle(1);
  assert.equal(h.byIdPrefix('wt-save').length, 0, 'a staged change is not a change yet');
  await h.click('wt-hand-set');
  assert.equal(unsavedOf(h.mod), 1);
  assert.equal(textOf(h, 'wt-save-text'), '1 change not saved as a version.');
  assert.equal(saves(), 0, 'setting a value saves no version');
  await h.click('wt-save-go');
  assert.equal(saves(), 1);
  assert.deepEqual(asked.find(([p]) => p.endsWith('/save')), [`/api/weights/sessions/${SID}/save`, 'POST', { note: '', by: 'you' }]);
  assert.equal(textOf(h, 'wt-save-text'), 'Saved as tiny, version 2.');
  assert.equal(h.byIdPrefix('wt-save-go').length, 0, 'nothing left to save');
  assert.equal(versionWords('shop-model@v12'), 'shop-model, version 12');
  await h.click('wt-save-close');
  assert.equal(h.byIdPrefix('wt-save').length, 0);
  // Another edit after the save is unsaved again.
  h.mod.handInput.set_value('-8'); h.mod.handInput.handlers.change({}, h.cx); await h.idle(1);
  await h.click('wt-hand-set');
  assert.equal(textOf(h, 'wt-save-text'), '1 change not saved as a version.');
  assert.equal(saves(), 1);
});

test('an agent\'s kept result is counted too, and a refusal is said in plain words', async () => {
  const h = await page();
  changed = 3;
  events = [{ id: 'e1', seq: 1, by: 'agent', kind: 'edit', op: 'set', layer: 1, neuron: 1, changes: [], changed: 3, touched: { neurons: [], weights: [] } },
    { id: 'e2', seq: 2, by: 'captain', kind: 'program', sentence: "Captain's verdict: Kept. The target improved, and the rest held." }];
  h.mod.read(h.cx); await h.idle(30);
  assert.equal(textOf(h, 'wt-save-text'), '3 changes not saved as a version.');
  refuse = 'There is nothing to save: the network is the same as its base.';
  await h.click('wt-save-go');
  assert.equal(textOf(h, 'wt-save-text'), refuse);
  assert.ok(!/HTTP|undefined/.test(h.text()));
  refuse = null;
  await h.click('wt-save-go');
  assert.equal(textOf(h, 'wt-save-text'), 'Saved as tiny, version 2.');
});

test('the network kept in the page has no versions, so nothing is offered', async () => {
  const h = await page(TINY);
  await h.click('wt-n-1-1');
  h.mod.handInput.set_value('-9'); h.mod.handInput.handlers.change({}, h.cx); await h.idle(1);
  await h.click('wt-hand-set');
  assert.equal(h.byIdPrefix('wt-save').length, 0);
});
