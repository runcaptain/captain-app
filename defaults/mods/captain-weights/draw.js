// CAPTAIN: the network, drawn. Columns left to right. A neuron is a circle whose fill is its activation for the
// chosen input. A weight is a line: blue when positive, red when negative, thicker and stronger the larger it is
// (the inspector gives the sign and the number in words, so colour is never the only cue). A bias is a small
// number on its neuron.
//
// Big columns are not drawn neuron by neuron:
//   - an input column that is a picture (inputShape) is drawn as that picture, and the weights into a selected
//     first-column neuron as a second picture under it, the same shape. Never one line per pixel.
//   - any other column above DOTS_MAX neurons is a strip of cells, pooled to at most STRIP_BINS.
//
// How it stays under the app's ceiling on shapes in one draw (2,000; the render tests count them): every line and
// every cell is ONE filled polygon, shapes of one colour share one path, neighbouring cells of the same shade in a
// row merge into one rectangle, and `shapesFor` counts as it goes against MAX_SHAPES: pictures and strips first,
// then lines from the strongest down, so what is left out is the faintest lines.
import { div, PathBuilder } from "gpui-kit";
import { int } from "./kit/num.js";

export const MAX_SHAPES = 1800;
export const DOTS_MAX = 64;
const STRIP_BINS = 96, STRIP_W = 14, STRIP_CELL = 4;
const CELL = 6; // a picture's pixel, in drawing units
const HEAT_GAP = 34; // between the input picture and the weights picture under it
const PAD = 28;
const LINES_TO_STRIP = 24;

/** Where everything sits, in the drawing's own units. Independent of the input and of what is selected, so the
 *  picture does not jump when a person clicks. */
export function layoutOf(net) {
  const sizes = net.sizes;
  const kindOf = (k) => (k === 0 && net.inputShape && net.inputShape[0] * net.inputShape[1] === sizes[0] ? "image" : sizes[k] > DOTS_MAX ? "strip" : "dots");
  const most = Math.max(1, ...sizes.filter((_n, k) => kindOf(k) === "dots"));
  const r = most <= 6 ? 24 : most <= 16 ? 11 : most <= 32 ? 7 : 4;
  const pitch = r * 2 + (r >= 20 ? 30 : r >= 10 ? 8 : 3);
  const gap = r >= 20 ? 190 : 150;
  const cols = [];
  let x = PAD;
  for (let k = 0; k < sizes.length; k++) {
    const kind = kindOf(k), n = sizes[k];
    let w, h, extra = null;
    if (kind === "image") {
      const [rows, across] = net.inputShape;
      w = across * CELL; h = rows * CELL;
      // Room under the picture for the weights picture of a selected neuron, kept whether or not one is selected.
      extra = { rows, across, heatTop: h + HEAT_GAP };
      h = h * 2 + HEAT_GAP;
    } else if (kind === "strip") {
      const bins = Math.min(n, STRIP_BINS);
      w = STRIP_W; h = bins * STRIP_CELL;
      extra = { bins };
    } else { w = r * 2; h = n * pitch - (pitch - r * 2); }
    cols.push({ k, kind, n, x, w, h, ...extra });
    x += w + gap;
  }
  const height = Math.max(...cols.map((c) => c.h)) + PAD * 2 + 22; // 22: the caption under each column
  for (const c of cols) c.top = PAD + (height - PAD * 2 - 22 - c.h) / 2;
  const width = x - gap + PAD + 70; // 70: the output's names
  /** The middle of neuron j of column k (a strip's cell for a strip; null for a picture). */
  const centre = (k, j) => {
    const c = cols[k];
    if (c.kind === "dots") return { x: c.x + r, y: c.top + j * pitch + r };
    if (c.kind === "strip") return { x: c.x + STRIP_W / 2, y: c.top + (Math.floor(j * c.bins / c.n) + 0.5) * STRIP_CELL };
    return null;
  };
  return { cols, r, pitch, width, height, centre };
}

const SHADE = ["40", "80", "bf", "ff"]; // an activation's four shades
const STRENGTH = ["38", "80", "e6"]; // a weight's three strengths
const level = (m, n) => Math.min(n - 1, Math.floor(m * n));

/** Cells of a grid as rectangles, neighbours of one shade in a row merged. `shadeAt(i)` is a key or null. */
function cells(rows, across, shadeAt, x0, y0, size, add) {
  for (let y = 0; y < rows; y++) {
    let start = 0, cur = shadeAt(y * across);
    for (let x = 1; x <= across; x++) {
      const s = x < across ? shadeAt(y * across + x) : undefined;
      if (s === cur) continue;
      if (cur != null) add(cur, [[x0 + start * size, y0 + y * size], [x0 + x * size, y0 + y * size], [x0 + x * size, y0 + (y + 1) * size], [x0 + start * size, y0 + (y + 1) * size]]);
      start = x; cur = s;
    }
  }
}

/** The number a line stands for: the weight, or in "nudges" the way the cost wants it moved (the negative of its
 *  gradient). Layers the backward walk has reached show nudges whatever the mode. */
export function lineValue(s, k, j, i) {
  const nudges = s.grad && (s.mode === "nudges" || (s.walkedFrom != null && k >= s.walkedFrom));
  return nudges ? -s.grad.dW[k][j][i] : s.net.layers[k].W[j][i];
}

/** Every shape of one draw, grouped by colour: {groups: Map(colour -> [polygon]), count, dropped}. Polygons are
 *  in the drawing's units; `s` is {net, run, grad, sel, selW, mode, walkedFrom, pulse}. */
export function shapesFor(L, s, c, lineScale = 1) {
  const groups = new Map();
  let count = 0, dropped = 0;
  const add = (colour, poly) => {
    if (count >= MAX_SHAPES) { dropped++; return false; }
    count++;
    (groups.get(colour) ?? groups.set(colour, []).get(colour)).push(poly);
    return true;
  };
  const { net, run, sel } = s;
  // Pictures and strips: the input as shades, and under a picture the weights into the selected neuron.
  for (const col of L.cols) {
    const a = run?.a?.[col.k];
    if (col.kind === "image") {
      if (a) cells(col.rows, col.across, (i) => (a[i] > 0.04 ? `${c.navy}${SHADE[level(a[i], 4)]}` : null), col.x, col.top, CELL, add);
      if (sel && sel.k === 1) {
        const row = Array.from({ length: col.n }, (_v, i) => lineValue(s, 0, sel.j, i));
        const top = Math.max(1e-12, ...row.map(Math.abs));
        cells(col.rows, col.across, (i) => { const m = Math.abs(row[i]) / top; return m < 0.08 ? null : `${row[i] > 0 ? c.blue : c.fail}${STRENGTH[level(m, 3)]}`; }, col.x, col.top + col.heatTop, CELL, add);
      }
    } else if (col.kind === "strip" && a) {
      const sum = new Array(col.bins).fill(0), n = new Array(col.bins).fill(0);
      for (let j = 0; j < col.n; j++) { const b = Math.floor(j * col.bins / col.n); sum[b] += a[j]; n[b]++; }
      cells(col.bins, 1, (b) => { const v = Math.min(1, Math.max(0, sum[b] / n[b])); return v > 0.04 ? `${c.navy}${SHADE[level(v, 4)]}` : null; }, col.x, col.top, STRIP_CELL, (colour, p) => add(colour, p.map(([x, y], q) => [q === 1 || q === 2 ? col.x + STRIP_W : x, y])));
    }
  }
  // Lines. Between two columns of circles: every weight. To or from a strip: only the selected neuron's
  // strongest. Into a picture column: none (the weights picture says it).
  const lines = [];
  for (let k = 0; k < net.layers.length; k++) {
    const from = L.cols[k], to = L.cols[k + 1];
    if (from.kind === "image" || to.kind === "image") continue;
    const W = net.layers[k].W;
    const strip = from.kind === "strip" || to.kind === "strip";
    let top = 1e-12;
    for (let j = 0; j < W.length; j++) for (let i = 0; i < W[j].length; i++) top = Math.max(top, Math.abs(lineValue(s, k, j, i)));
    const mine = [];
    for (let j = 0; j < W.length; j++) for (let i = 0; i < W[j].length; i++) {
      const attached = !!sel && ((sel.k === k && sel.j === i) || (sel.k === k + 1 && sel.j === j));
      if (strip && !attached) continue;
      const v = lineValue(s, k, j, i);
      mine.push({ k, j, i, v, m: Math.abs(v) / top, attached });
    }
    if (strip) { mine.sort((p, q) => q.m - p.m); mine.length = Math.min(mine.length, LINES_TO_STRIP); }
    lines.push(...mine);
  }
  // The selected neuron's lines first, then the strongest: when the budget runs out, the faintest are left out.
  lines.sort((p, q) => (q.attached - p.attached) || (q.m - p.m));
  for (const l of lines) {
    const a = L.centre(l.k, l.i), b = L.centre(l.k + 1, l.j);
    const ra = L.cols[l.k].kind === "dots" ? L.r : STRIP_W / 2, rb = L.cols[l.k + 1].kind === "dots" ? L.r : STRIP_W / 2;
    const x1 = a.x + ra, y1 = a.y, x2 = b.x - rb, y2 = b.y;
    const pulsed = s.pulse?.weights?.has(`${l.k}:${l.j}:${l.i}`);
    const chosen = s.selW && s.selW.k === l.k && s.selW.j === l.j && s.selW.i === l.i;
    // Dimmed: while the error walks backward, the layers it has not reached; otherwise, with a neuron open, the
    // lines that are not its own.
    const dim = !pulsed && (s.walkedFrom != null ? l.k < s.walkedFrom : !!sel && !l.attached);
    const hw = ((0.6 + 3.4 * l.m) * (pulsed ? 1.6 + 0.8 * (s.pulse.phase ?? 0) : chosen ? 1.5 : 1) * lineScale) / 2;
    const len = Math.hypot(x2 - x1, y2 - y1) || 1, nx = (-(y2 - y1) / len) * hw, ny = ((x2 - x1) / len) * hw;
    const colour = pulsed ? c.warn : `${l.v >= 0 ? c.blue : c.fail}${dim ? "1f" : STRENGTH[level(l.m, 3)]}`;
    add(colour, [[x1 + nx, y1 + ny], [x2 + nx, y2 + ny], [x2 - nx, y2 - ny], [x1 - nx, y1 - ny]]);
  }
  // Nudge marks: a small triangle beside a neuron, pointing up (blue) when the cost wants it brighter and down
  // (red) when it wants it dimmer, larger for a larger wish. Shown for the columns the backward walk has reached,
  // or for all of them in "nudges". Drawn as shapes, never as arrow characters.
  if (s.grad) {
    for (const col of L.cols) {
      if (col.kind !== "dots" || col.k === 0) continue;
      if (!(s.mode === "nudges" || (s.walkedFrom != null && col.k >= s.walkedFrom))) continue;
      const wish = s.grad.dA[col.k].map((d) => -d);
      const top = Math.max(1e-12, ...wish.map(Math.abs));
      for (let j = 0; j < col.n; j++) {
        const m = Math.abs(wish[j]) / top;
        if (m < 0.03) continue;
        const p = L.centre(col.k, j), size = L.r * (0.35 + 0.55 * m), x = p.x - L.r - size * 0.9, up = wish[j] > 0;
        const tip = up ? p.y - size * 0.6 : p.y + size * 0.6, base = up ? p.y + size * 0.4 : p.y - size * 0.4;
        add(`${up ? c.blue : c.fail}${col.k === net.layers.length ? "ff" : "99"}`, [[x, tip], [x + size * 0.55, base], [x - size * 0.55, base]]);
      }
    }
  }
  return { groups, count, dropped };
}

const two = (v) => (Object.is(Number(v.toFixed(2)), -0) ? "0.00" : v.toFixed(2));

/** The canvas: the shapes under, the circles and their words over them. `o` is {id, pz, width, height, onNeuron,
 *  onWeight, extra: elements laid over the drawing} and the drawing state `s` (see shapesFor). */
export function NetworkCanvas(c, s, o) {
  const { net, run, sel } = s, pz = o.pz;
  const L = o.layout ?? layoutOf(net);
  const w = Math.max(200, o.width), h = Math.max(160, o.height);
  pz.settle(L.width, L.height, w, h);
  const z = pz.zoom, paint = globalThis.window?.paint_path;
  const { groups } = shapesFor(L, s, c, Math.min(1.6, Math.max(0.6, z)));
  const layers = [];
  if (paint) for (const [colour, polys] of groups) {
    let p = PathBuilder.fill();
    for (const poly of polys) p = p.add_polygon(poly.map(([x, y]) => pz.at(x, y)), true);
    layers.push(div().absolute().inset_0().child(paint(p.build(), colour)));
  }
  const over = [];
  const few = net.layers.reduce((t, l) => t + l.W.length * l.W[0].length, 0) <= 24; // every weight gets its number
  const L_out = net.sizes.length - 1;
  const answer = run ? (() => { const out = run.a[L_out]; if (out.length === 1) return out[0] >= 0.5 ? 0 : -1; let b = 0; for (let j = 1; j < out.length; j++) if (out[j] > out[b]) b = j; return b; })() : -1;
  for (const col of L.cols) {
    const [cx0, cy0] = pz.at(col.x, col.top);
    const name = col.k === 0 ? "Input" : col.k === L_out ? "Output" : net.sizes.length === 3 ? "Hidden" : `Hidden ${col.k}`;
    const [, capY] = pz.at(0, L.height - PAD - 14);
    over.push(div().absolute().left(cx0 - 40 * z).top(capY).w(col.w * z + 80 * z).text_center().text_size(11 * z).text_color(c.grey500)
      .child(col.kind === "dots" ? name : `${name}, ${int(col.n)} neurons`));
    if (col.kind === "image") {
      over.push(div().absolute().left(cx0).top(cy0).w(col.w * z).h(col.across ? col.rows * CELL * z : 0).border_1().border_color(c.grey300));
      const [, hy] = pz.at(0, col.top + col.heatTop);
      const open = sel && sel.k === 1;
      over.push(div().id(`${o.id}-heat`).absolute().left(cx0).top(hy).w(col.w * z).h(col.rows * CELL * z).border_1().border_color(open ? c.blue : c.rule)
        .when(!open, (e) => e.flex().items_center().justify_center().p(10 * z).text_center().text_size(10.5 * z).text_color(c.grey500).child("Click a neuron in the next column to see its weights here")));
      over.push(div().absolute().left(cx0).top(hy - 16 * z).w(col.w * z + 120 * z).text_size(10.5 * z).text_color(open ? c.navy : c.grey500)
        .child(open ? `What neuron ${sel.j + 1} responds to: its ${col.n} weights` : ""));
      continue;
    }
    if (col.kind === "strip") {
      over.push(div().id(`${o.id}-strip-${col.k}`).absolute().left(cx0).top(cy0).w(STRIP_W * z).h(col.h * z).border_1().border_color(c.grey300));
      continue;
    }
    const a = run?.a?.[col.k];
    const showBias = col.k > 0 && (few || (sel && sel.k === col.k));
    for (let j = 0; j < col.n; j++) {
      const p = L.centre(col.k, j), [x, y] = pz.at(p.x - L.r, p.y - L.r), d = L.r * 2 * z;
      const v = a ? Math.min(1, Math.max(0, a[j])) : 0;
      const on = sel && sel.k === col.k && sel.j === j;
      const pulsed = s.pulse?.neurons?.has(`${col.k}:${j}`);
      const alpha = Math.round(v * 255).toString(16).padStart(2, "0");
      const label = col.k === 0 ? net.inputNames?.[j] : col.k === L_out ? net.outputNames?.[j] : null;
      over.push(div().id(`${o.id}-n-${col.k}-${j}`).absolute().left(x).top(y).size(d).rounded_full().bg(c.surface).cursor_pointer()
        .border_1().border_color(pulsed ? c.warn : on ? c.blue : c.grey500).when(on || pulsed, (e) => e.border_2())
        .hover((st) => st.border_color(c.blue))
        .tooltip(`${label ?? `${name}, neuron ${j + 1}`}: activation ${a ? two(a[j]) : "not run"}`)
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
        .on_click((_e, cx) => o.onNeuron?.({ k: col.k, j }, cx))
        .child(div().absolute().inset_0().rounded_full().bg(`${c.navy}${alpha}`))
        .when(L.r >= 20 && !!a, (e) => e.child(div().absolute().inset_0().flex().items_center().justify_center().text_size(11.5 * z).font_medium()
          .text_color(v > 0.55 ? c.surface : c.navy).child(two(a[j])))));
      if (showBias) {
        over.push(div().id(`${o.id}-b-${col.k}-${j}`).absolute().left(x + d * 0.72).top(y + d * 0.78).text_size((L.r >= 20 ? 10.5 : 9) * z).text_color(c.grey500)
          .child(`bias ${two(net.layers[col.k - 1].b[j])}`));
      }
      if (col.k === L_out && label != null) {
        const [lx, ly] = pz.at(p.x + L.r + 8, p.y - 8);
        over.push(div().absolute().left(lx).top(ly).text_size(11.5 * z).text_color(j === answer ? c.navy : c.grey500).when(j === answer, (e) => e.font_semibold())
          .child(j === answer && net.sizes[L_out] > 1 ? `${label}, its answer` : label));
      }
      if (col.k === 0 && label != null) {
        const [lx, ly] = pz.at(p.x - L.r - 100, p.y - 8);
        over.push(div().absolute().left(lx).top(ly).w(92 * z).text_right().text_size(11.5 * z).text_color(c.grey500).child(label));
      }
    }
  }
  // On a network small enough, each weight's number sits on its line, a third of the way along, and picks it.
  if (few) {
    for (let k = 0; k < net.layers.length; k++) for (let j = 0; j < net.layers[k].W.length; j++) for (let i = 0; i < net.layers[k].W[j].length; i++) {
      const a = L.centre(k, i), b = L.centre(k + 1, j);
      const [x, y] = pz.at(a.x + L.r + (b.x - a.x - L.r * 2) * 0.3, a.y + (b.y - a.y) * 0.3);
      const on = s.selW && s.selW.k === k && s.selW.j === j && s.selW.i === i;
      over.push(div().id(`${o.id}-w-${k}-${j}-${i}`).absolute().left(x - 16 * z).top(y - 8 * z).px(3 * z).bg(c.surface2).cursor_pointer()
        .border_1().border_color(on ? c.blue : "#00000000").text_size(10 * z).text_color(on ? c.blue : c.grey900)
        .tooltip("Pick this weight")
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
        .on_click((_e, cx) => o.onWeight?.({ k, j, i }, cx))
        .child(two(lineValue(s, k, j, i))));
    }
  }
  const marks = L.cols.map((col) => ({ x: col.x, y: col.top, w: col.w, h: col.h, on: sel?.k === col.k }));
  return pz.gestures(div().id(`${o.id}-canvas`).relative().w(w).h(h).flex_none().overflow_hidden().border_1().border_color(c.rule).bg(c.surface2))
    .children(layers)
    .children(over)
    .child(pz.minimap(c, L.width, L.height, marks) ?? div())
    .child(pz.buttons(c, L.width, L.height))
    .children(o.extra ?? []);
}
