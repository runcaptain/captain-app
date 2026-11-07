// captain-ui: Captain's local services (the cst backend), from a mod.
// The app starts them at launch and keeps shared "captain.daemon" current:
// {state: "ready", base, token, home}, or {state: "starting" | "missing" | "failed", error}.
// Requests go through fetch, in-process, so the token never shows up in `ps`.
// Where they listen comes only from the app (shared "captain.daemon").
import { shared, setShared, focused } from "herdr";

// When this copy of the mod started: services get ~15 s to come up before a screen says so.
const BORN = Date.now();
const PATIENCE_MS = 15000;
// The last failure's own words (a transport error, why the services didn't start), for Copy details. Never shown.
let LAST_DETAIL = null;

/** Where the services listen, or why they aren't there. Only the app says where:
 *  never guess from a serve.json, which can belong to another copy or another home. */
export async function services() {
  const d = shared("captain.daemon");
  if (d?.base && d?.token) return { base: d.base, token: d.token };
  if (d?.state === "missing" || d?.state === "failed") return { error: d.error };
  return { waiting: true };
}

function notUp(reason) {
  // Quiet while they come up; one plain sentence once they've had their chance.
  if (Date.now() - BORN < PATIENCE_MS) return { ok: false, starting: true, error: null };
  LAST_DETAIL = reason;
  return { ok: false, failed: true, error: "Captain's services didn't start." };
}

/** The cst workspace the shown space is bound to (shared "captain.spaces"), or null
 *  for the services' current one. Every request carries it, so switching spaces
 *  switches what the screens read. */
export function workspace() {
  const bound = (shared("captain.spaces") ?? {})[focused()?.space];
  return bound?.workspace ?? null;
}

/** Call fn(cx) whenever the shown space's workspace changes (reload, reset the view). */
export function watchWorkspace(cx, fn) {
  // Only a real switch counts: a reading with no focused space (null) is ignored, and a new workspace must read the
  // same twice in a row. Without this, a window that isn't focused reads null now and then, and every screen reset
  // itself each second and never finished loading (seen in a real window, 2026-11-06).
  let last = workspace();
  let seen = null;
  cx.timer.every(1000, (cx) => {
    const now = workspace();
    if (now === null || now === last) { seen = null; return; }
    if (seen !== now) { seen = now; return; }
    seen = null;
    const first = last === null;
    last = now;
    if (!first) fn(cx);
  });
}

/** Call Captain's services. Resolves {ok, status, data, error, starting, failed}; never rejects.
 *  starting: still coming up (show a skeleton). failed: give up and show ServiceError. */
export async function api(path, opts) {
  return call(path, opts);
}



async function call(path, { method = "GET", body, timeout = 20 } = {}) {
  const d = await services();
  // Why they didn't start (a tool's output, a path, a port) goes behind Copy details, never on screen.
  if (!d.base && d.error) LAST_DETAIL = String(d.error);
  if (!d.base) return d.error ? { ok: false, failed: true, error: "Captain's services didn't start." } : notUp("they didn't respond.");
  let r, text;
  try {
    const headers = { Authorization: `Bearer ${d.token}` };
    const ws = workspace();
    if (ws) headers["X-CST-Workspace"] = ws;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    r = await fetch(d.base + path, { method: body !== undefined && method === "GET" ? "POST" : method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    text = await r.text();
  } catch (e) {
    // Any transport failure ("error sending request for url ...", refused, reset, a body cut off): the services
    // went away or are restarting. Never show the transport's own words: quiet while they come up, then one plain
    // sentence with Retry. The raw text goes behind Copy details only.
    LAST_DETAIL = `${method} ${path}: ${String(e?.message ?? e)}`;
    // fetch gives up after 30 s by itself (gpui-kit fetch.rs, TIMEOUT), so no request holds its screen forever.
    if (/timed? ?out/i.test(String(e?.message ?? e))) return { ok: false, timedOut: true, error: "This took too long." };
    if (Date.now() - BORN < PATIENCE_MS) return { ok: false, starting: true, error: null };
    return { ok: false, failed: true, stopped: true, error: "Captain's services stopped." };
  }
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  const ok = r.status >= 200 && r.status < 300;
  return { ok, status: r.status, data, error: ok ? null : (data && (data.error || data.detail)) || `HTTP ${r.status}` };
}


/** Start the services over (a screen's Retry). */
export function retry() {
  setShared("captain.daemon.retry", Date.now());
}

/** What Copy details puts on the clipboard. No token. */
export function details(error) {
  const d = shared("captain.daemon") ?? {};
  return JSON.stringify({ error, state: d.state ?? "unknown", reason: d.error ?? null, transport: LAST_DETAIL, home: d.home ?? null, at: new Date().toISOString() }, null, 2);
}

/** A query string from an object, skipping empty values. */
export function query(params) {
  const parts = Object.entries(params ?? {}).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}
