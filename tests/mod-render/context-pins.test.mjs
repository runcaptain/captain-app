// "Send to agent" pins a thing in <app home>/context/<workspace>.json. The app replaces a file only when told the
// text it replaces, so the second pin used to be refused and every "Send to agent" after the first failed with
// "Couldn't pin that". This pins three things in a row, unpins one, survives another writer changing the file in
// between, and checks that a real failure says where the file is and what the app said.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/context-pins.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname);
globalThis.__shared = { 'captain.workspace': 'shop demos' };
globalThis.__files = {};
const ctx = await import(path.resolve(here, '../../defaults/captain/kit/context.js'));
const herdr = await import('herdr');
const file = `${herdr.appHome()}/context/shop_demos.json`;
const refs = () => JSON.parse(globalThis.__files[file]).items.map((i) => i.ref);

await ctx.pin({ ref: 'staging:outdoor', kind: 'staging', label: 'staging for Outdoor' });
assert.deepEqual(refs(), ['staging:outdoor'], 'the first pin makes the file');
await ctx.pin({ ref: 'candidate:outdoor/exp-1', kind: 'candidate', label: 'starter-ranker@v2 in Outdoor' });
await ctx.pin({ ref: 'dataset:demo-catalog@v1', kind: 'dataset', label: 'demo-catalog@v1' });
assert.deepEqual(refs(), ['staging:outdoor', 'candidate:outdoor/exp-1', 'dataset:demo-catalog@v1'], 'the second and third pins land too');
await ctx.pin({ ref: 'staging:outdoor', kind: 'staging', label: 'staging for Outdoor' });
assert.deepEqual(refs(), ['candidate:outdoor/exp-1', 'dataset:demo-catalog@v1', 'staging:outdoor'], 'pinning again moves it to the end');
await ctx.unpin('dataset:demo-catalog@v1');
assert.deepEqual(refs(), ['candidate:outdoor/exp-1', 'staging:outdoor']);

// Another writer (cst context add, from a terminal) changes the file between the read and the write: read again.
const before = globalThis.__files[file];
const origWrite = globalThis.__files;
let first = true;
globalThis.__files = new Proxy(origWrite, { get(t, k) { const v = t[k]; if (k === file && first) { first = false; queueMicrotask(() => { const d = JSON.parse(t[file]); d.items.push({ ref: 'run:late', kind: 'run', label: 'late' }); t[file] = JSON.stringify(d); }); } return v; } });
await ctx.pin({ ref: 'project:outdoor', kind: 'project', label: 'Outdoor' });
globalThis.__files = origWrite;
assert.ok(refs().includes('project:outdoor') && refs().includes('run:late'), `neither writer loses its pin: ${refs()} (was ${before.length} bytes)`);

// "Send to agent" end to end: pinned, a reference in the agent tab's input, and a note that says so.
globalThis.__toasts = []; globalThis.__runs = [];
globalThis.__agentList = [{ address: 't7', status: 'idle', tab_name: 'Claude Code', focused: true }];
const cx = { spawn: (f) => f(cx), notify() {} };
await ctx.sendToAgent(cx, { ref: 'staging:outdoor', kind: 'staging', label: 'staging for Outdoor' });
assert.equal(globalThis.__toasts.at(-1).title, 'Sent to your agent');

// A real failure says what failed: the file and the app's reason, not just "couldn't write".
const frozen = globalThis.__files;
globalThis.__files = new Proxy(frozen, { get(t, k) { return k === file ? 'someone else holds this' + Math.random() : t[k]; } });
await ctx.sendToAgent(cx, { ref: 'dataset:x@v1', kind: 'dataset', label: 'x@v1' });
globalThis.__files = frozen;
const bad = globalThis.__toasts.at(-1);
assert.equal(bad.title, "Couldn't pin that");
assert.ok(bad.body.includes(file) && bad.body.includes('The file changed while saving'), `the message names the file and the reason: ${bad.body}`);
assert.ok(!refs().includes('dataset:x@v1'), 'and nothing was half written');
console.log('context pins render test: ok');
process.exit(0);
