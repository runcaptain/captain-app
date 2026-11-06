// captain-ui: Captain's local services (the cst backend), from a mod.
// The app starts them at launch and keeps shared "captain.daemon" current:
// {state: "ready", base, token, home}, or {state: "starting" | "missing" | "failed", error}.
// Requests go through fetch, in-process, so the token never shows up in `ps`.
// Where they listen comes only from the app (shared "captain.daemon").
import { shared, setShared, focused } from "herdr";

// When this copy of the mod started: services get ~15 s to come up before a screen says so.
const BORN = Date.now();
const PATIENCE_MS = 15000;

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
  return { ok: false, failed: true, error: `Captain couldn't start its services: ${reason}` };
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
  let last = workspace();
  cx.timer.every(1000, (cx) => {
    const now = workspace();
    if (now !== last) {
      last = now;
      fn(cx);
    }
  });
}

/** Call Captain's services. Resolves {ok, status, data, error, starting, failed}; never rejects.
 *  starting: still coming up (show a skeleton). failed: give up and show ServiceError. */
export async function api(path, { method = "GET", body, timeout = 20 } = {}) {
  const d = await services();
  if (!d.base) return d.error ? { ok: false, failed: true, error: `Captain couldn't start its services: ${d.error}` } : notUp("they didn't respond.");
  let r;
  try {
    const headers = { Authorization: `Bearer ${d.token}` };
    const ws = workspace();
    if (ws) headers["X-CST-Workspace"] = ws;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    r = await fetch(d.base + path, { method: body !== undefined && method === "GET" ? "POST" : method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch (e) {
    const why = String(e?.message ?? e);
    // Refused: still binding the port, or gone.
    if (/refused|connect|reset|unreachable/i.test(why)) return notUp("they didn't respond.");
    return { ok: false, error: why };
  }
  const text = await r.text();
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
  return JSON.stringify({ error, state: d.state ?? "unknown", reason: d.error ?? null, home: d.home ?? null, at: new Date().toISOString() }, null, 2);
}

/** A query string from an object, skipping empty values. */
export function query(params) {
  const parts = Object.entries(params ?? {}).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}
