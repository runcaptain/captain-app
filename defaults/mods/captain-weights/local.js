// CAPTAIN: a stand-in for Captain's services over the two starter networks, with the shapes of shapes.js. It keeps
// each starter's numbers, its session record and its edits in memory, for as long as the page is open. adapter.js
// is the only file that calls it.
import { starter, STARTERS } from "./starters.js";
import { forward, costOf, costOver, answerOf, countOf } from "./net.js";

const OPEN = new Map(); // model id -> {net, base, session}

function entry(model) {
  if (OPEN.has(model)) return OPEN.get(model);
  const net = starter(model);
  if (!net) return null;
  net.version = 1;
  const total = countOf(net).total;
  const e = {
    net,
    base: net.layers.map(({ W, b }) => ({ W: W.map((r) => r.slice()), b: b.slice() })),
    session: {
      id: `local-${model.replace(/[^a-z0-9]+/gi, "-")}`, model,
      // A starter is open to everything: nothing here was done from the weights alone.
      ledger: { raw_weights: true, weights_read: 0, sandbox: true, forward_allowed: 50, forward_used: 0, gradients_computed: false, changed: 0, total },
      events: [],
    },
  };
  OPEN.set(model, e);
  return e;
}

/** Forget everything (a test starting over). */
export function localReset() { OPEN.clear(); }

export function localModels() { return STARTERS.map((s) => ({ ...s, sizes: starter(s.id).sizes })); }
export function localNetwork(model) { return entry(model)?.net ?? null; }
export function localSession(model) { return entry(model)?.session ?? null; }
export function localSessionById(id) { for (const e of OPEN.values()) if (e.session.id === id) return e.session; return null; }

function changedCount(e) {
  let n = 0;
  e.net.layers.forEach(({ W, b }, k) => {
    for (let j = 0; j < W.length; j++) {
      if (b[j] !== e.base[k].b[j]) n++;
      for (let i = 0; i < W[j].length; i++) if (W[j][i] !== e.base[k].W[j][i]) n++;
    }
  });
  return n;
}

function record(e, ev) {
  const s = e.session;
  const full = { id: `e${s.events.length + 1}`, seq: s.events.length + 1, at: new Date().toISOString(), touched: { neurons: [], weights: [] }, ...ev };
  s.events.push(full);
  s.ledger.changed = changedCount(e);
  return full;
}

const measure = (net, target, others) => ({
  target: costOf(forward(net, target.x).a.at(-1), target.y),
  keep: costOver(net, others),
  answers: others.map((o) => answerOf(forward(net, o.x).a.at(-1))),
  test: costOver(net, net.test),
});

function put(e, changes, which) {
  for (const c of changes) {
    if (c.i == null) e.net.layers[c.k].b[c.j] = c[which];
    else e.net.layers[c.k].W[c.j][c.i] = c[which];
  }
  e.net.version++;
}

/** Apply one edit (shapes.js EditRequest) and record it with its three numbers, before and after. */
export function localEdit(sessionId, req) {
  const e = [...OPEN.values()].find((x) => x.session.id === sessionId);
  if (!e) return null;
  const { net } = e, k = req.layer - 1, j = req.neuron;
  const layer = net.layers[k];
  if (!layer || !(j >= 0 && j < layer.b.length)) return null;
  const changes = [];
  const want = (i, value) => { const old = i == null ? layer.b[j] : layer.W[j][i]; if (old !== value && Number.isFinite(value)) changes.push({ k, j, i, old, new: value }); };
  if (req.op === "set") want(req.from ?? null, Number(req.value));
  else if (req.op === "scale") layer.W[j].forEach((w, i) => want(i, w * Number(req.factor)));
  else if (req.op === "zero") { layer.W[j].forEach((_w, i) => want(i, 0)); want(null, 0); }
  else return null;
  const target = net.examples[req.target ?? 0] ?? net.examples[0];
  const others = net.examples.filter((x) => x !== target);
  const before = measure(net, target, others);
  put(e, changes, "new");
  const after = measure(net, target, others);
  return record(e, {
    by: req.by ?? "you", kind: "edit", op: req.op, layer: req.layer, neuron: j, factor: req.op === "scale" ? Number(req.factor) : undefined, why: req.why,
    changes,
    touched: { neurons: [[req.layer, j]], weights: changes.filter((c) => c.i != null).map((c) => [c.k, c.j, c.i]) },
    cost: {
      target: { name: target.name, before: before.target, after: after.target },
      keep: { before: before.keep.cost, after: after.keep.cost, of: others.length, answers_changed: after.answers.filter((a, n) => a !== before.answers[n]).length },
      test: { before: before.test.cost, after: after.test.cost, of: net.test.length, right_before: before.test.right, right_after: after.test.right },
    },
  });
}

/** Take an edit back. It stays in the record, marked as taken back, and the undo is an event of its own. */
export function localUndo(sessionId, editId, by = "you") {
  const e = [...OPEN.values()].find((x) => x.session.id === sessionId);
  const ev = e?.session.events.find((x) => x.id === editId && x.kind === "edit" && !x.undone);
  if (!ev) return null;
  const test0 = costOver(e.net, e.net.test);
  // Only numbers still at the edit's value go back: a later edit to the same number is left as it is.
  const back = ev.changes.filter((c) => (c.i == null ? e.net.layers[c.k].b[c.j] : e.net.layers[c.k].W[c.j][c.i]) === c.new);
  put(e, back, "old");
  ev.undone = true;
  const test1 = costOver(e.net, e.net.test);
  return record(e, { by, kind: "undo", undoes: ev.id, layer: ev.layer, neuron: ev.neuron, touched: ev.touched, changes: back.map((c) => ({ ...c, old: c.new, new: c.old })),
    cost: { test: { before: test0.cost, after: test1.cost, of: e.net.test.length, right_before: test0.right, right_after: test1.right } } });
}

/** Record that numbers were read or the network was run, as an agent's tool would. Used by the tests and the
 *  sample: the real record is written by Captain's services. */
export function localNote(sessionId, ev) {
  const e = [...OPEN.values()].find((x) => x.session.id === sessionId);
  if (!e) return null;
  const l = e.session.ledger;
  if (ev.kind === "read") l.weights_read += ev.touched?.weights?.length ?? 0;
  if (ev.kind === "run") l.forward_used += 1;
  return record(e, { by: "agent", ...ev });
}
