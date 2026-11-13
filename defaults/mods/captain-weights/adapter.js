// CAPTAIN: every question the Weights page asks about a network goes through here, and nowhere else. A starter
// network ("starter:...") is answered by local.js, in memory. Anything else is asked of Captain's services at the
// routes in shapes.js, which are the ones the services serve (captain_search/weights/routes.py). If those change,
// this is the one file to change.
//
// Every function resolves to {ok: true, data} or {ok: false, error}: the page never sees a thrown error.
import { api as servicesApi } from "./kit/cst.js";
import { watchLive } from "./kit/live.js";
import { costOver } from "./net.js";
import { localModels, localNetwork, localSession, localSessionById, localEdit, localUndo } from "./local.js";

const enc = encodeURIComponent;
const isStarter = (model) => String(model ?? "").startsWith("starter:");
const isLocalSession = (id) => String(id ?? "").startsWith("local-");
// A copy, as an answer from the services would be: what the page holds never changes behind its back.
const ok = (data) => (data == null ? { ok: false, error: "That network is not here." } : { ok: true, data: JSON.parse(JSON.stringify(data)) });

// Every address below is written out where it is asked for, so the services' address test reads each one.
let api = servicesApi;
/** For tests: the request function. */
export function weightsTransport(fn) { api = fn ?? servicesApi; }

/** For the picture check only, which has no services that hold a model of parts: answers by address, {path:
 *  answer}. Any address not in it is asked of the services as usual. */
export function weightsCanned(answers) { api = async (path, opts) => answers[path] ?? servicesApi(path, opts); }

/** The networks that can be opened: {networks, fromServices}. Captain's services make their own "tiny" and
 *  "digits" the first time weights are opened in a workspace, so when they answer, the list is theirs. When they
 *  do not, the one tiny network kept in this mod stands in, so the page always has something to open. */
export async function listNetworks() {
  let r;
  try { r = await api("/api/weights/models", { timeout: 10 }); } catch { r = { ok: false }; }
  const theirs = r?.ok && Array.isArray(r.data) ? r.data.filter((m) => m && typeof m.id === "string") : [];
  return { ok: true, data: { networks: theirs.length ? theirs : localModels(), fromServices: theirs.length > 0 } };
}

/** A network, whole, as a session has edited it. The services send the numbers only through a session, and as
 *  a screen's look (`watch`): it is counted as the screen's, never against what the agent was allowed. */
export async function readNetwork(model, sessionId) {
  if (isStarter(model)) return ok(localNetwork(model));
  return api(`/api/weights/models/${enc(model)}${sessionId ? `?session=${enc(sessionId)}&watch=1` : ""}`, { timeout: 30 });
}

// ---- A model of parts: a trained model too big to draw neuron by neuron ------------------------------------------
// The services call this kind "session-ranker": a small transformer with tree rankers (docs/weights.md there, "A
// trained session model with rankers"). It is read, never edited or run. Its description holds no numbers. Its
// numbers are read through a session as a screen's look (`watch=1`), which is counted as the screen's and never
// against what an agent was allowed. Everything below turns the services' shapes into the page's (shapes.js
// PartsModel and the rest), so this is the one place that changes when theirs do.

const cap = (text) => { const t = String(text ?? ""); return t ? t.charAt(0).toUpperCase() + t.slice(1) : t; };
const query = (o) => Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}=${enc(v)}`).join("&");

/** Whether a description, or a row of the list, is a model of parts. */
export const isParts = (d) => Boolean(d) && (d.kind === "session-ranker" || d.kind === "parts" || (Array.isArray(d.parts) && d.parts.length > 0));

/** A tensor's name in plain words. The services name tensors as the training code does ("L0.linear1.weight"). */
function tensorWords(kind, name) {
  const last = String(name).split(".").slice(-2).join(".");
  if (kind === "embedding" || kind === "items") return "The table";
  if (kind === "norm") return /bias$/.test(last) ? "Shift" : "Scale";
  if (/linear1\.weight$/.test(last)) return "Into the hidden step";
  if (/linear2\.weight$/.test(last)) return "Out of the hidden step";
  if (/linear1\.bias$/.test(last)) return "Hidden step biases";
  if (/linear2\.bias$/.test(last)) return "Output biases";
  return /bias$/.test(last) ? "Biases" : /weight$/.test(last) ? "Weights" : String(name);
}
const two = (shape) => (!Array.isArray(shape) || !shape.length ? [1, 1] : shape.length === 1 ? [1, shape[0]] : [shape[0], shape[1]]);

/** The services' description of a trained model, as the page's PartsModel. */
export function partsFrom(d) {
  const parts = (d.parts ?? []).map((p) => {
    const tensors = [];
    if (p.kind === "attention") {
      const w = p.width ?? p.shape?.[1] ?? 0;
      // What a head looks for and what it copies are composed by the services, and need the number of heads.
      if (p.heads && p.layer != null) tensors.push(
        { id: "qk", name: "Query-key", shape: [w, w], per_head: true, how: { attention: p.layer, matrix: "query_key" } },
        { id: "ov", name: "Output-value", shape: [w, w], per_head: true, how: { attention: p.layer, matrix: "output_value" } });
      for (const m of p.matrices ?? []) tensors.push({ id: m.role, name: cap(m.role), technical: m.tensor, shape: two(m.shape), how: { tensor: m.tensor, rows: m.rows } });
    } else for (const t of p.tensors ?? []) tensors.push({ id: t, name: tensorWords(p.kind, t), technical: t, how: { tensor: t } });
    const shape = p.kind === "attention" && p.width ? [p.width, p.width] : two(p.shape);
    if (tensors.length && !tensors[0].shape) tensors[0].shape = shape;
    return { id: p.id, kind: p.kind, name: cap(p.name ?? p.id), says: p.says ?? "", technical: p.technical ?? p.id, shape, parameters: p.parameters ?? 0, feeds_from: p.feeds_from ?? [],
      block: p.layer != null ? p.layer + 1 : undefined, heads: p.heads ?? null, side: p.side ?? null, embeds: p.embeds ?? null, guessed: p.meaning_from === "name",
      hidden: p.hidden ?? null, inputs: p.inputs ?? null, outputs: p.outputs ?? null,
      // False when one of its tensors is not stored as plain decimals: the services refuse an edit to it.
      editable: p.editable !== false, tensors };
  });
  // Where it came from is a folder or a storage address. The page never shows it raw: it says which of the two
  // it is, and keeps the address for a person to copy.
  const address = typeof d.source === "string" ? "" : String(d.source?.from ?? "");
  const from = typeof d.source === "string" ? d.source : !address ? "" : /^[a-z][a-z0-9+.-]*:\/\//i.test(address) ? "Brought in from your model storage" : "Brought in from a folder on this computer";
  return { id: d.id, name: d.name ?? d.id, kind: "parts", kind_words: cap(d.kind_says ?? d.kind_words ?? "trained model"), parameters: d.parameters, width: d.width ?? null,
    source: from, source_address: address, read_only: d.can?.says ?? null, can_edit: d.can?.edit === true, edits_say: d.can?.edits_say ?? "", wiring: d.wiring_says ?? "", heads_words: d.heads ? "" : d.heads_says ?? "",
    parts, rankers: (d.rankers ?? []).map((r) => ({ id: r.name, name: r.name, trees: r.trees ?? null, inputs_count: r.inputs ?? null })) };
}

/** What a model is, without its numbers: {ok, data} where data is the services' own answer (for a model of parts,
 *  see partsFrom). A starter has none. */
export async function readDescription(model) {
  if (isStarter(model)) return { ok: true, data: null };
  return api(`/api/weights/models/${enc(model)}`, { timeout: 20 });
}

/** One matrix of a part as a grid of numbers (shapes.js Heatmap). `t` is one of the part's tensors (partsFrom).
 *  For a head's query-key or output-value matrix give `head`. `rows` is [first, end) within the matrix, for a
 *  table too tall to send whole. */
export async function readPartHeatmap(sessionId, t, { head = 0, rows = null } = {}) {
  const base = `/api/weights/sessions/${enc(sessionId)}`;
  if (t.how.attention != null) {
    const r = await api(`${base}/attention?${query({ layer: t.how.attention, head, matrix: t.how.matrix, watch: 1 })}`, { timeout: 30 });
    if (!r?.ok) return r ?? { ok: false };
    const of = Array.isArray(r.data?.of) ? r.data.of : [], one = of.find((h) => h.head === head && h.matrix) ?? of.find((h) => h.matrix);
    if (!one?.matrix?.grid) return { ok: false, error: "The services did not send that head's matrix." };
    const g = one.matrix.grid;
    return { ok: true, data: { tensor: t.id, shape: [g.length, g[0]?.length ?? 0], from: 0, values: g } };
  }
  const off = t.how.rows?.[0] ?? 0, span = rows ? [off + rows[0], off + rows[1]] : t.how.rows ?? null;
  const r = await api(`${base}/heatmap?${query({ tensor: t.how.tensor, rows: span ? `${span[0]}:${span[1]}` : null, watch: 1 })}`, { timeout: 30 });
  if (!r?.ok) return r ?? { ok: false };
  const g = Array.isArray(r.data?.grid) ? r.data.grid : [];
  const full = t.how.rows ? [t.how.rows[1] - t.how.rows[0], g[0]?.length ?? 0] : two(r.data?.shape);
  return { ok: true, data: { tensor: t.id, shape: full, from: rows ? rows[0] : 0, values: g } };
}

/** The rows of a table most like one row, by cosine (shapes.js NearRows). */
export async function readPartNearest(sessionId, t, row) {
  const r = await api(`/api/weights/sessions/${enc(sessionId)}/nearest?${query({ tensor: t.how.tensor, row, top: 8, watch: 1 })}`, { timeout: 30 });
  if (!r?.ok) return r ?? { ok: false };
  return { ok: true, data: { row: r.data?.row ?? row, name: r.data?.label ?? null, nearest: (r.data?.nearest ?? []).map((x) => ({ row: x.row, name: x.label ?? null, similarity: x.cosine })) } };
}

/** Change numbers of one matrix of a trained model: one staged change (parthand.js) as one edit. A cell is set
 *  by its place, a row is halved, doubled or zeroed whole. Rows are counted within the matrix, so a matrix that
 *  is a range of a stacked tensor has its first row added. Resolves to the event that records it. The services
 *  do not measure it: the event says so in its sentence. */
export async function sendPartEdit(sessionId, t, staged) {
  const row = (t.how.rows?.[0] ?? 0) + staged.row;
  const what = staged.kind === "cell" ? { op: "set", indices: [[row, staged.col]], value: staged.value }
    : staged.op === "zero" ? { op: "zero", neurons: [row] } : { op: "scale", neurons: [row], factor: staged.factor };
  return api(`/api/weights/sessions/${enc(sessionId)}/edits`, { method: "POST", body: { ...what, tensor: t.how.tensor, by: "you" }, timeout: 30 });
}

/** What each row of a table stands for, when the services know: {labels: [one per row] or null, says, sure}.
 *  `says` is the services' sentence about where the names came from, and `sure` false means a row's place is a
 *  guess. A route that is not there yet answers as no names. */
export async function readPartLabels(sessionId, t) {
  const r = await api(`/api/weights/sessions/${enc(sessionId)}/labels?${query({ tensor: t.how.tensor })}`, { timeout: 20 });
  const d = r?.ok ? r.data ?? {} : {};
  return { ok: true, data: { labels: Array.isArray(d.labels) ? d.labels : null, says: d.labels_says ?? "", sure: d.sure ?? null } };
}

/** A model's rankers side by side: {says, by: {input name: {used_by: [rankers], unused_by: [rankers]}}}. A
 *  route that is not there yet answers as nothing known. */
export async function readRankersCompared(model) {
  const r = await api(`/api/weights/models/${enc(model)}/rankers`, { timeout: 20 });
  const d = r?.ok ? r.data ?? {} : {};
  return { ok: true, data: { says: d.unused_by_all_says ?? "", by: Object.fromEntries((d.inputs ?? []).map((i) => [i.name, { used_by: i.used_by ?? [], unused_by: i.unused_by ?? [] }])) } };
}

/** A tree as the services send it (a root with `left` and `right` inside it) as a flat list of nodes. A value at
 *  or below the threshold goes left, so "above the threshold" is yes to the right. */
function flatTree(root) {
  const nodes = [];
  const walk = (n) => {
    const id = nodes.length;
    if (!n || n.left == null || n.right == null) { nodes.push({ id, leaf: n?.value ?? 0 }); return id; }
    const me = { id, input: n.name ?? `input ${(n.input ?? 0) + 1}`, threshold: n.threshold, categorical: Boolean(n.categorical) };
    nodes.push(me);
    me.no = walk(n.left); me.yes = walk(n.right);
    return id;
  };
  walk(root);
  return nodes;
}

/** One tree ranker of a model (shapes.js Ranker): its inputs by share of gain, the inputs no tree asks about,
 *  and its first trees. */
export async function readRanker(model, ranker) {
  const r = await api(`/api/weights/models/${enc(model)}/trees?${query({ ranker, trees: 3 })}`, { timeout: 30 });
  if (!r?.ok) return r ?? { ok: false };
  const d = r.data ?? {};
  return { ok: true, data: { id: d.ranker ?? ranker, name: d.ranker ?? ranker, trees: d.trees ?? null,
    inputs: (d.by_input ?? []).map((i) => ({ name: i.name, splits: i.splits ?? 0, gain_share: i.share ?? 0 })),
    unused: (d.unused ?? []).map((u) => u.name), names_from: d.names_from ?? null,
    shown: (d.first_trees ?? []).map((t) => ({ tree: t.tree, nodes: flatTree(t.root) })) } };
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
  return api(`/api/weights/models/${enc(model)}/session`, { timeout: 15 });
}

export async function readSessionById(id) {
  if (isLocalSession(id)) return ok(localSessionById(id));
  return api(`/api/weights/sessions/${enc(id)}`, { timeout: 15 });
}

/** Change numbers: one EditRequest (shapes.js). Resolves to the event that records it. */
export async function sendEdit(sessionId, req) {
  // `aim` is the input on screen, {index, x, y}: what the edit is meant to fix. The stand-in takes its number.
  // The services take the input itself, and measure the edit on it.
  const { aim, ...rest } = req;
  if (isLocalSession(sessionId)) return ok(localEdit(sessionId, { ...rest, target: aim?.index }));
  req = aim ? { ...rest, target: { inputs: [aim.x], targets: [aim.y] } } : rest;
  return api(`/api/weights/sessions/${enc(sessionId)}/edits`, { method: "POST", body: req, timeout: 30 });
}

/** Take an edit back. */
export async function sendUndo(sessionId, editId, by = "you") {
  if (isLocalSession(sessionId)) return ok(localUndo(sessionId, editId, by));
  return api(`/api/weights/sessions/${enc(sessionId)}/undo`, { method: "POST", body: { edit: editId, by }, timeout: 30 });
}

/** What can be brought in from a folder or a storage address: {versions: [{path, address, name, description}],
 *  count}. The services only list and read there. */
export async function readRegistry(from) {
  return api(`/api/weights/registry?from=${enc(from)}`, { timeout: 60 });
}

/** Bring a trained model in: the services copy its files into this workspace under `name`. `address` is a
 *  registry row's own. Resolves to {model, version, ref, parameters, says}. */
export async function sendImport(address, name) {
  return api("/api/weights/import", { method: "POST", body: { folder: address, name }, timeout: 300 });
}

/** Save a session's changes as a new version of its network: {model, version, ref, changed}. This is the only
 *  call that makes a version, besides a deploy. */
export async function sendSave(sessionId, note = "") {
  if (isLocalSession(sessionId)) return { ok: false, error: "This network lives only in the page, so it has no versions." };
  return api(`/api/weights/sessions/${enc(sessionId)}/save`, { method: "POST", body: { note, by: "you" }, timeout: 60 });
}

/** Start an agent on a session with a goal. Captain's services start it, hold it to its limits and give the
 *  verdict. Body: {goal, max_edits, target_input, target_output}. Resolves to {job, session, agent, goal}. A
 *  network kept in this mod has no services behind it, so there is nothing to start. */
export async function sendProgram(sessionId, body) {
  if (isLocalSession(sessionId)) return { ok: false, error: "This network lives only in the page, so no agent can be started on it." };
  return api(`/api/weights/sessions/${enc(sessionId)}/program`, { method: "POST", body, timeout: 60 });
}

/** Stop the agent working in a session. Captain's verdict on what it did so far follows. */
export async function sendProgramStop(sessionId) {
  return api(`/api/weights/sessions/${enc(sessionId)}/program/stop`, { method: "POST", body: {}, timeout: 30 });
}

/** A job the services run, as it stands: {status, message, result}. */
export async function readJob(id) {
  return api(`/api/jobs/${enc(id)}`, { scoped: false, timeout: 20 });
}

/** Hear when the model's numbers or the session's record change, whoever changed them. Returns the stop. */
export function watchWeights(cx, model, sessionId, onChange) {
  return watchLive(cx, [`weights:${model}`, sessionId ? `weights-session:${sessionId}` : null], onChange);
}

// ---- Deploying the edited network -------------------------------------------------------------------------------

/** The project a network is in, or null. A deployment belongs to a project. */
export async function projectOf(model) {
  if (isStarter(model)) return { ok: true, data: null };
  const r = await api("/api/weights/models", { timeout: 15 });
  if (!r?.ok || !Array.isArray(r.data)) return { ok: false, error: r?.error };
  return { ok: true, data: r.data.find((m) => m?.id === model)?.project ?? null };
}

/** The workspace's projects, for choosing where a deployment goes: [{id, name}]. */
export async function listProjects() {
  const r = await api("/api/projects", { scoped: false, timeout: 15 });
  if (!r?.ok) return { ok: false, error: r?.error };
  const rows = Array.isArray(r.data) ? r.data : r.data?.projects ?? [];
  return { ok: true, data: rows.filter((x) => x?.id).map((x) => ({ id: x.id, name: x.name ?? x.label ?? x.id })) };
}

/** Save the session as a new version and deploy that version to `host` ("local" is this computer), as one job.
 *  Resolves to {job, run, plan, saved: {ref, text}, session}. A refusal is a sentence. */
export async function sendDeploy(project, sessionId, host = "local") {
  return api(`/api/projects/${enc(project)}/deploy`, { method: "POST", body: { session: sessionId, host, by: "you" }, timeout: 60 });
}

/** Where the project's last deploy stands: {status: none | running | done | failed, error, env, staged, text}. */
export async function readDeploy(project) {
  return api(`/api/projects/${enc(project)}/deploy`, { timeout: 20 });
}

/** Hear when what is deployed from a project changes. Returns the stop. */
export function watchDeploy(cx, project, onChange) {
  return watchLive(cx, ["deployed", `deployment:${project}`], onChange);
}
