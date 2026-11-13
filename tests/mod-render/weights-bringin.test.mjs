// "Bring in a trained model" on the Weights page's list. It opens straight onto what the workspace's own model
// registries hold, grouped by registry: nobody types an address that Captain already has. A version that is
// already here is opened, one that cannot be opened says so, a box filters the rows, and only "Bring it in"
// copies anything. With no registry connected it offers to connect one. A folder kept somewhere else is behind
// "From somewhere else". Every path, registry and name here is made up.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-bringin.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { pathTail, nameFor, filtered } = await import('../../defaults/mods/captain-weights/bringin.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

const PLACE = '/made-up/models', KEPT = '/tmp/captain-app/captain/weights-models-from.json';
const ver = (brand, version, more = {}) => ({ id: `${brand}@${version}`, name: `${brand}, ${version}`, brand, architecture: 'arch-a', version, path: `arch-a/${brand}/${version}`,
  address: `/made-up/registry/arch-a/${brand}/${version}`, description: null, made: null, files: 10, can_open: true, why_not: null, here: null, ...more });
const TWO = [
  { id: 'reg-1', name: 'Model registry 1', read: true, count: 3, versions: [ver('shop', 'v2', { description: 'wider, two layers', made: '2026-11-12' }),
    ver('garden', 'v1', { description: 'a ranker alone', can_open: false, why_not: 'This version holds no session model file.' }), ver('shop', 'v1', { description: 'the first one kept', here: 'shop-first' })] },
  { id: 'reg-2', name: 'Kept by the team', read: true, count: 1, versions: [ver('trail', 'v4', { description: 'trail gear, fourth try', address: '/made-up/other/arch-a/trail/v4' })] },
];
const folderRows = [{ path: 'arch-a/shop/v2', address: `${PLACE}/arch-a/shop/v2`, name: 'v2', description: 'wider, two layers', has_manifest: true, files: 10 }];
let asked = [], models, refuse = null, attached;
weightsTransport(async (path, opts) => {
  asked.push([path, opts?.method ?? 'GET', opts?.body]);
  const [p, q = ''] = path.split('?');
  if (p === '/api/weights/models') return { ok: true, data: models };
  if (p === '/api/weights/registry' && !q) return attached === 'broken' ? { ok: false, error: 'HTTP 500' } : { ok: true, data: { registries: attached, count: 4, can_open: 3, read_only: true } };
  if (p === '/api/weights/registry') return decodeURIComponent(q.replace('from=', '')) === PLACE ? { ok: true, data: { from: PLACE, count: 1, listed: 1, read_only: true, versions: folderRows } }
    : { ok: false, error: `There is no folder at ${decodeURIComponent(q.replace('from=', ''))}.` };
  if (p === '/api/weights/import') {
    if (refuse) return { ok: false, error: refuse };
    models = [...models, { id: opts.body.name, name: opts.body.name, kind: 'session-ranker', kind_says: 'session model with rankers', parameters: 2281, says: 'A session model.' }];
    return { ok: true, data: { model: opts.body.name, version: 1, ref: `${opts.body.name}@v1`, kind: 'session-ranker' } };
  }
  return { ok: false, error: 'Not Found' };
});
async function page(regs = TWO, files = {}) {
  localReset(); liveReset(); fakeFeed(liveTransport); asked = []; refuse = null; attached = regs;
  models = [{ id: 'tiny', name: 'tiny', kind: 'dense', kind_says: 'fully connected network', says: 'A tiny network.', sizes: [2, 3, 1] },
    { id: 'shop-first', name: 'shop-first', kind: 'session-ranker', kind_says: 'session model with rankers', parameters: 2281, says: 'A session model.' }];
  globalThis.__props = {}; globalThis.__files = files; globalThis.__written = []; globalThis.__toasts = []; globalThis.__runs = []; globalThis.__shared = {};
  globalThis.__exec = async () => ({ code: 0, stdout: '', stderr: '' });
  const h = harness(Weights);
  await h.start();
  return h;
}
const one = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id);
const textIn = (e) => { const out = []; (function w(x) { if (typeof x === 'string' || typeof x === 'number') out.push(String(x)); else for (const k of x?.kids ?? []) w(k); })(e); return out.join(' '); };
const textOf = (h, id) => textIn(one(h, id)[0]);
const imports = () => asked.filter(([p]) => p === '/api/weights/import');
const rows = (h) => h.byIdPrefix('wt-bring-v-').filter((e) => /^wt-bring-v-\d+-\d+$/.test(e.call('id')[0])).map((e) => textIn(e));

test('it opens straight onto the workspace\'s registries, by name, with no address asked for or shown', async () => {
  const h = await page();
  await h.click('wt-bring');
  await h.idle(10);
  assert.deepEqual(asked.filter(([p]) => p.startsWith('/api/weights/registry')).map(([p]) => p), ['/api/weights/registry'], 'asked with no address');
  assert.ok(!h.text().includes('Where your') && h.byIdPrefix('wt-bring-where').length === 0, 'nothing to type');
  // Each registry is a group with its plain name and its count, and each version a 40 px row.
  assert.deepEqual([textOf(h, 'wt-bring-g-0'), textOf(h, 'wt-bring-g-1')], ['Model registry 1 3', 'Kept by the team 1']);
  assert.equal(textOf(h, 'wt-bring-count'), '4 versions in your model registries. Newest first.');
  assert.deepEqual(rows(h).map((r) => r.split(' ').slice(0, 2).join(' ')), ['shop, v2', 'garden, v1', 'shop, v1', 'trail, v4'], 'as the services ordered them: newest first');
  assert.equal(one(h, 'wt-bring-v-0-0')[0].call('min_h')[0], 40);
  assert.ok(rows(h)[0].includes('wider, two layers') && rows(h)[0].endsWith('Choose this one'));
  assert.ok(!/made-up/.test(h.text()), 'no address or path of a registry on the page');
  // A group folds, and unfolds.
  await h.click('wt-bring-g-0');
  assert.deepEqual(rows(h).map((r) => r.slice(0, 9)), ['trail, v4']);
  await h.click('wt-bring-g-0');
  assert.equal(rows(h).length, 4);
});

test('a version already here is opened, not copied, and one that cannot be opened says why', async () => {
  const h = await page();
  await h.click('wt-bring');
  await h.idle(10);
  // Cannot be opened: a capsule, the reason on hover, and no action.
  assert.equal(textOf(h, 'wt-bring-v-0-1-cannot'), 'Cannot be opened here');
  assert.equal(one(h, 'wt-bring-v-0-1-cannot')[0].call('tooltip')[0], 'This version holds no session model file.');
  assert.equal(h.byIdPrefix('wt-bring-v-0-1-choose').length + h.byIdPrefix('wt-bring-v-0-1-open').length, 0);
  // Already here: a capsule, and the action is "Open it".
  assert.equal(textOf(h, 'wt-bring-v-0-2-here'), 'Already here as shop-first');
  assert.equal(textOf(h, 'wt-bring-v-0-2-open'), 'Open it');
  assert.equal(h.byIdPrefix('wt-bring-v-0-2-choose').length, 0);
  await h.click('wt-bring-v-0-2-open');
  assert.equal(h.mod.model, 'shop-first', 'the model that is here opens');
  assert.equal(h.mod.bring, null);
  assert.equal(imports().length, 0, 'nothing is copied');
});

test('the filter narrows the rows, and only "Bring it in" calls the import route', async () => {
  const h = await page();
  await h.click('wt-bring');
  await h.idle(10);
  h.mod.filterInput.set_value('shop wider'); h.mod.filterInput.handlers.change({}, h.cx); await h.idle(1);
  assert.deepEqual(rows(h).map((r) => r.slice(0, 8)), ['shop, v2']);
  assert.equal(textOf(h, 'wt-bring-count'), '1 of 4 versions match. Newest first.');
  assert.deepEqual([textOf(h, 'wt-bring-g-0-count'), textOf(h, 'wt-bring-g-1-count')], ['1', '0']);
  assert.ok(h.text().includes('Nothing here matches.'));
  assert.deepEqual(filtered(TWO[0].versions, 'FIRST').map((v) => v.id), ['shop@v1'], 'by description or the name it has here, whatever the case');
  // Choose, name, then the deliberate press.
  await h.click('wt-bring-v-0-0-choose');
  assert.equal(textOf(h, 'wt-bring-picked'), 'Bring in shop, v2');
  assert.equal(textOf(h, 'wt-bring-copies'), "This copies the model's files into this workspace. Nothing is written where they are kept.");
  assert.equal(h.mod.nameInput.value(), 'shop-v2', 'a name is suggested');
  assert.equal(h.mod.nameInput.handlers.submit, undefined, 'Enter in the name box does nothing');
  assert.equal(imports().length, 0, 'opening, filtering and choosing copy nothing');
  h.mod.nameInput.set_value('Shop Model');
  await h.click('wt-bring-go');
  assert.ok(textOf(h, 'wt-bring-problem').startsWith('Give it a name in small letters'));
  h.mod.nameInput.set_value('shop-model');
  refuse = 'The folder /made-up/registry/arch-a/shop/v2 has no model.json.';
  await h.click('wt-bring-go');
  assert.equal(textOf(h, 'wt-bring-problem'), 'The folder that place has no model.json.', 'a refusal never shows the address');
  refuse = null;
  await h.click('wt-bring-go');
  assert.deepEqual(imports().at(-1), ['/api/weights/import', 'POST', { folder: '/made-up/registry/arch-a/shop/v2', name: 'shop-model' }]);
  assert.equal(imports().length, 2, 'one request for each press of "Bring it in" that passed the name check');
  assert.equal(textOf(h, 'wt-bring-done'), 'Brought in as shop-model. Its files were copied into this workspace.');
  assert.equal(h.byIdPrefix('wt-open-btn-shop-model').length, 1, 'it is in the list');
  // "Choose another" goes back to the registries.
  await h.click('wt-bring-close');
  await h.click('wt-bring'); await h.idle(10);
  await h.click('wt-bring-v-1-0-choose');
  await h.click('wt-bring-other');
  assert.equal(rows(h).length, 4);
});

test('with no registry connected: one sentence, and one button that opens where a registry is connected', async () => {
  const h = await page([]);
  await h.click('wt-bring');
  await h.idle(10);
  assert.equal(textOf(h, 'wt-bring-none'), 'No model registry is connected to this workspace yet. Connect one, and its trained models show here to bring in.');
  assert.equal(textOf(h, 'wt-bring-connect'), 'Connect a model registry');
  assert.equal(h.byIdPrefix('wt-bring-filter').length, 0);
  await h.click('wt-bring-connect');
  assert.deepEqual([globalThis.__shared['captain.open'].section, globalThis.__shared['captain.open'].view], ['data', 'models']);
  assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'data' }]);
  // A registry that Captain has not read yet says so, and services that fail are said in plain words.
  const unread = await page([{ id: 'reg-9', name: 'Model registry', read: false, count: 0, versions: [] }]);
  await unread.click('wt-bring'); await unread.idle(10);
  assert.ok(unread.text().includes('Captain has not read this registry yet. Open Data to read it.'));
  const broken = await page('broken');
  await broken.click('wt-bring'); await broken.idle(10);
  assert.equal(textOf(broken, 'wt-bring-problem'), "Captain couldn't read the workspace's model registries.");
  assert.ok(!/HTTP|500/.test(broken.text()));
});

test('"From somewhere else": a folder is typed once, kept, and never drawn again', async () => {
  const h = await page();
  await h.click('wt-bring'); await h.idle(10);
  assert.equal(textOf(h, 'wt-bring-elsewhere'), 'From somewhere else');
  await h.click('wt-bring-elsewhere'); await h.idle(10);
  assert.ok(h.text().includes('Where your other trained models are kept'));
  await h.click('wt-bring-keep');
  assert.equal(textOf(h, 'wt-bring-problem'), 'Type the folder or the storage address first.');
  h.mod.whereInput.set_value(PLACE);
  await h.click('wt-bring-keep'); await h.idle(10);
  assert.deepEqual(JSON.parse(globalThis.__files[KEPT]), { from: PLACE }, 'kept in the app\'s own folder on this computer');
  assert.equal(asked.at(-1)[0], `/api/weights/registry?from=${encodeURIComponent(PLACE)}`);
  assert.equal(textOf(h, 'wt-bring-count'), '1 model can be brought in from that place.');
  assert.ok(h.text().includes('arch-a/shop/v2') && !h.text().includes(PLACE), 'a row shows the end of its path, never the place');
  assert.equal(pathTail('one/two/three/four/v9'), 'three/four/v9');
  assert.equal(nameFor('Arch A/Shop Model/v2'), 'shop-model-v2');
  await h.click('wt-bring-choose-0');
  await h.click('wt-bring-go');
  assert.deepEqual(imports().at(-1), ['/api/weights/import', 'POST', { folder: `${PLACE}/arch-a/shop/v2`, name: 'shop-v2' }]);
  // The next time it lists the kept place at once, and a place that is gone is said without naming it.
  const again = await page(TWO, { [KEPT]: JSON.stringify({ from: '/made-up/gone' }) });
  await again.click('wt-bring'); await again.idle(10);
  await again.click('wt-bring-elsewhere'); await again.idle(10);
  assert.equal(textOf(again, 'wt-bring-problem'), 'There is no folder at that place.');
  await again.click('wt-bring-registries'); await again.idle(10);
  assert.equal(rows(again).length, 4, 'and the way back to the registries');
});
