// The palette's Captain entries (captain-nav publishes them in shared "palette.extra"): every entry has its own id,
// so none hides another. Entries that open a screen with no props all had the id "overlay:{}", and the palette kept
// one: "Compare your own rankings", Explain, What if, New workspace and Cloud and compute went missing. The tasks
// carry the New menu's icons, and Connect MLflow is there.
import test from 'node:test';
import assert from 'node:assert/strict';

const { staticEntries, openEntries, ITEMS, MOVED } = await import('../../defaults/mods/captain-nav/main.js');
const { default: Palette } = await import('../../defaults/frame/palette/main.js');
// The palette's whole list, as it shows it with nothing typed (its entries() isn't exported: scripts/palette.test.ts
// loads the file as a function body).
const entries = () => { const v = new Palette(); v.query = ''; v.recent = []; return v.rows().map((r) => r.e); };

test('every Captain palette entry is listed once, with an icon', () => {
  const sections = [{ id: 'data', label: 'Data', icon: 'database' }, { id: 'runs', label: 'Runs', icon: 'play' }];
  globalThis.__shared = { 'palette.extra': {
    'captain-app': staticEntries(sections),
    captain: openEntries([{ name: 'demo-catalog', kind: 'catalog' }], [{ id: 'i1', title: 'Long queries' }], [{ name: 'demo-tuned-vs-bm25' }]),
  } };
  const all = entries();
  const ids = all.map((e) => e.id);
  assert.equal(new Set(ids).size, ids.length, `duplicate ids: ${ids.filter((x, i) => ids.indexOf(x) !== i)}`);
  const titles = all.map((e) => e.title);
  for (const t of ['Compare your own rankings', 'Connect MLflow', 'Explain a query', 'New workspace...', 'Cloud and compute', 'Run an offline evaluation', 'Import a dataset']) {
    assert.ok(titles.includes(t), `${t} is in the palette`);
  }
  const captain = all.filter((e) => e.group === 'Captain');
  const bare = captain.filter((e) => !e.icon).map((e) => e.title);
  assert.deepEqual(bare, [], 'every Captain entry has an icon');
  assert.match(all.find((e) => e.title === 'Connect MLflow').icon, /^icons\/captain-[a-z-]+\.svg$/);
});

// The screens changed their words (offline evaluation, rating), and Compare moved inside Runs; people who type the
// earlier ones still land on the same entries.
test('typing an earlier word finds the entry under its new name', () => {
  globalThis.__shared = { 'palette.extra': {
    'captain-app': staticEntries(ITEMS),
    captain: openEntries([], [], [{ name: 'demo-tuned-vs-bm25' }]),
  } };
  const find = (q) => { const v = new Palette(); v.query = q; v.recent = []; return v.rows().map((r) => r.e.title); };
  for (const [typed, title] of [['backtest', 'Run an offline evaluation'], ['grading', 'Start rating'], ['comparisons', 'Go to Runs'],
    ['compare', 'Open demo-tuned-vs-bm25'], ['home', 'Go to Projects'], ['datasets', 'Go to Data']]) {
    assert.ok(find(typed).includes(title), `"${typed}" finds ${title}: ${find(typed).slice(0, 6).join(', ')}`);
  }
});

// Edgar, 2026-11-12: six views at most, in this order, and Experiments, Compare and Launch review are gone from the
// sidebar, the palette and the New menu. `cst` still does their work from a terminal.
test('the sidebar has only the agreed views, and the removed screens are nowhere in the palette', async () => {
  const labels = ITEMS.map((i) => i.label);
  const AGREED = ['Projects', 'Data', 'Notebooks', 'Runs', 'Deployments', 'Team'];
  assert.deepEqual(labels, AGREED.filter((l) => labels.includes(l)), `views in the agreed order: ${labels}`);
  for (const must of ['Projects', 'Data', 'Notebooks', 'Runs', 'Team']) assert.ok(labels.includes(must), `${must} is a view`);
  assert.equal(labels[0], 'Projects', 'Projects is first: it is home');
  assert.deepEqual(ITEMS[0].open, { all: true }, 'Projects opens on every project');
  globalThis.__shared = { 'palette.extra': {
    'captain-app': staticEntries(ITEMS),
    captain: openEntries([{ name: 'demo-catalog' }], [{ id: 'i1', title: 'Long queries' }], [{ name: 'demo-tuned-vs-bm25' }]),
  } };
  const titles = entries().filter((e) => e.group === 'Captain').map((e) => e.title);
  for (const t of titles) assert.doesNotMatch(t, /experiment|launch review|^Compare demo|Go to Compare/i, `${t} names a removed screen`);
  // Every view the sidebar lists is a slot in the layout, and no removed screen is.
  const fs = await import('node:fs');
  const layout = JSON.parse(fs.readFileSync(new URL('../../defaults/layouts/captain.json', import.meta.url), 'utf8'));
  const find = (n, id) => (n?.id === id ? n : (n?.children ?? []).map((k) => find(k, id)).find(Boolean) ?? (n?.root ? find(n.root, id) : null));
  const stack = find(layout, 'sections');
  const slots = stack.children.map((k) => k.id);
  for (const it of ITEMS) assert.ok(slots.includes(it.id), `${it.label} has a slot in the layout`);
  for (const gone of ['investigations', 'handoff']) assert.ok(!slots.includes(gone), `${gone} is not a screen any more`);
  assert.equal(stack.active, 'project', 'the app opens on Projects');
  assert.equal(stack.children.find((k) => k.id === 'project').content.props.allProjects, true, 'and on every project, not the last one');
  for (const k of stack.children) assert.ok(fs.existsSync(new URL(`../../defaults/mods/${k.content.mod}/main.js`, import.meta.url)), `${k.content.mod} exists`);
  assert.deepEqual(Object.keys(MOVED).sort(), ['comparisons', 'handoff', 'investigations']);
});
