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
const { layoutOf, shapesFor, lineQuad, tint, placeNumbers, MIN_LINE_PX, MAX_SHAPES, DOTS_MAX } = await import('../../defaults/mods/captain-weights/draw.js');
const { forward, backward, seededNet } = await import('../../defaults/mods/captain-weights/net.js');
const { starter: tinyStarter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { DIGITS, digitsNet, digitsRoutes } = await import('./fixtures/captain-weights/digits.mjs');
const starter = (id) => (id === DIGITS ? digitsNet() : tinyStarter(id));
const { palette } = await import('../../defaults/mods/captain-weights/kit/tokens.js');

// Captain's services know no networks here: only the starters show.
const served = { '/api/weights/models': { ok: false } };
weightsTransport(async (path) => served[path] ?? digitsRoutes(path) ?? { ok: false, error: 'Not Found' });

async function page(model) {
  localReset();
  globalThis.__props = model ? { model } : {};
  const h = harness(Weights);
  await h.start();
  return h;
}
const harnessTree = (h) => { const out = []; (function w(e) { if (!e || typeof e !== 'object') return; out.push(e); for (const k of e.kids ?? []) w(k); })(h.mod.render(h.cx)); return out; };
const paths = (h) => { const out = []; (function w(e) { if (!e || typeof e !== 'object') return; if (e.kind === 'path') out.push(e); for (const k of e.kids ?? []) w(k); })(h.mod.render(h.cx)); return out; };

test('with nothing open and no services, the tiny network is offered and the digit reader is said to need them', async () => {
  const h = await page(null);
  const text = h.text();
  assert.ok(text.includes('Tiny network'), text);
  assert.equal(h.byIdPrefix('wt-open-btn-').length, 1, 'the page ships one network: the services make the digit reader');
  assert.ok(text.includes("The digit reader (784 inputs, 13,002 numbers) needs Captain's services, which are not answering."), text);
  await h.click('wt-open-btn-starter:tiny');
  assert.equal(h.byIdPrefix('wt-title')[0].kids.join(''), 'Tiny network');
  // The way back is the breadcrumb: "Weights" returns to the list.
  await h.click('wt-back');
  assert.equal(h.byIdPrefix('wt-open-btn-').length, 1);
  assert.equal(h.byIdPrefix('wt-title').length, 0);
});

test('it is a page in the window\'s main slot beside the terminal, never a dialog, and another screen can open a network in it', async () => {
  const fs = await import('node:fs');
  const layout = JSON.parse(fs.readFileSync('defaults/layouts/captain.json', 'utf8'));
  const find = (n, id) => (n?.id === id ? n : (n?.children ?? []).map((k) => find(k, id)).find(Boolean));
  assert.equal(find(layout, 'sections').children.find((k) => k.id === 'weights')?.content.mod, 'captain-weights');
  assert.ok(!('overlay' in JSON.parse(fs.readFileSync('defaults/mods/captain-weights/mod.json', 'utf8'))), 'no dialog size: it is not one');
  const { staticEntries, ITEMS } = await import('../../defaults/mods/captain-nav/main.js');
  const entry = staticEntries(ITEMS).find((e) => e.title === "Open a network's weights");
  assert.deepEqual([entry.overlay.mod, entry.overlay.props.section], ['captain-go', 'weights'], 'the palette selects the page');
  assert.deepEqual(ITEMS.map((i) => i.id).slice(3, 6), ['runs', 'weights', 'deployments'], 'in the sidebar, between Runs and Deployments');
  for (const f of fs.readdirSync('defaults/mods/captain-weights').filter((x) => x.endsWith('.js'))) assert.ok(!/closeOverlay|kind:\s*"dialog"/.test(fs.readFileSync(`defaults/mods/captain-weights/${f}`, 'utf8')), f);
  // `cst app open weights:<model>` and links set shared "captain.open": the page opens that network, once.
  const h = await page(null);
  globalThis.__shared = { ...(globalThis.__shared ?? {}), 'captain.open': { section: 'weights', model: TINY, at: 5 } };
  await h.layoutLists({});
  assert.equal(h.mod.model, TINY);
  await h.click('wt-back');
  await h.layoutLists({});
  assert.equal(h.mod.model, null, 'a request is handled once');
  globalThis.__shared['captain.open'] = { section: 'deployments', open: 'x', at: 6 };
  await h.layoutLists({});
  assert.equal(h.mod.model, null, 'a request for another screen is not this page\'s');
  delete globalThis.__shared['captain.open'];
});

test('at the width left beside the terminal the neuron panel shows only while a neuron is open; wide, it is always there', async () => {
  const h = await page(TINY);
  const canvasW = () => h.byIdPrefix('wt-canvas')[0].call('w')[0];
  assert.equal(h.mod.width, 820, 'the width it assumes until the block says its own');
  assert.equal(h.byIdPrefix('wt-side').filter((e) => e.call('id')[0] === 'wt-side').length, 0);
  const full = canvasW();
  assert.equal(full, 780);
  await h.click('wt-n-1-1');
  assert.equal(h.byIdPrefix('wt-side').filter((e) => e.call('id')[0] === 'wt-side').length, 1);
  assert.ok(canvasW() < full && canvasW() >= 440, `${canvasW()}`);
  await h.click('wt-side-close');
  assert.equal(h.mod.sel, null);
  assert.equal(canvasW(), full);
  // The cost and what was done are under the drawing at every width.
  assert.ok(h.byIdPrefix('wt-cost').length > 0 && h.byIdPrefix('wt-timeline').length > 0);
  h.mod.width = 1300; h.mod.height = 860;
  h.mod.render(h.cx);
  const wide = harnessTree(h);
  assert.ok(wide.some((e) => e.call?.('id')?.[0] === 'wt-side'), 'wide: the panel is there with nothing open');
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
  assert.ok(h.text().includes('What neuron 5 responds to'));
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

test('a line is never thinner than half a pixel on screen, however far out the view is', async () => {
  const { PanZoom } = await import('../../defaults/mods/captain-weights/canvas.js');
  const width = (item, zoom) => { const pz = new PanZoom('t'); pz.zoom = zoom; pz.pan = { x: 7, y: 3 }; const q = lineQuad(item, pz); return Math.hypot(q[0][0] - q[3][0], q[0][1] - q[3][1]); };
  const faint = { line: [0, 0, 100, 40], w: 0.6 }, strong = { line: [0, 0, 100, 40], w: 4 };
  for (const zoom of [0.3, 0.5, 1, 2.5]) {
    assert.ok(width(faint, zoom) >= MIN_LINE_PX - 1e-9, `the faintest line at zoom ${zoom}: ${width(faint, zoom)} px`);
    assert.ok(width(strong, zoom) > width(faint, zoom), 'a larger weight is still thicker');
  }
  // The width follows the zoom gently, never with its square: at 0.3 a strong line keeps 0.6 of its width.
  assert.ok(Math.abs(width(strong, 0.3) - 4 * 0.6) < 1e-9, `${width(strong, 0.3)}`);
  assert.ok(Math.abs(width(strong, 1) - 4) < 1e-9);
  assert.ok(Math.abs(width(strong, 2.5) - 4 * 1.6) < 1e-9);
  // And the lines the drawing really makes: the digit network's faintest, zoomed all the way out.
  const net = starter(DIGITS), s = shapesFor(layoutOf(net), { net, run: forward(net, net.examples[0].x), sel: null, mode: 'values' }, palette(false));
  const lines = [...s.groups.values()].flat().filter((x) => x.line);
  assert.equal(lines.length, 416);
  assert.ok(Math.min(...lines.map((l) => width(l, 0.3))) >= MIN_LINE_PX - 1e-9);
});

test('a colour is only given a strength when it is six hex digits', () => {
  assert.equal(tint('#0043ca', '80'), '#0043ca80');
  assert.equal(tint('#fff', '40'), '#ffffff40');
  assert.equal(tint('#0043ca26', '80'), '#0043ca26', 'one that already has a strength is left alone');
  assert.equal(tint('red', '80'), 'red');
  for (const dark of [true, false]) for (const k of ['navy', 'blue', 'fail', 'warn']) assert.match(tint(palette(dark)[k], '80'), /^#[0-9a-f]{8}$/i, `${k} in ${dark ? 'dark' : 'light'}`);
});

test('the shapes are built once and kept: panning and zooming only map points', async () => {
  const h = await page(DIGITS);
  const built = () => h.mod.keep.built;
  const n0 = built();
  h.mod.pz.pan = { x: 40, y: -12 }; h.mod.pz.touched = true;
  h.mod.render(h.cx);
  h.mod.pz.zoomBy(1.4, 100, 100);
  h.mod.render(h.cx);
  assert.equal(built(), n0, 'no rebuild for a pan or a zoom');
  await h.click('wt-n-2-3');
  assert.equal(built(), n0 + 1, 'a new selection rebuilds once');
  h.mod.render(h.cx);
  assert.equal(built(), n0 + 1);
  await h.click('wt-in-4');
  assert.equal(built(), n0 + 2, 'a new input rebuilds once');
  await h.click('wt-mode-nudges');
  assert.equal(built(), n0 + 3, 'a new mode rebuilds once');
});

test('a network too big to read whole opens at 100% on the open neuron; one that fits is fitted', async () => {
  const { PanZoom } = await import('../../defaults/mods/captain-weights/canvas.js');
  // Fits: fitted whole, enlarged a little, centred.
  let pz = new PanZoom('t');
  pz.settle(400, 300, 800, 500);
  assert.ok(pz.zoom > 1 && Math.abs(pz.pan.x - (800 - 400 * pz.zoom) / 2) < 1e-9);
  // Too big, and nothing open: fitted whole all the same, so the first look is the whole network.
  pz = new PanZoom('t');
  pz.settle(1400, 600, 600, 400);
  assert.ok(pz.zoom < 0.8 && pz.at(1400, 0)[0] <= 600 && pz.at(0, 0)[0] >= 0);
  // Too big, with a neuron open: 100%, with the neuron in the middle.
  pz = new PanZoom('t');
  pz.settle(1400, 600, 600, 400, { x: 900, y: 300 });
  assert.equal(pz.zoom, 1);
  assert.deepEqual(pz.at(900, 300), [300, 200], 'the open neuron is in the middle of the canvas');
  // Never past the drawing's own edge: a focus at the far left starts at the left margin.
  pz = new PanZoom('t');
  pz.settle(1400, 600, 600, 400, { x: 10, y: 300 });
  assert.equal(pz.pan.x, 24);
  // The person's own pan is kept when the canvas changes size, and Fit shows it all again.
  pz.touched = true; pz.pan = { x: -333, y: 5 };
  pz.settle(1400, 600, 500, 400, { x: 900, y: 300 });
  assert.equal(pz.pan.x, -333);
  // On the page: the digit network beside an open neuron's panel is too wide for what is left.
  const h = await page(DIGITS);
  await h.click('wt-n-3-9');
  assert.equal(h.mod.pz.zoom, 1, 'opened at 100%');
  const L = layoutOf(starter(DIGITS)), p = L.centre(3, 9), cw = h.byIdPrefix('wt-canvas')[0].call('w')[0];
  const [sx] = h.mod.pz.at(p.x, p.y);
  assert.ok(sx > 0 && sx < cw, `the open neuron is on screen: ${sx} of ${cw}`);
  assert.ok(h.byIdPrefix('wt-minimap').length === 1, 'the overview map says there is more');
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
    if (!['draw.js', 'canvas.js', 'partsdraw.js'].includes(f)) {
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

test('an input\'s name is in words, counting from 1', async () => {
  const { inputName } = await import('../../defaults/mods/captain-weights/main.js');
  const pic = { inputShape: [28, 28] }, plain = {};
  assert.equal(inputName(pic, { name: 'practice 0: 9' }, 0), 'Picture 1: a 9');
  assert.equal(inputName(pic, { name: 'example 3: 8' }, 2), 'Picture 3: an 8');
  assert.equal(inputName(pic, { name: 'example 4' }, 3), 'Picture 4');
  assert.equal(inputName(plain, { name: 'practice 11' }, 11), 'Input 12');
  assert.equal(inputName(plain, { name: 'Both on' }, 3), 'Both on', 'a name already in words is kept');
  assert.equal(inputName(plain, {}, 0), 'Input 1');
});

test('no two numbers on the tiny network\'s lines share a spot', async () => {
  const boxesTouch = (p, q) => Math.abs(p.x - q.x) < 36 && Math.abs(p.y - q.y) < 15;
  for (const net of [starter(TINY), { ...starter(TINY), layers: [{ W: [[-5.62, -10.63], [4.92, -5.14], [-13.24, -7.99]], b: [10.18, 3.85, 7.81] }, { W: [[18.5, -13.43, -16.33]], b: [2.18] }] }]) {
    const L = layoutOf(net), all = [];
    net.layers.forEach((l, k) => l.W.forEach((row, j) => row.forEach((v, i) => all.push({ k, j, i, v }))));
    all.sort((p, q) => Math.abs(q.v) - Math.abs(p.v));
    const placed = placeNumbers(L, all), shown = placed.filter((p) => !p.hidden);
    assert.equal(placed.length, 9);
    for (let a = 0; a < shown.length; a++) for (let b = a + 1; b < shown.length; b++) assert.ok(!boxesTouch(shown[a], shown[b]), `${shown[a].v} and ${shown[b].v} overlap`);
    assert.ok(shown.length >= 7, `${shown.length} of 9 numbers are shown without a hover`);
    // The strongest weight keeps its place a third of the way along: it is the fainter one that moves.
    const a = L.centre(placed[0].k, placed[0].i), b = L.centre(placed[0].k + 1, placed[0].j);
    assert.ok(Math.abs(placed[0].y - (a.y + (b.y - a.y) / 3)) < 1e-9);
  }
  // On the page every weight still has its number to pick, shown or waiting for the pointer.
  const h = await page(TINY);
  assert.equal(h.byIdPrefix('wt-w-').length, 9);
});
