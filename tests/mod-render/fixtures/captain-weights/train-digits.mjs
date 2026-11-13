// Trains the Weights page's own digit reader (784-16-16-10) on made-up digits and writes its numbers to
// defaults/mods/captain-weights/digits-weights.js. No data is downloaded: the pictures are drawn by
// starters.js digitSet from seven strokes with a seed. Run it again only to change the network:
//   node tests/mod-render/fixtures/captain-weights/train-digits.mjs
// Plain gradient descent, one picture at a time, with the page's own backward(). The same seed gives the same
// file. The numbers are stored as 16-bit steps of one scale, which is why the file is small.
import fs from 'node:fs';
import { forward, backward, costOver, seededNet, seeded } from '../../../../defaults/mods/captain-weights/net.js';
import { digitSet } from '../../../../defaults/mods/captain-weights/starters.js';

const net = seededNet([784, 16, 16, 10], 7);
const train = digitSet(101, 60);
const rand = seeded(5);
// Stopped early on purpose: a network that gets every picture right leaves nothing to find or fix. At 5 passes it
// gets 48 of the 50 test pictures and 9 of the 10 pictures a person can choose.
const RATE = 0.35, EPOCHS = 5;
for (let epoch = 0; epoch < EPOCHS; epoch++) {
  const order = train.map((_e, n) => n).sort(() => rand() - 0.5);
  for (const n of order) {
    const e = train[n], g = backward(net, e.x, e.y, forward(net, e.x));
    net.layers.forEach(({ W, b }, k) => {
      for (let j = 0; j < b.length; j++) {
        b[j] -= RATE * g.dB[k][j];
        const row = W[j], grow = g.dW[k][j];
        for (let i = 0; i < row.length; i++) row[i] -= RATE * grow[i];
      }
    });
  }
}
// 16-bit steps of one scale.
let top = 0;
for (const { W, b } of net.layers) { for (const r of W) for (const v of r) top = Math.max(top, Math.abs(v)); for (const v of b) top = Math.max(top, Math.abs(v)); }
const scale = top / 32767;
const ints = [];
for (const { W, b } of net.layers) { for (const r of W) for (const v of r) ints.push(Math.round(v / scale)); for (const v of b) ints.push(Math.round(v / scale)); }
const bytes = Buffer.alloc(ints.length * 2);
ints.forEach((v, n) => bytes.writeInt16LE(v, n * 2));
const b64 = bytes.toString('base64');
const lines = b64.match(/.{1,116}/g).map((l) => `  "${l}",`).join('\n');
fs.writeFileSync(new URL('../../../../defaults/mods/captain-weights/digits-weights.js', import.meta.url), `// CAPTAIN: the numbers of the Weights page's own digit reader (784-16-16-10): ${ints.length} weights and biases,
// trained on made-up digits by tests/mod-render/fixtures/captain-weights/train-digits.mjs. Each number is a 16-bit
// step of SCALE, layer by layer: a layer's weights row by row, then its biases. Written by that script: do not
// edit by hand.
export const SCALE = ${scale};
export const SIZES = [784, 16, 16, 10];
export const PACKED = [
${lines}
].join("");
`);
const test = digitSet(23, 5);
console.log('train', costOver(net, train), 'test', costOver(net, test), 'bytes', b64.length);
