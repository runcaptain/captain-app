// The other places a model can be hosted, beside this computer and AWS: Modal, Google Cloud and Azure. Each is a
// card in "Where should it run?", a guided setup sheet that lists what will be made and what it costs before
// anything is, a line for what is set up with the one action that removes it, and words for the header.
//
// Everything comes from the services (GET /api/serving/hosts and /api/serving/setup?cloud=...), so a host the
// services don't offer is never drawn, and a host that has not run on a real account says so on its card.
// Each function takes the Ship object (ship.js), which holds what was read, and the colours.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { MONO } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Button, plainError } from "./kit/ui.js";
import { costState, refreshEstimate } from "./kit/cost.js";

const enc = encodeURIComponent;
// How each host is said: on a card, on a button, and in "Set up serving in your ...".
const WORDS = {
  modal: { card: "On Modal", stage: "Stage on Modal", yours: "Modal workspace", where: "Modal", login: null, admin: "your Modal workspace" },
  gcp: { card: "In your Google Cloud project", stage: "Stage in your Google Cloud project", yours: "Google Cloud", where: "your Google Cloud project", login: "Google Cloud", admin: "your Google Cloud project" },
  azure: { card: "In your Azure subscription", stage: "Stage in your Azure subscription", yours: "Azure", where: "your Azure subscription", login: "Azure", admin: "your Azure subscription" },
};
const DEFAULT_REGION = { gcp: "us-central1", azure: "eastus" };

/** The hosts this page draws itself: every one the services offer but this computer and AWS (ship.js draws those). */
export function others(ship) { return (ship.hosts ?? []).filter((h) => WORDS[h.cloud]); }

/** The host a staged endpoint runs on, from its compute kind. Null for this computer and for AWS. */
export function hostOf(ship, kind) { return others(ship).find((h) => h.kind === kind) ?? null; }

/** How to say where a staged endpoint runs: "Modal", "your Azure subscription". Null when ship.js says it itself. */
export function whereWords(ship, kind) { const h = hostOf(ship, kind); return h ? WORDS[h.cloud].where : null; }

/** Read the hosts, find each one's compute target and login, and ask for the estimate of those that are set up. */
export async function loadHosts(ship, targets, profiles) {
  const r = await api("/api/serving/hosts", { scoped: false });
  const list = r.ok && Array.isArray(r.data) ? r.data : [];
  ship.hostCost = ship.hostCost ?? {};
  ship.hosts = list.map((h) => ({ ...h,
    targetRec: (targets ?? []).find((t) => t.kind === h.kind && t.name === h.target && !t.deleted) ?? null,
    loginRec: h.login ? (profiles ?? []).find((p) => p.provider === h.login) ?? null : null }));
  for (const h of others(ship)) {
    if (!h.targetRec) continue;
    ship.hostCost[h.cloud] = ship.hostCost[h.cloud] ?? costState();
    await refreshEstimate(ship.hostCost[h.cloud], { kind: "staging", where: h.targetRec.name, size: {} });
  }
}

/** Is this host offered here? Modal always (it signs in by itself). A cloud when a login for it is connected, or
 *  when serving is already set up there. */
function offered(h) { return h.cloud === "modal" || Boolean(h.loginRec) || Boolean(h.targetRec); }

/** Is a Google Cloud or Azure login connected, or serving set up on one? Then "connect a cloud login" needn't be said. */
export function hasCloud(ship) { return others(ship).some((h) => h.cloud !== "modal" && offered(h)); }

/** One card per host, in the same shape as the two ship.js draws. `card` is ship.js's own card builder. */
export function HostCards(ship, c, card, { cand, wont, last }) {
  return others(ship).filter(offered).map((h) => {
    const w = WORDS[h.cloud], t = h.targetRec, est = ship.hostCost?.[h.cloud]?.estimate;
    const lines = ["A site can reach it.",
      t ? est?.text ?? "Working out the cost..." : `Serving isn't set up ${h.cloud === "modal" ? "on Modal" : `in ${w.where}`} yet.`,
      h.scale_to_zero ? "It sleeps when nobody calls it, and costs nothing asleep." : "This keeps billing until you stop it."];
    const button = t
      ? Button(c, `ship-stage-${h.cloud}`, w.stage, (_e, cx) => ship.stageAt(cx, t.name), { small: true, kind: (last === "cloud" || wont) ? "primary" : "secondary" })
      : Button(c, `ship-setup-${h.cloud}`, `Set up serving in your ${w.yours}`, (_e, cx) => openHostSheet(ship, cx, h.cloud), { small: true });
    return card(`ship-choice-${h.cloud}`, w.card, lines, button)
      .when(!h.proven, (e) => e.child(div().id(`ship-choice-${h.cloud}-unproven`).text_size(12).line_height("17px").text_color(c.warn)
        .child(h.proven_text ?? "Not run on a real account yet. Tested against a stand-in only.")));
  });
}

/** Open (or close) one host's setup sheet, and ask the services what it would make there and what it costs. */
export function openHostSheet(ship, cx, cloud) {
  ship.hostSheet = ship.hostSheet === cloud ? null : cloud;
  ship.sheet = false; // one sheet at a time
  ship.hostPlan = null; ship.hostProblem = null; ship.hostPolicy = null;
  cx.notify();
  if (!ship.hostSheet) return;
  const h = others(ship).find((x) => x.cloud === cloud);
  const q = new URLSearchParams({ cloud, plan: "true" });
  if (h?.loginRec) { q.set("profile", h.loginRec.name); q.set("region", h.loginRec.region ?? DEFAULT_REGION[cloud] ?? ""); }
  cx.spawn(async (cx) => {
    const r = await api(`/api/serving/setup?${q}`, { scoped: false, timeout: 60 });
    if (ship.hostSheet !== cloud) return;
    if (r.ok && r.data?.plan) ship.hostPlan = { ...r.data.plan, cloud };
    else ship.hostProblem = plainError(r.data?.plan_error ?? r.error, "Captain couldn't work out what to set up.");
    cx.notify();
  });
}

/** Create the setup after the person has read what it makes. A login that lacks a permission gets the sentence,
 *  and what to ask an admin for is ready to copy. */
function runHostSetup(ship, cx) {
  const p = ship.hostPlan;
  if (!p || ship.busy) return;
  const w = WORDS[p.cloud];
  ship.cx = cx;
  ship.busy = `Setting up serving in your ${w.yours}`; ship.busyStep = "stage"; ship.note = null; ship.hostPolicy = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const body = { cloud: p.cloud, confirm: true, ...(p.profile ? { profile: p.profile } : {}), ...(p.region ? { region: p.region } : {}),
      ...(p.environment ? { environment: p.environment } : {}) };
    const r = await api("/api/serving/setup", { body, scoped: false });
    let done = { ok: false, error: plainError(r.error, "The setup didn't start.") };
    if (r.ok) {
      done = await ship.wait(r.data.job, 45);
      if (!done.ok) { const j = await api(`/api/jobs/${enc(r.data.job)}`, { scoped: false }); ship.hostPolicy = j.ok ? j.data?.policy ?? null : null; }
    }
    ship.busy = null; ship.busyFor = null;
    ship.note = done.ok ? { tone: "good", text: `Serving is set up in your ${w.yours}. You can stage there now.` } : { tone: "fail", text: done.error };
    if (done.ok) { ship.hostSheet = null; ship.placesFor = null; ship.loadPlaces(cx); }
    cx.notify();
  });
}

/** Take one host's setup away again. The page asks first. */
function removeHostSetup(ship, cx, cloud) {
  if (ship.busy) return;
  ship.cx = cx;
  ship.busy = "Removing the serving setup"; ship.busyStep = "stage"; ship.note = null; ship.hostRemoveAsk = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await api(`/api/serving/setup?cloud=${enc(cloud)}`, { method: "DELETE", scoped: false });
    const done = r.ok ? await ship.wait(r.data.job, 30) : { ok: false, error: plainError(r.error, "It couldn't be removed.") };
    ship.busy = null; ship.busyFor = null;
    ship.note = done.ok ? { tone: done.result?.status === "removed" ? "good" : "fail", text: done.result?.text ?? "The serving setup is removed." } : { tone: "fail", text: done.error };
    ship.placesFor = null; ship.loadPlaces(cx);
    cx.notify();
  });
}

/** What setup will make on this host, row by row with a reason each, what each staged model adds, the cost, and a
 *  confirm. Nothing is made until "Create these". */
export function HostSheet(ship, c) {
  const cloud = ship.hostSheet, w = WORDS[cloud], p = ship.hostPlan;
  const policy = ship.hostPolicy ?? p?.login_policy ?? null;
  const row = (r, id) => v_flex().id(id).py(2)
    .child(h_flex().gap(8).child(div().w(190).flex_none().text_size(12).text_color(c.grey500).child(r.resource)).child(div().font_family(MONO).text_size(11.5).text_color(c.navy).child(r.name)))
    .child(div().pl(198).text_size(12).line_height("17px").text_color(c.grey900).child(r.why));
  return v_flex().id("ship-host-sheet").p(10).gap(5).bg(c.surface2).border_1().border_color(c.rule)
    // The title and the actions share the first row, so "Create these" is in view however long the list is.
    .child(h_flex().gap(6).items_center().flex_wrap()
      .child(div().id("ship-host-sheet-title").flex_1().min_w(200).text_size(12.5).font_semibold().text_color(c.navy).child(`Set up serving in your ${w.yours}`))
      .when(Boolean(p), (e) => e.child(Button(c, "ship-host-go", "Create these", (_e, cx) => runHostSetup(ship, cx), { small: true, kind: "primary", disabled: Boolean(ship.busy) })))
      .when(Boolean(policy), (e) => e.child(Button(c, "ship-host-policy-copy", "Copy what your login needs",
        (_e, cx) => ship.copy(cx, JSON.stringify(policy, null, 2), `What your login needs is on your clipboard. Send it to whoever manages ${w.admin}.`), { small: true, kind: ship.hostPolicy ? "primary" : "secondary" })))
      .child(Button(c, "ship-host-close", "Not now", (_e, cx) => { ship.hostSheet = null; cx.notify(); }, { small: true, kind: "quiet" })))
    .when(!p && !ship.hostProblem, (e) => e.child(div().text_size(12).text_color(c.grey500).child("Working out what to create...")))
    .when(Boolean(ship.hostProblem), (e) => e.child(div().id("ship-host-problem").text_size(12).text_color(c.fail).child(ship.hostProblem)))
    .when(Boolean(p), (e) => e
      .child(div().text_size(12).text_color(c.grey900).child(`Captain will create these${p.region ? ` in ${p.region}` : ""}, each named and labelled so it can be found and removed:`))
      .children((p?.creates ?? []).map((r, i) => row(r, `ship-host-row-${i}`)))
      .when((p?.each_deploy ?? []).length > 0, (x) => x
        .child(div().pt(4).text_size(12).text_color(c.grey900).child("Each model you stage there adds:"))
        .children(p.each_deploy.map((r, i) => row(r, `ship-host-each-${i}`))))
      .child(div().id("ship-host-cost").text_size(12).line_height("17px").font_medium().text_color(c.navy).child(p?.cost?.text ?? ""))
      .when(Boolean(p?.login_text), (x) => x.child(div().id("ship-host-login").text_size(12).line_height("17px").text_color(c.grey500).child(p.login_text)))
      .child(div().text_size(12).line_height("17px").text_color(c.grey500).child("Nothing is created until you say so. Remove serving setup takes all of it away again.")))
    .when(Boolean(ship.hostPolicy), (e) => e.child(div().id("ship-host-denied").text_size(12).text_color(c.grey900)
      .child("Your login needs more permissions for this. What to ask an admin for is ready to copy.")));
}

/** What Captain made on each host for serving, always listed, with the one action that removes it. */
export function HostLines(ship, c) {
  const up = others(ship).filter((h) => h.setup && !["none", "removed", "unknown"].includes(h.setup.status));
  if (!up.length) return null;
  return v_flex().id("ship-host-lines").gap(6).children(up.map((h) => v_flex().id(`ship-host-line-${h.cloud}`).gap(4)
    .child(h_flex().gap(8).items_center().flex_wrap()
      .child(div().flex_1().min_w(240).text_size(12).line_height("17px").text_color(c.grey900).child(`${h.label}. ${h.setup.text}`))
      .when(ship.hostRemoveAsk !== h.cloud, (e) => e.child(Button(c, `ship-host-remove-${h.cloud}`, "Remove serving setup", (_e, cx) => { ship.hostRemoveAsk = h.cloud; cx.notify(); }, { small: true, kind: "quiet" }))))
    .when(ship.hostRemoveAsk === h.cloud, (e) => e.child(h_flex().id(`ship-host-ask-${h.cloud}`).p(8).gap(8).items_center().bg(c.surface2).border_1().border_color(c.rule)
      .child(div().flex_1().min_w(0).text_size(12).text_color(c.grey900).child(`Remove everything Captain created for serving in your ${WORDS[h.cloud].yours}? You can set it up again later.`))
      .child(Button(c, `ship-host-yes-${h.cloud}`, "Remove it", (_e, cx) => removeHostSetup(ship, cx, h.cloud), { small: true, kind: "primary" }))
      .child(Button(c, `ship-host-no-${h.cloud}`, "Keep it", (_e, cx) => { ship.hostRemoveAsk = null; cx.notify(); }, { small: true }))))));
}
