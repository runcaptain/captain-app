// CAPTAIN: one deployed model's page, opened from the Deployments list: its state as the cloud reports it, its
// address, traffic and machine charts over a chosen time range, what it has cost, the requests it answered, its
// log, and the earlier versions that can be put back. Not a View: the list owns it and draws it in its place.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import * as UI from "./kit/ui.js";
import { Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { ago, money, ms, count, BarChart } from "./words.js";

const RANGES = [["15m", "15 minutes"], ["1h", "1 hour"], ["24h", "24 hours"], ["7d", "7 days"]];
// Two submenus, never more: the numbers, and everything else about this model on one scrolling page.
const PARTS = [{ id: "traffic", label: "Traffic and cost" }, { id: "details", label: "Details" }];
// Why a version is put back. A roll back is recorded with its reason, so one is asked for.
// The same five, word for word, as the project page offers, so the record reads the same from either screen.
const REASONS = ["It gives worse results", "It is too slow", "It is failing requests", "It was deployed by mistake", "Another reason"];
// The kit's control for a view's submenus (it refuses a third). A copy of the kit from before it had one draws the same.
const Submenus = UI.Submenus ?? ((c, tabs, active, onSelect) => h_flex().flex_none().border_1().border_color(c.grey300).children(tabs.map((t, i) =>
  h_flex().id(`submenu-${t.id}`).h(28).px(12).gap(6).items_center().cursor_pointer().text_size(12.5)
    .when(i < tabs.length - 1, (e) => e.border_r(1).border_color(c.grey300))
    .bg(t.id === active ? c.navy : c.surface).text_color(t.id === active ? c.surface : c.grey900)
    .on_click((_e, cx) => onSelect(t.id, cx)).child(div().child(t.label)))));

export class ModelPage {
  constructor(row, part, close, demo = null) {
    this.demo = demo;      // the picture check's numbers for this page, in place of asking the services
    this.row = row;
    this.close = close;
    this.part = part === "details" ? "details" : "traffic";
    this.asking = null;    // the version a reason is being asked for
    this.range = "1h";
    this.n = null;         // numbers for the range
    this.requests = null;
    this.log = null;
    this.versions = null;
    this.scale = null;
    this.where = null;     // GET /deployed/where: the table of where it runs
    this.copiedFact = null;
    this.copied = false;
    this.note = null;
    this.busy = null;
  }

  url(path) {
    return `/api/projects/${encodeURIComponent(this.row.project)}${path}`;
  }

  load(cx) {
    if (this.demo) {
      Object.assign(this, { where: this.demo.where ?? null, n: this.demo.numbers?.[this.range] ?? this.demo.numbers?.["1h"] ?? null, requests: this.demo.requests ?? { rows: [] },
        log: this.demo.log ?? { rows: [] }, versions: this.demo.versions ?? { rows: [] }, scale: this.demo.scale ?? null });
      return cx.notify();
    }
    if (!this.row.project) return;
    cx.spawn(async (cx) => {
      // Where it runs first: it is read from the cloud's own describe call, and is what the page opens on.
      if (!this.where) {
        const w = await api(this.url(`/deployed/where?env=${encodeURIComponent(this.row.env ?? "staging")}`), { timeout: 60 });
        this.where = w.ok ? w.data : { facts: [], text: plainError(w.error, "Where it runs could not be read.") };
        cx.notify();
      }
      const n = await api(this.url(`/deployed/numbers?range=${this.range}&env=${encodeURIComponent(this.row.env ?? "staging")}`), { timeout: 60 });
      if (n.ok) this.n = n.data; else this.note = plainError(n.error, "The numbers could not be read.");
      cx.notify();
      const [rq, lg, vs, sc] = await Promise.all([api(this.url("/staging/requests?limit=20")), api(this.url("/staging/logs?limit=60")),
        api(this.url("/staging/versions")), api(this.url("/staging/scale"))]);
      this.requests = rq.ok ? rq.data : { rows: [], text: plainError(rq.error, "") };
      this.log = lg.ok ? lg.data : { rows: [], note: plainError(lg.error, "") };
      this.versions = vs.ok ? vs.data : { rows: [] };
      this.scale = sc.ok ? sc.data : null;
      cx.notify();
    });
  }

  back(n, reason, cx) {
    this.asking = null;
    if (this.demo) return cx.notify();
    this.busy = `Putting version ${n} back`;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(this.url(`/staging/versions/${n}/rollback`), { method: "POST", body: { reason }, timeout: 900 });
      this.busy = null;
      this.note = r.ok ? (r.data?.text ?? `Version ${n} is being put back.`) : plainError(r.error, "That version could not be put back.");
      this.load(cx);
    });
  }

  traffic(c) {
    const n = this.n;
    if (!n) return div().text_size(13).text_color(c.grey500).child("Reading the numbers...");
    const s = n.summary ?? {}, b = n.buckets ?? [], cost = n.cost ?? {};
    const minutes = (n.bucket_seconds ?? 60) / 60;
    // Two to a row where there is room, one where there is not: each takes at least 300 and shares what is left.
    const chart = (id, title, line, values, opts) => v_flex().min_w(300).flex_1().gap(4)
      .child(div().text_size(13).font_semibold().text_color(c.navy).child(title))
      .child(BarChart(c, id, values, opts))
      .child(div().id(`${id}-line`).text_size(12.5).line_height("18px").text_color(c.grey500).child(line));
    const has = (k) => b.some((x) => x[k] != null);
    const figure = (id, value, label) => v_flex().id(id).min_w(120).gap(1)
      .child(div().text_size(18).font_semibold().text_color(c.navy).child(value))
      .child(div().text_size(12.5).text_color(c.grey500).child(label));
    return v_flex().gap(16)
      // What it is costing comes first: it is one of the first things a person asks.
      .child(v_flex().id("model-cost").gap(6)
        .child(h_flex().gap(28).flex_wrap()
          .child(figure("model-cost-hour", cost.hourly_usd ? money(cost.hourly_usd) : "$0.00", "an hour now"))
          .when(cost.today_usd != null, (e) => e.child(figure("model-cost-today", money(cost.today_usd), "today")))
          .when(cost.month_usd != null, (e) => e.child(figure("model-cost-month", money(cost.month_usd), "this month"))))
        .child(div().text_size(12.5).line_height("18px").text_color(c.grey500).child(cost.text ?? "")))
      .child(h_flex().id("model-ranges").gap(6)
        .children(RANGES.map(([id, label]) => div().id(`model-range-${id}`).h(26).px(10).flex().items_center().text_size(12.5).cursor_pointer()
          .border_1().border_color(this.range === id ? c.navy : c.rule).text_color(this.range === id ? c.navy : c.grey500).when(this.range === id, (e) => e.font_semibold())
          .on_click((_e, cx) => { this.range = id; this.n = null; cx.notify(); this.load(cx); }).child(label))))
      // In a person's words: how many, how many failed, how fast. The range's name comes from the services.
      .child(div().id("model-traffic-text").text_size(13).line_height("19px").text_color(c.navy)
        .child(s.requests ? `${count(s.requests)} request${s.requests === 1 ? "" : "s"} in ${n.range_text ?? "this range"}, ${s.errors ? `${count(s.errors)} failed` : "none failed"}.`
          + (s.p50_ms != null ? ` A request typically takes ${ms(s.p50_ms)}.` : "") : `No requests in ${n.range_text ?? "this range"}.`))
      .child(h_flex().id("model-charts").gap(20).flex_wrap().items_start()
        .child(chart("model-chart-requests", "Requests a minute", s.requests ? `${s.per_minute} on average, ${s.per_minute_peak} at the busiest` : "None in this range",
          b.map((x) => (x.requests ?? 0) / minutes)))
        // Failures are drawn against the same scale whether or not there were any, so "none" is a flat line and not a missing chart.
        .child(chart("model-chart-errors", "Requests that failed", s.errors ? `${count(s.errors)} of ${count(s.requests)}` : "None",
          b.map((x) => x.errors ?? 0), { color: c.fail }))
        .when(has("p95_ms"), (e) => e.child(chart("model-chart-time", "Response time", s.p50_ms != null ? `Typically ${ms(s.p50_ms)}, 19 in 20 within ${ms(s.p95_ms)}` : "", b.map((x) => x.p95_ms))))
        .when(has("copies"), (e) => e.child(chart("model-chart-copies", "Copies running", s.copies_now ? `${s.copies_now} now, ${s.copies_peak} at the most` : "", b.map((x) => x.copies))))
        .when(has("cpu"), (e) => e.child(chart("model-chart-cpu", "Processor in use", `${s.cpu}% of one core on average, ${s.cpu_peak}% at the most`, b.map((x) => x.cpu))))
        .when(has("memory"), (e) => e.child(chart("model-chart-memory", "Memory in use", `${s.memory}% on average, ${s.memory_peak}% at the most`, b.map((x) => x.memory)))))
      // How many copies, in words: the cloud's count now and what it may run. There is no chart of it, because no
      // cloud keeps a history of it that can be relied on.
      .when(Boolean(s.copies_text), (e) => e.child(div().id("model-copies-line").text_size(13).line_height("19px").text_color(c.navy).child(s.copies_text)))
      .child(div().id("model-source").text_size(12.5).line_height("18px").text_color(c.grey500).child([n.source_text, ...(n.not_read ?? [])].join(" ")));
  }

  list(c, id, rows, empty, line) {
    if (rows == null) return div().text_size(13).text_color(c.grey500).child("Reading...");
    if (!rows.length) return div().id(`${id}-none`).text_size(13).text_color(c.grey500).child(empty);
    return v_flex().id(id).children(rows.map((r, i) => line(r, i)));
  }

  // Everything else about this model, as sections down one page, each under a one-line heading.
  details(c) {
    const r = this.row, sc = this.scale;
    const section = (id, title, body) => v_flex().id(id).gap(8).child(div().text_size(14).font_semibold().text_color(c.navy).child(title)).child(body);
    const versions = this.list(c, "model-versions", this.versions?.rows, "This is the only version it has run.", (v) => v_flex().id(`model-version-${v.n}`).gap(6).py(8).border_b(1).border_color(c.rule)
      .child(h_flex().gap(12).items_center()
        .child(div().w(84).flex_none().text_size(13).font_semibold().text_color(c.navy).child(`Version ${v.n}`))
        .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(c.navy).child(String(v.model_version ?? "")))
        .child(div().w(150).flex_none().truncate().text_size(12.5).text_color(c.grey500).child([v.by, ago(v.at)].filter(Boolean).join(", ")))
        .child(v.live ? div().w(160).flex_none().text_size(12.5).text_color(c.grey500).child("Running now")
          : Button(c, `model-back-${v.n}`, "Put this version back", (_e, cx) => { this.asking = this.asking === v.n ? null : v.n; cx.notify(); },
            { small: true, disabled: Boolean(this.busy) || v.can_roll_back === false })))
      .when(this.asking === v.n, (e) => e.child(v_flex().id("model-back-why").gap(6)
        .child(div().text_size(12.5).text_color(c.navy).child("Why? It is kept with the record of the change."))
        .child(h_flex().gap(6).flex_wrap().children(REASONS.map((why, i) => Button(c, `model-back-why-${i}`, why, (_e, cx) => this.back(v.n, why, cx), { small: true })))))));
    return v_flex().gap(22)
      .child(section("model-section-versions", "Earlier versions", versions))
      .child(section("model-section-requests", "Requests it answered", this.list(c, "model-requests", this.requests?.rows, this.requests?.text || "It has answered no requests yet.",
        (q, i) => h_flex().id(`model-request-${i}`).gap(12).py(6).border_b(1).border_color(c.rule)
          .child(div().w(90).flex_none().text_size(12.5).text_color(c.grey500).child(ago(q.ts) ?? ""))
          .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(c.navy).child(String(q.query ?? "")))
          .child(div().w(70).flex_none().text_size(12.5).text_color(c.grey500).child(q.ms != null ? ms(q.ms) : "")))))
      .child(section("model-section-log", "Its log", this.list(c, "model-log", this.log?.rows, this.log?.note || "It has written nothing to its log yet.",
        (l, i) => h_flex().id(`model-log-${i}`).gap(12).py(3)
          .child(div().w(140).flex_none().text_size(12).text_color(c.grey500).child(String(l.ts ?? "").replace("T", " ").slice(0, 19)))
          .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(l.level === "error" ? c.fail : c.navy).child(String(l.text ?? ""))))))
      .child(section("model-section-machine", "Copies and machine", v_flex().id("model-settings").gap(6)
        .child(div().text_size(13).line_height("19px").text_color(c.navy).child(`Runs on ${r.where}.`))
        .child(div().id("model-copies").text_size(13).line_height("19px").text_color(c.navy).child(sc?.text ?? "Captain could not read how many copies it may run."))
        .child(div().text_size(12.5).line_height("18px").text_color(c.grey500).child("Change the copies, the machine or the idle limit from the project's page, under Deploy."))));
  }

  body(c) {
    return this.part === "traffic" ? this.traffic(c) : this.details(c);
  }

  render(c, _cx) {
    const r = this.row;
    const wrong = r.state === "failed" || r.state === "gone";
    const head = h_flex().h(56).flex_none().px(24).gap(12).items_center().border_b(1).border_color(c.rule)
      .child(Button(c, "model-close", "All deployments", (_e, cx) => { this.close(); cx.notify(); }, { small: true }))
      .child(r.cloud === "local" ? svg(icon("monitor")).size(16).text_color(c.navy) : Brand(c, r.cloud, 16))
      .child(div().id("model-title").text_size(18).font_semibold().text_color(c.navy).child(r.project_name ?? r.name ?? ""))
      .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(c.grey500).child(r.model_label ?? r.model_version ?? ""));
    const top = v_flex().gap(6)
      .child(div().id("model-state").text_size(14).line_height("20px").text_color(wrong ? c.fail : c.navy).child(r.state_text))
      .children((r.corrected ?? []).map((x) => div().text_size(12.5).line_height("18px").text_color(c.grey500).child(x)))
      .when(Boolean(this.busy), (e) => e.child(div().id("model-busy").text_size(13).text_color(c.navy).child(`${this.busy}...`)))
      .when(Boolean(this.note), (e) => e.child(div().id("model-note").text_size(13).line_height("19px").text_color(c.navy).child(this.note)));
    const parts = h_flex().id("model-parts").child(Submenus(c, PARTS, this.part, (id, cx) => { this.part = id; cx.notify(); }));
    // Where it runs: first on the page, as a small table. Each value says whether it was read from the cloud or
    // comes from Captain's record. What a person copies (the address, a test request, the log's name) has Copy.
    const w = this.where;
    const where = v_flex().id("model-where").gap(2)
      .child(div().pb(4).text_size(14).font_semibold().text_color(c.navy).child("Where it runs"))
      .children((w?.facts ?? []).map((f) => h_flex().id(`model-where-${f.id}`).gap(12).py(5).items_start().border_b(1).border_color(c.rule)
        .child(div().w(150).flex_none().text_size(12.5).line_height("18px").text_color(c.grey500).child(f.label))
        .child(f.copy
          // The code font only for what is copied. A request of several lines shows its first, and Copy takes it all.
          ? div().id(`model-where-${f.id}-value`).flex_1().min_w(0).truncate().text_size(12.5).line_height("18px").font_family(MONO).text_color(c.navy)
            .child(String(f.value).split("\n").find((x) => x.trim() && !x.trim().startsWith("#")) ?? String(f.value))
          : div().id(`model-where-${f.id}-value`).flex_1().min_w(0).text_size(13).line_height("18px").text_color(c.navy).child(String(f.value)))
        .when(Boolean(f.copy), (e) => e.child(Button(c, `model-where-${f.id}-copy`, this.copiedFact === f.id ? "Copied" : "Copy",
          (_e, cx) => { cx.write_to_clipboard?.(String(f.value)); this.copiedFact = f.id; cx.notify(); }, { small: true })))
        .child(div().w(150).flex_none().text_size(12).line_height("18px").text_color(c.grey500).child(f.source_text ?? ""))))
      .child(div().id("model-where-text").pt(6).text_size(12.5).line_height("18px").text_color(c.grey500).child(w ? (w.text ?? "") : "Reading where it runs..."));
    return v_flex().id("model-page").size_full().child(head)
      .child(div().id("model-body").flex_1().min_h(0).overflow_y_scroll().px(24).py(18).child(v_flex().gap(18).child(top).child(where).child(parts).child(this.body(c))));
  }
}
