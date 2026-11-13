// CAPTAIN: "Save this version" on the Weights page. Changes made in a session (a person's saved edits, an agent's
// kept result) live in the session until someone saves them as a new version of the network. Nothing does that
// but this button and "Deploy this version", which says that it saves. While the session holds changes that are
// not in a version, a line under the page's top says how many, with the button. After the press it says the
// version's name and number.
import { div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { Button, plainError } from "./kit/ui.js";
import { int } from "./kit/num.js";
import { sendSave } from "./adapter.js";

export const SAVE_H = 38; // the line's height

/** How many numbers the session has changed that are in no saved version: the ledger's count, unless the last
 *  thing that changed or saved the numbers was a save. A network kept in this page has no versions to save to. */
export function unsavedOf(v) {
  const s = v.session;
  if (!s || String(v.model ?? "").startsWith("starter:")) return 0;
  const last = (s.events ?? []).filter((e) => ["edit", "undo", "redo", "save"].includes(e.kind)).at(-1);
  if (!last || last.kind === "save") return 0;
  return Math.max(0, Number(s.ledger?.changed) || 0);
}

/** "tiny, version 2" from the services' ref "tiny@v2". */
export const versionWords = (ref) => String(ref ?? "").replace(/@v(\d+)$/, ", version $1");

/** Whether the line shows: there is something to save, or a save was just made or refused. */
export const saveShows = (v) => unsavedOf(v) > 0 || Boolean(v.saved);

/** Save the session's changes as a new version, then read again. */
export function saveVersion(v, cx) {
  if (!v.session || v.saved?.state === "saving") return;
  v.saved = { state: "saving" };
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await sendSave(v.session.id);
    v.saved = r?.ok ? { state: "done", text: `Saved as ${versionWords(r.data?.ref ?? `${r.data?.model}@v${r.data?.version}`)}.`, note: r.data?.not_measured ?? "" }
      : { state: "failed", text: plainError(r?.error, "Captain couldn't save that version.") };
    v.read(cx);
    cx.notify();
  });
}

/** The line. `v` is the page. */
export function SaveLine(c, v) {
  const n = unsavedOf(v), s = v.saved, saving = s?.state === "saving";
  const text = n > 0 && s?.state !== "failed" ? `${int(n)} ${n === 1 ? "change" : "changes"} not saved as a version.` : s?.text ?? "";
  return h_flex().id("wt-save").h(SAVE_H).flex_none().px(20).gap(10).items_center().border_b(1).border_color(c.rule).bg(c.surface2).text_size(12.5)
    .child(div().id("wt-save-text").min_w(0).truncate().text_color(s?.state === "failed" ? c.fail : c.navy).tooltip(text).child(saving ? "Saving this version." : text))
    // What the services say of a version of a model that cannot be run here: it was not measured.
    .when(n === 0 && Boolean(s?.note), (e) => e.child(div().id("wt-save-note").min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(s.note)))
    .child(div().flex_1())
    .when(n > 0, (e) => e.child(Button(c, "wt-save-go", "Save this version", (_e, cx) => saveVersion(v, cx), { small: true, disabled: saving })))
    .when(n === 0 && Boolean(s) && !saving, (e) => e.child(div().id("wt-save-close").flex_none().text_size(11.5).text_color(c.grey500).cursor_pointer().hover((x) => x.text_color(c.navy))
      .on_click((_e, cx) => { v.saved = null; cx.notify(); }).child("Hide")));
}
