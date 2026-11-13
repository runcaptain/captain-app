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
  if (p === '/api/projects/shop/state') return reply(200, { recipe: { id: 'search-judgments', label: 'Search ranking' } });
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

test('the Display popover: grouping, ordering and which properties show, kept, with Escape to close', async () => {
  delete store['projects-view'];
  const h = await page();
  assert.equal(one(h, 'ph-display-panel').length, 0, 'closed until asked for');
  await h.click('ph-display');
  assert.equal(one(h, 'ph-display-panel').length, 1);
  assert.equal(one(h, 'ph-display-panel')[0].call('w')[0], 320);
  const panel = textIn(one(h, 'ph-display-panel')[0]);
  for (const words of ['Grouping', 'State', 'Who made it', 'No groups', 'Ordering', 'Name', 'Newest first', 'Most models', 'Show on each row', 'State sentence', 'Put back the usual display']) assert.ok(panel.includes(words), words);
  // Grouping by who made it: a strip per person, with their projects under it.
  await h.click('ph-display-group-owner');
  assert.deepEqual(groupsOf(h.mod).map((g) => [g.label, g.rows.map((r) => r.p.name)]), [['Made by alex', ['Trail gear']], ['Made by sam', ['Demo shop', 'Garden centre']]]);
  assert.equal(textIn(one(h, 'ph-group-by-sam')[0]), 'Made by sam 2');
  assert.equal(one(h, 'ph-display-panel').length, 1, 'the popover stays open while choices are made');
  // No groups: no strip at all, every row in one list.
  await h.click('ph-display-group-none');
  assert.equal(h.byIdPrefix('ph-group-').length, 0);
  assert.equal(rowsShown(h).length, 3);
  // Ordering.
  await h.click('ph-display-order-newest');
  assert.deepEqual(groupsOf(h.mod)[0].rows.map((r) => r.p.name), ['Garden centre', 'Trail gear', 'Demo shop']);
  await h.click('ph-display-order-models');
  assert.deepEqual(groupsOf(h.mod)[0].rows.map((r) => r.p.name), ['Demo shop', 'Trail gear', 'Garden centre']);
  // Properties: each chip hides or shows one thing on every row.
  const i = h.mod.projects.findIndex((p) => p.id === 'shop');
  for (const [chip, id] of [['state', `ph-p-${i}-state`], ['models', `ph-p-${i}-models`], ['deployed', `ph-p-${i}-deployed`], ['owner', `ph-p-${i}-owner`], ['when', `ph-p-${i}-when`], ['what', `ph-p-${i}-what`]]) {
    assert.equal(one(h, id).length, 1, `${id} shows`);
    await h.click(`ph-display-show-${chip}`);
    assert.equal(one(h, id).length, 0, `${id} is hidden`);
  }
  assert.equal(one(h, `ph-p-${i}-name`).length + one(h, `ph-p-${i}-open`).length, 2, 'the name and Open always show');
  // Kept as the person left it.
  assert.deepEqual([JSON.parse(store['projects-view']).group, JSON.parse(store['projects-view']).order, JSON.parse(store['projects-view']).show.state], ['none', 'models', false]);
  // Escape closes it, and only it.
  await h.key('project-home', 'escape');
  assert.equal(one(h, 'ph-display-panel').length, 0);
  const again = await page();
  assert.deepEqual([again.mod.listView.group, again.mod.listView.order, again.mod.listView.show.models], ['none', 'models', false]);
  // The usual display comes back in one click, and the saved view (the pill) is left as it was.
  await again.click('ph-view-mine');
  await again.click('ph-display');
  await again.click('ph-display-reset');
  assert.deepEqual([again.mod.listView.group, again.mod.listView.order, again.mod.listView.show.models, again.mod.listView.view], ['state', 'name', true, 'mine']);
  // Picking a view or opening a project closes it.
  await again.click('ph-view-all');
  assert.equal(one(again, 'ph-display-panel').length, 0);
});

// ---- A project's page as a document with properties ----------------------------------------------------------------

async function projectPage(width) {
  for (const k of Object.keys(store)) delete store[k];
  projects = ALL; asked = [];
  globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' }, 'captain.scope': { kind: 'project', id: 'shop', label: 'Demo shop' } };
  globalThis.__props = {}; globalThis.__overlays = [];
  SUMS.shop.stages.inputs = [{ slot: 'catalog', label: 'Catalog', role: 'catalog', required: true, pinned: 'demo-catalog@v1', status: 'pinned', rows: 480 }, { slot: 'events', label: 'Events', role: 'events', required: false, status: 'missing' }];
  const { default: Mod } = await import(path.join(dir, 'main.js'));
  const h = harness(Mod);
  await h.start();
  h.mod.width = width;
  await h.idle(80);
  return h;
}

test('a project opens as a document: a large title, quiet sections, and its properties', async () => {
  const h = await projectPage(1200);
  // The bar above says where this is, with the way back. The name is in the page, large.
  assert.equal(textIn(one(h, 'ph-crumb-projects')[0]), 'Projects');
  const name = one(h, 'ph-name')[0];
  assert.deepEqual([textIn(name), name.call('text_size')[0], name.call('line_height')[0]], ['Demo shop', 24, '32px']);
  assert.equal(textIn(one(h, 'ph-building')[0]), 'Search ranking');
  // The properties: a column beside the document on a wide page.
  const props = one(h, 'ph-props')[0];
  assert.equal(props.call('w')[0], 232);
  assert.equal(textIn(props), 'Properties Deployed Running on this computer Baseline model keyword-baseline Data chosen 1 of 2 Made by sam Made ' + `${ALL[0].created_at.slice(0, 10)}, 1 month ago`);
  assert.deepEqual(h.mod.propertyRows().map((r) => [r.id, Boolean(r.go)]), [['deployed', true], ['baseline', true], ['data', false], ['owner', false], ['created', false]], 'a value is a button only when it goes somewhere');
  // The baseline opens that model's details in place.
  await h.click('ph-prop-baseline');
  assert.equal(h.mod.open, 'shop/keyword-baseline');
  // Data and Models are rows on the page, not boxes: no border or fill around the lists or their rows.
  for (const id of ['in-rows', 'exp-rows', 'in-catalog', 'exp-0']) { const e = one(h, id)[0]; assert.ok(e && !e.call('border_1') && !e.call('border_b'), `${id} is unboxed`); }
  assert.equal(one(h, 'in-catalog')[0].call('min_h')[0], 44);
  for (const part of ['part-inputs', 'part-experiments', 'part-ship']) assert.equal(one(h, part).length, 1, part);
  // Three text sizes on the page: the title, rows and controls, captions.
  const sizes = new Set();
  (function w(e) { if (!e || typeof e !== 'object') return; const s = e.call?.('text_size')?.[0]; if (s != null) sizes.add(s); for (const k of e.kids ?? []) w(k); })(h.mod.render(h.cx));
  assert.deepEqual([...sizes].sort((a, b) => a - b), [12, 13, 24]);
  // The way back to the list.
  await h.click('ph-crumb-projects');
  assert.equal(globalThis.__shared['captain.scope'], null);
});

test('on a page too narrow for a column the properties sit under the title', async () => {
  const h = await projectPage(600);
  const props = one(h, 'ph-props')[0];
  assert.ok(!props.call('w'), 'not a column');
  const order = h.byIdPrefix('ph-').concat(h.byIdPrefix('part-')).map((e) => e.call('id')?.[0]);
  const ids = []; (function w(e) { if (!e || typeof e !== 'object') return; const id = e.call?.('id')?.[0]; if (['ph-title', 'ph-props', 'part-inputs'].includes(id)) ids.push(id); for (const k of e.kids ?? []) w(k); })(h.mod.render(h.cx));
  assert.deepEqual(ids, ['ph-title', 'ph-props', 'part-inputs'], `under the title, over the sections: ${order.length}`);
});

test('who made a project is shown by name: a sign-in address is cut at its "@"', async () => {
  const { shortOwner } = await import(path.join(dir, 'projects-list.js'));
  assert.equal(shortOwner('sam@example.test'), 'sam');
  assert.equal(shortOwner('sam'), 'sam');
  assert.equal(shortOwner(null), '');
  delete store['projects-view'];
  const h = await page([{ id: 'garden', name: 'Garden centre', created_by: 'sam@example.test', created_at: daysAgo(2) }]);
  assert.equal(textIn(one(h, 'ph-p-0-owner')[0]), 'sam');
  assert.ok(!h.text().includes('example.test'), 'the address is not on the page');
});

test('the menus on a project\'s page open below their buttons, not over them', async () => {
  const h = await projectPage(1200);
  // A popup's anchor names the popover's own corner: "top_right" puts it under the button.
  const anchors = [];
  (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'popup') anchors.push([e.args?.[0], e.call?.('anchor')?.[0]]); for (const k of e.kids ?? []) w(k); })(h.mod.render(h.cx));
  assert.ok(anchors.some(([id]) => id === 'ph-more-pop'), JSON.stringify(anchors));
  for (const [id, anchor] of anchors) assert.ok(String(anchor).startsWith('top_'), `${id} opens below its button: ${anchor}`);
  await h.click('ph-more');
  assert.equal(one(h, 'ph-more-list').length, 1);
});
