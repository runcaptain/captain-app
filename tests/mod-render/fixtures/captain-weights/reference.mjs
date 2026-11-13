// A reference for the Weights page's arithmetic, written apart from the page's own (defaults/mods/captain-weights/
// net.js) so each checks the other: a plain forward pass, the squared-error cost, and gradients MEASURED by nudging
// one number at a time, never derived. Also a wide network, to check that a big column collapses to a strip.
const sig = (z) => 1 / (1 + Math.exp(-z));

export function refForward(net, x) {
  let a = x.slice();
  for (const { W, b } of net.layers) a = W.map((row, j) => { const z = row.reduce((s, w, i) => s + w * a[i], b[j]); return net.squish === 'relu' ? Math.max(0, z) : sig(z); });
  return a;
}

export function refCost(net, x, y) {
  return refForward(net, x).reduce((s, v, j) => s + (v - y[j]) ** 2, 0);
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
