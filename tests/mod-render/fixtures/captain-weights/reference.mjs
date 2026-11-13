// A reference for the Weights page's arithmetic, written apart from the page's own (defaults/mods/captain-weights/
// net.js) so each checks the other: a plain forward pass, the squared-error cost, and gradients MEASURED by nudging
// one number at a time, never derived. Also a wide network, to check that a big column collapses to a strip.
const sig = (z) => 1 / (1 + Math.exp(-z));

const act = (net, k) => net.layers[k].activation ?? net.squish ?? 'sigmoid';

export function refForward(net, x) {
  let a = x.slice();
  net.layers.forEach(({ W, b }, k) => {
    const z = W.map((row, j) => row.reduce((s, w, i) => s + w * a[i], b[j]));
    const kind = act(net, k);
    if (kind === 'softmax') { const m = Math.max(...z), e = z.map((v) => Math.exp(v - m)), t = e.reduce((p, q) => p + q, 0); a = e.map((v) => v / t); }
    else a = z.map((v) => (kind === 'relu' ? Math.max(0, v) : kind === 'tanh' ? Math.tanh(v) : kind === 'linear' ? v : sig(v)));
  });
  return a;
}

export function refCost(net, x, y) {
  const out = refForward(net, x);
  if (net.cost !== 'cross_entropy') return out.reduce((s, v, j) => s + (v - y[j]) ** 2, 0);
  const soft = act(net, net.layers.length - 1) === 'softmax';
  return -out.reduce((s, p, j) => s + (soft ? y[j] * Math.log(p) : y[j] * Math.log(p) + (1 - y[j]) * Math.log(1 - p)), 0);
}

/** The slope of the cost against one number, measured: nudge it up and down by `h` and divide. */
export function measured(net, x, y, k, j, i, h = 1e-5) {
  const layer = net.layers[k];
  const get = () => (i == null ? layer.b[j] : layer.W[j][i]);
  const set = (v) => { if (i == null) layer.b[j] = v; else layer.W[j][i] = v; };
  const was = get();
  set(was + h); const up = refCost(net, x, y);
  set(was - h); const down = refCost(net, x, y);
  set(was);
  return (up - down) / (2 * h);
}

/** A network with one column too wide to draw neuron by neuron: 4-100-3. */
export function wideNet(seededNet) {
  const net = seededNet([4, 100, 3], 5);
  const ex = (name, x, y) => ({ name, x, y });
  return { id: 'demo-wide', name: 'Wide sample', ...net, outputNames: ['A', 'B', 'C'],
    examples: [ex('First', [1, 0, 0.5, 0.2], [1, 0, 0]), ex('Second', [0, 1, 0.1, 0.9], [0, 1, 0])], test: [ex('First', [1, 0, 0.5, 0.2], [1, 0, 0])] };
}
