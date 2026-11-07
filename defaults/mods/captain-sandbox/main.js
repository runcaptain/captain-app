// CAPTAIN: What if... (plan 1, backend/docs/api.md "What-if sandbox"). Try settings of a pipeline offline before an
// A/B test: up to three knobs from its spec (numeric paths, plus spec.tunables when declared), each with values or a
// min:max:steps range, over queries and judgments. POST /api/sandbox/sweep runs a quick pass on a sample, then the best
// on every query. One knob draws mean nDCG against its value with interval whiskers and the current value marked; two
// draw a heat grid. Full-pass cells carry verdicts and slice flips. A picked cell can show its ripple (POST
// /api/sandbox/ripple: moved most, regressions, a must-not-break list) or become the next version (POST
// /api/sandbox/promote), then a backtest against the current one.
// Props: {pipeline?}.
import { View, div, svg, PathBuilder } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, openOverlay, notify } from "herdr";
import { palette, isDark, FONT, MONO, RADIUS } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Button, Badge, Skeleton, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { int, num } from "./kit/num.js";

const MAX_KNOBS = 3;
const sleep = (cx, ms) => new Promise((r) => cx.timer.after(ms, () => r()));
const kindOf = (d) => d?.kind ?? (d?.classes ?? []).find((k) => ["queries", "judgments"].includes(k)) ?? null;
const key = (values) => JSON.stringify(values);
const TONE = { win: "good", loss: "bad", noise: "neutral" };

/** Numeric settings in a spec, as dotted paths: k1, fields.title, boosts.0.weight. */
function numericPaths(spec, prefix = "", out = []) {
  if (spec == null || typeof spec !== "object") return out;
  for (const [k, v] of Object.entries(spec)) {
    if (k === "tunables") continue;
    const p = prefix ? `${prefix}.${k}` : k;
    if (typeof v === "number") out.push({ path: p, current: v });
    else if (v && typeof v === "object") numericPaths(v, p, out);
  }
  return out;
}

/** "1, 1.2, 1.5" or "0.8:2:4" (min:max:steps), as the services take it; null when it can't be read. */
function parseKnob(text) {
  const t = String(text ?? "").trim();
  const range = /^(-?[\d.]+)\s*:\s*(-?[\d.]+)\s*:\s*(\d+)$/.exec(t);
  if (range) return { min: Number(range[1]), max: Number(range[2]), steps: Number(range[3]) };
  const values = t.split(/[,\s]+/).filter(Boolean).map(Number);
  return values.length && values.every(Number.isFinite) ? { values } : null;
}

export default class Sandbox extends View {
  init(_props, cx) {
    this.p = props() ?? {};
    this.pipeline = this.p.pipeline ? String(this.p.pipeline).split("@")[0] : null;
    this.spec = null; this.knobs = []; this.states = {};
    this.queries = null; this.judgments = null;
    this.result = null; this.busy = null; this.error = null; this.progress = null;
    this.cell = null; this.ripple = null; this.promoted = null;
    this.mustNot = InputState.new({ placeholder: "e.g. running shoes, rain jacket" });
    cx.spawn(async (cx) => {
      const [p, d] = await Promise.all([api("/api/pipelines"), api("/api/datasets")]);
      this.pipelines = p.ok ? p.data ?? [] : [];
      this.datasets = d.ok ? d.data ?? [] : [];
      const of = (k) => this.datasets.filter((x) => kindOf(x) === k).map((x) => x.name);
      this.queries = of("queries").find((n) => !n.includes("/")) ?? of("queries")[0] ?? null;
      this.judgments = of("judgments").find((n) => !n.includes("/")) ?? of("judgments")[0] ?? null;
      if (!this.pipeline) this.pipeline = this.pipelines[0]?.name ?? null;
      if (this.pipeline) await this.loadSpec(cx);
      cx.notify();
    });
  }

  async loadSpec(cx) {
    this.spec = null; this.knobs = []; this.result = null; this.cell = null;
    const r = await api(`/api/pipelines/${encodeURIComponent(this.pipeline)}`);
    this.spec = r.ok ? r.data?.spec ?? {} : {};
    this.version = r.ok ? r.data?.version : null;
    // Declared tunables first, each with its own range; then every numeric setting.
    const tun = Object.entries(this.spec.tunables ?? {}).map(([path, range]) => ({ path, range, current: path.split(".").reduce((o, k) => o?.[k], this.spec) }));
    const seen = new Set(tun.map((t) => t.path));
    this.offer = [...tun, ...numericPaths(this.spec).filter((x) => !seen.has(x.path))];
    cx.notify();
  }

  toggleKnob(cx, k) {
    const i = this.knobs.findIndex((x) => x.path === k.path);
    if (i >= 0) { this.knobs.splice(i, 1); cx.notify(); return; }
    if (this.knobs.length >= MAX_KNOBS) { this.error = `Up to ${MAX_KNOBS} knobs at a time.`; cx.notify(); return; }
    this.error = null;
    this.knobs.push(k);
    if (!this.states[k.path]) {
      // Values around today's: the declared range, else three steps from three quarters to one and a half times.
      const cur = Number(k.current);
      const r = k.range;
      const start = r?.values ? r.values.join(", ") : r ? `${r.min}:${r.max}:${r.steps ?? 4}` : Number.isFinite(cur) && cur !== 0 ? [cur * 0.75, cur, cur * 1.5].map((v) => +v.toFixed(3)).join(", ") : "0, 1, 2";
      const st = InputState.new({ placeholder: "1, 1.2, 1.5  or  0.8:2:4" });
      st.set_value(start);
      this.states[k.path] = st;
    }
    cx.notify();
  }

  sweep(cx) {
    const knobs = {};
    for (const k of this.knobs) {
      const v = parseKnob(this.states[k.path]?.value());
      if (!v) { this.error = `Write ${k.path}'s values as 1, 1.2, 1.5 or a range like 0.8:2:4.`; cx.notify(); return; }
      knobs[k.path] = v;
    }
    if (!this.pipeline || !this.queries || !this.judgments) { this.error = "Pick a pipeline, its queries and its judgments."; cx.notify(); return; }
    if (!Object.keys(knobs).length && !this.spec?.tunables) { this.error = "Pick at least one knob."; cx.notify(); return; }
    this.busy = "sweep"; this.error = null; this.result = null; this.cell = null; this.ripple = null; this.promoted = null; this.progress = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/sandbox/sweep", { body: { pipeline: this.pipeline, queries: this.queries, judgments: this.judgments, ...(Object.keys(knobs).length ? { knobs } : {}) } });
      if (!r.ok || !r.data?.job) { this.busy = null; this.error = plainError(r.error, "Captain couldn't start the sweep."); cx.notify(); return; }
      for (let i = 0; i < 1200; i++) {
        const j = await api(`/api/jobs/${encodeURIComponent(r.data.job)}`);
        if (j.ok && j.data?.status === "done") { this.result = j.data.result; this.busy = null; cx.notify(); return; }
        if (j.ok && j.data?.status === "failed") { this.busy = null; this.error = plainError(j.data?.message ?? j.data?.error, "The sweep didn't finish."); cx.notify(); return; }
        if (j.ok) { this.progress = j.data?.progress ?? null; cx.notify(); }
        await sleep(cx, 700);
      }
      this.busy = null; this.error = "This took too long."; cx.notify();
    });
  }

  seeRipple(cx) {
    const must = this.mustNot.value().split(",").map((s) => s.trim()).filter(Boolean);
    this.busy = "ripple"; this.ripple = null; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/sandbox/ripple", { body: { pipeline: this.pipeline, queries: this.queries, judgments: this.judgments, values: this.cell.values, ...(must.length ? { must_not_break: must } : {}) }, timeout: 120 });
      this.busy = null;
      if (r.ok) this.ripple = r.data; else this.error = plainError(r.error, "Captain couldn't work out the ripple.");
      cx.notify();
    });
  }

  promote(cx) {
    this.busy = "promote"; this.error = null; cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/sandbox/promote", { body: { pipeline: this.pipeline, values: this.cell.values, note: `From What if: ${Object.entries(this.cell.values).map(([k, v]) => `${k} ${v}`).join(", ")}` } });
      this.busy = null;
      if (!r.ok) { this.error = plainError(r.error, "Captain couldn't make the next version."); cx.notify(); return; }
      this.promoted = r.data;
      notify({ title: `${this.pipeline}: next version ready`, body: "Nothing went live. Run a backtest against the current version next.", level: "info" });
      cx.notify();
    });
  }

  // ---- drawing --------------------------------------------------------------------------------------------

  pill(c, id, label, on, pick, sub) {
    return h_flex().id(id).h(28).px(10).gap(6).items_center().cursor_pointer().text_size(12.5).border_1()
      .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
      .on_click((_e, cx) => { pick(cx); cx.notify(); }).child(label)
      .when(sub != null, (e) => e.child(div().text_size(11).font_family(MONO).text_color(c.grey500).child(String(sub))));
  }

  form(c) {
    const names = (k) => (this.datasets ?? []).filter((x) => kindOf(x) === k).map((x) => x.name);
    const row = (label, child) => v_flex().gap(6).child(div().text_size(12.5).font_semibold().text_color(c.navy).child(label)).child(child);
    return v_flex().gap(14).p(16).border_1().border_color(c.rule)
      .child(row("Pipeline", !this.pipelines ? div().h(28).w(200).bg(c.surface2) : h_flex().flex_wrap().gap(6).children(this.pipelines.map((p) =>
        this.pill(c, `sb-pipe-${p.name}`, p.name, this.pipeline === p.name, (cx) => { this.pipeline = p.name; cx.spawn(async (cx) => this.loadSpec(cx)); }, `v${p.latest}`)))))
      .child(row(`Knobs, up to ${MAX_KNOBS}`, !this.offer ? div().h(28).w(200).bg(c.surface2) : this.offer.length
        ? h_flex().flex_wrap().gap(6).children(this.offer.map((k) => this.pill(c, `sb-knob-${k.path}`, k.path, this.knobs.some((x) => x.path === k.path), (cx) => this.toggleKnob(cx, k), k.current != null ? `now ${k.current}` : null)))
        : div().text_size(12.5).text_color(c.grey500).child("This pipeline has no numeric settings to try.")))
      .children(this.knobs.map((k) => h_flex().gap(10).items_center()
        .child(div().w(160).flex_none().text_size(12.5).font_family(MONO).text_color(c.navy).child(k.path))
        .child(div().id(`sb-v-${k.path}`).flex_1().h(30).flex().items_center().px(10).border_1().border_color(c.grey300).cursor_text()
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.states[k.path]).w_full().text_size(12.5).text_color(c.navy)))
        .child(div().text_size(11.5).text_color(c.grey500).child("values, or min:max:steps"))))
      .child(h_flex().gap(16)
        .child(v_flex().flex_1().min_w(0).child(row("Queries", h_flex().flex_wrap().gap(6).children(names("queries").map((n) => this.pill(c, `sb-q-${n}`, n, this.queries === n, () => { this.queries = n; }))))))
        .child(v_flex().flex_1().min_w(0).child(row("Judgments", h_flex().flex_wrap().gap(6).children(names("judgments").map((n) => this.pill(c, `sb-j-${n}`, n, this.judgments === n, () => { this.judgments = n; })))))))
      .child(h_flex().gap(10).items_center()
        .child(Button(c, "sb-run", this.busy === "sweep" ? (this.progress != null ? `Trying... ${Math.round(this.progress * 100)}%` : "Trying...") : "Try these settings", (_e, cx) => this.sweep(cx), { kind: "primary", disabled: Boolean(this.busy) }))
        .child(div().text_size(12).text_color(c.grey500).child("A quick pass on a sample first, then the best settings on every query.")));
  }

  // One knob: mean nDCG against the value, each point with its interval, the current value marked.
  line(c, cells, path, current, baseline) {
    const pts = cells.map((x) => ({ x: Number(x.values[path]), y: x.mean, lo: baseline + (x.ci?.[0] ?? 0), hi: baseline + (x.ci?.[1] ?? 0), cell: x })).sort((a, b) => a.x - b.x);
    const ys = pts.flatMap((p) => [p.y, p.lo, p.hi]).concat([baseline]);
    const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const xs = pts.map((p) => p.x).concat([Number(current)]).filter(Number.isFinite);
    const xlo = Math.min(...xs), xspan = Math.max(...xs) - xlo || 1;
    const X = (v) => ((v - xlo) / xspan) * 88 + 6, Y = (v) => (1 - (v - lo) / span) * 80 + 10;
    const paint = globalThis.window?.paint_path;
    let path_ = null;
    if (paint && pts.length > 1) { let b = PathBuilder.stroke(2); pts.forEach((p, i) => { b = i ? b.line_to(`${X(p.x)}%`, `${Y(p.y)}%`) : b.move_to(`${X(p.x)}%`, `${Y(p.y)}%`); }); path_ = paint(b.build(), c.blue); }
    return v_flex().gap(4)
      .child(div().id("sb-line").w_full().h(240).relative().border_1().border_color(c.rule)
        .child(div().absolute().left(`${X(Number(current))}%`).top(0).bottom(0).w(1).bg(c.grey300))
        .child(div().absolute().left(`${X(Number(current))}%`).top(2).ml(4).text_size(10.5).text_color(c.grey500).child(`now ${current}`))
        .child(div().absolute().left(0).right(0).top(`${Y(baseline)}%`).h(1).bg(c.rule))
        .when(Boolean(path_), (e) => e.child(div().absolute().inset_0().child(path_)))
        .children(pts.map((p, i) => div().id(`sb-pt-${i}`).absolute().left(`${X(p.x)}%`).top(`${Y(p.hi)}%`).h(`${Math.max(0.5, Y(p.lo) - Y(p.hi))}%`).w(2).ml(-1).bg(c.grey500)))
        .children(pts.map((p, i) => div().id(`sb-dot-${i}`).absolute().left(`${X(p.x)}%`).top(`${Y(p.y)}%`).size(10).ml(-5).mt(-5).rounded_full().cursor_pointer()
          .bg(this.cell && key(this.cell.values) === key(p.cell.values) ? c.navy : c.blue).tooltip(`${path} ${p.x}: nDCG ${num(p.y, 4)}, ${p.cell.verdict}`)
          .on_click((_e, cx) => { this.cell = p.cell; this.ripple = null; this.promoted = null; cx.notify(); })))
        .children(pts.map((p, i) => div().absolute().left(`${X(p.x)}%`).bottom(2).ml(-12).w(24).text_center().text_size(10.5).font_family(MONO).text_color(c.grey500).child(String(p.x)))))
      .child(div().text_size(11.5).text_color(c.grey500).child(`Mean nDCG by ${path}. Whiskers: the interval of the change against today's ${num(baseline, 4)}.`));
  }

  // Two knobs: a heat grid, green better and red worse than today, today's cell outlined.
  grid(c, cells, [a, b], current) {
    const av = [...new Set(cells.map((x) => x.values[a]))].sort((x, y) => x - y);
    const bv = [...new Set(cells.map((x) => x.values[b]))].sort((x, y) => x - y);
    const at = (x, y) => cells.find((k) => k.values[a] === x && k.values[b] === y);
    const maxd = Math.max(1e-9, ...cells.map((k) => Math.abs(k.delta ?? 0)));
    const colour = (k) => (!k ? c.surface2 : k.verdict === "noise" ? c.surface2 : k.delta > 0 ? c.goodLight : c.failLight);
    return v_flex().gap(4)
      .child(h_flex().gap(4).child(div().w(90)).children(av.map((x) => div().flex_1().text_center().text_size(11).font_family(MONO).text_color(c.grey500).child(`${a} ${x}`))))
      .children(bv.map((y) => h_flex().gap(4)
        .child(div().w(90).flex_none().text_size(11).font_family(MONO).text_color(c.grey500).child(`${b} ${y}`))
        .children(av.map((x) => {
          const k = at(x, y);
          const now = current?.[a] === x && current?.[b] === y;
          const picked = k && this.cell && key(this.cell.values) === key(k.values);
          return v_flex().id(`sb-cell-${x}-${y}`).flex_1().h(46).items_center().justify_center().cursor_pointer().bg(colour(k))
            .border_1().border_color(picked ? c.navy : now ? c.blue : c.rule)
            .when(Boolean(k), (e) => e.on_click((_e, cx) => { this.cell = k; this.ripple = null; this.promoted = null; cx.notify(); }))
            .child(div().text_size(12.5).font_family(MONO).text_color(c.navy).child(k ? num(k.mean, 4) : ""))
            .child(div().text_size(10.5).font_family(MONO).text_color(c.grey500).child(k ? `${k.delta >= 0 ? "+" : ""}${num(k.delta, 4)}${now ? ", now" : ""}` : ""));
        }))))
      .child(div().text_size(11.5).text_color(c.grey500).child(`Mean nDCG for each pair. Green beat today's setting, red lost to it; grey can't be told apart. Darker means more (largest change ${num(maxd, 4)}).`));
  }

  cellLine(c, k, i) {
    const flips = k.slice_flips ?? [];
    const picked = this.cell && key(this.cell.values) === key(k.values);
    return v_flex().id(`sb-full-${i}`).gap(3).p(10).cursor_pointer().border_1().border_color(picked ? c.blue : c.rule).bg(picked ? c.blueLight : c.surface)
      .on_click((_e, cx) => { this.cell = k; this.ripple = null; this.promoted = null; cx.notify(); })
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_family(MONO).text_color(c.navy).child(Object.entries(k.values).map(([p, v]) => `${p} ${v}`).join(", ")))
        .child(div().text_size(12).font_family(MONO).text_color(c.navy).child(num(k.mean, 4)))
        .child(div().w(70).text_right().text_size(12).font_family(MONO).text_color(k.delta > 0 ? c.good : k.delta < 0 ? c.fail : c.grey500).child(`${k.delta >= 0 ? "+" : ""}${num(k.delta, 4)}`))
        .child(Badge(c, k.verdict ?? "", TONE[k.verdict] ?? "neutral")))
      .when(flips.length > 0, (e) => e.child(div().text_size(11.5).text_color(c.grey900)
        .child(`Slices that move: ${flips.map((f) => `${f.slice} ${f.direction} ${num(Math.abs(f.delta), 3)} on ${int(f.n)} queries`).join("; ")}`)));
  }

  results(c) {
    const r = this.result;
    const paths = Object.keys(r.knobs ?? {});
    const sample = r.sample?.cells ?? [];
    return v_flex().gap(14)
      // Offline, and says so where it can't be missed.
      .child(h_flex().id("sb-note").gap(8).items_center().p(10).bg(c.warnLight)
        .child(svg(icon("triangle-alert")).size(14).text_color(c.warn))
        .child(div().flex_1().text_size(12.5).text_color(c.navy).child(r.note ?? "Offline, from grades: it narrows what's worth an A/B test."))
        .child(Badge(c, `grades: ${String(r.evidence ?? "").toLowerCase()}`, "warn")))
      .child(div().text_size(12.5).text_color(c.grey900).child(`Today: ${Object.entries(r.current ?? {}).map(([p, v]) => `${p} ${v}`).join(", ")}, nDCG ${num(r.baseline_mean, 4)} (${r.metric}).`))
      .child(v_flex().gap(8)
        .child(div().text_size(13).font_semibold().child(`Quick pass, ${int(r.sample?.n)} queries`))
        .child(paths.length === 1 ? this.line(c, sample, paths[0], r.current?.[paths[0]], r.baseline_mean)
          : paths.length === 2 ? this.grid(c, sample, paths, r.current)
          : v_flex().gap(4).children([...sample].sort((a, b) => b.mean - a.mean).map((k, i) => this.cellLine(c, k, `s${i}`)))))
      .child(v_flex().gap(8)
        .child(div().text_size(13).font_semibold().child(`Full pass, every query (${int(r.full?.n)})`))
        .children((r.full?.cells ?? []).map((k, i) => this.cellLine(c, k, i))));
  }

  actions(c) {
    const k = this.cell;
    if (!k) return div().text_size(12.5).text_color(c.grey500).child("Pick a setting above to see its ripple or make it the next version.");
    const rp = this.ripple, pr = this.promoted;
    const list = (title, items, line) => v_flex().gap(2).child(div().text_size(11.5).font_semibold().text_color(c.grey500).child(title.toUpperCase()))
      .children(items.length ? items.slice(0, 8).map((x, i) => div().id(`sb-${title}-${i}`).text_size(12.5).text_color(c.navy).child(line(x))) : [div().text_size(12).text_color(c.grey500).child("None.")]);
    return v_flex().gap(10).p(14).border_1().border_color(c.blue)
      .child(div().text_size(13).font_semibold().child(Object.entries(k.values).map(([p, v]) => `${p} ${v}`).join(", ")))
      .child(h_flex().gap(8).items_center()
        .child(div().id("sb-must").flex_1().h(30).flex().items_center().px(10).border_1().border_color(c.grey300).cursor_text()
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.mustNot).w_full().text_size(12.5).text_color(c.navy)))
        .child(Button(c, "sb-ripple", this.busy === "ripple" ? "Working it out..." : "See the ripple", (_e, cx) => this.seeRipple(cx), { small: true, disabled: Boolean(this.busy) }))
        .child(Button(c, "sb-promote", this.busy === "promote" ? "Making it..." : "Make this the next version", (_e, cx) => this.promote(cx), { kind: "primary", small: true, disabled: Boolean(this.busy) || Boolean(pr) })))
      .child(div().text_size(11.5).text_color(c.grey500).child("Queries that must not break, separated by commas (optional)."))
      .when(Boolean(rp), (e) => e
        .child(div().text_size(12.5).text_color(c.navy).child(rp.summary ?? ""))
        .child(h_flex().gap(16).items_start()
          .child(v_flex().flex_1().min_w(0).child(list("Moved most", rp.moved_most ?? [], (x) => `${x.query}: ${Math.round((x.top10_changed ?? 0) * 100)}% of the top 10 changed`)))
          .child(v_flex().flex_1().min_w(0).child(list("Regressions", rp.regressions ?? [], (x) => `${x.query}: nDCG ${num(x.ndcg_before, 3)} to ${num(x.ndcg_after, 3)}`)))
          .child(v_flex().flex_1().min_w(0).child(list("Must not break", rp.must_not_break ?? [], (x) => `${x.query ?? x}: ${(rp.broken ?? []).some((b) => (b.query ?? b) === (x.query ?? x)) ? "breaks" : "holds"}`)))))
      .when(Boolean(pr), (e) => e.child(h_flex().gap(10).items_center()
        .child(div().flex_1().text_size(12.5).text_color(c.good).child(`Made ${pr.pipeline ?? pr.ref ?? "the next version"}. Nothing went live.`))
        .child(Button(c, "sb-backtest", "Run a backtest against the current version", () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "backtest", values: { a: `${this.pipeline}@v${this.version}`, b: pr.pipeline ?? pr.ref } } }), { kind: "primary", small: true }))));
  }

  render() {
    const c = palette(isDark(theme()));
    return v_flex().id("sandbox").size_full().bg(c.surface).rounded(RADIUS).text_color(c.navy).font_family(FONT)
      .child(h_flex().h(56).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
        .child(div().size(28).flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon("flask-conical")).size(15)))
        .child(v_flex().flex_1().min_w(0)
          .child(div().text_size(15).font_semibold().child("What if..."))
          .child(div().text_size(12).text_color(c.grey500).child("Try other settings of a pipeline on your graded queries, before an A/B test.")))
        .child(div().id("sb-close").text_size(12.5).text_color(c.grey500).cursor_pointer().hover((s) => s.text_color(c.navy))
          .on_click(() => closeOverlay(props()?.overlay)).child("Close")))
      .child(div().id("sb-body").flex_1().min_h(0).overflow_y_scroll().child(v_flex().px(20).py(16).gap(16)
        .child(this.form(c))
        .when(Boolean(this.error), (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.error)))
        .when(this.busy === "sweep" && !this.result, (e) => e.child(Skeleton(c, 6)))
        .when(Boolean(this.result), (e) => e.child(this.results(c)).child(this.actions(c)))));
  }
}
