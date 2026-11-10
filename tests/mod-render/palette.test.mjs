// The palette's Captain entries (captain-nav publishes them in shared "palette.extra"): every entry has its own id,
// so none hides another. Entries that open a screen with no props all had the id "overlay:{}", and the palette kept
// one: "Compare your own rankings", Explain, What if, New workspace and Cloud and compute went missing. The tasks
// carry the New menu's icons, and Connect MLflow is there.
import test from 'node:test';
import assert from 'node:assert/strict';

const { staticEntries, openEntries } = await import('../../defaults/mods/captain-nav/main.js');
const { default: Palette } = await import('../../defaults/frame/palette/main.js');
// The palette's whole list, as it shows it with nothing typed (its entries() isn't exported: scripts/palette.test.ts
// loads the file as a function body).
const entries = () => { const v = new Palette(); v.query = ''; v.recent = []; return v.rows().map((r) => r.e); };

test('every Captain palette entry is listed once, with an icon', () => {
  const sections = [{ id: 'data', label: 'Data and signals', icon: 'database' }, { id: 'runs', label: 'Runs', icon: 'play' }];
  globalThis.__shared = { 'palette.extra': {
    'captain-app': staticEntries(sections),
    captain: openEntries([{ name: 'demo-catalog', kind: 'catalog' }], [{ id: 'i1', title: 'Long queries' }], [{ name: 'demo-tuned-vs-bm25' }]),
  } };
  const all = entries();
  const ids = all.map((e) => e.id);
  assert.equal(new Set(ids).size, ids.length, `duplicate ids: ${ids.filter((x, i) => ids.indexOf(x) !== i)}`);
  const titles = all.map((e) => e.title);
  for (const t of ['Compare your own rankings', 'Connect MLflow', 'Explain a query', 'New workspace...', 'Cloud and compute', 'New experiment', 'Import a dataset']) {
    assert.ok(titles.includes(t), `${t} is in the palette`);
  }
  const captain = all.filter((e) => e.group === 'Captain');
  const bare = captain.filter((e) => !e.icon).map((e) => e.title);
  assert.deepEqual(bare, [], 'every Captain entry has an icon');
  assert.match(all.find((e) => e.title === 'Connect MLflow').icon, /^icons\/captain-[a-z-]+\.svg$/);
});

// The screens changed their words (offline evaluation, experiment, rating, launch review, Compare); people who type
// the earlier ones still land on the same entries.
test('typing an earlier word finds the entry under its new name', () => {
  const sections = [{ id: 'investigations', label: 'Experiments', was: 'investigations', icon: 'search' }, { id: 'comparisons', label: 'Compare', was: 'comparisons side by side', icon: 'git-compare' },
    { id: 'handoff', label: 'Launch review', was: 'handoff hand off', icon: 'send' }];
  globalThis.__shared = { 'palette.extra': {
    'captain-app': staticEntries(sections),
    captain: openEntries([], [{ id: 'i1', title: 'Long queries' }], [{ name: 'demo-tuned-vs-bm25' }]),
  } };
  const find = (q) => { const v = new Palette(); v.query = q; v.recent = []; return v.rows().map((r) => r.e.title); };
  for (const [typed, title] of [['backtest', 'Run an offline evaluation'], ['investigation', 'New experiment'], ['grading', 'Start rating'],
    ['handoff', 'Start a launch review'], ['handoff', 'Go to Launch review'], ['comparisons', 'Go to Compare'], ['investigations', 'Go to Experiments'],
    ['investigation', 'Open experiment: Long queries']]) {
    assert.ok(find(typed).includes(title), `"${typed}" finds ${title}: ${find(typed).slice(0, 6).join(', ')}`);
  }
});
