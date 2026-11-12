// CAPTAIN: one deployed model's page, opened from the Deployments list: its state as the cloud reports it, its
// address, traffic and machine charts over a chosen time range, what it has cost, the requests it answered, its
// log, and the earlier versions that can be put back. Not a View: the list owns it and draws it in its place.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { api } from "./kit/cst.js";
import { MONO } from "./kit/tokens.js";
import { Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { ago, money, ms, count, BarChart } from "./words.js";

const RANGES = [["15m", "15 minutes"], ["1h", "1 hour"], ["24h", "24 hours"], ["7d", "7 days"]];
const PARTS = [["traffic", "Traffic and cost"], ["requests", "Requests it answered"], ["log", "Its log"], ["versions", "Earlier versions"], ["settings", "Copies and machine"]];

export class ModelPage {
  constructor(row, part, close, demo = null) {
    this.demo = demo;      // the picture check's numbers for this page, in place of asking the services
    this.row = row;
    this.close = close;
    this.part = part ?? "traffic";
    this.range = "1h";
    this.n = null;         // numbers for the range
    this.requests = null;
    this.log = null;
    this.versions = null;
    this.scale = null;
    this.copied = false;
    this.note = null;
    this.busy = null;
  }

  url(path) {
    return `/api/projects/${encodeURIComponent(this.row.project)}${path}`;
  }

  load(cx) {
    if (this.demo) {
      Object.assign(this, { n: this.demo.numbers?.[this.range] ?? this.demo.numbers?.["1h"] ?? null, requests: this.demo.requests ?? { rows: [] },
        log: this.demo.log ?? { rows: [] }, versions: this.demo.versions ?? { rows: [] }, scale: this.demo.scale ?? null });
      return cx.notify();
    }
    if (!this.row.project) return;
    cx.spawn(async (cx) => {
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

  back(n, cx) {
    if (this.demo) return;
    this.busy = `Putting version ${n} back`;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(this.url(`/staging/versions/${n}/rollback`), { method: "POST", body: {}, timeout: 900 });
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
    const chart = (id, title, line, values, opts) => v_flex().gap(4)
      .child(h_flex().gap(8).items_baseline()
        .child(div().text_size(13).font_semibold().text_color(c.navy).child(title))
        .child(div().id(`${id}-line`).text_size(12.5).text_color(c.grey500).child(line)))
      .child(BarChart(c, id, values, opts));
    const has = (k) => b.some((x) => x[k] != null);
    return v_flex().gap(16)
      .child(h_flex().id("model-ranges").gap(6)
        .children(RANGES.map(([id, label]) => div().id(`model-range-${id}`).h(26).px(10).flex().items_center().text_size(12.5).cursor_pointer()
          .border_1().border_color(this.range === id ? c.navy : c.rule).text_color(this.range === id ? c.navy : c.grey500).when(this.range === id, (e) => e.font_semibold())
          .on_click((_e, cx) => { this.range = id; this.n = null; cx.notify(); this.load(cx); }).child(label))))
      // In a person's words: how many, how many failed, how fast. The range's name comes from the services.
      .child(div().id("model-traffic-text").text_size(13).line_height("19px").text_color(c.navy)
        .child(s.requests ? `${count(s.requests)} request${s.requests === 1 ? "" : "s"} in ${n.range_text ?? "this range"}, ${s.errors ? `${count(s.errors)} failed` : "none failed"}.`
          + (s.p50_ms != null ? ` A request typically takes ${ms(s.p50_ms)}.` : "") : `No requests in ${n.range_text ?? "this range"}.`))
      .child(chart("model-chart-requests", "Requests a minute", s.requests ? `${s.per_minute} on average, ${s.per_minute_peak} at the busiest` : "none in this range",
        b.map((x) => (x.requests ?? 0) / minutes), { format: (v) => `${Math.round(v * 10) / 10} a minute` }))
      // Drawn only when some failed: an empty chart says nothing the word "none" does not.
      .child(s.errors ? chart("model-chart-errors", "Requests that failed", `${count(s.errors)} of ${count(s.requests)}`,
        b.map((x) => x.errors ?? 0), { color: c.fail, format: (v) => count(Math.round(v)) })
        : h_flex().id("model-chart-errors").gap(8).items_baseline()
          .child(div().text_size(13).font_semibold().text_color(c.navy).child("Requests that failed"))
          .child(div().text_size(12.5).text_color(c.grey500).child("none")))
      .when(has("p95_ms"), (e) => e.child(chart("model-chart-time", "Response time at the endpoint", s.p50_ms != null ? `typically ${ms(s.p50_ms)}, 19 in 20 within ${ms(s.p95_ms)}` : "",
        b.map((x) => x.p95_ms), { format: (v) => ms(v) })))
      .when(has("cpu"), (e) => e.child(chart("model-chart-cpu", "Processor in use", `${s.cpu}% of one core on average, ${s.cpu_peak}% at the most`, b.map((x) => x.cpu), { format: (v) => `${Math.round(v)}%` })))
      .when(has("memory"), (e) => e.child(chart("model-chart-memory", "Memory in use", `${s.memory}% on average, ${s.memory_peak}% at the most`, b.map((x) => x.memory), { format: (v) => `${Math.round(v)}%` })))
      .when(has("copies"), (e) => e.child(chart("model-chart-copies", "Copies running", s.copies_now ? `${s.copies_now} now, ${s.copies_peak} at the most` : "", b.map((x) => x.copies), { format: (v) => String(Math.round(v)) })))
      .child(v_flex().id("model-cost").gap(3)
        .child(div().text_size(13).font_semibold().text_color(c.navy).child("What it costs"))
        .child(div().text_size(13).line_height("19px").text_color(c.navy)
          .child([cost.hourly_usd ? `${money(cost.hourly_usd)} an hour now` : "Nothing now", cost.today_usd != null ? `${money(cost.today_usd)} today` : null,
            cost.month_usd != null ? `${money(cost.month_usd)} this month` : null].filter(Boolean).join(", ") + "."))
        .child(div().text_size(12.5).line_height("18px").text_color(c.grey500).child(cost.text ?? "")))
      .child(div().id("model-source").text_size(12.5).line_height("18px").text_color(c.grey500).child([n.source_text, ...(n.not_read ?? [])].join(" ")));
  }

  list(c, id, rows, empty, line) {
    if (rows == null) return div().text_size(13).text_color(c.grey500).child("Reading...");
    if (!rows.length) return div().id(`${id}-none`).text_size(13).text_color(c.grey500).child(empty);
    return v_flex().id(id).children(rows.map((r, i) => line(r, i)));
  }

  body(c) {
    const r = this.row;
    if (this.part === "traffic") return this.traffic(c);
    if (this.part === "requests") {
      return this.list(c, "model-requests", this.requests?.rows, this.requests?.text || "It has answered no requests yet.", (q, i) => h_flex().id(`model-request-${i}`).gap(12).py(6).border_b(1).border_color(c.rule)
        .child(div().w(90).flex_none().text_size(12.5).text_color(c.grey500).child(ago(q.ts) ?? ""))
        .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(c.navy).child(String(q.query ?? "")))
        .child(div().w(150).flex_none().truncate().text_size(12.5).text_color(c.grey500).child(String(q.model_version ?? "")))
        .child(div().w(70).flex_none().text_size(12.5).text_color(c.grey500).child(q.ms != null ? ms(q.ms) : "")));
    }
    if (this.part === "log") {
      return this.list(c, "model-log", this.log?.rows, this.log?.note || "It has written nothing to its log yet.", (l, i) => h_flex().id(`model-log-${i}`).gap(12).py(3)
        .child(div().w(150).flex_none().text_size(12).text_color(c.grey500).child(String(l.ts ?? "").replace("T", " ").slice(0, 19)))
        .child(div().flex_1().text_size(12.5).line_height("18px").text_color(l.level === "error" ? c.fail : c.navy).child(String(l.text ?? ""))));
    }
    if (this.part === "versions") {
      return v_flex().gap(8)
        .child(div().text_size(12.5).line_height("18px").text_color(c.grey500).child("Every version put on this deployment, newest first. Putting one back replaces what is running now."))
        .child(this.list(c, "model-versions", this.versions?.rows, "This is the only version it has run.", (v) => h_flex().id(`model-version-${v.n}`).gap(12).py(8).items_center().border_b(1).border_color(c.rule)
          .child(div().w(84).flex_none().text_size(13).font_semibold().text_color(c.navy).child(`Version ${v.n}`))
          .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(c.navy).child(String(v.model_version ?? "")))
          .child(div().w(190).flex_none().truncate().text_size(12.5).text_color(c.grey500).child([v.by, ago(v.at)].filter(Boolean).join(", ")))
          .child(v.live ? div().w(170).flex_none().text_size(12.5).text_color(c.grey500).child("Running now")
            : Button(c, `model-back-${v.n}`, "Put this version back", (_e, cx) => this.back(v.n, cx), { small: true, disabled: Boolean(this.busy) || v.can_roll_back === false }))));
    }
    const sc = this.scale;
    return v_flex().id("model-settings").gap(8)
      .child(div().text_size(13).line_height("19px").text_color(c.navy).child(`Runs on ${r.where}.`))
      .child(div().id("model-copies").text_size(13).line_height("19px").text_color(c.navy).child(sc?.text ?? "Captain could not read how many copies it may run."))
      .child(div().text_size(12.5).line_height("18px").text_color(c.grey500).child("Change the copies, the machine or the idle limit from the project's page, under Deploy."));
  }

  render(c, _cx) {
    const r = this.row;
    const wrong = r.state === "failed" || r.state === "gone";
    const address = r.address ?? r.name;
    const head = h_flex().h(56).flex_none().px(24).gap(12).items_center().border_b(1).border_color(c.rule)
      .child(Button(c, "model-close", "All deployments", (_e, cx) => { this.close(); cx.notify(); }, { small: true }))
      .child(r.cloud === "local" ? svg(icon("monitor")).size(16).text_color(c.navy) : Brand(c, r.cloud, 16))
      .child(div().id("model-title").text_size(18).font_semibold().text_color(c.navy).child(r.project_name ?? r.name ?? ""))
      .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(c.grey500).child(r.model_version ?? ""));
    const top = v_flex().gap(6)
      .child(div().id("model-state").text_size(14).line_height("20px").text_color(wrong ? c.fail : c.navy).child(r.state_text))
      .child(div().text_size(12.5).line_height("18px").text_color(c.grey500)
        .child([r.where, r.by ? `Deployed by ${r.by}` : null, ago(r.since)].filter(Boolean).join(". ") + "."))
      .children((r.corrected ?? []).map((x) => div().text_size(12.5).line_height("18px").text_color(c.grey500).child(x)))
      .when(Boolean(address), (e) => e.child(h_flex().gap(8).items_center().pt(4)
        .child(div().text_size(12.5).text_color(c.grey500).child(r.address ? "Its address" : "Its name in the cloud"))
        // The one thing on this page a person copies: the code font is for that and nothing else.
        .child(div().id("model-address").min_w(0).truncate().text_size(12.5).font_family(MONO).text_color(c.navy).child(String(address)))
        .child(Button(c, "model-copy", this.copied ? "Copied" : "Copy", (_e, cx) => { cx.write_to_clipboard(String(address)); this.copied = true; cx.notify(); }, { small: true }))))
      .when(Boolean(this.busy), (e) => e.child(div().id("model-busy").text_size(13).text_color(c.navy).child(`${this.busy}...`)))
      .when(Boolean(this.note), (e) => e.child(div().id("model-note").text_size(13).line_height("19px").text_color(c.navy).child(this.note)));
    const parts = h_flex().id("model-parts").gap(6).flex_wrap()
      .children(PARTS.map(([id, label]) => div().id(`model-part-${id}`).h(28).px(11).flex().items_center().text_size(13).cursor_pointer()
        .border_1().border_color(this.part === id ? c.navy : c.rule).text_color(this.part === id ? c.navy : c.grey500).when(this.part === id, (e) => e.font_semibold())
        .on_click((_e, cx) => { this.part = id; cx.notify(); }).child(label)));
    return v_flex().id("model-page").size_full().child(head)
      .child(div().id("model-body").flex_1().min_h(0).overflow_y_scroll().px(24).py(18).child(v_flex().gap(18).child(top).child(parts).child(this.body(c))));
  }
}
