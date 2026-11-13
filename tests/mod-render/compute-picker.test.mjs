// The compute picker on the sidebar's cloud chip (Edgar, 2026-11-12: "select what compute environment I'm working
// in whenever I want"). It lists every place work can run with the current one checked; picking one sets the
// workspace's default place for runs (PUT /api/settings/compute), and the forms' "Runs on" row then opens on it.
// It changes that one setting and nothing else.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/compute-picker.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-compute');
globalThis.__routes = [
  [/^\/api\/compute$/, 'compute'],
  [/^\/api\/settings\/compute$/, 'settings-aws', 'PUT'],
  [/^\/api\/settings$/, 'settings'],
  [/^\/api\/cloud\/summary$/, 'cloud'],
];
// The fixture client keeps the array it was given: change the routes in place.
const routes = (list) => { globalThis.__routes.length = 0; globalThis.__routes.push(...list); };
globalThis.__shared = {};
globalThis.__props = { overlay: 'o1' };
globalThis.__overlays = [];
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-compute-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-compute'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const cost = await import(path.join(work, 'kit/cost.js'));

let h = harness(Mod);
await h.start();
await h.idle(20);
let t = h.text();
// Every place work can run: this computer, then each compute target that runs jobs. A target that only serves
// models is not a place to run work, so it is not offered.
const ids = h.byIdPrefix('compute-').map((e) => e.call('id')[0]).filter((id) => !['compute-picker', 'compute-places', 'compute-add', 'compute-clouds', 'compute-connect', 'compute-error'].includes(id));
assert.deepEqual(ids, ['compute-local', 'compute-aws-batch', 'compute-gcp-jobs'], `the places: ${ids}`);
assert.ok(t.includes('This computer') && t.includes('Free. Nothing leaves this computer.'), 'this computer, in words');
assert.ok(t.includes('AWS Batch, us-west-1') && t.includes('Google Cloud Run, us-central1'), `each cloud place in words: ${t}`);
assert.ok(!t.includes('shop-serving'), 'a serving-only target is not a place to run work');
// The current one is checked.
const checked = (id) => { let n = 0; (function walk(e) { if (e && typeof e === 'object') { if (e.kind === 'svg' && /captain-check\.svg$/.test(String(e.args[0]))) n++; for (const k of e.kids ?? []) walk(k); } })(h.byIdPrefix(id)[0]); return n; };
assert.equal(checked('compute-local'), 1, 'this computer is checked');
assert.equal(checked('compute-aws-batch'), 0);

// Picking one sets the workspace's default place for runs, tells the open screens, and closes.
await h.click('compute-aws-batch');
await h.idle(20);
const put = globalThis.__bodies.filter(([p, o]) => p === '/api/settings/compute' && o.method === 'PUT').pop();
assert.deepEqual(put?.[1]?.body, { backtest: 'aws-batch' }, 'the setting, and only that');
assert.equal(globalThis.__shared['captain.compute']?.place, 'aws-batch', 'the chip and the forms are told');
assert.equal(cost.placeChanged(), globalThis.__shared['captain.compute'].at);
assert.ok((globalThis.__closed ?? []).includes('o1') || h.mod.current === 'aws-batch', 'and the panel is done');
assert.equal(globalThis.__asked.filter((p) => !/^\/api\/(compute|settings|settings\/compute|cloud\/summary)$/.test(p)).length, 0, `it asks for nothing else: ${globalThis.__asked}`);

// A form's "Runs on" row now opens on that place; a place the person picks in the form stays.
routes([[/^\/api\/compute$/, 'compute'], [/^\/api\/settings$/, 'settings-aws']]);
let st = cost.costState();
await cost.loadPlaces(st);
assert.equal(st.where, 'aws-batch', 'the form opens on the workspace default');
assert.equal(st.usual, 'aws-batch');
st = cost.costState();
st.picked = true;
await cost.loadPlaces(st);
assert.equal(st.where, 'local', "the person's own pick is not overridden");
// A default that is gone falls back to this computer.
routes([[/^\/api\/compute$/, 'compute'], [/^\/api\/settings$/, 'refused']]);
st = cost.costState();
await cost.loadPlaces(st);
assert.equal(st.where, 'local');

// A place the services refuse: their sentence shows, the panel stays, nothing is announced.
routes([[/^\/api\/compute$/, 'compute'], [/^\/api\/settings\/compute$/, 'refused', 'PUT'], [/^\/api\/settings$/, 'settings'], [/^\/api\/cloud\/summary$/, 'no-cloud']]);
globalThis.__shared = {};
h = harness(Mod);
await h.start();
await h.idle(20);
await h.click('compute-gcp-jobs');
await h.idle(20);
assert.ok(h.text().includes("shop-serving can't be used for backtests"), `the refusal, in the services' words: ${h.text()}`);
assert.equal(globalThis.__shared['captain.compute'], undefined, 'nothing is announced');
assert.equal(h.mod.current, 'local');
// With no cloud login yet, the last link connects one.
assert.equal(h.byIdPrefix('compute-connect').length, 1);
assert.equal(h.byIdPrefix('compute-clouds').length, 0);
await h.click('compute-add');
assert.equal(globalThis.__overlays.at(-1)?.props?.task, 'compute', 'Add a place to run work opens its form');
assert.ok(!/[←-⇿—]/.test(h.text()), 'no arrows or em dashes');
console.log('compute picker render test: ok');
process.exit(0);
