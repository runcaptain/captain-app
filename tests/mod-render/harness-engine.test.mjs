// The render harness behaves as the app's script engine does, where it matters for what a mod may rely on.
//
// Two faults came from the harness being kinder than the app. A callback that re-armed itself with timer.after
// multiplied here without end ("RangeError: Invalid array length"), and a wait that polled on a one-shot timer
// passed here while a row in the real window said "Stopping" for two hours. And Node has setTimeout, which the
// engine does not (QuickJS: vendor/gpui-kit crates/shell/src/engine/quickjs/sandbox.rs, ABSENT_GLOBALS), so a mod
// that used it passed every render test and threw in the window.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/harness-engine.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { tidy } from './backend.mjs';

// ---- a mod has no setTimeout, setInterval, clearTimeout, clearInterval or setImmediate -----------------------------
{
  const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'engine-mod-')));
  fs.writeFileSync(path.join(dir, 'main.js'), `
    export const tryIt = (name) => { try { globalThis[name](() => {}, 1); return null; } catch (e) { return String(e.message); } };
    export const viaFetch = async () => { try { await fetch("http://127.0.0.1:9/nothing"); } catch (e) { return String(e?.cause?.code ?? e.message); } return "answered"; };
  `);
  const mod = await import(path.join(dir, 'main.js'));
  assert.equal(mod.tryIt('setTimeout'), '`setTimeout` is not available in the shell: use cx.timer.after(ms, callback)', "the engine's own sentence");
  assert.equal(mod.tryIt('setInterval'), '`setInterval` is not available in the shell: use cx.timer.every(ms, callback)');
  assert.match(mod.tryIt('clearTimeout'), /is not available in the shell/);
  assert.match(mod.tryIt('clearInterval'), /is not available in the shell/);
  assert.match(mod.tryIt('setImmediate'), /is not available in the shell/);
  // A mod's fetch still works: Node's own code under it may use its timers.
  assert.ok(!/not available in the shell/.test(await mod.viaFetch()), 'fetch from a mod is not caught by this');
  // The tests and the harness keep the real ones.
  await new Promise((r) => setTimeout(r, 1));
  await new Promise((r) => setImmediate(r));
}

// ---- timers: `after` is called once, `every` at its interval, and both can be cancelled -----------------------------
{
  const seen = { after: 0, every: 0, rearm: 0, cancelled: 0, slow: 0 };
  class Mod {
    init(_props, cx) {
      cx.timer.after(30, () => { seen.after++; });
      cx.timer.every(40, () => { seen.every++; });
      cx.timer.every(60000, () => { seen.slow++; });
      // A callback that arms itself again: one chain, never more.
      const again = (cx) => { seen.rearm++; cx.timer.after(25, again); };
      cx.timer.after(25, again);
      const h = cx.timer.every(10, () => { seen.cancelled++; });
      h.cancel();
    }
    render() { return { kind: 'div', kids: [] }; }
  }
  const h = harness(Mod);
  await h.start();
  assert.deepEqual(seen, { after: 0, every: 0, rearm: 0, cancelled: 0, slow: 0 }, 'nothing is called before its time');
  await h.idle(400);
  assert.equal(seen.after, 1, 'a one-shot timer is called once, however long passes');
  assert.ok(seen.every >= 5 && seen.every <= 11, `an interval is called at about its interval: ${seen.every} in 400 ms at 40 ms`);
  assert.ok(seen.rearm >= 5 && seen.rearm <= 17, `a callback that arms itself again is one chain: ${seen.rearm} in 400 ms at 25 ms`);
  assert.equal(seen.cancelled, 0, 'a cancelled timer is never called');
  assert.equal(seen.slow, 0, 'a minute has not passed');
  // Time passes while the test awaits something else, as it does in the app.
  const before = seen.every;
  await new Promise((r) => setTimeout(r, 200));
  assert.ok(seen.every > before, 'timers are called while the test is not looking');
  // One forced pass of the clock, for a test that does not want to wait a minute: every interval once more.
  await h.layoutLists({});
  assert.ok(seen.slow >= 1, 'layoutLists turns every interval over');
  assert.equal(seen.after, 1, 'and a one-shot that was called is gone for good');
}
console.log('harness engine: ok');
process.exit(0);
