// A 784-16-16-10 network for the Weights page's tests, served the way Captain's services serve one: whole, with
// practice inputs, and with its test set kept back and scored by an evaluate route. The weights are seeded, not
// trained, and the pictures are drawn here from seven strokes: nothing is downloaded. The page itself ships no
// digit network: the services make one.
import { seeded, seededNet, costOver } from '../../../../defaults/mods/captain-weights/net.js';

export const DIGITS = 'digits';

// A digit as seven strokes: top, top left, top right, middle, bottom left, bottom right, bottom.
const STROKES = ['1110111', '0010010', '1011101', '1011011', '0111010', '1101011', '1101111', '1010010', '1111111', '1111011'];

/** A 28 by 28 picture of digit `d`: 784 numbers from 0 (dark) to 1 (bright), shifted and thickened a little. */
export function digitPicture(d, rand) {
  const px = new Array(784).fill(0);
  const dx = Math.round((rand() - 0.5) * 4), dy = Math.round((rand() - 0.5) * 4), thick = rand() < 0.5 ? 2 : 3;
  const x0 = 9 + dx, x1 = 18 + dx, y0 = 4 + dy, y1 = 13 + dy, y2 = 22 + dy;
  const bar = (xa, ya, xb, yb) => { for (let y = ya; y <= yb; y++) for (let x = xa; x <= xb; x++) if (x >= 0 && x < 28 && y >= 0 && y < 28) px[y * 28 + x] = Math.min(1, 0.75 + rand() * 0.25); };
  const on = STROKES[d], t = thick - 1;
  if (on[0] === '1') bar(x0, y0, x1, y0 + t);
  if (on[1] === '1') bar(x0, y0, x0 + t, y1);
  if (on[2] === '1') bar(x1 - t, y0, x1, y1);
  if (on[3] === '1') bar(x0, y1, x1, y1 + t);
  if (on[4] === '1') bar(x0, y1, x0 + t, y2);
  if (on[5] === '1') bar(x1 - t, y1, x1, y2);
  if (on[6] === '1') bar(x0, y2 - t, x1, y2);
  return px;
}

export function digitSet(seed, each) {
  const rand = seeded(seed), out = [];
  for (let n = 0; n < each; n++) for (let d = 0; d < 10; d++) {
    out.push({ name: each === 1 ? `A ${d}` : `A ${d}, picture ${n + 1}`, x: digitPicture(d, rand), y: Array.from({ length: 10 }, (_v, j) => (j === d ? 1 : 0)) });
  }
  return out;
}

/** The network with its test set (for a test to check against). The routes below never send `test`. */
export function digitsNet() {
  return { id: DIGITS, name: 'digits', ...seededNet([784, 16, 16, 10], 7), inputShape: [28, 28],
    outputNames: ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'], examples: digitSet(11, 1), test: digitSet(23, 5) };
}

/** What the services would answer for this network, or null for any other path. */
export function digitsRoutes(path) {
  const p = String(path).split('?')[0];
  const net = digitsNet();
  if (p === '/api/weights/models/digits') return { ok: true, data: { ...net, test: [] } };
  if (p === '/api/weights/models/digits/session') {
    // The services never send the test set, and the page never asks them to score it: the score is in the record,
    // here as an evaluation someone ran.
    const t = costOver(net, net.test);
    return { ok: true, data: { id: 'ws-digits', model: DIGITS, ledger: { raw_weights: true, weights_read: 0, sandbox: true, sandbox_used: false, forward_allowed: 50, forward_used: 0, changed: 0, total: 13002 },
      events: [{ id: 'e1', seq: 1, by: 'agent', kind: 'evaluate', touched: { neurons: [], weights: [] }, cost: { test: { of: t.of, before: t.cost, after: t.cost, right_before: t.right, right_after: t.right } } }] } };
  }
  return null;
}
