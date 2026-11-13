// The Weights page (defaults/mods/captain-weights) drawn in Node: the list with the two starter networks, the tiny
// network with every weight drawn, the digit reader with its input as a picture and a neuron's weights as a second
// picture, a wide column collapsed to a strip, and every draw under the app's ceiling on shapes.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness, shapesIn, SHAPES_PER_DRAW } from './harness.mjs';
import { wideNet } from './fixtures/captain-weights/reference.mjs';

const { default: Weights, factsOf } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { layoutOf, shapesFor, MAX_SHAPES, DOTS_MAX } = await import('../../defaults/mods/captain-weights/draw.js');
const { forward, backward, seededNet } = await import('../../defaults/mods/captain-weights/net.js');
const { starter, TINY, DIGITS } = await import('../../defaults/mods/captain-weights/starters.js');
const { palette } = await import('../../defaults/mods/captain-weights/kit/tokens.js');

// Captain's services know no networks here: only the starters show.
const served = { '/api/weights/models': { ok: false } };
weightsTransport(async (path) => served[path] ?? { ok: false, error: 'Not Found' });

async function page(model) {
  localReset();
  globalThis.__props = model ? { model } : {};
  const h = harness(Weights);
  await h.start();
  return h;
}
const paths = (h) => { const out = []; (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'path') out.push(e); for (const k of e.kids ?? []) w(k); })(h.mod.render(h.cx)); return out; };

test('with nothing open, the two starter networks are offered and nothing is filled', async () => {
  const h = await page(null);
  const text = h.text();
  assert.ok(text.includes('Tiny network') && text.includes('Digit reader'), text);
  assert.equal(h.byIdPrefix('wt-open-btn-').length, 2);
  await h.click('wt-open-btn-starter:tiny');
  assert.ok(h.text().includes('Weights of Tiny network'));
});

test('the tiny network: 6 circles, 9 lines, every weight and bias as a number', async () => {
  const h = await page(TINY);
  const text = h.text();
  assert.ok(text.includes('2 in, 1 out, 13 numbers (9 weights and 4 biases)'), text);
  assert.equal(h.byIdPrefix('wt-n-').length, 6, 'a circle per neuron');
  assert.equal(h.byIdPrefix('wt-w-').length, 9, 'a number on every line');
  assert.equal(h.byIdPrefix('wt-b-').length, 4, 'a bias on every neuron that has one');
  assert.ok(text.includes('bias -12.00') && text.includes('bias -3.00'));
  const net = starter(TINY);
  const s = shapesFor(layoutOf(net), { net, run: forward(net, net.examples[0].x), sel: null, mode: 'values' }, palette(false));
  assert.equal(s.count, 9, 'nine lines, one shape each');
  // Positive blue, negative red; three of the nine weights are negative (-2, -8 and none other... and 0.4 is positive).
  const c = palette(false);
  const byColour = (col) => [...s.groups].filter(([k]) => k.startsWith(col)).reduce((n, [, v]) => n + v.length, 0);
  assert.equal(byColour(c.fail), 2, 'the two negative weights are red');
  assert.equal(byColour(c.blue), 7, 'the seven positive weights are blue');
  // The circles show the activations of the chosen input, and change with it.
  const before = h.text();
  await h.click('wt-in-3');
  assert.notEqual(h.text(), before, 'another input, other activations');
  const run = forward(net, net.examples[3].x);
  assert.ok(h.text().includes(run.a[2][0].toFixed(2)), 'the output shows its activation');
});

test('the digit reader: the input is a picture, a first-column neuron opens its weights as a picture, no line per pixel', async () => {
  const h = await page(DIGITS);
  assert.ok(h.text().includes('784 in, 10 out, 13,002 numbers (12,960 weights and 42 biases)'), h.text());
  assert.equal(h.byIdPrefix('wt-n-').length, 42, 'circles for 16 + 16 + 10; the 784 inputs are a picture');
  const net = starter(DIGITS), L = layoutOf(net), c = palette(false);
  assert.deepEqual(L.cols.map((x) => x.kind), ['image', 'dots', 'dots', 'dots']);
  const run = forward(net, net.examples[8].x);
  const rest = shapesFor(L, { net, run, sel: null, mode: 'values' }, c);
  await h.click('wt-n-1-4');
  assert.ok(h.text().includes('What neuron 5 responds to: its 784 weights'));
  const open = shapesFor(L, { net, run, sel: { k: 1, j: 4 }, mode: 'values' }, c);
  assert.ok(open.count > rest.count + 100, 'the weights picture adds cells');
  for (const s of [rest, open]) assert.ok(s.count <= MAX_SHAPES && s.dropped === 0, `${s.count} shapes, ${s.dropped} left out`);
  // 16 x 16 + 16 x 10 lines between the circle columns, and never 12,544 into the first.
  const lineColours = [...rest.groups].filter(([k]) => k.startsWith(c.blue) || k.startsWith(c.fail)).reduce((n, [, v]) => n + v.length, 0);
  assert.equal(lineColours, 256 + 160);
});

test('every draw stays under the ceiling, at its worst: an "8", a neuron open, nudges on', async () => {
  for (const id of [TINY, DIGITS]) {
    const h = await page(id);
    for (let n = 0; n < h.mod.net.examples.length; n++) {
      await h.click(`wt-in-${n}`);
      await h.click('wt-n-1-0');
      const n1 = shapesIn(h.mod.render(h.cx));
      assert.ok(n1 <= SHAPES_PER_DRAW, `${id} input ${n}: ${n1} shapes`);
      await h.click('wt-n-1-0');
    }
  }
  // The budget itself: a picture of all-bright pixels with all-different weights still fits, by leaving out the
  // faintest lines.
  const net = starter(DIGITS), L = layoutOf(net), c = palette(true);
  const x = net.examples[0].x.map((_v, i) => 0.3 + 0.7 * ((i * 7919) % 97) / 97);
  const run = forward(net, x), grad = backward(net, x, net.examples[0].y, run);
  const worst = shapesFor(L, { net, run, grad, sel: { k: 1, j: 2 }, mode: 'nudges' }, c);
  assert.ok(worst.count <= MAX_SHAPES, `${worst.count}`);
  assert.ok(MAX_SHAPES < SHAPES_PER_DRAW);
});

test('a column above 64 neurons is a strip, and only the open neuron\'s strongest lines reach it', () => {
  const net = wideNet(seededNet), L = layoutOf(net), c = palette(false);
  assert.ok(net.sizes[1] > DOTS_MAX);
  assert.deepEqual(L.cols.map((x) => x.kind), ['dots', 'strip', 'dots']);
  const run = forward(net, net.examples[0].x);
  const closed = shapesFor(L, { net, run, sel: null, mode: 'values' }, c);
  const lines = (s) => [...s.groups].filter(([k]) => k.startsWith(c.blue) || k.startsWith(c.fail)).reduce((n, [, v]) => n + v.length, 0);
  assert.equal(lines(closed), 0, 'no lines to a strip until a neuron is open');
  const open = shapesFor(L, { net, run, sel: { k: 2, j: 1 }, mode: 'values' }, c);
  assert.equal(lines(open), 24, 'the 24 strongest of its 100');
});

test('the words: no arrows, no long dashes, no backend words', async () => {
  const fs = await import('node:fs');
  const dir = 'defaults/mods/captain-weights';
  for (const f of fs.readdirSync(dir).filter((x) => x.endsWith('.js'))) {
    const src = fs.readFileSync(`${dir}/${f}`, 'utf8');
    assert.ok(!/[←-⇿⟰-⟿⤀-⥿]/.test(src), `${f}: an arrow character`);
    assert.ok(!/[–—]/.test(src), `${f}: a long dash`);
    assert.ok(!/setTimeout|setInterval/.test(src), `${f}: the app has no setTimeout`);
    // The app's JavaScript has no locale data: 13002 would print without its comma. kit/num.js groups digits.
    assert.ok(!/toLocaleString|Intl\./.test(src), `${f}: use int() from kit/num.js`);
    // Off the drawing (where words scale with the zoom), three text sizes and two text colours.
    if (!['draw.js', 'canvas.js'].includes(f)) {
      for (const m of src.matchAll(/text_size\(([^)]+)\)/g)) assert.ok(['15', '12.5', '11.5'].includes(m[1]), `${f}: text size ${m[1]}`);
      assert.ok(!/text_color\(c\.grey900\)/.test(src), `${f}: a third text colour`);
    }
    for (const lit of src.match(/"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`/g) ?? []) assert.ok(!/\b(engine|daemon|core)s?\b/i.test(lit), `${f}: ${lit}`);
  }
  for (const id of [null, TINY, DIGITS]) {
    const text = (await page(id)).text();
    assert.ok(!/[←-⇿–—]/.test(text), text);
    assert.ok(!/\b(engine|daemon)\b/i.test(text));
  }
  assert.equal(factsOf(starter(TINY)), '2 in, 1 out, 13 numbers (9 weights and 4 biases)');
});
