// CAPTAIN: backpropagation, stepped. The error starts at the output and walks backward one layer at a time: first
// what each output wants, then for each layer the nudge the cost wants for every weight into it (its lines thicken
// where the cost's slope is large) and what the neurons behind it want. Play, next step, previous step. For one
// picked weight, the chain rule as three factors multiplied, each named in words.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { int } from "./kit/num.js";
import { chain, answerOf } from "./net.js";
import { neuronName, signed, plain } from "./inspect.js";

/** How many steps the walk has: the output's error, then one per layer. */
export const stepsOf = (net) => net.layers.length + 1;

/** The first layer whose lines show nudges at this step (draw.js `walkedFrom`), or null when not walking. At step
 *  1 that is past the last layer: only the output column has its marks. */
export function walkedFrom(net, step) { return step > 0 ? net.layers.length - (step - 1) : null; }

const columnName = (net, k) => (k === 0 ? "the input" : k === net.sizes.length - 1 ? "the output" : net.sizes.length === 3 ? "the hidden column" : `hidden column ${k}`);

/** What this step shows, in one or two sentences. */
export function walkWords(net, run, grad, step, wanted) {
  const of = stepsOf(net), L = net.layers.length;
  if (step <= 0 || !run || !grad) return "";
  if (step === 1) {
    const out = run.a[L];
    if (out.length === 1) {
      const d = -grad.dA[L][0];
      return `Step 1 of ${of}. The output's error: the network said ${plain(out[0])} and ${plain(wanted[0])} was wanted, so the cost wants the output ${Math.abs(d) < 0.00005 ? "left as it is" : d > 0 ? "brighter" : "dimmer"}.`;
    }
    const want = answerOf(wanted), said = answerOf(out), name = (j) => net.outputNames?.[j] ?? String(j + 1);
    return `Step 1 of ${of}. The output's error: the network said "${name(said)}" and "${name(want)}" was wanted, so the cost wants "${name(want)}" brighter and the others dimmer. The marks beside the outputs show how much.`;
  }
  const k = L - (step - 1); // the layer reached: it joins column k to column k + 1
  const behind = k > 0 ? ` The neurons of ${columnName(net, k)} cannot be changed directly: each mark there is a wish, the sum of what every neuron after it wants.` : " The input cannot be changed, so the walk ends here.";
  return `Step ${step} of ${of}. The nudges reach the weights into ${columnName(net, k + 1)}. A line is thicker where the cost's slope for that weight is larger, blue where the cost wants it up and red where it wants it down.${behind}`;
}

/** The weight with the largest slope in a layer: the one the walk picks when a person has picked none. */
export function steepestWeight(grad, k) {
  let best = { k, j: 0, i: 0 }, top = -1;
  grad.dW[k].forEach((row, j) => row.forEach((g, i) => { if (Math.abs(g) > top) { top = Math.abs(g); best = { k, j, i }; } }));
  return best;
}

/** The chain rule for one weight as rows: three factors, each named in words, and their product. */
export function chainRows(net, run, grad, at) {
  const c = chain(net, run, grad, at.k, at.j, at.i);
  const to = neuronName(net, at.k + 1, at.j), from = neuronName(net, at.k, at.i);
  const paths = c.last ? 1 : net.sizes.slice(at.k + 2).reduce((n, s) => n * s, 1);
  return {
    exact: c.exact, paths, gradient: c.gradient,
    rows: [
      { id: "before", label: `How active ${from} is`, value: plain(c.before) },
      { id: "steep", label: `How steep the squish is at ${to}`, value: plain(c.steep) },
      { id: "behind", label: c.last ? `How far off ${to} is` : `How far off ${to} is, added up over the ${int(paths)} ${paths === 1 ? "path" : "paths"} from it to the cost`, value: signed(c.behind) },
    ],
    product: signed(c.exact ? c.product : c.gradient),
  };
}

/** The chain rule block, for the inspector, when a weight is picked and gradients are worked out. */
export function ChainBlock(c, v) {
  if (!v.selW || !v.grad || !v.run) return null;
  const r = chainRows(v.net, v.run, v.grad, v.selW);
  return v_flex().id("wt-chain").gap(4).p(8).border_1().border_color(c.rule)
    .child(div().text_size(12.5).font_semibold().child("The chain rule for the picked weight"))
    .child(div().text_size(11.5).line_height("17px").text_color(c.grey500).child("How much the cost moves when this weight moves a little: three numbers, multiplied."))
    .children(r.rows.map((row, n) => h_flex().id(`wt-chain-${row.id}`).gap(8).items_start().text_size(12.5)
      .child(div().w(12).flex_none().text_color(c.grey500).child(n ? "x" : ""))
      .child(div().flex_1().min_w(0).line_height("17px").text_color(c.navy).child(row.label))
      .child(div().w(64).flex_none().text_right().font_medium().child(row.value))))
    .child(h_flex().id("wt-chain-product").gap(8).pt(4).border_t(1).border_color(c.rule).text_size(12.5)
      .child(div().w(12).flex_none().text_color(c.grey500).child("="))
      .child(div().flex_1().min_w(0).line_height("17px").child("The cost's slope for this weight (its gradient)"))
      .child(div().w(64).flex_none().text_right().font_semibold().child(r.product)))
    .when(!r.exact, (e) => e.child(div().id("wt-chain-softmax").text_size(11.5).line_height("17px").text_color(c.grey500)
      .child("Softmax ties the outputs together, so here the three do not multiply to the slope. The slope shown is the true one.")));
}

/** The walk's controls and its sentence, laid over the drawing. `v` is the page. */
export function WalkControls(c, v) {
  const step = v.walk.step, of = stepsOf(v.net);
  const btn = (id, text, tip, go, off = false) => div().id(id).h(24).px(8).flex().flex_none().items_center().text_size(12.5).border_1()
    .bg(c.surface).border_color(c.grey300).text_color(off ? c.grey500 : c.navy)
    .when(!off, (e) => e.cursor_pointer().hover((s) => s.border_color(c.blue).text_color(c.blue)).on_click((_e, cx) => go(cx)))
    .tooltip(tip).on_mouse_down("left", (_e, cx) => cx.stop_propagation?.()).child(text);
  const e = v.net.examples[v.inputAt];
  return [
    h_flex().id("wt-walk").absolute().top(8).left(8).gap(4).items_center()
      .child(btn("wt-walk-play", v.walk.playing ? "Pause" : step >= of ? "Walk the error back again" : "Walk the error back", "Backpropagation, played: the output's error, then each layer's nudges, one step at a time", (cx) => v.playWalk(cx)))
      .child(btn("wt-walk-next", "Next step", "One step further back", (cx) => v.stepWalk(1, cx), step >= of))
      .child(btn("wt-walk-prev", "Previous step", "One step toward the output", (cx) => v.stepWalk(-1, cx), step <= 0))
      .when(step > 0, (x) => x.child(btn("wt-walk-stop", "Show the weights again", "Leave the walk and draw the weights", (cx) => v.stopWalk(cx)))),
    step > 0 && e
      ? div().id("wt-walk-says").absolute().left(8).bottom(8).max_w(560).p(8).bg(c.surface).border_1().border_color(c.grey300).text_size(12.5).line_height("18px")
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.())
        .child(walkWords(v.net, v.run, v.grad, step, e.y))
      : null,
  ].filter(Boolean);
}
