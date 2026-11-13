// CAPTAIN: every question the Weights page asks about a network goes through here, and nowhere else. A starter
// network ("starter:...") is answered by local.js, in memory. Anything else is asked of Captain's services at the
// routes in shapes.js. When the services' real routes are known, this is the one file to change.
//
// Every function resolves to {ok: true, data} or {ok: false, error}: the page never sees a thrown error.
import { api } from "./kit/cst.js";
import { watchLive } from "./kit/live.js";
import { localModels, localNetwork, localSession, localSessionById, localEdit, localUndo } from "./local.js";

const enc = encodeURIComponent;
const isStarter = (model) => String(model ?? "").startsWith("starter:");
const isLocalSession = (id) => String(id ?? "").startsWith("local-");
const ok = (data) => (data == null ? { ok: false, error: "That network is not here." } : { ok: true, data });

let ask = (path, opts) => api(path, opts);
/** For tests: the request function. */
export function weightsTransport(fn) { ask = fn ?? ((path, opts) => api(path, opts)); }

/** The networks that can be opened. Captain's services make their own "tiny" and "digits" the first time
 *  weights are opened in a workspace, so when they answer, the list is theirs. When they do not, the two starters
 *  kept in this mod stand in, so the page always has something to open. */
export async function listNetworks() {
  let r;
  try { r = await ask("/api/weights/models", { timeout: 10 }); } catch { r = { ok: false }; }
  const theirs = r?.ok && Array.isArray(r.data) ? r.data.filter((m) => m && typeof m.id === "string") : [];
  return { ok: true, data: theirs.length ? theirs : localModels() };
}

/** A network, whole. */
export async function readNetwork(model) {
  if (isStarter(model)) return ok(localNetwork(model));
  return ask(`/api/weights/models/${enc(model)}`, { timeout: 30 });
}

/** The session open on a model: what the agent was allowed, and everything done so far. */
export async function readSession(model) {
  if (isStarter(model)) return ok(localSession(model));
  return ask(`/api/weights/models/${enc(model)}/session`, { timeout: 15 });
}

export async function readSessionById(id) {
  if (isLocalSession(id)) return ok(localSessionById(id));
  return ask(`/api/weights/sessions/${enc(id)}`, { timeout: 15 });
}

/** Change numbers: one EditRequest (shapes.js). Resolves to the event that records it. */
export async function sendEdit(sessionId, req) {
  if (isLocalSession(sessionId)) return ok(localEdit(sessionId, req));
  return ask(`/api/weights/sessions/${enc(sessionId)}/edits`, { method: "POST", body: req, timeout: 30 });
}

/** Take an edit back. */
export async function sendUndo(sessionId, editId, by = "you") {
  if (isLocalSession(sessionId)) return ok(localUndo(sessionId, editId, by));
  return ask(`/api/weights/sessions/${enc(sessionId)}/undo`, { method: "POST", body: { edit: editId, by }, timeout: 30 });
}

/** Hear when the model's numbers or the session's record change, whoever changed them. Returns the stop. */
export function watchWeights(cx, model, sessionId, onChange) {
  return watchLive(cx, [`weights:${model}`, sessionId ? `weights-session:${sessionId}` : null], onChange);
}
