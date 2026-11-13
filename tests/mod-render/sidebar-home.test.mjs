// The sidebar's rule (the owner: "If I keep on going back to the deployments menu, I want to default to the home
// deployments menu, not the deployment I was already looking at"): an entry always opens its view's home, a second
// click on the entry already shown goes home too, and a detail page opens only from a row or a link.
// One mechanism: the sidebar sets shared "captain.open" {section, home: true, at}, and every view honours `home`.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/sidebar-home.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__routes = [];
const mount = async (mod, fixtures, routes) => {
  globalThis.__fixtures = path.join(here, 'fixtures', fixtures);
  globalThis.__routes.length = 0;
  globalThis.__routes.push(...routes);
  const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-home-`)));
  fs.cpSync(path.join(root, 'defaults/mods', mod), work, { recursive: true });
  fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
  fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
  const { default: Mod } = await import(path.join(work, 'main.js'));
  const h = harness(Mod);
  await h.start();
  await h.idle(40);
  return h;
};
let tick = Date.now() + 60000;
const ask = async (h, o) => { globalThis.__shared['captain.open'] = { ...o, at: (tick += 1000) }; await h.layoutLists({}); await h.idle(30); };

// ---- the sidebar: every entry asks for its view's home, every time ---------------------------------------------
{
  const { ITEMS, openEntry } = await import('../../defaults/mods/captain-nav/main.js');
  const entries = ITEMS.filter((x) => !x.run);
  assert.deepEqual(entries.map((x) => x.id), ['project', 'data', 'notebooks', 'runs', 'deployments', 'team']);
  for (const it of entries) {
    openEntry(it);
    const o = globalThis.__shared['captain.open'];
    assert.deepEqual([o.section, o.home], [it.id, true], `${it.label} asks for its home`);
    assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: it.id }]);
    // A second click on the entry already shown asks again, with a new time: the view goes home again.
    await new Promise((r) => setTimeout(r, 3));
    openEntry(it);
    const again = globalThis.__shared['captain.open'];
    assert.ok(again.home && again.at > o.at, `${it.label}: a second click goes home too`);
  }
  openEntry(entries[0]);
  assert.equal(globalThis.__shared['captain.open'].all, true, 'Projects: every project');
  // The sidebar's rows call it, and nothing else decides what a click opens.
  const nav = fs.readFileSync(path.join(root, 'defaults/mods/captain-nav/main.js'), 'utf8');
  assert.ok(nav.includes('const go = (it) => () => openEntry(it, stack);'));
}

// ---- Data: from a table's page, or from Sources, back to the groups as they were left ----------------------------
{
  const h = await mount('captain-data', 'captain-data', [
    [/^\/api\/dataset-classes/, 'classes'], [/\/rows\?/, 'rows0'], [/^\/api\/data\/tables/, 'data-tables-big'],
    [/^\/api\/datasets(\?|$)/, 'datasets'], [/^\/api\/datasets\/[^?]+$/, 'detail'],
  ]);
  await h.click('cat-group-kept');
  h.mod.openDataset(h.cx, 'demo-catalog');
  await h.idle(40);
  assert.ok(h.mod.shown, 'a table\'s page is open');
  await ask(h, { section: 'data', home: true });
  assert.deepEqual([h.mod.shown, h.mod.tab], [null, 'tables'], 'home: the list, no page');
  assert.ok(h.byIdPrefix('cat-group-kept').length >= 1 && h.mod.catalog.isOpen('kept'), 'the groups, as the person left them');
  h.mod.showTab(h.cx, 'sources');
  await ask(h, { section: 'data', home: true });
  assert.equal(h.mod.tab, 'tables', 'from Sources too');
  // A link, which carries no `home`, still opens what it names.
  await ask(h, { section: 'data', view: 'sources' });
  assert.equal(h.mod.tab, 'sources');
  // And a request for another view is not this one's business.
  await ask(h, { section: 'runs', home: true });
  assert.equal(h.mod.tab, 'sources');
}

// ---- Runs: from Compare, or from a row's page in a narrow pane, back to the list --------------------------------
{
  const h = await mount('captain-runs', 'captain-runs', [
    [/^\/api\/jobs\?/, 'jobs-list'], [/^\/api\/live\?/, 'live'], [/\/compatibility$/, 'empty'], [/\/histogram/, 'empty'],
    [/^\/api\/backtests$/, 'backtests'], [/^\/api\/backtests\/[^?]+$/, 'backtest'], [/^\/api\/runs\//, 'run'],
    [/^\/api\/workflows$/, 'workflows'], [/\/runs$/, 'wf-runs-done'], [/^\/api\/workflows\/[^?]+$/, 'workflow'],
    [/^\/api\/grading$/, 'grading'], [/^\/api\/replays$/, 'replays'],
  ]);
  h.mod.showTab(h.cx, 'compare');
  h.mod.opened1 = true;
  await ask(h, { section: 'runs', home: true });
  assert.deepEqual([h.mod.tab, h.mod.opened1], ['runs', false], 'the list');
  assert.ok(h.byIdPrefix('submenu-runs').length === 1);
  // A link still opens a run.
  await ask(h, { section: 'runs', tab: 'compare' });
  assert.equal(h.mod.tab, 'compare');
}

// ---- Notebooks: the open notebook closes (its draft saved first), the list stays ---------------------------------
{
  const { Notebooks } = await import('../../defaults/mods/captain-notebooks/notebooks.js');
  const calls = [];
  const nb = Object.create(Notebooks.prototype);
  Object.assign(nb, { open: { name: 'weekly' }, dirty: true, want: 'weekly', exporting: true, flushDraft: () => calls.push('draft') });
  nb.home({ notify: () => calls.push('drawn') });
  assert.deepEqual([nb.open, nb.want, nb.exporting, calls], [null, null, false, ['draft', 'drawn']], 'the draft is saved, then the notebook closes');
  const main = fs.readFileSync(path.join(root, 'defaults/mods/captain-notebooks/main.js'), 'utf8');
  assert.ok(main.includes('if (o.home) { nb.home(cx); return; }'), 'the screen sends the sidebar\'s request there');
}
console.log('sidebar home: ok');
process.exit(0);
