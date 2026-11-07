// captain-ui: a lineage graph, drawn natively. Nodes are source tables, datasets, pipelines, models, backtests and
// handoffs; edges say what was made from what. Columns run upstream to downstream around the focused node, how far
// each way is the person's choice, and picking a column of the focused node highlights the column-level edges.
//
//   const lin = new LineageGraph({ id: "lin", onFocus: (id, cx) => ..., onColumn: (col, cx) => ... });
//   lin.set({ nodes: [{id, kind, label, version?, columns?}], edges: [{from, to}] }, focusId);
//   lin.render(c)            // keep the instance on the view
//
// Edges may use {from, to} or {src, dst}. Column edges: lin.setColumns([{from: {node, column}, to: {node, column}}]).
import { div, svg, PathBuilder } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { MONO } from "./tokens.js";
import { icon } from "./icons.js";

const NODE_W = 196;
const NODE_H = 52;
const GAP_X = 72;
const GAP_Y = 14;
const PAD = 16;

export const KINDS = {
  table: ["Source table", "table"], connection: ["Database", "server"], dataset: ["Dataset", "database"],
  pipeline: ["Pipeline", "workflow"], model: ["Model", "boxes"], backtest: ["Backtest", "flask-conical"],
  run: ["Backtest run", "flask-conical"], handoff: ["Handoff", "send"], label: ["Label", "tag"],
  notebook: ["Notebook", "file-text"], registry: ["Registry", "cloud"],
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
  return level;
}

export class LineageGraph {
  constructor({ id, onFocus, onColumn }) {
    Object.assign(this, { id, onFocus, onColumn });
    this.graph = { nodes: [], edges: [] };
    this.focus = null;
    this.up = 2;
    this.down = 2;
    this.column = null;
    this.colEdges = [];
  }

  set(graph, focus) {
    this.graph = graph ?? { nodes: [], edges: [] };
    if (focus !== undefined) { if (focus !== this.focus) { this.column = null; this.colEdges = []; } this.focus = focus; }
    this.cache = null;
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
    const level = this.focus && byId.has(this.focus) ? levels(this.graph, this.focus, this.up, this.down) : new Map((this.graph.nodes ?? []).map((n) => [n.id, 0]));
    const cols = new Map();
    for (const [id, l] of level) if (byId.has(id)) { if (!cols.has(l)) cols.set(l, []); cols.get(l).push(byId.get(id)); }
    const order = [...cols.keys()].sort((a, b) => a - b);
    const tallest = Math.max(1, ...[...cols.values()].map((v) => v.length));
    const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * GAP_Y;
    const pos = new Map();
    order.forEach((l, ci) => {
      const col = cols.get(l).sort((a, b) => String(a.label ?? a.id).localeCompare(String(b.label ?? b.id)));
      const colH = col.length * NODE_H + (col.length - 1) * GAP_Y;
      const top = PAD + (height - PAD * 2 - colH) / 2;
      col.forEach((n, i) => pos.set(n.id, { x: PAD + ci * (NODE_W + GAP_X), y: top + i * (NODE_H + GAP_Y), level: l }));
    });
    const width = PAD * 2 + order.length * NODE_W + Math.max(0, order.length - 1) * GAP_X;
    const edges = (this.graph.edges ?? []).map(ends).filter(([a, b]) => pos.has(a) && pos.has(b));
    const hidden = { up: 0, down: 0 };
    const all = this.focus ? levels(this.graph, this.focus, 99, 99) : new Map();
    for (const [, l] of all) { if (l < -this.up) hidden.up++; if (l > this.down) hidden.down++; }
    this.cache = { key, pos, width, height, edges, byId, hidden };
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
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
    const dx = Math.max(24, (x2 - x1) / 2);
    return p.move_to(x1, y1).cubic_bezier_to(x2, y2, x1 + dx, y1, x2 - dx, y2);
  }

  icon(name, size, color) {
    return svg(icon(name)).size(size).flex_none().when(!!color, (e) => e.text_color(color));
  }

  step(c, id, label, value, set) {
    const btn = (k, text, on) => div().id(`${this.id}-${id}-${k}`).size(22).flex().items_center().justify_center().cursor_pointer()
      .border_1().border_color(c.grey300).text_size(13).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue))
      .on_click((_e, cx) => { set(on); this.cache = null; cx.notify(); }).child(text);
    return h_flex().gap(6).items_center().text_size(12)
      .child(div().text_color(c.grey900).child(label))
      .child(btn("less", "-", Math.max(0, value - 1)))
      .child(div().w(18).text_center().font_family(MONO).child(String(value)))
      .child(btn("more", "+", Math.min(6, value + 1)));
  }

  render(c) {
    const L = this.layout();
    const focus = L.byId.get(this.focus);
    const colNodes = new Set(this.colEdges.flatMap((e) => [e.from?.node, e.to?.node]));
    const paint = globalThis.window?.paint_path;
    const plain = [], lit = [];
    for (const [a, b] of L.edges) {
      const onColumn = this.colEdges.some((e) => e.from?.node === a && e.to?.node === b);
      (onColumn ? lit : plain).push([a, b]);
    }
    const layer = (list, color, width) => {
      if (!paint || !list.length) return null;
      let p = PathBuilder.stroke(width);
      for (const [a, b] of list) p = this.curve(p, L.pos.get(a), L.pos.get(b));
      return div().absolute().top(0).left(0).w(L.width).h(L.height).child(paint(p.build(), color));
    };
    const nodes = [...L.pos.entries()].map(([id, at]) => {
      const n = L.byId.get(id);
      const [kindWord, ic] = KINDS[n.kind] ?? [n.kind ?? "Node", "layers"];
      const on = id === this.focus;
      const viaColumn = colNodes.has(id);
      return v_flex().id(`${this.id}-node-${id}`).absolute().left(at.x).top(at.y).w(NODE_W).h(NODE_H).px(10).py(6).gap(2).justify_center().cursor_pointer()
        .bg(on ? c.blueLight : c.surface).border_1().border_color(on || viaColumn ? c.blue : c.grey300)
        .hover((s) => s.border_color(c.blue))
        .tooltip(`${kindWord}: ${n.label ?? id}${n.version ? ` v${n.version}` : ""}`)
        .on_click((_e, cx) => { if (id !== this.focus) this.onFocus?.(id, cx); })
        .child(h_flex().gap(6).items_center()
          .child(this.icon(ic, 13, on ? c.blue : c.navy))
          .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_medium().font_family(MONO).text_color(on ? c.blue : c.navy).child(n.label ?? id))
          .when(n.version != null, (e) => e.child(div().flex_none().px(4).border_1().border_color(c.grey300).text_size(10).font_family(MONO).text_color(c.grey900).child(`v${n.version}`))))
        .child(div().truncate().text_size(10.5).text_color(c.grey500).child(viaColumn && this.column ? `${kindWord}, column ${this.columnAt(id) ?? this.column}` : kindWord));
    });
    const cols = focus?.columns ?? [];
    return v_flex().id(`${this.id}-wrap`).w_full().gap(10)
      .child(h_flex().flex_wrap().gap(16).items_center()
        .child(this.step(c, "up", "Upstream", this.up, (v) => { this.up = v; }))
        .child(this.step(c, "down", "Downstream", this.down, (v) => { this.down = v; }))
        .when(L.hidden.up + L.hidden.down > 0, (e) => e.child(div().text_size(11.5).text_color(c.grey500)
          .child(`${L.hidden.up + L.hidden.down} more ${L.hidden.up && L.hidden.down ? "further each way" : L.hidden.up ? "further upstream" : "further downstream"}`))))
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
      .child(div().id(`${this.id}-canvas`).w_full().h(Math.min(560, L.height + 16)).overflow_scrollbar().border_1().border_color(c.rule).bg(c.surface2)
        .child(div().relative().w(L.width).h(L.height)
          .child(layer(plain, c.grey300, 1.5) ?? div())
          .child(layer(lit, c.blue, 2.5) ?? div())
          .children(nodes)))
      .child(h_flex().flex_wrap().gap(12).text_size(11).text_color(c.grey500)
        .children([...new Set([...L.pos.keys()].map((id) => L.byId.get(id)?.kind))].filter(Boolean).map((k) =>
          h_flex().gap(4).items_center().child(this.icon((KINDS[k] ?? [k, "layers"])[1], 11, c.grey500)).child(div().child((KINDS[k] ?? [k])[0])))));
  }
}
