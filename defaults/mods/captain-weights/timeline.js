// CAPTAIN: what the agent is doing, as it does it. Every read, run and edit in the session's record is one line in
// words, newest first. An edit opens to its old and new values and can be taken back. The header is the session's
// ledger: what the agent was allowed, what it used, and the share of numbers changed. "From the weights alone" is
// said only when the ledger shows no sandbox and no forward pass.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { Button } from "./kit/ui.js";
import { neuronName, signed } from "./inspect.js";
import { cost3 } from "./cost.js";

const SHOWN_CHANGES = 6;
const count = (n) => Number(n).toLocaleString("en-US");
const plural = (n, one, many) => `${count(n)} ${n === 1 ? one : many}`;
const trim = (v) => String(Number(Number(v).toFixed(4)));

/** Where an edit landed: "layer 2, neuron 7". Layers are counted from the first one after the input, neurons
 *  from 1. */
export function whereOf(ev) {
  if (ev.layer == null) return ev.tensor ? String(ev.tensor).replace(/\.(weight|bias)$/, "") : "the network";
  return ev.neuron == null ? `layer ${ev.layer}` : `layer ${ev.layer}, neuron ${ev.neuron + 1}`;
}

/** One event as a sentence a person can read without knowing the tool. */
export function eventWords(ev) {
  const who = ev.by === "agent" ? "Agent" : "You";
  const changes = ev.changes ?? [];
  const weights = changes.filter((c) => c.i != null), biases = changes.filter((c) => c.i == null);
  const total = ev.changed ?? changes.length;
  const test = ev.cost?.test;
  const cost = test && Number.isFinite(test.before) && Number.isFinite(test.after) ? ` Cost on the test set: ${cost3(test.before)} to ${cost3(test.after)}.` : "";
  const where = whereOf(ev);
  let said;
  if (ev.kind === "edit") {
    if (ev.op === "scale") said = `${who} scaled ${plural(total, "weight", "weights")} into ${where}, by ${trim(ev.factor)}.`;
    else if (ev.op === "zero") said = `${who} set ${plural(weights.length, "weight", "weights")}${biases.length ? " and the bias" : ""} of ${where} to zero.`;
    else if (ev.op === "set" && total === 1 && changes.length === 1) said = `${who} set ${biases.length ? "the bias" : "one weight"} of ${where} from ${signed(changes[0].old)} to ${signed(changes[0].new)}.`;
    else said = `${who} changed ${plural(total, "number", "numbers")} in ${where}.`;
  } else if (ev.kind === "undo") said = `${who} took back an edit to ${where}.`;
  else if (ev.kind === "redo") said = `${who} put back an edit to ${where}.`;
  else if (ev.kind === "read") { const n = ev.count ?? ev.touched?.weights?.length ?? 0; said = n ? `${who} read ${plural(n, "weight", "weights")}${ev.layer != null ? ` of ${where}` : ""}.` : `${who} read the network's shape.`; }
  else if (ev.kind === "run") said = ev.input ? `${who} ran the network on "${ev.input}".` : `${who} ran the network.`;
  else if (ev.kind === "gradients") said = `${who} worked out the gradients: how the cost changes with each weight.`;
  else if (ev.kind === "evaluate") said = `${who} scored the network on the test set.`;
  else if (ev.kind === "open") said = `${who} opened a session on this network.`;
  else if (ev.kind === "save") said = `${who} saved the edited network as a new version.`;
  // A kind this page has no words for: the services' own sentence, when it has none of their names in it.
  else said = typeof ev.sentence === "string" && ev.sentence ? ev.sentence : `${who} did something this page has no words for yet.`;
  return `${said}${cost}`;
}

/** The ledger as the header says it: {allowed: [sentence], changed, alone}. `alone` is true ONLY when the ledger
 *  itself shows that no sandbox was allowed and no forward pass was used. A field that is missing counts against. */
export function ledgerFacts(ledger) {
  const l = ledger ?? {};
  const alone = l.sandbox === false && l.sandbox_used !== true && l.forward_used === 0 && l.weights_alone !== false;
  const allowed = [
    l.raw_weights === true ? `Raw weights: allowed, ${count(l.weights_read ?? 0)} read` : l.raw_weights === false ? "Raw weights: not allowed" : null,
    l.sandbox === true ? "A sandbox (code run on the numbers): allowed" : l.sandbox === false ? "A sandbox (code run on the numbers): not allowed" : null,
    Number.isFinite(l.forward_allowed) ? `Forward passes (runs of the network): ${count(l.forward_used ?? 0)} of ${count(l.forward_allowed)} used` : null,
    l.gradients_computed === true ? "Gradients were worked out" : null,
  ].filter(Boolean);
  const share = l.total > 0 && Number.isFinite(l.changed) ? (100 * l.changed) / l.total : null;
  const changed = share == null ? null : `${count(l.changed)} of ${count(l.total)} numbers changed (${share === 0 ? "0" : share < 0.1 ? share.toFixed(3) : share.toFixed(1)}%)`;
  return { allowed, changed, alone };
}

/** What a change's number is called: "the bias", or "the weight from First input". */
function changeName(net, c) {
  return c.i == null ? "The bias" : `The weight from ${neuronName(net, c.k, c.i)}`;
}

/** The panel. `v` is the page: {net, session, openEdit, busy} with openTimeline(id, cx) and undo(id, cx). */
export function Timeline(c, v, { width, height }) {
  const s = v.session;
  const facts = ledgerFacts(s?.ledger);
  const events = (s?.events ?? []).slice().reverse();
  const row = (ev) => {
    const open = ev.id === v.openEdit, isEdit = ev.kind === "edit";
    let el = v_flex().id(`wt-ev-${ev.id}`).px(8).py(5).gap(4).when(open, (e) => e.bg(c.surface2))
      .child(h_flex().gap(8).items_start()
        .child(div().id(`wt-ev-line-${ev.id}`).flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(ev.undone ? c.grey500 : c.navy)
          .when(isEdit, (e) => e.cursor_pointer().hover((st) => st.text_color(c.blue)).tooltip(open ? "Close this edit" : "See the old and new values").on_click((_e, cx) => v.openTimeline(ev.id, cx)))
          .child(`${eventWords(ev)}${ev.undone ? " Taken back." : ""}`)));
    if (open && isEdit) {
      const changes = ev.changes ?? [], total = ev.changed ?? changes.length;
      el = el
        .when(Boolean(ev.why), (e) => e.child(div().id(`wt-ev-why-${ev.id}`).text_size(12).line_height("17px").text_color(c.grey900).child(`Why: ${ev.why}`)))
        .child(v_flex().id(`wt-ev-changes-${ev.id}`).gap(1).children(changes.slice(0, SHOWN_CHANGES).map((ch, n) => h_flex().id(`wt-ev-change-${ev.id}-${n}`).gap(8).text_size(12)
          .child(div().w(210).flex_none().truncate().text_color(c.grey900).child(changeName(v.net, ch)))
          .child(div().flex_none().font_medium().child(`${signed(ch.old)} to ${signed(ch.new)}`))))
          .when(total > SHOWN_CHANGES, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`and ${count(total - SHOWN_CHANGES)} more`))))
        .when(!ev.undone, (e) => e.child(h_flex().pt(2).child(Button(c, `wt-ev-undo-${ev.id}`, v.busy === ev.id ? "Taking it back" : "Take this edit back", (_e, cx) => v.undo(ev.id, cx), { small: true, disabled: v.busy === ev.id }))));
    }
    return el;
  };
  return v_flex().id("wt-timeline").w(width).h(height).flex_none().gap(6)
    .child(h_flex().gap(8).items_center()
      .child(div().text_size(13).font_semibold().child("What the agent is doing"))
      .when(facts.alone, (e) => e.child(div().id("wt-alone").flex_none().px(6).h(20).flex().items_center().bg(c.goodLight).text_color(c.good).text_size(11.5).font_medium()
        .tooltip("The record shows no sandbox was allowed and the network was never run").child("From the weights alone")))
      .child(div().flex_1())
      .when(Boolean(facts.changed), (e) => e.child(div().id("wt-ledger-changed").flex_none().text_size(11.5).text_color(c.grey500).child(facts.changed))))
    .when(facts.allowed.length > 0, (e) => e.child(div().id("wt-ledger").text_size(11.5).line_height("16px").text_color(c.grey500).child(`${facts.allowed.join(". ")}.`)))
    .when(Boolean(v.note), (e) => e.child(div().id("wt-note").text_size(12).text_color(c.fail).child(v.note)))
    .child(div().id("wt-events").flex_1().min_h(0).overflow_y_scroll().border_1().border_color(c.rule)
      .child(events.length
        ? v_flex().children(events.map(row))
        : div().p(10).text_size(12.5).line_height("18px").text_color(c.grey500).child("Nothing yet. Each read, run and edit shows here as it happens, and the neurons it touched pulse in the drawing.")));
}
