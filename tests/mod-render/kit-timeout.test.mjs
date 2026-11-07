// The kit turns a request that times out (fetch's own deadline) into "This took too long.", with no status, so a
// screen retries it instead of loading forever, and never shows the transport's words.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/kit-timeout.test.mjs
import assert from 'node:assert/strict';
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://127.0.0.1:9', token: 't' } };
globalThis.fetch = async () => { throw new Error('operation timed out after 30s'); };
const { api } = await import('../../defaults/captain/kit/cst.js');
const r = await api('/api/backtests');
assert.equal(r.ok, false);
assert.equal(r.error, 'This took too long.');
assert.ok(r.timedOut && !r.status, 'a timeout, with no status: screens retry it');
console.log('kit timeout test: ok');
process.exit(0);
