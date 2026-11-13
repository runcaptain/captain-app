// CAPTAIN: the one live channel. Every screen learns that Captain's data changed from here, and from nowhere else.
//
// The owner, after a Deployments row said "Stopping" for two hours while the model was long gone: "Everything that
// you see on every screen should be live updating every second... instant sync, instant awareness."
//
// A screen's contract:
//   - it reads once when it shows;
//   - it reads again the moment one of its topics changes (watchLive);
//   - it reads again right after its own action returns.
// No screen keeps a timer to re-read Captain's data. Timers are only for clocks and counters that tick on the page
// (elapsed time, "Updated 4 s ago"), at one second.
//
//   watchLive(cx, ["deployed", "deployment:shop"], (cx, why) => this.load(cx));
//
// The feed is the services' change feed: GET /api/changes?since=<seq>&topics=a,b&wait=25, held open and answered
// the moment a topic changes: {seq, changed: [{topic, seq}], reset}. One request at a time per mod, shared by every
// watcher in it. While it can't be reached the kit re-reads every FALLBACK_MS instead and liveState() says
// "reconnecting", so a screen says "Reconnecting" in its Updated line and never looks fine while it is stale.
import { api } from "./cst.js";

export const WAIT_S = 25; // how long the services may hold one request (the app's fetch gives up at 30 s)
export const FALLBACK_MS = 2000; // how often every watcher re-reads while the feed is down
export const PUMP_MS = 250; // how often the kit looks at whether to ask again
const QUICK_MS = 400; // an answer this fast with nothing in it means the services did not hold the request
const IDLE_GAP_MS = 1000; // so it is asked again a second later, not at once
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000]; // between tries while the feed is down

const S = {
  watchers: new Set(), // {topics, fn, cx}
  seq: null, // the last sequence number heard
  state: "connecting", // connecting | live | reconnecting
  since: 0, // when the state last changed
  asking: false,
  gen: 0, // rises when the topics change: an answer to an older request is dropped
  askedFor: null, // the topics of the request that is open
  client: `m${Math.random().toString(36).slice(2, 10)}`, // this mod's name to the feed, so it lets an older request go
  nextAsk: 0, // not before this time
  failures: 0,
  lastFallback: 0,
  pumping: false,
  ask: (path, opts) => api(path, opts), // the request (a test puts its own here)
  now: () => Date.now(),
};

let PUMPED = new WeakSet(); // the views that have a pump timer

/** For tests: the request function and the clock. */
export function liveTransport(ask, now) { if (ask) S.ask = ask; if (now) S.now = now; }

/** Forget everything (a test starting over). */
export function liveReset() {
  S.watchers.clear();
  PUMPED = new WeakSet();
  Object.assign(S, { seq: null, state: "connecting", since: 0, asking: false, gen: 0, askedFor: null, nextAsk: 0, failures: 0, lastFallback: 0, pumping: false });
}

/** "live" while the feed answers; "reconnecting" while it can't be reached (the screens are then re-read every two
 *  seconds); "connecting" before the first answer. `since` is when that began. */
export function liveState() { return { state: S.state, since: S.since, seq: S.seq, watchers: S.watchers.size, client: S.client }; }

/** The words for a screen's Updated line. While the feed is down: "Reconnecting". Else how long ago `at` (a time
 *  in ms, the screen's last successful read) was: "Updated just now", "Updated 4 s ago", "Updated 3 min ago". */
export function updatedWords(at, now = S.now()) {
  if (S.state === "reconnecting") return "Reconnecting";
  if (!at) return "";
  const s = Math.max(0, Math.floor((now - at) / 1000));
  if (s < 2) return "Updated just now";
  if (s < 60) return `Updated ${s} s ago`;
  if (s < 3600) return `Updated ${Math.floor(s / 60)} min ago`;
  return `Updated ${Math.floor(s / 3600)} h ago`;
}

/** Does a watcher's topic cover a changed one? Exact, or a watcher's "run:" covers every "run:<id>". */
export function covers(watched, changed) {
  return watched === changed || (watched.endsWith(":") && changed.startsWith(watched));
}

function setState(state) {
  if (S.state === state) return;
  S.state = state;
  S.since = S.now();
}

function tell(why, topics = null) {
  for (const w of [...S.watchers]) {
    if (!S.watchers.has(w)) continue;
    const hit = topics === null ? null : topics.filter((t) => w.topics.some((mine) => covers(mine, t)));
    if (hit !== null && !hit.length) continue;
    try { w.fn(w.cx, why, hit ?? []); } catch { /* one screen's reader must not stop the others */ }
  }
}

function topicsAsked() {
  return [...new Set([...S.watchers].flatMap((w) => w.topics))].sort();
}

/** One step, from the timer: ask the feed if it is time, and re-read on the fallback clock while it is down. */
export function livePump(cx) {
  if (!S.watchers.size) return;
  const now = S.now();
  if (S.state === "reconnecting" && now - S.lastFallback >= FALLBACK_MS) {
    S.lastFallback = now;
    tell("fallback");
  }
  if (S.asking || now < S.nextAsk) return;
  S.asking = true;
  const topics = topicsAsked();
  const started = now;
  const gen = S.gen;
  S.askedFor = topics.join(",");
  const path = `/api/changes?topics=${encodeURIComponent(S.askedFor)}&wait=${WAIT_S}&client=${S.client}${S.seq !== null ? `&since=${S.seq}` : ""}`;
  const go = async (cx) => {
    let r;
    try { r = await S.ask(path, { timeout: WAIT_S + 4, scoped: false }); } catch { r = { ok: false }; }
    // The topics changed while this was open: a newer request has taken its place, and this answer is dropped.
    // Nothing is lost, since the newer one asked from the same sequence.
    if (gen !== S.gen) return;
    S.asking = false;
    const took = S.now() - started;
    // A request held to its end with nothing to say comes back as a timeout from some transports: that is the feed
    // working, not the feed down.
    if (!r?.ok && r?.timedOut && S.state === "live") { S.nextAsk = 0; return; }
    if (!r?.ok || typeof r.data?.seq !== "number") {
      const was = S.state;
      setState("reconnecting");
      S.nextAsk = S.now() + BACKOFF_MS[Math.min(S.failures, BACKOFF_MS.length - 1)];
      S.failures++;
      if (was !== "reconnecting") { S.lastFallback = S.now(); cx?.notify?.(); }
      return;
    }
    const back = S.state !== "live" && S.state !== "connecting";
    const first = S.seq === null;
    const reset = !!r.data.reset || (S.seq !== null && r.data.seq < S.seq);
    const changed = (r.data.changed ?? []).map((x) => (typeof x === "string" ? x : x?.topic)).filter(Boolean);
    S.seq = r.data.seq;
    S.failures = 0;
    setState("live");
    // The services answered at once with nothing: they did not hold the request, so ask again in a second.
    S.nextAsk = !changed.length && took < QUICK_MS ? S.now() + IDLE_GAP_MS : 0;
    // Back after being down, or told that something was missed: every watcher reads once.
    if (back || (reset && !first)) tell(back ? "reconnected" : "reset");
    else if (changed.length) tell("changed", changed);
    if (back) cx?.notify?.();
  };
  if (cx?.spawn) cx.spawn(go); else go(cx);
}

/** The watchers changed. If the topics they ask for are not the ones the open request asked for, that request is
 *  left behind and a new one goes out now: a page that opens another thing is listening to it at once, not when
 *  the old request ends (up to 25 s). The feed lets the old one go when it sees this client ask again. */
function retopic(cx) {
  if (S.askedFor === null || topicsAsked().join(",") === S.askedFor) return;
  S.gen++;
  S.asking = false;
  S.nextAsk = 0;
  if (S.watchers.size) livePump(cx);
}

/** Call `onChange(cx, why, topics)` when one of `topics` changes. `why` is "changed" (with the topics that did),
 *  "reset" or "reconnected" (something may have been missed: read everything), or "fallback" (the feed is down: this
 *  is the two-second re-read). Returns a function that stops watching. The first watcher in a mod starts the mod's
 *  one connection; when the view goes away its timer goes with it. */
export function watchLive(cx, topics, onChange) {
  const w = { topics: [...new Set((Array.isArray(topics) ? topics : [topics]).filter(Boolean).map(String))], fn: onChange, cx };
  S.watchers.add(w);
  // One pump for each view that watches: a view's timer goes away with the view, so a view opened later must
  // not rely on the timer of one that is gone. Two pumps never ask twice: only one request is open at a time.
  if (!PUMPED.has(cx)) {
    PUMPED.add(cx);
    S.pumping = true;
    cx.timer.every(PUMP_MS, (cx) => livePump(cx));
  }
  retopic(cx);
  return () => { S.watchers.delete(w); retopic(cx); };
}

/** Change what a watcher follows (a page that opens another thing): returns the new stop function. */
export function rewatch(stop, cx, topics, onChange) {
  stop?.();
  return watchLive(cx, topics, onChange);
}
