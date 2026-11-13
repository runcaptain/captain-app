// The list of projects as quiet rows (captain-project/projects-list.js), with a faked backend: one row per
// project with its state sentence and its capsules, groups under strips that fold, saved views as pills, and the
// person's view kept as they left it. Every name here is made up.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/project-list-quiet.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-project');
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
globalThis.__exec = async () => ({ code: 0, stdout: '', stderr: '' });

const NOW = Date.now(), daysAgo = (n) => new Date(NOW - n * 86400000).toISOString();
let projects, asked;
const exp = (pipeline, more = {}) => ({ id: pipeline, pipeline: `${pipeline}@v1`, status: 'done', ...more });
const SUMS = {
  shop: { stages: { inputs: [], recipe: { label: 'Search ranking' }, ship: { staging: { host: 'local', kind: 'local', status: 'running' } } }, exps: { champion: 'shop/keyword-baseline@v1', experiments: [exp('shop/keyword-baseline', { is_champion: true }), exp('shop/starter-ranker', { evaluation: { comparison: 'win', evidence: 'HUMAN_GRADED' } })] } },
  trail: { stages: { inputs: [], recipe: { label: 'Search ranking' }, ship: {} }, exps: { champion: null, experiments: [exp('trail/first-try')] } },
  garden: { stages: { inputs: [], recipe: { label: 'Recommendations' }, ship: {} }, exps: { champion: null, experiments: [] } },
};
const reply = (status, data) => ({ ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url) => {
  const p = new URL(url).pathname;
  asked.push(p);
  if (p === '/api/projects') return reply(200, projects);
  if (p === '/api/workspace') return reply(200, { name: 'demo', org: 'demo', user: 'sam' });
  if (p === '/api/deployed') return reply(200, { rows: [{ project: 'shop', state: 'running', cloud: 'local' }] });
  const m = /^\/api\/projects\/([^/]+)\/(stages|experiments)$/.exec(p);
  if (m && m[1] === 'kids') return reply(500, { detail: 'boom' });
  if (m && SUMS[m[1]]) return reply(200, m[2] === 'stages' ? SUMS[m[1]].stages : SUMS[m[1]].exps);
  return reply(404, { detail: 'Not Found' });
};
const ALL = [
  { id: 'shop', name: 'Demo shop', description: 'Product search for the shop', created_by: 'sam', created_at: daysAgo(40) },
  { id: 'trail', name: 'Trail gear', description: '', created_by: 'alex', created_at: daysAgo(3) },
  { id: 'garden', name: 'Garden centre', description: '', created_by: 'sam', created_at: daysAgo(0) },
];
async function page(list = ALL, width = 1000) {
  for (const k of Object.keys(store)) if (k !== 'projects-view') delete store[k];
  projects = list; asked = [];
  globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
  globalThis.__props = {}; globalThis.__overlays = [];
  const { default: Mod } = await import(path.join(dir, 'main.js'));
  const h = harness(Mod);
  await h.start();
  h.mod.width = width;
  await h.idle(60);
  return h;
}
const one = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const textIn = (e) => { const out = []; (function w(x) { if (typeof x === 'string' || typeof x === 'number') out.push(String(x)); else for (const k of x?.kids ?? []) w(k); })(e); return out.join(' '); };
const rowsShown = (h) => h.byIdPrefix('ph-p-').filter((e) => /^ph-p-\d+$/.test(e.call('id')?.[0] ?? '')).map((e) => textIn(e));
const { ago, groupsOf, projectFacts } = await import(path.join(dir, 'projects-list.js'));

test('one quiet row per project: name, state sentence, capsules, who made it and when', async () => {
  delete store['projects-view'];
  const h = await page();
  const rows = rowsShown(h);
  assert.equal(rows.length, 3);
  const shop = rows.find((r) => r.includes('Demo shop'));
  assert.ok(shop.includes('Product search for the shop'), 'what it is, beside the name');
  assert.ok(shop.includes('starter-ranker: ranks better than keyword-baseline. Human graded.'), `the page's own sentence: ${shop}`);
  assert.ok(shop.includes('2 models') && shop.includes('Running on this computer'), 'the capsules');
  assert.ok(shop.includes('sam') && shop.includes('1 month ago') && shop.includes('Open'));
  assert.ok(rows.find((r) => r.includes('Garden centre')).includes('No model yet.') && rows.find((r) => r.includes('Garden centre')).includes('Not deployed'));
  // A row is 44 px at least, unboxed, and opens on a click anywhere or on its quiet "Open".
  const i = h.mod.projects.findIndex((p) => p.id === 'trail'), row = one(h, `ph-p-${i}`)[0];
  assert.equal(row.call('min_h')[0], 44);
  assert.ok(!row.call('border_1'), 'no box around a row');
  assert.ok(!one(h, `ph-p-${i}-state`)[0].call('truncate'), 'the state sentence wraps: it is never cut off');
  assert.ok(one(h, `ph-p-${i}-name`)[0].call('truncate'), 'a long name is cut, the columns after it stay put');
  await h.click(`ph-p-${i}-open`);
  assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'project', id: 'trail', label: 'Trail gear' });
  assert.equal(ago(daysAgo(0), NOW), 'today');
  assert.equal(ago(daysAgo(1), NOW), 'yesterday');
  assert.equal(ago(daysAgo(400), NOW), '1 year ago');
  assert.equal(ago('not a date'), '');
});

test('groups under strips that fold, with their counts, in a fixed order', async () => {
  delete store['projects-view'];
  const h = await page();
  assert.deepEqual(groupsOf(h.mod).map((g) => [g.label, g.rows.length]), [['Deployed', 1], ['In progress', 1], ['No model yet', 1]]);
  assert.equal(textIn(one(h, 'ph-group-deployed')[0]), 'Deployed 1');
  const order = h.byIdPrefix('ph-').map((e) => e.call('id')?.[0]).filter((id) => /^ph-group-[a-z]+$|^ph-p-\d+$/.test(id));
  assert.equal(order[0], 'ph-group-deployed');
  assert.equal(order[2], 'ph-group-progress', 'each strip is followed by its rows');
  await h.click('ph-group-progress');
  assert.ok(!rowsShown(h).some((r) => r.includes('Trail gear')), 'folded: its rows are gone');
  assert.equal(one(h, 'ph-group-progress').length, 1, 'the strip stays, with its count');
  assert.deepEqual(JSON.parse(store['projects-view']).folded, ['progress'], 'kept on this computer');
  // Kept as the person left it: a new page opens with the group still folded.
  const again = await page();
  assert.ok(!rowsShown(again).some((r) => r.includes('Trail gear')));
  await again.click('ph-group-progress');
  assert.ok(rowsShown(again).some((r) => r.includes('Trail gear')));
});

test('saved views as pills: All, Deployed, Mine', async () => {
  delete store['projects-view'];
  const h = await page();
  assert.deepEqual(['all', 'deployed', 'mine'].map((v) => textIn(one(h, `ph-view-${v}`)[0])), ['All', 'Deployed', 'Mine']);
  assert.equal(textIn(one(h, 'ph-count')[0]), '3 projects');
  await h.click('ph-view-deployed');
  assert.deepEqual(rowsShown(h).map((r) => r.includes('Demo shop')), [true]);
  assert.equal(textIn(one(h, 'ph-count')[0]), '1 project');
  await h.click('ph-view-mine');
  assert.ok(asked.includes('/api/workspace'), 'who the person is comes from the services');
  assert.deepEqual(rowsShown(h).map((r) => r.includes('Trail gear')), [false, false], 'the two that sam made');
  assert.equal(JSON.parse(store['projects-view']).view, 'mine');
  const again = await page();
  assert.equal(again.mod.listView.view, 'mine', 'the view is kept');
  await again.click('ph-view-all');
  assert.equal(rowsShown(again).length, 3);
});

test('a view with nothing in it, a project that cannot be read, and a narrow page', async () => {
  delete store['projects-view'];
  const none = await page([ALL[1], ALL[2]]);
  await none.click('ph-view-deployed');
  assert.equal(textIn(one(none, 'ph-none')[0]), 'Nothing is deployed yet A project shows here once its model is running somewhere.');
  assert.equal(one(none, 'ph-toolbar').length, 1, 'the pills stay, to pick another view');
  delete store['projects-view'];
  const h = await page([...ALL, { id: 'kids', name: 'Kids range', created_by: 'alex', created_at: daysAgo(2) }], 600);
  const kids = rowsShown(h).find((r) => r.includes('Kids range'));
  assert.ok(kids.includes("Couldn't read this project. Open it to try again.") && !/boom|500/.test(h.text()), 'never a raw error');
  assert.equal(projectFacts(h.mod, h.mod.projects[3]).group, 'failed');
  assert.equal(textIn(one(h, 'ph-group-failed')[0]), "Couldn't be read 1");
  // Narrow: who and when go first. The name, the sentence and the capsules stay.
  assert.equal(h.byIdPrefix('ph-p-0-owner').length + h.byIdPrefix('ph-p-0-when').length, 0);
  assert.equal(h.byIdPrefix('ph-p-0-deployed').length, 1);
  // No project at all: one centred state with the page's one filled button.
  const empty = await page([]);
  assert.ok(textIn(one(empty, 'ph-empty')[0]).startsWith('Start with a project'));
  assert.equal(empty.byIdPrefix('ph-toolbar').length, 0);
});
