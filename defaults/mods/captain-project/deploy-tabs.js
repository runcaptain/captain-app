// The deployment page's Copies, Metrics, Logs, Settings and Cost tabs, the small chart in its header, the live card
// that follows every tab, and the list of deployments across projects. Each takes the Ship object (ship.js), which
// holds what was read from the services, and the colours.
//
// Three states are said apart everywhere: stopped, idle, and handling requests. An endpoint that is up with nothing
// to do is fine, and is never drawn as a fault.
import { div } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { MONO } from "./kit/tokens.js";
import { Badge, Button } from "./kit/ui.js";

const short = (ref) => String(ref ?? "").replace(/^.*\//, "");
const clock = (ts) => String(ts ?? "").slice(11, 23);
const when = (ts) => String(ts ?? "").slice(0, 16).replace("T", " ");
const ms = (x) => (x === null || x === undefined ? "" : `${Number(x) >= 10 ? Number(x).toFixed(0) : Number(x).toFixed(1)} ms`);
const RANGE_LABEL = { "15m": "15 min", "1h": "1 hour", "24h": "24 hours", "7d": "7 days" };
const TONE = { handling: "good", idle: "neutral", up: "good", stopped: "neutral" };

/** Bars over equal slices of time. `over` draws a red mark on a slice (errors), `marks` a thin line where a version
 *  was staged. The largest value is printed, so the height means something. */
export function Bars(c, id, values, { height = 56, color, over = [], marks = [], unit = "", from = "", to = "" } = {}) {
  const nums = values.map((v) => (v === null || v === undefined ? 0 : Number(v)));
  const hi = Math.max(0, ...nums);
  const n = Math.max(1, nums.length);
  const box = div().id(id).w_full().h(height).relative().bg(c.surface2);
  if (hi > 0) box.children(nums.map((v, i) => (v > 0 ? div().absolute().bottom(0).left(`${(i / n) * 100}%`).w(`${(1 / n) * 100 * 0.8}%`)
    .h(`${Math.max(4, (v / hi) * 92)}%`).bg(color ?? c.blue) : null)).filter(Boolean));
  box.children(over.map((v, i) => (v > 0 ? div().absolute().top(0).left(`${(i / n) * 100}%`).w(`${(1 / n) * 100 * 0.8}%`).h(4).bg(c.fail) : null)).filter(Boolean));
  box.children(marks.filter((m) => m !== null && m !== undefined).map((m) => div().absolute().top(0).bottom(0).left(`${((m + 0.5) / n) * 100}%`).w(1).bg(c.grey500)));
  return v_flex().w_full().gap(2)
    .child(box)
    .child(h_flex().text_size(10).font_family(MONO).text_color(c.grey500)
      .child(div().child(from)).child(div().flex_1())
      .child(div().id(`${id}-max`).child(hi > 0 ? `highest ${unit === "ms" ? ms(hi) : hi.toLocaleString("en-US")}` : "nothing in this range"))
      .child(div().flex_1()).child(div().child(to)));
}

/** The time range, shared by the header's chart and the Metrics tab. */
export function RangePicker(ship, c) {
  return h_flex().id("dep-range").gap(4).items_center().children(Object.entries(RANGE_LABEL).map(([id, label]) =>
    div().id(`dep-range-${id}`).h(22).px(8).flex().flex_none().items_center().cursor_pointer().text_size(11.5).border_1()
      .border_color(ship.range === id ? c.blue : c.grey300).bg(ship.range === id ? c.blueLight : c.surface).text_color(ship.range === id ? c.blue : c.grey900)
      .on_click((_e, cx) => { ship.range = id; ship.met = null; ship.loadTab(cx); cx.notify(); }).child(label)));
}

/** The header's chart: requests over the chosen range, errors in red, a line where each version was staged. */
export function HeaderChart(ship, c) {
  const m = ship.met;
  return v_flex().id("dep-chart").w_full().gap(4)
    .child(h_flex().gap(8).items_center().flex_wrap()
      .child(div().text_size(12).font_medium().text_color(c.navy).child("Requests"))
      .child(div().id("dep-chart-text").flex_1().min_w(160).text_size(12).text_color(c.grey500).child(m ? m.text : "Reading the request log..."))
      .child(RangePicker(ship, c)))
    .when(Boolean(m), (e) => e.child(Bars(c, "dep-chart-bars", m.buckets.map((b) => b.requests), {
      over: m.buckets.map((b) => b.errors), marks: m.markers.map((k) => k.slot), from: when(m.start).slice(5), to: "now" })));
}

/** The small card that follows every tab: which of the three states it is in, and the live counts. */
export function LiveCard(ship, c, id = "dep-live") {
  const d = ship.liveD;
  if (!d) return null;
  return h_flex().id(id).flex_none().gap(8).items_center().px(10).h(28).bg(c.surface).border_1().border_color(c.rule)
    .child(div().size(7).rounded_full().bg(d.state === "handling" ? c.good : d.state === "idle" ? c.blue : c.grey300))
    .child(div().id(`${id}-state`).text_size(12).font_medium().text_color(c.navy).child(d.label))
    .child(div().id(`${id}-line`).text_size(12).text_color(c.grey500).child(d.line));
}

const shell = (c, id) => v_flex().id(id).w_full().p(12).gap(8).bg(c.surface).border_1().border_color(c.rule);
const lead = (c, id, text) => div().id(id).text_size(12).line_height("17px").text_color(c.grey500).child(text);

/** Each running copy: where, version, when it started, how long it took to be ready, what it handled, its state. */
export function CopiesTab(ship, c) {
  const d = ship.copiesD;
  const cell = (label, value, w = 150) => v_flex().w(w).flex_none().gap(1)
    .child(div().text_size(11).text_color(c.grey500).child(label)).child(div().text_size(12.5).text_color(c.navy).truncate().child(String(value ?? "")));
  return shell(c, "dep-copies")
    .child(lead(c, "dep-copies-text", d ? d.text : "Looking at the endpoint..."))
    .children((d?.rows ?? []).map((r, i) => v_flex().id(`dep-copy-${i}`).py(8).gap(8).border_t(1).border_color(c.rule)
      .child(h_flex().gap(10).items_center().flex_wrap()
        .child(div().font_family(MONO).text_size(12.5).font_semibold().text_color(c.navy).child(r.copy))
        .child(Badge(c, r.status, TONE[r.state] ?? "neutral"))
        .child(div().flex_1().min_w(0))
        .when(Boolean(r.can_stop) && !ship.busy, (e) => e.child(Button(c, `dep-copy-${i}-stop`, "Stop", (_e, cx) => { ship.stopAsk = true; cx.notify(); }, { small: true }))))
      // Idle is said in a full sentence, in plain colour: a quiet, ready endpoint is not a problem.
      .child(div().id(`dep-copy-${i}-state`).text_size(12).line_height("17px").text_color(c.grey900).child(r.text))
      .child(h_flex().gap(14).flex_wrap()
        .child(cell("Where", r.where, 220)).child(cell("Machine", r.machine)).child(cell("Version", short(r.model_version), 200))
        .child(cell("Created", when(r.created_at))).child(cell("Started", when(r.started_at)))
        .child(cell("Startup", r.ready_s === null || r.ready_s === undefined ? "" : `${r.ready_s} s`, 90))
        .child(cell("Requests handled", r.requests === null || r.requests === undefined ? "" : Number(r.requests).toLocaleString("en-US"), 120))
        .child(cell("Errors", r.errors ?? "", 70)).child(cell("Peak memory", r.memory_mb ? `${Math.round(r.memory_mb)} MB` : "", 100)))
      .when(ship.stopAsk, (e) => e.child(ship.stopBox(c)))))
    .when(Boolean(d?.warning), (e) => e.child(div().id("dep-copies-warning").text_size(12).text_color(c.warn).child(d.warning)));
}

/** Requests, response time (median and 95th percentile, at the endpoint and as the site sees it), errors and copies
 *  over one shared time range, with a line where each version was staged. */
export function MetricsTab(ship, c) {
  const m = ship.met, s = m?.summary ?? {};
  const b = m?.buckets ?? [];
  const marks = (m?.markers ?? []).map((k) => k.slot);
  const span = { from: m ? when(m.start).slice(5) : "", to: "now", marks };
  const block = (id, title, line, chart) => v_flex().id(id).w_full().py(8).gap(4).border_t(1).border_color(c.rule)
    .child(h_flex().gap(8).items_center().flex_wrap()
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(title))
      .child(div().id(`${id}-line`).flex_1().min_w(200).text_size(12).line_height("17px").text_color(c.grey900).child(line)))
    .when(Boolean(chart), (e) => e.child(chart));
  const pair = (id, a, z, hasAny) => (hasAny ? h_flex().gap(12).items_start()
    .child(v_flex().flex_1().min_w(0).gap(2).child(div().text_size(11).text_color(c.grey500).child("Median")).child(Bars(c, `${id}-p50`, a, { ...span, unit: "ms", height: 44 })))
    .child(v_flex().flex_1().min_w(0).gap(2).child(div().text_size(11).text_color(c.grey500).child("95th percentile")).child(Bars(c, `${id}-p95`, z, { ...span, unit: "ms", height: 44, color: c.navy }))) : null);
  return shell(c, "dep-metrics")
    .child(h_flex().gap(8).items_center().flex_wrap()
      .child(lead(c, "dep-metrics-text", m ? m.text : "Reading the request log...").flex_1().min_w(200))
      .child(RangePicker(ship, c)))
    .when(Boolean(m), (e) => e
      .child(block("dep-m-requests", "Requests", `${Number(s.requests ?? 0).toLocaleString("en-US")} in ${m.range_text}.`,
        Bars(c, "dep-m-requests-bars", b.map((x) => x.requests), span)))
      .child(block("dep-m-endpoint", "Response time at the endpoint", s.endpoint_text ?? "",
        pair("dep-m-endpoint", b.map((x) => x.p50), b.map((x) => x.p95), s.p50 !== null && s.p50 !== undefined)))
      .child(block("dep-m-site", "Response time as your site sees it", s.site_text ?? "",
        pair("dep-m-site", b.map((x) => x.site_p50), b.map((x) => x.site_p95), Boolean(s.site_reported))))
      .child(block("dep-m-errors", "Errors", s.errors ? `${s.errors} in ${m.range_text}, ${(Number(s.error_rate) * 100).toFixed(1)}% of requests.` : `None in ${m.range_text}.`,
        s.errors ? Bars(c, "dep-m-errors-bars", b.map((x) => x.errors), { ...span, color: c.fail, height: 36 }) : null))
      .child(block("dep-m-copies", "Copies", `${s.copies} running now. Minimum ${s.copies_min}, maximum ${s.copies_max}: staging runs one copy and none when idle.`, null))
      .child(block("dep-m-memory", "Memory", s.memory_mb ? `${Math.round(s.memory_mb)} MB at its peak since this copy started.` : "Known while the endpoint is up.", null))
      .child(block("dep-m-waiting", "Requests waiting", `${s.waiting ?? 0} being answered right now.`, null))
      .when((m.markers ?? []).length > 0, (x) => x.child(div().id("dep-m-markers").pt(6).text_size(11.5).line_height("16px").text_color(c.grey500)
        .child(`The thin line marks where a version was staged: ${m.markers.map((k) => `${k.text} at ${when(k.at).slice(11)}`).join(". ")}.`))));
}

/** The endpoint's log: lines with millisecond times, a filter, and a marker while it is still growing. */
export function LogsTab(ship, c) {
  const d = ship.logsD;
  const tone = (lv) => (lv === "error" ? c.fail : lv === "warn" ? c.warn : c.grey900);
  return shell(c, "dep-logs")
    .child(h_flex().gap(8).items_center().flex_wrap()
      .child(h_flex().id("dep-logs-filter").w(260).h(28).px(8).items_center().border_1().border_color(c.grey300)
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(ship.logBox).w_full().text_size(12.5).text_color(c.navy)))
      .child(Button(c, "dep-logs-apply", "Filter", (_e, cx) => { ship.logsD = null; ship.loadTab(cx); cx.notify(); }, { small: true }))
      .when(Boolean(ship.logBox.value().trim()), (e) => e.child(Button(c, "dep-logs-clear", "Show all", (_e, cx) => { ship.logBox.set_value(""); ship.logsD = null; ship.loadTab(cx); cx.notify(); }, { small: true, kind: "quiet" })))
      .child(div().flex_1().min_w(0))
      .when(Boolean(d?.streaming), (e) => e.child(h_flex().id("dep-logs-streaming").gap(6).items_center()
        .child(div().size(7).rounded_full().bg(c.good)).child(div().text_size(12).text_color(c.good).child("Streaming")))))
    .child(lead(c, "dep-logs-text", d ? d.text : "Reading the log..."))
    .when(Boolean(d?.rows?.length), (e) => e.child(v_flex().id("dep-logs-lines").w_full().py(4).bg(c.surface2)
      .children(d.rows.map((x, i) => h_flex().id(`dep-log-${i}`).px(8).py(1).gap(10).items_start()
        .child(div().w(92).flex_none().font_family(MONO).text_size(11.5).text_color(c.grey500).child(clock(x.ts)))
        .child(div().flex_1().min_w(0).font_family(MONO).text_size(11.5).line_height("16px").text_color(tone(x.level)).child(x.text))))))
    .when(Boolean(d?.note), (e) => e.child(div().id("dep-logs-note").text_size(11.5).text_color(c.grey500).child(d.note)));
}

/** How the endpoint is set up, in groups, with the idle time editable while it runs. */
export function SettingsTab(ship, c) {
  const d = ship.setD;
  const span = (m) => (m >= 1440 ? `${m / 1440} day${m === 1440 ? "" : "s"}` : m >= 60 ? `${m / 60} h` : `${m} min`);
  return shell(c, "dep-settings")
    .child(lead(c, "dep-settings-text", d ? d.text : "Reading the settings..."))
    .children((d?.groups ?? []).map((g, i) => v_flex().id(`dep-set-${i}`).py(8).gap(4).border_t(1).border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().text_size(12.5).font_semibold().text_color(c.navy).child(g.title))
        .when(g.edit === "idle" && Boolean(d.can_edit) && !ship.setEdit, (e) => e.child(Button(c, "dep-set-edit", "Edit", (_e, cx) => { ship.setEdit = true; ship.setNote = null; cx.notify(); }, { small: true }))))
      .children(g.rows.map(([label, value], j) => h_flex().id(`dep-set-${i}-${j}`).gap(10).items_start()
        .child(div().w(210).flex_none().text_size(12).text_color(c.grey500).child(label))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("17px").text_color(c.grey900).child(String(value)))))
      .when(g.edit === "idle" && ship.setEdit, (e) => e.child(v_flex().id("dep-set-idle").mt(4).p(8).gap(6).bg(c.surface2).border_1().border_color(c.rule)
        .child(div().text_size(12).text_color(c.grey900).child("Stop the endpoint after this long with no requests. It takes effect in a few seconds, with no restart."))
        .child(h_flex().gap(6).flex_wrap().items_center()
          .children((d.idle_choices ?? []).map((m) => div().id(`dep-set-idle-${m}`).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12).border_1()
            .border_color(d.idle_minutes === m ? c.blue : c.grey300).bg(d.idle_minutes === m ? c.blueLight : c.surface).text_color(d.idle_minutes === m ? c.blue : c.navy)
            .on_click((_e, cx) => ship.saveIdle(cx, m)).child(span(m))))
          .child(Button(c, "dep-set-done", "Done", (_e, cx) => { ship.setEdit = false; cx.notify(); }, { small: true, kind: "quiet" })))))))
    .when(Boolean(ship.setNote), (e) => e.child(div().id("dep-set-note").text_size(12).text_color(ship.setNote.tone === "fail" ? c.fail : c.good).child(ship.setNote.text)));
}

/** What the deployment has cost: so far this month, the last 24 hours, and the rate while it is up, by machine. */
export function CostTab(ship, c) {
  const d = ship.costD;
  const fig = (id, label, value) => v_flex().id(id).w(190).flex_none().p(10).gap(2).bg(c.surface2)
    .child(div().text_size(11.5).text_color(c.grey500).child(label)).child(div().text_size(18).font_semibold().text_color(c.navy).child(value));
  return shell(c, "dep-cost")
    .child(lead(c, "dep-cost-text", d ? d.text : "Working out the cost..."))
    .when(Boolean(d) && !d.free && d.month_text !== undefined, (e) => e
      .child(h_flex().gap(10).flex_wrap()
        .child(fig("dep-cost-month", "So far this month", d.month_text))
        .child(fig("dep-cost-day", "Last 24 hours", d.day_text))
        .child(fig("dep-cost-rate", "While it is up", d.hourly_usd ? `$${Number(d.hourly_usd).toFixed(2)} an hour` : d.running ? "Not priced" : "Stopped")))
      .child(div().id("dep-cost-rate-text").text_size(12).text_color(c.grey900).child(d.rate_text))
      .when((d.rows ?? []).length > 0, (x) => x.child(v_flex().id("dep-cost-machines").pt(4).gap(2)
        .child(div().text_size(12.5).font_semibold().text_color(c.navy).child("By machine, this month"))
        .children(d.rows.map((r, i) => div().id(`dep-cost-machine-${i}`).text_size(12).text_color(c.grey900).child(r.text)))))
      .when(Boolean(d.estimate), (x) => x.child(div().id("dep-cost-estimate").text_size(12).text_color(c.grey500).child(`The estimate shown before it was staged: ${d.estimate}`)))
      .child(div().id("dep-cost-note").text_size(11.5).text_color(c.grey500).child(d.note)));
}

/** Every staged model across projects, as cards: who staged it and when, where it runs, its state, and a small
 *  chart of its last 24 hours with errors in red. Live and Stopped. Stopped ones stay findable. */
export function DeploymentsList(ship, c) {
  const which = ship.listWhich, d = ship.listD;
  const tab = (id, label) => div().id(`deps-tab-${id}`).py(6).text_size(13).font_medium().cursor_pointer().border_b(2)
    .border_color(which === id ? c.blue : "#00000000").text_color(which === id ? c.blue : c.grey900)
    .on_click((_e, cx) => { ship.listWhich = id; ship.listD = null; ship.loadList(cx); cx.notify(); }).child(label);
  return v_flex().id("deployments").w_full().gap(10)
    .child(h_flex().gap(18).border_b(1).border_color(c.rule).child(tab("live", "Live")).child(tab("stopped", "Stopped")))
    .child(lead(c, "deps-text", d ? d.text : "Looking across your projects..."))
    .child(h_flex().gap(10).flex_wrap().items_stretch().children((d?.rows ?? []).map((r, i) => v_flex().id(`deps-card-${i}`).w(340).flex_none().p(12).gap(6)
      .bg(c.surface).border_1().border_color(c.rule).cursor_pointer().hover((x) => x.border_color(c.blue))
      .on_click((_e, cx) => ship.openFromList(cx, r))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().text_color(c.navy).child(r.name))
        .child(Badge(c, r.status, TONE[r.state] ?? "neutral")))
      .child(div().font_family(MONO).text_size(12).text_color(c.grey900).truncate().child(short(r.model_version)))
      .child(div().id(`deps-card-${i}-where`).text_size(12).text_color(c.grey900).child(r.where))
      .child(div().id(`deps-card-${i}-since`).text_size(12).text_color(c.grey500).child(r.since_text))
      .child(Bars(c, `deps-card-${i}-bars`, r.series ?? [], { over: r.errors ?? [], height: 34, from: "24 h ago", to: "now" }))
      .child(div().id(`deps-card-${i}-line`).text_size(12).text_color(c.grey500)
        .child(`${r.line}${r.requests_24h ? ` ${Number(r.requests_24h).toLocaleString("en-US")} in 24 hours.` : ""}`)))));
}
