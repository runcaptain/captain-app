// CAPTAIN: Data and signals, a dataset's Insights tab (Unity Catalog's Insights, for Captain's own records), from
// GET /api/datasets/{name}/insights:
// - one line on how much uses it, then what does: pipelines built on it, runs scored with it, environments it reached
//   and datasets made from it, each with how far downstream it is and who made it when;
// - saved notebooks that name it;
// - who works with it (made its versions, built on it);
// - recent activity, newest first.
// A row opens what it names: a run in Comparisons, a dataset or a notebook here.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { setShared, run } from "herdr";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Skeleton } from "./kit/ui.js";
import { int } from "./kit/num.js";

const enc = (name) => String(name).split("/").map(encodeURIComponent).join("/");
const KINDS = [["pipeline", "Pipelines built on it", "pipeline", "pipelines"], ["run", "Runs scored with it", "run", "runs"],
  ["environment", "Environments it reached", "environment", "environments"], ["dataset", "Datasets made from it", "dataset", "datasets"]];
const plural = (n, one, many) => `${int(n)} ${n === 1 ? one : many}`;
function ago(iso) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "";
  const s = (Date.now() - t) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} minutes ago` : s < 129600 ? `${Math.round(s / 3600)} hours ago` : `${Math.round(s / 86400)} days ago`;
}
const away = (level) => (level <= 1 ? "uses it directly" : `${level} steps downstream`);

export class DatasetInsights {
  constructor(host) {
    this.host = host; // Data's main view
    this.data = null; // the insights | {error}
    this.for = null;
  }

  reset() {
    this.data = null;
    this.for = null;
  }

  load(cx, d) {
    if (!d || d.error || this.for === d.name) return;
    this.for = d.name;
    this.data = null;
    cx.spawn(async (cx) => {
      const r = await api(`/api/datasets/${enc(d.name)}/insights`, { timeout: 60 });
      if (this.for !== d.name) return;
      const e = String(r.data?.error ?? r.data?.detail ?? r.error ?? "").trim();
      this.data = r.ok ? r.data : { error: r.status === 404 && /not found/i.test(e) ? "This backend can't read a dataset's insights yet." : e ? `${e[0].toUpperCase()}${e.slice(1)}${/[.!?]$/.test(e) ? "" : "."}` : "Couldn't read what uses this dataset." };
      cx.notify();
    });
  }

  /** Open what a row names, where it lives. */
  go(cx, e) {
    if (e.kind === "dataset") { this.host.tab = "datasets"; this.host.select(cx, e.name); return; }
    if (e.kind === "run") {
      setShared("captain.open", { section: "comparisons", backtest: e.name, version: e.version, at: Date.now() });
      run("layout.select", { stack: "sections", child: "comparisons" });
    }
  }

  openNotebook(cx, name) {
    const nb = this.host.nb;
    this.host.tab = "notebooks";
    if (nb.list === null) { nb.want = name; nb.load(cx); } else nb.openNotebook(cx, name);
    cx.notify();
  }

  render(c) {
    const d = this.data;
    if (!d) return v_flex().id("ds-insights").p(20).child(Skeleton(c, 6));
    if (d.error) return v_flex().id("ds-insights").p(20).child(div().p(12).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).text_color(c.fail).child(d.error));
    const n = d.counts ?? {};
    const parts = KINDS.filter(([k]) => n[k]).map(([k, , one, many]) => plural(n[k], one, many));
    const nothing = !parts.length && !n.notebooks;
    const section = (title, body) => v_flex().gap(4).child(div().text_size(12.5).font_medium().text_color(c.navy).child(title)).child(body);
    // Rows wrap in a narrow pane: the name first, the details under it when there's no room beside it.
    const row = (id, cells, onClick) => h_flex().id(id).min_h(28).py(4).px(8).gap_x(10).gap_y(2).flex_wrap().items_center().border_b(1).border_color(c.rule).text_size(12)
      .when(!!onClick, (e) => e.cursor_pointer().hover((s) => s.bg(c.surface2)).on_click(onClick)).children(cells);
    return v_flex().id("ds-insights").flex_1().min_h(0).overflow_y_scrollbar().px(20).py(12).gap(16)
      .child(div().text_size(13.5).text_color(c.navy).child(nothing
        ? "Nothing uses this dataset yet. Pipelines built on it, runs scored with it and notebooks that name it will show here."
        : `Used by ${parts.length ? parts.join(", ") : "nothing yet"}. Named in ${plural(n.notebooks ?? 0, "notebook", "notebooks")}.`))
      .children(KINDS.filter(([k]) => (d.used_by?.[k] ?? []).length).map(([k, title]) => section(title, v_flex().border_1().border_color(c.rule)
        .children(d.used_by[k].map((e, i) => row(`ins-${k}-${i}`, [
          div().min_w(140).max_w(320).flex_1().truncate().font_family(MONO).text_color(e.kind === "run" || e.kind === "dataset" ? c.blue : c.navy).child(e.label),
          div().flex_none().text_color(c.grey900).child(away(e.level)),
          div().min_w(120).flex_1().truncate().text_color(c.grey500).child([e.created_by, ago(e.created_at)].filter(Boolean).join(", ")),
          div().flex_none().text_color(c.grey500).child(e.via?.length > 1 ? `from versions ${e.via.join(", ")}` : e.via?.length ? `from version ${e.via[0]}` : ""),
        ], e.kind === "run" || e.kind === "dataset" ? (_ev, cx) => this.go(cx, e) : null))))))
      .when((d.notebooks ?? []).length > 0, (e) => e.child(section("Notebooks that name it", v_flex().border_1().border_color(c.rule)
        .children(d.notebooks.map((nb, i) => row(`ins-nb-${i}`, [
          div().min_w(140).max_w(320).flex_1().truncate().font_family(MONO).text_color(c.blue).child(nb.name),
          div().flex_1().text_color(c.grey900).child(`named in ${plural(nb.cells, "cell", "cells")}`),
        ], (_ev, cx) => this.openNotebook(cx, nb.name)))))))
      .when((d.people ?? []).length > 0, (e) => e.child(section("Who works with it", v_flex().border_1().border_color(c.rule)
        .children(d.people.map((p, i) => row(`ins-person-${i}`, [
          div().min_w(140).max_w(320).flex_1().truncate().text_color(c.navy).child(p.user),
          div().flex_1().text_color(c.grey900).child([p.versions ? `made ${plural(p.versions, "version", "versions")}` : null, p.builds ? `built on it ${plural(p.builds, "time", "times")}` : null].filter(Boolean).join(", ")),
        ]))))))
      .when((d.activity ?? []).length > 0, (e) => e.child(section("Recent activity", v_flex().border_1().border_color(c.rule)
        .children(d.activity.map((a, i) => row(`ins-act-${i}`, [
          div().w(120).flex_none().text_color(c.grey500).child(ago(a.at)),
          div().min_w(120).flex_1().truncate().text_color(c.grey900).child(a.who ?? ""),
          div().flex_1().min_w(0).truncate().text_color(c.navy).child(a.what),
        ]))))))
      .child(div().text_size(11.5).text_color(c.grey500).child(`From Captain's own records: lineage up to three steps downstream, the last ${plural(Math.min(d.versions_total ?? 0, 10), "version", "versions")} of ${plural(d.versions_total ?? 0, "version", "versions")}, and saved notebooks.`));
  }
}
