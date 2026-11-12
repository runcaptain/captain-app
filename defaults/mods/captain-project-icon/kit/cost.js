// captain-ui: where work runs and what it costs, the same in every form that can run work on a cloud.
//
// One row: "Runs on" with the places to pick from ("This computer", marked free, and each place added in Cloud and
// compute), the estimate beside it in plain words, and "How this is estimated", which opens the basis: the
// machine, its price, where the price came from and how the time was worked out. The numbers and every sentence
// come from the services (POST /api/costs/estimate), so a screen never works out a price itself.
//
// A screen keeps one small state object and calls:
//   const st = costState();                         in init
//   await loadPlaces(st)                            once, in a task: the places work can run
//   await refreshEstimate(st, { kind, size })       when the place or the size changes (never from render)
//   RunsOn(c, "id", st, { onPick, onToggle, onAdd }) in render
//   needsConfirm(st) / confirmText(st)              before starting: above the threshold, ask again with the number
//   costSlice(st)                                   for watchView, so an agent sees the place and the estimate
// CostLine(c, id, est, st, on) draws just the estimate with its disclosure, for a screen with its own place picker
// (a staging card). costText(cost) is the line for a finished run: "Cost about $0.42 (12 min on aws-batch)".
import { div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { shared, setShared } from "herdr";
import { api } from "./cst.js";
import { icon } from "./icons.js";
import { Badge } from "./ui.js";

export const LOCAL = "local";

export function costState(where = LOCAL) {
  return { places: null, where, estimate: null, loading: false, open: false, confirming: false, asked: null };
}

const KIND_WORDS = {
  "aws-ecs": "AWS ECS", "aws-batch": "AWS Batch", "aws-sagemaker": "AWS SageMaker", "gcp-cloud-run": "Google Cloud Run", "gcp-vertex": "Vertex AI",
  "azure-container-apps": "Azure Container Apps", "azure-ml": "Azure ML", modal: "Modal",
};

/** The places work can run, from GET /api/compute: this computer first, then each compute target that runs jobs. */
export function placesFrom(rows) {
  return [{ value: LOCAL, label: "This computer", free: true },
    ...(Array.isArray(rows) ? rows : []).filter((t) => t.kind !== "local" && (t.purposes ?? ["backtest"]).includes("backtest"))
      .map((t) => ({ value: t.name, label: t.name, brand: t.provider, kind: t.kind, region: t.region ?? null, profile: t.profile ?? null }))];
}

/** One place in words, for the line under its name: "Free. Nothing leaves this computer.", "AWS Batch, us-west-1". */
export function placeWords(p) {
  if (!p || p.value === LOCAL) return "Free. Nothing leaves this computer.";
  return [KIND_WORDS[p.kind] ?? p.kind ?? "Your cloud", p.region].filter(Boolean).join(", ");
}

/** The workspace's default place for runs changed (the compute picker on the cloud chip): tell every open screen.
 *  Screens compare `placeChanged()` with the value they last saw, from a timer. */
export function announcePlace(place) { setShared("captain.compute", { place, at: Date.now() }); }
export function placeChanged() { return shared("captain.compute")?.at ?? null; }

/** The places work can run, and the workspace's default among them (Settings, or the compute picker on the cloud
 *  chip). A form opens on the default; once the person picks a place in the form (st.picked), their pick stays. */
export async function loadPlaces(st) {
  const [r, s] = await Promise.all([api("/api/compute", { scoped: false }), api("/api/settings", { scoped: false })]);
  st.places = placesFrom(r.ok ? r.data : []);
  st.usual = s.ok ? s.data?.compute?.backtest ?? LOCAL : LOCAL;
  if (!st.picked && st.where === LOCAL && st.places.some((p) => p.value === st.usual)) st.where = st.usual;
  if (!st.places.some((p) => p.value === st.where)) st.where = LOCAL;
  return st.places;
}

/** Ask the services what this work costs where it is set to run. `want` is {kind, size, where?}. Asks only when
 *  the question changed, so calling it from a timer is cheap. Returns true when the estimate changed. */
export async function refreshEstimate(st, want) {
  if (!want?.kind) { const had = st.estimate; st.estimate = null; st.asked = null; return Boolean(had); }
  const body = { kind: want.kind, where: want.where ?? st.where ?? LOCAL, size: want.size ?? {} };
  const key = JSON.stringify(body);
  if (st.asked === key) return false;
  // A new place never shows (or publishes) the last place's number while its own is on the way.
  if (st.estimate && st.estimate.where !== undefined && body.where !== (st.askedWhere ?? body.where)) st.estimate = null;
  st.askedWhere = body.where;
  st.asked = key;
  st.loading = true;
  st.confirming = false;
  const r = await api("/api/costs/estimate", { body, scoped: false });
  if (st.asked !== key) return false; // a newer question went out meanwhile
  st.loading = false;
  st.estimate = r.ok ? r.data : { text: "Captain couldn't work out an estimate just now.", basis: [], available: false };
  return true;
}

/** Above the threshold set in Settings, a form asks again before it starts. */
export function needsConfirm(st) { return Boolean(st.estimate?.confirm) && !st.confirming; }

const dollars = (x) => (x < 0.01 ? "less than $0.01" : `$${Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

/** The sentence that repeats the number before work above the threshold starts. */
export function confirmText(st) {
  const e = st.estimate ?? {};
  const top = e.unit === "hour" ? `${dollars((e.high ?? 0) * 24)} a day` : dollars(e.high ?? 0);
  return `This is estimated at up to ${top} on ${e.where_label ?? "your cloud"}, above the ${dollars(e.threshold ?? 5)} you set to be asked about. Start it?`;
}

/** What an agent should see: the place and the estimate (kit view.js, watchView). */
export function costSlice(st) {
  const e = st.estimate;
  return { place: st.where === LOCAL ? "This computer" : st.where,
    ...(e ? { estimate: e.text, low: e.low ?? null, high: e.high ?? null, unit: e.unit ?? null, free: Boolean(e.free), priced_as_of: e.priced_as_of ?? null } : {}) };
}

/** A finished run's cost in one line, or null when there is none to show. */
export function costText(cost) {
  return cost && typeof cost.text === "string" && cost.text ? cost.text : null;
}

/** The estimate in plain words, with "How this is estimated" under it. `on.onToggle(e, cx)` opens the basis. */
export function CostLine(c, id, est, st, on = {}) {
  if (st?.loading && !est) return div().id(`${id}-wait`).text_size(12.5).text_color(c.grey500).child("Working out the cost...");
  if (!est) return div().id(`${id}-none`);
  const basis = (est.basis ?? []).filter(Boolean);
  const tone = est.free ? c.good : est.available === false ? c.grey500 : c.grey900;
  return v_flex().id(id).gap(4)
    .child(h_flex().gap(6).items_start()
      .child(svg(icon(est.free ? "check" : est.available === false ? "clock" : "cloud")).size(13).flex_none().mt(2).text_color(tone))
      .child(div().id(`${id}-text`).flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(tone).child(est.text ?? "")))
    .when(basis.length > 0, (e) => e.child(h_flex().id(`${id}-how`).gap(4).items_center().cursor_pointer().text_color(c.blue)
      .on_click((ev, cx) => on.onToggle?.(ev, cx))
      .child(svg(icon(st?.open ? "chevron-down" : "chevron-right")).size(12))
      .child(div().text_size(12).child("How this is estimated"))))
    .when(Boolean(st?.open) && basis.length > 0, (e) => e.child(v_flex().id(`${id}-basis`).pl(16).gap(3)
      .children(basis.map((b, i) => div().id(`${id}-basis-${i}`).text_size(12).line_height("17px").text_color(c.grey500).child(b)))));
}

/** The whole row. `on`: onPick(place, e, cx), onToggle(e, cx), onAdd(e, cx). `opts.fixed` shows the place without
 *  a choice (work that only runs on this computer); `opts.note` adds one more plain line under the estimate. */
export function RunsOn(c, id, st, on = {}, opts = {}) {
  const places = st.places ?? [{ value: LOCAL, label: "This computer", free: true }];
  const clouds = places.filter((p) => p.value !== LOCAL);
  const chip = (p) => {
    const sel = st.where === p.value;
    return h_flex().id(`${id}-${p.value}`).h(30).px(10).gap(6).items_center().cursor_pointer().border_1()
      .border_color(sel ? c.blue : c.grey300).bg(sel ? c.blueLight : c.surface).text_color(sel ? c.blue : c.navy).text_size(12.5)
      .on_click((e, cx) => { st.picked = true; on.onPick?.(p, e, cx); })
      .child(svg(icon(p.value === LOCAL ? "monitor" : "cloud")).size(13))
      .child(p.label)
      .when(Boolean(p.free), (e) => e.child(Badge(c, "free", sel ? "blue" : "neutral")));
  };
  const only = opts.fixed || !clouds.length;
  return v_flex().id(id).p(12).gap(8).bg(c.surface2).border_1().border_color(c.rule)
    .child(h_flex().gap(10).items_center().flex_wrap()
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child("Runs on"))
      .when(only, (e) => e.child(h_flex().id(`${id}-here`).gap(6).items_center().text_size(12.5).text_color(c.navy)
        .child(svg(icon("monitor")).size(13)).child("This computer").child(Badge(c, "free", "neutral"))))
      .when(!only, (e) => e.children(places.map(chip)))
      .when(!opts.fixed && !clouds.length && Boolean(on.onAdd), (e) => e.child(div().id(`${id}-add`).cursor_pointer().text_size(12.5).text_color(c.blue)
        .on_click((ev, cx) => on.onAdd(ev, cx)).child("Add a place to run work"))))
    .child(CostLine(c, `${id}-cost`, st.estimate, st, on))
    .when(Boolean(opts.note), (e) => e.child(div().id(`${id}-note`).text_size(12).line_height("17px").text_color(c.grey500).child(opts.note)))
    .when(Boolean(st.confirming), (e) => e.child(div().id(`${id}-confirm`).p(8).bg(c.warnLight).border_1().border_color(c.warn)
      .text_size(12.5).line_height("18px").text_color(c.warn).child(confirmText(st))));
}
