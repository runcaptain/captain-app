// CAPTAIN: one deployed model as one card. The same card is a row of the Deployments list and the header of the
// model's own page, so a model looks the same wherever it is met.
//
//   top      a dot for its state, the model's name large, the project small, and the one action that matters now
//   address  the address a site calls, in one field, with Copy, and "Copy a test request"
//   numbers  Traffic, Speed and Cost: one large figure each, with a small caption
//   footer   where it runs, which version, who deployed it and when
//
// Three text sizes: 17 for the name, 22 for a figure, 12.5 for everything else. One typeface: the code font is only
// for the address, which a person copies. A number that ticks in place has a fixed width, not another font.
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { MONO } from "./tokens.js";
import { Button } from "./ui.js";
import { icon } from "./icons.js";
import { Brand } from "./brands.js";
import { ago, money, ms, count, Spark, deployingWords, spanWords, middle, machineShort } from "./modelwords.js";

const SMALL = 12.5, NAME = 17, FIGURE = 22;

/** The colour of the dot: green running, grey asleep or stopped, amber on its way, red failed. */
export function dotColor(c, r) {
  if (r.state === "running") return c.good;
  if (r.state === "starting" || r.state === "deploying" || r.deploying) return c.warn;
  if (r.state === "failed" || r.state === "gone") return c.fail;
  return c.grey500;
}

/** One labelled figure: a caption above, the figure large, a line of small words under it. */
function Figure(c, id, label, figure, caption, extra) {
  return v_flex().id(id).flex_1().min_w(0).gap(2)
    .child(div().text_size(SMALL).text_color(c.grey500).child(label))
    .child(h_flex().gap(10).items_end()
      .child(div().id(`${id}-figure`).text_size(FIGURE).line_height("28px").font_medium().text_color(figure == null ? c.grey500 : c.navy).child(figure ?? "–"))
      .when(Boolean(extra), (e) => e.child(extra)))
    .child(div().id(`${id}-caption`).text_size(SMALL).line_height("18px").text_color(c.grey500).child(caption ?? ""));
}

/** A deploy under way: each step on its own line, with what it took or usually takes, counted on the page's clock. */
function Steps(c, r) {
  const g = r.deploying;
  const mark = (st) => st.status === "done" ? svg(icon("check")).size(14).text_color(c.good)
    : st.status === "running" ? div().size(8).rounded_full().bg(c.warn)
      : st.status === "failed" ? svg(icon("x")).size(14).text_color(c.fail)
        : div().size(8).rounded_full().border_1().border_color(c.grey300);
  const began = (st) => (st.started_at ? (Date.now() - new Date(st.started_at).getTime()) / 1000 : null);
  return v_flex().id(`dep-steps-${r.id}`).gap(8)
    .children((g.steps ?? []).filter((st) => st.status !== "skipped").map((st, i) => h_flex().id(`dep-step-${r.id}-${i}`).gap(12).items_center()
      .child(div().w(16).flex_none().flex().items_center().justify_center().child(mark(st)))
      .child(div().flex_1().min_w(0).truncate().text_size(SMALL).text_color(st.status === "pending" ? c.grey500 : c.navy)
        .when(st.status === "running", (e) => e.font_semibold()).child(st.title ?? ""))
      // A fixed width, so the seconds ticking in place do not push what is beside them about.
      .child(div().w(76).flex_none().text_size(SMALL).text_color(st.status === "running" ? c.navy : c.grey500)
        .child(st.status === "done" && st.seconds != null ? spanWords(st.seconds)
          : st.status === "running" && began(st) != null ? spanWords(began(st)) : ""))
      .child(div().w(190).flex_none().text_size(SMALL).text_color(c.grey500)
        .child(st.status !== "done" && st.typical ? `usually ${st.typical}` : ""))))
;  // The job's own last line is not shown: it repeats the sentence above and runs a few seconds behind it.
}

/**
 * The card. `r` is a row of GET /api/deployed. `o`:
 *   numbers   the model's numbers (GET .../deployed/numbers), or null
 *   busy      what is being done to it now, in words, or null
 *   action    {id, label, go} the one action that matters now, or null
 *   more      [{id, label, go}] the quiet actions beside it (Details, and on the page the rest)
 *   copied    which thing was just copied: "address", "request" or null
 *   onCopy    (what, e, cx) => void
 *   question  an element to show in place of the actions (a confirm), or null
 *   notes     small lines under the card's body (what Captain corrected, a new version on its way)
 */
export function ModelCard(c, r, o = {}) {
  const n = o.numbers, s = n?.summary ?? {};
  const found = r.link === "unlinked";
  const inCloud = r.in_cloud !== false;
  const wrong = (r.state === "failed" || r.state === "gone") && inCloud;
  const deploying = r.state === "deploying" && r.deploying;
  // The last hour, from the newest twelve five-minute counts of the day's numbers.
  const buckets = (n?.buckets ?? []).slice(-12);
  const hour = buckets.map((b) => b.requests ?? 0);
  const inHour = hour.reduce((a, b) => a + b, 0), failed = buckets.reduce((a, b) => a + (b.errors ?? 0), 0);
  const name = found ? r.name : (r.model_label ?? r.model_version ?? "Model version not known");
  // Something found in a cloud has no project: the line under its name says what it is, once.
  const small = found ? (r.state_text ?? `Found in your ${r.cloud_label} account.`) : (r.project_name ?? r.project ?? "");
  // A failure in plain words, with the one thing to do. What the cloud itself said is on the model's page
  // (`o.raw`), not on the card.
  const failedWords = /health|ping|did not pass|never answered/i.test(String(r.reason ?? r.state_text ?? ""))
    ? "It started but never answered. Deploy again, or open Details to see its log."
    : r.state === "gone" ? "It is no longer in the cloud. Deploy again to bring it back."
      : "It is not answering. Deploy again, or open Details to see what the cloud said.";
  const state = deploying ? deployingWords(r.deploying) : wrong && !o.raw ? failedWords
    : r.remembered_text ? `${r.state_text} ${r.remembered_text}` : r.state_text;
  // A short, ordinary state ("Running on 1 copy.") sits beside the project, under the name. Anything that needs
  // reading (it failed, it is being deployed, it was stopped and why) gets a line of its own.
  const plain = !wrong && !deploying && inCloud && !found && !o.busy && String(state ?? "").length <= 80;
  // The footer is one line that never wraps: where it runs at the left, who and when at the right. The account,
  // the machine's own name and how many copies it may run are on the model's page.
  const when = ago(r.since);
  const who = [r.by ? (r.by_name ?? r.by) : null, when].filter(Boolean).join(", ");
  const where = [r.cloud_label, r.cloud === "local" ? null : r.region, machineShort(r.machine_words ?? (r.cloud === "local" ? null : r.machine))].filter(Boolean).join(" · ");
  // While something is being done to it, no action is offered: what is being done is said on its own line.
  const actions = o.busy ? []
    : [...(o.more ?? []).map((a) => Button(c, a.id, a.label, a.go, { kind: "quiet" })),
      // Filled is only for an action that brings a model up. Stopping one is outlined: on a screen of running
      // models nothing is filled, because nothing needs doing.
      ...(o.action ? [Button(c, o.action.id, o.action.label, o.action.go, { kind: o.action.kind ?? "primary" })] : [])];
  // Figures are for a model that is answering. One that failed says what happened and what to do, and no more.
  const showNumbers = !found && !deploying && inCloud && !wrong && Boolean(n);
  return v_flex().id(`dep-row-${r.id}`).w_full().bg(c.surface).border_1().border_color(wrong ? c.fail : c.rule).px(20).py(14).gap(10)
    // Its state, its name, and the one action.
    .child(h_flex().gap(12).items_center()
      .child(div().id(`dep-dot-${r.id}`).size(10).flex_none().rounded_full().bg(o.busy ? c.warn : dotColor(c, r)))
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(div().id(`dep-model-${r.id}`).truncate().text_size(NAME).line_height("22px").font_semibold().text_color(c.navy).child(name))
        .child(h_flex().gap(6).min_w(0).text_size(SMALL).text_color(c.grey500)
          .child(div().id(`dep-name-${r.id}`).flex_none().child(small))
          .when(plain && Boolean(state), (e) => e.child(div().flex_none().child("·")).child(div().id(`dep-state-${r.id}`).min_w(0).truncate().child(state)))))
      .when(!o.question, (e) => e.child(h_flex().flex_none().gap(4).items_center().children(actions))))
    // What it is doing, in one sentence. Red only for something in a cloud that is not answering.
    .when(Boolean(o.busy), (e) => e.child(div().id(`dep-busy-${r.id}`).text_size(SMALL).line_height("18px").font_medium().text_color(c.navy).child(`${o.busy}...`)))
    .when(!plain && !found && !o.busy && Boolean(state), (e) => e.child(div().id(`dep-state-${r.id}`).text_size(SMALL).line_height("18px").text_color(wrong ? c.fail : deploying ? c.navy : c.grey900).child(state)))
    .when(Boolean(o.question), (e) => e.child(o.question))
    .when(Boolean(deploying), (e) => e.child(Steps(c, r)))
    // The address a site calls: one field, with Copy. The code font is for what a person copies, and nothing else.
    .when(Boolean(r.address) && inCloud && !deploying && !wrong, (e) => e.child(h_flex().gap(12).items_center()
      .child(h_flex().id(`dep-address-field-${r.id}`).flex_1().min_w(0).h(34).pl(12).pr(4).gap(8).items_center().bg(c.surface2).border_1().border_color(c.rule)
        .child(div().id(`dep-address-${r.id}`).flex_1().min_w(0).truncate().text_size(SMALL).font_family(MONO).text_color(c.navy).child(middle(r.address, o.addressChars ?? 58)))
        .child(div().id(`dep-copy-${r.id}`).size(26).flex().flex_none().items_center().justify_center().cursor_pointer().text_color(o.copied === "address" ? c.good : c.grey500)
          .hover((x) => x.text_color(c.navy)).accessibility_label("Copy the address").tooltip(o.copied === "address" ? "Copied" : "Copy the address")
          .on_click((ev, cx) => o.onCopy?.("address", ev, cx)).child(svg(icon(o.copied === "address" ? "check" : "copy")).size(14))))
      .when(!found && Boolean(o.onCopy), (x) => x.child(Button(c, `dep-copy-request-${r.id}`, o.copied === "request" ? "Copied" : "Copy a test request",
        (ev, cx) => o.onCopy?.("request", ev, cx), { kind: "quiet" })))))
    // Traffic, Speed and Cost.
    .when(showNumbers, (e) => e.child(h_flex().id(`dep-numbers-${r.id}`).gap(20).items_start()
      .child(Figure(c, `dep-traffic-${r.id}`, "Traffic", count(inHour),
        `request${inHour === 1 ? "" : "s"} in the last hour${inHour ? (failed ? `, ${count(failed)} failed` : ", none failed") : ""}`,
        hour.some((v) => v > 0) ? Spark(c, `dep-spark-${r.id}`, hour, { height: 24, width: 84 }) : null))
      .child(Figure(c, `dep-speed-${r.id}`, "Speed", s.p50_ms != null ? ms(s.p50_ms) : null,
        s.p50_ms != null ? "typical response time" : "no requests to time yet"))
      // A sleeping model has no copy awake and bills nothing, whatever its rate is when it wakes.
      .child(Figure(c, `dep-cost-${r.id}`, "Cost", !n.cost ? null : r.state === "asleep" ? "$0.00" : n.cost.hourly_usd ? money(n.cost.hourly_usd) : "$0.00",
        !n.cost ? "not known" : [r.state === "asleep" ? "while asleep" : n.cost.hourly_usd ? "an hour" : "costs nothing now",
          // Said only when there is something to say: a month's cost that is not known is not "$0.00".
          n.cost.month_usd > 0 ? `${money(n.cost.month_usd)} this month` : null].filter(Boolean).join(" · ")))))
    .children((o.notes ?? []).map((x, i) => div().id(`dep-fixed-${r.id}-${i}`).text_size(SMALL).line_height("18px").text_color(c.grey500).child(x)))
    // Where it runs, quietly, on one line.
    .child(h_flex().id(`dep-system-${r.id}`).gap(8).items_center().pt(10).border_t(1).border_color(c.rule)
      .child(r.cloud === "local" ? svg(icon("monitor")).size(14).flex_none().text_color(c.grey500) : Brand(c, r.cloud, 14))
      .child(div().id(`dep-where-${r.id}`).flex_1().min_w(0).truncate().text_size(SMALL).text_color(c.grey500).child(where))
      .child(div().id(`dep-who-${r.id}`).flex_none().text_size(SMALL).text_color(c.grey500).child(who)));
}
