// CAPTAIN: the Deployments screen's line to the app's change feed (GET /api/changes, a long poll). The screen says
// which topics it shows and is told the moment one changes, then reads again what it already reads. Nothing here
// waits on a clock: one request is held open by the service, and the next is sent when it answers.
//
// A small local helper until the shared kit has one (kit/live.js): `watchLive(cx, topics, onChange)` is the same
// call, so the swap is this import.
import { api } from "./kit/cst.js";

const HOLD_S = 25;     // how long the service holds a request with nothing to say
const DEAD_MS = 45000; // no answer at all for this long: the channel is down

/** Listen for changes. `topics()` returns the topics shown now (it is asked before every request, so a page
 *  opened or a job started is listened to from the next one). `onChange(cx, changed, reset)` is called with the
 *  topics that changed, or with `reset` true when everything must be read again (the first answer, a service that
 *  restarted, a client too far behind). Returns the channel: `up()` says whether it is alive, `kick(cx)` sends a
 *  new request now (after the topics changed, or to try again while it is down). */
export function watchLive(cx, topics, onChange) {
  const ch = { seq: 0, run: 0, flying: false, heardAt: 0, failed: false,
    up() { return !this.failed && Date.now() - this.heardAt < DEAD_MS; },
    kick(cx) { this.run += 1; this.flying = false; ask(cx, this, topics, onChange); } };
  ask(cx, ch, topics, onChange);
  return ch;
}

function ask(cx, ch, topics, onChange) {
  if (ch.flying) return;
  ch.flying = true;
  const mine = ch.run;
  const want = [...new Set(topics())].sort().join(",");
  cx.spawn(async (cx) => {
    let r;
    try {
      r = await api(`/api/changes?since=${ch.seq}&topics=${encodeURIComponent(want)}&hold=${HOLD_S}`, { timeout: HOLD_S + 15 });
    } catch (e) { r = { ok: false }; }
    if (mine !== ch.run) return;  // a newer request took over (the topics changed): this answer is for the old ones
    ch.flying = false;
    if (!r.ok || typeof r.data?.seq !== "number") {
      // Down. Nothing is retried in a loop here: the screen's fallback tick calls `kick` every two seconds.
      const was = !ch.failed;
      ch.failed = true;
      if (was) cx.notify();
      return;
    }
    const first = ch.seq === 0 || ch.failed;
    ch.failed = false;
    ch.heardAt = Date.now();
    ch.seq = r.data.seq;
    // The feed names each change either as a topic or as {topic, seq}: both are read.
    const changed = (r.data.changed ?? []).map((x) => (typeof x === "string" ? x : x?.topic)).filter(Boolean);
    if (first || r.data.reset) onChange(cx, [], true);
    else if (changed.length) onChange(cx, changed, false);
    ask(cx, ch, topics, onChange);  // and wait for the next change
  });
}
