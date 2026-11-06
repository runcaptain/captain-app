// Switching to a space bound to another workspace reloads each native screen (kit watchWorkspace): its list is
// asked for again, and what was open is dropped.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/workspace-switch.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const LIST = { 'captain-data': /^\/api\/datasets(\?|$)/, 'captain-runs': /^\/api\/backtests$/, 'captain-compare': /^\/api\/backtests$/ };
// Each screen's own fixtures (tests/mod-render/fixtures/<mod>), enough to draw its list and first detail.
const ROUTES = {
  'captain-data': [[/^\/api\/dataset-classes/, 'classes'], [/\/rows\?/, 'rows0'], [/^\/api\/datasets(\?|$)/, 'datasets'], [/^\/api\/datasets\/[^?]+$/, 'detail'], [/^\/api\/labels$/, 'labels']],
  'captain-runs': [[/^\/api\/backtests$/, 'backtests'], [/^\/api\/backtests\/[^?]+$/, 'backtest'], [/^\/api\/runs\//, 'run']],
  'captain-compare': [[/^\/api\/backtests$/, 'backtests'], [/\/compatibility$/, 'compat'], [/\/histogram/, 'hist'], [/\/queries\?/, 'queries0'], [/^\/api\/runs\/[^?]+\?v=/, 'run']],
};
for (const [mod, list] of Object.entries(LIST)) {
  globalThis.__fixtures = path.join(here, `fixtures/${mod}`);
  globalThis.__routes = ROUTES[mod];
  globalThis.__asked = [];
  globalThis.__space = 's1';
  globalThis.__shared = { 'captain.spaces': { s1: { workspace: 'acme' }, s2: { workspace: 'globex' } } };
  const work = fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-ws-`));
  fs.cpSync(path.join(root, 'defaults/mods', mod), work, { recursive: true });
  fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
  fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
  const { default: Mod } = await import(path.join(work, 'main.js'));
  const h = harness(Mod);
  await h.start();
  await h.layoutLists({});
  const before = globalThis.__asked.filter((p) => list.test(p)).length;
  assert.ok(before >= 1, `${mod}: loaded its list`);
  await h.layoutLists({});
  assert.equal(globalThis.__asked.filter((p) => list.test(p)).length, before, `${mod}: no reload without a switch`);
  globalThis.__space = 's2';
  await h.layoutLists({});
  assert.ok(globalThis.__asked.filter((p) => list.test(p)).length > before, `${mod}: reloaded after the switch`);
}
console.log('workspace-switch render test: ok');
