// CAPTAIN: changing the network by hand, and handing what is selected to an agent. With a neuron open: type or
// drag its bias, or a picked weight, to a new value; halve, double or zero the neuron. None of these changes the
// network. Each one stages a change: the drawing and the cost for the chosen input show what it would do, marked
// as not saved, and nothing is sent until the person presses the save button ("Set this value" or "Save this
// change"). "Put it back", Escape, or leaving the neuron drops it. A saved edit goes through adapter.js, lands in
// the timeline like the agent's, and can be taken back.
import { div } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { Button } from "./kit/ui.js";
import { int } from "./kit/num.js";
import { neuronName, signed, plain } from "./inspect.js";
import { answerOf } from "./net.js";

export const TRACK_W = 200; // the drag track, in pixels

/** The number the hand tools act on: the picked weight, else the open neuron's bias. {k: layer, j, i: from or null
 *  for the bias, value, name}. Null when nothing that has a number is open. */
export function handTarget(v) {
  const { net, sel, selW } = v;
  if (!net) return null;
  if (selW) return { k: selW.k, j: selW.j, i: selW.i, value: net.layers[selW.k].W[selW.j][selW.i], name: `the weight from ${neuronName(net, selW.k, selW.i)} into ${neuronName(net, selW.k + 1, selW.j)}` };
  if (sel && sel.k > 0) return { k: sel.k - 1, j: sel.j, i: null, value: net.layers[sel.k - 1].b[sel.j], name: `the bias of ${neuronName(net, sel.k, sel.j)}` };
  return null;
}

/** How far a full drag across the track moves a number: wide enough to cross zero and double it. */
export function dragSpan(value) { return Math.max(4, Math.abs(value) * 4); }

/** The numbers a staged change would set, worked out from the network as it is now: [{k, j, i, value}], `i` null
 *  for the bias. A staged change is {kind: "value", k, j, i, value} (one number) or {kind: "neuron", k, j, op,
 *  factor} (halve, double or zero the neuron j fed by layer k). */
export function stagedChanges(net, s) {
  const row = net?.layers?.[s?.k]?.W?.[s?.j];
  if (!row) return [];
  const { k, j } = s;
  if (s.kind === "value") return [{ k, j, i: s.i ?? null, value: s.value }];
  if (s.op === "scale") return row.map((w, i) => ({ k, j, i, value: w * s.factor }));
  if (s.op === "zero") return [...row.map((_w, i) => ({ k, j, i, value: 0 })), { k, j, i: null, value: 0 }];
  return [];
}

/** The network as it would be if the staged change were saved. The network itself is never touched: only the
 *  one row and the one bias list that would change are copied. */
export function withStaged(net, s) {
  const changes = stagedChanges(net, s);
  if (!changes.length) return net;
  const layers = net.layers.slice(), at = layers[s.k];
  const W = at.W.slice(), b = at.b.slice();
  W[s.j] = W[s.j].slice();
  for (const c of changes) { if (c.i == null) b[c.j] = c.value; else W[c.j][c.i] = c.value; }
  layers[s.k] = { ...at, W, b };
  return { ...net, layers };
}

/** The edit that saves a staged change, in the adapter's shape (shapes.js EditRequest). */
export function stagedRequest(s) {
  if (s.kind === "value") return { op: "set", layer: s.k + 1, neuron: s.j, from: s.i ?? null, value: s.value };
  return s.op === "scale" ? { op: "scale", factor: s.factor, layer: s.k + 1, neuron: s.j } : { op: "zero", layer: s.k + 1, neuron: s.j };
}

/** What a staged change would do, in one sentence. */
export function stagedWords(net, s, was) {
  if (s.kind === "value") return `Not saved. ${signed(was)} will become ${signed(s.value)}.`;
  const n = net.layers[s.k].W[s.j].length, weights = `${int(n)} ${n === 1 ? "weight" : "weights"} into this neuron`;
  if (s.op === "zero") return `Not saved. ${weights} and its bias will be set to zero.`;
  return `Not saved. ${weights} will be ${s.factor === 0.5 ? "halved" : s.factor === 2 ? "doubled" : `multiplied by ${s.factor}`}.`;
}

/** What to call a staged change once it is dropped, for the one quiet line that says so. */
export function stagedName(v) {
  const s = v.staged;
  if (!s) return null;
  if (s.kind === "value") return s.i == null ? `the bias of ${neuronName(v.net, s.k + 1, s.j)}` : `the weight from ${neuronName(v.net, s.k, s.i)} into ${neuronName(v.net, s.k + 1, s.j)}`;
  return `the weights of ${neuronName(v.net, s.k + 1, s.j)}`;
}

/** The name the services know a network by. A starter kept in this mod is "starter:tiny" here and "tiny" there. */
export const serviceName = (model) => String(model ?? "").replace(/^starter:/, "");

/** What is selected, as a pin for the agent's context (kit/context.js): {ref, kind, label}. The ref uses the
 *  services' numbering: the neuron's column, and its index counted from 0. */
export function selectionPin(v) {
  const { net, sel, selW } = v, model = serviceName(v.model);
  if (selW) return { ref: `weights:${model}@layer=${selW.k + 1},neuron=${selW.j},from=${selW.i}`, kind: "weights", label: `${net.name}: ${handTarget(v).name} (${signed(net.layers[selW.k].W[selW.j][selW.i])})` };
  if (sel) return { ref: `weights:${model}@layer=${sel.k},neuron=${sel.j}`, kind: "weights", label: `${net.name}: ${neuronName(net, sel.k, sel.j)}${sel.k > 0 ? ` (bias ${plain(net.layers[sel.k - 1].b[sel.j])})` : ""}` };
  return { ref: `weights:${model}`, kind: "weights", label: `${net.name}: the whole network` };
}

const RULES = "Follow the weights playbook: read before you run anything, say what you were allowed and what you used (raw weights, a sandbox, forward passes), and say \"from the weights alone\" only if the session's record does.";

/** The three starter prompts, each written around what is selected. Returns [{id, label, prompt, needs}]: `needs`
 *  is a sentence when the prompt cannot be sent yet. */
export function starters(v) {
  const { net, sel, run } = v, model = serviceName(v.model);
  const open = `Start with \`cst weights open ${model} --agent\`.`;
  const column = sel && sel.k > 0 ? sel.k : 1;
  // The command line counts layers and neurons from 1, as the page does.
  const where = sel && sel.k > 0 ? `${neuronName(net, sel.k, sel.j)} (layer ${sel.k}, neuron ${sel.j + 1})` : null;
  const e = net.examples[v.inputAt];
  const out = run?.a?.at(-1);
  const wrong = out && e ? answerOf(out) !== answerOf(e.y) : false;
  const said = out ? (out.length === 1 ? plain(out[0]) : `"${net.outputNames?.[answerOf(out)] ?? answerOf(out)}"`) : "nothing yet";
  const wanted = e ? (e.y.length === 1 ? plain(e.y[0]) : `"${net.outputNames?.[answerOf(e.y)] ?? answerOf(e.y)}"`) : "";
  return [
    { id: "detects", label: "Find what this neuron detects", needs: where ? null : "Open a neuron first.",
      prompt: `In Captain's Weights page I have the network "${model}" open, with ${where} selected. Find what this neuron detects. ${open} Then read: \`cst weights heatmap --layer ${column} --neuron ${(sel?.j ?? 0) + 1}\` for its incoming weights and \`cst weights stats layer${column}.weight --neurons\` for how it compares with its neighbours. Look at what it writes to as well. Tell me in one sentence what it responds to, how sure you are, and what would make you surer. Trained weights often look close to random, so say so if there is no clear pattern. Change nothing. ${RULES}` },
    { id: "fix", label: "Fix this wrong answer with the smallest edit", needs: e ? null : "Pick an input first.",
      prompt: `In Captain's Weights page I have the network "${model}" open on the input "${e?.name}". ${wrong ? `It gets it wrong: it answers ${said} and ${wanted} is wanted.` : `It answers ${said} and ${wanted} is wanted. Check whether that is right before changing anything.`} Fix a wrong answer with the smallest edit. Start with \`cst weights open ${model} --agent --blind\`: blind hides the scores from you until you save or close, which is what lets the record say "from the weights alone". Before any edit, write one sentence: what you think causes it, what will change by roughly how much, and what will not change. Name the target with \`cst weights sets --target-example ${v.inputAt + 1}\` and what should not change with \`--keep\`, make ONE \`cst weights edit\` with \`--why\`, then \`cst weights evaluate\`. Keep the edit only if the target improves and the rest holds. Otherwise \`cst weights undo\` and tell me what that ruled out. Finish with \`cst weights close\` and \`cst weights report\`. ${RULES}` },
    { id: "layer", label: "Explain this layer", needs: null,
      prompt: `In Captain's Weights page I have the network "${model}" open. Explain layer ${column} (the weights into column ${column}, ${net.sizes[column]} neurons fed by ${net.sizes[column - 1]}). ${open} Read its shape with \`cst weights layers\`, its numbers with \`cst weights stats layer${column}.weight --neurons\`, and a few neurons with \`cst weights heatmap --layer ${column}\`. Tell me what this layer seems to compute, which neurons do most of the work, which look dead or saturated, and what you could not tell without running the network. Change nothing. ${RULES}` },
  ];
}

/** A save button: outlined like every other button, in the page's accent colour because something waits on it. */
function SaveButton(c, id, label, onClick, disabled) {
  return div().id(id).h(26).px(10).flex().flex_none().items_center().justify_center().text_size(12.5).font_medium().border_1()
    .when(disabled, (e) => e.bg(c.surface2).text_color(c.grey500).border_color(c.rule))
    .when(!disabled, (e) => e.bg(c.surface).border_color(c.blue).text_color(c.blue).cursor_pointer().hover((x) => x.bg(c.blueLight)).on_click(onClick))
    .child(label);
}

/** The hand tools, for the panel beside the drawing. `v` is the page. */
export function HandBlock(c, v) {
  const t = handTarget(v);
  if (!t || !v.session) return null;
  const sel = v.sel, busy = Boolean(v.busy), s = v.staged;
  const one = s?.kind === "value" ? s : null, whole = s?.kind === "neuron" ? s : null;
  // The dot sits in the middle on the saved value, and stays where a staged value put it.
  const knob = Math.max(0, Math.min(TRACK_W, TRACK_W / 2 + (((one?.value ?? t.value) - t.value) / dragSpan(t.value)) * TRACK_W));
  const mine = (v.session.events ?? []).filter((e) => e.kind === "edit" && e.by === "you" && !e.undone).at(-1);
  const pick = (id, label, what) => { const on = whole && whole.op === what.op && whole.factor === what.factor;
    return on ? div().id(id).h(26).px(10).flex().flex_none().items_center().text_size(12.5).font_medium().border_1().border_color(c.blue).bg(c.blueLight).text_color(c.blue).child(label)
      : Button(c, id, label, (_e, cx) => v.stageNeuron(what, cx), { small: true, disabled: busy }); };
  const choice = (saveId, saveLabel) => h_flex().id("wt-hand-staged").gap(6).flex_wrap()
    .child(SaveButton(c, saveId, saveLabel, (_e, cx) => v.saveStaged(cx), busy))
    .child(Button(c, "wt-hand-back", "Put it back", (_e, cx) => v.putBack(cx), { small: true, disabled: busy }));
  return v_flex().id("wt-hand").gap(8).p(8).border_1().border_color(s ? c.blue : c.rule)
    .child(div().text_size(12.5).font_semibold().child("Change it by hand"))
    .child(div().id("wt-hand-target").text_size(12.5).line_height("17px").text_color(c.navy).child(`${t.name.charAt(0).toUpperCase()}${t.name.slice(1)} is ${signed(t.value)}.`))
    .child(h_flex().gap(8).items_center()
      .child(div().id("wt-hand-input").w(96).h(28).flex().flex_none().items_center().px(8).border_1().border_color(one ? c.blue : c.grey300).cursor_text()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.()).child(Input.new(v.handInput).w_full().text_size(12.5).text_color(c.navy)))
      .child(div().flex_1().min_w(0).text_size(11.5).line_height("15px").text_color(c.grey500).child(one ? "" : "Type a new value. Nothing changes until you save it.")))
    .child(v_flex().gap(3)
      .child(div().id("wt-hand-track").relative().w(TRACK_W).h(18).flex_none().cursor_pointer()
        .tooltip("Drag to try a new value. Nothing changes until you save it")
        .on_mouse_down("left", (e, cx) => { cx.stop_propagation?.(); v.dragStart(e.position?.x ?? 0, cx); })
        .on_mouse_move((e, cx) => { if (v.drag) v.dragMove(e.position?.x ?? 0, cx); })
        .on_mouse_up("left", (_e, cx) => v.dragEnd(cx))
        .on_hover((over, cx) => { if (!over && v.drag) v.dragEnd(cx); })
        .child(div().absolute().left(0).top(8).w(TRACK_W).h(2).bg(c.grey300))
        .child(div().absolute().left(TRACK_W / 2).top(4).w(1).h(10).bg(c.grey500))
        .child(div().id("wt-hand-knob").absolute().left(knob - 6).top(3).size(12).rounded_full().bg(v.drag || one ? c.blue : c.surface).border_1().border_color(c.blue)))
      .child(div().id("wt-hand-drag-says").text_size(11.5).text_color(c.grey500).child("Drag the dot to try a value: left is lower, right is higher.")))
    .when(Boolean(v.typo), (e) => e.child(div().id("wt-hand-typo").text_size(11.5).line_height("16px").text_color(c.grey500).child(v.typo)))
    .when(Boolean(one), (e) => e
      .child(div().id("wt-hand-unsaved").text_size(12.5).line_height("17px").text_color(c.navy).child(stagedWords(v.net, one, t.value)))
      .child(choice("wt-hand-set", "Set this value")))
    .when(Boolean(sel) && sel.k > 0, (e) => e.child(h_flex().flex_wrap().gap(6)
      .child(pick("wt-hand-halve", "Halve its weights", { op: "scale", factor: 0.5 }))
      .child(pick("wt-hand-double", "Double its weights", { op: "scale", factor: 2 }))
      .child(pick("wt-hand-zero", "Set this neuron to zero", { op: "zero" }))))
    .when(Boolean(whole), (e) => e
      .child(div().id("wt-hand-unsaved").text_size(12.5).line_height("17px").text_color(c.navy).child(stagedWords(v.net, whole, t.value)))
      .child(choice("wt-hand-save", "Save this change")))
    .when(Boolean(mine), (e) => e.child(h_flex().child(Button(c, "wt-hand-undo", "Take back my last edit", (_e, cx) => v.undo(mine.id, cx), { small: true, disabled: busy }))))
    .when(Boolean(v.note), (e) => e.child(div().id("wt-hand-note").text_size(12.5).line_height("17px").text_color(c.fail).child(v.note)));
}
