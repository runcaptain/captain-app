// CAPTAIN: "Bring in a trained model" on the list of networks. A person says once where their trained models are
// kept (a folder or a storage address). Captain's services list what is there, the person chooses one, gives it
// a name, and presses "Bring it in": the services copy that model's files into this workspace, and write nothing
// where they are kept.
//
// The place is typed by the person and kept in a small file in the app's own folder on this computer. It is never
// in the code and never drawn on the page once typed: a row shows the last three parts of its path, and a
// refusal that names the place has it taken out.
import { div } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { appHome, readTextFile, writeTextFile, exec } from "herdr";
import { Button, Skeleton, plainError } from "./kit/ui.js";
import { int } from "./kit/num.js";
import { readRegistry, sendImport } from "./adapter.js";

const SHOWN = 12; // rows listed before the rest are counted
const folder = () => `${appHome()}/captain`;
const file = () => `${folder()}/weights-models-from.json`;

/** Where the person said their trained models are kept, or "" when they have not said. */
export async function readWhere() {
  let raw = null;
  try { raw = await readTextFile(file()); } catch { raw = null; }
  try { const from = JSON.parse(raw ?? "{}").from; return { raw, from: typeof from === "string" ? from : "" }; } catch { return { raw, from: "" }; }
}
/** Keep the place. The app replaces a file only when told the text it replaces, so it is read first. */
export async function keepWhere(from) {
  try { await exec("mkdir", ["-p", folder()]); } catch { /* it exists, or the write says why not */ }
  const { raw } = await readWhere();
  try { await writeTextFile(file(), JSON.stringify({ from }, null, 2), raw ?? undefined); return true; } catch { return false; }
}

/** The last three parts of a path: "arch-a/shop/v2". */
export const pathTail = (path) => String(path ?? "").split("/").filter(Boolean).slice(-3).join("/");
/** A name to suggest for a model, from its path: the last two parts, in the letters a name may hold. */
export const nameFor = (path) => String(path ?? "").split("/").filter(Boolean).slice(-2).join("-").toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
/** A sentence from the services with the place itself taken out. */
const without = (text, from) => (from ? String(text).split(from).join("that place") : String(text));

/** List what can be brought in from the kept place. */
export function list(v, cx, from) {
  const b = v.bring = { step: "list", from, rows: null, problem: null };
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await readRegistry(from);
    if (v.bring !== b) return;
    if (r?.ok) b.rows = Array.isArray(r.data?.versions) ? r.data.versions : [];
    else { b.rows = []; b.problem = without(plainError(r?.error, "Captain couldn't list the models kept there."), from); }
    cx.notify();
  });
}

/** Open the picker: ask where the models are kept the first time, else list them. */
export function openBringIn(v, cx) {
  v.bring = { step: "reading" };
  cx.notify();
  cx.spawn(async (cx) => {
    const { from } = await readWhere();
    if (v.bring?.step !== "reading") return;
    if (from) list(v, cx, from); else { v.whereInput.set_value(""); v.bring = { step: "where" }; cx.notify(); }
  });
}

/** Keep the place that was typed, then list it. */
export function keepTyped(v, cx) {
  const from = String(v.whereInput.value() ?? "").trim();
  if (!from) { v.bring = { step: "where", problem: "Type the folder or the storage address first." }; return cx.notify(); }
  cx.spawn(async (cx) => {
    const kept = await keepWhere(from);
    v.whereInput.set_value("");
    list(v, cx, from);
    if (!kept && v.bring) v.bring.note = "Captain couldn't keep that place on this computer, so it will ask again next time.";
    cx.notify();
  });
}

/** Choose one of the listed models: ask for its name. Nothing is brought in yet. */
export function choose(v, row, cx) {
  v.nameInput.set_value(nameFor(row.path));
  v.bring = { ...v.bring, step: "name", picked: row, problem: null };
  cx.notify();
}

/** Bring the chosen model in under the typed name. The one request that copies anything. */
export function bringIn(v, cx) {
  const b = v.bring, name = String(v.nameInput.value() ?? "").trim();
  if (!b?.picked || b.step === "working") return;
  if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) { b.problem = "Give it a name in small letters, numbers and dashes, such as shop-model."; return cx.notify(); }
  b.step = "working"; b.problem = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await sendImport(b.picked.address, name);
    if (v.bring !== b) return;
    if (r?.ok) { v.bring = { step: "done", says: `Brought in as ${r.data?.model ?? name}. Its files were copied into this workspace.`, model: r.data?.model ?? name }; v.loadList(cx); }
    else { b.step = "name"; b.problem = without(plainError(r?.error, "Captain couldn't bring that model in."), b.from); }
    cx.notify();
  });
}

const quiet = (c, id, text, go) => div().id(id).flex_none().text_size(11.5).text_color(c.blue).cursor_pointer().hover((x) => x.text_color(c.navy)).on_click(go).child(text);
const box = (c, id, state, w) => div().id(id).w(w).h(28).flex().flex_none().items_center().px(8).border_1().border_color(c.grey300).bg(c.surface).cursor_text().child(Input.new(state).w_full().text_size(12.5).text_color(c.navy));

/** The button, and under it the picker while it is open. `v` is the page. */
export function BringIn(c, v) {
  const b = v.bring;
  if (!b) return h_flex().child(Button(c, "wt-bring", "Bring in a trained model", (_e, cx) => openBringIn(v, cx), { small: true }));
  const close = quiet(c, "wt-bring-close", b.step === "done" ? "Hide" : "Close without bringing anything in", (_e, cx) => { v.bring = null; cx.notify(); });
  let body;
  if (b.step === "reading") body = Skeleton(c, 2);
  else if (b.step === "where") body = v_flex().gap(6)
    .child(div().text_size(12.5).text_color(c.navy).child("Where your trained models are kept"))
    .child(div().text_size(11.5).line_height("16px").text_color(c.grey500).child("A folder on this computer or a storage address. Type it once: Captain keeps it on this computer, only reads there, and does not show it again."))
    .child(h_flex().gap(8).items_center().child(box(c, "wt-bring-where", v.whereInput, 320)).child(Button(c, "wt-bring-keep", "Keep this place and list it", (_e, cx) => keepTyped(v, cx), { small: true })));
  else if (b.step === "done") body = div().id("wt-bring-done").text_size(12.5).line_height("18px").text_color(c.navy).child(b.says);
  else if (b.step === "list") {
    const rows = b.rows ?? [];
    body = v_flex().gap(6)
      .child(h_flex().gap(10).items_center()
        .child(div().id("wt-bring-count").flex_1().min_w(0).text_size(12.5).text_color(c.navy).child(!b.rows ? "Listing what is kept there." : rows.length ? `${int(rows.length)} ${rows.length === 1 ? "model" : "models"} can be brought in from where yours are kept.` : b.problem ? "" : "Nothing that can be brought in was found there."))
        .child(quiet(c, "wt-bring-change", "Change where they are kept", (_e, cx) => { v.whereInput.set_value(""); v.bring = { step: "where" }; cx.notify(); })))
      .when(!b.rows, (e) => e.child(Skeleton(c, 3)))
      .child(v_flex().children(rows.slice(0, SHOWN).map((r, n) => h_flex().id(`wt-bring-row-${n}`).py(6).gap(10).items_center().when(n > 0, (e) => e.border_t(1).border_color(c.rule))
        .child(v_flex().flex_1().min_w(0).gap(1)
          .child(div().text_size(12.5).font_medium().truncate().child(pathTail(r.path)))
          .child(div().text_size(11.5).line_height("16px").text_color(c.grey500).truncate().child(r.description || "No description was saved with it.")))
        .child(Button(c, `wt-bring-choose-${n}`, "Choose this one", (_e, cx) => choose(v, r, cx), { small: true })))))
      .when(rows.length > SHOWN, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`and ${int(rows.length - SHOWN)} more, not listed here`)));
  } else {
    const working = b.step === "working";
    body = v_flex().gap(6)
      .child(div().id("wt-bring-picked").text_size(12.5).text_color(c.navy).child(`Bring in ${pathTail(b.picked.path)}`))
      .child(div().id("wt-bring-copies").text_size(11.5).line_height("16px").text_color(c.grey500).child("This copies the model's files into this workspace. Nothing is written where they are kept."))
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(div().flex_none().text_size(12.5).text_color(c.grey500).child("Name it"))
        .child(box(c, "wt-bring-name", v.nameInput, 200))
        // The deliberate press: outlined, in the accent colour.
        .child(div().id("wt-bring-go").h(26).px(10).flex().flex_none().items_center().text_size(12.5).font_medium().border_1()
          .when(working, (e) => e.bg(c.surface2).text_color(c.grey500).border_color(c.rule))
          .when(!working, (e) => e.bg(c.surface).border_color(c.blue).text_color(c.blue).cursor_pointer().hover((x) => x.bg(c.blueLight)).on_click((_e, cx) => bringIn(v, cx)))
          .child(working ? "Bringing it in" : "Bring it in"))
        .when(!working, (e) => e.child(Button(c, "wt-bring-other", "Choose another", (_e, cx) => { v.bring = { ...b, step: "list", picked: null, problem: null }; cx.notify(); }, { small: true }))));
  }
  return v_flex().id("wt-bring-panel").max_w(560).p(12).gap(10).border_1().border_color(c.rule).bg(c.surface)
    .child(h_flex().child(div().flex_1().text_size(12.5).font_semibold().child("Bring in a trained model")).child(close))
    .child(body)
    .when(Boolean(b.note), (e) => e.child(div().id("wt-bring-note").text_size(11.5).line_height("16px").text_color(c.grey500).child(b.note)))
    .when(Boolean(b.problem), (e) => e.child(div().id("wt-bring-problem").text_size(12.5).line_height("18px").text_color(c.fail).child(b.problem)));
}
