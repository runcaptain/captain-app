// CAPTAIN: a model of parts on the Weights page: what the page keeps for one, what it reads when a part is
// opened, and the panel beside the drawing. A part opens to one plain sentence about what it is, a picture of its
// matrix, its largest rows, and for an embedding table a row to pick with the rows most like it. The rankers open
// to a bar for each input by its share of gain, the inputs no tree uses, and one tree drawn (partsdraw.js).
//
// Such a model is read here and nothing else: no session is opened on it, nothing is counted against an agent,
// and there is no editing, running, backward walk or deploy. Everything is read through adapter.js: the model
// when the page opens it (and again when the change feed says it changed), a part's numbers when a person opens
// the part. No timer.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { int } from "./kit/num.js";
import { Skeleton, plainError } from "./kit/ui.js";
import { readPartHeatmap, readPartRows, readPartNearest } from "./adapter.js";
import { signed, plain } from "./inspect.js";
import { Heat, reduceGrid, shapeWords, countWords, HEAT_MAX } from "./partsdraw.js";
import { serviceName } from "./hand.js";

export const READ_ONLY = "This model can be read here, not changed or run yet.";

/** What the page keeps for an open model of parts. */
export function partsState(model) {
  return { model, view: "model", sel: null, head: 0, tensor: null, row: null, heat: null, rows: null, near: null, problem: null, asks: 0, rankerAt: 0, treeAt: 0 };
}

/** "Session model with rankers, 2,146 numbers": the model in one line, for the list and the top of the page. */
export function partsFacts(m) {
  const kind = m.kind_words ?? "Trained model";
  return Number.isFinite(m.parameters) ? `${kind}, ${countWords(m.parameters)}` : kind;
}

/** What a part is, in one plain sentence, with the technical word first and plain words beside it. */
export function partSentence(p) {
  const [rows, cols] = p.shape ?? [];
  switch (p.kind) {
    case "embedding": return `A lookup table (an embedding): one row of ${int(cols)} numbers for each of ${int(rows)} ${p.rows_are ?? "things the model knows"}. Rows that are alike stand for things the model treats alike.`;
    case "projection": return `A projection: it turns ${int(cols)} numbers that describe a text into the ${int(rows)} numbers the model works in.`;
    case "attention": return `Attention: which earlier events this one looks at. ${int(p.heads ?? 1)} ${(p.heads ?? 1) === 1 ? "head" : "heads"}, each looking for something of its own.`;
    case "feed_forward": return `Feed-forward: after looking back, each event is worked on by itself, through a hidden step ${int(rows)} wide.`;
    case "norm": return "A norm (layer normalisation): it keeps the numbers in a steady range between one step and the next.";
    case "head": return `A head: it turns the model's last ${int(cols)} numbers into ${rows === 1 ? "one answer" : `${int(rows)} answers`}.`;
    default: return `${p.name}: ${shapeWords(p.shape)}.`;
  }
}

/** What one of a part's matrices is, in plain words. */
export function tensorSentence(p, t) {
  if (p.kind === "attention") {
    if (t.id === "qk") return "Query-key: how strongly an event with one pattern looks at an earlier event with another. A strong cell means that pair attends.";
    if (t.id === "ov") return "Output-value: what is written into the event when it looks. A strong cell means that pattern is copied across.";
    return `${t.name}: one of the four matrices the heads are built from.`;
  }
  return "";
}

/** The pin for the agent's context: the model, or the open part. */
export function partsPin(v) {
  const m = v.pm.model, name = serviceName(v.model), p = m.parts.find((x) => x.id === v.pm.sel);
  return p ? { ref: `weights:${name}@part=${p.id}`, kind: "weights", label: `${m.name}: ${p.name} (${shapeWords(p.shape)})` } : { ref: `weights:${name}`, kind: "weights", label: `${m.name}: the whole model` };
}

const partOf = (v) => v.pm.model.parts.find((p) => p.id === v.pm.sel) ?? null;
const tensorOf = (v, p = partOf(v)) => p?.tensors?.find((t) => t.id === v.pm.tensor) ?? p?.tensors?.[0] ?? null;

/** Read what the panel shows for the open part: its matrix, its largest rows, and for a table with a row picked
 *  the rows most like it. An answer to an older question is dropped. */
export function loadPart(v, cx) {
  const s = v.pm, p = partOf(v), t = tensorOf(v);
  if (!p || !t) return;
  const mine = ++s.asks, which = { tensor: t.id, head: t.per_head ? s.head : undefined };
  cx.spawn(async (cx) => {
    const [heat, rows, near] = await Promise.all([
      readPartHeatmap(v.model, p.id, which), readPartRows(v.model, p.id, which),
      p.kind === "embedding" && s.row != null ? readPartNearest(v.model, p.id, s.row) : Promise.resolve(null),
    ]);
    if (v.pm !== s || mine !== s.asks) return;
    s.problem = heat?.ok ? null : plainError(heat?.error, "Captain couldn't read that part.");
    if (heat?.ok) s.heat = { ...heat.data, grid: reduceGrid(heat.data.values) };
    s.rows = rows?.ok && Array.isArray(rows.data) ? rows.data : s.rows ?? [];
    // The rows most like the picked one, and what the picked row stands for.
    const list = near?.ok ? (Array.isArray(near.data) ? near.data : near.data?.nearest) : null;
    s.near = near == null ? null : Array.isArray(list) ? list : [];
    s.rowName = near?.ok && near.data?.row === s.row ? near.data.name ?? null : null;
    cx.notify();
  });
}

/** Open a part (or close it, on a second click with no head named). */
export function openPart(v, id, head, cx) {
  const s = v.pm;
  if (s.sel === id && head == null) { s.sel = null; return cx.notify(); }
  const same = s.sel === id;
  s.sel = id;
  if (!same) { s.tensor = null; s.row = null; s.rowName = null; s.heat = null; s.rows = null; s.near = null; s.problem = null; s.head = 0; }
  if (head != null) { s.head = head; const p = partOf(v); if (!tensorOf(v, p)?.per_head) s.tensor = p.tensors.find((t) => t.per_head)?.id ?? s.tensor; }
  if (partOf(v)?.kind === "embedding" && s.row == null) s.row = 0;
  loadPart(v, cx);
  cx.notify();
}
export function pickTensor(v, id, cx) { v.pm.tensor = id; v.pm.heat = null; v.pm.rows = null; loadPart(v, cx); cx.notify(); }
export function pickHead(v, n, cx) { v.pm.head = n; loadPart(v, cx); cx.notify(); }
export function pickRow(v, row, cx) {
  const p = partOf(v), n = p?.shape?.[0] ?? 1;
  v.pm.row = Math.max(0, Math.min(n - 1, row)); v.pm.near = null; v.pm.rowName = null;
  loadPart(v, cx); cx.notify();
}

const chip = (c, id, text, on, go, tip = null) => div().id(id).h(26).px(9).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
  .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
  .when(Boolean(tip), (e) => e.tooltip(tip)).on_click(go).child(text);
const label = (c, text) => div().text_size(11.5).text_color(c.grey500).child(text);
const heading = (text) => div().text_size(12.5).font_semibold().child(text);
const rowName = (r) => (r.name ? `${r.name}` : `Row ${int(r.row + 1)}`);

/** A row of a list with a bar: a name, a bar for its size against the largest, and a number in words. */
function barRow(c, id, name, share, right, { on = false, go = null, tip = null } = {}) {
  return h_flex().id(id).h(24).gap(8).items_center().px(4).when(on, (e) => e.bg(c.blueLight))
    .when(Boolean(go), (e) => e.cursor_pointer().hover((s) => s.bg(c.blueLight)).on_click(go))
    .when(Boolean(tip), (e) => e.tooltip(tip))
    .child(div().w(118).flex_none().truncate().text_size(12.5).text_color(c.navy).child(name))
    .child(div().w(56).h(6).flex_none().bg(c.surface2).child(div().h_full().w(Math.max(share > 0 ? 1 : 0, Math.round(56 * Math.min(1, Math.max(0, share))))).bg(c.blue)))
    .child(div().flex_1().min_w(0).truncate().text_right().text_size(11.5).text_color(c.grey500).child(right));
}

/** The panel for the open part. `v` is the page, `width` the panel's inner width. */
export function PartPanel(c, v, width) {
  const s = v.pm, p = partOf(v);
  if (!p) {
    return v_flex().id("wt-part").gap(8)
      .child(heading("Open a part"))
      .child(div().text_size(12.5).line_height("18px").text_color(c.grey500).child("Click a block in the drawing to see what it is, a picture of its numbers and its largest rows."));
  }
  const t = tensorOf(v), table = p.kind === "embedding";
  const fact = (id, name, value) => v_flex().gap(2).child(label(c, name)).child(div().id(id).text_size(15).font_semibold().child(value));
  let out = v_flex().id("wt-part").gap(12)
    .child(div().id("wt-part-name").text_size(12.5).font_semibold().child(p.name))
    .child(div().id("wt-part-says").text_size(12.5).line_height("18px").text_color(c.navy).child(partSentence(p)))
    .child(h_flex().gap(18)
      .child(fact("wt-part-shape", "Shape", shapeWords(p.shape)))
      .child(fact("wt-part-count", "Numbers", int(p.parameters))));
  // Which of its matrices is showing, and for attention which head.
  if ((p.tensors?.length ?? 0) > 1) out = out.child(v_flex().gap(4).child(label(c, "Matrix"))
    .child(h_flex().id("wt-part-tensors").gap(6).flex_wrap().children(p.tensors.map((x) => chip(c, `wt-part-tensor-${x.id}`, x.name, x.id === t.id, (_e, cx) => pickTensor(v, x.id, cx), `Show ${x.name}, ${shapeWords(x.shape)}`)))));
  if (t?.per_head && (p.heads ?? 1) > 1) out = out.child(h_flex().id("wt-part-heads").gap(6).flex_wrap()
    .children(Array.from({ length: p.heads }, (_x, n) => chip(c, `wt-part-head-${n}`, `Head ${int(n + 1)}`, n === s.head, (_e, cx) => pickHead(v, n, cx)))));
  const says = t ? tensorSentence(p, t) : "";
  if (says) out = out.child(div().id("wt-part-tensor-says").text_size(11.5).line_height("16px").text_color(c.grey500).child(says));
  if (s.problem) return out.child(div().id("wt-part-problem").text_size(12.5).line_height("18px").text_color(c.fail).child(s.problem));
  if (!s.heat) return out.child(Skeleton(c, 4));
  // The picture. A matrix too large to draw cell by cell is averaged down, and the panel says so.
  const g = s.heat.grid, [R, C] = s.heat.shape ?? [g.rows, g.cols];
  const reduced = s.heat.reduced || g.reduced;
  const mark = table && s.row != null ? Math.floor((s.row * g.rows) / Math.max(1, R)) : null;
  out = out.child(v_flex().gap(4)
    .child(heading(`The numbers as a picture (${shapeWords([R, C])})`))
    .child(Heat(c, "wt-part-heat", g, { width: Math.max(120, width), mark }))
    .child(div().id("wt-part-heat-says").text_size(11.5).line_height("16px").text_color(c.grey500)
      .child(`Each cell is one number: blue is positive, red is negative, stronger is larger, blank is near zero.${reduced ? ` Shown reduced: ${shapeWords([R, C])} drawn as ${shapeWords([g.rows, g.cols])}, each cell an average.` : ""}`)));
  // The largest rows, by their length as a vector. In a table a click picks the row.
  const rows = s.rows ?? [], top = Math.max(1e-12, ...rows.map((r) => r.norm ?? 0));
  if (rows.length) out = out.child(v_flex().id("wt-part-rows").gap(2)
    .child(h_flex().pb(2).child(div().flex_1().text_size(12.5).font_semibold().child("Largest rows")).child(label(c, "size, and its largest number")))
    .children(rows.map((r) => barRow(c, `wt-part-row-${r.row}`, rowName(r), (r.norm ?? 0) / top, `${plain(r.norm)}, ${plain(r.largest)}`,
      { on: table && r.row === s.row, go: table ? (_e, cx) => pickRow(v, r.row, cx) : null, tip: table ? "Pick this row" : `Row ${int(r.row + 1)}: its size is its length as a vector` }))));
  if (table) {
    const named = s.rowName ?? rows.find((r) => r.row === s.row)?.name ?? null;
    const step = (id, text, to, off) => div().id(id).h(26).px(9).flex().flex_none().items_center().text_size(12.5).border_1().bg(c.surface).border_color(c.grey300).text_color(off ? c.grey500 : c.navy)
      .when(!off, (e) => e.cursor_pointer().hover((x) => x.border_color(c.blue).text_color(c.blue)).on_click((_e, cx) => pickRow(v, to, cx))).child(text);
    out = out.child(v_flex().id("wt-part-near").gap(4)
      .child(heading("Rows most like this one"))
      .child(h_flex().gap(6).items_center().flex_wrap()
        .child(div().id("wt-part-row-at").text_size(12.5).text_color(c.navy).child(`Row ${int(s.row + 1)} of ${int(R)}${named ? `: ${named}` : ""}`))
        .child(div().flex_1())
        .child(step("wt-part-row-prev", "Previous row", s.row - 1, s.row <= 0))
        .child(step("wt-part-row-next", "Next row", s.row + 1, s.row >= R - 1)))
      .when(!s.near, (e) => e.child(Skeleton(c, 3)))
      .when(Boolean(s.near) && s.near.length === 0, (e) => e.child(label(c, "No other row to compare it with.")))
      .children((s.near ?? []).map((r) => barRow(c, `wt-part-near-${r.row}`, rowName(r), Math.max(0, r.similarity), `alike ${signed(r.similarity)}`,
        { go: (_e, cx) => pickRow(v, r.row, cx), tip: "How alike two rows are, from -1 (opposite) to +1 (the same direction). Click to pick this row" }))));
  }
  return out;
}

/** The ranker that is showing. */
export const rankerOf = (v) => v.pm.model.rankers?.[v.pm.rankerAt] ?? null;

/** "41% of gain, 12 splits". */
export function gainWords(i) {
  const pct = Math.round((i.gain_share ?? 0) * 100);
  return `${pct}% of gain, ${int(i.splits ?? 0)} ${i.splits === 1 ? "split" : "splits"}`;
}

/** Which tree is drawn, with the steps to the one before and after: the line over the tree. */
export function TreeBar(c, v) {
  const s = v.pm, r = rankerOf(v), shown = r?.shown ?? [];
  const step = (id, text, to, off) => div().id(id).h(26).px(9).flex().flex_none().items_center().text_size(12.5).border_1().bg(c.surface).border_color(c.grey300).text_color(off ? c.grey500 : c.navy)
    .when(!off, (e) => e.cursor_pointer().hover((x) => x.border_color(c.blue).text_color(c.blue)).on_click((_e, cx) => { s.treeAt = to; v.treePz = null; cx.notify(); })).child(text);
  return h_flex().id("wt-ranker-trees").h(36).flex_none().gap(6).items_center()
    .child(div().id("wt-ranker-tree-at").flex_1().min_w(0).truncate().text_size(12.5).text_color(c.navy)
      .child(shown.length ? `Tree ${int(s.treeAt + 1)} of the first ${int(shown.length)} is drawn${(r.trees ?? 0) > shown.length ? ` (the ranker has ${int(r.trees)})` : ""}.` : ""))
    .when(shown.length > 1, (e) => e
      .child(step("wt-ranker-tree-prev", "Previous tree", s.treeAt - 1, s.treeAt <= 0))
      .child(step("wt-ranker-tree-next", "Next tree", s.treeAt + 1, s.treeAt >= shown.length - 1)));
}

/** The rankers' panel: which ranker, the inputs it never uses, and a bar for each input by its share of gain. */
export function RankerPanel(c, v) {
  const s = v.pm, all = s.model.rankers ?? [], r = rankerOf(v);
  if (!r) return v_flex().id("wt-ranker").child(label(c, "This model has no rankers."));
  const unused = r.unused ?? [], inputs = (r.inputs ?? []).slice().sort((a, b) => (b.gain_share ?? 0) - (a.gain_share ?? 0));
  const top = Math.max(1e-12, ...inputs.map((i) => i.gain_share ?? 0));
  const shown = r.shown ?? [];
  return v_flex().id("wt-ranker").gap(12)
    .when(all.length > 1, (e) => e.child(h_flex().id("wt-ranker-pick").gap(6).flex_wrap()
      .children(all.map((x, n) => chip(c, `wt-ranker-${x.id}`, x.name, n === s.rankerAt, (_e, cx) => { s.rankerAt = n; s.treeAt = 0; v.treePz = null; cx.notify(); })))))
    .child(div().id("wt-ranker-name").text_size(12.5).font_semibold().child(r.name))
    .child(div().id("wt-ranker-says").text_size(12.5).line_height("18px").text_color(c.navy)
      .child(`A ranker made of ${int(r.trees ?? shown.length)} small decision trees. Each tree asks a few yes or no questions about an item and adds a little to its score.`))
    .child(div().id("wt-ranker-unused").p(8).bg(c.surface2).text_size(12.5).line_height("18px").text_color(c.navy)
      .child(unused.length ? `Never used by this ranker: ${int(unused.length)} ${unused.length === 1 ? "input" : "inputs"} (${unused.join(", ")}).` : "Every input is used by this ranker."))
    .child(v_flex().id("wt-ranker-inputs").gap(2)
      .child(h_flex().pb(2).child(div().flex_1().text_size(12.5).font_semibold().child(`Inputs (${int(inputs.length)})`)).child(label(c, "share of gain")))
      .child(div().pb(2).text_size(11.5).line_height("16px").text_color(c.grey500).child("Gain is how much an input's questions improved the ranker while it was trained. A split is one question."))
      .children(inputs.map((i, n) => barRow(c, `wt-ranker-input-${n}`, i.name, (i.gain_share ?? 0) / top, gainWords(i)))));
}

export { HEAT_MAX };
