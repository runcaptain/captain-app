// CAPTAIN: "Bring in a trained model" on the list of networks. It opens straight onto what the workspace's own
// model registries hold: Captain already knows them, so nobody types an address. Each registry is a group that
// folds, each version a quiet row with its one-line description and a small capsule when it is already here or
// cannot be opened. A box filters the rows. The person chooses a version, gives it a name, and presses "Bring it
// in": only that press copies the model's files into this workspace, and nothing is written where they are kept.
// A version that is already here is opened, not copied again.
//
// With no registry connected, one sentence says so and one button opens Data, where a registry is connected.
// A model kept somewhere else (a folder on this computer) is behind "From somewhere else": the person types that
// place once, and it is kept in a small file in the app's own folder. No place is ever in the code, and none is
// drawn on the page: a row shows a name or the last parts of a path, and a refusal that names a place has it
// taken out.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { appHome, readTextFile, writeTextFile, exec, setShared, run } from "herdr";
import { Button, Skeleton, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int } from "./kit/num.js";
import { readRegistry, sendImport } from "./adapter.js";

const SHOWN = 12;   // rows of a typed place listed before the rest are counted
const ROW_H = 40;
const folder = () => `${appHome()}/captain`;
const file = () => `${folder()}/weights-models-from.json`;

/** Where the person said their other trained models are kept, or "" when they have not said. */
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
/** A sentence from the services with a place taken out of it. */
const without = (text, ...places) => places.filter(Boolean).reduce((t, p) => t.split(p).join("that place"), String(text));

/** The versions of a registry that the filter keeps: every word typed must be in the version's name, its
 *  description, its path, or the name it has here. */
export function filtered(versions, text) {
  const words = String(text ?? "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return versions ?? [];
  return (versions ?? []).filter((v) => { const hay = `${v.name ?? ""} ${v.description ?? ""} ${v.path ?? ""} ${v.here ?? ""}`.toLowerCase(); return words.every((w) => hay.includes(w)); });
}

/** Open the picker on what the workspace's model registries hold. */
export function openBringIn(v, cx) {
  const b = v.bring = { step: "registries", groups: null, problem: null, folded: [] };
  v.filterInput.set_value("");
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await readRegistry();
    if (v.bring !== b) return;
    if (r?.ok) b.groups = Array.isArray(r.data?.registries) ? r.data.registries : [];
    else { b.groups = []; b.problem = plainError(r?.error, "Captain couldn't read the workspace's model registries."); }
    cx.notify();
  });
}

/** List what can be brought in from a typed place ("From somewhere else"). */
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

/** "From somewhere else": ask where the first time, else list the kept place. */
export function openElsewhere(v, cx) {
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

/** Choose one version: ask for its name. Nothing is brought in yet. The step it came from is kept, to go back. */
export function choose(v, row, cx) {
  const b = v.bring;
  v.nameInput.set_value(nameFor(row.path ?? row.name));
  v.bring = { ...b, back: b.step, step: "name", picked: row, problem: null };
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
    else { b.step = "name"; b.problem = without(plainError(r?.error, "Captain couldn't bring that model in."), b.picked.address, b.from); }
    cx.notify();
  });
}

/** Open Data on its sources, where a model registry is connected to the workspace. */
function connectRegistry() {
  setShared("captain.open", { section: "data", view: "models", at: Date.now() });
  run("layout.select", { stack: "sections", child: "data" });
}

const quiet = (c, id, text, go) => div().id(id).flex_none().text_size(11.5).text_color(c.blue).cursor_pointer().hover((x) => x.text_color(c.navy)).on_click(go).child(text);
const box = (c, id, state, w) => div().id(id).w(w).h(28).flex().flex_none().items_center().px(8).border_1().border_color(c.grey300).bg(c.surface).cursor_text().child(Input.new(state).w_full().text_size(12.5).text_color(c.navy));
/** A fine outlined capsule: a state in a few words. */
const capsule = (c, id, text, tip = null) => div().id(id).h(20).px(8).flex().flex_none().items_center().rounded_full().border_1().border_color(c.rule).text_size(11.5).text_color(c.grey500)
  .when(Boolean(tip), (e) => e.tooltip(tip)).child(text);

/** One version of a registry: its name bright, its description muted, a capsule for its state, and one action. */
function versionRow(c, v, g, k, row) {
  const id = `wt-bring-v-${g}-${k}`;
  return h_flex().id(id).min_h(ROW_H).px(8).gap(10).items_center().rounded(6).hover((s) => s.bg(c.surface2))
    .child(div().flex_none().max_w(200).truncate().text_size(12.5).font_medium().text_color(c.navy).child(row.name ?? row.id ?? pathTail(row.path)))
    .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(row.description || "No description was saved with it."))
    .when(Boolean(row.here), (e) => e.child(capsule(c, `${id}-here`, `Already here as ${row.here}`)))
    .when(!row.here && !row.can_open, (e) => e.child(capsule(c, `${id}-cannot`, "Cannot be opened here", row.why_not ?? "This version holds no session model file.")))
    // Already here: opened, never copied again.
    .when(Boolean(row.here), (e) => e.child(Button(c, `${id}-open`, "Open it", (_e, cx) => { v.bring = null; v.open(row.here, cx); }, { small: true })))
    .when(!row.here && Boolean(row.can_open), (e) => e.child(Button(c, `${id}-choose`, "Choose this one", (_e, cx) => choose(v, row, cx), { small: true })));
}

/** The workspace's registries, as groups that fold. */
function registries(c, v) {
  const b = v.bring, groups = b.groups ?? [], text = String(v.filterInput.value() ?? "");
  const other = h_flex().pt(4).child(quiet(c, "wt-bring-elsewhere", "From somewhere else", (_e, cx) => openElsewhere(v, cx)));
  if (!b.groups) return v_flex().gap(8).child(div().text_size(12.5).text_color(c.grey500).child("Reading the workspace's model registries.")).child(Skeleton(c, 3));
  if (b.problem) return v_flex().gap(8).child(other);
  if (!groups.length) return v_flex().gap(8)
    .child(div().id("wt-bring-none").text_size(12.5).line_height("18px").text_color(c.navy).child("No model registry is connected to this workspace yet. Connect one, and its trained models show here to bring in."))
    .child(h_flex().child(Button(c, "wt-bring-connect", "Connect a model registry", () => connectRegistry(), { small: true })))
    .child(other);
  const total = groups.reduce((n, g) => n + (g.versions ?? []).length, 0);
  const shown = groups.map((g) => filtered(g.versions, text));
  const kept = shown.reduce((n, rows) => n + rows.length, 0);
  return v_flex().gap(6)
    .child(h_flex().gap(10).items_center()
      .child(box(c, "wt-bring-filter", v.filterInput, 220))
      .child(div().id("wt-bring-count").flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500)
        .child(text.trim() ? `${int(kept)} of ${int(total)} ${total === 1 ? "version" : "versions"} match. Newest first.` : `${int(total)} ${total === 1 ? "version" : "versions"} in your model ${groups.length === 1 ? "registry" : "registries"}. Newest first.`)))
    .child(v_flex().id("wt-bring-groups").gap(2).children(groups.flatMap((g, n) => {
      const folded = b.folded.includes(g.id), rows = shown[n];
      return [
        h_flex().id(`wt-bring-g-${n}`).h(30).px(8).gap(8).items_center().rounded(6).bg(c.surface2).cursor_pointer()
          .tooltip(folded ? `Show what ${g.name} holds` : `Fold ${g.name}`)
          .on_click((_e, cx) => { b.folded = folded ? b.folded.filter((x) => x !== g.id) : [...b.folded, g.id]; cx.notify(); })
          .child(svg(icon(folded ? "chevron-right" : "chevron-down")).size(13).flex_none().text_color(c.grey500))
          .child(div().text_size(12.5).font_medium().text_color(c.navy).child(g.name))
          .child(div().id(`wt-bring-g-${n}-count`).text_size(11.5).text_color(c.grey500).child(int(rows.length))),
        ...(folded ? [] : rows.map((row, k) => versionRow(c, v, n, k, row))),
        ...(!folded && !rows.length ? [div().px(8).py(6).text_size(11.5).text_color(c.grey500).child(g.read === false ? "Captain has not read this registry yet. Open Data to read it." : text.trim() ? "Nothing here matches." : "This registry holds no versions yet.")] : []),
      ];
    })))
    .child(other);
}

/** The button, and under it the picker while it is open. `v` is the page. */
export function BringIn(c, v) {
  const b = v.bring;
  if (!b) return h_flex().child(Button(c, "wt-bring", "Bring in a trained model", (_e, cx) => openBringIn(v, cx), { small: true }));
  const close = quiet(c, "wt-bring-close", b.step === "done" ? "Hide" : "Close without bringing anything in", (_e, cx) => { v.bring = null; cx.notify(); });
  const toRegistries = quiet(c, "wt-bring-registries", "Back to your model registries", (_e, cx) => openBringIn(v, cx));
  let body;
  if (b.step === "registries") body = registries(c, v);
  else if (b.step === "reading") body = Skeleton(c, 2);
  else if (b.step === "where") body = v_flex().gap(6)
    .child(div().text_size(12.5).text_color(c.navy).child("Where your other trained models are kept"))
    .child(div().text_size(11.5).line_height("16px").text_color(c.grey500).child("A folder on this computer, or a storage address that is not one of your model registries. Type it once: Captain keeps it on this computer, only reads there, and does not show it again."))
    .child(h_flex().gap(8).items_center().child(box(c, "wt-bring-where", v.whereInput, 320)).child(Button(c, "wt-bring-keep", "Keep this place and list it", (_e, cx) => keepTyped(v, cx), { small: true })))
    .child(h_flex().child(toRegistries));
  else if (b.step === "done") body = div().id("wt-bring-done").text_size(12.5).line_height("18px").text_color(c.navy).child(b.says);
  else if (b.step === "list") {
    const rows = b.rows ?? [];
    body = v_flex().gap(6)
      .child(h_flex().gap(10).items_center()
        .child(div().id("wt-bring-count").flex_1().min_w(0).text_size(12.5).text_color(c.navy).child(!b.rows ? "Listing what is kept there." : rows.length ? `${int(rows.length)} ${rows.length === 1 ? "model" : "models"} can be brought in from that place.` : b.problem ? "" : "Nothing that can be brought in was found there."))
        .child(quiet(c, "wt-bring-change", "Change that place", (_e, cx) => { v.whereInput.set_value(""); v.bring = { step: "where" }; cx.notify(); })))
      .when(!b.rows, (e) => e.child(Skeleton(c, 3)))
      .child(v_flex().children(rows.slice(0, SHOWN).map((r, n) => h_flex().id(`wt-bring-row-${n}`).min_h(ROW_H).px(8).gap(10).items_center().rounded(6).hover((s) => s.bg(c.surface2))
        .child(div().flex_none().max_w(220).truncate().text_size(12.5).font_medium().child(pathTail(r.path)))
        .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(r.description || "No description was saved with it."))
        .child(Button(c, `wt-bring-choose-${n}`, "Choose this one", (_e, cx) => choose(v, r, cx), { small: true })))))
      .when(rows.length > SHOWN, (e) => e.child(div().text_size(11.5).text_color(c.grey500).child(`and ${int(rows.length - SHOWN)} more, not listed here`)))
      .child(h_flex().child(toRegistries));
  } else {
    const working = b.step === "working";
    body = v_flex().gap(6)
      .child(div().id("wt-bring-picked").text_size(12.5).text_color(c.navy).child(`Bring in ${b.picked.name ?? pathTail(b.picked.path)}`))
      .child(div().id("wt-bring-copies").text_size(11.5).line_height("16px").text_color(c.grey500).child("This copies the model's files into this workspace. Nothing is written where they are kept."))
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(div().flex_none().text_size(12.5).text_color(c.grey500).child("Name it"))
        .child(box(c, "wt-bring-name", v.nameInput, 200))
        // The deliberate press: outlined, in the accent colour.
        .child(div().id("wt-bring-go").h(26).px(10).flex().flex_none().items_center().text_size(12.5).font_medium().border_1()
          .when(working, (e) => e.bg(c.surface2).text_color(c.grey500).border_color(c.rule))
          .when(!working, (e) => e.bg(c.surface).border_color(c.blue).text_color(c.blue).cursor_pointer().hover((x) => x.bg(c.blueLight)).on_click((_e, cx) => bringIn(v, cx)))
          .child(working ? "Bringing it in" : "Bring it in"))
        .when(!working, (e) => e.child(Button(c, "wt-bring-other", "Choose another", (_e, cx) => { v.bring = { ...b, step: b.back ?? "registries", picked: null, problem: null }; cx.notify(); }, { small: true }))));
  }
  return v_flex().id("wt-bring-panel").max_w(640).p(12).gap(10).border_1().border_color(c.rule).bg(c.surface)
    .child(h_flex().child(div().flex_1().text_size(12.5).font_semibold().child("Bring in a trained model")).child(close))
    .child(body)
    .when(Boolean(b.note), (e) => e.child(div().id("wt-bring-note").text_size(11.5).line_height("16px").text_color(c.grey500).child(b.note)))
    .when(Boolean(b.problem), (e) => e.child(div().id("wt-bring-problem").text_size(12.5).line_height("18px").text_color(c.fail).child(b.problem)));
}
