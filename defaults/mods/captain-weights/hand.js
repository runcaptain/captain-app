// CAPTAIN: changing the network by hand, and handing what is selected to an agent. With a neuron open: type or
// drag its bias, or a picked weight, to a new value; halve, double or zero the neuron. The cost for the chosen
// input moves while a number is dragged, and the edit is recorded when the drag ends. Every edit goes through
// adapter.js, lands in the timeline like the agent's, and can be taken back.
import { div } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { Button } from "./kit/ui.js";
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
  const where = sel && sel.k > 0 ? `${neuronName(net, sel.k, sel.j)} (column ${sel.k}, neuron ${sel.j} counted from 0)` : null;
  const e = net.examples[v.inputAt];
  const out = run?.a?.at(-1);
  const wrong = out && e ? answerOf(out) !== answerOf(e.y) : false;
  const said = out ? (out.length === 1 ? plain(out[0]) : `"${net.outputNames?.[answerOf(out)] ?? answerOf(out)}"`) : "nothing yet";
  const wanted = e ? (e.y.length === 1 ? plain(e.y[0]) : `"${net.outputNames?.[answerOf(e.y)] ?? answerOf(e.y)}"`) : "";
  return [
    { id: "detects", label: "Find what this neuron detects", needs: where ? null : "Open a neuron first.",
      prompt: `In Captain's Weights page I have the network "${model}" open, with ${where} selected. Find what this neuron detects. ${open} Then read: \`cst weights heatmap --layer ${column - 1} --neuron ${sel?.j ?? 0}\` for its incoming weights and \`cst weights stats --neurons\` for how it compares with its neighbours. Look at what it writes to as well. Tell me in one sentence what it responds to, how sure you are, and what would make you surer. Trained weights often look close to random, so say so if there is no clear pattern. Change nothing. ${RULES}` },
    { id: "fix", label: "Fix this wrong answer with the smallest edit", needs: e ? null : "Pick an input first.",
      prompt: `In Captain's Weights page I have the network "${model}" open on the input "${e?.name}". ${wrong ? `It gets it wrong: it answers ${said} and ${wanted} is wanted.` : `It answers ${said} and ${wanted} is wanted. Check whether that is right before changing anything.`} Fix a wrong answer with the smallest edit. ${open} Before any edit, write one sentence: what you think causes it, what will change by roughly how much, and what will not change. Name the target and what should not change with \`cst weights sets\`, make ONE \`cst weights edit\` with \`--why\`, then \`cst weights evaluate\`. Keep the edit only if the target improves and the rest holds. Otherwise \`cst weights undo\` and tell me what that ruled out. Finish with \`cst weights report\`. ${RULES}` },
    { id: "layer", label: "Explain this layer", needs: null,
      prompt: `In Captain's Weights page I have the network "${model}" open. Explain layer ${column} (the weights into column ${column}, ${net.sizes[column]} neurons fed by ${net.sizes[column - 1]}). ${open} Read its shape with \`cst weights layers\`, its numbers with \`cst weights stats layer${column}.weight --neurons\`, and a few neurons with \`cst weights heatmap --layer ${column - 1}\`. Tell me what this layer seems to compute, which neurons do most of the work, which look dead or saturated, and what you could not tell without running the network. Change nothing. ${RULES}` },
  ];
}

/** The hand tools, for the panel beside the drawing. `v` is the page. */
export function HandBlock(c, v) {
  const t = handTarget(v);
  if (!t || !v.session) return null;
  const sel = v.sel, busy = Boolean(v.busy);
  const span = dragSpan(v.drag?.from ?? t.value), base = v.drag?.from ?? t.value;
  // The knob sits in the middle when nothing is dragged, and follows the pointer while it is.
  const knob = Math.max(0, Math.min(TRACK_W, TRACK_W / 2 + ((t.value - base) / span) * TRACK_W));
  const mine = (v.session.events ?? []).filter((e) => e.kind === "edit" && e.by === "you" && !e.undone).at(-1);
  return v_flex().id("wt-hand").gap(8).p(8).border_1().border_color(c.rule)
    .child(div().text_size(12.5).font_semibold().child("Change it by hand"))
    .child(div().id("wt-hand-target").text_size(12.5).line_height("17px").text_color(c.navy).child(`${t.name.charAt(0).toUpperCase()}${t.name.slice(1)} is ${signed(t.value)}.`))
    .child(h_flex().gap(8).items_center()
      .child(div().id("wt-hand-input").w(96).h(28).flex().flex_none().items_center().px(8).border_1().border_color(c.grey300).cursor_text()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.()).child(Input.new(v.handInput).w_full().text_size(12.5).text_color(c.navy)))
      .child(Button(c, "wt-hand-set", "Set this value", (_e, cx) => v.setTyped(cx), { small: true, disabled: busy })))
    .child(v_flex().gap(3)
      .child(div().id("wt-hand-track").relative().w(TRACK_W).h(18).flex_none().cursor_pointer()
        .tooltip("Drag to change the number and watch the cost move")
        .on_mouse_down("left", (e, cx) => { cx.stop_propagation?.(); v.dragStart(e.position?.x ?? 0, cx); })
        .on_mouse_move((e, cx) => { if (v.drag) v.dragMove(e.position?.x ?? 0, cx); })
        .on_mouse_up("left", (_e, cx) => v.dragEnd(cx))
        .on_hover((over, cx) => { if (!over && v.drag) v.dragEnd(cx); })
        .child(div().absolute().left(0).top(8).w(TRACK_W).h(2).bg(c.grey300))
        .child(div().absolute().left(TRACK_W / 2).top(4).w(1).h(10).bg(c.grey500))
        .child(div().id("wt-hand-knob").absolute().left(knob - 6).top(3).size(12).rounded_full().bg(v.drag ? c.blue : c.surface).border_1().border_color(c.blue)))
      .child(div().text_size(11.5).text_color(c.grey500).child(v.drag ? `Now ${signed(t.value)}. Let go to keep it.` : "Drag the dot: left is lower, right is higher.")))
    .when(Boolean(sel) && sel.k > 0, (e) => e.child(h_flex().flex_wrap().gap(6)
      .child(Button(c, "wt-hand-halve", "Halve its weights", (_e, cx) => v.editNeuron({ op: "scale", factor: 0.5 }, cx), { small: true, disabled: busy }))
      .child(Button(c, "wt-hand-double", "Double its weights", (_e, cx) => v.editNeuron({ op: "scale", factor: 2 }, cx), { small: true, disabled: busy }))
      .child(Button(c, "wt-hand-zero", "Set this neuron to zero", (_e, cx) => v.editNeuron({ op: "zero" }, cx), { small: true, disabled: busy }))))
    .when(Boolean(mine), (e) => e.child(h_flex().child(Button(c, "wt-hand-undo", "Take back my last edit", (_e, cx) => v.undo(mine.id, cx), { small: true, disabled: busy }))))
    .when(Boolean(v.note), (e) => e.child(div().id("wt-hand-note").text_size(12.5).line_height("17px").text_color(c.fail).child(v.note)));
}
