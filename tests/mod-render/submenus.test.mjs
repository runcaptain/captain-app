// No view has more than two submenus (Edgar, 2026-11-12: "Data Tables has five sub-submenus, Runs has five submenus,
// and the same goes for Compare. They need to be reduced down to one or two menus max.").
//
// A view is a slot of the layout's "sections" stack. Three checks on each:
//   1. Drawn with Captain's services down (so it needs no fixtures), it shows at most two `submenu-<id>` elements.
//   2. Its own source draws no other strip of tabs: it does not call the kit's Tabs, and it holds no list of tabs
//      (an array named tabs or TABS) longer than two. A third submenu fails here even if it only shows later, on
//      a record's page.
//   3. The kit's Submenus refuses a third.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/submenus.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';
import { InputState } from 'gpui-base';
// The app's InputState has set_masked (Team's access token uses it); the stub models it here as captain-team.test.mjs does.
const makeInput = InputState.new;
InputState.new = (options) => { const state = makeInput(options); state.set_masked = (value) => { state.masked = value; }; return state; };

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');

// Views still being cut down on this branch. Each must really be over the limit, so the list can only shrink:
// a view that now passes and is still listed fails the test.
const NOT_YET = new Set(['captain-data', 'captain-runs', 'captain-compare']);

const find = (n, id) => (n?.id === id ? n : (n?.children ?? []).map((k) => find(k, id)).find(Boolean) ?? (n?.root ? find(n.root, id) : null));
const layout = JSON.parse(fs.readFileSync(path.join(root, 'defaults/layouts/captain.json'), 'utf8'));
const views = find(layout, 'sections').children.map((k) => k.content.mod);
assert.ok(views.length >= 4, `the layout's views: ${views}`);

/** How many items an array literal starting at `at` (its "[") holds at its top level. */
function arrayLength(text, at) {
  let depth = 0, items = 0, any = false, quote = null;
  for (let i = at; i < text.length; i++) {
    const ch = text[i];
    if (quote) { if (ch === '\\') i++; else if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; if (depth === 1) any = true; continue; }
    if (ch === '[' || ch === '(' || ch === '{') { depth++; if (depth === 2) any = true; continue; }
    if (ch === ']' || ch === ')' || ch === '}') { depth--; if (depth === 0) return items + (any ? 1 : 0); continue; }
    if (ch === ',' && depth === 1) { items++; any = false; } else if (depth === 1 && !/\s/.test(ch)) any = true;
  }
  return items;
}

/** Tab strips a view's own files hold beyond the kit's Submenus: [file, what]. */
function ownStrips(mod) {
  const dir = path.join(root, 'defaults/mods', mod);
  const found = [];
  for (const f of fs.readdirSync(dir).filter((x) => x.endsWith('.js'))) {
    const text = fs.readFileSync(path.join(dir, f), 'utf8');
    if (/\bTabs\(c,/.test(text)) found.push([f, 'calls the kit\'s Tabs']);
    for (const m of text.matchAll(/\b(?:const|let)\s+(\w*(?:tabs|TABS))\s*=\s*\[/g)) {
      const n = arrayLength(text, m.index + m[0].length - 1);
      if (n > 2) found.push([f, `${m[1]} has ${n} entries`]);
    }
  }
  return found;
}

// 3. The kit refuses a third.
const { Submenus, MAX_SUBMENUS } = await import(path.join(root, 'defaults/captain/kit/ui.js'));
assert.equal(MAX_SUBMENUS, 2);
const c = new Proxy({}, { get: () => '#000000' });
const three = [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }, { id: 'c', label: 'C' }];
assert.throws(() => Submenus(c, three, 'a', () => {}), /at most 2 submenus/);
assert.doesNotThrow(() => Submenus(c, three.slice(0, 2), 'a', () => {}));
assert.equal(arrayLength('[["a", "A"], ["b", "B"], ["c", "C"]]', 0), 3);
assert.equal(arrayLength('[["a", "A"], ["b", `x ${[1, 2].length}`]]', 0), 2);

const over = [];
for (const mod of views) {
  const dir = path.join(root, 'defaults/mods', mod);
  assert.ok(fs.existsSync(path.join(dir, 'main.js')), `${mod} is in the layout but has no main.js`);
  const problems = ownStrips(mod).map(([f, what]) => `${f} ${what}`);
  // Drawn with the services down: every screen shows its header then.
  const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-submenus-`)));
  fs.cpSync(dir, work, { recursive: true });
  fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
  fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
  globalThis.__down = { ok: false, failed: true, error: "Captain's services didn't start." };
  globalThis.__shared = {};
  globalThis.__props = {};
  const { default: Mod } = await import(path.join(work, 'main.js'));
  const h = harness(Mod);
  await h.start();
  const drawn = h.byIdPrefix('submenu-').map((e) => e.call('id')[0]);
  // The old strips used ids like "tab-<name>": they count too, so a view can't keep five by not using the kit.
  const old = h.byIdPrefix('tab-').map((e) => e.call('id')[0]);
  if (drawn.length + old.length > 2) problems.push(`draws ${drawn.length + old.length} submenus (${[...drawn, ...old].join(', ')})`);
  globalThis.__down = null;
  if (NOT_YET.has(mod)) assert.ok(problems.length > 0, `${mod} is within the limit now: take it off NOT_YET`);
  else if (problems.length) over.push(`${mod}: ${problems.join('; ')}`);
}
assert.deepEqual(over, [], `views with more than two submenus:\n${over.join('\n')}`);
for (const m of NOT_YET) assert.ok(views.includes(m), `${m} is on NOT_YET but is not a view: take it off`);
console.log(`submenus render test: ok (${views.length} views, ${NOT_YET.size} still to cut down)`);
process.exit(0);
