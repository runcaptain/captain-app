// The one live channel (kit/live.js). The owner: "Everything that you see on every screen should be live updating
// every second... instant sync, instant awareness." A screen watches topics; the kit holds one request to the
// services' change feed per mod and calls the screen the moment a topic changes. While the feed is down every
// watcher is re-read every two seconds and the screen says "Reconnecting".
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/live-channel.test.mjs
import assert from 'node:assert/strict';
import { watchLive, livePump, liveState, liveTransport, liveReset, updatedWords, covers, FALLBACK_MS, WAIT_S } from '../../defaults/captain/kit/live.js';

globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
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

// ---- a second view of the same mod drives the channel itself, and a view that went away is dropped --------------
{
  const timers2 = [];
  const cx2 = { timer: { every: (_ms, f) => timers2.push(f) }, spawn: (f) => f(cx2), notify: () => {} };
  const heard2 = [];
  watchLive(cx2, ['notebooks'], (_cx, why) => heard2.push(why));
  watchLive(cx2, ['settings'], () => {});
  assert.equal(timers2.length, 1, 'its own timer, once, however many watchers it adds');
  const before = liveState().watchers;
  // The first view goes away: its timer stops. Only the second view ticks from here.
  answer = (path) => ok(200 + asked.length, decodeURIComponent(path).includes('notebooks') ? ['notebooks'] : []);
  const tick2 = async (ms = 250) => { now += ms; for (const f of timers2) f(cx2); await new Promise((r) => setImmediate(r)); };
  for (let i = 0; i < 16; i++) await tick2();
  assert.ok(heard2.includes('changed'), 'the second view hears changes with the first one gone');
  assert.ok(liveState().watchers < before, 'the first view\'s watchers were dropped');
  assert.ok(!decodeURIComponent(asked.at(-1)).includes('deployment:shop'), 'and its topics are no longer asked for');
}
console.log('live channel: ok');
process.exit(0);

// ---- the channel reaching the services late reads a screen whose own first read may have failed ---------------
{
  liveReset();
  const t4 = [];
  const cx4 = { timer: { every: (_ms, f) => t4.push(f) }, spawn: (f) => f(cx4), notify: () => {} };
  const heard4 = [];
  watchLive(cx4, ['data'], (_cx, why) => heard4.push(why));
  answer = () => ok(700);
  now += 5000; // the services took a while to come up before the channel first asked
  now += 250; for (const f of t4) f(cx4); await new Promise((r) => setImmediate(r));
  assert.deepEqual(heard4, ['connected'], 'read again on the first answer');
  // A screen that connected at once is not read twice.
  liveReset();
  const heard5 = [];
  watchLive(cx4, ['data'], (_cx, why) => heard5.push(why));
  now += 250; for (const f of t4) f(cx4); await new Promise((r) => setImmediate(r));
  assert.deepEqual(heard5, [], 'connected at once: its own first read stands');
}

// ---- a screen that showed before the services were up is read when the channel first reaches them -----------
{
  liveReset();
  globalThis.__shared['captain.daemon'] = { state: 'starting' };
  const t6 = [];
  const cx6 = { timer: { every: (_ms, f) => t6.push(f) }, spawn: (f) => f(cx6), notify: () => {} };
  const heard6 = [];
  watchLive(cx6, ['projects'], (_cx, why) => heard6.push(why));
  globalThis.__shared['captain.daemon'] = { state: 'ready', base: 'http://captain.test', token: 't' };
  answer = () => ok(900);
  now += 250; for (const f of t6) f(cx6); await new Promise((r) => setImmediate(r));
  assert.deepEqual(heard6, ['connected'], 'its first read could not have worked, so it reads now, at once');
}

// ---- a pause in which nothing ran (the computer slept) drops nobody ----------------------------------------------
{
  liveReset();
  const t3 = [];
  const cx3 = { timer: { every: (_ms, f) => t3.push(f) }, spawn: (f) => f(cx3), notify: () => {} };
  const heard3 = [];
  watchLive(cx3, ['runs'], (_cx, why) => heard3.push(why));
  answer = () => ok(500 + asked.length, ['runs']);
  const tick3 = async (ms = 250) => { now += ms; for (const f of t3) f(cx3); await new Promise((r) => setImmediate(r)); };
  await tick3(); await tick3();
  now += 8 * 3600 * 1000; // eight hours asleep
  await tick3(); await tick3();
  assert.equal(liveState().watchers, 1, 'the view is still watched after a long pause');
  assert.ok(heard3.length >= 2, 'and still hears changes');
}

// ---- a watcher that stops is no longer called, and its topics are no longer asked for ------------------------------
stopList();
const quiet = heard.list.length;
answer = (path) => (path.includes('deployed,') ? ok(99, ['deployed']) : ok(30, []));
await tick(1000); await tick(1000);
assert.equal(heard.list.length, quiet);
assert.ok(!decodeURIComponent(asked.at(-1)).includes('deployed,'), 'only what is still watched is asked for');
console.log('live channel: ok');
process.exit(0);
