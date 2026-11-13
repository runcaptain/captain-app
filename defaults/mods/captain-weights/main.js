// CAPTAIN: Weights: a model's network, drawn, so a person can watch what an agent reads and changes in it. Layers
// left to right, a neuron's fill for its activation on the chosen input, a weight as a blue or red line, a bias as
// a small number on its neuron (draw.js). It opens from the palette ("Open a network's weights") on a list with
// two starter networks, or straight on a model: props {model}.
//
// The numbers come through adapter.js and nowhere else (shapes.js says what they look like). The page reads once
// when it opens, again the moment the model's or the session's topic changes on the app's change feed, and again
// right after its own action returns. It has no timer that reads data.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, props, closeOverlay } from "herdr";
import { palette, isDark, FONT, RADIUS } from "./kit/tokens.js";
import { Button, Skeleton, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { listNetworks, readNetwork, readSession, readTestCost, watchWeights } from "./adapter.js";
import { forward, backward, countOf } from "./net.js";
import { PanZoom } from "./canvas.js";
import { NetworkCanvas, layoutOf } from "./draw.js";
import { Inspector } from "./inspect.js";
import { CostPanel } from "./cost.js";

const SIDE_W = 340; // the panel beside the drawing
const BAND_H = 178; // the cost and the timeline, under the drawing
const count = (n) => Number(n).toLocaleString("en-US");

/** "784 in, 10 out, 13,002 numbers": the network as a function, in one line. */
export function factsOf(net) {
  const n = countOf(net);
  return `${count(net.sizes[0])} in, ${count(net.sizes.at(-1))} out, ${count(n.total)} numbers (${count(n.weights)} weights and ${count(n.biases)} biases)`;
}

export default class CaptainWeights extends View {
  init(_props, cx) {
    this.model = props()?.model ?? null; // the network open, or null for the list
    this.list = null;       // the networks that can be opened
    this.net = null;        // the open network (shapes.js Network)
    this.problem = null;    // why it could not be read, in a sentence
    this.inputAt = 0;       // which of the network's examples is the input
    this.run = null;        // the network run on that input: activations and weighted sums
    this.sel = null;        // the open neuron: {k: column, j: index}
    this.selW = null;       // the picked weight: {k: layer, j: to, i: from}
    this.mode = "values";   // what the lines show: "values" (the weights) or "nudges" (what the cost wants)
    this.grad = null;       // backpropagation for the chosen input, while nudges are showing
    this.session = null;    // the session on this network: what the agent was allowed, and everything done
    this.test = null;       // the cost across the test set: {cost, right, of}
    this.openEdit = null;   // the edit opened in the timeline
    this.reads = 0;         // how many times the network has been asked for: an older answer never replaces a newer
    this.stop = null;       // stops listening to the change feed
    this.pz = new PanZoom("wt");
    this.width = 1200; this.height = 760;
    if (this.model) this.open(this.model, cx); else this.loadList(cx);
  }

  loadList(cx) {
    cx.spawn(async (cx) => { const r = await listNetworks(); this.list = r.ok ? r.data : []; cx.notify(); });
  }

  /** Open a network: read its session and its numbers, run it on its first input, and listen for changes. */
  open(model, cx) {
    this.model = model; this.net = null; this.problem = null; this.sel = null; this.selW = null; this.inputAt = 0;
    this.session = null; this.test = null; this.openEdit = null; this.readFor = null;
    this.pz = new PanZoom("wt");
    this.stop?.(); this.stop = null;
    this.read(cx);
    cx.notify();
  }

  /** Read everything again: the session's record, the numbers if they changed, and the test-set cost. Called when
   *  the page opens, when the change feed says the model or the session changed, and after the page's own action.
   *  Quiet: nothing on screen goes blank while it reads. */
  read(cx) {
    const model = this.model, mine = ++this.reads;
    cx.spawn(async (cx) => {
      const s = await readSession(model);
      if (mine !== this.reads || model !== this.model) return;
      if (s.ok) this.session = s.data;
      // The numbers are read again only when an edit or an undo has been recorded since they were last read.
      const changes = (this.session?.events ?? []).filter((e) => e.kind === "edit" || e.kind === "undo" || e.kind === "redo");
      const mark = changes.at(-1)?.id ?? "";
      if (!this.net || mark !== this.readFor) {
        const r = await readNetwork(model, this.session?.id);
        if (mine !== this.reads || model !== this.model) return;
        if (r.ok) { this.take(r.data); this.readFor = mark; }
        else if (!this.net) this.problem = plainError(r.error, "Captain couldn't read that network.");
      }
      if (this.net && !this.stop) this.stop = watchWeights(cx, model, this.session?.id, (cx) => this.read(cx));
      cx.notify();
      if (!this.net) return;
      // The test set's cost. A network that carries its test set is scored through the adapter each time. One that
      // does not (the services keep the test set to themselves) takes the number from its last edit, and asks
      // once when there is none.
      const last = changes.at(-1)?.cost?.test;
      if (!this.net.test?.length && last && Number.isFinite(last.after)) this.test = { cost: last.after, right: last.right_after ?? null, of: last.of ?? null };
      else if (this.net.test?.length || !this.test) {
        const t = await readTestCost(model, this.session?.id);
        if (mine !== this.reads || model !== this.model) return;
        if (t.ok) this.test = t.data;
      }
      cx.notify();
    });
  }

  /** Show an edit: its three numbers under the cost, and its line open in the timeline. */
  openTimeline(id, cx) { this.openEdit = this.openEdit === id ? null : id; cx.notify(); }

  /** New numbers for the open network: keep the input and the selection, and run it again. */
  take(net) {
    this.net = net;
    this.layout = layoutOf(net);
    this.inputAt = Math.min(this.inputAt, Math.max(0, net.examples.length - 1));
    this.rerun();
  }

  rerun() {
    const e = this.net?.examples?.[this.inputAt];
    this.run = e ? forward(this.net, e.x) : null;
    // Worked out here from the numbers the page already has: looking never spends an agent's allowance.
    this.grad = e && this.wantsGrad() ? backward(this.net, e.x, e.y, this.run) : null;
  }

  /** Whether anything on screen needs the gradients. */
  wantsGrad() { return this.mode === "nudges"; }
  setMode(mode, cx) { this.mode = mode; this.rerun(); cx.notify(); }

  pickInput(n, cx) { this.inputAt = n; this.rerun(); cx.notify(); }
  pickNeuron(at, cx) { this.sel = this.sel && this.sel.k === at.k && this.sel.j === at.j ? null : at; this.selW = null; cx.notify(); }
  pickWeight(at, cx) { this.selW = at; this.sel = { k: at.k + 1, j: at.j }; cx.notify(); }
  back(cx) { this.stop?.(); this.stop = null; this.model = null; this.net = null; this.session = null; this.problem = null; if (!this.list) this.loadList(cx); cx.notify(); }

  /** What the drawing needs to know. */
  drawing() {
    return { net: this.net, run: this.run, grad: this.grad, sel: this.sel, selW: this.selW, mode: this.mode, walkedFrom: null, pulse: null };
  }

  header(c) {
    const net = this.net;
    return h_flex().h(56).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
      .child(div().size(28).flex().flex_none().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("waypoints")).size(15)))
      .child(v_flex().flex_1().min_w(0)
        .child(div().id("wt-title").truncate().text_size(15).font_semibold().child(net ? `Weights of ${net.name}` : "Weights"))
        .when(Boolean(net), (e) => e.child(div().id("wt-facts").truncate().text_size(12).text_color(c.grey500).child(factsOf(net)))))
      .when(Boolean(this.model), (e) => e.child(Button(c, "wt-back", "Open another network", (_e, cx) => this.back(cx), { small: true })))
      .child(div().id("wt-close").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
        .on_click(() => closeOverlay(props()?.overlay)).child("Close"));
  }

  /** Nothing open: the two starter networks, then the ones Captain knows. */
  chooser(c) {
    const rows = this.list ?? [];
    return v_flex().id("wt-chooser").flex_1().min_h(0).px(20).py(24).gap(14).items_center()
      .child(div().text_size(15).font_semibold().child("Open a network to see inside it"))
      .child(div().max_w(520).text_center().text_size(13).line_height("19px").text_color(c.grey500)
        .child("Every neuron, weight and bias is drawn. Pick an input to watch the network light up, and open a neuron to see what feeds it."))
      .when(!this.list, (e) => e.child(div().w(520).child(Skeleton(c, 2))))
      .child(v_flex().w(520).border_1().border_color(c.rule).children(rows.map((m, n) => h_flex().id(`wt-open-${m.id}`).px(14).py(12).gap(12).items_center()
        .when(n > 0, (e) => e.border_t(1).border_color(c.rule))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(13).font_medium().child(m.name))
          .when(Boolean(m.says), (e) => e.child(div().text_size(12).line_height("17px").text_color(c.grey500).child(m.says))))
        .child(Button(c, `wt-open-btn-${m.id}`, `Open ${m.name}`, (_e, cx) => this.open(m.id, cx), { small: true })))));
  }

  /** The inputs a person can choose, as a row of picks. */
  inputs(c) {
    const ex = this.net.examples;
    return h_flex().id("wt-inputs").h(44).flex_none().px(20).gap(6).items_center()
      .child(div().flex_none().pr(4).text_size(12.5).text_color(c.grey500).child("Input"))
      .child(h_flex().flex_1().min_w(0).gap(6).overflow_x_scrollbar().children(ex.map((e, n) => {
        const on = n === this.inputAt;
        return div().id(`wt-in-${n}`).h(26).px(9).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
          .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
          .on_click((_e, cx) => this.pickInput(n, cx)).child(e.name);
      })))
      .child(this.modes(c));
  }

  /** What the lines show: the weights, or the way the cost wants each one moved. */
  modes(c) {
    const pick = (id, text, mode, tip) => { const on = this.mode === mode; return div().id(id).h(26).px(9).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).tooltip(tip)
      .on_click((_e, cx) => this.setMode(mode, cx)).child(text); };
    return h_flex().id("wt-modes").flex_none().gap(6).items_center()
      .child(div().flex_none().pr(4).text_size(12.5).text_color(c.grey500).child("Lines show"))
      .child(pick("wt-mode-values", "The weights", "values", "Each line is a weight: blue is positive, red is negative, thicker is larger"))
      .child(pick("wt-mode-nudges", "What the cost wants", "nudges", "Each line is the nudge the cost wants for that weight on this input (its gradient, turned around): blue is up, red is down"));
  }

  side(c) {
    return div().id("wt-side").w(SIDE_W).flex_none().h_full().overflow_y_scroll().child(div().p(16).child(Inspector(c, this)));
  }

  render() {
    const c = palette(isDark(theme()));
    const net = this.net;
    const cw = Math.max(320, this.width - SIDE_W - 41), ch = Math.max(180, this.height - 56 - 44 - BAND_H - 12);
    return v_flex().id("weights").size_full().bg(c.surface).rounded(RADIUS).text_color(c.navy).font_family(FONT)
      .on_size_change((size, cx) => { if (Math.abs(size.width - this.width) > 1 || Math.abs(size.height - this.height) > 1) { this.width = size.width; this.height = size.height; cx.notify(); } })
      .child(this.header(c))
      .when(!this.model, (e) => e.child(this.chooser(c)))
      .when(Boolean(this.model) && !net && !this.problem, (e) => e.child(div().p(20).child(Skeleton(c, 6))))
      .when(Boolean(this.problem), (e) => e.child(v_flex().id("wt-problem").p(20).gap(10).items_start()
        .child(div().text_size(13).line_height("19px").text_color(c.fail).child(this.problem))
        .child(Button(c, "wt-retry", "Try again", (_e, cx) => this.open(this.model, cx), { small: true }))))
      .when(Boolean(net), (e) => e
        .child(this.inputs(c))
        .child(h_flex().flex_1().min_h(0)
          .child(div().pl(20).child(NetworkCanvas(c, this.drawing(), { id: "wt", pz: this.pz, layout: this.layout, width: cw, height: ch,
            onNeuron: (at, cx) => this.pickNeuron(at, cx), onWeight: (at, cx) => this.pickWeight(at, cx) })))
          .child(div().w(20).flex_none())
          .child(div().w(1).h_full().flex_none().bg(c.rule))
          .child(this.side(c)))
        .child(h_flex().id("wt-band").h(BAND_H).flex_none().border_t(1).border_color(c.rule)
          .child(div().pl(20).pt(12).child(CostPanel(c, this, { width: Math.min(560, cw), height: BAND_H - 12 })))));
  }
}
