import { register } from 'node:module';
register('./hooks.mjs', import.meta.url);

// The app's script engine (QuickJS, vendor/gpui-kit crates/shell/src/engine/quickjs/sandbox.rs, ABSENT_GLOBALS) has
// no setTimeout, setInterval, clearTimeout or clearInterval: each throws, with the same sentence as below. Node has
// them, so a mod that used one passed every render test and failed in the real window. Here they throw for a mod
// too. A mod is any code outside tests/ and Node itself: the screens under defaults/, and the copies of them the
// tests make in a temp folder. The tests and the harness keep the real ones. setImmediate is Node's alone: the
// engine has no such name at all.
const ABSENT = {
  setTimeout: 'use cx.timer.after(ms, callback)',
  setInterval: 'use cx.timer.every(ms, callback)',
  clearTimeout: 'cancel the handle returned by cx.timer.after',
  clearInterval: 'cancel the handle returned by cx.timer.every',
  setImmediate: 'use cx.timer.after(1, callback)',
};
const self = import.meta.url;
function fromMod() {
  const lines = String(new Error().stack ?? '').split('\n').slice(1);
  // The first frame that is not this file is whoever called the timer.
  const caller = lines.find((l) => !l.includes(self));
  if (!caller || !caller.includes('file://')) return false;        // Node's own code (fetch's internals, a stream)
  return !caller.includes('/tests/') && !caller.includes('/node_modules/');
}
for (const [name, hint] of Object.entries(ABSENT)) {
  const real = globalThis[name];
  if (typeof real !== 'function') continue;
  const stub = function (...args) {
    if (fromMod()) throw new TypeError(`\`${name}\` is not available in the shell: ${hint}`);
    return real.apply(this, args);
  };
  Object.assign(stub, real); // setTimeout.__promisify__ and friends
  globalThis[name] = stub;
}
