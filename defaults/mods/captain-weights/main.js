// CAPTAIN: Weights: a model's network, drawn, so a person can watch what an agent reads and changes in it. Layers
// left to right, a neuron's fill for its activation on the chosen input, a weight as a blue or red line, a bias as
// a small number on its neuron (draw.js).
//
// It is a page in the window's main slot (the layout's "weights" block), beside the terminal where the agent
// runs: the whole point is watching the network while the agent edits it. It is not one of the sidebar's views.
// It opens from the palette ("Open a network's weights"), from `cst app open weights[:<model>]`, or from any
// screen that sets shared "captain.open" to {section: "weights", model?} and selects the block. With no model it
// shows the list of networks.
//
// The numbers come through adapter.js and nowhere else (shapes.js says what they look like). The page reads once
// when it opens, again the moment the model's or the session's topic changes on the app's change feed, and again
// right after its own action returns. It has no timer that reads data.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState } from "gpui-base";
import { theme, props, shared } from "herdr";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { liveState, updatedWords } from "./kit/live.js";
import { SendToAgent } from "./kit/context.js";
import { Button, Skeleton, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int } from "./kit/num.js";
import { listNetworks, readNetwork, readSession, readTestCost, watchWeights, sendUndo, sendEdit } from "./adapter.js";
import { forward, backward, countOf } from "./net.js";
import { PanZoom } from "./canvas.js";
import { NetworkCanvas, layoutOf } from "./draw.js";
import { Inspector } from "./inspect.js";
import { CostPanel } from "./cost.js";
import { Timeline, ledgerFacts } from "./timeline.js";
import { HandBlock, handTarget, dragSpan, TRACK_W, selectionPin, starters } from "./hand.js";
import { askAgent } from "./kit/agent.js";
import { WalkControls, ChainBlock, walkedFrom, stepsOf, steepestWeight } from "./back.js";

const WIDE = 1100; // from this width the neuron panel is always beside the drawing; below it, only while a neuron is open
const PULSE_BEAT_MS = 120, PULSE_BEATS = 22; // touched neurons and weights pulse for about 2.6 seconds after an event
const BAND_H = 190; // the cost and the timeline, under the drawing
const count = (n) => int(n);

/** "784 in, 10 out, 13,002 numbers": the network as a function, in one line. */
export function factsOf(net) {
  const n = countOf(net);
  // "Made-up data" is said only when the network says so of itself.
  return `${count(net.sizes[0])} in, ${count(net.sizes.at(-1))} out, ${count(n.total)} numbers (${count(n.weights)} weights and ${count(n.biases)} biases)${net.synthetic ? ". Made-up data" : ""}`;
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
    this.walk = { step: 0, playing: false }; // the backward walk: 0 is off, 1 the output's error, then a layer a step
    this.session = null;    // the session on this network: what the agent was allowed, and everything done
    this.test = null;       // the cost across the test set: {cost, right, of}
    this.openEdit = null;   // the edit opened in the timeline
    this.pulse = null;      // what the last events touched, while it pulses: {neurons, weights, until, phase}
    this.beats = 0;         // how many pulse beats have been armed: an older one does nothing when it fires
    this.keep = {};         // the drawing's shapes, kept between draws (draw.js)
    this.seen = null;       // the last event's number already on screen: newer ones pulse
    this.busy = null;       // the edit being taken back, or "hand" while a change by hand is sent
    this.note = null;       // why the last change by hand could not be made
    this.drag = null;       // a number being dragged: {x0, from, k, j, i}
    this.handInput = InputState.new({ placeholder: "a number" }); // the value typed for the picked number
    this.handInput.on("submit", (_e, ecx) => this.setTyped(ecx ?? cx));
    this.reads = 0;         // how many times the network has been asked for: an older answer never replaces a newer
    this.stop = null;       // stops listening to the change feed
    this.pz = new PanZoom("wt");
    this.width = 820; this.height = 760; // until the block says its size: about what is left beside the terminal
    this.askOpen = false;   // whether the three things to ask an agent are showing
    this.asked = 0;         // the `at` of the last request from another screen that was handled
    this.readAt = 0;        // when the page last read
    if (this.model) this.open(this.model, cx); else this.loadList(cx);
    this.takeOpen(cx);
    // A link on another screen, the palette or `cst app open` cannot reach this page's props: it sets the shared
    // "open" value and selects this block. Looked at a few times a second, as the other screens do. Reads no data.
    cx.timer.every(300, (cx) => this.takeOpen(cx));
    // The walk's clock: one step a beat while it plays. It moves the picture only, and reads nothing.
    cx.timer.every(1200, (cx) => { if (this.walk.playing) this.stepWalk(1, cx); });
    this.cx = cx;
  }

  loadList(cx) {
    cx.spawn(async (cx) => { const r = await listNetworks(); this.list = r.data.networks; this.fromServices = r.data.fromServices; cx.notify(); });
  }

  /** What another screen asked this page to show: one network, or the list. Each request is handled once. */
  takeOpen(cx) {
    const o = shared("captain.open");
    if (!o || o.section !== "weights" || !o.at || o.at === this.asked) return;
    this.asked = o.at;
    if (o.home || !o.model) { if (this.model) this.back(cx); else this.loadList(cx); return; }
    if (o.model !== this.model) this.open(String(o.model), cx);
  }

  /** Open a network: read its session and its numbers, run it on its first input, and listen for changes. */
  open(model, cx) {
    this.model = model; this.net = null; this.problem = null; this.sel = null; this.selW = null; this.inputAt = 0;
    this.session = null; this.test = null; this.openEdit = null; this.readFor = null; this.seen = null; this.pulse = null; this.note = null; this.undoNote = null; this.drag = null;
    this.walk = { step: 0, playing: false }; this.mode = "values";
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
      if (s.ok) {
        // The services answer with the newest session on the model. When an agent opens one, the page moves to
        // it: its record replaces the old one, nothing in it pulses as new, and the page listens to its topic.
        if (this.session && this.session.id !== s.data.id) { this.seen = null; this.readFor = null; this.openEdit = null; this.test = null; this.stop?.(); this.stop = null; }
        this.session = s.data;
        this.noticeEvents();
      }
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
      if (this.net) this.readAt = Date.now();
      cx.notify();
      if (!this.net) return;
      // The test set's cost. A network that carries its test set is scored through the adapter each time. One that
      // does not (the services keep the test set to themselves) takes the number from its last edit, and asks
      // once when there is none.
      // An evaluation measures the numbers as they are, so its number counts too: the page asks for one only
      // when the record holds none since the last change.
      const last = (this.session?.events ?? []).filter((e) => ["edit", "undo", "redo", "evaluate"].includes(e.kind)).at(-1)?.cost?.test;
      if (!this.net.test?.length && last && Number.isFinite(last.after)) this.test = { cost: last.after, right: last.right_after ?? null, of: last.of ?? null };
      else if (this.net.test?.length || !this.test) {
        const t = await readTestCost(model, this.session?.id);
        if (mine !== this.reads || model !== this.model) return;
        if (t.ok) this.test = t.data;
      }
      cx.notify();
    });
  }

  /** Events that arrived since the last read make what they touched pulse. The first read pulses nothing: it is
   *  history, not something happening now. */
  noticeEvents() {
    const events = this.session?.events ?? [];
    const last = events.reduce((m, e) => Math.max(m, e.seq ?? 0), 0);
    if (this.seen != null && last > this.seen) {
      const neurons = new Set(), weights = new Set();
      for (const e of events) {
        if ((e.seq ?? 0) <= this.seen) continue;
        for (const [k, j] of e.touched?.neurons ?? []) neurons.add(`${k}:${j}`);
        for (const [k, j, i] of e.touched?.weights ?? []) weights.add(`${k}:${j}:${i}`);
      }
      if (neurons.size || weights.size) {
        this.pulse = { neurons, weights, left: PULSE_BEATS, phase: 1 };
        this.pulsed = { neurons, weights }; // what pulsed last, kept after the pulse ends
        this.beat(this.cx);
      }
    }
    this.seen = last;
  }

  /** The pulse's clock. It only animates: each beat moves the pulse's phase and arms the next beat, and when the
   *  pulse is over nothing is armed, so no timer runs while nothing pulses. It reads no data. */
  beat(cx) {
    const mine = ++this.beats;
    cx.timer.after(PULSE_BEAT_MS, (cx) => {
      if (mine !== this.beats || !this.pulse) return; // an older beat, or the pulse was replaced or dropped
      this.pulse.left -= 1;
      if (this.pulse.left <= 0) this.pulse = null;
      else { this.pulse.phase = 0.5 + 0.5 * Math.sin(this.pulse.left / 1.2); this.beat(cx); }
      cx.notify();
    });
  }

  /** Take an edit back, then read again. */
  undo(id, cx) {
    if (this.busy || !this.session) return;
    this.busy = id;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await sendUndo(this.session.id, id, "you");
      this.busy = null;
      this.undoNote = r.ok ? null : plainError(r.error, "Captain couldn't take that edit back.");
      this.read(cx);
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
    if (!this.drag) this.syncInput();
  }

  rerun() {
    const e = this.net?.examples?.[this.inputAt];
    this.run = e ? forward(this.net, e.x) : null;
    // Worked out here from the numbers the page already has: looking never spends an agent's allowance.
    this.grad = e && this.wantsGrad() ? backward(this.net, e.x, e.y, this.run) : null;
  }

  /** Whether anything on screen needs the gradients. */
  wantsGrad() { return this.mode === "nudges" || this.walk.step > 0; }

  /** Move the backward walk one step (dir 1) or back one (dir -1). The first step picks the weight with the
   *  largest slope into the output when none is picked, so its chain rule is on screen from the start. */
  stepWalk(dir, cx) {
    if (!this.net) return;
    const of = stepsOf(this.net), step = Math.max(0, Math.min(of, this.walk.step + dir));
    if (step === this.walk.step && dir > 0) { this.walk.playing = false; return cx.notify(); }
    this.walk.step = step;
    if (step === 0) this.walk.playing = false;
    if (step >= of) this.walk.playing = false;
    if (step > 0) this.mode = "values";
    this.rerun();
    if (step > 0 && !this.selW && this.grad) { const at = steepestWeight(this.grad, this.net.layers.length - 1); this.selW = at; this.sel = { k: at.k + 1, j: at.j }; }
    cx.notify();
  }
  /** Play from the start (or pause). */
  playWalk(cx) {
    if (this.walk.playing) { this.walk.playing = false; return cx.notify(); }
    if (this.walk.step >= stepsOf(this.net)) this.walk.step = 0;
    this.walk.playing = true;
    this.stepWalk(1, cx);
  }
  stopWalk(cx) { this.walk = { step: 0, playing: false }; this.rerun(); cx.notify(); }
  setMode(mode, cx) { this.mode = mode; if (mode === "nudges") this.walk = { step: 0, playing: false }; this.rerun(); cx.notify(); }

  pickInput(n, cx) { this.inputAt = n; this.rerun(); cx.notify(); }
  pickNeuron(at, cx) { this.sel = this.sel && this.sel.k === at.k && this.sel.j === at.j ? null : at; this.selW = null; this.syncInput(); cx.notify(); }
  pickWeight(at, cx) { this.selW = at; this.sel = { k: at.k + 1, j: at.j }; this.syncInput(); cx.notify(); }

  /** The typed value starts as the number it would change. */
  syncInput() { const t = handTarget(this); this.note = null; if (t) this.handInput.set_value(String(Number(t.value.toFixed(4)))); }

  /** Send one change by hand through the adapter, then read again: it lands in the timeline like the agent's. */
  commit(req, cx, undoLocal = null) {
    if (this.busy || !this.session) return;
    this.busy = "hand"; this.note = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await sendEdit(this.session.id, { ...req, target: this.inputAt, by: "you" });
      this.busy = null;
      if (!r.ok) { undoLocal?.(); this.rerun(); this.note = plainError(r.error, "Captain couldn't make that change."); }
      this.read(cx);
      cx.notify();
    });
  }

  /** Set the picked number to the value typed. */
  setTyped(cx) {
    const t = handTarget(this), text = String(this.handInput.value() ?? "").trim(), value = Number(text);
    if (!t) return;
    if (!text || !Number.isFinite(value)) { this.note = "Type a number, such as -1.5."; return cx.notify(); }
    if (value === t.value) return;
    this.commit({ op: "set", layer: t.k + 1, neuron: t.j, from: t.i, value }, cx);
  }

  /** Halve, double or zero the open neuron. */
  editNeuron(what, cx) {
    if (!this.sel || this.sel.k === 0) return;
    this.commit({ ...what, layer: this.sel.k, neuron: this.sel.j }, cx);
  }

  /** Dragging a number: the page's own copy of the network follows the pointer, so the drawing and the cost for
   *  this input move at once. The edit is sent when the drag ends. */
  dragStart(x, cx) { const t = handTarget(this); if (!t || this.busy) return; this.drag = { x0: x, from: t.value, k: t.k, j: t.j, i: t.i }; cx.notify(); }
  put(d, value) { if (d.i == null) this.net.layers[d.k].b[d.j] = value; else this.net.layers[d.k].W[d.j][d.i] = value; }
  dragMove(x, cx) {
    const d = this.drag;
    if (!d) return;
    this.put(d, Number((d.from + ((x - d.x0) / TRACK_W) * dragSpan(d.from)).toFixed(3)));
    this.rerun();
    cx.notify();
  }
  dragEnd(cx) {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    const value = d.i == null ? this.net.layers[d.k].b[d.j] : this.net.layers[d.k].W[d.j][d.i];
    this.syncInput();
    if (value === d.from) return cx.notify();
    this.commit({ op: "set", layer: d.k + 1, neuron: d.j, from: d.i, value }, cx, () => this.put(d, d.from));
  }

  /** Start an agent on one of the three tasks, with what is selected written into the prompt. */
  ask(starter, cx) { cx.spawn(async (cx) => { await askAgent(cx, starter.prompt); }); }
  back(cx) { this.stop?.(); this.stop = null; this.model = null; this.net = null; this.session = null; this.problem = null; this.sel = null; this.selW = null; this.loadList(cx); cx.notify(); }

  /** What the drawing needs to know. */
  drawing() {
    return { net: this.net, run: this.run, grad: this.grad, sel: this.sel, selW: this.selW, mode: this.mode, walkedFrom: walkedFrom(this.net, this.walk.step), pulse: this.pulse };
  }

  /** The top of the page: where this is (with the way back), the network in one line, how much of it has
   *  changed, and what the session's record says was allowed and used. */
  header(c) {
    const net = this.net;
    const facts = ledgerFacts(this.session?.ledger);
    const starterModel = String(this.model ?? "").startsWith("starter:");
    const live = !net || starterModel ? "" : liveState().state === "live" ? "Live" : updatedWords(this.readAt);
    return v_flex().id("wt-head").flex_none().px(20).pt(12).pb(10).gap(3).border_b(1).border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(div().id("wt-back").flex_none().text_size(15).font_semibold().text_color(this.model ? c.blue : c.navy)
          .when(Boolean(this.model), (e) => e.cursor_pointer().tooltip("Back to the list of networks").on_click((_e, cx) => this.back(cx)))
          .child("Weights"))
        .when(Boolean(this.model), (e) => e
          .child(svg(icon("chevron-right")).size(13).flex_none().text_color(c.grey500))
          .child(div().id("wt-title").min_w(0).truncate().text_size(15).font_semibold().child(net?.name ?? String(this.model).replace(/^starter:/, ""))))
        .child(div().flex_1())
        .when(Boolean(net), (e) => e
          .child(SendToAgent(c, "wt-send", selectionPin(this)))
          .child(Button(c, "wt-ask", this.askOpen ? "Hide what to ask" : "Ask an agent", (_e, cx) => { this.askOpen = !this.askOpen; cx.notify(); }, { small: true })))
        .when(Boolean(live), (e) => e.child(div().id("wt-live").flex_none().text_size(11.5).text_color(c.grey500).child(live))))
      .when(Boolean(net), (e) => e
        .child(h_flex().gap(10).items_center()
          .child(div().id("wt-facts").min_w(0).truncate().text_size(12.5).text_color(c.grey500).child(factsOf(net)))
          .when(Boolean(facts.changed), (x) => x.child(div().id("wt-ledger-changed").flex_none().text_size(12.5).child(facts.changed))))
        .when(facts.allowed.length > 0 || facts.alone, (x) => x.child(h_flex().gap(8).items_center()
          .when(facts.alone, (y) => y.child(div().id("wt-alone").flex_none().px(6).h(20).flex().items_center().bg(c.goodLight).text_color(c.good).text_size(11.5).font_medium()
            .tooltip("The record shows no sandbox was allowed and the network was never run").child("From the weights alone")))
          .child(div().id("wt-ledger").flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).tooltip(`${facts.allowed.join(". ")}.`).child(`${facts.allowed.join(". ")}.`)))));
  }

  /** The three things to ask an agent, written around what is selected. Shown when "Ask an agent" is pressed. */
  askStrip(c) {
    return h_flex().id("wt-agent").flex_none().px(20).py(8).gap(6).items_center().flex_wrap().border_b(1).border_color(c.rule).bg(c.surface2)
      .children(starters(this).map((s) => Button(c, `wt-ask-${s.id}`, s.label, (_e, cx) => this.ask(s, cx), { small: true, disabled: Boolean(s.needs) })))
      .child(div().id("wt-agent-about").min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(`It will be told about ${selectionPin(this).label.replace(/^[^:]*: /, "")}.`));
  }

  /** Nothing open: the networks that can be opened. */
  chooser(c) {
    const rows = this.list ?? [];
    return v_flex().id("wt-chooser").flex_1().min_h(0).px(20).py(24).gap(14)
      .child(div().max_w(560).text_size(12.5).line_height("19px").text_color(c.grey500)
        .child("Open a network to see inside it: every neuron, weight and bias is drawn, and what an agent reads or changes shows as it happens."))
      .when(!this.list, (e) => e.child(div().max_w(560).child(Skeleton(c, 2))))
      .child(v_flex().max_w(560).border_1().border_color(c.rule).bg(c.surface).children(rows.map((m, n) => h_flex().id(`wt-open-${m.id}`).px(14).py(12).gap(12).items_center()
        .when(n > 0, (e) => e.border_t(1).border_color(c.rule))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(12.5).font_medium().child(m.name))
          .when(Boolean(m.says), (e) => e.child(div().text_size(12.5).line_height("17px").text_color(c.grey500).child(m.says))))
        .child(Button(c, `wt-open-btn-${m.id}`, `Open ${m.name}`, (_e, cx) => this.open(m.id, cx), { small: true })))))
      // Without the services there is only the tiny network: say where the bigger one comes from.
      .when(Boolean(this.list) && !this.fromServices, (e) => e.child(div().id("wt-needs-services").max_w(560).text_size(11.5).line_height("16px").text_color(c.grey500)
        .child("The digit reader (784 inputs, 13,002 numbers) needs Captain's services, which are not answering. It shows here when they do.")));
  }

  /** The inputs a person can choose, as a row of picks: ten at a time, with a step to the ones before and after. */
  inputs(c) {
    const ex = this.net.examples, PAGE = 10;
    const start = Math.floor(this.inputAt / PAGE) * PAGE, shown = ex.slice(start, start + PAGE);
    const step = (id, text, to) => div().id(id).h(26).px(9).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(c.grey300).bg(c.surface).text_color(c.navy).hover((s) => s.border_color(c.blue).text_color(c.blue))
      .on_click((_e, cx) => this.pickInput(to, cx)).child(text);
    return h_flex().id("wt-inputs").h(44).flex_none().px(20).gap(6).items_center()
      .child(div().flex_none().pr(4).text_size(12.5).text_color(c.grey500).child(ex.length > PAGE ? `Input ${int(this.inputAt + 1)} of ${int(ex.length)}` : "Input"))
      .child(h_flex().flex_1().min_w(0).gap(6).overflow_x_scrollbar()
        .when(start > 0, (e) => e.child(step("wt-in-earlier", "Earlier inputs", start - 1)))
        .children(shown.map((e, m) => {
          const n = start + m, on = n === this.inputAt;
          return div().id(`wt-in-${n}`).h(26).px(9).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
            .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
            .on_click((_e, cx) => this.pickInput(n, cx)).child(e.name);
        }))
        .when(start + PAGE < ex.length, (e) => e.child(step("wt-in-later", "Later inputs", start + PAGE))))
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

  /** The neuron panel, beside the drawing. On a narrow page it is there only while a neuron is open, with a way
   *  to close it, so the drawing keeps its room. */
  side(c, w, narrow) {
    return div().id("wt-side").w(w).flex_none().h_full().overflow_y_scroll().child(v_flex().p(14).gap(12)
      .when(narrow, (e) => e.child(h_flex().child(div().flex_1()).child(div().id("wt-side-close").text_size(11.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
        .on_click((_e, cx) => { this.sel = null; this.selW = null; cx.notify(); }).child("Close this neuron"))))
      .child(Inspector(c, this, [ChainBlock(c, this), HandBlock(c, this)])));
  }

  render() {
    const c = palette(isDark(theme()));
    const net = this.net, W = this.width, H = this.height;
    const narrow = W < WIDE;
    const rail = narrow ? (this.sel ? Math.min(300, Math.round(W * 0.4)) : 0) : 340;
    const headH = net ? 84 : 46;
    const cw = Math.max(260, W - 40 - (rail ? rail + 1 : 0));
    const ch = Math.max(200, H - headH - 44 - (this.askOpen ? 42 : 0) - BAND_H - 14);
    const costW = Math.min(520, Math.max(300, Math.round((W - 40) * 0.45)));
    return v_flex().id("weights").size_full().bg(c.canvas).text_color(c.navy).font_family(FONT)
      .on_size_change((size, cx) => { if (Math.abs(size.width - this.width) > 1 || Math.abs(size.height - this.height) > 1) { this.width = size.width; this.height = size.height; cx.notify(); } })
      .child(this.header(c))
      .when(!this.model, (e) => e.child(this.chooser(c)))
      .when(Boolean(this.model) && !net && !this.problem, (e) => e.child(div().p(20).child(Skeleton(c, 6))))
      .when(Boolean(this.problem), (e) => e.child(v_flex().id("wt-problem").p(20).gap(10).items_start()
        .child(div().text_size(12.5).line_height("19px").text_color(c.fail).child(this.problem))
        .child(Button(c, "wt-retry", "Try again", (_e, cx) => this.open(this.model, cx), { small: true }))))
      .when(Boolean(net), (e) => e
        .when(this.askOpen, (x) => x.child(this.askStrip(c)))
        .child(this.inputs(c))
        .child(h_flex().flex_1().min_h(0)
          .child(div().pl(20).child(NetworkCanvas(c, this.drawing(), { id: "wt", pz: this.pz, layout: this.layout, width: cw, height: ch, extra: WalkControls(c, this), focus: this.sel, keep: this.keep,
            onNeuron: (at, cx) => this.pickNeuron(at, cx), onWeight: (at, cx) => this.pickWeight(at, cx) })))
          .when(rail > 0, (x) => x
            .child(div().w(1).h_full().flex_none().bg(c.rule))
            .child(this.side(c, rail, narrow))))
        .child(h_flex().id("wt-band").h(BAND_H).flex_none().border_t(1).border_color(c.rule).bg(c.surface)
          .child(div().pl(20).pt(10).child(CostPanel(c, this, { width: costW, height: BAND_H - 12 })))
          .child(div().w(16).flex_none())
          .child(div().w(1).h_full().flex_none().bg(c.rule))
          .child(div().pl(14).pt(10).child(Timeline(c, this, { width: Math.max(220, W - costW - 20 - 16 - 1 - 14 - 16), height: BAND_H - 22 })))));
  }
}
