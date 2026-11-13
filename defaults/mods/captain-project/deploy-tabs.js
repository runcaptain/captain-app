// Pieces of the deployed model's screen that sit behind "More" (the log, the settings, the running copy), the bars
// drawn for requests over time, and the list of deployments across projects. Each takes the Ship object (ship.js), which
// holds what was read from the services, and the colours.
//
// Three states are said apart everywhere: stopped, idle, and handling requests. An endpoint that is up with nothing
// to do is fine, and is never drawn as a fault.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { MONO } from "./kit/tokens.js";
import { Badge, Button } from "./kit/ui.js";
import { icon } from "./kit/icons.js";

const short = (ref) => String(ref ?? "").replace(/^.*\//, "");
const clock = (ts) => String(ts ?? "").slice(11, 23);
const when = (ts) => String(ts ?? "").slice(0, 16).replace("T", " ");
const ms = (x) => (x === null || x === undefined ? "" : `${Number(x) >= 10 ? Number(x).toFixed(0) : Number(x).toFixed(1)} ms`);
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
    .child(h_flex().text_size(12.5).text_color(c.grey500)
      .child(div().child(from)).child(div().flex_1())
      .child(div().id(`${id}-max`).child(hi > 0 ? `highest ${unit === "ms" ? ms(hi) : hi.toLocaleString("en-US")}` : "nothing in this range"))
      .child(div().flex_1()).child(div().child(to)));
}

/** The small refresh mark the Deployments screen uses: an icon with a name for screen readers and a tooltip, for
 *  someone who wants the newest now. The screen keeps itself current: this is never the way it stays so. */
export function RefreshMark(c, id, busy, onClick) {
  return div().id(id).size(24).flex().flex_none().items_center().justify_center().text_color(busy ? c.blue : c.grey500)
    .accessibility_label("Refresh").tooltip(busy ? "Refreshing" : "Refresh")
    .when(!busy, (e) => e.cursor_pointer().hover((x) => x.text_color(c.navy)).on_click(onClick))
    .child(svg(icon("refresh-cw")).size(14));
}
const shell = (c, id) => v_flex().id(id).w_full().p(12).gap(8).bg(c.surface).border_1().border_color(c.rule);
const lead = (c, id, text) => div().id(id).text_size(12.5).line_height("18px").text_color(c.grey500).child(text);

/** Each running copy: where, version, when it started, how long it took to be ready, what it handled, its state. */
export function CopiesTab(ship, c) {
  const d = ship.copiesD;
  const cell = (label, value, w = 150) => v_flex().w(w).flex_none().gap(1)
    .child(div().text_size(12.5).text_color(c.grey500).child(label)).child(div().text_size(12.5).text_color(c.navy).truncate().child(String(value ?? "")));
  return shell(c, "dep-copies")
    .child(lead(c, "dep-copies-text", d ? d.text : "Looking at the endpoint..."))
    .children((d?.rows ?? []).map((r, i) => v_flex().id(`dep-copy-${i}`).py(8).gap(8).border_t(1).border_color(c.rule)
      .child(h_flex().gap(10).items_center().flex_wrap()
        .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(`Copy ${r.copy}`))
        .child(Badge(c, r.status, TONE[r.state] ?? "neutral"))
        .child(div().flex_1().min_w(0))
        .when(Boolean(r.can_stop) && !ship.busy, (e) => e.child(Button(c, `dep-copy-${i}-stop`, "Stop", (_e, cx) => { ship.stopAsk = true; cx.notify(); }, { small: true }))))
      // Idle is said in a full sentence, in plain colour: a quiet, ready endpoint is not a problem.
      .child(div().id(`dep-copy-${i}-state`).text_size(12.5).line_height("18px").text_color(c.grey900).child(r.text))
      .child(h_flex().gap(14).flex_wrap()
        .child(cell("Where", r.where, 220)).child(cell("Machine", r.machine)).child(cell("Version", short(r.model_version), 200))
        .child(cell("Created", when(r.created_at))).child(cell("Started", when(r.started_at)))
        .child(cell("Startup", r.ready_s === null || r.ready_s === undefined ? "" : `${r.ready_s} s`, 90))
        .child(cell("Requests handled", r.requests === null || r.requests === undefined ? "" : Number(r.requests).toLocaleString("en-US"), 120))
        .child(cell("Errors", r.errors ?? "", 70)).child(cell("Peak memory", r.memory_mb ? `${Math.round(r.memory_mb)} MB` : "", 100)))
      .when(ship.stopAsk, (e) => e.child(ship.stopBox(c)))))
    .when(Boolean(d?.warning), (e) => e.child(div().id("dep-copies-warning").text_size(12.5).text_color(c.warn).child(d.warning)));
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
      .when(ship.running(), (e) => e.child(RefreshMark(c, "dep-logs-refresh", ship.refreshing === "logs", (_e, cx) => ship.refreshNow(cx, "logs"))))
      .when(Boolean(ship.logBox.value().trim()), (e) => e.child(Button(c, "dep-logs-clear", "Show all", (_e, cx) => { ship.logBox.set_value(""); ship.logsD = null; ship.loadTab(cx); cx.notify(); }, { small: true })))
      .child(div().flex_1().min_w(0))
      .when(Boolean(d?.streaming), (e) => e.child(h_flex().id("dep-logs-streaming").gap(6).items_center()
        .child(div().size(7).rounded_full().bg(c.good)).child(div().text_size(12.5).text_color(c.good).child("Streaming")))))
    .child(lead(c, "dep-logs-text", d ? d.text : "Reading the log..."))
    .when(Boolean(d?.rows?.length), (e) => e.child(v_flex().id("dep-logs-lines").w_full().py(4).bg(c.surface2)
      .children(d.rows.map((x, i) => h_flex().id(`dep-log-${i}`).px(8).py(1).gap(10).items_start()
        .child(div().w(92).flex_none().font_family(MONO).text_size(12.5).text_color(c.grey500).child(clock(x.ts)))
        .child(div().flex_1().min_w(0).font_family(MONO).text_size(12.5).line_height("18px").text_color(tone(x.level)).child(x.text))))))
    .when(Boolean(d?.note), (e) => e.child(div().id("dep-logs-note").text_size(12.5).text_color(c.grey500).child(d.note)));
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
        .child(div().w(210).flex_none().text_size(12.5).text_color(c.grey500).child(label))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.grey900).child(String(value)))))
      .when(g.edit === "idle" && ship.setEdit, (e) => e.child(v_flex().id("dep-set-idle").mt(4).p(8).gap(6).bg(c.surface2).border_1().border_color(c.rule)
        .child(div().text_size(12.5).text_color(c.grey900).child("Stop the endpoint after this long with no requests. It takes effect in a few seconds, with no restart."))
        .child(h_flex().gap(6).flex_wrap().items_center()
          .children((d.idle_choices ?? []).map((m) => div().id(`dep-set-idle-${m}`).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
            .border_color(d.idle_minutes === m ? c.blue : c.grey300).bg(d.idle_minutes === m ? c.blueLight : c.surface).text_color(d.idle_minutes === m ? c.blue : c.navy)
            .on_click((_e, cx) => ship.saveIdle(cx, m)).child(span(m))))
          .child(Button(c, "dep-set-done", "Done", (_e, cx) => { ship.setEdit = false; cx.notify(); }, { small: true })))))))
    .when(Boolean(ship.setNote), (e) => e.child(div().id("dep-set-note").text_size(12.5).text_color(ship.setNote.tone === "fail" ? c.fail : c.good).child(ship.setNote.text)));
}

/** What the deployed model accepts before a real site sends it traffic: its key, a request size and rate limit, and
 *  a time budget with a fallback answer. Each sentence is the services' own. Each change takes effect in seconds. */
export function ProtectBox(ship, c) {
  const d = ship.protD;
  if (!d) return shell(c, "dep-protect").child(lead(c, "dep-protect-text", "Reading what it accepts..."));
  const save = (body) => (_e, cx) => ship.saveProtect(cx, body);
  const chip = (id, label, on, click) => div().id(id).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
    .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).on_click(click).child(label);
  const part = (id, title, text, chips) => v_flex().id(id).py(8).gap(4).border_t(1).border_color(c.rule)
    .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(title))
    .child(div().id(`${id}-text`).text_size(12.5).line_height("18px").text_color(c.grey900).child(text))
    .child(h_flex().gap(6).flex_wrap().items_center().children(chips));
  const lim = d.limits ?? {}, bud = d.budget ?? {}, key = d.key ?? {};
  const asking = ship.keyAsk;
  return shell(c, "dep-protect")
    .child(lead(c, "dep-protect-text", "What it accepts from a site. Each change takes effect in a few seconds, with no restart."))
    .child(v_flex().id("dep-key").py(8).gap(4).border_t(1).border_color(c.rule)
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child("Key"))
      .child(div().id("dep-key-text").text_size(12.5).line_height("18px").text_color(c.grey900).child(key.text ?? ""))
      .when(!key.can_rotate && Boolean(key.why_not), (e) => e.child(div().id("dep-key-why").text_size(12.5).line_height("18px").text_color(c.grey500).child(key.why_not)))
      .when(Boolean(key.can_rotate) && !asking, (e) => e.child(h_flex().gap(6)
        .child(Button(c, "dep-key-copy", "Copy the key", (_e, cx) => ship.copyKey(cx), { small: true }))
        .child(Button(c, "dep-key-change", "Change the key", (_e, cx) => { ship.keyAsk = { grace: 60 }; cx.notify(); }, { small: true }))))
      .when(Boolean(asking), (e) => e.child(v_flex().id("dep-key-ask").mt(4).p(8).gap(6).bg(c.surface2).border_1().border_color(c.rule)
        .child(div().text_size(12.5).line_height("18px").text_color(c.grey900).child("A new key is made. How long should the old one keep working, so your site can switch?"))
        .child(h_flex().gap(6).flex_wrap().items_center()
          .children([[60, "1 hour"], [1440, "1 day"], [10080, "1 week"], [0, "Stop it now"]].map(([m, label]) => chip(`dep-key-grace-${m}`, label, asking.grace === m, (_e, cx) => { ship.keyAsk = { grace: m }; cx.notify(); }))))
        .child(h_flex().gap(6)
          .child(Button(c, "dep-key-yes", "Change the key", (_e, cx) => ship.rotateKey(cx, asking.grace), { small: true }))
          .child(Button(c, "dep-key-no", "Not now", (_e, cx) => { ship.keyAsk = null; cx.notify(); }, { small: true }))))))
    .child(part("dep-size", "Request size", d.size_text ?? "", [[64, "64 KB"], [256, "256 KB"], [1024, "1 MB"]].map(([kb, label]) => chip(`dep-size-${kb}`, label, lim.max_request_kb === kb, save({ max_request_kb: kb })))))
    .child(part("dep-rate", "Requests a second", d.rate_text ?? "", [[0, "No limit"], [10, "10"], [50, "50"], [200, "200"]].map(([n, label]) => chip(`dep-rate-${n}`, label, (lim.rate_per_s ?? 0) === n, save({ rate_per_s: n })))))
    .child(part("dep-budget", "Time budget", d.budget_text ?? "", [
      ...[[0, "None"], [100, "100 ms"], [200, "200 ms"], [500, "500 ms"]].map(([ms, label]) => chip(`dep-budget-${ms}`, label, (bud.ms ?? 0) === ms, save({ budget_ms: ms }))),
      ...(d.has_fallback ? [chip("dep-fallback", bud.fallback ? "Fallback answer: on" : "Fallback answer: off", Boolean(bud.fallback), save({ fallback: !bud.fallback }))] : [])]))
    .when(Boolean(ship.protNote), (e) => e.child(div().id("dep-protect-note").text_size(12.5).text_color(ship.protNote.tone === "fail" ? c.fail : c.good).child(ship.protNote.text)));
}

/** Every staged model across projects, as cards: who staged it and when, where it runs, its state, and a small
 *  chart of its last 24 hours with errors in red. Live and Stopped. Stopped ones stay findable. */
export function DeploymentsList(ship, c) {
  const which = ship.listWhich, d = ship.listD;
  const tab = (id, label) => div().id(`deps-tab-${id}`).py(6).text_size(12.5).font_medium().cursor_pointer().border_b(2)
    .border_color(which === id ? c.blue : "#00000000").text_color(which === id ? c.blue : c.grey900)
    .on_click((_e, cx) => { ship.listWhich = id; ship.listD = null; ship.loadList(cx); cx.notify(); }).child(label);
  return v_flex().id("deployments").w_full().gap(10)
    .child(h_flex().gap(18).border_b(1).border_color(c.rule).child(tab("live", "Live")).child(tab("stopped", "Stopped")))
    .child(lead(c, "deps-text", d ? d.text : "Looking across your projects..."))
    .child(h_flex().gap(10).flex_wrap().items_stretch().children((d?.rows ?? []).map((r, i) => v_flex().id(`deps-card-${i}`).w(340).flex_none().p(12).gap(6)
      .bg(c.surface).border_1().border_color(c.rule).cursor_pointer().hover((x) => x.border_color(c.blue))
      .on_click((_e, cx) => ship.openFromList(cx, r))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_size(12.5).font_semibold().text_color(c.navy).child(r.name))
        .child(Badge(c, r.status, TONE[r.state] ?? "neutral")))
      .child(div().text_size(12.5).text_color(c.grey900).truncate().child(short(r.model_version)))
      .child(div().id(`deps-card-${i}-where`).text_size(12.5).text_color(c.grey900).child(r.where))
      .child(div().id(`deps-card-${i}-since`).text_size(12.5).text_color(c.grey500).child(r.since_text))
      .child(Bars(c, `deps-card-${i}-bars`, r.series ?? [], { over: r.errors ?? [], height: 34, from: "24 h ago", to: "now" }))
      .child(div().id(`deps-card-${i}-line`).text_size(12.5).text_color(c.grey500)
        .child(`${r.line}${r.requests_24h ? ` ${Number(r.requests_24h).toLocaleString("en-US")} in 24 hours.` : ""}`)))));
}
