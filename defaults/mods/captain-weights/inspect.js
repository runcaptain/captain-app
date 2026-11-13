// CAPTAIN: a neuron, opened. Beside the drawing: the weights coming in (each with the activation it multiplies
// and what that adds), the bias, the weighted sum and the activation for the chosen input, the weights going out,
// and when nudges are showing, what the cost wants of this neuron and of each of its weights. One plain sentence
// says what the numbers mean. `neuronFacts` is the numbers alone, so a test can check them against the network.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { squishOf } from "./net.js";

const ROWS = 8; // the most incoming or outgoing weights listed; the rest are counted

/** A number with its sign, short: "+6.00", "-0.0312", "0.00". */
export function signed(v) {
  if (!Number.isFinite(v)) return "none";
  const a = Math.abs(v);
  if (a < 0.00005) return "0.00";
  return `${v < 0 ? "-" : "+"}${a >= 0.1 ? a.toFixed(2) : a.toFixed(4)}`;
}
/** A number without a forced plus: "0.50", "-12.00". */
export function plain(v) {
  if (!Number.isFinite(v)) return "none";
  const a = Math.abs(v);
  if (a < 0.00005) return "0.00";
  return `${v < 0 ? "-" : ""}${a >= 0.1 || a === 0 ? a.toFixed(2) : a.toFixed(4)}`;
}

const SQUISH_WORDS = { sigmoid: "sigmoid, which presses any sum into 0 to 1", relu: "ReLU, which keeps a positive sum and turns a negative one into 0", tanh: "tanh, which presses any sum into -1 to 1", linear: "none: the sum is the activation", softmax: "softmax, which makes the outputs add up to 1" };

/** What to call neuron j of column k, in words a person would use. */
export function neuronName(net, k, j) {
  const last = net.sizes.length - 1;
  if (k === 0) {
    if (net.inputNames?.[j]) return net.inputNames[j];
    if (net.inputShape) return `the pixel in row ${Math.floor(j / net.inputShape[1]) + 1}, column ${(j % net.inputShape[1]) + 1}`;
    return `input ${j + 1}`;
  }
  if (k === last) return net.outputNames?.[j] != null ? `the output "${net.outputNames[j]}"` : `output ${j + 1}`;
  return net.sizes.length === 3 ? `hidden neuron ${j + 1}` : `neuron ${j + 1} of hidden column ${k}`;
}

/** Everything the panel shows for one neuron, as numbers. `grad` may be null (nudges not showing). */
export function neuronFacts(net, run, grad, sel) {
  const { k, j } = sel, last = net.sizes.length - 1;
  const f = { k, j, name: neuronName(net, k, j), activation: run?.a?.[k]?.[j] ?? null, isInput: k === 0, isOutput: k === last };
  if (k > 0) {
    const { W, b } = net.layers[k - 1];
    f.bias = b[j];
    f.sum = run?.z?.[k]?.[j] ?? null;
    f.squish = squishOf(net, k - 1);
    f.incoming = W[j].map((w, i) => ({ i, name: neuronName(net, k - 1, i), w, a: run?.a?.[k - 1]?.[i] ?? null, adds: run ? w * run.a[k - 1][i] : null, gradient: grad ? grad.dW[k - 1][j][i] : null }));
    f.biasGradient = grad ? grad.dB[k - 1][j] : null;
  }
  if (k < last) f.outgoing = net.layers[k].W.map((row, t) => ({ j: t, name: neuronName(net, k + 1, t), w: row[j], gradient: grad ? grad.dW[k][t][j] : null }));
  // The desired nudge: the way the cost wants this activation to move. Positive: brighter.
  f.nudge = grad ? -grad.dA[k][j] : null;
  return f;
}

/** One plain sentence about the neuron: its strongest incoming weight and what that means, then its bias. */
export function neuronSentence(net, f) {
  if (f.isInput) return `This is ${f.name}. It has no weights of its own: its value is the input.`;
  const top = f.incoming.reduce((a, b) => (Math.abs(b.w) > Math.abs(a.w) ? b : a), f.incoming[0]);
  const first = Math.abs(top.w) < 0.00005 ? "All its weights are zero: nothing before it can move it."
    : top.w > 0 ? `Its largest weight is positive (${signed(top.w)}, from ${top.name}): when ${top.name} is bright, this neuron lights up.`
      : `Its largest weight is negative (${signed(top.w)}, from ${top.name}): when ${top.name} is bright, this neuron goes dark.`;
  const bias = Math.abs(f.bias) < 0.00005 ? "Its bias is zero."
    : f.bias < 0 ? `Its bias is ${plain(f.bias)}: the weights must add up to more than ${plain(-f.bias)} before it turns on.`
      : `Its bias is ${signed(f.bias)}: it leans toward on before any input arrives.`;
  return `${first} ${bias}`;
}

/** One plain sentence about a picked weight. */
export function weightSentence(net, at) {
  const w = net.layers[at.k].W[at.j][at.i], from = neuronName(net, at.k, at.i), to = neuronName(net, at.k + 1, at.j);
  const size = Math.abs(w) < 0.00005 ? "zero" : Math.abs(w) >= 1 ? "large" : "small";
  if (size === "zero") return `This weight is zero: ${from} has no say in ${to}.`;
  return w > 0 ? `This weight is ${size} and positive (${signed(w)}): when ${from} is bright, ${to} lights up.`
    : `This weight is ${size} and negative (${signed(w)}): when ${from} is bright, ${to} goes dark.`;
}

/** What the cost wants of this neuron, in words. */
export function nudgeSentence(f) {
  if (f.nudge == null) return null;
  if (Math.abs(f.nudge) < 0.00005) return "For this input the cost does not care which way this neuron moves.";
  return f.nudge > 0 ? `For this input the cost wants this neuron brighter (desired nudge ${signed(f.nudge)}).`
    : `For this input the cost wants this neuron dimmer (desired nudge ${signed(f.nudge)}).`;
}

const label = (c, text) => div().text_size(11).text_color(c.grey500).child(text);
const big = (c, id, text) => div().id(id).text_size(15).font_semibold().child(text);

/** A weight's row: where it comes from or goes to, a bar for its size in its sign's colour, the number with its
 *  sign, and beside it what it multiplies and adds, or its gradient. */
function weightRow(c, id, r, top, on, right, onPick) {
  const m = top > 0 ? Math.abs(r.w) / top : 0;
  return h_flex().id(id).h(24).gap(8).items_center().px(4).cursor_pointer().when(on, (e) => e.bg(c.blueLight))
    .hover((s) => s.bg(c.blueLight)).tooltip("Pick this weight")
    .on_click(onPick)
    .child(div().w(104).flex_none().truncate().text_size(12).text_color(c.grey900).child(r.name))
    .child(div().w(44).h(6).flex_none().bg(c.surface2).child(div().h_full().w(Math.max(1, Math.round(44 * m))).bg(r.w >= 0 ? c.blue : c.fail)))
    .child(div().w(54).flex_none().text_right().text_size(12).font_medium().text_color(c.navy).child(signed(r.w)))
    .child(div().flex_1().min_w(0).truncate().text_right().text_size(11).text_color(c.grey500).child(right));
}

/** The panel. `v` is the page: {net, run, grad, sel, selW} and pickWeight(at, cx). */
export function Inspector(c, v) {
  const { net, run, grad, sel, selW } = v;
  if (!sel) {
    return v_flex().id("wt-inspect").gap(8)
      .child(div().text_size(13).font_semibold().child("Open a neuron"))
      .child(div().text_size(12.5).line_height("18px").text_color(c.grey500).child("Click a circle in the drawing to see its weights, its bias and how it got its activation."));
  }
  const f = neuronFacts(net, run, grad, sel);
  const title = f.name.charAt(0).toUpperCase() + f.name.slice(1);
  let out = v_flex().id("wt-inspect").gap(12)
    .child(div().id("wt-inspect-name").text_size(13).font_semibold().child(title))
    .child(div().id("wt-inspect-says").text_size(12.5).line_height("18px").text_color(c.grey900).child(selW ? weightSentence(net, selW) : neuronSentence(net, f)));
  // The three numbers of the neuron itself.
  out = out.child(h_flex().gap(18)
    .child(v_flex().gap(2).child(label(c, "Activation")).child(big(c, "wt-inspect-a", f.activation == null ? "not run" : plain(f.activation))))
    .when(!f.isInput, (e) => e
      .child(v_flex().gap(2).child(label(c, "Weighted sum")).child(big(c, "wt-inspect-z", f.sum == null ? "not run" : plain(f.sum))))
      .child(v_flex().gap(2).child(label(c, "Bias")).child(big(c, "wt-inspect-b", plain(f.bias))))));
  if (!f.isInput) {
    out = out.child(div().id("wt-inspect-how").text_size(11.5).line_height("17px").text_color(c.grey500)
      .child(`The weighted sum is each weight times the activation it comes from, added up, plus the bias. The squish is ${SQUISH_WORDS[f.squish] ?? f.squish}.`));
  }
  const nudge = nudgeSentence(f);
  if (nudge) out = out.child(div().id("wt-inspect-nudge").p(8).bg(c.surface2).text_size(12.5).line_height("18px").text_color(c.navy).child(nudge));
  const list = (id, heading, rows, pick, right) => {
    const top = Math.max(0, ...rows.map((r) => Math.abs(r.w)));
    // The ones that matter most for this input first: by what they add when the network ran, else by size.
    const sorted = rows.slice().sort((p, q) => Math.abs(q.adds ?? q.w) - Math.abs(p.adds ?? p.w) || Math.abs(q.w) - Math.abs(p.w));
    const shown = rows.length > ROWS ? sorted.slice(0, ROWS) : rows;
    return v_flex().id(id).gap(2)
      .child(h_flex().pb(2).child(div().flex_1().text_size(12).font_semibold().child(heading)).child(label(c, right.head)))
      .children(shown.map((r) => { const at = pick(r); return weightRow(c, `${id}-${at.j}-${at.i}`, r, top, !!selW && selW.k === at.k && selW.j === at.j && selW.i === at.i, right.cell(r), (_e, cx) => v.pickWeight(at, cx)); }))
      .when(rows.length > shown.length, (e) => e.child(div().id(`${id}-more`).px(4).pt(2).text_size(11.5).text_color(c.grey500)
        .child(`and ${(rows.length - shown.length).toLocaleString("en-US")} more, smaller for this input`)));
  };
  const showGrad = grad != null;
  if (f.incoming) {
    out = out.child(list("wt-in-w", `Weights coming in (${f.incoming.length.toLocaleString("en-US")})`, f.incoming, (r) => ({ k: sel.k - 1, j: sel.j, i: r.i }),
      showGrad ? { head: "the cost's slope for it", cell: (r) => signed(r.gradient) }
        : { head: "times activation, adds", cell: (r) => (r.a == null ? "" : `x ${plain(r.a)} = ${signed(r.adds)}`) }));
    if (showGrad) out = out.child(div().id("wt-inspect-bg").px(4).text_size(11.5).text_color(c.grey500).child(`The cost's slope for the bias: ${signed(f.biasGradient)}. A positive slope means the cost falls when the number goes down.`));
  }
  if (f.outgoing) {
    out = out.child(list("wt-out-w", `Weights going out (${f.outgoing.length})`, f.outgoing, (r) => ({ k: sel.k, j: r.j, i: sel.j }),
      showGrad ? { head: "the cost's slope for it", cell: (r) => signed(r.gradient) } : { head: "", cell: () => "" }));
  }
  return out;
}
