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
assert.equal(asked[0], `/api/changes?topics=${encodeURIComponent('deployed,deployment:shop,run:')}&wait=${WAIT_S}&client=${liveState().client}`, 'the first request has no since: it learns where the feed is');
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

// ---- a new topic is listened to at once, not when the open request ends (up to 25 s) ------------------------------
liveReset();
asked.length = 0; held.length = 0;
const page = [];
answer = () => ok(40);
let stopPage = watchLive(cx, ['project:shop'], (_cx, why, topics) => page.push([why, topics]));
await tick();
answer = null;
await tick(1100); // the first answer came at once, so the next request waits a second
assert.equal(held.length, 1, 'one request is open, for the first project');
const open = asked.length;
// The page opens another project: no tick, no wait.
stopPage();
stopPage = watchLive(cx, ['project:gifts'], (_cx, why, topics) => page.push([why, topics]));
await new Promise((r) => setImmediate(r));
assert.ok(asked.length > open, 'a new request went out the moment the topics changed');
assert.ok(decodeURIComponent(asked.at(-1)).includes('topics=project:gifts&') && asked.at(-1).endsWith('&since=40'), asked.at(-1));
assert.ok(asked.at(-1).includes(`&client=${liveState().client}`), 'it names this mod, so the feed lets the older request go');
// The old request's answer, whenever it comes, is dropped: it cannot wind the sequence back or call anyone.
const old = held.shift();
held.pop()(ok(41, ['project:gifts']));
await new Promise((r) => setImmediate(r));
assert.deepEqual(page, [['changed', ['project:gifts']]], 'the new project is heard');
old(ok(40, []));
await new Promise((r) => setImmediate(r));
assert.equal(liveState().seq, 41, 'an answer to a request left behind changes nothing');
await tick(); await tick();
assert.equal(held.length, 1, 'and still one request open, not two');
// The same topics again do not reopen anything.
const same = asked.length;
watchLive(cx, ['project:gifts'], () => {});
await new Promise((r) => setImmediate(r));
assert.equal(asked.length, same, 'a second watcher of the same topic asks nothing new');
// ---- a second view of the same mod hears changes after the first view is gone ----------------------------------
// The channel's state is shared by every view of a mod. A page opened again must drive it from its own timer:
// the first view's timer went with the first view.
{
  liveReset();
  const t1 = [], t2 = [];
  const cx1 = { timer: { every: (_ms, f) => t1.push(f) }, spawn: (f) => f(cx1), notify: () => {} };
  const cx2 = { timer: { every: (_ms, f) => t2.push(f) }, spawn: (f) => f(cx2), notify: () => {} };
  watchLive(cx1, ['deployed'], () => {});
  const heard2 = [];
  watchLive(cx2, ['notebooks'], (_cx, why) => heard2.push(why));
  assert.equal(t2.length, 1, 'the second view has a timer of its own');
  let n = 0;
  answer = () => ok(3000 + (n += 1), n > 1 ? ['notebooks'] : []);
  // Only the second view's timer runs from here: the first view is gone.
  for (let i = 0; i < 12; i++) { now += 250; for (const f of t2) f(cx2); await new Promise((r) => setImmediate(r)); }
  assert.ok(heard2.includes('changed'), 'it hears changes with the first view gone');
}

// ---- a long pause (the computer asleep) loses nobody ---------------------------------------------------------------
{
  liveReset();
  const t3 = [];
  const cx3 = { timer: { every: (_ms, f) => t3.push(f) }, spawn: (f) => f(cx3), notify: () => {} };
  const heard3 = [];
  watchLive(cx3, ['runs'], (_cx, why) => heard3.push(why));
  let n = 0;
  answer = () => ok(5000 + (n += 1), n > 1 ? ['runs'] : []);
  const tick3 = async (ms = 250) => { now += ms; for (const f of t3) f(cx3); await new Promise((r) => setImmediate(r)); };
  await tick3(); await tick3();
  const before = heard3.length;
  now += 8 * 3600 * 1000; // eight hours with no timer running
  await tick3(); await tick3();
  assert.equal(liveState().watchers, 1, 'the view is still watched after the pause');
  assert.ok(heard3.length > before, 'and still hears changes');
}


// ---- the first answer names what changed just before: a screen read a moment earlier, so it reads those once more --
liveReset();
asked.length = 0; held.length = 0;
const early = [];
answer = () => ({ ok: true, data: { seq: 70, changed: [], reset: false, recent: [{ topic: 'mlflow', seq: 69 }, { topic: 'notebooks', seq: 68 }] } });
watchLive(cx, ['mlflow'], (_cx, why, topics) => early.push([why, topics]));
watchLive(cx, ['projects'], (_cx, why, topics) => early.push(['other', why, topics]));
await tick();
assert.deepEqual(early, [['changed', ['mlflow']]], 'only the watcher of what just changed reads again, and nobody else');
answer = null;
console.log('live channel: ok');
process.exit(0);
