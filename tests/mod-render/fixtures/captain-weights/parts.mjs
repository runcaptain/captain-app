// A made-up trained model for the Weights page's tests and pictures: a small transformer "session model" (width 8,
// two layers, two heads) with two tree rankers, answered in the shapes Captain's services use for the kind
// "session-ranker" (backend/core/captain_search/docs/weights.md, "A trained session model with rankers").
// Every name and every number here is invented by this file. Nothing comes from a real model.
export const PARTS = 'demo-session';
export const SESSION = 'ws-demo';

const W = 8, HIDDEN = 16, HEADS = 2;
// A small seeded generator, so the same numbers come out on every run.
function rand(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
const seedOf = (text) => [...text].reduce((h, ch) => (Math.imul(h, 31) + ch.charCodeAt(0)) >>> 0, 7);
/** A matrix of made-up numbers between -1 and 1, the same for the same name. */
export function matrix(name, rows, cols) {
  const r = rand(seedOf(name));
  return Array.from({ length: rows }, () => Array.from({ length: cols }, () => Number((r() * 2 - 1).toFixed(4))));
}

const ITEMS = ['Blue kettle', 'Red kettle', 'Tea towel', 'Garden trowel', 'Seed tray', 'Watering can', 'Picture book', 'Story book', 'Wooden train', 'Toy drum', 'Gift card', 'Wrapping paper'];

/** Every tensor of the made-up model: name -> shape, as the training code would name them. */
export function tensorShapes({ tall = false } = {}) {
  const sh = { 'item.weight': [tall ? 1000 : 12, W], 'act.weight': [4, W], 'pos.weight': [6, W], 'text.weight': [W, 16], 'text.bias': [W], 'ln.weight': [W], 'ln.bias': [W] };
  for (const k of [0, 1]) Object.assign(sh, {
    [`L${k}.self_attn.in_proj_weight`]: [3 * W, W], [`L${k}.self_attn.in_proj_bias`]: [3 * W], [`L${k}.self_attn.out_proj.weight`]: [W, W], [`L${k}.self_attn.out_proj.bias`]: [W],
    [`L${k}.norm1.weight`]: [W], [`L${k}.norm1.bias`]: [W], [`L${k}.linear1.weight`]: [HIDDEN, W], [`L${k}.linear1.bias`]: [HIDDEN],
    [`L${k}.linear2.weight`]: [W, HIDDEN], [`L${k}.linear2.bias`]: [W], [`L${k}.norm2.weight`]: [W], [`L${k}.norm2.bias`]: [W] });
  Object.assign(sh, { 'buy.weight': [1, W], 'buy.bias': [1], 'logit_scale': [], cn: [40, W] });
  return sh;
}
const numbers = (sh, names) => names.reduce((n, t) => n + (sh[t].length ? sh[t].reduce((a, b) => a * b, 1) : 1), 0);

/** The services' description of the model: its parts, no numbers (`GET /api/weights/models/demo-session`). */
export function partsModel(o = {}) {
  const sh = tensorShapes(o);
  const part = (id, kind, name, says, tensors, feeds_from, more = {}) => ({ id, kind, name, says, technical: id, tensors, shape: sh[tensors[0]], parameters: numbers(sh, tensors), feeds_from, ...more });
  const table = (stem, name, what, known = true) => part(stem, 'embedding', name, `embedding table: one row of ${W} numbers for each of ${sh[`${stem}.weight`][0]} values. ${what[0].toUpperCase()}${what.slice(1)}.`,
    [`${stem}.weight`], [], { side: 'session', rows: sh[`${stem}.weight`][0], width: W, embeds: known ? what : null, meaning_from: known ? 'name' : null });
  const parts = [
    table('item', 'item table', 'which item the event was about'),
    table('act', 'action table', 'what kind of event it was'),
    table('pos', 'position table', 'where in the session the event sits'),
    part('text', 'projection', 'text projection', `projection: turns the item's text vector of 16 numbers into ${W}, the model's width.`, ['text.weight', 'text.bias'], [], { side: 'session', inputs: 16, outputs: W }),
    part('ln', 'norm', 'norm ln', 'norm: rescales each vector so its numbers stay in a steady range.', ['ln.weight', 'ln.bias'], ['item', 'act', 'pos', 'text'], { side: 'session', width: W }),
  ];
  let last = ['ln'];
  for (const k of [0, 1]) {
    const a = `L${k}.self_attn`;
    const mats = ['query', 'key', 'value'].map((role, i) => ({ role, name: `${role}: made-up words`, tensor: `${a}.in_proj_weight`, rows: [i * W, (i + 1) * W], shape: [W, W] }));
    mats.push({ role: 'output', name: 'output: how what was copied is written back', tensor: `${a}.out_proj.weight`, rows: [0, W], shape: [W, W] });
    parts.push(part(a, 'attention', `attention, layer ${k + 1}`, 'attention: which earlier events this one looks at, and what it copies from them.',
      [`${a}.in_proj_weight`, `${a}.in_proj_bias`, `${a}.out_proj.weight`, `${a}.out_proj.bias`], last, { side: 'session', layer: k, width: W, heads: o.heads === null ? null : HEADS, head_width: W / HEADS, matrices: mats }));
    parts.push(part(`L${k}.norm1`, 'norm', `norm after attention, layer ${k + 1}`, 'norm: rescales each vector so its numbers stay in a steady range.', [`L${k}.norm1.weight`, `L${k}.norm1.bias`], [a], { side: 'session', layer: k, width: W }));
    parts.push(part(`L${k}.ff`, 'feed_forward', `feed-forward, layer ${k + 1}`, `feed-forward: reworks each event's vector on its own, from ${W} numbers up to ${HIDDEN} and back.`,
      [`L${k}.linear1.weight`, `L${k}.linear1.bias`, `L${k}.linear2.weight`, `L${k}.linear2.bias`], [`L${k}.norm1`], { side: 'session', layer: k, inputs: W, hidden: HIDDEN }));
    parts.push(part(`L${k}.norm2`, 'norm', `norm after feed-forward, layer ${k + 1}`, 'norm: rescales each vector so its numbers stay in a steady range.', [`L${k}.norm2.weight`, `L${k}.norm2.bias`], [`L${k}.ff`], { side: 'session', layer: k, width: W }));
    last = [`L${k}.norm2`];
  }
  parts.push(part('buy', 'head', 'head buy', `small head at the end: ${W} numbers in, 1 out. What it is for is not stated in the file.`, ['buy.weight', 'buy.bias'], last, { side: 'session', inputs: W, outputs: 1 }));
  // An older layout: the catalog's item vectors on the candidate side, stored in a form that cannot be edited.
  if (o.older) parts.push({ ...part('cn', 'items', 'item vectors', 'item vectors: one row per catalog item.', ['cn'], [], { side: 'candidate', rows: 40, width: W }), editable: false });
  parts.push(part('logit_scale', 'scale', 'score sharpness', 'one number: how sharply the scores of candidates are told apart.', ['logit_scale'], [], { side: null }));
  const parameters = parts.reduce((n, p) => n + p.parameters, 0);
  return { id: PARTS, name: PARTS, version: 1, says: `A session model, width ${W}, 2 layers, 2 heads, with 2 tree rankers.`, numbers: false, sizes: [], layers: [], examples: [], test: [],
    kind: 'session-ranker', kind_says: 'session model with rankers', width: W, depth: 2, heads: o.heads === null ? null : HEADS,
    heads_says: o.heads === null ? 'Unknown. The shapes do not tell how many heads there are, and no file beside them says.' : '2 heads, as model.json says. The shapes alone cannot tell.',
    parameters, parts, wiring_says: 'The order of the parts is the usual one for these names. The file holds numbers, not wiring, so it is not confirmed.',
    rankers: [{ name: 'full', objective: 'lambdarank', trees: 40, inputs: 4 }, { name: 'quick', objective: 'binary', trees: 25, inputs: 2 }],
    source: { from: '/models/demo/v1', read_at: '2026-11-13T10:00:00Z', files: {} },
    can: o.edits ? { read: true, edit: true, run: false, deploy: false, says: 'This model can be read and changed here, not run yet.', edits_say: NOT_MEASURED }
      : { read: true, edit: false, run: false, deploy: false, says: 'This model can be read here, not changed or run yet.' } };
}

export const SOURCE_SAYS = 'Brought in from a training folder on this computer on 2026-11-13, 12 files kept unchanged.';
export const NOT_MEASURED = 'Not measured: this model cannot be run here yet.';
// What a test has changed in the made-up model: the numbers set, and the session's record. `partsReset()` clears it.
const CHANGED = { numbers: new Map(), events: [], refuse: null };
export function partsReset() { CHANGED.numbers.clear(); CHANGED.events = []; CHANGED.refuse = null; }
/** Make the next edit be refused with this sentence (null: accept again). */
export function partsRefuse(sentence) { CHANGED.refuse = sentence; }
export const partsEvents = () => CHANGED.events;
/** A tensor's numbers as they are now: the made-up ones with every applied edit. */
function current(name, R, C) {
  const m = matrix(name, R, C);
  for (const [key, v] of CHANGED.numbers) { const [t, r, c] = key.split('|'); if (t === name && m[r]) m[r][c] = v; }
  return m;
}
/** The services' edit route for this kind: set by `indices`, or scale or zero whole rows by `neurons`. */
function editAnswer(sh, body) {
  if (CHANGED.refuse) return { ok: false, error: CHANGED.refuse };
  const shape = sh[body.tensor];
  if (!shape) return { ok: false, error: `There is no tensor named ${body.tensor}.` };
  const [R, C] = shape.length === 2 ? shape : [1, shape[0] ?? 1], now = current(body.tensor, R, C);
  const at = body.indices ?? (body.neurons ?? []).flatMap((r) => Array.from({ length: C }, (_x, c) => [r, c]));
  const changes = at.map(([r, c]) => ({ tensor: body.tensor, index: [r, c], old: now[r][c], new: body.op === 'set' ? body.value : body.op === 'zero' ? 0 : now[r][c] * body.factor })).filter((x) => x.old !== x.new);
  for (const x of changes) CHANGED.numbers.set(`${body.tensor}|${x.index[0]}|${x.index[1]}`, x.new);
  const n = CHANGED.events.length + 1, verb = body.op === 'set' ? 'set' : body.op === 'zero' ? 'set to zero' : 'scaled';
  const ev = { id: `e${n}`, seq: n, at: '2026-11-13T10:00:00Z', by: body.by ?? 'you', kind: 'edit', op: body.op, tensor: body.tensor, tensors: [body.tensor], changes, changed: changes.length, undone: false,
    measured: false, not_measured: NOT_MEASURED, touched: { neurons: [], weights: [] }, sentence: `You ${verb} ${changes.length} ${changes.length === 1 ? 'number' : 'numbers'} in ${body.tensor}. ${NOT_MEASURED}` };
  CHANGED.events.push(ev);
  return { ok: true, data: ev };
}
function undoAnswer(body) {
  const ev = CHANGED.events.find((e) => e.id === body.edit && e.kind === 'edit' && !e.undone);
  if (!ev) return { ok: false, error: 'There is no such edit to take back.' };
  for (const x of ev.changes) CHANGED.numbers.set(`${x.tensor}|${x.index[0]}|${x.index[1]}`, x.old);
  ev.undone = true;
  const n = CHANGED.events.length + 1, out = { id: `e${n}`, seq: n, by: 'you', kind: 'undo', undoes: ev.id, tensor: ev.tensor, measured: false, touched: { neurons: [], weights: [] }, sentence: `You took back an edit to ${ev.tensor}. ${NOT_MEASURED}` };
  CHANGED.events.push(out);
  return { ok: true, data: out };
}
const changedNow = () => { let n = 0; for (const [key, v] of CHANGED.numbers) { const [t, r, c] = key.split('|'); const sh = tensorShapes()[t]; if (matrix(t, sh[0], sh[1] ?? 1)[r]?.[c] !== v) n++; } return n; };

const leaf = (n, value) => ({ leaf: n, value, count: 10 });
const ask = (input, name, threshold, left, right, more = {}) => ({ input, name, threshold, gain: 5, categorical: false, missing_goes: 'left', left, right, ...more });
/** A full tree `depth` questions deep, with made-up thresholds: 2^depth leaves. */
function deepTree(depth, n = { at: 0 }) {
  if (depth === 0) return leaf(n.at++, Number((0.01 * (n.at % 7) - 0.03).toFixed(2)));
  const mine = n.at++;
  return ask(mine % 3, ['price', 'items seen', 'days since last visit'][mine % 3], mine + 1, deepTree(depth - 1, n), deepTree(depth - 1, n));
}

/** The services' answer for one ranker (`GET /api/weights/models/demo-session/trees?ranker=`). A value at or below
 *  the threshold goes left. */
export function rankerAnswer(ranker = 'full', o = {}) {
  if (ranker === 'quick') return { model: PARTS, version: 1, ranker: 'quick', rankers: ['full', 'quick'], kind: 'trees', objective: 'binary', trees: 25, inputs: 2, unused: [],
    by_input: [{ input: 0, name: 'model score', splits: 40, gain: 70, share: 0.7, trees: 25 }, { input: 1, name: 'price', splits: 12, gain: 30, share: 0.3, trees: 9 }],
    first_trees: [{ tree: 0, leaves: 2, depth: 1, root: ask(0, 'model score', 0.5, leaf(0, -0.2), leaf(1, 0.3)) }] };
  return { model: PARTS, version: 1, ranker: 'full', rankers: ['full', 'quick'], names_from: 'the cols file', kind: 'trees', objective: 'lambdarank', trees: 40, inputs: 4,
    unused: o.many ? Array.from({ length: 18 }, (_x, i) => ({ input: 12 + i, name: `input ${13 + i}`, constant: false })) : [{ input: 3, name: 'hour of day', constant: true }],
    unused_says: '1 of 4 inputs are never asked about by any tree, so changing them cannot change the answer: hour of day.',
    // As the services send it: in the inputs' own order, not by gain.
    by_input: o.many ? Array.from({ length: 30 }, (_x, i) => ({ input: i, name: `input ${i + 1}`, splits: i < 12 ? 30 - i : 0, gain: i < 12 ? 30 - i : 0, share: i < 12 ? (30 - i) / 294 : 0, trees: 1 })) : [{ input: 0, name: 'items seen', splits: 18, gain: 25, share: 0.25, trees: 12 }, { input: 1, name: 'price', splits: 31, gain: 46, share: 0.46, trees: 30 },
      { input: 2, name: 'days since last visit', splits: 22, gain: 29, share: 0.29, trees: 20 }, { input: 3, name: 'hour of day', splits: 0, gain: 0, share: 0, trees: 0 }],
    first_trees: [
      { tree: 0, leaves: 4, depth: 2, root: ask(1, 'price', 12.5, ask(0, 'items seen', 3, leaf(0, -0.05), leaf(1, 0.21)), ask(2, 'days since last visit', 7, leaf(2, 0.09), leaf(3, -0.18))) },
      { tree: 1, leaves: 3, depth: 2, root: ask(2, 'days since last visit', 2, leaf(0, -0.11), ask(1, 'price', 30, leaf(1, -0.07), leaf(2, 0.14))) },
      { tree: 2, leaves: 2, depth: 1, root: ask(0, 'shop section', 3, leaf(0, 0.02), leaf(1, -0.02), { categorical: true }) },
      ...(o.deep ? [{ tree: 3, leaves: 32, depth: 5, root: deepTree(5) }] : []),
    ],
    rule: 'At each question, a value at or below the threshold goes left. Anything else goes right.' };
}

const cosine = (a, b) => a.reduce((s, v, i) => s + v * b[i], 0) / ((Math.hypot(...a) * Math.hypot(...b)) || 1);
const span = (text, size) => { if (!text) return [0, size]; const [a, b] = String(text).split(':'); return [Number(a || 0), Number(b || size)]; };

/** Answers for the services' routes over the made-up model, or undefined for any other path. `tall` gives the
 *  item table 1,000 rows. `heads: null` is a model whose files do not say how many heads it has. */
export function partsRoutes(path, o = {}, opts = {}) {
  const [p, q = ''] = path.split('?');
  const arg = Object.fromEntries(q.split('&').filter(Boolean).map((kv) => kv.split('=').map(decodeURIComponent)));
  const m = partsModel(o), sh = tensorShapes(o);
  if (p === '/api/weights/models') return { ok: true, data: [{ id: m.id, name: m.name, says: m.says, sizes: [], parameters: m.parameters, versions: [1], kind: m.kind, kind_says: m.kind_says, ...(o.sourced ? { source_says: SOURCE_SAYS } : {}) }] };
  if (p === `/api/weights/models/${PARTS}` && o.sourced) return { ok: true, data: { ...m, source_says: SOURCE_SAYS } };
  if (p === `/api/weights/models/${PARTS}`) return { ok: true, data: m };
  if (p === `/api/weights/models/${PARTS}/session`) return { ok: true, data: { id: SESSION, model: PARTS, ledger: { raw_weights: true, weights_read: 0, changed: changedNow(), total: m.parameters }, events: CHANGED.events.map((e) => ({ ...e })) } };
  if (p === `/api/weights/sessions/${SESSION}/edits`) return o.edits ? editAnswer(sh, opts.body ?? {}) : { ok: false, error: 'This model can be read here, not changed or run yet.' };
  if (p === `/api/weights/sessions/${SESSION}/undo`) return undoAnswer(opts.body ?? {});
  if (p === `/api/weights/sessions/${SESSION}/save`) {
    if (!changedNow()) return { ok: false, error: 'There is nothing to save: the network is the same as its base.' };
    const n = CHANGED.events.length + 1;
    CHANGED.events.push({ id: `e${n}`, seq: n, by: 'you', kind: 'save', sentence: `You saved the network as ${PARTS}@v2.` });
    return { ok: true, data: { model: PARTS, version: 2, ref: `${PARTS}@v2`, base: `${PARTS}@v1`, changed: changedNow(), measured: false, not_measured: NOT_MEASURED } };
  }
  if (p === `/api/weights/models/${PARTS}/trees`) return ['full', 'quick', undefined].includes(arg.ranker) ? { ok: true, data: rankerAnswer(arg.ranker, o) } : { ok: false, error: `${PARTS} has no ranker named '${arg.ranker}'.` };
  if (p === `/api/weights/models/${PARTS}/rankers`) return o.plain ? { ok: false, error: 'Not Found' } : { ok: true, data: { model: PARTS, version: 1,
    rankers: [{ name: 'full', trees: 40, inputs: 4, used: 3, unused: ['hour of day'] }, { name: 'quick', trees: 25, inputs: 2, used: 2, unused: [] }],
    inputs: [{ name: 'price', group: 'price', by: {}, used_by: ['full', 'quick'], unused_by: [] }, { name: 'hour of day', group: 'hour', by: {}, used_by: [], unused_by: ['full'] },
      { name: 'items seen', group: 'items', by: {}, used_by: ['full'], unused_by: [] }],
    unused_by_all: ['hour of day'], unused_by_all_says: '1 input is used by no ranker: hour of day.', same_inputs: false } };
  // What each row stands for. The services name the item table's rows, with one row past the saved list.
  // A tall table's names, for the rows asked for only.
  if (p === `/api/weights/sessions/${SESSION}/labels` && arg.tensor === 'item.weight' && o.tall && o.named) { const [a, b] = span(arg.rows, 1000);
    return { ok: true, data: { tensor: arg.tensor, rows: 1000, rows_shown: [a, b], labels: Array.from({ length: b - a }, (_x, i) => `thing ${a + i + 1}`), labels_says: '', sure: true } }; }
  if (p === `/api/weights/sessions/${SESSION}/labels` && arg.tensor === 'cn') return { ok: true, data: { tensor: 'cn', rows: 40, labels: Array.from({ length: 40 }, (_x, i) => `item ${i + 1}, price bucket ${(i % 4) + 1}`), labels_says: '', sure: true } };
  if (p === `/api/weights/sessions/${SESSION}/labels`) return o.plain || arg.tensor !== 'item.weight' || o.tall ? { ok: true, data: { tensor: arg.tensor, labels: null, labels_says: '', sure: null } }
    : { ok: true, data: { tensor: arg.tensor, rows: ITEMS.length, labels: [...ITEMS.slice(0, -1), 'none or unknown'], labels_from: 'a saved list', extra_row: ITEMS.length - 1, sure: false,
      labels_says: 'The saved list is one name short of the rows. The last row is taken to be "none or unknown", which is a guess.' } };
  const at = new RegExp(`^/api/weights/sessions/${SESSION}/(heatmap|nearest|attention)$`).exec(p);
  if (!at) return undefined;
  if (at[1] === 'attention') {
    const head = Number(arg.head ?? 0), grid = matrix(`L${arg.layer}/${arg.matrix}/${head}`, W, W);
    return { ok: true, data: { layer: Number(arg.layer), part: `L${arg.layer}.self_attn`, width: W, heads: HEADS, run: false,
      of: [{ head, head_width: W / HEADS, query_key: { shape: [W, W] }, output_value: { shape: [W, W] }, reads: [], matrix: { which: arg.matrix, grid, scale: 1 } }] } };
  }
  const shape = sh[arg.tensor];
  if (!shape) return { ok: false, error: `There is no tensor named ${arg.tensor}.` };
  const [R, C] = shape.length === 2 ? shape : [1, shape[0] ?? 1], all = current(arg.tensor, R, C);
  if (at[1] === 'heatmap') {
    const [r0, r1] = span(arg.rows, R), grid = all.slice(r0, r1);
    return { ok: true, data: { tensor: arg.tensor, shape, rows: [r0, r1], cols: [0, C], grid_shape: [grid.length, C], scale: 1, min: -1, max: 1, grid, grids: [] } };
  }
  const row = Number(arg.row ?? 0), names = arg.tensor === 'item.weight' && R === ITEMS.length ? ITEMS : null;
  const one = (r) => ({ row: r, label: names ? names[r] : null, cosine: cosine(all[row], all[r]), norm: Math.hypot(...all[r]) });
  const order = all.map((_v, r) => r).filter((r) => r !== row).sort((a, b) => cosine(all[row], all[b]) - cosine(all[row], all[a]));
  return { ok: true, data: { tensor: arg.tensor, rows: R, width: C, row, label: names ? names[row] : null, norm: Math.hypot(...all[row]),
    nearest: order.slice(0, Number(arg.top ?? 10)).map(one), farthest: order.slice(-3).reverse().map(one) } };
}
