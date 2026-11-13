// CAPTAIN: Explain: why did it rank this? (plan 2, backend/docs/api.md "Query debugger"). One card per stage, top to
// bottom: query understanding (tokens, expansions, synonyms, unknown terms), retrieval (each result's score parts as
// small bars, field:term), boosts (rules fired, before and after, movement), filter (what it removed, and why) and the
// final ranking with grades. A stage the engine can't explain says so plainly. With two systems, each stage sits side
// by side and the first stage where a relevant result was lost is highlighted, with its sentence.
// Props: {run, query_id, a?, b?} (GET /api/runs/{run}/queries/{qid}/explain, from a comparison in Runs)
//      | {pipeline, q, vs?} (GET /api/explain) | none: pick a pipeline and type a query.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, shared } from "herdr";
import { palette, isDark, FONT, MONO, RADIUS } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Button, Badge, Skeleton, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { askAgent, copyPrompt } from "./kit/agent.js";

const TOP = 10;
const enc = encodeURIComponent;
const num = (v) => (v == null ? "" : Number(v).toFixed(Math.abs(v) >= 10 ? 1 : 2));
const KIND = { understand: "Query understanding", retrieve: "Retrieval", rules: "Boosts", filter: "Filter", final: "Final ranking" };

export default class Explain extends View {
  init(_props, cx) {
    // Test hook: opened bare (`captain-app do mod.open`), shared "captain.explain.preview" supplies the props.
    const given = props() ?? {};
    this.p = given.run || given.pipeline ? given : { ...given, ...(shared("captain.explain.preview") ?? {}) };
    this.trace = null; this.error = null; this.loading = false;
    this.query = InputState.new({ placeholder: "e.g. earbuds under 50" });
    this.query.on("submit", (_e, ecx) => this.ask(ecx ?? cx));
    this.pipeline = this.p.pipeline ?? null; this.vs = this.p.vs ?? null;
    if (this.p.q) this.query.set_value(this.p.q);
    cx.spawn(async (cx) => { const r = await api("/api/pipelines"); this.pipelines = r.ok ? r.data ?? [] : []; if (!this.pipeline) this.pipeline = this.pipelines[0]?.name ?? null; cx.notify(); });
    if (this.p.run || (this.p.pipeline && this.p.q)) this.ask(cx);
  }

  ask(cx) {
    const p = this.p;
    const q = this.query.value().trim();
    let path;
    if (p.run && p.query_id) {
      const qs = new URLSearchParams({ ...(p.a ? { a: p.a } : {}), ...(p.b ? { b: p.b } : {}) }).toString();
      path = `/api/runs/${enc(p.run)}/queries/${enc(p.query_id)}/explain${qs ? `?${qs}` : ""}`;
    } else {
      if (!this.pipeline || !q) { this.error = !this.pipeline ? "Pick a model version." : "Type a query."; cx.notify(); return; }
      path = `/api/explain?${new URLSearchParams({ pipeline: this.pipeline, q, k: String(TOP), ...(this.vs ? { vs: this.vs } : {}) })}`;
    }
    this.loading = true; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(path, { timeout: 60 });
      this.loading = false;
      if (r.ok) this.trace = r.data; else this.error = plainError(r.error, "Captain couldn't explain that query.");
      cx.notify();
    });
  }

  arms() {
    const t = this.trace;
    if (!t) return [];
    return t.a && t.b ? [t.a, t.b] : [t];
  }

  // The divergence in one sentence: "At boosts, the relevant p0088 (title) dropped from 2 to 38."
  divergence() {
    const t = this.trace;
    const d = t?.first_divergence;
    if (!d) return null;
    const title = (id) => { for (const arm of [t.a, t.b]) for (const st of arm?.stages ?? []) for (const it of st.items ?? []) if (it.id === id && it.title) return it.title; return null; };
    const name = (id) => (title(id) ? `${id} (${title(id)})` : id);
    const parts = [
      ...(d.ranked_lower ?? []).map((x) => `the relevant ${name(x.id)} dropped from ${x.rank_a} to ${x.rank_b}`),
      ...(d.lost ?? []).map((x) => `the relevant ${name(typeof x === "string" ? x : x.id)} was lost`),
    ];
    return `At ${d.stage}, ${parts.length ? parts.slice(0, 3).join("; ") : "a relevant result was lost or ranked lower"}.`;
  }

  // What an agent gets: the query, the systems, each stage's notes, and the divergence.
  prompt() {
    const t = this.trace;
    if (!t) return "";
    const lines = [`Explain why Captain's search ranked these results for the query "${t.a?.query ?? t.query}".`];
    for (const arm of this.arms()) {
      lines.push(`\n${arm.pipeline} (${arm.engine ?? "engine"}):`);
      for (const st of arm.stages ?? []) {
        const top = (st.items ?? []).slice(0, 5).map((i) => `${i.id}${i.grade != null ? ` grade ${i.grade}` : ""}`).join(", ");
        lines.push(`- ${st.stage}: ${st.explained === false ? "not explained" : (st.notes ?? []).join("; ") || "ok"}${top ? `. Top: ${top}` : ""}`);
      }
    }
    const div = this.divergence();
    lines.push(div ? `\nFirst divergence: ${div}` : t.note ? `\n${t.note}` : "");
    lines.push("Use the cst CLI (cst explain) to dig deeper. Change nothing without asking me.");
    return lines.join("\n");
  }

  // ---- stages ---------------------------------------------------------------------------------------------

  chip(c, text, tone) {
    const bg = tone === "warn" ? c.warnLight : c.surface2, fg = tone === "warn" ? c.warn : c.navy;
    return div().px(6).py(1).text_size(12).font_family(MONO).bg(bg).text_color(fg).child(text);
  }

  notes(c, st) {
    return v_flex().gap(2).children((st.notes ?? []).map((n, i) => div().id(`n-${st.stage}-${i}`).text_size(12).line_height("17px").text_color(c.grey900).child(n)));
  }

  understand(c, st) {
    const syn = Object.entries(st.synonyms ?? {});
    const unknown = new Set(st.unknown_terms ?? []);
    return v_flex().gap(8)
      .child(h_flex().flex_wrap().gap(4).children((st.tokens ?? st.terms ?? []).map((t) => this.chip(c, t, unknown.has(t) ? "warn" : null))))
      .when((st.expanded_with ?? []).length > 0, (e) => e.child(h_flex().gap(6).items_center().child(div().text_size(12).text_color(c.grey500).child("Expanded with"))
        .children(st.expanded_with.map((t) => this.chip(c, t)))))
      // Unknown terms are the amber chips; the stage's own notes say it in words below.
      .when(syn.length > 0, (e) => e.child(div().text_size(12).text_color(c.grey900).child(`Synonyms: ${syn.map(([k, v]) => `${k} = ${[].concat(v).join(", ")}`).join("; ")}`)));
  }

  itemRow(c, it, extra) {
    return h_flex().id(`it-${it.id}-${it.rank}`).min_h(24).gap(8).items_center()
      .child(div().w(22).flex_none().text_size(11.5).font_family(MONO).text_color(c.grey500).child(String(it.rank ?? "")))
      .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.navy).tooltip(`${it.id} ${it.title ?? ""}`).child(it.title ?? it.id))
      .when(it.grade != null, (e) => e.child(Badge(c, `grade ${it.grade}`, it.grade >= 2 ? "good" : it.grade === 1 ? "neutral" : "bad")))
      .child(extra ?? div());
  }

  retrieve(c, st) {
    const items = (st.items ?? []).slice(0, TOP);
    const max = Math.max(1e-9, ...items.map((i) => i.score ?? 0));
    const hues = [c.blue, "#7c3aed", "#0d9488", "#d97706", "#db2777", c.grey500];
    return v_flex().gap(4).children(items.map((it) => {
      const parts = Object.entries(it.score_parts ?? {}).sort((a, b) => b[1] - a[1]);
      return v_flex().gap(2)
        .child(this.itemRow(c, it, div().w(48).flex_none().text_size(11.5).font_family(MONO).text_color(c.grey900).child(num(it.score))))
        // Each part of the score, as a bar sized against the stage's top score, labeled field:term.
        .child(h_flex().pl(30).h(8).w_full().children(parts.map(([k, v], i) => div().id(`sp-${it.id}-${k}`).h_full().flex_none()
          .w(Math.max(2, Math.round((v / max) * 360))).bg(hues[i % hues.length]).tooltip(`${k}: ${num(v)}`))))
        .child(h_flex().pl(30).flex_wrap().gap(8).children(parts.slice(0, 4).map(([k, v], i) => h_flex().gap(3).items_center()
          .child(div().size(6).bg(hues[i % hues.length]))
          .child(div().text_size(11).font_family(MONO).text_color(c.grey500).child(`${k} ${num(v)}`)))));
    }));
  }

  rules(c, st) {
    const moved = (st.items ?? []).filter((i) => (i.fired ?? []).length || i.moved).slice(0, TOP);
    if (!moved.length) return div().text_size(12).text_color(c.grey500).child("No result changed here.");
    return v_flex().gap(4).children(moved.map((it) => {
      const sp = it.score_parts ?? {};
      const m = it.moved ?? 0;
      return v_flex().gap(1)
        .child(this.itemRow(c, it, div().text_size(11.5).text_color(m > 0 ? c.good : m < 0 ? c.fail : c.grey500).child(m > 0 ? `up ${m}` : m < 0 ? `down ${-m}` : "same place")))
        .child(div().pl(30).text_size(11.5).font_family(MONO).text_color(c.grey500)
          .child(`${(it.fired ?? []).join(", ") || "no rule"}. Score ${num(sp.before)} before, ${num(sp.after)} after`));
    }));
  }

  filter(c, st) {
    const removed = st.removed ?? [];
    return v_flex().gap(4)
      .child(div().text_size(12).text_color(c.navy).child(removed.length ? `Removed ${removed.length}` : "Removed nothing"))
      .children(removed.slice(0, TOP).map((r) => h_flex().id(`rm-${r.id}`).gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.navy).child(r.title ?? r.id))
        .when(r.price != null, (e) => e.child(div().text_size(11.5).font_family(MONO).text_color(c.grey500).child(`$${r.price}`)))));
  }

  final(c, st) {
    return v_flex().gap(2).children((st.items ?? []).slice(0, TOP).map((it) => this.itemRow(c, it,
      it.moved ? div().text_size(11.5).text_color(it.moved > 0 ? c.good : c.fail).child(it.moved > 0 ? `up ${it.moved}` : `down ${-it.moved}`) : null)));
  }

  stageBody(c, st) {
    if (!st) return div().text_size(12).text_color(c.grey500).child("This system has no such stage.");
    // A stage the engine doesn't explain says so, instead of showing empty bars.
    if (st.explained === false) return v_flex().gap(4).child(div().text_size(12.5).text_color(c.grey900).child((st.notes ?? [])[0] ?? "This system doesn't explain this step."));
    const body = { understand: this.understand, retrieve: this.retrieve, rules: this.rules, filter: this.filter, final: this.final }[st.kind];
    return v_flex().gap(8).child(body ? body.call(this, c, st) : div()).child(this.notes(c, st));
  }

  stages(c) {
    const arms = this.arms();
    const n = Math.max(...arms.map((a) => (a.stages ?? []).length));
    const div0 = this.trace?.first_divergence?.stage;
    return v_flex().gap(10).children([...Array(n)].map((_, i) => {
      const sts = arms.map((a) => (a.stages ?? [])[i]);
      const name = sts.find(Boolean)?.stage ?? `stage ${i + 1}`;
      const kind = sts.find(Boolean)?.kind;
      const hot = div0 && sts.some((s) => s?.stage === div0);
      return v_flex().id(`stage-${i}`).p(14).gap(10).border_1().border_color(hot ? c.blue : c.rule).bg(hot ? c.blueLight : c.surface)
        .child(h_flex().gap(8).items_center()
          .child(div().size(20).flex_none().flex().items_center().justify_center().text_size(11).font_semibold().bg(hot ? c.blue : c.surface2).text_color(hot ? c.onBlue : c.grey900).child(String(i + 1)))
          .child(div().text_size(13.5).font_semibold().text_color(c.navy).child(name))
          .child(div().text_size(11.5).text_color(c.grey500).child(KIND[kind] && KIND[kind].toLowerCase() !== String(name).toLowerCase() ? KIND[kind] : ""))
          .child(div().flex_1())
          .children(sts.map((s, k) => (s?.timing_ms != null ? div().text_size(11).font_family(MONO).text_color(c.grey500).child(`${arms.length > 1 ? (k ? "B " : "A ") : ""}${num(s.timing_ms)} ms`) : div()))))
        .when(Boolean(hot), (e) => e.child(div().text_size(12.5).font_semibold().text_color(c.blue).child(this.divergence())))
        .child(h_flex().gap(16).items_start().children(sts.map((s, k) => v_flex().flex_1().min_w(0).gap(6)
          .when(arms.length > 1, (e) => e.child(div().text_size(11.5).font_semibold().text_color(c.grey500).child(`${k ? "B" : "A"}: ${arms[k].pipeline}`)))
          .child(this.stageBody(c, s)))));
    }));
  }

  // No props: pick a pipeline (and one to compare with) and type a query.
  asker(c) {
    const pipes = this.pipelines ?? [];
    const chips = (id, value, pick, none) => h_flex().flex_wrap().gap(6)
      .when(Boolean(none), (e) => e.child(this.pick(c, `${id}-none`, none, value == null, () => pick(null))))
      .children(pipes.map((p) => this.pick(c, `${id}-${p.name}`, p.name, value === p.name, () => pick(p.name))));
    return v_flex().gap(12).p(16).border_1().border_color(c.rule)
      .child(v_flex().gap(6).child(div().text_size(12.5).font_semibold().child("Model version")).child(!this.pipelines ? div().h(28).w(200).bg(c.surface2) : chips("ex-pipe", this.pipeline, (v) => { this.pipeline = v; })))
      .child(v_flex().gap(6).child(div().text_size(12.5).font_semibold().child("Compare with (optional)")).child(chips("ex-vs", this.vs, (v) => { this.vs = v; }, "Nothing")))
      .child(v_flex().gap(6).child(div().text_size(12.5).font_semibold().child("Query"))
        .child(h_flex().gap(8)
          .child(div().id("ex-q").flex_1().h(32).flex().items_center().px(10).border_1().border_color(c.grey300).cursor_text()
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.query).w_full().text_size(13).text_color(c.navy)))
          .child(Button(c, "ex-go", this.loading ? "Explaining..." : "Explain", (_e, cx) => this.ask(cx), { kind: "primary", disabled: this.loading }))));
  }

  pick(c, id, label, on, onPick) {
    return div().id(id).h(28).px(10).flex().items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
      .on_click((_e, cx) => { onPick(); cx.notify(); }).child(label);
  }

  render() {
    const c = palette(isDark(theme()));
    const t = this.trace;
    const arms = this.arms();
    const fromRun = Boolean(this.p.run);
    const div0 = this.divergence();
    return v_flex().id("explain").size_full().bg(c.surface).rounded(RADIUS).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(56).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
        .child(div().size(28).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("search")).size(15)))
        .child(v_flex().flex_1().min_w(0)
          .child(div().text_size(15).font_semibold().child("Why it ranked this way"))
          .when(Boolean(t), (e) => e.child(div().truncate().text_size(12).text_color(c.grey500).child(`"${t.a?.query ?? t.query}", ${arms.map((a) => a.pipeline).join(" vs ")}`))))
        .when(Boolean(t), (e) => e
          .child(Button(c, "ex-copy", "Copy prompt", (_e, cx) => copyPrompt(cx, this.prompt()), { small: true }))
          .child(Button(c, "ex-agent", "Open an agent", (_e, cx) => askAgent(cx, this.prompt()), { small: true })))
        .child(div().id("ex-close").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => closeOverlay(props()?.overlay)).child("Close")))
      .child(div().id("ex-body").flex_1().min_h(0).overflow_y_scroll().child(v_flex().px(20).py(16).gap(14)
        .when(!fromRun, (e) => e.child(this.asker(c)))
        .when(Boolean(this.error), (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.error)))
        .when(this.loading && !t, (e) => e.child(Skeleton(c, 6)))
        // Two systems: the first stage where a relevant result was lost, or why that can't be said.
        .when(Boolean(t) && arms.length > 1, (e) => e.child(div().id("ex-div").p(12).text_size(13).line_height("19px")
          .bg(div0 ? c.blueLight : c.surface2).text_color(div0 ? c.blue : c.grey900)
          .child(div0 ?? t.note ?? "Both systems kept every relevant result through every stage.")))
        .when(Boolean(t), (e) => e.child(this.stages(c)))));
  }
}
