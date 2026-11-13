// captain-ui: a living evaluation set's health (plan 4, backend/docs/api.md "Living evaluation sets"), as a tab any
// screen can host: the dataset page in Data shows it for a queries or judgments set.
//   const h = new EvalHealth();  h.open(cx, dataset)  (dataset: {name, kind | classes})  h.render(c)
// It picks the partner set (queries <-> judgments), the logs and the catalog, guessed from their kinds, asks POST
// /api/evals/health, and shows the status, the findings in words, traffic coverage by head, torso and tail with the
// top unseen queries, staleness (retired, changed and with which fields, relevant now out of stock), coverage per run
// arm and reliability (or its note). "Refresh as a new version" asks POST /api/evals/refresh with dry_run for the
// changelog, then publishes (a job), says so, and offers "Rate the new and changed pairs" (Start rating).
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { notify, openOverlay } from "herdr";
import { api } from "./cst.js";
import { Badge, Button, Skeleton, plainError } from "./ui.js";
import { icon } from "./icons.js";
import { MONO } from "./tokens.js";
import { int } from "./num.js";
import { jobDone } from "./live.js";

const pct = (x) => (x == null ? "" : `${Math.round(x * 100)}%`);
// A dataset's role. The record's own `kind` is "dataset"; the role is in `classes` (the list's `kind` carries it too).
const ROLES = ["queries", "judgments", "events", "catalog"];
const kindOf = (d) => (d?.classes ?? []).find((k) => ROLES.includes(k)) ?? (ROLES.includes(d?.kind) ? d.kind : null);

/** Wait for a job: {ok, result} or {ok: false, error}. */
async function finished(cx, job) {
  const r = await jobDone(cx, job, { minutes: 6 });
  if (r.ok) return { ok: true, result: r.result };
  if (r.failed) return { ok: false, error: r.message || "It didn't finish." };
  return { ok: false, error: "This took too long." };
}

export class EvalHealth {
  constructor() { this.reset(); }

  reset() {
    this.d = null; this.list = null; this.pick = {}; this.health = null; this.busy = null; this.error = null;
    this.plan = null; this.published = null; this.open_ = {};
  }

  /** Whether a dataset has a health tab: a queries or a judgments set. */
  static applies(d) { return ["queries", "judgments"].includes(kindOf(d)); }

  open(cx, d) {
    if (this.d?.name === d?.name && this.list) return;
    this.reset();
    this.d = d;
    cx.spawn(async (cx) => {
      const r = await api("/api/datasets");
      this.list = r.ok ? r.data ?? [] : [];
      this.guess();
      if (this.pick.queries && this.pick.judgments) await this.check(cx);
      cx.notify();
    });
  }

  of(kind) { return (this.list ?? []).filter((x) => kindOf(x) === kind); }

  // The partner set by name first (demo-queries <-> demo-judgments), then any of that kind; the first logs and catalog.
  guess() {
    const me = this.d.name, kind = kindOf(this.d);
    const other = kind === "queries" ? "judgments" : "queries";
    const twin = me.replace(kind, other);
    const partner = this.of(other).find((x) => x.name === twin)?.name ?? this.of(other)[0]?.name ?? null;
    this.pick = { [kind]: me, [other]: partner, logs: this.of("events")[0]?.name ?? null, catalog: this.of("catalog")[0]?.name ?? null };
  }

  body() {
    const p = this.pick;
    return { queries: p.queries, judgments: p.judgments, ...(p.logs ? { logs: p.logs } : {}), ...(p.catalog ? { catalog: p.catalog } : {}) };
  }

  async check(cx) {
    this.busy = "check"; this.error = null; cx.notify();
    const r = await api("/api/evals/health", { body: this.body(), timeout: 120 });
    this.busy = null;
    if (r.ok) this.health = r.data; else this.error = plainError(r.error, "Captain couldn't check this set.");
    cx.notify();
  }

  refresh(cx, publish) {
    if (!this.pick.logs) { this.error = "Refreshing samples new queries from logs: pick the logs first."; cx.notify(); return; }
    this.busy = publish ? "publish" : "plan"; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/evals/refresh", { body: { ...this.body(), ...(publish ? {} : { dry_run: true }) } });
      const out = r.ok && r.data?.job ? await finished(cx, r.data.job) : { ok: false, error: r.error };
      this.busy = null;
      if (!out.ok) { this.error = plainError(out.error, "Captain couldn't refresh this set."); cx.notify(); return; }
      if (!publish) { this.plan = out.result; cx.notify(); return; }
      this.published = out.result;
      this.plan = null;
      notify({ title: "New versions published", body: `${out.result.queries ?? "queries"} and ${out.result.judgments ?? "judgments"}. The old versions are unchanged.`, level: "info" });
      await this.check(cx);
    });
  }

  // ---- drawing --------------------------------------------------------------------------------------------

  chooser(c, label, kind, key, optional) {
    const opts = this.of(kind);
    return v_flex().gap(4).min_w(0)
      .child(h_flex().gap(6).child(div().text_size(11.5).font_semibold().text_color(c.grey900).child(label))
        .when(Boolean(optional), (e) => e.child(div().text_size(11).text_color(c.grey500).child("optional"))))
      .child(h_flex().flex_wrap().gap(4)
        .when(Boolean(optional), (e) => e.child(this.pill(c, `eh-${key}-none`, "None", this.pick[key] == null, (cx) => { this.pick[key] = null; this.health = null; cx.spawn(async (cx) => this.check(cx)); })))
        .children(opts.map((x) => this.pill(c, `eh-${key}-${x.name}`, x.name, this.pick[key] === x.name, (cx) => { this.pick[key] = x.name; this.health = null; cx.spawn(async (cx) => this.check(cx)); })))
        .when(!opts.length, (e) => e.child(div().text_size(12).text_color(c.grey500).child(`No ${kind} datasets yet.`))));
  }

  pill(c, id, label, on, pick) {
    return div().id(id).h(24).px(8).flex().items_center().cursor_pointer().text_size(12).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
      .on_click((_e, cx) => { pick(cx); cx.notify(); }).child(label);
  }

  section(c, title, children) {
    return v_flex().w_full().gap(8).p(14).border_1().border_color(c.rule)
      .child(div().text_size(13).font_semibold().text_color(c.navy).child(title)).children(children);
  }

  bar(c, label, share) {
    return h_flex().gap(8).items_center()
      .child(div().w(52).flex_none().text_size(12).text_color(c.grey900).child(label))
      .child(div().flex_1().h(10).bg(c.surface2).child(div().h_full().w(Math.max(0, Math.round((share ?? 0) * 260))).bg(share >= 0.5 ? c.good : share >= 0.2 ? c.warn : c.fail)))
      .child(div().w(40).flex_none().text_size(12).font_family(MONO).text_color(c.navy).child(pct(share)));
  }

  // A count that opens to its list.
  expandable(c, key, title, items, line) {
    const open = Boolean(this.open_[key]);
    return v_flex().gap(4)
      .child(h_flex().id(`eh-x-${key}`).gap(6).items_center().cursor_pointer().on_click((_e, cx) => { this.open_[key] = !open; cx.notify(); })
        .child(svg(icon(open ? "chevron-down" : "chevron-right")).size(11).text_color(c.grey500))
        .child(div().text_size(12.5).text_color(items.length ? c.navy : c.grey500).child(`${items.length} ${title}`)))
      .when(open && items.length > 0, (e) => e.child(v_flex().pl(18).gap(2).children(items.slice(0, 50).map((x, i) => div().id(`eh-${key}-${i}`).text_size(12).font_family(MONO).text_color(c.grey900).child(line(x))))));
  }

  report(c) {
    const h = this.health;
    const rep = h.representativeness ?? {}, st = h.staleness ?? {}, rel = h.reliability ?? {};
    const tone = h.status === "healthy" ? "good" : h.status === "needs refresh" ? "warn" : "bad";
    // w_full and min_w(0) on the text: in a scroll view a row is as wide as its text unless told, so it ran off.
    return v_flex().w_full().gap(12)
      .child(h_flex().gap(10).items_center()
        .child(Badge(c, h.status ?? "checked", tone))
        .child(div().flex_1().min_w(0).text_size(12.5).text_color(c.grey900).child(`${h.queries ?? ""} and ${h.judgments ?? ""}: ${int(h.n_queries ?? 0)} queries, ${int(h.n_grades ?? 0)} grades`)))
      .child(v_flex().gap(4).children((h.findings ?? []).map((f, i) => h_flex().id(`eh-f-${i}`).gap(8).items_start()
        .child(div().size(6).mt(6).flex_none().bg(tone === "good" ? c.good : c.warn))
        .child(div().flex_1().min_w(0).text_size(13).line_height("19px").text_color(c.navy).child(f)))))
      .child(this.section(c, "Does it look like your traffic?", [
        rep.traffic_covered != null ? div().text_size(12.5).text_color(c.grey900).child(`The set's queries cover ${pct(rep.traffic_covered)} of recent searches.`) : div(),
        this.bar(c, "Head", rep.by_band?.head), this.bar(c, "Torso", rep.by_band?.torso), this.bar(c, "Tail", rep.by_band?.tail),
        (rep.top_unseen ?? []).length ? v_flex().gap(2).pt(4)
          .child(div().text_size(11.5).font_semibold().text_color(c.grey500).child("SEARCHED OFTEN, NOT IN THE SET"))
          .children(rep.top_unseen.slice(0, 8).map((u, i) => h_flex().id(`eh-u-${i}`).gap(8)
            .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.navy).child(u.query))
            .child(div().text_size(12).font_family(MONO).text_color(c.grey500).child(int(u.searches ?? 0))))) : div(),
      ]))
      .child(this.section(c, "Are the grades still true?", [
        this.expandable(c, "retired", "grades on products that are gone", st.retired ?? [], (x) => `${x.query_id} ${x.product_id} (grade ${x.grade})`),
        this.expandable(c, "changed", "grades on products changed since rating", st.changed ?? [], (x) => `${x.query_id} ${x.product_id}: ${[].concat(x.fields ?? x.changed ?? []).join(", ")}`),
        this.expandable(c, "oos", "relevant products now out of stock", st.out_of_stock_relevant ?? [], (x) => `${x.query_id} ${x.product_id} (grade ${x.grade})`),
      ]))
      .when((h.coverage ?? []).length > 0, (e) => e.child(this.section(c, "How much of each run is graded?",
        h.coverage.map((r, i) => h_flex().id(`eh-cov-${i}`).gap(8).items_center()
          .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.navy).child(`${r.arm}, in ${r.run}`))
          .child(Badge(c, `${pct(r.judged_at_10)} of top 10 graded`, r.judged_at_10 >= 0.9 ? "good" : "warn"))))))
      .child(this.section(c, "Can the grades be trusted?", [
        rel.exact_agreement != null
          ? div().text_size(12.5).text_color(c.navy).child(`Raters agreed exactly on ${pct(rel.exact_agreement)} of ${rel.pairs_graded_twice} pairs rated twice.`)
          : div().text_size(12.5).text_color(c.grey900).child(rel.note ?? "Agreement can't be measured yet."),
      ]));
  }

  refreshPanel(c) {
    const plan = this.plan, done = this.published;
    return v_flex().gap(8).p(14).border_1().border_color(plan ? c.blue : c.rule).bg(plan ? c.blueLight : c.surface)
      .child(h_flex().gap(10).items_center()
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().text_size(13).font_semibold().text_color(c.navy).child("Refresh as a new version"))
          .child(div().min_w(0).text_size(12).text_color(c.grey900).child("Adds queries from recent traffic and drops grades on retired products, as new versions. The sealed test set and the old versions stay as they are.")))
        .when(!plan, (e) => e.child(Button(c, "eh-plan", this.busy === "plan" ? "Planning..." : "See what would change", (_e, cx) => this.refresh(cx, false), { small: true, disabled: Boolean(this.busy) }))))
      .when(Boolean(plan), (e) => e
        .child(v_flex().gap(2).children((plan.changelog ?? []).map((l, i) => div().id(`eh-cl-${i}`).text_size(12.5).text_color(c.navy).child(l))))
        .child(h_flex().gap(8)
          .child(Button(c, "eh-publish", this.busy === "publish" ? "Publishing..." : "Publish new versions", (_e, cx) => this.refresh(cx, true), { kind: "primary", small: true, disabled: Boolean(this.busy) }))
          .child(Button(c, "eh-cancel", "Not now", (_e, cx) => { this.plan = null; cx.notify(); }, { kind: "quiet", small: true }))))
      .when(Boolean(done), (e) => e.child(h_flex().gap(10).items_center()
        .child(div().flex_1().text_size(12.5).text_color(c.good).child(`Published ${done.queries} and ${done.judgments}. ${(done.regrade ?? []).length + (done.added_queries ?? []).length} pairs and queries to grade.`))
        .child(Button(c, "eh-grade", "Rate the new and changed pairs", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "grading", values: { queries: String(done.queries ?? "").split("@")[0] } } }), { kind: "primary", small: true }))));
  }

  render(c) {
    if (!this.list) return v_flex().p(20).child(Skeleton(c, 5));
    const kind = kindOf(this.d);
    const other = kind === "queries" ? "judgments" : "queries";
    return v_flex().id("eval-health").w_full().gap(14).px(20).py(14)
      .child(h_flex().gap(16).items_start().flex_wrap()
        .child(this.chooser(c, other === "judgments" ? "Its grades" : "Its queries", other, other))
        .child(this.chooser(c, "Recent searches (logs)", "events", "logs", true))
        .child(this.chooser(c, "Catalog", "catalog", "catalog", true)))
      .when(Boolean(this.error), (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.error)))
      .when(!this.pick[other], (e) => e.child(div().text_size(12.5).text_color(c.grey500).child(`Pick its ${other} to check its health.`)))
      .when(this.busy === "check" && !this.health, (e) => e.child(Skeleton(c, 5)))
      .when(Boolean(this.health), (e) => e.child(this.report(c)).child(this.refreshPanel(c)));
  }
}
