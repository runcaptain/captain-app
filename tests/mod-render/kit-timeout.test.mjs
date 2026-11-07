// The kit ends a request that never answers: with keepAwake running, api() resolves "This took too long." once its
// deadline passes, so a screen retries instead of loading forever.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/kit-timeout.test.mjs
import assert from 'node:assert/strict';
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://127.0.0.1:9', token: 't' } };
globalThis.fetch = () => new Promise(() => {}); // never answers
const { api, keepAwake } = await import('../../defaults/captain/kit/cst.js');
const ticks = [];
keepAwake({ timer: { every: (_ms, f) => ticks.push(f) } });
const p = api('/api/backtests', { timeout: 0.2 });
await new Promise((r) => setTimeout(r, 300));
for (const f of ticks) f({ notify() {} });
const r = await p;
assert.equal(r.ok, false);
assert.equal(r.error, 'This took too long.');
assert.ok(!r.status, 'no status: screens treat it as a failure to retry');
console.log('kit timeout test: ok');
process.exit(0);
