// CAPTAIN: a model of parts, drawn. A trained model is too wide to draw neuron by neuron (a column 48 wide is a
// blot), so each of its parts is one block, left to right as the data flows: the embedding tables stacked in a
// column and sized by their rows, the projection, each attention block with its heads, each feed-forward block,
// the heads at the end. A block says its name, its shape and how many numbers it holds, and opens on a click.
// Also here: one tree of a ranker as boxes and lines, and a matrix as a picture of shaded cells.
//
// The blocks and the words are plain elements. Only the joining lines and a picture's cells are shapes, and a
// picture is capped (HEAT_MAX cells a side), so a draw stays far under the app's ceiling of 2,000 shapes.
import { div, PathBuilder } from "gpui-kit";
import { int } from "./kit/num.js";
import { tint, lineQuad, cells, STRENGTH } from "./draw.js";

const PAD = 28, GAP = 46;
const BLOCK_W = 150, NORM_W = 78, HEAD_H = 24;
const CAPTION = 22; // the room over a block for its group's name
export const HEAT_MAX = 32; // the most cells a side a picture draws: 32 by 32 is at most 1,024 shapes

/** "12 by 8". */
export const shapeWords = (shape) => (Array.isArray(shape) && shape.length === 2 ? `${int(shape[0])} by ${int(shape[1])}` : "");
/** "96 numbers". */
export const countWords = (n) => `${int(n)} ${n === 1 ? "number" : "numbers"}`;

/** Where every block sits, in the drawing's own units: {boxes: [{part, x, y, w, h}], lines: [{from, to}],
 *  captions: [{text, x, y, w}], width, height}. Embedding tables share the first column; every other part has its
 *  own, in the order the model lists them; heads at the end share the last. A model with a candidate side (the
 *  parts that read the item being scored) has that side as a second row under the first. */
export function partsLayout(model) {
  const all = model.parts ?? [];
  const sides = [all.filter((p) => p.side !== "candidate"), all.filter((p) => p.side === "candidate")].filter((g) => g.length);
  const boxes = [], at = new Map(), captions = [];
  let top = PAD, width = PAD * 2;
  sides.forEach((parts, n) => {
    const tables = parts.filter((p) => p.kind === "embedding"), rest = parts.filter((p) => p.kind !== "embedding");
    const most = Math.max(1, ...tables.map((p) => p.shape?.[0] ?? 1));
    // A table's height follows its rows, between a block that still holds its three lines and one twice that.
    const heightOf = (p) => (p.kind === "embedding" ? Math.round(58 + 62 * Math.sqrt((p.shape?.[0] ?? 1) / most))
      : p.kind === "attention" ? 72 + Math.max(1, p.heads ?? 1) * (HEAD_H + 4) : p.kind === "norm" ? 58 : 64);
    const total = (list) => list.reduce((h, p) => h + heightOf(p), 0) + Math.max(0, list.length - 1) * 14;
    // Heads at the end sit one under another, like the tables at the start.
    const heads = []; while (rest.length && rest.at(-1).kind === "head") heads.unshift(rest.pop());
    const inner = Math.max(total(tables), total(heads), ...rest.map(heightOf), 64);
    const mid = top + CAPTION + inner / 2;
    let x = PAD;
    const stack = (list) => {
      let y = mid - total(list) / 2;
      for (const p of list) { const b = { part: p, x, y, w: BLOCK_W, h: heightOf(p) }; boxes.push(b); at.set(p.id, b); y += b.h + 14; }
      if (list.length) x += BLOCK_W + GAP;
    };
    stack(tables);
    for (const p of rest) {
      const w = p.kind === "norm" ? NORM_W : BLOCK_W, h = heightOf(p);
      const b = { part: p, x, y: mid - h / 2, w, h };
      boxes.push(b); at.set(p.id, b);
      x += w + GAP;
    }
    stack(heads);
    // Names over the columns: the tables, each layer's blocks, the heads.
    const side = sides.length > 1 ? (n === 0 ? "Session side: " : "Candidate side: ") : "";
    if (tables.length) captions.push({ text: `${side}Lookup tables`, x: at.get(tables[0].id).x, y: top, w: BLOCK_W + GAP });
    const groups = new Map();
    for (const p of rest) if (p.block != null) { const b = at.get(p.id), g = groups.get(p.block) ?? { x0: b.x, x1: b.x + b.w }; g.x0 = Math.min(g.x0, b.x); g.x1 = Math.max(g.x1, b.x + b.w); groups.set(p.block, g); }
    for (const [k, g] of groups) captions.push({ text: `Layer ${int(k)}`, x: g.x0, y: top, w: g.x1 - g.x0 });
    if (heads.length) captions.push({ text: heads.length === 1 ? "Head" : "Heads", x: at.get(heads[0].id).x, y: top, w: BLOCK_W });
    width = Math.max(width, x - GAP + PAD);
    top += CAPTION + inner + PAD;
  });
  // A line for each part a block reads from.
  const lines = [];
  for (const b of boxes) for (const id of b.part.feeds_from ?? []) if (at.has(id)) lines.push({ from: at.get(id), to: b });
  return { boxes, lines, captions, width, height: top };
}

/** The joining lines as one path. */
function joins(lines, pz, c) {
  const paint = globalThis.window?.paint_path;
  if (!paint || !lines.length) return null;
  let p = PathBuilder.fill();
  for (const l of lines) p = p.add_polygon(lineQuad({ line: l, w: 1.2 }, pz), true);
  return div().absolute().inset_0().child(paint(p.build(), c.grey500));
}

/** The diagram. `o` is {id, pz, layout, width, height, sel: the open part's id, head: the open head, onPart(id,
 *  head, cx)}. */
export function PartsCanvas(c, model, o) {
  const pz = o.pz, L = o.layout ?? partsLayout(model);
  const w = Math.max(200, o.width), h = Math.max(160, o.height);
  const open = L.boxes.find((b) => b.part.id === o.sel);
  pz.settle(L.width, L.height, w, h, open ? { x: open.x + open.w / 2, y: open.y + open.h / 2 } : { x: 0, y: L.height / 2 });
  const z = pz.zoom;
  const over = [];
  for (const cap of L.captions) {
    const [x, y] = pz.at(cap.x, cap.y);
    over.push(div().absolute().left(x).top(y).w(cap.w * z).text_size(10.5 * z).text_color(c.grey500).child(cap.text));
  }
  for (const b of L.boxes) {
    const p = b.part, on = p.id === o.sel, [x, y] = pz.at(b.x, b.y);
    const small = p.kind === "norm";
    let box = div().id(`${o.id}-part-${p.id}`).absolute().left(x).top(y).w(b.w * z).h(b.h * z).overflow_hidden().cursor_pointer()
      .bg(on ? c.blueLight : c.surface).border_1().border_color(on ? c.blue : c.grey500).when(on, (e) => e.border_2())
      .hover((s) => s.border_color(c.blue))
      .tooltip(`Open ${p.name}: ${shapeWords(p.shape)}, ${countWords(p.parameters)}`)
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
      .on_click((_e, cx) => o.onPart?.(p.id, null, cx))
      .child(div().px(7 * z).pt(6 * z).text_size(11.5 * z).font_medium().text_color(on ? c.blue : c.navy).truncate().child(p.name))
      .child(div().px(7 * z).text_size(10.5 * z).text_color(c.grey500).truncate().child(shapeWords(p.shape)))
      .child(div().px(7 * z).text_size(10.5 * z).text_color(c.grey500).truncate().child(small ? int(p.parameters) : countWords(p.parameters)));
    // An attention block shows its heads: each opens the block on that head.
    if (p.kind === "attention") {
      for (let n = 0; n < Math.max(1, p.heads ?? 1); n++) {
        const mine = on && (o.head ?? 0) === n;
        box = box.child(div().id(`${o.id}-part-${p.id}-head-${n}`).absolute().left(7 * z).top((66 + n * (HEAD_H + 4)) * z).w((b.w - 16) * z).h(HEAD_H * z).flex().items_center().px(6 * z)
          .border_1().border_color(mine ? c.blue : c.grey300).bg(c.surface2).text_size(10.5 * z).text_color(mine ? c.blue : c.navy)
          .hover((s) => s.border_color(c.blue))
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
          .on_click((_e, cx) => { cx.stop_propagation?.(); o.onPart?.(p.id, n, cx); })
          .child(`Head ${int(n + 1)}`));
      }
    }
    over.push(box);
  }
  const lines = L.lines.map((l) => [l.from.x + l.from.w, l.from.y + l.from.h / 2, l.to.x, l.to.y + l.to.h / 2]);
  const marks = L.boxes.map((b) => ({ x: b.x, y: b.y, w: b.w, h: b.h, on: b.part.id === o.sel }));
  return pz.gestures(div().id(`${o.id}-canvas`).relative().w(w).h(h).flex_none().overflow_hidden().border_1().border_color(c.rule).bg(c.surface2))
    .child(joins(lines, pz, c) ?? div())
    .children(over)
    .child(pz.minimap(c, L.width, L.height, marks) ?? div())
    .child(pz.buttons(c, L.width, L.height));
}

// ---- One tree of a ranker ---------------------------------------------------------------------------------------

const NODE_W = 172, NODE_H = 40, LEAF_W = 84, LEAF_H = 30, LEVEL = 86, SIDE = 18;

/** A threshold as a person would write it: 12.5, 3, 0.0312. */
export function thresholdWords(v) {
  if (!Number.isFinite(v)) return "";
  const a = Math.abs(v);
  return String(Number(a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(2) : v.toPrecision(3)));
}
/** A split as a sentence: "price above 12.5". A split on a category asks whether the value is outside a set. */
export const splitWords = (n) => (n.categorical ? `${n.input} outside the set it lists` : `${n.input} above ${thresholdWords(n.threshold)}`);
/** What a leaf adds to the score: "adds +0.21". */
export function leafWords(v) {
  const a = Math.abs(v);
  return `adds ${v < 0 ? "-" : "+"}${a >= 0.1 || a === 0 ? a.toFixed(2) : String(Number(a.toPrecision(2)))}`;
}

/** Where a tree's boxes sit: the root on top, a split's "no" child to the left and its "yes" child to the right
 *  (a larger value goes right, as on a number line), leaves evenly spaced. {boxes: [{node, x, y, w, h}], lines: [{from, to, says}], width, height}. */
export function treeLayout(nodes) {
  const by = new Map((nodes ?? []).map((n) => [n.id, n]));
  const kids = new Set(); for (const n of by.values()) { if (n.yes != null) kids.add(n.yes); if (n.no != null) kids.add(n.no); }
  const root = [...by.values()].find((n) => !kids.has(n.id));
  const boxes = [], lines = [], seen = new Set();
  let nextX = PAD, deepest = 0;
  const place = (n, depth) => {
    if (!n || seen.has(n.id)) return null;
    seen.add(n.id);
    deepest = Math.max(deepest, depth);
    const leaf = n.leaf != null || (n.yes == null && n.no == null);
    const w = leaf ? LEAF_W : NODE_W, h = leaf ? LEAF_H : NODE_H, y = PAD + depth * LEVEL;
    if (leaf) { const b = { node: n, leaf, x: nextX, y, w, h }; nextX += w + SIDE; boxes.push(b); return b; }
    const a = place(by.get(n.no), depth + 1), d = place(by.get(n.yes), depth + 1);
    const under = [a, d].filter(Boolean);
    const cx = under.length ? under.reduce((s, b) => s + b.x + b.w / 2, 0) / under.length : nextX + w / 2;
    const b = { node: n, leaf, x: cx - w / 2, y, w, h };
    if (!under.length) nextX += w + SIDE;
    boxes.push(b);
    if (a) lines.push({ from: b, to: a, says: "no" });
    if (d) lines.push({ from: b, to: d, says: "yes" });
    return b;
  };
  place(root, 0);
  // A wide split over two narrow leaves can reach past the left edge: move everything right to the margin.
  const left = Math.min(PAD, ...boxes.map((b) => b.x));
  if (left < PAD) for (const b of boxes) b.x += PAD - left;
  const width = Math.max(PAD * 2, ...boxes.map((b) => b.x + b.w + PAD));
  return { boxes, lines, width, height: PAD * 2 + deepest * LEVEL + NODE_H };
}

/** One tree, drawn. `o` is {id, pz, width, height}. */
export function TreeCanvas(c, tree, o) {
  const pz = o.pz, L = treeLayout(tree?.nodes);
  const w = Math.max(200, o.width), h = Math.max(140, o.height);
  pz.settle(L.width, L.height, w, h, { x: L.width / 2, y: 0 });
  const z = pz.zoom, over = [];
  for (const b of L.boxes) {
    const [x, y] = pz.at(b.x, b.y);
    over.push(div().id(`${o.id}-node-${b.node.id}`).absolute().left(x).top(y).w(b.w * z).h(b.h * z).flex().items_center().justify_center().px(4 * z).overflow_hidden()
      .bg(c.surface).border_1().border_color(b.leaf ? c.grey300 : c.grey500).text_size((b.leaf ? 10.5 : 11.5) * z).text_color(c.navy)
      .tooltip(b.leaf ? "A leaf: what this path adds to the score" : `A split, which is one question: ${splitWords(b.node)}?`)
      .child(div().truncate().child(b.leaf ? leafWords(b.node.leaf ?? 0) : splitWords(b.node))));
  }
  for (const l of L.lines) {
    const [x, y] = pz.at((l.from.x + l.from.w / 2 + l.to.x + l.to.w / 2) / 2 - 14, (l.from.y + l.from.h + l.to.y) / 2 - 7);
    over.push(div().absolute().left(x).top(y).w(28 * z).text_center().bg(c.surface2).text_size(10 * z).text_color(c.grey500).child(l.says));
  }
  const lines = L.lines.map((l) => [l.from.x + l.from.w / 2, l.from.y + l.from.h, l.to.x + l.to.w / 2, l.to.y]);
  return pz.gestures(div().id(`${o.id}-canvas`).relative().w(w).h(h).flex_none().overflow_hidden().border_1().border_color(c.rule).bg(c.surface2))
    .child(joins(lines, pz, c) ?? div())
    .children(over)
    .child(pz.minimap(c, L.width, L.height, L.boxes.map((b) => ({ x: b.x, y: b.y, w: b.w, h: b.h, on: false }))) ?? div())
    .child(pz.buttons(c, L.width, L.height));
}

// ---- A matrix as a picture --------------------------------------------------------------------------------------

/** A grid averaged down to at most `max` cells a side: {values, rows, cols, by: [rows per cell, columns per
 *  cell], reduced}. A grid that already fits comes back as it is. */
export function reduceGrid(values, max = HEAT_MAX) {
  const rows = values?.length ?? 0, cols = values?.[0]?.length ?? 0;
  const br = Math.max(1, Math.ceil(rows / max)), bc = Math.max(1, Math.ceil(cols / max));
  if (br === 1 && bc === 1) return { values: values ?? [], rows, cols, by: [1, 1], reduced: false };
  const R = Math.ceil(rows / br), C = Math.ceil(cols / bc);
  const out = Array.from({ length: R }, (_r, y) => Array.from({ length: C }, (_c, x) => {
    let sum = 0, n = 0;
    for (let r = y * br; r < Math.min(rows, (y + 1) * br); r++) for (let q = x * bc; q < Math.min(cols, (x + 1) * bc); q++) { sum += values[r][q]; n++; }
    return n ? sum / n : 0;
  }));
  return { values: out, rows: R, cols: C, by: [br, bc], reduced: true };
}

/** A matrix as shaded cells: blue for positive, red for negative, stronger for larger, nothing for near zero.
 *  `grid` is from reduceGrid. Drawn into a box at most `width` wide and `maxH` tall. `mark` is a row to outline. */
export function Heat(c, id, grid, { width, maxH = 220, mark = null } = {}) {
  const { values, rows, cols } = grid;
  if (!rows || !cols) return div().id(id);
  const size = Math.max(2, Math.min(18, Math.floor(Math.min(width / cols, maxH / rows))));
  const w = cols * size, h = rows * size;
  const paint = globalThis.window?.paint_path;
  let top = 1e-12;
  for (const row of values) for (const v of row) top = Math.max(top, Math.abs(v));
  const groups = new Map();
  const shade = (i) => { const v = values[Math.floor(i / cols)][i % cols], m = Math.abs(v) / top; return m < 0.08 ? null : tint(v > 0 ? c.blue : c.fail, STRENGTH[Math.min(2, Math.floor(m * 3))]); };
  cells(rows, cols, shade, 0, 0, size, (colour, poly) => (groups.get(colour) ?? groups.set(colour, []).get(colour)).push(poly));
  let box = div().id(id).relative().w(w).h(h).flex_none().border_1().border_color(c.grey300).bg(c.surface);
  if (paint) for (const [colour, polys] of groups) {
    let p = PathBuilder.fill();
    for (const poly of polys) p = p.add_polygon(poly, true);
    box = box.child(div().absolute().inset_0().child(paint(p.build(), colour)));
  }
  if (mark != null && mark >= 0 && mark < rows) box = box.child(div().id(`${id}-mark`).absolute().left(0).top(mark * size).w(w).h(size).border_1().border_color(c.navy));
  return box;
}
