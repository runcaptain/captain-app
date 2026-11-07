// An imported run (mode "imported": only a readout, no stored rankings) in captain-compare. Its per-query,
// histogram and query routes answer 400, and its version is 0. The overview must draw from the run record alone,
// and Queries, Segments and Surfaces must say in words why there is nothing per query. Never "Loading" forever.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/imported-run.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-compare-imported');
globalThis.__routes = [
  [/^\/api\/backtests$/, 'backtests'],
  [/\/compatibility$/, 'compat'],
  [/\/histogram/, 'norank'],
  [/\/queries/, 'norank'],
  [/^\/api\/runs\/[^?]+\?v=/, 'run'],
];
globalThis.__shared = {};
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-compare-imp-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-compare'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();
await h.layoutLists({});

let text = h.text();
if (process.env.SHOW) console.log('overview ::', text.replace(/\s+/g, ' ').slice(0, 700));
assert.ok(globalThis.__asked.some((p) => /^\/api\/runs\/demos\/aptdeco\/ab6c-seg\?v=0/.test(p)), `opened v0: ${globalThis.__asked.join(' ')}`);
assert.ok(!/Loading/.test(text), 'not stuck loading');
assert.ok(text.includes('Conversion (relative)') && !text.includes('conversion.rel'), 'comparisons from the record, in words');
assert.ok(/imported/i.test(text), 'says it was imported');
assert.ok(text.includes('Imported from a readout: no per-query rankings') && text.indexOf('Imported from a readout: no per-query rankings') < text.indexOf('Conversion (relative), B minus A'), 'warnings above the result');
assert.ok(!/stored rankings|HTTP 400/.test(text), 'no backend error on the overview');
for (const t of ['tab-queries', 'tab-segments', 'tab-surfaces']) {
  await h.click(t);
  await h.layoutLists({});
  text = h.text();
  if (process.env.SHOW) console.log(t, '::', text.replace(/\s+/g, ' ').slice(0, 400));
  assert.ok(/imported from a readout/.test(text) && /No .* for this run/.test(text), `${t}: human empty state`);
  assert.ok(!/stored rankings|Loading/.test(text), `${t}: no raw error, not loading`);
}

// A run whose record is gone: one sentence, Retry and Copy details; the storage key only on the clipboard.
globalThis.__routes.unshift([/^\/api\/runs\/[^?]+\?v=/, 'missing']);
h.mod.open(h.cx, 'demos/aptdeco/ab6c-seg', 0);
await h.idle();
text = h.text();
assert.ok(text.includes("This run's record is missing from the workspace."), `missing record sentence: ${text.slice(0, 300)}`);
assert.ok(!/MANIFEST|backtest-runs\//.test(text), 'no storage key on screen');
assert.ok(text.includes('Retry') && text.includes('Copy details'), 'actions');
console.log('imported-run render test: ok');
