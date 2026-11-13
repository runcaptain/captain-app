// CAPTAIN: the arithmetic of a small fully connected network, in plain JavaScript: run it forward, measure the
// cost, and walk the error backward. The Weights page uses it for the two starter networks and to answer at once
// when a person drags a weight. A real model's numbers come from Captain's services through adapter.js, in the
// same shapes (shapes.js). tests/mod-render/weights-net.test.mjs checks every gradient here against a measured one.
//
// A network: { sizes: [2, 3, 1], squish: "sigmoid" | "relu", layers: [{ W: [to][from], b: [to] }] }.
// layers[k] joins the neurons of column k to the neurons of column k + 1.

const sigmoid = (z) => 1 / (1 + Math.exp(-z));

/** The squish: a neuron's weighted sum to its activation. */
export function squish(kind, z) { return kind === "relu" ? Math.max(0, z) : sigmoid(z); }

/** How steep the squish is at z: how much the activation moves when the weighted sum moves a little. */
export function steepness(kind, z) {
  if (kind === "relu") return z > 0 ? 1 : 0;
  const s = sigmoid(z);
  return s * (1 - s);
}

/** Run the network on one input. a[0] is the input; a[k] and z[k] are column k's activations and weighted sums. */
export function forward(net, x) {
  const a = [Array.from(x)], z = [null];
  for (let k = 0; k < net.layers.length; k++) {
    const { W, b } = net.layers[k], prev = a[k];
    const zk = new Array(W.length), ak = new Array(W.length);
    for (let j = 0; j < W.length; j++) {
      const row = W[j];
      let s = b[j];
      for (let i = 0; i < row.length; i++) s += row[i] * prev[i];
      zk[j] = s;
      ak[j] = squish(net.squish, s);
    }
    z.push(zk);
    a.push(ak);
  }
  return { a, z };
}

/** The cost of one example: the squared differences between what came out and what was wanted, added up. */
export function costOf(out, wanted) {
  let c = 0;
  for (let j = 0; j < out.length; j++) c += (out[j] - wanted[j]) ** 2;
  return c;
}

/** The average cost over a set of examples [{x, y}], and how many the network got right (its brightest output is
 *  the wanted one; with one output, it is on the right side of a half). */
export function costOver(net, examples) {
  let total = 0, right = 0;
  for (const e of examples) {
    const out = forward(net, e.x).a.at(-1);
    total += costOf(out, e.y);
    if (answerOf(out) === answerOf(e.y)) right++;
  }
  const n = Math.max(1, examples.length);
  return { cost: total / n, right, of: examples.length };
}

/** Which answer a column of outputs gives: the brightest, or for a single output whether it is above a half. */
export function answerOf(out) {
  if (out.length === 1) return out[0] >= 0.5 ? 1 : 0;
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
  dA[L] = a[L].map((v, j) => 2 * (v - wanted[j]));
  for (let k = L; k >= 1; k--) {
    const { W } = net.layers[k - 1];
    dZ[k] = dA[k].map((d, j) => d * steepness(net.squish, z[k][j]));
    dB[k - 1] = dZ[k].slice();
    dW[k - 1] = W.map((row, j) => row.map((_w, i) => dZ[k][j] * a[k - 1][i]));
    const back = new Array(a[k - 1].length).fill(0);
    for (let j = 0; j < W.length; j++) for (let i = 0; i < back.length; i++) back[i] += W[j][i] * dZ[k][j];
    dA[k - 1] = back;
  }
  dZ[0] = null;
  return { cost: costOf(a[L], wanted), dA, dZ, dW, dB };
}

/** The chain rule along one weight: the three factors whose product is the weight's gradient, when the weight
 *  feeds an output neuron directly. For a weight further back, the third factor is everything behind the neuron it
 *  feeds: the sum over every path from that neuron to the cost. */
export function chain(net, run, grad, layer, to, from) {
  const k = layer + 1;
  const before = run.a[layer][from];
  const steep = steepness(net.squish, run.z[k][to]);
  const behind = grad.dA[k][to];
  return { before, steep, behind, product: before * steep * behind, last: k === net.layers.length };
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
  return { sizes: sizes.slice(), squish: squishKind, layers };
}
