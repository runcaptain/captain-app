// CAPTAIN: every question the Weights page asks about a network goes through here, and nowhere else. A starter
// network ("starter:...") is answered by local.js, in memory. Anything else is asked of Captain's services at the
// routes in shapes.js, which are the ones the services serve (captain_search/weights/routes.py). If those change,
// this is the one file to change.
//
// Every function resolves to {ok: true, data} or {ok: false, error}: the page never sees a thrown error.
import { api } from "./kit/cst.js";
import { watchLive } from "./kit/live.js";
import { costOver } from "./net.js";
import { localModels, localNetwork, localSession, localSessionById, localEdit, localUndo } from "./local.js";

const enc = encodeURIComponent;
const isStarter = (model) => String(model ?? "").startsWith("starter:");
const isLocalSession = (id) => String(id ?? "").startsWith("local-");
// A copy, as an answer from the services would be: what the page holds never changes behind its back.
const ok = (data) => (data == null ? { ok: false, error: "That network is not here." } : { ok: true, data: JSON.parse(JSON.stringify(data)) });

let ask = (path, opts) => api(path, opts);
/** For tests: the request function. */
export function weightsTransport(fn) { ask = fn ?? ((path, opts) => api(path, opts)); }

/** The networks that can be opened: {networks, fromServices}. Captain's services make their own "tiny" and
 *  "digits" the first time weights are opened in a workspace, so when they answer, the list is theirs. When they
 *  do not, the one tiny network kept in this mod stands in, so the page always has something to open. */
export async function listNetworks() {
  let r;
  try { r = await ask("/api/weights/models", { timeout: 10 }); } catch { r = { ok: false }; }
  const theirs = r?.ok && Array.isArray(r.data) ? r.data.filter((m) => m && typeof m.id === "string") : [];
  return { ok: true, data: { networks: theirs.length ? theirs : localModels(), fromServices: theirs.length > 0 } };
}

/** A network, whole, as a session has edited it. The services send the numbers only through a session, and as
 *  a screen's look (`watch`): it is counted as the screen's, never against what the agent was allowed. */
export async function readNetwork(model, sessionId) {
  if (isStarter(model)) return ok(localNetwork(model));
  return ask(`/api/weights/models/${enc(model)}${sessionId ? `?session=${enc(sessionId)}&watch=1` : ""}`, { timeout: 30 });
}

/** The cost across the test set, as the numbers are now: {cost, right, of}, or null when it is not known. Only
 *  a network kept in this mod carries its test set. The services keep theirs back and say the score in the record
 *  of each edit. The page never asks them to score: a score shown is feedback from running the network, and
 *  asking would end a session's "from the weights alone". */
export async function readTestCost(model) {
  if (!isStarter(model)) return { ok: true, data: null };
  const net = localNetwork(model);
  return net ? { ok: true, data: costOver(net, net.test) } : ok(null);
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
  // `aim` is the input on screen, {index, x, y}: what the edit is meant to fix. The stand-in takes its number.
  // The services take the input itself, and measure the edit on it.
  const { aim, ...rest } = req;
  if (isLocalSession(sessionId)) return ok(localEdit(sessionId, { ...rest, target: aim?.index }));
  req = aim ? { ...rest, target: { inputs: [aim.x], targets: [aim.y] } } : rest;
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
