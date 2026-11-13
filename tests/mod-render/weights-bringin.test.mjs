// "Bring in a trained model" on the Weights page's list: the person says once where their trained models are
// kept, the services list what is there, the person chooses one, names it and presses "Bring it in". The place
// is kept in a file in the app's own folder, never drawn on the page, and nothing is copied before that press.
// Every path and name here is made up.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-bringin.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { pathTail, nameFor } = await import('../../defaults/mods/captain-weights/bringin.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

const PLACE = '/made-up/models', KEPT = '/tmp/captain-app/captain/weights-models-from.json';
let asked = [], models, refuse = null;
const versions = [
  { path: 'arch-a/shop/v2', address: `${PLACE}/arch-a/shop/v2`, name: 'v2', description: 'wider, two layers', has_manifest: true, files: 10 },
  { path: 'arch-a/shop/v1', address: `${PLACE}/arch-a/shop/v1`, name: 'v1', description: null, has_manifest: false, files: 9 },
];
weightsTransport(async (path, opts) => {
  asked.push([path, opts?.method ?? 'GET', opts?.body]);
  const [p, q = ''] = path.split('?');
  if (p === '/api/weights/models') return { ok: true, data: models };
  if (p === '/api/weights/registry') return decodeURIComponent(q.replace('from=', '')) === PLACE ? { ok: true, data: { from: PLACE, count: 2, listed: 2, read_only: true, versions } }
    : { ok: false, error: `There is no folder at ${decodeURIComponent(q.replace('from=', ''))}.` };
  if (p === '/api/weights/import') {
    if (refuse) return { ok: false, error: refuse };
    models = [...models, { id: opts.body.name, name: opts.body.name, kind: 'session-ranker', kind_says: 'session model with rankers', parameters: 2281, says: 'A session model.' }];
    return { ok: true, data: { model: opts.body.name, version: 1, ref: `${opts.body.name}@v1`, kind: 'session-ranker' } };
  }
  return { ok: false, error: 'Not Found' };
});
async function page(files = {}) {
  localReset(); liveReset(); fakeFeed(liveTransport); asked = []; refuse = null;
  models = [{ id: 'tiny', name: 'tiny', kind: 'dense', kind_says: 'fully connected network', says: 'A tiny network.', sizes: [2, 3, 1] }];
  globalThis.__props = {}; globalThis.__files = files; globalThis.__written = []; globalThis.__toasts = [];
  globalThis.__exec = async () => ({ code: 0, stdout: '', stderr: '' });
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const imports = () => asked.filter(([p]) => p === '/api/weights/import');

test('the first time it asks where the models are kept, keeps it, and never draws it again', async () => {
  const h = await page();
  assert.equal(textOf(h, 'wt-bring'), 'Bring in a trained model');
  await h.click('wt-bring');
  await h.idle(10);
  assert.ok(h.text().includes('Where your trained models are kept'));
  // Nothing typed: said in words, nothing asked.
  await h.click('wt-bring-keep');
  assert.equal(textOf(h, 'wt-bring-problem'), 'Type the folder or the storage address first.');
  h.mod.whereInput.set_value(PLACE);
  await h.click('wt-bring-keep');
  await h.idle(10);
  assert.deepEqual(JSON.parse(globalThis.__files[KEPT]), { from: PLACE }, 'kept in the app\'s own folder on this computer');
  assert.equal(asked.at(-1)[0], `/api/weights/registry?from=${encodeURIComponent(PLACE)}`);
  assert.equal(textOf(h, 'wt-bring-count'), '2 models can be brought in from where yours are kept.');
  assert.equal(h.mod.whereInput.value(), '', 'the box is emptied');
  assert.ok(!h.text().includes(PLACE), 'the place itself is not on the page');
  // A row: the last three parts of its path, and its one-line description.
  assert.ok(h.text().includes('arch-a/shop/v2') && h.text().includes('wider, two layers') && h.text().includes('No description was saved with it.'));
  assert.equal(pathTail('one/two/three/four/v9'), 'three/four/v9');
  assert.equal(imports().length, 0);
});

test('choosing one asks for a name, and only "Bring it in" copies it', async () => {
  const h = await page({ [KEPT]: JSON.stringify({ from: PLACE }) });
  await h.click('wt-bring');
  await h.idle(10);
  assert.equal(textOf(h, 'wt-bring-count'), '2 models can be brought in from where yours are kept.', 'the kept place is used: it is not asked for again');
  await h.click('wt-bring-choose-0');
  assert.equal(textOf(h, 'wt-bring-picked'), 'Bring in arch-a/shop/v2');
  assert.equal(textOf(h, 'wt-bring-copies'), "This copies the model's files into this workspace. Nothing is written where they are kept.");
  assert.equal(h.mod.nameInput.value(), 'shop-v2', 'a name is suggested from its path');
  assert.equal(nameFor('Arch A/Shop Model/v2'), 'shop-model-v2');
  assert.equal(h.mod.nameInput.handlers.submit, undefined, 'Enter in the name box does nothing');
  assert.equal(imports().length, 0, 'choosing copies nothing');
  // A name the services would not take is refused in words.
  h.mod.nameInput.set_value('Shop Model');
  await h.click('wt-bring-go');
  assert.ok(textOf(h, 'wt-bring-problem').startsWith('Give it a name in small letters'));
  assert.equal(imports().length, 0);
  // A refusal from the services, with the place taken out of it.
  h.mod.nameInput.set_value('shop-model');
  refuse = `The folder ${PLACE}/arch-a/shop/v2 has no model.json.`;
  await h.click('wt-bring-go');
  assert.equal(textOf(h, 'wt-bring-problem'), 'The folder that place/arch-a/shop/v2 has no model.json.');
  refuse = null;
  await h.click('wt-bring-go');
  assert.deepEqual(imports().at(-1), ['/api/weights/import', 'POST', { folder: `${PLACE}/arch-a/shop/v2`, name: 'shop-model' }]);
  assert.equal(textOf(h, 'wt-bring-done'), 'Brought in as shop-model. Its files were copied into this workspace.');
  assert.equal(h.byIdPrefix('wt-open-btn-shop-model').length, 1, 'it is in the list');
  assert.ok(!h.text().includes(PLACE));
  await h.click('wt-bring-close');
  assert.equal(textOf(h, 'wt-bring'), 'Bring in a trained model');
});

test('a place that cannot be listed says so without naming it, and can be changed', async () => {
  const h = await page({ [KEPT]: JSON.stringify({ from: '/made-up/gone' }) });
  await h.click('wt-bring');
  await h.idle(10);
  assert.equal(textOf(h, 'wt-bring-problem'), 'There is no folder at that place.');
  await h.click('wt-bring-change');
  assert.ok(h.text().includes('Where your trained models are kept'));
  h.mod.whereInput.set_value(PLACE);
  await h.click('wt-bring-keep');
  await h.idle(10);
  assert.deepEqual(JSON.parse(globalThis.__files[KEPT]), { from: PLACE });
  assert.equal(h.byIdPrefix('wt-bring-choose-').length, 2);
});
