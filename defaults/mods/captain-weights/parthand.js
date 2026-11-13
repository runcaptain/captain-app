// CAPTAIN: changing a trained model's numbers by hand, from an open part's panel. It works as it does for a
// neuron (hand.js): pick a row and a column (or click a cell in the picture), type a value, or choose to halve,
// double or zero the row. Each only stages a change: the picture previews it, a line says "Not saved. ...", and
// nothing is sent until "Set this value" or "Save this change" is pressed. "Put it back", Escape or going
// anywhere else drops it. A saved edit is one request through adapter.js. The services do not measure it (this
// kind of model cannot be run here), and their sentence says so in the list of what was changed. Saving a
// version is a separate press (save.js).
import { div } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { Button, plainError } from "./kit/ui.js";
import { int } from "./kit/num.js";
import { sendPartEdit, sendUndo } from "./adapter.js";
import { signed } from "./inspect.js";
import { loadPart, partOf, tensorOf } from "./parts.js";

const SHOWN_CHANGES = 5;

/** What the hand tools act on: the picked row and column of the matrix that is showing. Null when the model
 *  cannot be changed, the matrix is one the services compose (a head's query-key), or its numbers are not read
 *  yet. {t, row, col, value, R, C} with `value` undefined when the row is outside the rows on screen. */
export function partTarget(v) {
  const s = v.pm, p = s && partOf(v), t = p && tensorOf(v, p);
  if (!p || !t || !s.model.can_edit || p.editable === false || !t.how?.tensor || !s.heat || s.problem) return null;
  const [R, C] = s.heat.shape ?? [0, 0];
  const row = Math.max(0, Math.min(R - 1, s.row ?? 0)), col = Math.max(0, Math.min(C - 1, s.col ?? 0));
  return { p, t, row, col, R, C, value: s.heat.values[row - (s.heat.from ?? 0)]?.[col] };
}

/** The numbers on screen with a staged change applied: a copy with only the changed row replaced. `from` is the
 *  place of the first row on screen in the whole matrix. A staged change is {kind: "cell", row, col, value} or
 *  {kind: "row", row, op: "scale" | "zero", factor}. */
export function withPartStaged(values, from, staged) {
  const at = (staged?.row ?? -1) - from;
  if (!staged || !values[at]) return values;
  const out = values.slice();
  out[at] = staged.kind === "cell" ? values[at].map((x, c) => (c === staged.col ? staged.value : x)) : values[at].map((x) => (staged.op === "zero" ? 0 : x * staged.factor));
  return out;
}

/** What a staged change would do, in one sentence. */
export function partStagedWords(staged, was, C) {
  if (staged.kind === "cell") return `Not saved. ${signed(was)} will become ${signed(staged.value)}.`;
  const numbers = `${int(C)} ${C === 1 ? "number" : "numbers"} in this row`;
  return staged.op === "zero" ? `Not saved. ${numbers} will be set to zero.` : `Not saved. ${numbers} will be ${staged.factor === 0.5 ? "halved" : staged.factor === 2 ? "doubled" : `multiplied by ${staged.factor}`}.`;
}

/** The box starts as the number it would change. */
export function syncPartInput(v) {
  const t = partTarget(v), s = v.pm;
  s.typo = null;
  if (t && Number.isFinite(t.value)) { s.synced = String(Number(t.value.toFixed(4))); v.partInput.set_value(s.synced); }
}

/** Going anywhere else with a change not saved drops it, and one quiet line says so. */
export function leavePart(v) {
  const s = v.pm;
  if (!s) return;
  const p = partOf(v);
  s.dropped = s.staged && p ? `Your unsaved change to ${p.name} was dropped. Nothing was saved.` : null;
  s.staged = null; s.typo = null; s.handNote = null;
}

/** Stage what is typed in the box. It changes nothing in the model: "Set this value" does. */
export function stagePartTyped(v, cx) {
  const s = v.pm, t = s && partTarget(v);
  if (!t || s.busy || !Number.isFinite(t.value)) return;
  const text = String(v.partInput.value() ?? "").trim(), value = Number(text);
  s.dropped = null; s.handNote = null;
  if (text === s.synced || (text && value === t.value)) { s.typo = null; if (s.staged?.kind === "cell") s.staged = null; return cx.notify(); }
  if (!text || !Number.isFinite(value)) { s.typo = "Type a number, such as -1.5."; if (s.staged?.kind === "cell") s.staged = null; return cx.notify(); }
  s.typo = null;
  s.staged = { kind: "cell", row: t.row, col: t.col, value };
  cx.notify();
}

/** Stage halving, doubling or zeroing the picked row. */
export function stagePartRow(v, what, cx) {
  const s = v.pm, t = partTarget(v);
  if (!t || s.busy) return;
  s.dropped = null; s.handNote = null;
  syncPartInput(v);
  s.staged = { kind: "row", row: t.row, ...what };
  cx.notify();
}

/** Drop the staged change: the picture and the box go back to the saved numbers. */
export function putBackPart(v, cx) {
  const s = v.pm;
  if (!s || s.busy) return;
  s.staged = null;
  syncPartInput(v);
  cx.notify();
}

/** Pick the column, or a cell of the picture. A staged change elsewhere is dropped. */
export function pickCell(v, row, col, cx) {
  const s = v.pm, t = partTarget(v);
  if (!t) return;
  leavePart(v);
  s.col = Math.max(0, Math.min(t.C - 1, col));
  if (row != null && row !== t.row) { s.row = Math.max(0, Math.min(t.R - 1, row)); s.near = null; s.rowName = null; loadPart(v, cx); }
  syncPartInput(v);
  cx.notify();
}

/** Save the staged change as one edit, then read the part and the record again. This is the only place the
 *  page changes a trained model. A refusal leaves the change staged. */
export function savePartStaged(v, cx) {
  const s = v.pm, t = partTarget(v), staged = s?.staged;
  if (!t || !staged || s.busy || !s.session) return;
  s.busy = true; s.handNote = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await sendPartEdit(s.session, t.t, staged);
    if (v.pm !== s) return;
    s.busy = false;
    if (!r?.ok) s.handNote = plainError(r?.error, "Captain couldn't make that change.");
    else if (s.staged === staged) {
      // The numbers on screen take the saved ones at once, so the picture does not jump back while it reads again.
      if (s.heat) s.heat = { ...s.heat, values: withPartStaged(s.heat.values, s.heat.from ?? 0, staged) };
      s.staged = null;
      syncPartInput(v);
      loadPart(v, cx);
    }
    v.readPartsSession(cx);
    cx.notify();
  });
}

/** Take an edit back, then read again. */
export function undoPart(v, id, cx) {
  const s = v.pm;
  if (!s?.session || s.busy) return;
  s.busy = true; s.handNote = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await sendUndo(s.session, id, "you");
    if (v.pm !== s) return;
    s.busy = false;
    if (!r?.ok) s.handNote = plainError(r?.error, "Captain couldn't take that edit back.");
    else loadPart(v, cx);
    v.readPartsSession(cx);
    cx.notify();
  });
}

const stepper = (c, id, text, go, off) => div().id(id).h(26).px(9).flex().flex_none().items_center().text_size(12.5).border_1().bg(c.surface).border_color(c.grey300).text_color(off ? c.grey500 : c.navy)
  .when(!off, (e) => e.cursor_pointer().hover((x) => x.border_color(c.blue).text_color(c.blue)).on_click(go)).child(text);

/** A save button: outlined like every other, in the accent colour because something waits on it. */
const saveButton = (c, id, text, go, off) => div().id(id).h(26).px(10).flex().flex_none().items_center().justify_center().text_size(12.5).font_medium().border_1()
  .when(off, (e) => e.bg(c.surface2).text_color(c.grey500).border_color(c.rule))
  .when(!off, (e) => e.bg(c.surface).border_color(c.blue).text_color(c.blue).cursor_pointer().hover((x) => x.bg(c.blueLight)).on_click(go)).child(text);

/** The hand tools for the open part. Null when the model or this matrix cannot be changed here. */
export function PartHandBlock(c, v, names = null) {
  const s = v.pm, t = partTarget(v);
  if (!t) return null;
  const st = s.staged, busy = Boolean(s.busy), cell = st?.kind === "cell" ? st : null, whole = st?.kind === "row" ? st : null;
  const known = Number.isFinite(t.value);
  const rowWords = `Row ${int(t.row + 1)} of ${int(t.R)}${names?.[t.row] ? ` (${names[t.row]})` : ""}`;
  const pick = (id, text, what) => { const on = whole && whole.op === what.op && whole.factor === what.factor;
    return on ? div().id(id).h(26).px(10).flex().flex_none().items_center().text_size(12.5).font_medium().border_1().border_color(c.blue).bg(c.blueLight).text_color(c.blue).child(text)
      : Button(c, id, text, (_e, cx) => stagePartRow(v, what, cx), { small: true, disabled: busy }); };
  const choice = (id, text) => h_flex().id("wt-phand-staged").gap(6).flex_wrap()
    .child(saveButton(c, id, text, (_e, cx) => savePartStaged(v, cx), busy))
    .child(Button(c, "wt-phand-back", "Put it back", (_e, cx) => putBackPart(v, cx), { small: true, disabled: busy }));
  const mine = (v.session?.events ?? []).filter((e) => e.kind === "edit" && e.by === "you" && !e.undone).at(-1);
  return v_flex().id("wt-phand").gap(8).p(8).border_1().border_color(st ? c.blue : c.rule)
    .child(div().text_size(12.5).font_semibold().child("Change it by hand"))
    .child(h_flex().gap(6).items_center().flex_wrap()
      .child(div().id("wt-phand-row").flex_none().pr(6).text_size(12.5).text_color(c.navy).child(rowWords))
      .child(stepper(c, "wt-phand-row-prev", "Previous row", (_e, cx) => pickCell(v, t.row - 1, t.col, cx), t.row <= 0))
      .child(stepper(c, "wt-phand-row-next", "Next row", (_e, cx) => pickCell(v, t.row + 1, t.col, cx), t.row >= t.R - 1)))
    .child(h_flex().gap(6).items_center().flex_wrap()
      .child(div().id("wt-phand-col").flex_none().pr(6).text_size(12.5).text_color(c.navy).child(`Column ${int(t.col + 1)} of ${int(t.C)}`))
      .child(stepper(c, "wt-phand-col-prev", "Previous column", (_e, cx) => pickCell(v, null, t.col - 1, cx), t.col <= 0))
      .child(stepper(c, "wt-phand-col-next", "Next column", (_e, cx) => pickCell(v, null, t.col + 1, cx), t.col >= t.C - 1)))
    .child(div().id("wt-phand-target").text_size(12.5).line_height("17px").text_color(c.navy).child(known ? `The number at this row and column is ${signed(t.value)}.` : "Reading this row."))
    .child(h_flex().gap(8).items_center()
      .child(div().id("wt-phand-input").w(96).h(28).flex().flex_none().items_center().px(8).border_1().border_color(cell ? c.blue : c.grey300).cursor_text()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation?.()).child(Input.new(v.partInput).w_full().text_size(12.5).text_color(c.navy)))
      .child(div().flex_1().min_w(0).text_size(11.5).line_height("15px").text_color(c.grey500).child(cell ? "" : "Type a new value, or click a cell in the picture. Nothing changes until you save it.")))
    .when(Boolean(s.typo), (e) => e.child(div().id("wt-phand-typo").text_size(11.5).line_height("16px").text_color(c.grey500).child(s.typo)))
    .when(Boolean(cell), (e) => e
      .child(div().id("wt-phand-unsaved").text_size(12.5).line_height("17px").text_color(c.navy).child(partStagedWords(cell, t.value, t.C)))
      .child(choice("wt-phand-set", "Set this value")))
    .child(h_flex().flex_wrap().gap(6)
      .child(pick("wt-phand-halve", "Halve this row", { op: "scale", factor: 0.5 }))
      .child(pick("wt-phand-double", "Double this row", { op: "scale", factor: 2 }))
      .child(pick("wt-phand-zero", "Set this row to zero", { op: "zero" })))
    .when(Boolean(whole), (e) => e
      .child(div().id("wt-phand-unsaved").text_size(12.5).line_height("17px").text_color(c.navy).child(partStagedWords(whole, t.value, t.C)))
      .child(choice("wt-phand-save", "Save this change")))
    // The services do not measure an edit to this kind of model, and say so before anything is saved.
    .when(Boolean(s.model.edits_say), (e) => e.child(div().id("wt-phand-measure").text_size(11.5).line_height("16px").text_color(c.grey500).child(s.model.edits_say)))
    .when(Boolean(mine), (e) => e.child(h_flex().child(Button(c, "wt-phand-undo", "Take back my last edit", (_e, cx) => undoPart(v, mine.id, cx), { small: true, disabled: busy }))))
    .when(Boolean(s.handNote), (e) => e.child(div().id("wt-phand-note").text_size(12.5).line_height("17px").text_color(c.fail).child(s.handNote)));
}

/** What was changed in this model's session, newest first, in the services' own sentences. Null when nothing
 *  has been. */
export function ChangesList(c, v) {
  const evs = (v.session?.events ?? []).filter((e) => ["edit", "undo", "redo", "save"].includes(e.kind) && typeof e.sentence === "string" && e.sentence);
  if (!evs.length) return null;
  const shown = evs.slice(-SHOWN_CHANGES).reverse();
  return v_flex().id("wt-changes").gap(4)
    .child(div().text_size(12.5).font_semibold().child("What was changed"))
    .children(shown.map((e) => div().id(`wt-change-${e.id}`).text_size(12.5).line_height("17px").text_color(e.undone ? c.grey500 : c.navy).child(`${e.sentence}${e.undone ? " Taken back." : ""}`)))
    .when(evs.length > shown.length, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`and ${int(evs.length - shown.length)} earlier`)));
}
