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
import { shared } from "herdr";
import { api } from "./cst.js";

export const WAIT_S = 25; // how long the services may hold one request (the app's fetch gives up at 30 s)
export const FALLBACK_MS = 2000; // how often every watcher re-reads while the feed is down
export const PUMP_MS = 250; // how often the kit looks at whether to ask again
const QUICK_MS = 400; // an answer this fast with nothing in it means the services did not hold the request
const IDLE_GAP_MS = 1000; // so it is asked again a second later, not at once
const SETTLED_MS = 1500; // a watcher this old when the channel first connects has had its own first read come and go
const PAUSE_MS = 1000; // no pump at all for this long was a pause, not a view going away
const GONE_MS = 3000; // a view whose timer has not ticked for this long has gone away
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000]; // between tries while the feed is down

const S = {
  watchers: new Set(), // {topics, fn, cx}
  seq: null, // the last sequence number heard
  state: "connecting", // connecting | live | reconnecting
  since: 0, // when the state last changed
  asking: false,
  nextAsk: 0, // not before this time
  failures: 0,
  lastFallback: 0,
  lastPump: 0,
  owners: new WeakMap(), // a view's cx -> {beat}: the view that drives the channel and owns its watchers
  ask: (path, opts) => api(path, opts), // the request (a test puts its own here)
  now: () => Date.now(),
};

/** For tests: the request function and the clock. */
export function liveTransport(ask, now) { if (ask) S.ask = ask; if (now) S.now = now; }

/** Forget everything (a test starting over). */
export function liveReset() {
  S.watchers.clear();
  Object.assign(S, { seq: null, state: "connecting", since: 0, asking: false, nextAsk: 0, failures: 0, lastFallback: 0, lastPump: 0, owners: new WeakMap() });
}

/** "live" while the feed answers; "reconnecting" while it can't be reached (the screens are then re-read every two
 *  seconds); "connecting" before the first answer. `since` is when that began. */
export function liveState() { return { state: S.state, since: S.since, seq: S.seq, watchers: S.watchers.size }; }

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

/** A view that went away stops ticking: its watchers are dropped, so nothing is read for a screen nobody sees and
 *  its topics are no longer asked for. */
function dropGone() {
  const now = S.now();
  for (const w of [...S.watchers]) if (now - w.owner.beat > GONE_MS) S.watchers.delete(w);
}

function tell(why, topics = null, only = null) {
  dropGone();
  for (const w of [...S.watchers]) {
    if (!S.watchers.has(w) || (only && !only(w))) continue;
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
  // After a pause in which no view's timer ran (the computer slept, the app was held up), nobody can be told from
  // gone: every view starts afresh, and only one that stays silent from here is dropped.
  const t = S.now();
  if (t - S.lastPump > PAUSE_MS) for (const w of S.watchers) w.owner.beat = t;
  S.lastPump = t;
  dropGone();
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
  const path = `/api/changes?topics=${encodeURIComponent(topics.join(","))}&wait=${WAIT_S}${S.seq !== null ? `&since=${S.seq}` : ""}`;
  const go = async (cx) => {
    let r;
    try { r = await S.ask(path, { timeout: WAIT_S + 4, scoped: false }); } catch { r = { ok: false }; }
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
    // The first answer of all. A screen read once when it showed; if the channel only reached the services well
    // after that (they were still coming up, and the screen's own read may have failed), it reads again now.
    else if (first) { const t = S.now(); tell("connected", null, (w) => w.early || t - w.at > SETTLED_MS); }
    else if (changed.length) tell("changed", changed);
    if (back) cx?.notify?.();
  };
  if (cx?.spawn) cx.spawn(go); else go(cx);
}

/** Call `onChange(cx, why, topics)` when one of `topics` changes. `why` is "changed" (with the topics that did),
 *  "reset", "reconnected" or "connected" (something may have been missed: read everything), or "fallback" (the feed is down: this
 *  is the two-second re-read). Returns a function that stops watching. The first watcher in a mod starts the mod's
 *  one connection; when the view goes away its timer goes with it. */
export function watchLive(cx, topics, onChange) {
  // Each view drives the channel from its own timer, once. The mod's state is shared, so a second view of the same
  // mod (a page opened again) must start its own: the first view's timer may be gone with it.
  let owner = S.owners.get(cx);
  if (!owner) {
    owner = { beat: S.now() };
    S.owners.set(cx, owner);
    const mine = owner;
    cx.timer.every(PUMP_MS, (cx) => { mine.beat = S.now(); livePump(cx); });
  }
  const w = { topics: [...new Set((Array.isArray(topics) ? topics : [topics]).filter(Boolean).map(String))], fn: onChange, cx, owner, at: S.now(),
    // Registered before the services were up: the screen's own first read cannot have worked.
    early: shared("captain.daemon")?.state !== "ready" };
  S.watchers.add(w);
  // A new topic: the request in flight does not ask for it, so the next one goes out at once.
  S.nextAsk = 0;
  return () => { S.watchers.delete(w); };
}

/** Change what a watcher follows (a page that opens another thing): returns the new stop function. */
export function rewatch(stop, cx, topics, onChange) {
  stop?.();
  return watchLive(cx, topics, onChange);
}
