// CAPTAIN: the arithmetic of a small fully connected network, in plain JavaScript: run it forward, measure the
// cost, and walk the error backward. The Weights page uses it for the two starter networks and to answer at once
// when a person drags a weight. A real model's numbers come from Captain's services through adapter.js, in the
// same shapes (shapes.js). tests/mod-render/weights-net.test.mjs checks every gradient here against a measured one.
//
// A network: { sizes: [2, 3, 1], squish: "sigmoid", cost: "squared_error" | "cross_entropy",
//              layers: [{ W: [to][from], b: [to], activation? }] }.
// layers[k] joins the neurons of column k to the neurons of column k + 1. A layer's `activation` (sigmoid, relu,
// tanh, linear, or softmax on the last layer) wins over the network's `squish`. The formulas are the ones
// Captain's services use (captain_search/weights/net.py), so the page and the services agree to the last digit.

const EPS = 1e-12;
const sigmoid = (z) => 1 / (1 + Math.exp(-Math.min(500, Math.max(-500, z))));

/** The squish of layer k: what turns its neurons' weighted sums into activations. */
export function squishOf(net, k) { return net.layers[k].activation ?? net.squish ?? "sigmoid"; }

/** A column's weighted sums to its activations. Softmax needs the whole column: its outputs add up to 1. */
export function squish(kind, z) {
  if (kind === "softmax") {
    const top = Math.max(...z), e = z.map((v) => Math.exp(v - top)), sum = e.reduce((s, v) => s + v, 0);
    return e.map((v) => v / sum);
  }
  return z.map((v) => (kind === "relu" ? Math.max(0, v) : kind === "tanh" ? Math.tanh(v) : kind === "linear" ? v : sigmoid(v)));
}

/** How steep the squish is at one neuron: how much its activation moves when its weighted sum moves a little.
 *  `a` is its activation there. For softmax this is only the neuron's effect on itself. */
export function steepness(kind, z, a) {
  if (kind === "relu") return z > 0 ? 1 : 0;
  if (kind === "tanh") { const t = a ?? Math.tanh(z); return 1 - t * t; }
  if (kind === "linear") return 1;
  const s = a ?? sigmoid(z);
  return s * (1 - s);
}

/** Run the network on one input. a[0] is the input; a[k] and z[k] are column k's activations and weighted sums. */
export function forward(net, x) {
  const a = [Array.from(x)], z = [null];
  for (let k = 0; k < net.layers.length; k++) {
    const { W, b } = net.layers[k], prev = a[k];
    const zk = new Array(W.length);
    for (let j = 0; j < W.length; j++) {
      const row = W[j];
      let s = b[j];
      for (let i = 0; i < row.length; i++) s += row[i] * prev[i];
      zk[j] = s;
    }
    z.push(zk);
    a.push(squish(squishOf(net, k), zk));
  }
  return { a, z };
}

const clip = (p) => Math.min(1 - EPS, Math.max(EPS, p));

/** The cost of one example. Squared error: the squared differences between what came out and what was wanted,
 *  added up. Cross-entropy: how surprised the network is by the wanted answer. */
export function costOf(net, out, wanted) {
  let c = 0;
  if ((net.cost ?? "squared_error") !== "cross_entropy") { for (let j = 0; j < out.length; j++) c += (out[j] - wanted[j]) ** 2; return c; }
  const soft = squishOf(net, net.layers.length - 1) === "softmax";
  for (let j = 0; j < out.length; j++) {
    const p = clip(out[j]);
    c -= soft ? wanted[j] * Math.log(p) : wanted[j] * Math.log(p) + (1 - wanted[j]) * Math.log(1 - p);
  }
  return c;
}

/** The average cost over a set of examples [{x, y}], and how many the network got right (its brightest output is
 *  the wanted one; with one output, it is on the right side of a half). */
export function costOver(net, examples) {
  let total = 0, right = 0;
  for (const e of examples) {
    const out = forward(net, e.x).a.at(-1);
    total += costOf(net, out, e.y);
    if (answerOf(out) === answerOf(e.y)) right++;
  }
  const n = Math.max(1, examples.length);
  return { cost: total / n, right, of: examples.length };
}

/** Which answer a column of outputs gives: the brightest, or for a single output whether it is above a half. */
export function answerOf(out) {
  if (out.length === 1) return out[0] > 0.5 ? 1 : 0;
  let best = 0;
  for (let j = 1; j < out.length; j++) if (out[j] > out[best]) best = j;
  return best;
}

/** Backpropagation for one example. Returns the cost and, for every column and every weight, how the cost changes
 *  when that number rises a little:
 *    dA[k][j]  the cost against neuron j's activation in column k (its "desired nudge" is the negative of this)
 *    dZ[k][j]  the cost against its weighted sum
 *    dW[k][j][i], dB[k][j]  the cost against each weight and bias of layers[k]
 *  A positive entry means raising the number raises the cost, so the network wants it lower. */
export function backward(net, x, wanted, run = forward(net, x)) {
  const { a, z } = run, L = net.layers.length;
  const dA = new Array(L + 1), dZ = new Array(L + 1), dW = new Array(L), dB = new Array(L);
  const last = squishOf(net, L - 1), out = a[L];
  if ((net.cost ?? "squared_error") !== "cross_entropy") {
    dA[L] = out.map((v, j) => 2 * (v - wanted[j]));
    if (last === "softmax") {
      const s = out.map((v, j) => v * dA[L][j]), sum = s.reduce((t, v) => t + v, 0);
      dZ[L] = s.map((v, j) => v - out[j] * sum);
    } else dZ[L] = dA[L].map((d, j) => d * steepness(last, z[L][j], out[j]));
  } else {
    dA[L] = out.map((v, j) => { const p = clip(v); return last === "softmax" ? -wanted[j] / p : (p - wanted[j]) / (p * (1 - p)); });
    dZ[L] = out.map((v, j) => v - wanted[j]);
  }
  for (let k = L; k >= 1; k--) {
    const { W } = net.layers[k - 1];
    if (k < L) dZ[k] = dA[k].map((d, j) => d * steepness(squishOf(net, k - 1), z[k][j], a[k][j]));
    dB[k - 1] = dZ[k].slice();
    dW[k - 1] = W.map((row, j) => row.map((_w, i) => dZ[k][j] * a[k - 1][i]));
    const back = new Array(a[k - 1].length).fill(0);
    for (let j = 0; j < W.length; j++) for (let i = 0; i < back.length; i++) back[i] += W[j][i] * dZ[k][j];
    dA[k - 1] = back;
  }
  dZ[0] = null;
  return { cost: costOf(net, out, wanted), dA, dZ, dW, dB };
}

/** The chain rule along one weight: three factors whose product is the weight's gradient.
 *    before  how active the neuron before is
 *    steep   how steep the squish is at the neuron the weight feeds
 *    behind  how far off that neuron is, as the cost sees it. For an output neuron that is the output's own
 *            error. For a neuron further back it is everything behind it: the sum over every path to the cost.
 *  Softmax ties its outputs together, so there the product of the three is not the gradient (`exact` is false)
 *  and `gradient` is the true one. */
export function chain(net, run, grad, layer, to, from) {
  const k = layer + 1, kind = squishOf(net, layer);
  const before = run.a[layer][from];
  const steep = steepness(kind, run.z[k][to], run.a[k][to]);
  const behind = grad.dA[k][to];
  return { before, steep, behind, product: before * steep * behind, gradient: grad.dW[layer][to][from], exact: kind !== "softmax", last: k === net.layers.length };
}

/** How many weights and biases the network has. */
export function countOf(net) {
  let weights = 0, biases = 0;
  for (const { W, b } of net.layers) { weights += W.length * (W[0]?.length ?? 0); biases += b.length; }
  return { weights, biases, total: weights + biases };
}

/** A copy that shares nothing with the original, so an edit can be tried and taken back. */
export function copyNet(net) {
  return { ...net, layers: net.layers.map(({ W, b }) => ({ W: W.map((r) => r.slice()), b: b.slice() })) };
}

/** A small repeatable source of numbers in [0, 1): the same seed always gives the same network. */
export function seeded(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A network of these sizes with small seeded weights, as one starts before any learning. */
export function seededNet(sizes, seed, squishKind = "sigmoid") {
  const r = seeded(seed);
  const normal = () => Math.sqrt(-2 * Math.log(1 - r())) * Math.cos(2 * Math.PI * r());
  const layers = [];
  for (let k = 0; k + 1 < sizes.length; k++) {
    const scale = 1 / Math.sqrt(sizes[k]);
    layers.push({
      W: Array.from({ length: sizes[k + 1] }, () => Array.from({ length: sizes[k] }, () => normal() * scale)),
      b: Array.from({ length: sizes[k + 1] }, () => normal() * 0.1),
    });
  }
  return { sizes: sizes.slice(), squish: squishKind, cost: "squared_error", layers };
}
