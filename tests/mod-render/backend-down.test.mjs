// Edgar's rule for every native screen: with Captain's backend down, a screen shows a quiet skeleton, then one plain
// sentence with Retry and Copy details. Never a transport or tool error, never a host or port, never "engine" or
// "daemon". This renders each section's every tab while the backend is still starting, after it failed to come up,
// and when a request fails in transport, and checks the visible text.
//
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/backend-down.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const FORBIDDEN = /curl|127\.0\.0\.1|\bport\b|engine|daemon|ECONNREFUSED|localhost/i;
// What the kit's api() can hand back when the backend is not there (kit/cst.js), with the worst real-world text.
const RAW = "curl: (7) Failed to connect to 127.0.0.1 port 56688 after 0 ms: Couldn't connect to server";
const MODES = {
  starting: { ok: false, starting: true, error: null },
  failed: { ok: false, failed: true, error: "Captain's services didn't start." },
  // An older kit that put the raw reason in the sentence: the screen must still not show it.
  failedRaw: { ok: false, failed: true, error: `Captain couldn't start its services: ${RAW}` },
  stopped: { ok: false, failed: true, stopped: true, error: "Captain's services stopped." },
  transport: { ok: false, error: RAW },
};
const SCREENS = {
  'captain-data': ['tab-labels', 'tab-tables', 'tab-notebooks', 'tab-models', 'tab-datasets'],
  'captain-runs': ['tab-workflows', 'tab-grading', 'tab-replays', 'tab-backtests'],
  'captain-compare': ['tab-queries', 'tab-segments', 'tab-surfaces', 'tab-overview'],
};

let checked = 0;
for (const [mod, tabs] of Object.entries(SCREENS)) {
  if (!fs.existsSync(path.join(root, 'defaults/mods', mod))) continue;
  const work = fs.mkdtempSync(path.join(os.tmpdir(), `${mod}-down-`));
  fs.cpSync(path.join(root, 'defaults/mods', mod), work, { recursive: true });
  fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
  fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
  const { default: Mod } = await import(path.join(work, 'main.js'));
  for (const [mode, answer] of Object.entries(MODES)) {
    globalThis.__down = answer;
    globalThis.__shared = {};
    const h = harness(Mod);
    await h.start();
    const seen = [h.text()];
    for (const t of tabs) {
      if (h.byIdPrefix(t).length) {
        await h.click(t);
        await h.layoutLists({});
        seen.push(h.text());
      }
    }
    const all = seen.join(' || ');
    if (process.env.SHOW) console.log(mod, mode, '::', all.replace(/\s+/g, ' ').slice(0, 260));
    const bad = all.match(FORBIDDEN);
    assert.ok(!bad, `${mod}, backend ${mode}: shows "${bad?.[0]}" in: ${all.slice(Math.max(0, all.search(FORBIDDEN) - 80), all.search(FORBIDDEN) + 80)}`);
    if (mode !== 'starting') {
      assert.ok(/Couldn't load|This took too long|Captain's services (stopped|didn't start)/.test(all), `${mod}, backend ${mode}: no plain sentence in: ${all.slice(0, 200)}`);
      assert.ok(h.byIdPrefix('').some((e) => /-retry$/.test(String(e.call('id')?.[0] ?? ''))), `${mod}, backend ${mode}: no Retry`);
    }
    checked++;
  }
  globalThis.__down = null;
}
assert.ok(checked >= 3, 'no screens checked');
console.log(`backend-down render test: ok (${checked} screen and state pairs)`);
