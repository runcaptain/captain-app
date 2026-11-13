// CAPTAIN: the list of projects, as quiet rows. One row per project, largely unboxed, with aligned columns: its
// icon, its name, its state in one plain sentence under the name, then a trailing cluster of fine outlined
// capsules (how many models, whether it is deployed, with a glyph), who made it, and when. Rows sit in groups
// ("Deployed", "In progress", "No model yet"), each under a lightly tinted strip that folds. Above the list a
// toolbar holds the saved views as pills: All, Deployed, Mine.
//
// Everything here is drawn from what the page already reads (main.js `loadSummaries`): nothing is read on a
// clock. Which view is picked and which groups are folded is the person's own view state, kept on this computer.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { icon } from "./kit/icons.js";
import { ProjectBadge } from "./kit/projecticon.js";
import { int } from "./kit/num.js";

export const VIEWS = [{ id: "all", label: "All" }, { id: "deployed", label: "Deployed" }, { id: "mine", label: "Mine" }];
export const GROUPS = [
  { id: "deployed", label: "Deployed", icon: "circle-check", tone: "good" },
  { id: "progress", label: "In progress", icon: "activity", tone: "blue" },
  { id: "empty", label: "No model yet", icon: "square", tone: "grey500" },
  { id: "reading", label: "Reading", icon: "clock", tone: "grey500" },
  { id: "failed", label: "Couldn't be read", icon: "triangle-alert", tone: "warn" },
];
const KEY = "projects-view";
const ROW_H = 44;

/** The person's view of the list, as it was left: {view, folded: [group ids]}. */
export function readListView() {
  try { const v = JSON.parse(localStorage.getItem(KEY) ?? "{}"); return { view: VIEWS.some((x) => x.id === v.view) ? v.view : "all", folded: Array.isArray(v.folded) ? v.folded.filter((g) => GROUPS.some((x) => x.id === g)) : [] }; }
  catch { return { view: "all", folded: [] }; }
}
export function keepListView(lv) { try { localStorage.setItem(KEY, JSON.stringify({ ...lv })); } catch { /* it lasts while the page is open */ } }

/** How long ago, in a few letters: "today", "yesterday", "5 days ago", "3 months ago". */
export function ago(iso, now = Date.now()) {
  const t = Date.parse(String(iso ?? ""));
  if (!Number.isFinite(t)) return "";
  const days = Math.floor((now - t) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${int(days)} days ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${int(months)} ${months === 1 ? "month" : "months"} ago`;
  const years = Math.floor(days / 365);
  return `${int(years)} ${years === 1 ? "year" : "years"} ago`;
}

/** What a row says about one project, from what the page has read: {ready, failed, models, state, up, deployed,
 *  what, owner, when, group}. `v` is the page. The state is the sentence the project's own page uses. */
export function projectFacts(v, p) {
  const sum = v.summaries?.[p.id], ready = Boolean(sum) && !sum.failed;
  const told = ready ? v.rowState(sum) : null;
  const dep = ready ? v.rowDeployed(p, sum) : { up: false, text: "Not deployed" };
  const models = ready ? (sum.exps.experiments ?? []).length : 0;
  return { ready, failed: Boolean(sum?.failed), models, state: told, up: dep.up, deployed: dep.text,
    what: p.description || (ready ? sum.stages.recipe?.label : null) || "", owner: p.created_by ?? "", when: ago(p.created_at),
    group: !sum ? "reading" : sum.failed ? "failed" : dep.up ? "deployed" : models > 0 ? "progress" : "empty" };
}

/** The rows of a saved view, in groups, in the groups' own order: [{...group, rows: [{p, i, f}]}]. Empty groups
 *  are left out. */
export function groupsOf(v, view = "all") {
  const rows = (v.projects ?? []).map((p, i) => ({ p, i, f: projectFacts(v, p) }))
    .filter((r) => (view === "deployed" ? r.f.up : view === "mine" ? Boolean(v.me) && r.p.created_by === v.me : true));
  return GROUPS.map((g) => ({ ...g, rows: rows.filter((r) => r.f.group === g.id) })).filter((g) => g.rows.length);
}

/** A fine outlined capsule: a count or a state, with a glyph when it is a state. */
function capsule(c, id, text, glyph = null) {
  return h_flex().id(id).h(20).px(8).gap(5).flex_none().items_center().rounded_full().border_1().border_color(c.rule).text_size(12).text_color(c.grey900)
    .when(Boolean(glyph), (e) => e.child(glyph)).child(div().child(text));
}
/** A state as a shape and a colour: a filled dot for running, a ring for not. Never colour alone. */
const dot = (c, on) => div().size(8).flex_none().rounded_full().when(on, (e) => e.bg(c.good)).when(!on, (e) => e.border_1().border_color(c.grey500));

/** One project's row. The whole row opens it, and so does its quiet "Open". */
function row(c, v, { p, i, f }, wide) {
  const open = (_e, cx) => v.pickProject(cx, p);
  return h_flex().id(`ph-p-${i}`).min_h(ROW_H).px(12).py(6).gap(12).items_center().cursor_pointer().rounded(6)
    .hover((s) => s.bg(c.hover)).on_click(open)
    .child(ProjectBadge(c, p, 24))
    .child(v_flex().flex_1().min_w(0).gap(1)
      .child(h_flex().gap(8).items_center()
        .child(div().id(`ph-p-${i}-name`).flex_none().max_w(280).truncate().text_size(13).font_medium().text_color(c.navy).child(p.name))
        .when(Boolean(f.what), (e) => e.child(div().id(`ph-p-${i}-what`).min_w(0).truncate().text_size(12).text_color(c.grey500).child(f.what))))
      // Its state in one plain sentence. It wraps: it is never cut off.
      .child(div().id(`ph-p-${i}-state`).text_size(12).line_height("17px").text_color(c.grey500)
        .child(!f.ready ? (f.failed ? "Couldn't read this project. Open it to try again." : "Reading...") : f.state)))
    .child(h_flex().flex_none().gap(8).items_center()
      .when(f.ready, (e) => e
        .child(capsule(c, `ph-p-${i}-models`, `${int(f.models)} model${f.models === 1 ? "" : "s"}`))
        .child(capsule(c, `ph-p-${i}-deployed`, f.deployed, dot(c, f.up))))
      // Who made it and when: the first things to go when the page is narrow.
      .when(wide && Boolean(f.owner), (e) => e.child(div().id(`ph-p-${i}-owner`).w(72).flex_none().truncate().text_size(12).text_color(c.grey500).tooltip(`Made by ${f.owner}`).child(f.owner)))
      .when(wide && Boolean(f.when), (e) => e.child(div().id(`ph-p-${i}-when`).w(84).flex_none().text_right().text_size(12).text_color(c.grey500).tooltip(`Made ${f.when}`).child(f.when)))
      .child(div().id(`ph-p-${i}-open`).h(24).px(8).flex().flex_none().items_center().rounded(6).text_size(12).font_medium().text_color(c.blue)
        .hover((s) => s.bg(c.blueLight)).on_click((e, cx) => { cx.stop_propagation?.(); open(e, cx); }).child("Open")));
}

/** A group's strip: a chevron, its glyph, its label and its count. A click folds or unfolds it. */
function strip(c, v, g, folded) {
  return h_flex().id(`ph-group-${g.id}`).h(32).px(12).gap(8).items_center().rounded(6).bg(c.tint).cursor_pointer()
    .tooltip(folded ? `Show the ${int(g.rows.length)} in ${g.label}` : `Fold ${g.label}`)
    .on_click((_e, cx) => v.foldGroup(g.id, cx))
    .child(svg(icon(folded ? "chevron-right" : "chevron-down")).size(14).flex_none().text_color(c.grey500))
    .child(svg(icon(g.icon)).size(14).flex_none().text_color(c[g.tone] ?? c.grey500))
    .child(div().text_size(13).font_medium().text_color(c.navy).child(g.label))
    .child(div().id(`ph-group-${g.id}-count`).text_size(12).text_color(c.grey500).child(int(g.rows.length)));
}

/** A small centred state: a line icon, a short title, one muted sentence, and at most one action. Loading,
 *  nothing here and nothing in this view all read the same way. */
export function Quiet(c, id, ic, title, body, action = null) {
  return v_flex().id(id).w_full().py(56).px(24).gap(8).items_center()
    .child(svg(icon(ic)).size(24).text_color(c.grey500))
    .child(div().pt(4).text_size(14).font_semibold().text_color(c.navy).child(title))
    .when(Boolean(body), (e) => e.child(div().max_w(420).text_center().text_size(13).line_height("19px").text_color(c.grey500).child(body)))
    .when(Boolean(action), (e) => e.child(div().pt(8).child(action)));
}

/** The toolbar: the saved views as pills, and how many projects the view holds. */
function toolbar(c, v, shown) {
  const lv = v.listView;
  const pill = (x) => { const on = lv.view === x.id;
    return div().id(`ph-view-${x.id}`).h(26).px(12).flex().flex_none().items_center().rounded_full().cursor_pointer().text_size(13)
      .border_1().border_color(on ? c.grey300 : c.rule).bg(on ? c.surface : c.canvas).text_color(on ? c.navy : c.grey500).when(on, (e) => e.font_medium())
      .hover((s) => s.text_color(c.navy)).tooltip(x.id === "all" ? "Every project" : x.id === "deployed" ? "Projects with a model running somewhere" : "Projects you made")
      .on_click((_e, cx) => v.pickListView(x.id, cx)).child(x.label); };
  return h_flex().id("ph-toolbar").h(44).flex_none().gap(6).items_center()
    .children(VIEWS.map(pill))
    .child(div().flex_1())
    .child(div().id("ph-count").flex_none().text_size(12).text_color(c.grey500).child(`${int(shown)} ${shown === 1 ? "project" : "projects"}`));
}

/** The list. `v` is the page: {projects, summaries, listView, me, width} and pickProject, foldGroup,
 *  pickListView. `none` is what to show when there is no project at all. */
export function ProjectsList(c, v, none) {
  const ps = v.projects;
  if (ps === null) return Quiet(c, "ph-reading", "clock", "Reading your projects", "They show here as soon as Captain's services answer.");
  if (!ps.length) return none;
  const groups = groupsOf(v, v.listView.view), shown = groups.reduce((n, g) => n + g.rows.length, 0);
  const wide = (v.width ?? 900) >= 760;
  const view = v.listView.view;
  return v_flex().id("ph-list").gap(4)
    .child(toolbar(c, v, shown))
    .when(!shown, (e) => e.child(view === "deployed" ? Quiet(c, "ph-none", "rocket", "Nothing is deployed yet", "A project shows here once its model is running somewhere.")
      : Quiet(c, "ph-none", "user", "No project of yours yet", v.me ? "A project shows here when you are the one who made it." : "Captain could not tell who you are, so it cannot say which projects are yours.")))
    .child(v_flex().id("ph-projects").gap(2).children(groups.flatMap((g) => {
      const folded = v.listView.folded.includes(g.id);
      return [strip(c, v, g, folded), ...(folded ? [] : g.rows.map((r) => row(c, v, r, wide))), div().h(6)];
    })));
}
