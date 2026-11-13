// CAPTAIN: the two starter networks the Weights page opens with nothing set up. Both are made here, from a seed:
// nothing is downloaded and no real data is used.
//   starter:tiny    2-3-1, 13 numbers. Answers "is exactly one of the two inputs on?". Three hidden neurons were
//                   set by hand, and one bias is set wrong on purpose, so "Both on" comes out wrong: a mistake a
//                   person or an agent can find and fix with one number.
//   starter:digits  784-16-16-10, 13,002 numbers. Reads a 28 by 28 picture of a digit. The pictures are drawn
//                   here from seven strokes with a little jitter, and the network was trained on such pictures
//                   (digits-weights.js). It was stopped early, so it still gets a few wrong. A score on these
//                   pictures says nothing about handwriting.
import { seeded } from "./net.js";
import { SCALE, SIZES, PACKED } from "./digits-weights.js";

export const TINY = "starter:tiny";
export const DIGITS = "starter:digits";

export const STARTERS = [
  { id: TINY, name: "Tiny network", says: "2 inputs, 3 hidden neurons, 1 output. Every number fits on the screen." },
  { id: DIGITS, name: "Digit reader", says: "A 28 by 28 picture in, ten answers out. 13,002 numbers, trained on made-up digits." },
];

function tiny() {
  const input = (name, x, y) => ({ name, x, y: [y] });
  const examples = [input("Both off", [0, 0], 0), input("Only the first on", [1, 0], 1), input("Only the second on", [0, 1], 1), input("Both on", [1, 1], 0)];
  return {
    id: TINY, name: "Tiny network", sizes: [2, 3, 1], squish: "sigmoid", cost: "squared_error",
    layers: [
      // Hidden 1 turns on when either input is on, hidden 2 was meant to turn on only when both are, hidden 3
      // does little. Hidden 2's bias should be near -9: at -12 it stays half off when both inputs are on.
      { W: [[6, 6], [6, 6], [-2, 3]], b: [-3, -12, -1] },
      { W: [[7, -8, 0.4]], b: [-3] },
    ],
    inputNames: ["First input", "Second input"],
    outputNames: ["Exactly one is on"],
    examples,
    test: examples,
  };
}

// A digit as seven strokes: top, top left, top right, middle, bottom left, bottom right, bottom.
const STROKES = ["1110111", "0010010", "1011101", "1011011", "0111010", "1101011", "1101111", "1010010", "1111111", "1111011"];

/** A 28 by 28 picture of digit `d`, as 784 numbers from 0 (dark) to 1 (bright), shifted and thickened a little by
 *  `rand` so no two are the same. */
export function digitPicture(d, rand) {
  const px = new Array(784).fill(0);
  const dx = Math.round((rand() - 0.5) * 4), dy = Math.round((rand() - 0.5) * 4), thick = rand() < 0.5 ? 2 : 3;
  const x0 = 9 + dx, x1 = 18 + dx, y0 = 4 + dy, y1 = 13 + dy, y2 = 22 + dy;
  const bar = (xa, ya, xb, yb) => {
    for (let y = ya; y <= yb; y++) for (let x = xa; x <= xb; x++) {
      if (x >= 0 && x < 28 && y >= 0 && y < 28) px[y * 28 + x] = Math.min(1, 0.75 + rand() * 0.25);
    }
  };
  const on = STROKES[d];
  const t = thick - 1;
  if (on[0] === "1") bar(x0, y0, x1, y0 + t);
  if (on[1] === "1") bar(x0, y0, x0 + t, y1);
  if (on[2] === "1") bar(x1 - t, y0, x1, y1);
  if (on[3] === "1") bar(x0, y1, x1, y1 + t);
  if (on[4] === "1") bar(x0, y1, x0 + t, y2);
  if (on[5] === "1") bar(x1 - t, y1, x1, y2);
  if (on[6] === "1") bar(x0, y2 - t, x1, y2);
  return px;
}

/** `each` pictures of every digit, with the wanted answer: 1 for the right digit, 0 for the rest. */
export function digitSet(seed, each) {
  const rand = seeded(seed), out = [];
  for (let n = 0; n < each; n++) for (let d = 0; d < 10; d++) {
    out.push({ name: each === 1 ? `A ${d}` : `A ${d}, picture ${n + 1}`, x: digitPicture(d, rand), y: Array.from({ length: 10 }, (_v, j) => (j === d ? 1 : 0)), digit: d });
  }
  return out;
}

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/** The digit reader's numbers, unpacked (digits-weights.js): 16-bit steps of one scale, layer by layer, a layer's
 *  weights row by row and then its biases. Decoded here by hand: the app's JavaScript may have no atob. */
export function unpack(packed, scale, sizes) {
  const bytes = [];
  for (let n = 0; n + 3 < packed.length + 1; n += 4) {
    const q = [0, 1, 2, 3].map((d) => B64.indexOf(packed[n + d]));
    bytes.push((q[0] << 2) | (q[1] >> 4));
    if (q[2] >= 0) bytes.push(((q[1] & 15) << 4) | (q[2] >> 2));
    if (q[3] >= 0) bytes.push(((q[2] & 3) << 6) | q[3]);
  }
  let at = 0;
  const next = () => { const v = bytes[at] | (bytes[at + 1] << 8); at += 2; return (v >= 32768 ? v - 65536 : v) * scale; };
  const layers = [];
  for (let k = 0; k + 1 < sizes.length; k++) {
    const W = Array.from({ length: sizes[k + 1] }, () => Array.from({ length: sizes[k] }, next));
    layers.push({ W, b: Array.from({ length: sizes[k + 1] }, next) });
  }
  return layers;
}

function digits() {
  return {
    id: DIGITS, name: "Digit reader", sizes: SIZES.slice(), squish: "sigmoid", cost: "squared_error", layers: unpack(PACKED, SCALE, SIZES),
    inputShape: [28, 28],
    outputNames: ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
    examples: digitSet(11, 1),
    test: digitSet(23, 5),
  };
}

/** A fresh copy of a starter network, or null when the id is not one. */
export function starter(id) {
  if (id === TINY) return tiny();
  if (id === DIGITS) return digits();
  return null;
}
