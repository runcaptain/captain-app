// CAPTAIN: Weights: a model's network, drawn, so a person can watch what an agent reads and changes in it. Layers
// left to right, a neuron's fill for its activation on the chosen input, a weight as a blue or red line, a bias as
// a small number on its neuron (draw.js).
//
// It is a page in the window's main slot (the layout's "weights" block), beside the terminal where the agent
// runs: the whole point is watching the network while the agent edits it. It is the sidebar's Weights view.
// It opens from the sidebar, the palette ("Open a network's weights"), from `cst app open weights[:<model>]`, or from any
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
import { Button, Skeleton, Submenus, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int } from "./kit/num.js";
import { listNetworks, readNetwork, readSession, readTestCost, watchWeights, sendUndo, sendEdit, readDescription, isParts, partsFrom, weightsCanned } from "./adapter.js";
import { forward, backward, countOf } from "./net.js";
import { PanZoom } from "./canvas.js";
import { NetworkCanvas, layoutOf } from "./draw.js";
import { Inspector } from "./inspect.js";
import { CostPanel } from "./cost.js";
import { Timeline, ledgerFacts } from "./timeline.js";
import { HandBlock, handTarget, dragSpan, TRACK_W, selectionPin, starters, withStaged, stagedChanges, stagedRequest, stagedName } from "./hand.js";
import { askAgent } from "./kit/agent.js";
import { DeployLine, startDeploy } from "./deploy.js";
import { partsState, partsFacts, partsPin, openPart, loadPart, loadRanker, showView, PartPanel, RankerPanel, TreeBar, rankerOf, READ_ONLY } from "./parts.js";
import { PartsCanvas, TreeCanvas, partsLayout } from "./partsdraw.js";
import { AgentStrip, stripHeight } from "./program.js";
import { SaveLine, saveShows, SAVE_H } from "./save.js";
import { BringIn, list as listBringIn } from "./bringin.js";
import { stagePartTyped, putBackPart, leavePart } from "./parthand.js";
import { WalkButtons, walkWords, ChainBlock, walkedFrom, stepsOf, steepestWeight } from "./back.js";

const TIGHT = 640; // under this width nothing fits side by side: an open neuron takes the drawing's place, and the cost is one line
const WIDE = 1100; // from this width the neuron panel is always beside the drawing
const REPLACE = 520; // under this width an open neuron takes the drawing's place. From here to WIDE it sits under a shorter drawing, so both show

/** Where the drawing and the panel go on a page `W` wide with `ch` of height for the two: {mode, rail, cw, drawH,
 *  panelH}. "beside": the panel is always there, to the right. "under": while something is open the panel sits
 *  under a shorter drawing. "replace": while something is open the panel takes the drawing's place. */
export function splitOf(W, ch, open) {
  if (W >= WIDE) return { mode: "beside", rail: 340, cw: Math.max(200, W - 40 - 341), drawH: ch, panelH: ch };
  const cw = Math.max(200, W - 40);
  if (W < REPLACE) return { mode: "replace", rail: 0, cw, drawH: ch, panelH: ch };
  const drawH = open ? Math.max(150, Math.round(ch * 0.45)) : ch;
  return { mode: "under", rail: 0, cw, drawH, panelH: Math.max(0, ch - drawH) };
}
const PULSE_BEAT_MS = 120, PULSE_BEATS = 22; // touched neurons and weights pulse for about 2.6 seconds after an event
const BAND_H = 190; // the cost and the timeline, under the drawing
const count = (n) => int(n);

/** What to call an input a person can pick. A name the network gives in words is kept ("Both on"). A numbered
 *  one from the services ("practice 0: 9", "example 1: 7") reads like code, so it is said plainly, counting from
 *  1: "Picture 1: a 9" for a picture network, "Input 1" otherwise. `n` is its place in the list, from 0. */
export function inputName(net, e, n) {
  const m = /^(?:practice|example)\s+\d+(?::\s*(.+))?$/i.exec(String(e?.name ?? ""));
  if (e?.name && !m) return e.name;
  const what = net.inputShape ? "Picture" : "Input";
  const label = m?.[1];
  return label ? `${what} ${n + 1}: ${/^(8|11|18)/.test(label) ? "an" : "a"} ${label}` : `${what} ${n + 1}`;
}

/** "784 in, 10 out, 13,002 numbers": the network as a function, in one line. */
export function factsOf(net) {
  const n = countOf(net);
  // "Made-up data" is said only when the network says so of itself.
  return `${count(net.sizes[0])} in, ${count(net.sizes.at(-1))} out, ${count(n.total)} numbers (${count(n.weights)} weights and ${count(n.biases)} biases)${net.synthetic ? ". Made-up data" : ""}`;
}

export default class CaptainWeights extends View {
  init(_props, cx) {
    // For the picture check only, which can't click: props.show sets the page up as if a person had (a network, an
    // input, an open neuron, a picked weight, the mode, how far the walk has gone). Honoured only in the app's
    // test window (the app sets `app.testWindow` at each start): in a person's app a layout naming it does nothing.
    this.show = shared("app.testWindow") === true ? (props()?.show ?? null) : null;
    if (this.show?.answers) weightsCanned(this.show.answers); // the picture check's own answers, by address
    this.model = this.show?.model ?? props()?.model ?? null; // the network open, or null for the list
    this.kind = null;       // what the open model is, once known: "network" (drawn neuron by neuron) or "parts"
    this.pm = null;         // an open model of parts: its description and what is open in it (parts.js)
    this.treePz = null;     // the view of the ranker's tree that is drawn
    this.showLastEdit = this.show?.openEdit === "last";
    this.showDeploy = this.show?.deploy === true; // press "Deploy this version" once the network is read
    this.showInto = this.show?.into ?? null;     // and deploy into this project (kept here: `show` is cleared once staged)
    this.list = null;       // the networks that can be opened
    this.net = null;        // the open network (shapes.js Network)
    this.problem = null;    // why it could not be read, in a sentence
    this.servicesAnswered = false; // true once Captain's services have answered this page: until then it asks again
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
    this.drag = null;       // the dot being dragged: {x0, from}
    this.staged = null;     // a change by hand that is not saved: {kind: "value", k, j, i, value} or {kind: "neuron", k, j, op, factor} (hand.js)
    this.shown = null;      // the network as drawn: the open one, or the open one with the staged change while one waits
    this.dropped = null;    // one quiet sentence after a staged change was dropped by leaving it
    this.typo = null;       // what to type, while the box holds something that is not a number
    this.synced = "";       // the text the page last wrote into the box: the saved value
    this.handInput = InputState.new({ placeholder: "a number" }); // the value typed for the picked number
    // Typing stages the value. Enter does the same and no more: saving is a click on "Set this value".
    this.handInput.on("change", (_e, ecx) => this.stageTyped(ecx ?? cx));
    this.handInput.on("submit", (_e, ecx) => this.stageTyped(ecx ?? cx));
    this.handle = cx.focus_handle?.() ?? null; // so Escape reaches the page
    this.partInput = InputState.new({ placeholder: "a number" }); // the value typed for a trained model's picked number (parthand.js)
    this.partInput.on("change", (_e, ecx) => stagePartTyped(this, ecx ?? cx));
    this.partInput.on("submit", (_e, ecx) => stagePartTyped(this, ecx ?? cx)); // Enter only stages, as for a neuron
    this.whereInput = InputState.new({ placeholder: "a folder or a storage address" }); // where trained models are kept, typed once (bringin.js)
    this.nameInput = InputState.new({ placeholder: "a name" }); // the name for a model being brought in
    this.bring = null;      // the picker for bringing in a trained model, while it is open (bringin.js)
    this.goalInput = InputState.new({ placeholder: "What should the agent fix?" }); // the goal for an agent (program.js)
    this.agentRun = null;   // an agent started from here: {state, goal, job, says} (program.js)
    this.agentNote = null;  // why the agent could not be started or stopped
    this.saved = null;      // a version saved from here, or why it could not be: {state, text} (save.js)
    this.reads = 0;         // how many times the network has been asked for: an older answer never replaces a newer
    this.stop = null;       // stops listening to the change feed
    this.pz = new PanZoom("wt");
    this.width = 820; this.height = 760; // until the block says its size: about what is left beside the terminal
    this.askOpen = false;   // whether the three things to ask an agent are showing
    this.deploy = null;     // a deploy started from here: {state, project, ref, address, text} (deploy.js)
    this.asked = 0;         // the `at` of the last request from another screen that was handled
    this.readAt = 0;        // when the page last read
    if (this.model) this.open(this.model, cx); else this.loadList(cx);
    // For the picture check only (see `show` above): the picker open on a place, as if it had been kept.
    if (this.show?.bringFrom) listBringIn(this, cx, String(this.show.bringFrom));
    this.takeOpen(cx);
    // A link on another screen, the palette or `cst app open` cannot reach this page's props: it sets the shared
    // "open" value and selects this block. Looked at a few times a second, as the other screens do. Reads no data.
    cx.timer.every(300, (cx) => this.takeOpen(cx));
    // Until Captain's services first answer (they may still be starting when the page opens), ask again by itself.
    cx.timer.every(3000, (cx) => { if (!this.servicesAnswered) { if (this.model) this.read(cx); else this.loadList(cx); } });
    // The walk's clock: one step a beat while it plays. It moves the picture only, and reads nothing.
    cx.timer.every(1200, (cx) => { if (this.walk.playing) this.stepWalk(1, cx); });
    this.cx = cx;
  }

  loadList(cx) {
    cx.spawn(async (cx) => { const r = await listNetworks(); this.list = r.data.networks; this.fromServices = r.data.fromServices; if (r.data.fromServices) this.servicesAnswered = true; cx.notify(); });
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
    this.leave();
    this.kind = null; this.pm = null; this.treePz = null; this.agentRun = null; this.agentNote = null; this.saved = null;
    this.model = model; this.net = null; this.problem = null; this.sel = null; this.selW = null; this.inputAt = 0;
    this.session = null; this.test = null; this.openEdit = null; this.readFor = null; this.seen = null; this.pulse = null; this.note = null; this.undoNote = null; this.drag = null;
    this.walk = { step: 0, playing: false }; this.mode = "values";
    this.stopDeploy?.(); this.stopDeploy = null; this.deploy = null;
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
      // First, what kind of model this is. A model of parts is read as a description and never through a
      // session: nothing is opened on it and nothing is counted. A network small enough to draw goes on below.
      if (this.kind !== "network") {
        const d = await readDescription(model);
        if (mine !== this.reads || model !== this.model) return;
        if (d.ok && isParts(d.data)) return this.takeParts(partsFrom(d.data), cx);
        if (this.kind === "parts") return cx.notify(); // it could not be read again: what is on screen stays
        if (d.ok) this.kind = "network";
      }
      const s = await readSession(model);
      if (mine !== this.reads || model !== this.model) return;
      // No session, no numbers: say why in the services' own sentence when it is one.
      if (!s.ok && !this.net) { this.problem = plainError(s.error, "Captain couldn't read that network."); return cx.notify(); }
      // For the picture check (see `show` in init): open the last edit in the list, as a click on its line would.
      if (s.ok && this.showLastEdit) { const last = (s.data.events ?? []).filter((e) => e.kind === "edit").at(-1); if (last) this.openEdit = last.id; }
      if (s.ok) {
        this.servicesAnswered = true; this.problem = null;
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
      if (this.showDeploy && this.net && this.session) { this.showDeploy = false; startDeploy(this, cx, this.showInto); }
      cx.notify();
      if (!this.net) return;
      // The test set's cost. A network kept in this mod is scored through the adapter. For the services' networks
      // the number comes from the record: the last edit, undo or evaluation that carries one. A blind session's
      // record hides its scores until it is saved or closed, and the page says so.
      const scored = (this.session?.events ?? []).filter((e) => ["edit", "undo", "redo", "evaluate"].includes(e.kind) && (e.cost?.test || e.scores === "hidden")).at(-1);
      const last = scored?.cost?.test;
      if (this.net.test?.length) {
        const t = await readTestCost(model);
        if (mine !== this.reads || model !== this.model) return;
        if (t.ok) this.test = t.data;
      } else if (last && Number.isFinite(last.after)) this.test = { cost: last.after, right: last.right_after ?? null, of: last.of ?? null };
      else {
        // No score in the record. While nothing has been changed, the network is the version the session opened
        // on, and the services say how that version does on its test set (a fact about the model, not a score
        // shown to anyone). Once numbers have changed, that fact is about another network: say what is known.
        const held = this.session?.sets?.held_out, base = held?.base;
        if (base && Number.isFinite(base.cost) && this.session?.ledger?.changed === 0) this.test = { cost: base.cost, right: base.right ?? null, of: base.of ?? held.of ?? null };
        else this.test = { unknown: true, hidden: scored?.scores === "hidden", of: held?.of ?? null };
      }
      cx.notify();
    });
  }

  /** A model of parts, read: keep what is open in it, and listen for the model changing. */
  takeParts(model, cx) {
    this.kind = "parts"; this.servicesAnswered = true; this.problem = null;
    if (this.pm) this.pm.model = model; else { this.pm = partsState(model); this.pz = new PanZoom("wp"); }
    this.partsLayout = partsLayout(model);
    if (this.pm.sel && !model.parts.some((p) => p.id === this.pm.sel)) this.pm.sel = null;
    if (!this.stop) this.stop = watchWeights(cx, this.model, null, (cx) => this.read(cx));
    this.readAt = Date.now();
    // For the picture check (see `show` in init): open a part, a head, a row, or the rankers.
    if (this.show) {
      const w = this.show; this.show = null;
      if (w.part) { openPart(this, w.part, w.head ?? null, cx); if (Number.isInteger(w.row)) this.pm.row = w.row; if (Number.isFinite(w.stagedCell)) this.pm.showStage = w.stagedCell; }
      if (w.rankers) { this.pm.view = "rankers"; this.pm.rankerAt = w.ranker ?? 0; this.pm.treeAt = w.tree ?? 0; }
    }
    if (this.pm.sel) loadPart(this, cx);
    if (this.pm.view === "rankers") loadRanker(this, cx);
    this.readPartsSession(cx);
    cx.notify();
  }

  /** The record of the session the page looks at a trained model through: what was changed in it, and whether
   *  that is saved as a version. Read when the model is, after the page's own edit, and when the feed says so. */
  readPartsSession(cx) {
    const pm = this.pm, model = this.model;
    cx.spawn(async (cx) => {
      const s = await readSession(model);
      if (this.pm !== pm || !s?.ok || !s.data?.id) return;
      const moved = pm.session !== s.data.id;
      this.session = s.data; pm.session = s.data.id;
      // Listen to the session's own topic too, once it is known.
      if (moved) { this.stop?.(); this.stop = watchWeights(cx, model, s.data.id, (cx) => this.read(cx)); }
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

  inputName(e, n) { return inputName(this.net, e, n); }

  /** Show an edit: its three numbers under the cost, and its line open in the timeline. */
  openTimeline(id, cx) { this.openEdit = this.openEdit === id ? null : id; cx.notify(); }

  /** Set the page up as the picture check asks (see `show` in init). */
  stage(w) {
    if (Number.isInteger(w.input)) this.inputAt = Math.min(w.input, this.net.examples.length - 1);
    if (Array.isArray(w.neuron)) this.sel = { k: w.neuron[0], j: w.neuron[1] };
    if (Array.isArray(w.weight)) { this.selW = { k: w.weight[0], j: w.weight[1], i: w.weight[2] }; this.sel = { k: w.weight[0] + 1, j: w.weight[1] }; }
    if (w.mode === "nudges") this.mode = "nudges";
    if (w.ask) this.askOpen = true;
    this.syncInput();
    const t = handTarget(this);
    if (t && Number.isFinite(w.stagedValue)) { this.staged = { kind: "value", k: t.k, j: t.j, i: t.i, value: w.stagedValue }; this.handInput.set_value(String(w.stagedValue)); }
    if (this.sel?.k > 0 && w.stagedNeuron) this.staged = { kind: "neuron", k: this.sel.k - 1, j: this.sel.j, ...w.stagedNeuron };
    this.rerun();
    for (let n = 0; n < (w.walk ?? 0); n++) this.stepWalk(1, { notify() {} });
  }

  /** New numbers for the open network: keep the input and the selection, and run it again. */
  take(net) {
    this.net = net;
    this.layout = layoutOf(net);
    this.inputAt = Math.min(this.inputAt, Math.max(0, net.examples.length - 1));
    this.rerun();
    // With a change staged the box keeps what the person typed. Only the saved value it is compared with moves.
    if (!this.staged) this.syncInput(); else { const t = handTarget(this); if (t) this.synced = String(Number(t.value.toFixed(4))); }
    if (this.show) { const w = this.show; this.show = null; this.stage(w); }
  }

  rerun() {
    const e = this.net?.examples?.[this.inputAt];
    // While a change by hand waits to be saved, the drawing and this input's cost show the network as it would be.
    // The open network's own numbers are not touched.
    const net = this.shown = this.net && this.staged ? withStaged(this.net, this.staged) : this.net;
    this.run = e ? forward(net, e.x) : null;
    // Worked out here from the numbers the page already has: looking never spends an agent's allowance.
    this.grad = e && this.wantsGrad() ? backward(net, e.x, e.y, this.run) : null;
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
    // Beside a terminal the neuron panel would take half the drawing, so there the walk opens nothing by itself.
    if (step > 0 && !this.selW && !this.staged && this.grad && this.width >= WIDE) { const at = steepestWeight(this.grad, this.net.layers.length - 1); this.selW = at; this.sel = { k: at.k + 1, j: at.j }; this.syncInput(); }
    cx.notify();
  }
  /** Play from the start (or pause). */
  playWalk(cx) {
    if (this.walk.playing) { this.walk.playing = false; return cx.notify(); }
    if (this.walk.step >= stepsOf(this.net)) this.walk.step = 0;
    this.walk.playing = true;
    this.stepWalk(1, cx);
  }
  /** Picking what the lines show also leaves the backward walk. */
  setMode(mode, cx) { this.mode = mode; this.walk = { step: 0, playing: false }; this.rerun(); cx.notify(); }

  pickInput(n, cx) { this.leave(); this.inputAt = n; this.rerun(); cx.notify(); }
  pickNeuron(at, cx) { this.leave(); this.sel = this.sel && this.sel.k === at.k && this.sel.j === at.j ? null : at; this.selW = null; this.syncInput(); this.rerun(); cx.notify(); }
  pickWeight(at, cx) { this.leave(); this.selW = at; this.sel = { k: at.k + 1, j: at.j }; this.syncInput(); this.rerun(); cx.notify(); }
  closeNeuron(cx) { this.leave(); this.sel = null; this.selW = null; this.rerun(); cx.notify(); }

  /** The box starts as the number it would change. */
  syncInput() { const t = handTarget(this); this.note = null; this.typo = null; if (t) { this.synced = String(Number(t.value.toFixed(4))); this.handInput.set_value(this.synced); } }

  /** Going somewhere else (another neuron, another input, another network, the list) with a change not saved
   *  drops it, and one quiet line says so. Nothing is ever saved by leaving. */
  leave() {
    const name = stagedName(this);
    this.dropped = name ? `Your unsaved change to ${name} was dropped. Nothing was saved.` : null;
    this.staged = null; this.drag = null; this.typo = null;
    // A trained model's staged change is dropped the same way, and the list of networks says so.
    if (this.pm?.staged) { leavePart(this); this.dropped = this.pm.dropped; }
  }

  /** Stage what is typed in the box. It changes nothing in the network: "Set this value" does. */
  stageTyped(cx) {
    const t = handTarget(this), text = String(this.handInput.value() ?? "").trim(), value = Number(text);
    if (!t || this.busy) return;
    this.dropped = null; this.note = null;
    // The page's own text (the saved value), or the saved value typed again: nothing is staged.
    if (text === this.synced || (text && value === t.value)) { this.typo = null; if (this.staged?.kind === "value") { this.staged = null; this.rerun(); } return cx.notify(); }
    if (!text || !Number.isFinite(value)) { this.typo = "Type a number, such as -1.5."; if (this.staged?.kind === "value") { this.staged = null; this.rerun(); } return cx.notify(); }
    this.typo = null;
    this.staged = { kind: "value", k: t.k, j: t.j, i: t.i, value };
    this.rerun();
    cx.notify();
  }

  /** Stage halving, doubling or zeroing the open neuron. */
  stageNeuron(what, cx) {
    if (!this.sel || this.sel.k === 0 || this.busy) return;
    this.dropped = null; this.note = null;
    this.syncInput();
    this.staged = { kind: "neuron", k: this.sel.k - 1, j: this.sel.j, ...what };
    this.handle?.focus?.();
    this.rerun();
    cx.notify();
  }

  /** Drop the staged change: the drawing, the cost and the box go back to the saved numbers. */
  putBack(cx) {
    if (this.busy) return;
    this.staged = null; this.drag = null;
    this.syncInput();
    this.rerun();
    cx.notify();
  }

  /** Save the staged change as one edit through the adapter, then read again: it lands in the timeline like the
   *  agent's. This is the only place the page changes a network by hand. A refusal leaves the change staged. */
  saveStaged(cx) {
    const s = this.staged;
    if (!s || this.busy || !this.session) return;
    this.busy = "hand"; this.note = null; this.drag = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const e = this.net.examples[this.inputAt];
      const r = await sendEdit(this.session.id, { ...stagedRequest(s), aim: e ? { index: this.inputAt, x: e.x, y: e.y } : undefined, by: "you" });
      this.busy = null;
      if (!r.ok) this.note = plainError(r.error, "Captain couldn't make that change.");
      else if (this.staged === s) {
        // The page's copy takes the saved numbers at once, so the drawing does not jump back while it reads again.
        for (const c of stagedChanges(this.net, s)) { if (c.i == null) this.net.layers[c.k].b[c.j] = c.value; else this.net.layers[c.k].W[c.j][c.i] = c.value; }
        this.staged = null;
        this.syncInput();
        this.rerun();
      }
      this.read(cx);
      cx.notify();
    });
  }

  /** Dragging the dot stages a value as it moves: the drawing and the cost for this input follow it, marked as not
   *  saved. Letting go sends nothing. */
  dragStart(x, cx) {
    const t = handTarget(this);
    if (!t || this.busy) return;
    this.drag = { x0: x, from: this.staged?.kind === "value" ? this.staged.value : t.value };
    this.handle?.focus?.();
    cx.notify();
  }
  dragMove(x, cx) {
    const d = this.drag, t = handTarget(this);
    if (!d || !t) return;
    const value = Number((d.from + ((x - d.x0) / TRACK_W) * dragSpan(t.value)).toFixed(3));
    this.dropped = null; this.typo = null;
    if (value !== t.value) this.staged = { kind: "value", k: t.k, j: t.j, i: t.i, value };
    else if (this.staged?.kind === "value") this.staged = null;
    this.handInput.set_value(this.staged?.kind === "value" ? String(value) : this.synced);
    this.rerun();
    cx.notify();
  }
  dragEnd(cx) { if (!this.drag) return; this.drag = null; cx.notify(); }

  /** Escape puts a staged change back. */
  key(e, cx) {
    if ((e?.key ?? e?.keystroke) !== "escape") return;
    if (this.pm?.staged) { putBackPart(this, cx); return cx.stop_propagation?.(); }
    if (!this.staged) return;
    this.putBack(cx);
    cx.stop_propagation?.();
  }

  /** Start an agent on one of the three tasks, with what is selected written into the prompt. */
  ask(starter, cx) { cx.spawn(async (cx) => { await askAgent(cx, starter.prompt); }); }
  back(cx) { this.leave(); this.stop?.(); this.stop = null; this.kind = null; this.pm = null; this.model = null; this.net = null; this.session = null; this.problem = null; this.sel = null; this.selW = null; this.loadList(cx); cx.notify(); }

  /** What the drawing needs to know. */
  drawing() {
    return { net: this.shown ?? this.net, run: this.run, grad: this.grad, sel: this.sel, selW: this.selW, mode: this.mode, walkedFrom: walkedFrom(this.net, this.walk.step), pulse: this.pulse };
  }

  /** The top of the page: where this is (with the way back), the network in one line, how much of it has
   *  changed, and what the session's record says was allowed and used. */
  header(c) {
    const net = this.net, pm = this.pm;
    const facts = ledgerFacts(this.session?.ledger);
    // "From the weights alone" is a claim about work done. On a session where nothing has been edited there is
    // no work to make it about, so it waits for the first edit: before that the line only says what is allowed.
    const edited = (this.session?.edits?.length ?? 0) > 0 || (this.session?.events ?? []).some((e) => e.kind === "edit");
    facts.alone = facts.alone && edited;
    const starterModel = String(this.model ?? "").startsWith("starter:");
    // "Live" once the page is listening on the change feed, for a network and a trained model alike. The feed's
    // first answer does not draw the page again, so waiting for it left a model read once saying "Updated just now".
    const feed = liveState().state;
    const live = !(net || pm) || starterModel ? "" : feed === "reconnecting" ? "Reconnecting" : this.stop ? "Live" : updatedWords(this.readAt);
    return v_flex().id("wt-head").flex_none().px(20).pt(12).pb(10).gap(3).border_b(1).border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(div().id("wt-back").flex_none().text_size(15).font_semibold().text_color(this.model ? c.blue : c.navy)
          .when(Boolean(this.model), (e) => e.cursor_pointer().tooltip("Back to the list of networks").on_click((_e, cx) => this.back(cx)))
          .child("Weights"))
        .when(Boolean(this.model), (e) => e
          .child(svg(icon("chevron-right")).size(13).flex_none().text_color(c.grey500))
          .child(div().id("wt-title").min_w(0).truncate().text_size(15).font_semibold().child(net?.name ?? pm?.model.name ?? String(this.model).replace(/^starter:/, ""))))
        .child(div().flex_1())
        .when(Boolean(pm), (e) => e.child(SendToAgent(c, "wt-send", partsPin(this))))
        .when(Boolean(net), (e) => e
          .child(SendToAgent(c, "wt-send", selectionPin(this)))
          .child(Button(c, "wt-ask", this.askOpen ? "Hide what to ask" : "Ask an agent", (_e, cx) => { this.askOpen = !this.askOpen; cx.notify(); }, { small: true }))
          // The services' networks can be deployed. The tiny network kept in this page lives only while it is open.
          .when(!starterModel && Boolean(this.session), (x) => x.child(Button(c, "wt-deploy-go", "Deploy this version", (_e, cx) => startDeploy(this, cx),
            { small: true, disabled: this.deploy?.state === "asking" || this.deploy?.state === "running" }))))
        .when(Boolean(live), (e) => e.child(h_flex().flex_none().gap(5).items_center()
          .when(live === "Live", (x) => x.child(div().id("wt-live-dot").size(6).rounded_full().flex_none().bg(c.good)))
          .child(div().id("wt-live").flex_none().text_size(11.5).text_color(c.grey500).child(live)))))
      // A model of parts: what it is and how many numbers, then where it came from.
      .when(Boolean(pm), (e) => e
        .child(div().id("wt-facts").min_w(0).truncate().text_size(12.5).text_color(c.grey500).child(partsFacts(pm.model)))
        // Where it came from, in words. The address itself is never on screen: it can be copied.
        .when(Boolean(pm.model.source), (x) => x.child(h_flex().gap(10).items_center()
          .child(div().id("wt-source").flex_none().text_size(11.5).text_color(c.grey500).child(pm.model.source))
          .when(Boolean(pm.model.source_address), (y) => y.child(div().id("wt-source-copy").flex_none().text_size(11.5).text_color(c.blue).cursor_pointer().hover((st) => st.text_color(c.navy))
            .on_click((_e, cx) => { cx.write_to_clipboard?.(pm.model.source_address); pm.copied = true; cx.notify(); }).child(pm.copied ? "Copied where it came from" : "Copy where it came from"))))))
      .when(Boolean(net), (e) => e
        .child(h_flex().gap(10).items_center()
          .child(div().id("wt-facts").min_w(0).truncate().text_size(12.5).text_color(c.grey500).child(factsOf(net)))
          .when(Boolean(facts.changed), (x) => x.child(div().id("wt-ledger-changed").flex_none().text_size(12.5).child(facts.changed))))
        .when(facts.allowed.length > 0 || facts.alone, (x) => x.child(h_flex().gap(8).items_center()
          .when(facts.alone, (y) => y.child(div().id("wt-alone").flex_none().px(6).h(20).flex().items_center().bg(c.goodLight).text_color(c.good).text_size(11.5).font_medium()
            .tooltip("The record shows no sandbox was allowed and the network was never run").child("From the weights alone")))
          .child(div().id("wt-ledger").flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).tooltip(`${facts.allowed.join(". ")}.`).child(`${facts.short.join(". ")}.`)))));
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
      .when(Boolean(this.dropped), (e) => e.child(div().id("wt-dropped").max_w(560).text_size(11.5).line_height("16px").text_color(c.grey500).child(this.dropped)))
      .child(div().max_w(560).text_size(12.5).line_height("19px").text_color(c.grey500)
        .child("Open a network to see inside it: every neuron, weight and bias is drawn, and what an agent reads or changes shows as it happens."))
      .when(!this.list, (e) => e.child(div().max_w(560).child(Skeleton(c, 2))))
      .child(v_flex().max_w(560).border_1().border_color(c.rule).bg(c.surface).children(rows.map((m, n) => h_flex().id(`wt-open-${m.id}`).px(14).py(12).gap(12).items_center()
        .when(n > 0, (e) => e.border_t(1).border_color(c.rule))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(12.5).font_medium().child(m.name))
          // A trained model says its kind in words and how many numbers it has, then where it came from.
          .when(isParts(m), (e) => e.child(div().id(`wt-open-kind-${m.id}`).text_size(12.5).line_height("17px").text_color(c.grey500).child(partsFacts(m)))
            .when(Boolean(m.says), (x) => x.child(div().id(`wt-open-says-${m.id}`).text_size(11.5).line_height("16px").text_color(c.grey500).child(m.says))))
          .when(!isParts(m) && Boolean(m.says), (e) => e.child(div().text_size(12.5).line_height("17px").text_color(c.grey500).child(m.says))))
        .child(Button(c, `wt-open-btn-${m.id}`, `Open ${m.name}`, (_e, cx) => this.open(m.id, cx), { small: true })))))
      // A trained model kept somewhere else is brought in from here, when the services answer.
      .when(Boolean(this.list) && Boolean(this.fromServices), (e) => e.child(BringIn(c, this)))
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
            .on_click((_e, cx) => this.pickInput(n, cx)).child(inputName(this.net, e, n));
        }))
        .when(start + PAGE < ex.length, (e) => e.child(step("wt-in-later", "Later inputs", start + PAGE))));
  }

  /** The row above the drawing: what the lines show, and the backward walk. */
  controls(c) {
    return h_flex().id("wt-controls").flex_none().px(20).pb(8).gap(6).items_center().flex_wrap()
      .child(this.modes(c))
      // One quiet line: the drawing is showing a change that is not saved, or one was just dropped by leaving it.
      .child(div().id("wt-staged-says").flex_1().min_w(0).truncate().px(6).text_size(11.5).text_color(this.staged ? c.blue : c.grey500)
        .child(this.staged ? "Not saved: this is a preview." : this.dropped ?? ""))
      .child(WalkButtons(c, this));
  }

  /** What the lines show: the weights, or the way the cost wants each one moved. */
  modes(c) {
    const pick = (id, text, mode, tip) => { const on = this.mode === mode && this.walk.step === 0; return div().id(id).h(26).px(9).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).tooltip(tip)
      .on_click((_e, cx) => this.setMode(mode, cx)).child(text); };
    return h_flex().id("wt-modes").flex_none().gap(6).items_center()
      .child(div().flex_none().pr(4).text_size(12.5).text_color(c.grey500).child("Lines show"))
      .child(pick("wt-mode-values", "The weights", "values", "Each line is a weight: blue is positive, red is negative, thicker is larger"))
      .child(pick("wt-mode-nudges", "What the cost wants", "nudges", "Each line is the nudge the cost wants for that weight on this input (its gradient, turned around): blue is up, red is down"));
  }

  /** The neuron panel. Beside the drawing on a wide page. On a narrower one it is there only while a neuron is
   *  open, under the drawing (`h` is then its height) or in its place, with a way to close it. */
  side(c, w, narrow, h = null) {
    return div().id("wt-side").w(w).flex_none().when(h == null, (e) => e.h_full()).when(h != null, (e) => e.h(h).border_t(1).border_color(c.rule)).overflow_y_scroll().child(v_flex().p(14).gap(12)
      .when(narrow, (e) => e.child(h_flex().child(div().flex_1()).child(div().id("wt-side-close").text_size(11.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
        .on_click((_e, cx) => this.closeNeuron(cx)).child("Close this neuron"))))
      .child(Inspector(c, this, [ChainBlock(c, this), HandBlock(c, this)])));
  }

  /** A model of parts, under the header: the diagram of its parts with the open part beside it, or its rankers:
   *  one tree drawn, with the inputs by share of gain beside it. Nothing here edits or runs the model. */
  partsBody(c, W, H) {
    const pm = this.pm, narrow = W < WIDE;
    const rankers = pm.model.rankers ?? [], onRankers = pm.view === "rankers" && rankers.length > 0;
    const ch = Math.max(180, H - 84 - 44 - 14 - (saveShows(this) ? SAVE_H : 0));
    const sp = splitOf(W, ch, Boolean(pm.sel)), cw = sp.cw;
    // One quiet line: what can be done with this model here, or that a change is staged, or that one was dropped.
    const says = pm.staged ? "Not saved: the picture shows your change as a preview." : pm.dropped ?? pm.model.read_only ?? READ_ONLY;
    const strip = h_flex().id("wt-parts-bar").h(44).flex_none().px(20).gap(10).items_center()
      .when(rankers.length > 0, (e) => e.child(Submenus(c, [{ id: "model", label: "Model" }, { id: "rankers", label: "Rankers" }], onRankers ? "rankers" : "model", (id, cx) => showView(this, id, cx))))
      .child(div().id("wt-read-only").flex_1().min_w(0).truncate().text_size(11.5).text_color(pm.staged ? c.blue : c.grey500).child(says));
    const panel = (inner, close, w, h = null) => div().id("wt-side").w(w).flex_none().when(h == null, (e) => e.h_full()).when(h != null, (e) => e.h(h).border_t(1).border_color(c.rule)).overflow_y_scroll().child(v_flex().p(14).gap(12)
      .when(Boolean(close) && narrow, (e) => e.child(h_flex().child(div().flex_1()).child(div().id("wt-side-close").text_size(11.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
        .on_click((_e, cx) => { leavePart(this); pm.sel = null; cx.notify(); }).child("Close this part"))))
      .child(inner));
    const rule = div().w(1).h_full().flex_none().bg(c.rule);
    if (onRankers) {
      const r = rankerOf(this), tree = r?.shown?.[pm.treeAt] ?? null, waiting = !r && !pm.rankerProblem;
      this.treePz ??= new PanZoom("wr");
      // On a page too narrow for both side by side, the inputs sit over the tree.
      const half = Math.round(ch * 0.46), BAR = 36;
      const drawn = (w, h) => v_flex().w(w).child(TreeBar(c, this)).child(tree ? TreeCanvas(c, tree, { id: "wr", pz: this.treePz, width: w, height: h - BAR, open: pm.treeOpen,
        onOpen: (id, cx) => { pm.treeOpen = new Set(pm.treeOpen).add(id); cx.notify(); } })
        : div().id("wr-none").w(w).text_size(12.5).line_height("18px").text_color(c.grey500).child(waiting ? "Reading the ranker." : "No tree of this ranker was sent to draw."));
      return v_flex().flex_1().min_h(0).when(saveShows(this), (e) => e.child(SaveLine(c, this))).child(strip)
        .when(!narrow, (e) => e.child(h_flex().flex_1().min_h(0).child(div().pl(20).child(drawn(cw, ch))).child(rule).child(panel(RankerPanel(c, this, sp.rail - 30), false, sp.rail))))
        // Narrower: one column that scrolls, the inputs first and the tree under them, so neither hides the other.
        .when(narrow, (e) => e.child(div().id("wt-rankers-column").flex_1().min_h(0).overflow_y_scroll().child(v_flex().px(20).pb(14).gap(10)
          .child(RankerPanel(c, this, W - 40)).child(drawn(W - 40, Math.max(half, 320))))));
    }
    const canvas = (h) => div().pl(20).child(PartsCanvas(c, pm.model, { id: "wp", pz: this.pz, layout: this.partsLayout, width: cw, height: h, sel: pm.sel, head: pm.head,
      onPart: (id, head, cx) => openPart(this, id, head, cx) }));
    const body = sp.mode === "beside" ? h_flex().flex_1().min_h(0).child(canvas(ch)).child(rule).child(panel(PartPanel(c, this, sp.rail - 30), true, sp.rail))
      : sp.mode === "replace" ? h_flex().flex_1().min_h(0).child(pm.sel ? panel(PartPanel(c, this, W - 30), true, W) : canvas(ch))
        // Between: the open part sits under a shorter diagram, so both show.
        : v_flex().flex_1().min_h(0).child(canvas(sp.drawH)).when(Boolean(pm.sel), (e) => e.child(panel(PartPanel(c, this, W - 30), true, W, sp.panelH)));
    return v_flex().flex_1().min_h(0).when(saveShows(this), (e) => e.child(SaveLine(c, this))).child(strip).child(body);
  }

  render() {
    const c = palette(isDark(theme()));
    const net = this.net, W = this.width, H = this.height;
    const narrow = W < WIDE, tight = W < TIGHT;
    const served = Boolean(this.session) && !String(this.model ?? "").startsWith("starter:");
    const headH = net || this.pm ? 84 : 46;
    // While the error walks backward, a strip under the drawing says what the step shows. It is never laid over
    // the drawing, where it hid the network's last column.
    const walking = Boolean(net) && this.walk.step > 0 && Boolean(this.grad);
    const saysH = walking ? (W < 560 ? 76 : 58) : 0;
    const ch = Math.max(180, H - headH - 44 - (tight ? 72 : 38) - (this.askOpen ? (served ? stripHeight(W) : 42) : 0) - (this.deploy ? 34 : 0) - (saveShows(this) ? SAVE_H : 0) - BAND_H - 14 - saysH);
    // The neuron panel: always beside the drawing on a wide page; under a shorter drawing while a neuron is open
    // on a narrower one; and in the drawing's place only on a page too tight for both.
    const sp = splitOf(W, ch, Boolean(this.sel));
    const canvas = (h) => div().pl(20).child(NetworkCanvas(c, this.drawing(), { id: "wt", pz: this.pz, layout: this.layout, width: sp.cw, height: h, focus: this.sel, keep: this.keep,
      onNeuron: (at, cx) => this.pickNeuron(at, cx), onWeight: (at, cx) => this.pickWeight(at, cx) }));
    const costW = Math.min(520, Math.max(300, Math.round((W - 40) * 0.45)));
    return v_flex().id("weights").size_full().bg(c.canvas).text_color(c.navy).font_family(FONT)
      .when(Boolean(this.handle), (e) => e.track_focus(this.handle))
      .on_key_down((e, cx) => this.key(e, cx))
      .on_size_change((size, cx) => { if (Math.abs(size.width - this.width) > 1 || Math.abs(size.height - this.height) > 1) { this.width = size.width; this.height = size.height; cx.notify(); } })
      .child(this.header(c))
      .when(!this.model, (e) => e.child(this.chooser(c)))
      .when(Boolean(this.model) && !net && !this.pm && !this.problem, (e) => e.child(div().p(20).child(Skeleton(c, 6))))
      .when(Boolean(this.pm), (e) => e.child(this.partsBody(c, W, H)))
      .when(Boolean(this.problem), (e) => e.child(v_flex().id("wt-problem").p(20).gap(10).items_start()
        .child(div().text_size(12.5).line_height("19px").text_color(c.fail).child(this.problem))
        .child(Button(c, "wt-retry", "Try again", (_e, cx) => this.open(this.model, cx), { small: true }))))
      .when(Boolean(net), (e) => e
        .when(Boolean(this.deploy), (x) => x.child(DeployLine(c, this)))
        // Changes that are in no version yet, and the one button that saves them.
        .when(saveShows(this), (x) => x.child(SaveLine(c, this)))
        // A network the services hold: a goal for an agent they start. The one kept in this page: prompts to hand over.
        .when(this.askOpen, (x) => x.child(served ? AgentStrip(c, this) : this.askStrip(c)))
        .child(this.inputs(c))
        .child(this.controls(c))
        .child(sp.mode === "beside" ? h_flex().flex_1().min_h(0).child(canvas(ch)).child(div().w(1).h_full().flex_none().bg(c.rule)).child(this.side(c, sp.rail, false))
          : sp.mode === "replace" ? h_flex().flex_1().min_h(0).child(this.sel ? this.side(c, W, true) : canvas(ch))
            : v_flex().flex_1().min_h(0).child(canvas(sp.drawH)).when(Boolean(this.sel), (x) => x.child(this.side(c, W, true, sp.panelH))))
        .when(walking, (x) => x.child(div().id("wt-walk-says").h(saysH).flex_none().px(20).pt(6).overflow_hidden().text_size(12.5).line_height("18px")
          .child(walkWords(this.shown ?? net, this.run, this.grad, this.walk.step, net.examples[this.inputAt].y))))
        .when(!tight, (x) => x.child(h_flex().id("wt-band").h(BAND_H).flex_none().border_t(1).border_color(c.rule).bg(c.surface)
          .child(div().pl(20).pt(10).child(CostPanel(c, this, { width: costW, height: BAND_H - 12 })))
          .child(div().w(16).flex_none())
          .child(div().w(1).h_full().flex_none().bg(c.rule))
          .child(div().pl(14).pt(10).child(Timeline(c, this, { width: Math.max(220, W - costW - 20 - 16 - 1 - 14 - 16), height: BAND_H - 22 })))))
        .when(tight, (x) => x.child(v_flex().id("wt-band").h(BAND_H).flex_none().border_t(1).border_color(c.rule).bg(c.surface).px(20).pt(6).gap(4)
          .child(CostPanel(c, this, { width: W - 40, height: 28, compact: true }))
          .child(Timeline(c, this, { width: W - 40, height: BAND_H - 28 - 22 })))));
  }
}
