// captain-ui: "Where this came from", drawn as a graph (Edgar, 2026-11-12: "a React Flow-like setup where it shows
// you the whole lineage of the model or where the data went"). Boxes are sources, tables, model versions, models,
// evaluations and deployments: rounded, with an icon in the kind's colour, a title and the kind in words. A smooth
// curve runs from the right edge of what was read to the left edge of what was made from it, with a dot where it
// joins each box and an arrowhead at the far end. The canvas has a dotted grid. No library: the layout is written
// here and everything is drawn with the app's own path painter.
//
//   const lin = new LineageGraph({ id: "lin", onOpen: (node, cx) => true | false, onFocus, onColumn });
//   lin.set({ nodes: [{id, kind, label, version?, columns?}], edges: [{from, to}] }, focusId);
//   lin.render(c, { width, height })       // keep the instance on the view
//
// Layout: layered, left to right. A box's column is how many steps it is from the focused box (upstream to the
// left, downstream to the right), or its longest path from a source when nothing is focused. Within a column the
// boxes are ordered by where their neighbours sit (two barycenter sweeps), so arrows cross as little as a simple
// layout manages. The whole chain is shown, fitted to the canvas.
//
// Hovering a box lights everything upstream and downstream of it and dims the rest. Clicking a box opens it
// (onOpen), or shows its details when it has no page. Drag the background (or scroll) to pan, hold Command and
// scroll (or use + and -) to zoom, Fit to see it all; a box can be dragged aside.
//
// Edges may use {from, to} or {src, dst}. Column edges: lin.setColumns([{from: {node, column}, to: {node, column}}]).
import { div, svg, PathBuilder } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { MONO } from "./tokens.js";
import { icon } from "./icons.js";
import { BRANDS } from "./brands.js";

const NODE_W = 204;
const NODE_H = 52;
const GAP_X = 84; // room for an arrow to bend and for its head
const GAP_Y = 16;
const PAD = 20;
const ARROW = 9; // the arrowhead's length, in layout units
const DOT = 4; // the radius of the dot where a connector joins a box
const GRID = 22; // the spacing of the canvas's dotted grid
const RADIUS = 8; // a box's corners
const ALL = 99; // every step, each way: the whole chain

// [the word for the kind, its icon, the palette colour of its edge stripe].
export const KINDS = {
  table: ["Database table", "table", "grey500"], connection: ["Source", "server", "grey500"], dataset: ["Table", "database", "blue"],
  pipeline: ["Model version", "workflow", "navy"], model: ["Model", "boxes", "navy"], "model-unregistered": ["Model, not in a registry", "boxes", "grey500"],
  architecture: ["Model family", "layers", "grey500"], backtest: ["Evaluation", "flask-conical", "good"],
  run: ["Evaluation run", "flask-conical", "good"], handoff: ["Launch review", "send", "warn"], label: ["Label", "tag", "grey500"],
  notebook: ["Notebook", "file-text", "grey500"], registry: ["Registry", "cloud", "grey500"], environment: ["Deployment", "rocket", "warn"],
  demo: ["Demo", "rocket", "warn"],
  // MLflow, read through the bridge (kit/mlflow.js withMlflow): drawn with its own mark.
  "mlflow-run": ["MLflow run", "brand:mlflow"], "mlflow-model": ["MLflow model", "brand:mlflow"],
};

const ends = (e) => [e.from ?? e.src, e.to ?? e.dst];

/** Columns of node ids around the focus: negative levels upstream, positive downstream, within `up` and `down`. */
export function levels(graph, focus, up, down) {
  const ups = new Map(), downs = new Map();
  for (const e of graph.edges ?? []) {
    const [a, b] = ends(e);
    if (!downs.has(a)) downs.set(a, []);
    if (!ups.has(b)) ups.set(b, []);
    downs.get(a).push(b);
    ups.get(b).push(a);
  }
  const level = new Map([[focus, 0]]);
  const walk = (next, sign, max) => {
    let frontier = [focus];
    for (let d = 1; d <= max && frontier.length; d++) {
      const nxt = [];
      for (const id of frontier) for (const o of next.get(id) ?? []) if (!level.has(o)) { level.set(o, sign * d); nxt.push(o); }
      frontier = nxt;
    }
  };
  walk(ups, -1, up);
  walk(downs, 1, down);
  // MLflow feeds the pipelines downstream from the side (a model version, and the run that made it): neither up nor
  // down from the focus, so place each one column before what it feeds. Other side inputs stay out, so the graph
  // doesn't fill with everything that touches a downstream node.
  const kind = new Map((graph.nodes ?? []).map((n) => [n.id, n.kind]));
  for (let pass = 0; pass < 2; pass++) {
    for (const e of graph.edges ?? []) {
      const [a, b] = ends(e);
      if (level.has(a) || !level.has(b) || !String(kind.get(a) ?? "").startsWith("mlflow-")) continue;
      level.set(a, level.get(b) - 1);
    }
  }
  return level;
}

/** With no focus, the whole graph: each node's column is its longest path from a node with nothing upstream. */
export function ranks(graph) {
  const ids = (graph.nodes ?? []).map((n) => n.id);
  const ins = new Map(ids.map((id) => [id, []]));
  for (const e of graph.edges ?? []) { const [a, b] = ends(e); if (ins.has(b) && ins.has(a)) ins.get(b).push(a); }
  const rank = new Map();
  const visit = (id, seen) => {
    if (rank.has(id)) return rank.get(id);
    if (seen.has(id)) return 0; // a cycle: break it here
    seen.add(id);
    const r = Math.max(-1, ...ins.get(id).map((p) => visit(p, seen))) + 1;
    seen.delete(id);
    rank.set(id, r);
    return r;
  };
  for (const id of ids) visit(id, new Set());
  return rank;
}

/** Order each column so a box sits near the boxes it is joined to: two sweeps, left to right then right to left,
 *  each placing a box at the average position of its neighbours in the column just passed. `cols` is a list of
 *  columns of ids, in order; `edges` is [[from, to]]. Returns the columns reordered. */
export function untangle(cols, edges) {
  const out = cols.map((c) => [...c]);
  const near = new Map();
  for (const [a, b] of edges) { (near.get(a) ?? near.set(a, []).get(a)).push(b); (near.get(b) ?? near.set(b, []).get(b)).push(a); }
  const sweep = (order) => {
    for (let k = 1; k < order.length; k++) {
      const prev = new Map(out[order[k - 1]].map((id, i) => [id, i]));
      const here = out[order[k]];
      const was = new Map(here.map((id, i) => [id, i]));
      const want = (id) => { const ps = (near.get(id) ?? []).filter((n) => prev.has(n)).map((n) => prev.get(n)); return ps.length ? ps.reduce((x, y) => x + y, 0) / ps.length : was.get(id); };
      const score = new Map(here.map((id) => [id, want(id)]));
      here.sort((x, y) => (score.get(x) - score.get(y)) || (was.get(x) - was.get(y)));
    }
  };
  const fwd = out.map((_c, i) => i);
  sweep(fwd);
  sweep([...fwd].reverse());
  sweep(fwd);
  return out;
}

/** How many pairs of edges between neighbouring columns cross, for a layout: fewer is easier to read. */
export function crossings(cols, edges) {
  const at = new Map();
  cols.forEach((col, ci) => col.forEach((id, i) => at.set(id, [ci, i])));
  const spans = edges.filter(([a, b]) => at.has(a) && at.has(b) && Math.abs(at.get(a)[0] - at.get(b)[0]) === 1)
    .map(([a, b]) => (at.get(a)[0] < at.get(b)[0] ? [at.get(a), at.get(b)] : [at.get(b), at.get(a)]));
  let n = 0;
  for (let i = 0; i < spans.length; i++) for (let j = i + 1; j < spans.length; j++) {
    const [p, q] = spans[i], [r, t] = spans[j];
    if (p[0] === r[0] && (p[1] - r[1]) * (q[1] - t[1]) < 0) n++;
  }
  return n;
}

export class LineageGraph {
  constructor({ id, onFocus, onColumn, onOpen }) {
    Object.assign(this, { id, onFocus, onColumn, onOpen });
    this.graph = { nodes: [], edges: [] };
    this.focus = null;
    this.up = ALL; // the whole chain, upstream and downstream
    this.down = ALL;
    this.hover = null; // the box under the pointer: everything upstream and downstream of it lights up
    this.column = null;
    this.colEdges = [];
    this.sel = null; // the selected node: its details show, and the path through it lights up
    this.moved = new Map(); // node id -> {x, y}, where the person dragged it (layout coordinates)
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    this.press = null; // {kind: "node"|"pan", id, x0, y0, ox, oy, moved} while the left button is held
    this.view = null; // the canvas's size, from its last mouse event
  }

  set(graph, focus) {
    this.graph = graph ?? { nodes: [], edges: [] };
    if (focus !== undefined) {
      if (focus !== this.focus) { this.column = null; this.colEdges = []; this.sel = null; this.moved = new Map(); this.pan = { x: 0, y: 0 }; this.zoom = 1; this.fitted = false; this.touched = false; }
      this.focus = focus;
    }
    if (this.sel && !(this.graph.nodes ?? []).some((n) => n.id === this.sel)) this.sel = null;
    this.hover = null;
    this.cache = null;
  }

  /** Where a node is drawn, in layout coordinates: where it was dragged, else where the layout put it. */
  at(L, id) { return this.moved.get(id) ?? L.pos.get(id); }

  /** The nodes on the path through `id`: everything upstream of it and everything downstream, among `edges`. */
  pathOf(edges, id) {
    const walk = (dir) => {
      const seen = new Set([id]);
      const todo = [id];
      while (todo.length) {
        const x = todo.pop();
        for (const [a, b] of edges) {
          const [from, to] = dir ? [a, b] : [b, a];
          if (from === x && !seen.has(to)) { seen.add(to); todo.push(to); }
        }
      }
      return seen;
    };
    return { up: walk(false), down: walk(true) };
  }

  /** Zoom by `f` about a point on the canvas (its top-left when none). */
  zoomBy(f, px = 0, py = 0) {
    const z = Math.min(1.75, Math.max(0.3, this.zoom * f));
    this.pan = { x: px - (px - this.pan.x) * (z / this.zoom), y: py - (py - this.pan.y) * (z / this.zoom) };
    this.zoom = z;
  }

  /** Zoom and pan so the whole graph fits the canvas. */
  fit(L, w, h) {
    const z = Math.min(1, Math.max(0.3, Math.min((w - 16) / Math.max(1, L.width), (h - 16) / Math.max(1, L.height))));
    this.zoom = z;
    this.pan = { x: (w - L.width * z) / 2, y: (h - L.height * z) / 2 };
  }

  /** The layout's size with dragged nodes counted. */
  extent(L) {
    let w = L.width, h = L.height;
    for (const [, p] of this.moved) { w = Math.max(w, p.x + NODE_W + PAD); h = Math.max(h, p.y + NODE_H + PAD); }
    return { w, h };
  }

  setColumns(edges) {
    this.colEdges = edges ?? [];
    this.cache = null;
  }

  /** Node positions and the edges among shown nodes, cached until the graph, focus or depth changes. */
  layout() {
    const key = `${this.focus}|${this.up}|${this.down}|${(this.graph.nodes ?? []).length}|${(this.graph.edges ?? []).length}`;
    if (this.cache?.key === key) return this.cache;
    const byId = new Map((this.graph.nodes ?? []).map((n) => [n.id, n]));
    const level = this.focus && byId.has(this.focus) ? levels(this.graph, this.focus, this.up, this.down) : ranks(this.graph);
    const cols = new Map();
    for (const [id, l] of level) if (byId.has(id)) { if (!cols.has(l)) cols.set(l, []); cols.get(l).push(byId.get(id)); }
    const order = [...cols.keys()].sort((a, b) => a - b);
    const tallest = Math.max(1, ...[...cols.values()].map((v) => v.length));
    const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * GAP_Y;
    const pos = new Map();
    // By name first, so the same graph always starts the same; then untangled.
    const named = order.map((l) => cols.get(l).sort((a, b) => String(a.label ?? a.id).localeCompare(String(b.label ?? b.id))).map((n) => n.id));
    const all0 = (this.graph.edges ?? []).map(ends);
    const sorted = untangle(named, all0);
    order.forEach((l, ci) => {
      const col = sorted[ci].map((id) => byId.get(id));
      const colH = col.length * NODE_H + (col.length - 1) * GAP_Y;
      const top = PAD + (height - PAD * 2 - colH) / 2;
      col.forEach((n, i) => pos.set(n.id, { x: PAD + ci * (NODE_W + GAP_X), y: top + i * (NODE_H + GAP_Y), level: l }));
    });
    const width = PAD * 2 + order.length * NODE_W + Math.max(0, order.length - 1) * GAP_X;
    const edges = (this.graph.edges ?? []).map(ends).filter(([a, b]) => pos.has(a) && pos.has(b));
    const hidden = { up: 0, down: 0 };
    const all = this.focus ? levels(this.graph, this.focus, 99, 99) : new Map();
    for (const [, l] of all) { if (l < -this.up) hidden.up++; if (l > this.down) hidden.down++; }
    this.cache = { key, pos, width, height, edges, byId, hidden, columns: sorted };
    return this.cache;
  }

  /** The column a column-level edge touches at this node. */
  columnAt(id) {
    for (const e of this.colEdges) {
      if (e.to?.node === id) return e.to.column;
      if (e.from?.node === id) return e.from.column;
    }
    return null;
  }

  curve(p, a, b) {
    // On screen: layout coordinates scaled by the zoom and moved by the pan.
    const z = this.zoom, T = (q) => ({ x: q.x * z + this.pan.x, y: q.y * z + this.pan.y });
    a = T(a); b = T(b);
    const x1 = a.x + NODE_W * z, y1 = a.y + NODE_H * z / 2, x2 = b.x, y2 = b.y + NODE_H * z / 2;
    const dx = Math.max(24, (x2 - x1) / 2);
    // The line stops where the arrowhead starts; the head's point touches the dot on the box's edge.
    const end = x2 - (ARROW + DOT) * z;
    return p.move_to(x1, y1).cubic_bezier_to(end, y2, x1 + dx, y1, end - dx, y2);
  }

  /** The arrowhead at the box an edge points to: a small filled triangle, its point on the box's left edge. */
  head(b) {
    const z = this.zoom;
    const x = b.x * z + this.pan.x - DOT * z, y = b.y * z + this.pan.y + NODE_H * z / 2;
    return [[x, y], [x - ARROW * z, y - ARROW * z * 0.55], [x - ARROW * z, y + ARROW * z * 0.55]];
  }

  /** The canvas's dotted grid, as one filled path: it moves with the pan and spreads with the zoom, so the canvas
   *  reads as a surface the boxes sit on. Left out when zoomed so far out that the dots would touch. */
  grid(w, h) {
    const step = GRID * this.zoom;
    if (!(w > 0 && h > 0) || step < 9) return null;
    let p = PathBuilder.fill();
    const x0 = ((this.pan.x % step) + step) % step, y0 = ((this.pan.y % step) + step) % step;
    for (let y = y0; y < h; y += step) for (let x = x0; x < w; x += step) p = p.add_polygon([[x, y], [x + 1.5, y], [x + 1.5, y + 1.5], [x, y + 1.5]], true);
    return p.build();
  }

  icon(name, size, color) {
    // "brand:mlflow": a product's mark in its own color (kit/brands.js), whatever color was asked.
    if (String(name).startsWith("brand:")) { const b = BRANDS[name.slice(6)]; return svg(`icons/brand-${name.slice(6)}.svg`).size(size).flex_none().when(!!b, (e) => e.text_color(b.hex)); }
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  /** The selected node's details, over the canvas's top-left corner. */
  details(c, L) {
    const n = L.byId.get(this.sel);
    if (!n) return null;
    const [kindWord, ic] = KINDS[n.kind] ?? [n.kind ?? "Box", "layers"];
    const all = (this.graph.edges ?? []).map(ends);
    const name = (id) => L.byId.get(id)?.label ?? id;
    const ins = all.filter(([, b]) => b === n.id).map(([a]) => name(a));
    const outs = all.filter(([a]) => a === n.id).map(([, b]) => name(b));
    const cols = (n.columns ?? []).map((x) => (typeof x === "string" ? x : x.name));
    const line = (label, items) => v_flex().gap(2)
      .child(div().text_size(10.5).text_color(c.grey500).child(label))
      .child(div().text_size(11.5).font_family(MONO).text_color(c.navy).line_height("16px")
        .child(items.length ? items.slice(0, 6).join(", ") + (items.length > 6 ? `, and ${items.length - 6} more` : "") : "None"));
    return v_flex().id(`${this.id}-details`).absolute().top(8).left(8).w(260).p(10).gap(8).bg(c.surface).border_1().border_color(c.grey300)
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
      .on_scroll_wheel((_e, cx) => cx.stop_propagation?.())
      .child(h_flex().gap(6).items_center()
        .child(this.icon(ic, 14, c.blue))
        .child(div().flex_1().min_w(0).truncate().text_size(13).font_medium().font_family(MONO).text_color(c.navy).child(n.label ?? n.id))
        .when(n.version != null, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10).font_family(MONO).text_color(c.grey900).child(`v${n.version}`)))
        .child(div().id(`${this.id}-details-close`).flex_none().cursor_pointer().on_click((_e, cx) => { this.sel = null; cx.notify(); }).child(this.icon("x", 13, c.grey500))))
      .child(div().text_size(11.5).text_color(c.grey900).child(kindWord))
      .child(line(`Made from (${ins.length})`, ins))
      .child(line(`Feeds (${outs.length})`, outs))
      .when(cols.length > 0, (e) => e.child(line(`Columns (${cols.length})`, cols)))
      .when(!!this.onFocus && n.id !== this.focus && this.focus != null, (e) => e.child(div().id(`${this.id}-details-focus`).h(26).px(8).flex().items_center().justify_center()
        .cursor_pointer().border_1().border_color(c.blue).text_size(12).text_color(c.blue).hover((s) => s.bg(c.blueLight))
        .on_click((_e, cx) => this.onFocus(n.id, cx)).child("Put this in the middle")));
  }

  /** A small map of the whole graph, with the part on screen outlined; a press there moves the view to it. */
  minimap(c, L, w, h) {
    const E = this.extent(L);
    const z = this.zoom;
    if (!w || (E.w * z <= w && E.h * z <= h)) return null; // it all fits: no map needed
    const MW = 150, MH = 96;
    const s = Math.min(MW / E.w, MH / E.h);
    const vx = -this.pan.x / z * s, vy = -this.pan.y / z * s, vw = w / z * s, vh = h / z * s;
    return div().id(`${this.id}-minimap`).absolute().right(8).bottom(8).w(MW).h(MH).bg(c.surface).border_1().border_color(c.grey300).overflow_hidden()
      .on_mouse_down("left", (e, cx) => {
        cx.stop_propagation?.();
        const lp = e.local_position;
        if (!lp) return;
        this.pan = { x: -(lp.x / s) * z + w / 2, y: -(lp.y / s) * z + h / 2 };
        cx.notify();
      })
      .children([...L.pos.keys()].map((id) => {
        const q = this.at(L, id);
        return div().absolute().left(q.x * s).top(q.y * s).w(Math.max(2, NODE_W * s)).h(Math.max(2, NODE_H * s))
          .bg(id === this.sel ? c.blue : c.grey300);
      }))
      .child(div().absolute().left(vx).top(vy).w(vw).h(vh).border_1().border_color(c.blue));
  }

  /** opts.width: the canvas's width, when the host knows it (only a mod's root hears its own size). With it, a graph
   *  too big for the canvas starts fitted rather than cut off, and the minimap knows what's on screen. */
  render(c, opts = {}) {
    const L = this.layout();
    const focus = L.byId.get(this.focus);
    const colNodes = new Set(this.colEdges.flatMap((e) => [e.from?.node, e.to?.node]));
    const paint = globalThis.window?.paint_path;
    // Before the zoom is read: the fit sets it.
    // As tall as the host gives it, so the chain has room; else sized to the graph.
    const H = opts.height > 0 ? Math.max(300, opts.height) : Math.max(320, Math.min(620, L.height + 16));
    if (opts.width > 0) {
      this.view = { w: opts.width, h: H };
      // Fitted when it first has boxes, and again whenever the canvas changes size or the graph grows (a screen
      // learns its real width a moment after it first draws), until the person pans, zooms or drags: then the
      // view is theirs.
      const sized = `${opts.width}x${H}:${L.pos.size}`;
      if (L.pos.size && (!this.fitted || (!this.touched && sized !== this.fitFor))) {
        this.fitted = true;
        this.fitFor = sized;
        const E = this.extent(L);
        if (E.w > opts.width || E.h > H) this.fit(L, opts.width, H);
        else { this.zoom = 1; this.pan = { x: Math.max(0, (opts.width - E.w) / 2), y: Math.max(0, (H - E.h) / 2) }; }
      }
    }
    const z = this.zoom;
    // Everything upstream and downstream of the box under the pointer lights up, and the rest dims. With nothing
    // hovered, the selected box's path stays lit (and a traced column's edges, which the person picked).
    const lead = this.hover && L.pos.has(this.hover) ? this.hover : this.sel && L.pos.has(this.sel) ? this.sel : null;
    const path = lead ? this.pathOf(L.edges, lead) : null;
    const onPath = (a, b) => !!path && ((path.up.has(a) && path.up.has(b)) || (path.down.has(a) && path.down.has(b)));
    const inPath = (id) => !!path && (path.up.has(id) || path.down.has(id));
    const plain = [], lit = [];
    for (const [a, b] of L.edges) {
      const onColumn = this.colEdges.some((e) => e.from?.node === a && e.to?.node === b);
      (onColumn || onPath(a, b) ? lit : plain).push([a, b]);
    }

    // One stroked path for the lines and one filled path for their arrowheads, per colour.
    const layer = (list, color, width) => {
      if (!paint || !list.length) return null;
      let p = PathBuilder.stroke(width);
      let heads = PathBuilder.fill();
      for (const [a, b] of list) { p = this.curve(p, this.at(L, a), this.at(L, b)); heads = heads.add_polygon(this.head(this.at(L, b)), true); }
      return div().absolute().top(0).left(0).size_full()
        .child(div().absolute().inset_0().child(paint(p.build(), color)))
        .child(div().absolute().inset_0().child(paint(heads.build(), color)));
    };
    const nodes = [...L.pos.keys()].map((id) => {
      const at = this.at(L, id);
      const n = L.byId.get(id);
      const [kindWord, ic, stripe] = KINDS[n.kind] ?? [n.kind ?? "Box", "layers", "grey500"];
      const on = id === this.focus;
      const picked = id === this.sel;
      const viaColumn = colNodes.has(id);
      const dim = !!path && !inPath(id);
      // A rounded box: an icon in the kind's colour, the title, and the kind in words under it.
      return h_flex().id(`${this.id}-node-${id}`).absolute().left(at.x * z + this.pan.x).top(at.y * z + this.pan.y).w(NODE_W * z).h(NODE_H * z)
        .px(10 * z).gap(9 * z).items_center().cursor_pointer().overflow_hidden().rounded(RADIUS * z)
        .bg(picked || on ? c.blueLight : c.surface).border_1().when(picked, (e) => e.border_2())
        .border_color(picked || on || viaColumn || (inPath(id) && !picked) ? c.blue : c.grey300)
        .when(dim, (e) => e.opacity(0.45))
        .hover((s) => s.border_color(c.blue))
        .tooltip(`${kindWord}: ${n.label ?? id}${n.version ? ` v${n.version}` : ""}`)
        .on_hover((over, cx) => { if (over) { if (this.hover !== id) { this.hover = id; cx.notify(); } } else if (this.hover === id) { this.hover = null; cx.notify(); } })
        .on_mouse_down("left", (e, cx) => {
          cx.stop_propagation?.();
          if (e.click_count >= 2) { if (id !== this.focus) this.onFocus?.(id, cx); return; }
          this.press = { kind: "node", id, x0: e.position?.x ?? 0, y0: e.position?.y ?? 0, ox: at.x, oy: at.y, moved: false };
        })
        // A click opens the box where it has a page (onOpen answers true). Where it has none, its details show
        // (and a second click clears them). A drag that ends here isn't a click.
        .on_click((_e, cx) => {
          if (this.dragEnd && Date.now() - this.dragEnd < 250) return;
          if (id !== this.focus && this.onOpen?.(n, cx) === true) return;
          this.sel = this.sel === id ? null : id;
          cx.notify();
        })
        .child(div().size(28 * z).flex().flex_none().items_center().justify_center().rounded(6 * z).bg(c.surface2)
          .child(this.icon(ic, 14 * z, c[stripe] ?? c.grey500)))
        .child(v_flex().flex_1().min_w(0).gap(1 * z)
          .child(h_flex().gap(6 * z).items_center()
            .child(div().flex_1().min_w(0).truncate().text_size(12.5 * z).font_medium().text_color(on || picked ? c.blue : c.navy).child(n.label ?? id))
            .when(n.version != null, (e) => e.child(div().flex_none().px(4 * z).rounded(3 * z).border_1().border_color(c.grey300).text_size(10 * z).text_color(c.grey900).child(`v${n.version}`))))
          .child(div().truncate().text_size(10.5 * z).text_color(c.grey500).child(viaColumn && this.column ? `${kindWord}, column ${this.columnAt(id) ?? this.column}` : kindWord)));
    });
    // The dots where connectors join the boxes: one on the right edge of a box something reads from, one on the
    // left edge of a box made from something.
    const outs = new Set(L.edges.map(([a]) => a)), ins = new Set(L.edges.map(([, b]) => b));
    const dot = (id, side) => {
      const at = this.at(L, id);
      const cx0 = (at.x + (side === "out" ? NODE_W : 0)) * z + this.pan.x, cy0 = (at.y + NODE_H / 2) * z + this.pan.y;
      const lit = !!path && inPath(id);
      return div().id(`${this.id}-dot-${side}-${id}`).absolute().left(cx0 - DOT * z).top(cy0 - DOT * z).size(DOT * 2 * z).rounded_full()
        .bg(c.surface).border_1().border_color(lit ? c.blue : c.grey500).when(!!path && !lit, (e) => e.opacity(0.45));
    };
    const dots = [...[...outs].map((id) => dot(id, "out")), ...[...ins].map((id) => dot(id, "in"))];
    const view = (e) => { if (e?.bounds) this.view = { w: e.bounds.width, h: e.bounds.height }; };
    const zoomBtn = (k, text, tip, go) => div().id(`${this.id}-zoom-${k}`).h(24).min_w(24).px(6).flex().items_center().justify_center().cursor_pointer()
      .bg(c.surface).border_1().border_color(c.grey300).text_size(12).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue))
      .tooltip(tip).on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
      .on_click((_e, cx) => { go(); cx.notify(); }).child(text);
    const vw = this.view?.w ?? 0, vh = this.view?.h ?? H;
    const cols = focus?.columns ?? [];
    return v_flex().id(`${this.id}-wrap`).w_full().gap(10)
      .when(cols.length > 0, (e) => e.child(h_flex().id(`${this.id}-cols`).gap(4).items_center().overflow_x_scrollbar().text_size(11.5)
        .child(div().flex_none().text_color(c.grey500).child("Trace a column"))
        .children(cols.map((col) => {
          const name = typeof col === "string" ? col : col.name;
          const on = this.column === name;
          return div().id(`${this.id}-col-${name}`).h(22).px(6).flex().flex_none().items_center().cursor_pointer().font_family(MONO)
            .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.grey900)
            .on_click((_e, cx) => { this.column = on ? null : name; if (on) this.colEdges = []; this.cache = null; this.onColumn?.(this.column, cx); cx.notify(); })
            .child(name);
        }))))
      .child(div().id(`${this.id}-canvas`).relative().w_full().flex_none().h(H).overflow_hidden().border_1().border_color(c.rule).bg(c.surface2)
        .when(this.press?.kind === "pan" && this.press.moved, (e) => e.cursor_grabbing())
        // Background press: pan. Nodes stop the press, so it's theirs to drag.
        .on_mouse_down("left", (e, cx) => {
          view(e);
          this.press = { kind: "pan", x0: e.position?.x ?? 0, y0: e.position?.y ?? 0, ox: this.pan.x, oy: this.pan.y, moved: false };
        })
        .on_mouse_move((e, cx) => {
          view(e);
          const p = this.press;
          if (!p) return;
          const dx = (e.position?.x ?? 0) - p.x0, dy = (e.position?.y ?? 0) - p.y0;
          if (!p.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
          p.moved = true;
          this.touched = true;
          if (p.kind === "node") this.moved.set(p.id, { x: p.ox + dx / this.zoom, y: p.oy + dy / this.zoom });
          else this.pan = { x: p.ox + dx, y: p.oy + dy };
          cx.notify();
        })
        .on_mouse_up("left", (_e, cx) => {
          const p = this.press;
          this.press = null;
          if (p?.moved) { this.dragEnd = Date.now(); cx.notify(); }
          else if (p?.kind === "pan" && this.sel) { this.sel = null; cx.notify(); } // a press on the background clears the selection
        })
        .on_hover((over, cx) => { if (!over && this.press) { this.press = null; cx.notify(); } })
        // Scroll pans. With Command (or Control) held, it zooms about the pointer.
        .on_scroll_wheel((e, cx) => {
          view(e);
          cx.stop_propagation?.();
          const d = e.delta ?? { x: 0, y: 0 };
          this.touched = true;
          if (e.modifiers?.platform || e.modifiers?.control) {
            const lp = e.local_position ?? (e.bounds && e.position ? { x: e.position.x - e.bounds.x, y: e.position.y - e.bounds.y } : { x: 0, y: 0 });
            this.zoomBy(Math.exp(d.y * 0.005), lp.x, lp.y);
          } else this.pan = { x: this.pan.x + d.x, y: this.pan.y + d.y };
          cx.notify();
        })
        .child((() => { const g = paint ? this.grid(vw || opts.width || 0, vh) : null; return g ? div().id(`${this.id}-grid`).absolute().inset_0().child(paint(g, c.grey300)) : div(); })())
        .child(layer(plain, c.grey500, 1.5) ?? div())
        .child(layer(lit, c.blue, 2.5) ?? div())
        .children(nodes)
        .children(dots)
        .child(this.details(c, L) ?? div())
        .child(this.minimap(c, L, vw, vh) ?? div())
        .child(h_flex().absolute().top(8).right(8).gap(4)
          .child(zoomBtn("out", "-", "Zoom out", () => { this.touched = true; this.zoomBy(1 / 1.2, (vw || L.width) / 2, vh / 2); }))
          .child(zoomBtn("in", "+", "Zoom in", () => { this.touched = true; this.zoomBy(1.2, (vw || L.width) / 2, vh / 2); }))
          .child(zoomBtn("fit", "Fit", "Show the whole graph", () => { this.moved = new Map(); this.touched = false; this.fit(L, vw || L.width, vh); }))))
      .child(div().id(`${this.id}-hint`).text_size(11).text_color(c.grey500).child("Hover a box to see what feeds it and what it feeds. Click one to open it. Drag to move, Command and scroll to zoom."))
      .child(h_flex().flex_wrap().gap(12).text_size(11).text_color(c.grey500)
        .children([...new Set([...L.pos.keys()].map((id) => L.byId.get(id)?.kind))].filter(Boolean).map((k) =>
          h_flex().gap(4).items_center().child(this.icon((KINDS[k] ?? [k, "layers"])[1], 12, c[(KINDS[k] ?? [])[2]] ?? c.grey500)).child(div().child((KINDS[k] ?? [k])[0])))));
  }
}
