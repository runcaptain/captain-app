// The one live channel (kit/live.js). The owner: "Everything that you see on every screen should be live updating
// every second... instant sync, instant awareness." A screen watches topics; the kit holds one request to the
// services' change feed per mod and calls the screen the moment a topic changes. While the feed is down every
// watcher is re-read every two seconds and the screen says "Reconnecting".
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/live-channel.test.mjs
import assert from 'node:assert/strict';
import { watchLive, livePump, liveState, liveTransport, liveReset, updatedWords, covers, FALLBACK_MS, WAIT_S } from '../../defaults/captain/kit/live.js';

let now = 1_000_000;
const asked = [];
let answer = null; // what the feed says next: a function of the path, or null to hold the request open
const held = [];
liveTransport((path) => {
  asked.push(path);
  const a = answer?.(path);
  if (a) return Promise.resolve(a);
  return new Promise((resolve) => held.push(resolve));
}, () => now);
const timers = [];
const cx = { timer: { every: (_ms, f) => timers.push(f) }, spawn: (f) => f(cx), notify: () => {} };
const tick = async (ms = 250) => { now += ms; for (const f of timers) f(cx); await new Promise((r) => setImmediate(r)); };
const ok = (seq, changed = [], reset = false) => ({ ok: true, data: { seq, changed: changed.map((topic) => ({ topic, seq })), reset } });

// ---- one connection, shared by every watcher, asking for the union of their topics --------------------------------
liveReset();
const heard = { list: [], page: [] };
const stopList = watchLive(cx, ['deployed'], (_cx, why, topics) => heard.list.push([why, topics]));
watchLive(cx, ['deployment:shop', 'run:'], (_cx, why, topics) => heard.page.push([why, topics]));
assert.equal(timers.length, 1, 'one timer for the whole mod, however many watchers');
answer = () => ok(10);
await tick();
assert.equal(asked.length, 1);
assert.equal(asked[0], `/api/changes?topics=${encodeURIComponent('deployed,deployment:shop,run:')}&wait=${WAIT_S}`, 'the first request has no since: it learns where the feed is');
assert.deepEqual([liveState().state, liveState().seq], ['live', 10]);
assert.deepEqual(heard, { list: [], page: [] }, 'learning where the feed is calls nobody: each screen read once when it showed');

// ---- held open, and answered the moment a topic changes ----------------------------------------------------------
answer = null;
await tick(1000);
assert.equal(asked.length, 2, 'one request in flight');
assert.ok(asked[1].endsWith('&since=10'));
await tick(); await tick(); await tick();
assert.equal(asked.length, 2, 'and no second one while it is held');
held.shift()(ok(12, ['deployed', 'deployment:shop']));
await new Promise((r) => setImmediate(r));
assert.deepEqual(heard.list, [['changed', ['deployed']]], 'the list hears its topic, at once, not at a timer');
assert.deepEqual(heard.page, [['changed', ['deployment:shop']]], 'and the page hears its own');
await tick();
assert.ok(asked.at(-1).endsWith('&since=12'), 'asked again at once, from where it got to');
// A topic nobody here watches calls nobody. A watcher of "run:" hears any run.
held.shift()(ok(13, ['notebooks']));
await new Promise((r) => setImmediate(r));
assert.equal(heard.list.length + heard.page.length, 2);
await tick();
held.shift()(ok(14, ['run:abc']));
await new Promise((r) => setImmediate(r));
assert.deepEqual(heard.page.at(-1), ['changed', ['run:abc']]);
assert.ok(covers('run:', 'run:abc') && covers('runs', 'runs') && !covers('runs', 'run:abc') && !covers('run:a', 'run:abc'));

// ---- "reset": something was missed, so every watcher reads once -----------------------------------------------
await tick();
now += 3000; // a held request takes time before it is answered
held.shift()(ok(3, [], true));
await new Promise((r) => setImmediate(r));
assert.deepEqual([heard.list.at(-1), heard.page.at(-1)], [['reset', []], ['reset', []]]);
// The sequence going backwards means the services restarted: the same.
await tick();
now += 3000;
held.shift()(ok(1));
await new Promise((r) => setImmediate(r));
assert.equal(heard.list.at(-1)[0], 'reset');

// ---- down: "Reconnecting", a two-second re-read, and backoff on the feed itself ---------------------------------
const before = { list: heard.list.length, asks: asked.length };
answer = () => ({ ok: false, failed: true });
await tick(600);
assert.equal(liveState().state, 'reconnecting');
assert.equal(updatedWords(now - 500), 'Reconnecting', 'the screen never looks fine while it is stale');
for (let i = 0; i < 16; i++) await tick(); // four seconds
const fallbacks = heard.list.slice(before.list).filter(([why]) => why === 'fallback').length;
assert.ok(fallbacks >= 1 && fallbacks <= 3, `re-read about every ${FALLBACK_MS} ms while down: ${fallbacks}`);
assert.ok(asked.length - before.asks <= 5, `the feed itself is tried with backoff, not hammered: ${asked.length - before.asks}`);
// Back: every watcher reads once, and the words go back to a time.
answer = () => ok(20);
for (let i = 0; i < 40 && liveState().state !== 'live'; i++) await tick();
assert.equal(liveState().state, 'live');
assert.equal(heard.list.at(-1)[0], 'reconnected', 'what was missed while down is read');
assert.equal(updatedWords(now - 500), 'Updated just now');
assert.equal(updatedWords(now - 4200), 'Updated 4 s ago');
assert.equal(updatedWords(now - 190000), 'Updated 3 min ago');

// ---- a feed that answers at once (the stand-in before the long poll exists) is asked once a second ----------------
const n0 = asked.length;
for (let i = 0; i < 20; i++) await tick(); // five seconds
assert.ok(asked.length - n0 >= 4 && asked.length - n0 <= 6, `about once a second: ${asked.length - n0}`);

// ---- a watcher that stops is no longer called, and its topics are no longer asked for ------------------------------
stopList();
const quiet = heard.list.length;
answer = (path) => (path.includes('deployed,') ? ok(99, ['deployed']) : ok(30, []));
await tick(1000); await tick(1000);
assert.equal(heard.list.length, quiet);
assert.ok(!decodeURIComponent(asked.at(-1)).includes('deployed,'), 'only what is still watched is asked for');
console.log('live channel: ok');
process.exit(0);
