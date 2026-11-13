// CAPTAIN: the cost, under the drawing. One number for the chosen input and one for the whole test set, a small
// curve of the test-set cost with a mark for every edit, and for one edit its three numbers: the input it was
// meant to fix, the inputs that should not change, and the test set.
import { div, PathBuilder } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { int } from "./kit/num.js";
import { costOf } from "./net.js";

const MAX_POINTS = 60; // the curve shows the last 60 steps

/** A cost, to three places: "0.412". */
export const cost3 = (v) => (Number.isFinite(v) ? v.toFixed(3) : "unknown");
const count = (n) => int(n);

const COST_WORDS = {
  squared_error: "Cost is one number for how wrong the network is: the squared distance between its answer and the wanted answer. 0 is perfect.",
  cross_entropy: "Cost is one number for how wrong the network is: how surprised it is by the wanted answer (cross-entropy). 0 is perfect.",
};

/** The test-set cost step by step: where it started, then after every edit and every undo. Each step:
 *  {cost, id, kind: "start" | "edit" | "undo", by, undone}. `now` is the test-set cost as it is, used when the
 *  record has no edits to say where it started. */
export function costSteps(session, now) {
  const evs = (session?.events ?? []).filter((e) => (e.kind === "edit" || e.kind === "undo") && Number.isFinite(e.cost?.test?.after));
  if (!evs.length) return Number.isFinite(now?.cost) ? [{ cost: now.cost, id: null, kind: "start" }] : [];
  const steps = [{ cost: evs[0].cost.test.before, id: null, kind: "start" }];
  for (const e of evs) steps.push({ cost: e.cost.test.after, id: e.id, kind: e.kind, by: e.by, undone: !!e.undone });
  return steps;
}

/** The edit whose three numbers show: the one opened, else the last one made. */
export function editInFocus(session, openId) {
  const edits = (session?.events ?? []).filter((e) => e.kind === "edit" && e.cost);
  return edits.find((e) => e.id === openId) ?? edits.at(-1) ?? null;
}

/** An edit's three numbers as rows: [{id, name, before, after, says}]. */
export function threeNumbers(ev) {
  const c = ev?.cost;
  if (!c) return [];
  const row = (id, name, part, says) => (part && Number.isFinite(part.before) && Number.isFinite(part.after) ? [{ id, name, before: part.before, after: part.after, says }] : []);
  const t = c.target, k = c.keep, s = c.test;
  return [
    ...row("target", t?.of > 1 ? `Target (${count(t.of)} inputs)` : `Target${t?.name && t.name !== "target" ? `: ${t.name}` : ""}`, t, "what the edit was meant to fix"),
    ...row("keep", `Should not change${k?.of ? ` (${count(k.of)} inputs)` : ""}`, k, k?.answers_changed != null ? `${count(k.answers_changed)} ${k.answers_changed === 1 ? "answer" : "answers"} changed` : "the other inputs"),
    ...row("test", `Test set${s?.of ? ` (${count(s.of)} inputs)` : ""}`, s, s?.right_after != null && s?.of ? `${count(s.right_after)} of ${count(s.of)} right` : "inputs kept apart"),
  ];
}

function Curve(c, v, steps, w, h) {
  const box = div().id("wt-cost-curve").relative().w(w).h(h).flex_none().border_1().border_color(c.rule);
  const shown = steps.slice(-MAX_POINTS);
  if (shown.length < 2) return box.child(div().absolute().inset_0().flex().items_center().justify_center().text_size(11.5).text_color(c.grey500).child("Each edit adds a point here."));
  const ys = shown.map((s) => s.cost), lo = Math.min(0, ...ys), hi = Math.max(...ys, 1e-9), span = hi - lo || 1;
  const X = (n) => 10 + (n / (shown.length - 1)) * (w - 20), Y = (y) => 8 + (1 - (y - lo) / span) * (h - 16);
  const paint = globalThis.window?.paint_path;
  let line = PathBuilder.stroke(1.5);
  shown.forEach((s, n) => { line = n ? line.line_to(X(n), Y(s.cost)) : line.move_to(X(n), Y(s.cost)); });
  const focus = editInFocus(v.session, v.openEdit)?.id;
  return box
    .when(Boolean(paint), (e) => e.child(div().absolute().inset_0().child(paint(line.build(), c.grey500))))
    // A mark per edit: filled for one still applied, hollow for one taken back or for the undo itself.
    .children(shown.map((s, n) => {
      if (!s.id) return null;
      const hollow = s.undone || s.kind === "undo", on = s.id === focus;
      return div().id(`wt-cost-mark-${s.id}`).absolute().left(X(n) - 5).top(Y(s.cost) - 5).size(10).rounded_full().cursor_pointer()
        .border_1().when(on, (e) => e.border_2()).border_color(on ? c.navy : c.blue).bg(hollow ? c.surface : c.blue)
        .tooltip(`${s.kind === "undo" ? "An edit taken back" : s.by === "agent" ? "The agent's edit" : "Your edit"}: test-set cost ${cost3(s.cost)}`)
        .on_click((_e, cx) => v.openTimeline(s.id, cx));
    }).filter(Boolean));
}

/** The panel. `v` is the page: {net, run, inputAt, test, session, openEdit} and openTimeline(id, cx). */
export function CostPanel(c, v, { width, height }) {
  const { net, run } = v;
  const e = net.examples[v.inputAt];
  const here = run && e ? costOf(net, run.a.at(-1), e.y) : null;
  const t = v.test;
  const steps = costSteps(v.session, t);
  const focus = editInFocus(v.session, v.openEdit);
  const rows = threeNumbers(focus);
  const number = (id, label, value, under) => v_flex().gap(1).flex_none()
    .child(div().text_size(11.5).text_color(c.grey500).child(label))
    .child(div().id(id).text_size(15).font_semibold().child(value))
    .child(div().id(`${id}-under`).text_size(11.5).text_color(c.grey500).child(under));
  const curveW = Math.max(120, Math.min(260, width - 290));
  return v_flex().id("wt-cost").w(width).h(height).flex_none().gap(8)
    .child(h_flex().gap(6).items_center()
      .child(div().text_size(12.5).font_semibold().child("Cost"))
      .child(div().id("wt-cost-says").flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(COST_WORDS[net.cost ?? "squared_error"] ?? COST_WORDS.squared_error)))
    .child(h_flex().gap(22).items_start()
      .child(number("wt-cost-input", "This input", here == null ? "not run" : cost3(here), e?.name ?? ""))
      .child(number("wt-cost-test", t?.of ? `Test set (${count(t.of)} inputs)` : "Test set", t ? cost3(t.cost) : "reading", t?.of ? `${count(t.right)} of ${count(t.of)} right` : ""))
      .child(Curve(c, v, steps, curveW, 62)))
    .when(rows.length > 0, (el) => el.child(v_flex().id("wt-cost-edit").gap(2)
      .child(div().text_size(11.5).text_color(c.grey500).child(focus.id === v.openEdit ? "The edit that is open" : "The last edit"))
      .children(rows.map((r) => h_flex().id(`wt-cost-${r.id}`).gap(8).items_center().text_size(12.5)
        .child(div().w(190).flex_none().truncate().text_color(c.navy).child(r.name))
        .child(div().w(120).flex_none().font_medium().child(`${cost3(r.before)} to ${cost3(r.after)}`))
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(r.says))))));
}
