// A made-up model of parts for the Weights page's tests and pictures: a small transformer "session model" (width 8)
// with two tree rankers, in the shapes of defaults/mods/captain-weights/shapes.js (PartsModel, Heatmap, RowStats,
// NearRows). Every name and every number here is invented by this file. Nothing comes from a real model.
export const PARTS = 'demo-session';

const WIDTH = 8;
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

const t = (id, name, shape, extra = {}) => ({ id, name, shape, parameters: shape[0] * shape[1], ...extra });
const part = (id, kind, name, shape, tensors, extra = {}) => ({ id, kind, name, shape, parameters: tensors.reduce((n, x) => n + x.parameters, 0), tensors, ...extra });
const table = (id, name, rows, of) => part(id, 'embedding', name, [rows, WIDTH], [t('table', 'The table', [rows, WIDTH])], { rows_are: of });
const attention = (id, block, from) => part(id, 'attention', `Attention ${block}`, [WIDTH, WIDTH], [
  t('qk', 'Query-key', [WIDTH, WIDTH], { per_head: true }), t('ov', 'Output-value', [WIDTH, WIDTH], { per_head: true }),
  t('query', 'Query', [WIDTH, WIDTH]), t('key', 'Key', [WIDTH, WIDTH]), t('value', 'Value', [WIDTH, WIDTH]), t('output', 'Output', [WIDTH, WIDTH]),
].map((x) => (x.per_head ? { ...x, parameters: 0 } : x)), { block, heads: 2, feeds_from: from });
const norm = (id, block, from) => part(id, 'norm', 'Norm', [2, WIDTH], [t('scale', 'Scale and shift', [2, WIDTH])], { block, feeds_from: from });
const feed = (id, block, from) => part(id, 'feed_forward', `Feed-forward ${block}`, [WIDTH * 2, WIDTH], [t('in', 'Into the hidden step', [WIDTH * 2, WIDTH]), t('out', 'Out of the hidden step', [WIDTH, WIDTH * 2])], { block, feeds_from: from });

const ITEMS = ['Blue kettle', 'Red kettle', 'Tea towel', 'Garden trowel', 'Seed tray', 'Watering can', 'Picture book', 'Story book', 'Wooden train', 'Toy drum', 'Gift card', 'Wrapping paper'];
const KINDS = ['Looked at', 'Added to basket', 'Bought', 'Searched'];
const rowName = (partId, row) => (partId === 'emb-item' ? ITEMS[row] : partId === 'emb-kind' ? KINDS[row] : partId === 'emb-pos' ? `Place ${row + 1}` : null);

const tree = (n, nodes) => ({ tree: n, nodes });
/** The made-up model, whole. */
export function partsModel() {
  const parts = [
    table('emb-item', 'Item table', 12, 'items'),
    table('emb-kind', 'Event kind table', 4, 'kinds of event'),
    table('emb-pos', 'Position table', 6, 'places in the session'),
    part('proj', 'projection', 'Text projection', [WIDTH, 16], [t('weight', 'The projection', [WIDTH, 16])]),
    attention('attn-1', 1, ['emb-item', 'emb-kind', 'emb-pos', 'proj']),
    norm('norm-1a', 1, ['attn-1']),
    feed('ff-1', 1, ['norm-1a']),
    norm('norm-1b', 1, ['ff-1']),
    attention('attn-2', 2, ['norm-1b']),
    norm('norm-2a', 2, ['attn-2']),
    feed('ff-2', 2, ['norm-2a']),
    norm('norm-2b', 2, ['ff-2']),
    part('head-next', 'head', 'Next item head', [12, WIDTH], [t('weight', 'The head', [12, WIDTH])], { feeds_from: ['norm-2b'] }),
    part('head-buy', 'head', 'Will buy head', [1, WIDTH], [t('weight', 'The head', [1, WIDTH])], { feeds_from: ['norm-2b'] }),
  ];
  const rankers = [
    { id: 'rank-first', name: 'First ranker', trees: 40,
      inputs: [{ name: 'price', splits: 31, gain_share: 0.46 }, { name: 'days since last visit', splits: 22, gain_share: 0.29 }, { name: 'items seen', splits: 18, gain_share: 0.25 }, { name: 'hour of day', splits: 0, gain_share: 0 }],
      unused: ['hour of day'],
      shown: [
        tree(0, [{ id: 0, input: 'price', threshold: 12.5, yes: 1, no: 2 }, { id: 1, input: 'items seen', threshold: 3, yes: 3, no: 4 }, { id: 2, input: 'days since last visit', threshold: 7, yes: 5, no: 6 },
          { id: 3, leaf: 0.21 }, { id: 4, leaf: -0.05 }, { id: 5, leaf: -0.18 }, { id: 6, leaf: 0.09 }]),
        tree(1, [{ id: 0, input: 'days since last visit', threshold: 2, yes: 1, no: 2 }, { id: 1, leaf: -0.11 }, { id: 2, input: 'price', threshold: 30, yes: 3, no: 4 }, { id: 3, leaf: -0.07 }, { id: 4, leaf: 0.14 }]),
      ] },
    { id: 'rank-second', name: 'Second ranker', trees: 25,
      inputs: [{ name: 'model score', splits: 40, gain_share: 0.7 }, { name: 'price', splits: 12, gain_share: 0.3 }],
      unused: [],
      shown: [tree(0, [{ id: 0, input: 'model score', threshold: 0.5, yes: 1, no: 2 }, { id: 1, leaf: 0.3 }, { id: 2, leaf: -0.2 }])] },
  ];
  const parameters = parts.reduce((n, p) => n + p.parameters, 0);
  return { id: PARTS, name: 'Demo session model', kind: 'parts', kind_words: 'Session model with rankers', parameters, width: WIDTH, source: 'Imported from a made-up training run.', parts, rankers };
}

const norms = (m) => m.map((row) => Math.hypot(...row));
const cosine = (a, b) => a.reduce((s, v, i) => s + v * b[i], 0) / ((Math.hypot(...a) * Math.hypot(...b)) || 1);

/** Answers for the services' routes over the made-up model, or undefined for any other path. `wide` makes every
 *  heatmap answer as a 96 by 96 matrix the services already reduced to 64 by 64. */
export function partsRoutes(path, { wide = false } = {}) {
  const [p, q = ''] = path.split('?');
  const arg = Object.fromEntries(q.split('&').filter(Boolean).map((kv) => kv.split('=').map(decodeURIComponent)));
  const m = partsModel();
  if (p === '/api/weights/models') return { ok: true, data: [{ id: m.id, name: m.name, kind: m.kind, kind_words: m.kind_words, parameters: m.parameters, source: m.source }] };
  if (p === `/api/weights/models/${PARTS}`) return { ok: true, data: m };
  const at = new RegExp(`^/api/weights/models/${PARTS}/parts/([^/]+)/(heatmap|rows|nearest)$`).exec(p);
  if (!at) return undefined;
  const it = m.parts.find((x) => x.id === at[1]);
  if (!it) return { ok: false, error: 'There is no part with that name.' };
  const tensor = it.tensors.find((x) => x.id === arg.tensor) ?? it.tensors[0];
  const name = `${it.id}/${tensor.id}/${tensor.per_head ? arg.head ?? 0 : ''}`;
  const values = matrix(name, tensor.shape[0], tensor.shape[1]);
  if (at[2] === 'heatmap') {
    if (wide) return { ok: true, data: { tensor: tensor.id, shape: [96, 96], shown: [64, 64], reduced: true, values: matrix(name, 64, 64) } };
    return { ok: true, data: { tensor: tensor.id, shape: tensor.shape, shown: tensor.shape, reduced: false, values } };
  }
  if (at[2] === 'rows') {
    const n = norms(values);
    return { ok: true, data: values.map((row, r) => ({ row: r, name: rowName(it.id, r), norm: n[r], mean: row.reduce((s, v) => s + v, 0) / row.length, largest: Math.max(...row.map(Math.abs)) }))
      .sort((a, b) => b.norm - a.norm).slice(0, Number(arg.top ?? 8)) };
  }
  const row = Number(arg.row ?? 0);
  return { ok: true, data: { row, name: rowName(it.id, row), nearest: values.map((v, r) => ({ row: r, name: rowName(it.id, r), similarity: cosine(values[row], v) })).filter((x) => x.row !== row)
    .sort((a, b) => b.similarity - a.similarity).slice(0, Number(arg.top ?? 8)) } };
}
