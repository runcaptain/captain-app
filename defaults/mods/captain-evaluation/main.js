// CAPTAIN: New evaluation (Edgar, 2026-11-12: "I don't think that a customer who's working could run one without
// data like QA pairs and such... They may have to put it together themselves", and "improve how that whole
// experience feels and how much customizability you have"). A person sets an offline evaluation up themselves:
//
//   1. Models: the one (or several) to test, and the one to compare with.
//   2. What to test on: their own questions and the products they want back (a table in Data or a file on this
//      computer, with a column mapper and a preview of what it makes), logged searches, or a saved test set.
//   3. The measure that decides the verdict, picked from the list with one line each. A run computes every
//      measure it has the data for.
//   4. The limits, as fields with their defaults: nothing is capped out of sight.
//   5. Where it runs (kit cost.js, which opens on the workspace's place from the compute picker).
//
// The setup is saved under its name as a test: the same name again is the same test on another model, and its
// runs line up in Runs, Compare. Opened with props {values: {test: "<name>"}} it starts from that saved test, so
// "run it again on the new model" is picking the model and one click.
//
// Everything offered is something a run does (GET /api/evaluations/options reads the runner's own tables). Depth
// and slices are fixed by the runner, so they are said, not offered: each measure's line says how deep it looks,
// and the form says which slices every run reports.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, TextareaState, Textarea } from "gpui-base";
import { theme, props, run, setShared, closeOverlay, openOverlay, home } from "herdr";
import { api } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int } from "./kit/num.js";
import { publishView, watchView } from "./kit/view.js";
import { costState, loadPlaces, refreshEstimate, needsConfirm, costSlice, RunsOn, LOCAL } from "./kit/cost.js";

const enc = (name) => String(name).split("/").map(encodeURIComponent).join("/");
const bare = (ref) => String(ref ?? "").replace(/@v\d+$/, "");
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());
const sentence = (t) => { const s = String(t ?? "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
/** What to test on. */
export const ON = [
  ["own", "My own questions and answers", "A table of questions and the products you want back. Captain turns it into a test set."],
  ["logs", "Logged searches", "Real searches and what shoppers clicked. Grades come from the clicks."],
  ["saved", "A saved test set", "A set of questions and a set of answers already in Data."],
];
/** The columns the mapper asks for: [role, label, required, what it is]. */
export const ROLES = [
  ["query", "Question", true, "What a person searches for."],
  ["product", "Wanted product", true, "The id of a product that is a good answer."],
  ["grade", "Grade", false, "How good an answer it is, 0 to 3. Leave it out and every row counts as the grade below."],
  ["slice", "Group", false, "A group for each question. Results are also reported per group."],
];

/** What is still missing before the evaluation can start, as sentences, in the order of the form. */
export function missing(v, state) {
  const out = [];
  if (!v.name.trim()) out.push("Give the test a name.");
  else if (!/^[a-z0-9][a-z0-9._/-]*$/i.test(v.name.trim())) out.push("The name takes letters, digits, dashes and slashes.");
  if (!v.models.length) out.push("Pick the model to test.");
  if (!v.against) out.push("Pick the model to compare it with.");
  if (v.against && v.models.includes(v.against)) out.push("A model can't be compared with itself.");
  if (v.on === "own") {
    if (v.src === "data" && !v.dataset) out.push("Pick the table that holds your questions.");
    if (v.src === "file" && !v.file.trim()) out.push("Give the file that holds your questions.");
    if (!v.mapping.query || !v.mapping.product) out.push("Say which column is the question and which is the wanted product.");
    else if (state.preview && !state.preview.ready) out.push(state.preview.problem ?? "No row has both a question and a product.");
  }
  if (v.on === "logs" && !v.logs) out.push("Pick the logged searches.");
  if (v.on === "saved" && (!v.queries || !v.judgments)) out.push("Pick the questions and the answers.");
  const m = (state.opts?.measures ?? []).find((x) => x.id === v.measure);
  if (m && !measureOk(m, v.on)) out.push(`${m.label} can't decide this evaluation: ${measureWhy(m)}`);
  if (!(Number(v.mde) >= 0)) out.push("The smallest gain worth acting on is a number, 0 or more.");
  if (v.on === "logs" && !(Number(v.population_size) >= 1)) out.push("Simulated shoppers is a whole number, 1 or more.");
  return out;
}
/** Whether a measure can be the deciding one for this kind of test data, and why not. */
export function measureOk(m, on) { return m.needs === "answers" || on === "logs"; }
export function measureWhy(m) {
  return m.needs === "shoppers" ? "it comes from simulated shoppers, which only a test on logged searches has." : "it needs how often each question was searched, which only logged searches have.";
}

export default class NewEvaluation extends View {
  init(_props, cx) {
    const given = props()?.values ?? {};
    this.v = { name: given.name ?? "", models: given.b ? [given.b] : [], against: given.a ?? null, on: "own", src: "data", dataset: null, file: "",
      mapping: {}, default_grade: 3, logs: null, queries: given.queries ?? null, judgments: given.judgments ?? null,
      measure: "ndcg@10", mde: "0.003", population_size: "4000", expect: "" };
    if (given.queries || given.judgments) this.v.on = "saved";
    this.opts = null; this.pipelines = null; this.datasets = null; this.roles = new Map(); this.tests = null;
    this.preview = null; this.previewFor = null; this.previewing = false;
    this.error = null; this.busy = null; this.tried = false;
    this.inputs = {
      name: InputState.new({ placeholder: "tuned-vs-keyword" }), file: InputState.new({ placeholder: "~/Downloads/my-questions.csv" }),
      mde: InputState.new({ placeholder: "0.003" }), population_size: InputState.new({ placeholder: "4000" }),
      expect: TextareaState.new({ placeholder: "The tuned model finds more of the products we want for long questions", rows: 2 }),
    };
    for (const [k, st] of Object.entries(this.inputs)) {
      if (this.v[k]) st.set_value(String(this.v[k]));
      st.on?.("change", (_e, cx) => { this.v[k] = st.value(); if (k === "file") this.previewSoon(); cx.notify(); });
    }
    this.inputs.file.on?.("submit", (_e, cx) => this.loadPreview(cx));
    // Where it runs and what that costs there (kit cost.js): it opens on the workspace's place.
    this.cost = costState(given.compute || LOCAL);
    if (given.compute) this.cost.picked = true;
    this.load(cx, given.test ?? null);
    cx.timer.every(500, (cx) => {
      if (this.previewDue && Date.now() > this.previewDue) { this.previewDue = null; this.loadPreview(cx); }
      cx.spawn(async (cx) => { if (await refreshEstimate(this.cost, this.costWant())) cx.notify(); });
    });
    // The form open over the page and what is filled in, for agents (cst app context).
    watchView(cx, "dialog", () => ({ mod: "captain-evaluation", form: "backtest", title: "New evaluation",
      fields: { name: this.v.name, models: this.v.models, compared_with: this.v.against, test_on: this.v.on, measure: this.v.measure, smallest_gain: this.v.mde },
      runs_on: costSlice(this.cost) }));
  }

  close() { publishView("dialog", null); closeOverlay(props()?.overlay); }

  load(cx, test) {
    cx.spawn(async (cx) => {
      const [o, p, d, cl, t] = await Promise.all([api("/api/evaluations/options"), api("/api/pipelines"), api("/api/datasets"), api("/api/dataset-classes"), api("/api/backtests"), loadPlaces(this.cost)]);
      if ([o, p, d].some((r) => r.starting)) { cx.timer.after(1000, (cx) => this.load(cx, test)); return; }
      this.opts = o.ok ? o.data : { measures: [], slices: [], limits: [] };
      this.pipelines = p.ok && Array.isArray(p.data) ? p.data.map((x) => ({ value: `${x.name}@v${x.latest}`, label: x.name, version: x.latest })) : [];
      this.datasets = d.ok && Array.isArray(d.data) ? d.data : [];
      this.roles = new Map((cl.ok ? cl.data?.classes ?? [] : []).map((k) => [k.name, k.role]));
      this.tests = t.ok && Array.isArray(t.data) ? t.data.filter((b) => b.mode !== "imported") : [];
      for (const l of this.opts.limits ?? []) if (this.v[l.id] === undefined || this.v[l.id] === "") this.set(l.id, String(l.default));
      if (!o.ok) this.error = "Couldn't read what an evaluation can measure. Close this and try again.";
      if (test) await this.startFrom(test);
      cx.notify();
    });
  }

  set(k, value) {
    this.v[k] = value;
    if (this.inputs[k] && this.inputs[k].value() !== String(value)) this.inputs[k].set_value(String(value));
  }

  /** Datasets whose class plays a role (queries, judgments, events), as their newest version. */
  byRole(role) {
    return (this.datasets ?? []).filter((d) => d.kind === role || (d.classes ?? []).some((k) => this.roles.get(k) === role))
      .map((d) => ({ value: `${d.name}@v${d.latest}`, label: d.name, version: d.latest, rows: d.rows }));
  }

  /** Start from a saved test: its models, what it tests on, its measure and its limits. Only the model changes. */
  async startFrom(name) {
    const r = await api(`/api/backtests/${enc(name)}`);
    if (!r.ok) { this.error = `Couldn't read the test ${name}.`; return; }
    const b = r.data;
    this.from = name;
    this.set("name", name);
    this.v.against = b.arms?.[0] ?? null;
    this.v.models = (b.arms ?? []).slice(1);
    if (b.mode === "historic") { this.v.on = "logs"; this.v.logs = b.logs ?? null; }
    else { this.v.on = "saved"; this.v.queries = b.queries ?? null; this.v.judgments = b.judgments ?? null; }
    this.v.measure = b.preregistration?.primary_metric ?? this.v.measure;
    this.set("mde", String(b.preregistration?.mde ?? this.v.mde));
    this.set("expect", b.preregistration?.hypothesis ?? "");
    if (b.compute) { this.cost.where = b.compute; this.cost.picked = true; }
  }

  // ---- the person's own table ---------------------------------------------------------------------------------

  source() { return this.v.src === "data" ? (this.v.dataset ? { dataset: this.v.dataset } : null) : (this.v.file.trim() ? { file: expand(this.v.file) } : null); }

  previewSoon() { this.previewDue = Date.now() + 600; }

  /** What the table holds and what the mapping makes of it. Writes nothing. */
  loadPreview(cx, keepMapping = false) {
    const src = this.source();
    if (!src) { this.preview = null; cx.notify(); return; }
    const key = JSON.stringify([src, keepMapping ? this.v.mapping : null, this.v.default_grade]);
    this.previewFor = key;
    this.previewing = true;
    cx.spawn(async (cx) => {
      const r = await api("/api/testsets/preview", { body: { source: src, ...(keepMapping ? { mapping: this.v.mapping } : {}), default_grade: this.v.default_grade, limit: 6 }, timeout: 60 });
      if (this.previewFor !== key) return;
      this.previewing = false;
      if (r.ok) { this.preview = r.data; this.v.mapping = { ...(r.data.mapping ?? {}) }; }
      else this.preview = { error: sentence(r.data?.error ?? r.data?.detail ?? r.error) || "Couldn't read that table." };
      cx.notify();
    });
    cx.notify();
  }

  mapColumn(cx, role, col) {
    const m = { ...this.v.mapping };
    if (m[role] === col) delete m[role]; else m[role] = col;
    this.v.mapping = m;
    this.loadPreview(cx, true);
  }

  // ---- cost, and starting it ----------------------------------------------------------------------------------

  costWant() {
    const rows = (ref) => (this.datasets ?? []).find((d) => `${d.name}@v${d.latest}` === ref)?.rows;
    const queries = this.v.on === "own" ? this.preview?.questions : this.v.on === "saved" ? rows(this.v.queries) : undefined;
    return { kind: "evaluation", size: { ...(queries ? { queries } : {}), arms: 1 + Math.max(1, this.v.models.length), mode: this.v.on === "logs" ? "historic" : "simulated" }, where: this.cost.where };
  }

  /** Save the test, and run it unless `runIt` is false. */
  submit(cx, runIt = true) {
    this.tried = true;
    const miss = missing(this.v, { preview: this.preview, opts: this.opts });
    if (miss.length || this.busy) { cx.notify(); return; }
    if (runIt && needsConfirm(this.cost)) { this.cost.confirming = true; cx.notify(); return; }
    const v = this.v, name = v.name.trim();
    this.busy = runIt ? "Starting" : "Saving";
    this.error = null;
    cx.spawn(async (cx) => {
      const fail = (r, fallback) => { this.busy = null; this.error = sentence(r.data?.error ?? r.data?.detail ?? (r.status ? r.error : null)) || fallback; cx.notify(); };
      let queries = v.queries, judgments = v.judgments;
      if (v.on === "own") {
        const made = await api("/api/testsets", { body: { name, source: this.source(), mapping: v.mapping, default_grade: v.default_grade }, timeout: 120 });
        if (!made.ok) return fail(made, "Couldn't make a test set from that table.");
        queries = made.data.queries; judgments = made.data.judgments;
      }
      const where = this.cost.where && this.cost.where !== LOCAL ? this.cost.where : undefined;
      const plan = await api("/api/backtests", { body: {
        name, arms: [v.against, ...v.models], mode: v.on === "logs" ? "historic" : "simulated",
        ...(v.on === "logs" ? { logs: v.logs, population_size: Number(v.population_size) } : { queries, judgments }),
        hypothesis: v.expect.trim() || "Nothing was written down before the run.", primary: v.measure, mde: Number(v.mde), ...(where ? { compute: where } : {}),
      }, timeout: 120 });
      if (!plan.ok) return fail(plan, "Couldn't save the test.");
      let live = null;
      if (runIt) {
        const started = await api(`/api/backtests/${enc(name)}/run`, { body: where ? { compute: where } : {} });
        if (!started.ok) return fail(started, "The test is saved, but it couldn't start.");
        live = started.data?.live ?? null;
      }
      // Runs comes forward on it: live while it runs, else the saved test.
      setShared("captain.open", live ? { section: "runs", live, at: Date.now() } : { section: "runs", backtest: name, at: Date.now() });
      run("layout.select", { stack: "sections", child: "runs" });
      this.close();
    });
    cx.notify();
  }

  // ---- drawing ------------------------------------------------------------------------------------------------

  chip(c, id, label, on, onClick, { sub, disabled, tip } = {}) {
    return h_flex().id(id).min_h(28).px(10).py(3).gap(6).flex_none().items_center().text_size(12.5)
      .border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(disabled ? c.grey500 : on ? c.blue : c.navy)
      .when(!disabled, (e) => e.cursor_pointer().hover((s) => s.border_color(c.blue)).on_click(onClick))
      .when(!!tip, (e) => e.tooltip(tip))
      .child(div().child(label)).when(!!sub, (e) => e.child(div().text_size(11).text_color(c.grey500).child(sub)));
  }

  field(c, id, st, width = 260) {
    return div().id(id).w(width).h(30).px(8).flex().flex_none().items_center().border_1().border_color(c.grey300).bg(c.surface)
      .child(Input.new(st).w_full().text_size(12.5).text_color(c.navy));
  }

  section(c, id, n, title, ...children) {
    return v_flex().id(`ev-${id}`).flex_none().gap(8).pb(18)
      .child(h_flex().gap(8).items_center()
        .child(div().size(20).flex().flex_none().items_center().justify_center().bg(c.blueLight).text_color(c.blue).text_size(11.5).font_medium().child(String(n)))
        .child(div().text_size(14).font_semibold().child(title)))
      .children(children.flat().filter(Boolean));
  }

  label(c, text) { return div().text_size(12).text_color(c.grey500).child(text); }

  models(c) {
    const ps = this.pipelines ?? [];
    const v = this.v;
    if (this.pipelines && !ps.length) return [div().id("ev-no-models").text_size(12.5).text_color(c.grey900).child("There are no models yet. Train or import one from a project, then come back.")];
    return [
      this.label(c, "Test"),
      h_flex().id("ev-models").flex_wrap().gap(6).children(ps.map((p, i) => this.chip(c, `ev-model-${i}`, p.label, v.models.includes(p.value),
        (_e, cx) => { v.models = v.models.includes(p.value) ? v.models.filter((x) => x !== p.value) : [...v.models, p.value]; cx.notify(); }, { sub: `v${p.version}` }))),
      this.label(c, "Compare with"),
      h_flex().id("ev-against").flex_wrap().gap(6).children(ps.map((p, i) => this.chip(c, `ev-against-${i}`, p.label, v.against === p.value,
        (_e, cx) => { v.against = p.value; cx.notify(); }, { sub: `v${p.version}` }))),
    ];
  }

  testOn(c) {
    const v = this.v;
    const pick = (id, list, value, onPick, empty) => (list.length
      ? h_flex().id(id).flex_wrap().gap(6).children(list.map((d, i) => this.chip(c, `${id}-${i}`, d.label, value === d.value, (_e, cx) => onPick(d.value, cx), { sub: d.rows ? `${int(d.rows)} ${d.rows === 1 ? "row" : "rows"}` : `v${d.version}` })))
      : div().id(`${id}-none`).text_size(12.5).text_color(c.grey900).child(empty));
    const out = [h_flex().id("ev-on").flex_wrap().gap(6).children(ON.map(([id, label]) => this.chip(c, `ev-on-${id}`, label, v.on === id, (_e, cx) => { v.on = id; cx.notify(); }))),
      div().text_size(12.5).text_color(c.grey900).child(ON.find(([id]) => id === v.on)[2])];
    if (v.on === "logs") out.push(this.label(c, "Logged searches"), pick("ev-logs", this.byRole("events"), v.logs, (x, cx) => { v.logs = x; cx.notify(); }, "No logged searches in Data yet. Import some, or test on your own questions."));
    if (v.on === "saved") out.push(
      this.label(c, "Questions"), pick("ev-queries", this.byRole("queries"), bareOr(v.queries, this.byRole("queries")), (x, cx) => { v.queries = x; cx.notify(); }, "No sets of questions in Data yet."),
      this.label(c, "Answers"), pick("ev-judgments", this.byRole("judgments"), bareOr(v.judgments, this.byRole("judgments")), (x, cx) => { v.judgments = x; cx.notify(); }, "No sets of answers in Data yet. Rate some, or test on your own questions."));
    if (v.on === "own") out.push(...this.own(c));
    return out;
  }

  /** Their own table: where it is, which column is which, and what that makes. */
  own(c) {
    const v = this.v, p = this.preview;
    const tables = (this.datasets ?? []).map((d) => ({ value: d.name, label: d.name, rows: d.rows }));
    const out = [
      h_flex().gap(6).children([["data", "A table in Data"], ["file", "A file on this computer"]].map(([id, label]) =>
        this.chip(c, `ev-src-${id}`, label, v.src === id, (_e, cx) => { v.src = id; this.preview = null; v.mapping = {}; this.loadPreview(cx); }))),
      v.src === "data"
        ? (tables.length ? h_flex().id("ev-tables").flex_wrap().gap(6).children(tables.slice(0, 40).map((d, i) => this.chip(c, `ev-table-${i}`, d.label, v.dataset === d.value,
          (_e, cx) => { v.dataset = d.value; v.mapping = {}; this.loadPreview(cx); }, { sub: d.rows ? `${int(d.rows)} ${d.rows === 1 ? "row" : "rows"}` : null })))
          : div().id("ev-tables-none").text_size(12.5).text_color(c.grey900).child("No tables in Data yet. Use a file on this computer."))
        : h_flex().gap(8).items_center()
          .child(this.field(c, "ev-file", this.inputs.file, 380))
          .child(Button(c, "ev-file-read", "Read it", (_e, cx) => this.loadPreview(cx), { small: true }))
          .child(div().text_size(12).text_color(c.grey500).child("CSV, TSV, Parquet, JSON or JSON lines. Or drop the file on this form.")),
    ];
    if (this.previewing && !p) out.push(Skeleton(c, 3));
    if (p?.error) out.push(div().id("ev-preview-error").p(10).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).text_color(c.fail).child(p.error));
    if (!p || p.error) return out;
    // The mapper: one row per thing the test needs, each a pick among the table's columns.
    out.push(v_flex().id("ev-mapper").gap(8).p(12).border_1().border_color(c.rule)
      .child(div().text_size(12.5).font_medium().child(`Which column is which, in ${p.source}`))
      .children(ROLES.map(([role, label, required, what]) => h_flex().id(`ev-map-${role}`).gap(10).items_start()
        .child(v_flex().w(150).flex_none().gap(1)
          .child(div().text_size(12.5).child(required ? label : `${label} (optional)`))
          .child(div().text_size(11).line_height("15px").text_color(c.grey500).child(what)))
        .child(h_flex().flex_1().min_w(0).flex_wrap().gap(4).children(p.columns.map((col, i) => this.chip(c, `ev-map-${role}-${i}`, col, v.mapping[role] === col, (_e, cx) => this.mapColumn(cx, role, col)))))))
      // The grade a row gets when it has none: a limit said as a field, not hidden.
      .child(h_flex().id("ev-default-grade").gap(10).items_center()
        .child(div().w(150).flex_none().text_size(12.5).child("A row with no grade counts as"))
        .children([3, 2, 1].map((g) => this.chip(c, `ev-grade-${g}`, `${g}, ${(this.opts?.grades ?? {})[g] ?? ""}`, v.default_grade === g, (_e, cx) => { v.default_grade = g; this.loadPreview(cx, true); })))));
    // The preview: what the mapping makes, and every row it leaves out with the reason.
    const skipped = Object.entries(p.skipped ?? {});
    out.push(v_flex().id("ev-preview").gap(6)
      .child(div().id("ev-preview-count").text_size(12.5).text_color(p.ready ? c.navy : c.fail)
        .child(p.ready ? `This makes ${int(p.questions)} ${p.questions === 1 ? "question" : "questions"} with ${int(p.answers)} wanted ${p.answers === 1 ? "answer" : "answers"}, from ${int(p.total)} rows.` : (p.problem ?? "No row has both a question and a product.")))
      .when(skipped.length > 0, (e) => e.child(div().id("ev-preview-skipped").text_size(12).text_color(c.grey900)
        .child(`Left out: ${skipped.map(([why, n]) => `${int(n)} with ${why}`).join(", ")}.`)))
      .when((p.sample ?? []).length > 0, (e) => e.child(v_flex().border_1().border_color(c.rule)
        .child(h_flex().h(26).bg(c.surface2).border_b(1).border_color(c.rule).text_size(12).font_medium().text_color(c.grey900)
          .child(div().flex_1().min_w(0).px(8).child("Question")).child(div().w(180).flex_none().px(8).child("Wanted product")).child(div().w(110).flex_none().px(8).child("Grade")).child(div().w(120).flex_none().px(8).child("Group")))
        .children(p.sample.slice(0, 5).map((r, i) => h_flex().id(`ev-sample-${i}`).h(26).border_b(1).border_color(c.rule).text_size(12).items_center()
          .child(div().flex_1().min_w(0).px(8).truncate().child(String(r.query)))
          .child(div().w(180).flex_none().px(8).truncate().font_family(MONO).child(String(r.product_id)))
          .child(div().w(110).flex_none().px(8).truncate().child(`${r.grade}, ${(this.opts?.grades ?? {})[r.grade] ?? ""}`))
          .child(div().w(120).flex_none().px(8).truncate().text_color(c.grey900).child(String(r.slice ?? ""))))))));
    return out;
  }

  measures(c) {
    const v = this.v;
    const rows = (this.opts?.measures ?? []).map((m, i) => {
      const ok = measureOk(m, v.on), on = v.measure === m.id;
      return h_flex().id(`ev-measure-${m.id}`).min_h(34).px(10).py(4).gap(10).items_center().border_b(1).border_color(c.rule)
        .when(ok, (e) => e.cursor_pointer().hover((s) => s.bg(c.surface2)).on_click((_e, cx) => { v.measure = m.id; cx.notify(); }))
        .bg(on ? c.blueLight : c.surface)
        .child(div().size(14).flex_none().rounded_full().border_1().border_color(on ? c.blue : c.grey300).flex().items_center().justify_center()
          .when(on, (e) => e.child(div().size(8).rounded_full().bg(c.blue))))
        .child(div().w(190).flex_none().text_size(12.5).text_color(ok ? c.navy : c.grey500).child(m.label))
        .child(div().flex_1().min_w(0).text_size(12).line_height("16px").text_color(c.grey900).child(ok ? m.line : `${m.line} Not for this test: ${measureWhy(m)}`));
    });
    return [
      div().text_size(12.5).text_color(c.grey900).child("Pick the measure that decides the verdict. Every measure below is computed and shown either way."),
      v_flex().id("ev-measures").border_1().border_color(c.rule).children(rows),
      // Slices are fixed by the runner: said, with what feeds them, rather than offered as a choice that does nothing.
      div().id("ev-slices").text_size(12).line_height("17px").text_color(c.grey500)
        .child(`Each run also reports its results ${(this.opts?.slices ?? []).map((s) => `${s.label.toLowerCase()} (needs ${s.needs})`).join(", and ")}.`),
    ];
  }

  limits(c) {
    return (this.opts?.limits ?? []).filter((l) => !l.only || l.only === this.v.on).map((l) => h_flex().id(`ev-limit-${l.id}`).gap(10).items_center()
      .child(div().w(210).flex_none().text_size(12.5).child(l.label))
      .child(this.field(c, `ev-${l.id}`, this.inputs[l.id], 110))
      .child(div().flex_1().min_w(0).text_size(12).line_height("16px").text_color(c.grey500).child(`${l.line} Starts at ${l.default}.`)));
  }

  render() {
    const c = palette(isDark(theme()));
    const v = this.v;
    const miss = this.opts ? missing(v, { preview: this.preview, opts: this.opts }) : [];
    const ready = this.opts && this.pipelines;
    return v_flex().id("new-evaluation").size_full().bg(c.surface).font_family(FONT).text_color(c.navy)
      // A file dropped on the form is the person's own questions.
      .on_drop((e, cx) => { const p = (e.paths ?? [])[0]; if (!p) return; v.on = "own"; v.src = "file"; this.set("file", p); v.mapping = {}; this.loadPreview(cx); })
      .child(h_flex().h(52).flex_none().px(20).gap(10).items_center().border_b(1).border_color(c.rule)
        .child(svg(icon("flask-conical")).size(16).text_color(c.blue))
        .child(div().flex_1().text_size(16).font_semibold().child(this.from ? `Run ${this.from} on another model` : "New evaluation"))
        .child(div().id("ev-close").size(24).flex().items_center().justify_center().cursor_pointer().text_color(c.grey500).hover((s) => s.text_color(c.navy)).on_click(() => this.close()).child(svg(icon("x")).size(14))))
      .child(!ready ? v_flex().flex_1().child(Skeleton(c, 8)) : v_flex().id("ev-body").flex_1().min_h(0).overflow_y_scrollbar().px(20).pt(16)
        .child(this.section(c, "models", 1, "Which models", this.models(c)))
        .child(this.section(c, "on", 2, "What to test on", this.testOn(c)))
        .child(this.section(c, "measure", 3, "What decides it", this.measures(c)))
        .child(this.section(c, "limits", 4, "Limits", this.limits(c)))
        .child(this.section(c, "where", 5, "Where it runs", RunsOn(c, "ev-runs-on", this.cost, {
          onPick: (p, _e, cx) => { if (this.cost.where !== p.value) this.cost.estimate = null; this.cost.where = p.value; this.cost.confirming = false; cx.notify(); },
          onToggle: (_e, cx) => { this.cost.open = !this.cost.open; cx.notify(); },
          onAdd: () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "compute" } }),
        })))
        .child(this.section(c, "name", 6, "Save it as a test",
          h_flex().gap(10).items_center()
            .child(div().w(210).flex_none().text_size(12.5).child("Name"))
            .child(this.field(c, "ev-name", this.inputs.name, 300))
            .child(div().flex_1().min_w(0).text_size(12).line_height("16px").text_color(c.grey500).child((this.tests ?? []).some((t) => t.name === v.name.trim())
              ? "A test with this name exists. This run joins its history, so the models line up in Runs, Compare."
              : "The setup is saved under this name. Run it again on a new model later with one pick.")),
          h_flex().gap(10).items_start()
            .child(v_flex().w(210).flex_none().gap(1).child(div().text_size(12.5).child("What you expect (optional)")).child(div().text_size(11).line_height("15px").text_color(c.grey500).child("Written down before the run, so the result can't move the goalposts.")))
            .child(div().id("ev-expect").flex_1().min_w(0).border_1().border_color(c.grey300).child(Textarea.new(this.inputs.expect).w_full().text_size(12.5).text_color(c.navy))))))
      .child(v_flex().flex_none().border_t(1).border_color(c.rule).px(20).py(10).gap(8)
        .when(this.tried && miss.length > 0, (e) => e.child(v_flex().id("ev-missing").gap(2).children(miss.map((m, i) => div().id(`ev-missing-${i}`).text_size(12.5).text_color(c.fail).child(m)))))
        .when(!!this.error, (e) => e.child(div().id("ev-error").text_size(12.5).text_color(c.fail).child(this.error)))
        .child(h_flex().gap(8).items_center()
          .child(div().flex_1())
          .child(Button(c, "ev-cancel", "Cancel", () => this.close(), { small: true, kind: "quiet" }))
          .child(Button(c, "ev-save", this.busy === "Saving" ? "Saving" : "Save the test without running it", (_e, cx) => this.submit(cx, false), { small: true, disabled: !!this.busy || !ready }))
          .child(Button(c, "ev-run", this.busy === "Starting" ? "Starting" : this.cost.confirming ? "Start anyway" : "Run the evaluation", (_e, cx) => this.submit(cx, true), { kind: "primary", small: true, disabled: !!this.busy || !ready }))));
  }
}

/** A saved ref ("name@v2", maybe with ":split") as the matching option's value, so the chip shows as picked. */
function bareOr(ref, options) {
  if (!ref) return null;
  const name = bare(String(ref).split(":")[0]);
  return options.find((o) => o.value === ref)?.value ?? options.find((o) => bare(o.value) === name)?.value ?? ref;
}
