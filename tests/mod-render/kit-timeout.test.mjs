// The kit ends a request that never answers: fetch gives up after 30 s by itself, and api() turns that into
// "This took too long.", so a screen retries instead of loading forever.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/kit-timeout.test.mjs
import assert from 'node:assert/strict';
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://127.0.0.1:9', token: 't' } };
globalThis.fetch = () => Promise.reject(new Error('error sending request for url: operation timed out'));
const { api } = await import('../../defaults/captain/kit/cst.js');
const r = await api('/api/backtests');
assert.equal(r.ok, false);
assert.equal(r.timedOut, true);
assert.equal(r.error, 'This took too long.');
assert.ok(!r.status, 'no status: screens treat it as a failure to retry');
console.log('kit timeout test: ok');
process.exit(0);
