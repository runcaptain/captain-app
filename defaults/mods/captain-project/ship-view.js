// Ship, as a person meets it: where to deploy, one Deploy button per place, one confirm, one progress view, and a
// plain screen for the deployed model.
//
//   ShipSection(ship, c)    the bottom of the project page: one card per place a model can run. "This computer"
//                           always, one card per cloud login connected in Captain, and "Connect a cloud". Nothing
//                           is drawn for a cloud that is not connected.
//   DeployedView(ship, c)   the deployed model's own screen, top to bottom: the state in words, the address, a box
//                           to try a search, how much it has answered and how fast, what it costs, and Stop.
//                           Everything else sits behind "More", each with a sentence saying what it is for.
//
// A label passes one test before it goes here: would a person who has never deployed a model know what clicking
// it does? So there is no "staging", no bare "Metrics" or "Versions", and no separate "set up serving" to
// understand: Deploy does the first-time setup and the deploy in one run.
//
// The services answer three calls: GET /deploy/hosts (where it can run: this computer and one entry per cloud login
// connected in Captain), POST /deploy (without `confirm` it answers the plan, with it it starts), and GET /deploy
// (where the run stands, step by step).
import { div, svg } from "gpui-kit";
import { h_flex, v_flex, Input } from "gpui-base";
import { exec, openOverlay, run, setShared } from "herdr";
import { MONO } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Button, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { SendToAgent } from "./kit/context.js";
import { LogsTab, SettingsTab, CopiesTab, ProtectBox, Bars } from "./deploy-tabs.js";

const enc = encodeURIComponent;
const short = (ref) => String(ref ?? "").replace(/^.*\//, "");
const took = (s) => (s === null || s === undefined ? "" : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
const count = (n, one, many = `${one}s`) => `${Number(n ?? 0).toLocaleString("en-US")} ${Number(n) === 1 ? one : many}`;

// ---- what the services say --------------------------------------------------------------------------------------

/** One place a model can run, as the cards read it, from one entry of GET /deploy/hosts. `twice` is whether the
 *  same cloud has more than one login connected. */
function place(h, twice = false) {
  const local = h.host === "local";
  // The login's name is shown only when it says something the cloud's name doesn't: "AWS  aws" says it twice.
  const same = (a, b) => String(a ?? "").toLowerCase().replace(/[^a-z0-9]/g, "") === String(b ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const login = h.login && (twice || !(same(h.login, h.label) || same(h.login, h.host) || same(h.login, h.login_label))) ? h.login : null;
  return { id: h.id ?? (h.login ? `${h.host}:${h.login}` : h.host), host: h.host, login: h.login ?? null, kind: local ? "local" : "cloud",
    provider: local ? null : h.host, label: h.label, sub: local ? "" : [login, h.region].filter(Boolean).join(" \u00b7 "),
    // The machine the price is for, in the services' own sentence: the price and the machine named are one fact.
    machine: h.machine?.text ?? null,
    can_deploy: Boolean(h.can_deploy), why_not: h.why_not ?? null, why_not_action: h.fit?.outcome === "over-limit" ? "settings" : null,
    reach: h.reachable ? "A site can reach it." : "Only you can reach it.",
    cost: h.estimate?.text ?? (local ? "Free." : ""),
    // A fit that is tight is worth a line. One that is fine is not.
    note: local ? (h.fit?.outcome === "tight" ? h.fit.text : null) : h.sleeps ? "It sleeps when nobody calls it, and wakes on the next request." : null,
    unproven: !local && h.proven === null && h.proven_text ? h.proven_text : null,
    first_time: Boolean(h.first_time), last: Boolean(h.last) };
}

/** Where this project's model can run. Asked when the page opens and after anything is deployed or stopped. */
export function loadPlaces(ship, cx) {
  const pid = ship.pid();
  if (!pid) return;
  cx.spawn(async (cx) => {
    const r = await api(`/api/projects/${enc(pid)}/deploy/hosts`, { scoped: false, timeout: 60 });
    if (ship.pid() !== pid) return;
    const hosts = r.ok ? r.data.hosts ?? [] : [];
    const count = (host) => hosts.filter((x) => x.host === host).length;
    ship.dp = r.ok ? { places: hosts.map((x) => place(x, count(x.host) > 1)), model_version: r.data.model_version ?? null, beside: r.data.beside ?? null }
      : { places: [], problem: plainError(r.error, "Captain couldn't read where this can run.") };
    cx.notify();
  });
}

/** Which of the states it is in (answering, ready, asleep): read wherever the deployed model is named. */
export function loadLive(ship, cx) {
  const pid = ship.pid();
  if (!pid || !ship.staged()) return;
  cx.spawn(async (cx) => { const r = await api(`/api/projects/${enc(pid)}/staging/live`, { scoped: false, timeout: 60 }); if (ship.pid() === pid && r.ok) { ship.liveD = r.data; cx.notify(); } });
  loadHealth(ship, cx);
}

/** The health Captain last recorded for the deployed model. Reading it never runs a check: the service does that
 *  once a minute. */
export function loadHealth(ship, cx) {
  const pid = ship.pid();
  if (!pid || !ship.staged()) return;
  cx.spawn(async (cx) => { const r = await api(`/api/projects/${enc(pid)}/staging/health`, { scoped: false, timeout: 60 }); if (ship.pid() === pid && r.ok) { ship.healthD = r.data; cx.notify(); } });
}

/** Run a check now, or set what healthy means. */
export function healthDo(ship, cx, body) {
  if (ship.healthBusy) return;
  ship.healthBusy = true; ship.healthNote = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const r = body ? await api(`/api/projects/${enc(ship.pid())}/staging/health/target`, { method: "PUT", body, scoped: false })
      : await api(`/api/projects/${enc(ship.pid())}/staging/health/check`, { body: {}, scoped: false, timeout: 60 });
    ship.healthBusy = false;
    if (r.ok) { ship.healthD = r.data; ship.healthNote = body ? "Saved. The next check uses it." : null; }
    else ship.healthNote = plainError(r.error, body ? "That target couldn't be saved." : "The check couldn't run.");
    cx.notify();
  });
}

/** What the plain screen shows without being asked: the state, the last hour's numbers and the cost. */
export function loadPlain(ship, cx) {
  const pid = ship.pid();
  if (!pid || !ship.staged()) return;
  const get = (path, set) => cx.spawn(async (cx) => { const r = await api(`/api/projects/${enc(pid)}${path}`, { scoped: false, timeout: 60 }); if (ship.pid() === pid && r.ok) { set(r.data); cx.notify(); } });
  get("/staging/live", (d) => { ship.liveD = d; });
  get("/staging/health", (d) => { ship.healthD = d; });
  get("/staging/metrics?range=1h", (d) => { ship.met = d; });
  get("/staging/cost", (d) => { ship.costD = d; });
  const open = ship.more;
  if (open === "requests") get("/staging/requests?limit=50", (d) => { ship.reqs = d; });
  if (open === "versions") { get("/staging/versions", (d) => { ship.vers = d; }); get("/staging/rollbacks", (d) => { ship.backs = d; }); }
  if (open === "logs") get(`/staging/logs?limit=300${ship.logBox.value().trim() ? `&q=${enc(ship.logBox.value().trim())}` : ""}`, (d) => { ship.logsD = d; });
  if (open === "settings") { get("/staging/settings", (d) => { ship.setD = d; }); get("/staging/protect", (d) => { ship.protD = d; }); get("/staging/copies", (d) => { ship.copiesD = d; }); }
}

/** Deploy to a place. A cloud answers first with its plan, and nothing starts without a yes. */
export function deployTo(ship, cx, place, confirmed = false) {
  if (ship.djob && !ship.djob.failed) return;
  const cand = ship.pick();
  const body = { host: place.host, ...(place.login ? { login: place.login } : {}), ...(cand ? { candidate: cand.id } : { baseline: true }), ...(confirmed ? { confirm: true } : {}) };
  ship.dnote = null; ship.dasking = place.id;
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await api(`/api/projects/${enc(ship.pid())}/deploy`, { body, scoped: false, timeout: 60 });
    ship.dasking = null;
    if (r.status === 409 && r.data?.needs_confirm) { ship.dconfirm = { place, plan: r.data.plan, all: false }; cx.notify(); return; }
    if (!r.ok) { ship.dnote = { tone: "fail", text: plainError(r.error, "It couldn't be deployed.") }; cx.notify(); return; }
    ship.dconfirm = null;
    try { localStorage.setItem(`deploy-where:${ship.pid()}`, place.id); } catch {}
    await follow(ship, cx, place, r.data.plan?.steps ?? []);
  });
}

/** Take away what a failed deploy left in the account (DELETE /deploy, a job), and say when it is gone. */
function removeCreated(ship, cx) {
  if (ship.removing) return;
  ship.removing = true; ship.removeNote = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const pid = ship.pid();
    const r = await api(`/api/projects/${enc(pid)}/deploy`, { method: "DELETE", scoped: false, timeout: 60 });
    let left = null;
    for (let i = 0; r.ok && i < 600 && ship.pid() === pid; i++) {
      const s = await api(`/api/projects/${enc(pid)}/deploy`, { scoped: false });
      if (s.ok) { left = s.data?.exists_now ?? []; if (s.data?.status !== "running" && (s.data?.status !== "failed" || !left.length)) break; }
      try { await exec("sleep", ["1"]); } catch { break; }
    }
    ship.removing = false;
    const gone = r.ok && !(left ?? []).length;
    if (gone && ship.djob?.failed) ship.djob = { ...ship.djob, failed: { ...ship.djob.failed, exists: [], can: (ship.djob.failed.can ?? []).filter((x) => x !== "remove") } };
    ship.removeNote = gone ? "Removed. Nothing from this deploy is left in your account."
      : r.ok ? `Some of it is still there: ${(left ?? []).map((x) => `${x.resource} ${x.name}`).join(", ")}. Try again, or remove it in your cloud's console.`
      : plainError(r.error, "It couldn't be removed.");
    cx.notify();
    loadPlaces(ship, cx);
  });
}

/** Watch one run to its end, drawing each named step as it happens (GET /deploy, twice a second). */
async function follow(ship, cx, place, planned) {
  const pid = ship.pid();
  ship.djob = { place, steps: planned.map((x) => ({ ...x, status: "pending" })), started: Date.now() };
  cx.notify();
  for (let i = 0; i < 30 * 120 && ship.pid() === pid; i++) {
    const r = await api(`/api/projects/${enc(pid)}/deploy`, { scoped: false });
    if (r.ok && r.data) {
      const d = r.data;
      if ((d.steps ?? []).length) ship.djob = { ...ship.djob, steps: d.steps };
      if (d.status === "done" || (d.status === "none" && i > 2)) {
        ship.djob = null;
        // It is deployed. A step after that (the trained-numbers check, saving to the registry) may not have
        // finished: that is said, with its reason, and the model stays up.
        const soft = d.soft_failed ?? [];
        if (d.status === "done") ship.dnote = soft.length
          ? { tone: "fail", text: `Deployed to ${place.label}, but one step didn't finish. ${soft.map((x) => `${x.title}: ${x.error}`).join(" ")}` }
          : { tone: "good", text: `Deployed to ${place.label}.` };
        break;
      }
      if (d.status === "failed") { ship.djob = { ...ship.djob, failed: { text: plainError(d.error, "It didn't finish."), step: d.failed_step?.title ?? null, policy: d.policy ?? null, can: d.can ?? ["retry"], exists: d.exists_now ?? [], fix: d.fix_text ?? null } }; break; }
      cx.notify();
    }
    try { await exec("sleep", ["0.5"]); } catch { break; }
  }
  if (ship.djob && !ship.djob.failed) ship.djob = null;
  ship.load(cx); loadPlaces(ship, cx);
  ship.page.reload?.(cx); // the Next banner and the steps follow what was just done
  ship.page.reload?.(cx);
  cx.notify();
}

/** Check now: does the deployed model compute the same numbers it was trained on? So its result
 *  is kept with the deployment and shown at the top of the screen when it fails. */
export function checkNow(ship, cx) {
  if (ship.checking) return;
  ship.checking = true; ship.checkNote = null;
  cx.notify();
  cx.spawn(async (cx) => {
    const r = await api(`/api/projects/${enc(ship.pid())}/staging/check`, { body: {}, scoped: false, timeout: 300 });
    ship.checking = false;
    if (r.ok) ship.checkResult = r.data; else ship.checkNote = plainError(r.error, "The check couldn't run.");
    ship.load(cx);
    cx.notify();
  });
}

/** The last result of that check: the one just run here, else the one kept with the deployment. */
export function lastCheck(ship) { return ship.checkResult ?? ship.staged()?.check ?? null; }

// ---- small pieces -----------------------------------------------------------------------------------------------

const dot = (c, tone) => div().size(8).flex_none().rounded_full().bg({ good: c.good, busy: c.blue, fail: c.fail, quiet: c.grey300 }[tone] ?? c.grey300);
const line = (c, id, text, tone = "grey900") => div().id(id).text_size(12.5).line_height("18px").text_color(c[tone]).child(text);

/** Which state the deployed model is in, as a sentence a person would say. */
export function stateWords(ship) {
  const s = ship.staged(), st = ship.status?.status, live = ship.liveD;
  if (!s) return { tone: "quiet", text: "Nothing is deployed." };
  let last = null;
  try { last = localStorage.getItem(`deploy-where:${ship.pid()}`); } catch {}
  const clouds = (ship.dp?.places ?? []).filter((p) => p.kind !== "local");
  // The place it was deployed to: the one the services name, else the one chosen here last, else the cloud its kind says.
  const place = clouds.find((p) => s.host && p.host === s.host && (!s.login || p.login === s.login)) ?? clouds.find((p) => p.id === last)
    ?? clouds.find((p) => p.provider && String(s.kind ?? "").startsWith(p.provider));
  const where = s.kind === "local" ? "this computer" : place?.label ?? "your cloud";
  if (st === "starting") return { tone: "busy", text: `Starting on ${where}.` };
  if (st === "failed") return { tone: "fail", text: `It failed to start on ${where}. ${s.error ?? ""}`.trim() };
  if (st !== "running") return { tone: "quiet", text: "Stopped. Nothing is running and nothing is billing." };
  if (ship.status?.asleep || live?.state === "asleep") return { tone: "quiet", text: `Asleep on ${where}. It wakes on the next request, and costs nothing asleep.` };
  if (live?.state === "handling") return { tone: "good", text: `Running on ${where}. Answering requests.` };
  return { tone: "good", text: `Running on ${where}. Ready, with nothing to do right now.` };
}

// ---- the Ship section -------------------------------------------------------------------------------------------

function placeCard(ship, c, p) {
  const busy = Boolean(ship.djob) || Boolean(ship.dasking) || Boolean(ship.busy);
  const sub = p.sub;
  return v_flex().id(`ship-place-${p.id}`).flex_1().min_w(240).max_w(380).p(12).gap(6).bg(c.surface).border_1().border_color(c.rule)
    .child(h_flex().gap(8).items_center()
      .child(p.provider ? Brand(c, p.provider, 16) : svg(icon("monitor")).size(15).flex_none().text_color(c.grey900))
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(p.label))
      .when(Boolean(sub), (e) => e.child(div().min_w(0).truncate().text_size(12.5).text_color(c.grey500).child(sub))))
    .child(line(c, `ship-place-${p.id}-reach`, p.reach ?? ""))
    .child(line(c, `ship-place-${p.id}-cost`, p.cost))
    .when(Boolean(p.machine), (e) => e.child(line(c, `ship-place-${p.id}-machine`, p.machine, "grey500")))
    .when(Boolean(p.note), (e) => e.child(line(c, `ship-place-${p.id}-note`, p.note, "grey500")))
    .when(Boolean(p.unproven), (e) => e.child(line(c, `ship-place-${p.id}-unproven`, p.unproven, "warn")))
    .when(Boolean(p.why_not), (e) => e.child(line(c, `ship-place-${p.id}-why`, p.why_not, "fail")))
    .when(p.why_not_action === "settings", (e) => e.child(div().id(`ship-place-${p.id}-limit`).text_size(12.5).text_color(c.blue).cursor_pointer()
      .on_click(() => run("app.settings")).child("Change the limit in Settings")))
    .child(div().flex_1())
    .child(h_flex().child(Button(c, `ship-deploy-${p.id}`, ship.dasking === p.id ? "Checking..." : "Deploy", (_e, cx) => deployTo(ship, cx, p),
      { small: true, disabled: !p.can_deploy || busy })));
}

function connectCard(c, wide) {
  const open = () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "cloud" } });
  return v_flex().id("ship-connect-cloud").flex_1().min_w(240).when(!wide, (e) => e.max_w(380)).p(12).gap(6).bg(c.surface).border_1().border_color(c.rule)
    .child(h_flex().gap(8).items_center().child(svg(icon("cloud")).size(15).flex_none().text_color(c.grey900))
      .child(div().text_size(12.5).font_semibold().text_color(c.navy).child(wide ? "Connect a cloud to deploy where a site can reach it" : "Connect a cloud")))
    .child(line(c, "ship-connect-cloud-text", wide ? "On this computer only you can reach the model. Connect AWS, Google Cloud, Azure or Modal, and it gets an address your site can call."
      : "Another place to deploy: AWS, Google Cloud, Azure or Modal."))
    .child(div().flex_1())
    .child(h_flex().child(Button(c, "ship-connect-cloud-go", "Connect a cloud", open, { small: true })));
}

/** Before a cloud deploy: what the host's own plan says will be made, what it costs, how long it takes. One button.
 *  Nothing here is written on this side: the list, short and in full, is the plan the services answered. */
function confirmBox(ship, c) {
  const { place, plan, all } = ship.dconfirm;
  const groups = plan.creates?.groups ?? [], n = plan.creates?.count ?? groups.reduce((t, g) => t + (g.items ?? []).length, 0);
  return v_flex().id("ship-confirm").w_full().p(14).gap(8).bg(c.surface).border_1().border_color(c.blue)
    .child(div().id("ship-confirm-title").text_size(15).font_semibold().text_color(c.navy).child(`Deploy ${short(plan.model_version)} to ${place.label}?`))
    .when(Boolean(plan.creates?.short), (e) => e.child(line(c, "ship-confirm-first", `${plan.first_time ? "The first time, Captain makes this in your account. " : "Captain makes this in your account. "}${plan.creates.short}`)))
    .when(!plan.creates?.short && (plan.exists ?? []).length > 0, (e) => e.child(line(c, "ship-confirm-skips", "Everything it needs is already set up in your account. Only the model itself is new.")))
    .children(groups.map((g, i) => v_flex().id(`ship-confirm-g${i}`).gap(2)
      .child(h_flex().gap(10).items_start()
        .child(div().w(190).flex_none().text_size(12.5).font_medium().text_color(c.navy).child(g.title))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.grey900).child(g.summary ?? `${count((g.items ?? []).length, "thing")}`)))
      .when(all, (x) => x.children((g.items ?? []).map((it, j) => h_flex().id(`ship-confirm-g${i}-i${j}`).pl(200).gap(8).items_start()
        .child(div().flex_none().text_size(12.5).text_color(c.navy).child(it.name ?? ""))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.grey500).child([it.resource, it.why].filter(Boolean).join(". "))))))))
    .when(n > 0, (e) => e.child(h_flex().child(Button(c, "ship-confirm-all", all ? "Show less" : `Show everything (${count(n, "thing")})`, (_e, cx) => { ship.dconfirm.all = !all; cx.notify(); }, { small: true }))))
    .when(Boolean(plan.machine?.text ?? place.machine), (e) => e.child(line(c, "ship-confirm-machine", plan.machine?.text ?? place.machine)))
    .when(all && (plan.exists ?? []).length > 0, (e) => e.child(line(c, "ship-confirm-exists", `Already there, and used as it is: ${plan.exists.map((x) => x.name).join(", ")}.`, "grey500")))
    .child(line(c, "ship-confirm-cost", `${plan.cost?.text ?? ""}${plan.cost?.sleeps ? "" : " It bills until you stop it."}`.trim(), "navy"))
    .when(Boolean(plan.minutes?.text), (e) => e.child(line(c, "ship-confirm-minutes", plan.minutes.text)))
    .children((plan.warnings ?? []).map((w, i) => line(c, `ship-confirm-warning-${i}`, w, "fail")))
    .child(h_flex().gap(8)
      .child(Button(c, "ship-confirm-yes", "Deploy", (_e, cx) => deployTo(ship, cx, place, true), { small: true, kind: "primary" }))
      .child(Button(c, "ship-confirm-no", "Not now", (_e, cx) => { ship.dconfirm = null; cx.notify(); }, { small: true })));
}

/** One run, setup and deploy together, with each step named as it happens. */
function progressBox(ship, c) {
  const j = ship.djob, failed = j.failed;
  const tone = (s) => ({ done: "good", skipped: "good", running: "busy", failed: "fail" }[s.status] ?? "quiet");
  const word = (s) => ({ done: took(s.seconds), skipped: "already there", running: s.seconds ? took(s.seconds) : "now", failed: "failed", pending: "" }[s.status] ?? "");
  return v_flex().id("ship-progress").w_full().p(14).gap(6).bg(c.surface).border_1().border_color(failed ? c.fail : c.rule)
    .child(h_flex().gap(8).items_center()
      .child(div().id("ship-progress-title").flex_1().text_size(15).font_semibold().text_color(c.navy).child(failed ? `Deploying to ${j.place.label} stopped` : `Deploying to ${j.place.label}`))
      .when(!failed, (e) => e.child(div().id("ship-progress-elapsed").text_size(12.5).text_color(c.grey500).child(took((Date.now() - j.started) / 1000)))))
    .children(j.steps.map((s, i) => h_flex().id(`ship-step-${i}`).gap(10).items_center()
      .child(dot(c, tone(s)))
      .child(div().flex_1().min_w(0).text_size(12.5).text_color(s.status === "pending" ? c.grey500 : c.navy).child(s.title))
      .child(div().flex_none().text_size(12.5).text_color(s.status === "failed" ? c.fail : c.grey500).child(word(s)))))
    .when(!j.steps.length && !failed, (e) => e.child(line(c, "ship-progress-message", "Starting...", "grey500")))
    .when(Boolean(failed), (e) => e
      .child(line(c, "ship-progress-problem", failed.text, "fail"))
      .when(Boolean(failed.policy), (x) => x.child(v_flex().id("ship-progress-policy").gap(4)
        .child(line(c, "ship-progress-policy-text", "Your login is missing a permission. Give this policy to whoever manages the account, then deploy again:"))
        .child(div().p(8).bg(c.surface2).font_family(MONO).text_size(12.5).line_height("18px").text_color(c.navy).child(JSON.stringify(failed.policy, null, 2)))
        .child(h_flex().child(Button(c, "ship-progress-copy-policy", "Copy the policy", (_e, cx) => ship.copy(cx, JSON.stringify(failed.policy, null, 2), "The policy is on your clipboard."), { small: true })))))
      // When deploying again cannot work, what to do in its place, in the services' own sentence.
      .when(Boolean(failed.fix), (x) => x.child(line(c, "ship-progress-fix", failed.fix)))
      // What the deploy left in the account, by name, with a way to take it away.
      .when((failed.exists ?? []).length > 0, (x) => x.child(line(c, "ship-progress-exists", `This now exists in your account: ${failed.exists.map((r) => `${r.resource} ${r.name}`).join(", ")}.`)))
      .when(Boolean(ship.removeNote), (x) => x.child(line(c, "ship-progress-remove-note", ship.removeNote, "grey500")))
      .child(h_flex().gap(8).flex_wrap()
        // "Deploy again" only when it can work.
        .when((failed.can ?? ["retry"]).includes("retry"), (x) => x.child(Button(c, "ship-progress-again", "Deploy again", (_e, cx) => { const p = j.place; ship.djob = null; ship.removeNote = null; deployTo(ship, cx, p, true); }, { small: true, kind: "primary" })))
        .when((failed.can ?? []).includes("remove") && (failed.exists ?? []).length > 0, (x) => x.child(Button(c, "ship-progress-remove", ship.removing ? "Removing..." : "Remove what was created", (_e, cx) => removeCreated(ship, cx), { small: true, disabled: Boolean(ship.removing) })))
        .child(Button(c, "ship-progress-close", "Close", (_e, cx) => { ship.djob = null; ship.removeNote = null; cx.notify(); }, { small: true }))));
}

/** The bottom of the project page. Only where to deploy, or what is deployed. */
export function ShipSection(ship, c) {
  ship.settlePast();
  const d = ship.dp, s = ship.staged(), on = ship.running() || ship.status?.status === "starting";
  const cand = ship.pick(), champ = ship.page.exps?.champion ?? null;
  const what = cand ? short(cand.pipeline) : champ ? short(champ) : null;
  const wrap = v_flex().id("ship").w_full().gap(10);
  if (ship.djob) return wrap.child(progressBox(ship, c));
  if (ship.dconfirm) return wrap.child(confirmBox(ship, c));
  if (on && s) {
    const w = stateWords(ship);
    const address = s.url ?? s.endpoint?.url ?? s.endpoint?.name ?? "";
    return wrap.child(v_flex().id("ship-deployed").w_full().p(12).gap(6).bg(c.surface).border_1().border_color(c.rule)
      .child(h_flex().gap(8).items_center().flex_wrap()
        .child(div().id("ship-deployed-name").text_size(15).font_semibold().text_color(c.navy).child(short(s.model_version)))
        .child(div().flex_1().min_w(0))
        .child(Button(c, "ship-open", "Open it", (_e, cx) => ship.page.openDeployment(cx), { small: true })))
      // The Deployments screen is where every deployed model is managed: this opens this one there.
      .child(div().id("ship-open-in-deployments").text_size(12.5).text_color(c.blue).cursor_pointer()
        .on_click(() => { setShared("captain.open", { section: "deployments", open: `${ship.pid()}:${s.env ?? "staging"}`, at: Date.now() }); run("layout.select", { stack: "sections", child: "deployments" }); })
        .child("Open in Deployments"))
      .child(h_flex().gap(8).items_center().child(dot(c, ship.healthD?.status === "unhealthy" ? "fail" : w.tone)).child(line(c, "ship-deployed-state", w.text)))
      .when(ship.healthD?.status === "unhealthy", (e) => e.child(line(c, "ship-deployed-unhealthy", ship.healthD.text, "fail")))
      .when(Boolean(address), (e) => e.child(h_flex().gap(8).items_center().flex_wrap()
        .child(div().id("ship-deployed-address").min_w(0).max_w_full().truncate().font_family(MONO).text_size(12.5).text_color(c.navy).child(address))
        .child(Button(c, "ship-deployed-copy", "Copy", (_e, cx) => ship.copy(cx, address, "The address is on your clipboard."), { small: true })))))
      .when(Boolean(ship.dpast), (e) => e.child(pastLine(ship, c)))
      .when(Boolean(ship.dnote), (e) => e.child(note(c, ship.dnote)));
  }
  if (!what) return wrap.child(line(c, "ship-nothing", "Train a model, or pick one to compare others with, and you can deploy it here.", "grey500"));
  const places = d?.places ?? [];
  const clouds = places.filter((p) => p.kind !== "local");
  return wrap
    .child(line(c, "ship-question", `Where should ${what} run?`))
    .when(!d, (e) => e.child(line(c, "ship-loading", "Looking at where it can run...", "grey500")))
    .when(Boolean(d?.problem), (e) => e.child(line(c, "ship-places-problem", d.problem, "fail")))
    .when(Boolean(d), (e) => e.child(h_flex().id("ship-places").flex_wrap().items_stretch().gap(10)
      .children(places.map((p) => placeCard(ship, c, p)))
      .child(connectCard(c, clouds.length === 0))))
    .when(Boolean(s) && !on && !ship.page.deployOpen, (e) => e.child(line(c, "ship-last", `Last deployed: ${short(s.model_version)}. ${stateWords(ship).text}${s.spent?.text ? ` ${s.spent.text}.` : ""}`, "grey500")))
    .when(Boolean(ship.dnote), (e) => e.child(note(c, ship.dnote)));
}

/** An earlier failure, once a model is running: one line, and a way to put it away. */
const pastLine = (ship, c) => h_flex().id("ship-past").gap(8).items_center().flex_wrap()
  .child(div().id("ship-past-text").flex_1().min_w(0).text_size(12.5).text_color(c.grey500).child(ship.dpast.text))
  .child(Button(c, "ship-past-dismiss", "Dismiss", (_e, cx) => { ship.dpast = null; cx.notify(); }, { small: true }));

const note = (c, n) => div().id("ship-note").px(10).py(6).text_size(12.5).bg(n.tone === "fail" ? c.failLight : c.goodLight).text_color(n.tone === "fail" ? c.fail : c.good).child(n.text);

// ---- the deployed model's screen --------------------------------------------------------------------------------

/** What sits behind "More": a label that says what the thing is for, and the sentence it opens with. */
function moreItems(ship) {
  const k = ship.conn, s = ship.staged();
  const base = s?.arms?.baseline && s.serve !== "baseline" ? short(s.arms.baseline).replace(/@v\d+$/, "") : null;
  return [
    { id: "requests", label: "Requests it has answered", text: "Each request it answered: what was asked, what it returned, and how long it took. Pick one to see it in full." },
    base ? { id: "differ", label: "Where the two models disagree", text: `Searches where this model and ${base} return the most different results. Pick one to run it.` } : null,
    { id: "health", label: "Is it healthy?", text: "Captain checks once a minute that it answers, how many requests fail and how fast it is, against the targets you set. It tells you when that changes." },
    { id: "check", label: "Check it uses the same numbers it was trained on", text: "A model can be fed different numbers when it runs than when it was trained, and then it ranks worse than its tests said. This compares the two. It should pass before you put it in production." },
    { id: "traffic", label: "Use it from your site", text: "The code your site pastes, the address of each model, and the key. You can test it quietly with a copy of your site's requests first." },
    { id: "versions", label: "Earlier versions", text: "Every version deployed here, newest first, and a way to put an earlier one back." },
    { id: "logs", label: "Logs", text: "What the model wrote down while it answered, line by line." },
    { id: "settings", label: "Settings", text: "Where it runs, on what machine, when it stops by itself, its key, and what it accepts from a site." },
  ].filter(Boolean);
}

function moreBody(ship, c, id) {
  const [, , connect] = ship.steps();
  if (id === "differ") return differBody(ship, c);
  if (id === "health") return healthBody(ship, c);
  if (id === "check") return checkBody(ship, c);
  if (id === "requests") return v_flex().gap(8).child(numbers(ship, c, true)).child(ship.requestsTab(c));
  if (id === "traffic") return v_flex().id("dep-traffic").w_full().bg(c.surface).border_1().border_color(c.rule)
    .child(ship.connectStep(c, connect));
  if (id === "versions") return ship.versionsTab(c);
  if (id === "logs") return LogsTab(ship, c);
  if (id === "settings") return v_flex().gap(8).child(SettingsTab(ship, c)).child(ProtectBox(ship, c)).child(CopiesTab(ship, c));
  return null;
}

/** The searches where the two models return the most different results. Picking one runs it. */
function differBody(ship, c) {
  const d = ship.diff;
  if (!d) return line(c, "dep-differ-loading", "Comparing the two on this project's own searches...", "grey500");
  return v_flex().id("dep-differ").w_full().p(12).gap(4).bg(c.surface).border_1().border_color(c.rule)
    .when(Boolean(d.text), (e) => e.child(line(c, "dep-differ-text", d.text, d.mostly_same ? "navy" : "grey900")))
    .when(!d.queries.length, (e) => e.child(line(c, "dep-differ-none", d.none_differ ?? (d.checked ? `None of the ${d.checked} searches checked come back different.` : "There are no searches to compare yet."))))
    .children(d.queries.slice(0, 10).map((q, i) => h_flex().id(`dep-differ-${i}`).gap(8).items_center().py(3).cursor_pointer().hover((x) => x.bg(c.surface2))
      .on_click((_e, cx) => ship.tryQuery(cx, q.query))
      .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(c.blue).child(q.query))
      .child(div().flex_none().text_size(12.5).text_color(c.grey500).child(`${q.moved} of the top 10 in another place`))));
}

/** Its health as last recorded, what healthy means here, and a way to change that or check now. */
function healthBody(ship, c) {
  const h = ship.healthD, t = h?.target ?? {};
  const choice = (id, label, on, body) => div().id(id).h(26).px(10).flex().flex_none().items_center().cursor_pointer().text_size(12.5).border_1()
    .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
    .on_click((_e, cx) => healthDo(ship, cx, body)).child(label);
  return v_flex().id("dep-health").w_full().p(12).gap(8).bg(c.surface).border_1().border_color(c.rule)
    .child(line(c, "dep-health-text", h?.text ?? "Reading its health...", h?.status === "unhealthy" ? "fail" : "grey900"))
    .when(Boolean(h?.checked_at), (e) => e.child(line(c, "dep-health-when", `Last checked ${String(h.checked_at).slice(11, 16)} UTC.${h.last_request_at ? ` Last request ${String(h.last_request_at).slice(11, 16)} UTC.` : ""}`, "grey500")))
    .child(v_flex().gap(4)
      .child(div().text_size(12.5).font_medium().text_color(c.navy).child("Unhealthy when 95 in 100 requests take longer than"))
      .child(h_flex().gap(6).flex_wrap().children([[50, "50 ms"], [200, "200 ms"], [500, "500 ms"], [1000, "1 second"]].map(([v, label]) => choice(`dep-health-p95-${v}`, label, t.p95_ms === v, { p95_ms: v, error_rate: t.error_rate })))))
    .child(v_flex().gap(4)
      .child(div().text_size(12.5).font_medium().text_color(c.navy).child("Unhealthy when more requests than this fail"))
      .child(h_flex().gap(6).flex_wrap().children([[0.001, "1 in 1,000"], [0.01, "1 in 100"], [0.05, "5 in 100"]].map(([v, label]) => choice(`dep-health-err-${String(v).replace(".", "_")}`, label, t.error_rate === v, { error_rate: v, ...(t.p95_ms != null ? { p95_ms: t.p95_ms } : {}) })))))
    .when(!t.set, (e) => e.child(line(c, "dep-health-default", "No target is set yet. Until one is, only a model that stops answering or fails more than 5 in 100 requests counts as unhealthy.", "grey500")))
    .when(Boolean(ship.healthNote), (e) => e.child(line(c, "dep-health-note", ship.healthNote, "grey500")))
    .child(h_flex().child(Button(c, "dep-health-check", ship.healthBusy ? "Checking..." : "Check now", (_e, cx) => healthDo(ship, cx, null), { small: true, disabled: Boolean(ship.healthBusy) || !ship.running() })));
}

/** The check that it uses the numbers it was trained on: the last result, in words, and Check now. */
function checkBody(ship, c) {
  const k = lastCheck(ship);
  const tone = k?.status === "fail" ? "fail" : k?.status === "pass" ? "good" : "grey900";
  return v_flex().id("dep-check").w_full().p(12).gap(8).bg(c.surface).border_1().border_color(c.rule)
    .child(line(c, "dep-check-result", k?.text ?? "Not checked yet. It needs a few requests to look at: try a search first.", tone))
    .when(k?.status === "fail" && (k.features ?? []).length > 0, (e) => e.child(line(c, "dep-check-features",
      `Most different: ${k.features.slice(0, 3).map((f) => f.feature).join(", ")}.`)))
    .when(Boolean(ship.checkNote), (e) => e.child(line(c, "dep-check-note", ship.checkNote, "fail")))
    .child(h_flex().child(Button(c, "dep-check-now", ship.checking ? "Checking..." : "Check now", (_e, cx) => checkNow(ship, cx), { small: true, disabled: Boolean(ship.checking) || !ship.running() })));
}

/** How much it has answered and how fast, in two sentences. With `charts`, the last hour drawn under them. */
function numbers(ship, c, charts = false) {
  const m = ship.met, s = m?.summary;
  if (!m) return line(c, "dep-numbers", "Reading what it has answered...", "grey500");
  const n = s?.requests ?? 0;
  const first = n ? `It has answered ${count(n, "request")} in ${m.range_text}${s.errors ? `, and ${count(s.errors, "one failed", "failed")}` : ""}.` : `It has answered no requests in ${m.range_text}.`;
  // In units a person expects: "under 1 ms", never 0.2 ms. The endpoint's own time first, then the site's once it reports.
  const under = (x) => (Number(x) < 1 ? "under 1 ms" : `${Math.round(Number(x))} ms`);
  const cap = (x) => `${Math.max(1, Math.ceil(Number(x)))} ms`;
  const fast = n && s.p50 !== null && s.p50 !== undefined ? ` Usually in ${under(s.p50)} at the endpoint, almost always within ${cap(s.p95)}.` : "";
  const site = s?.site_reported ? ` As your site sees it, the network included: usually ${under(s.site_p50)}.` : "";
  return v_flex().id("dep-numbers").gap(4)
    .child(line(c, "dep-numbers-text", first + fast + site))
    .when(charts && n > 0, (e) => e.child(Bars(c, "dep-numbers-bars", m.buckets.map((b) => b.requests), { height: 44, over: m.buckets.map((b) => b.errors), from: "an hour ago", to: "now" })));
}

function costLine(ship, c) {
  const d = ship.costD, s = ship.staged();
  if (!d) return line(c, "dep-cost-line", s?.kind === "local" ? "Runs on this computer. No cloud cost." : "Working out the cost...", "grey500");
  if (d.free) return line(c, "dep-cost-line", "Runs on this computer. No cloud cost.");
  const month = String(d.month_text ?? "$0.00");
  return line(c, "dep-cost-line", `${d.rate_text ?? ""} ${month.charAt(0).toUpperCase()}${month.slice(1)} so far this month.`.trim());
}

/** The deployed model, top to bottom. */
export function DeployedView(ship, c) {
  const s = ship.staged(), on = ship.running(), w = stateWords(ship);
  ship.settlePast();
  if (!s || ship.djob || ship.dconfirm) return ShipSection(ship, c);
  const address = on ? (s.url ?? s.endpoint?.url ?? s.endpoint?.name ?? "") : null;
  const k = ship.conn, r = ship.result;
  const base = s.arms?.baseline ? short(s.arms.baseline).replace(/@v\d+$/, "") : null;
  const items = moreItems(ship), open = items.find((x) => x.id === ship.more) ?? null;
  const top = v_flex().id("deployed").w_full().p(14).gap(10).bg(c.surface).border_1().border_color(c.rule)
    .child(h_flex().gap(8).items_center().flex_wrap()
      .child(div().id("ship-name").text_size(15).font_semibold().text_color(c.navy).child(short(s.model_version)))
      .when(on && Boolean(s.arms?.baseline) && s.serve !== "baseline", (e) => e.child(div().id("ship-beside").text_size(12.5).text_color(c.grey500).child(`compared with ${short(s.arms.baseline)}`)))
      .child(div().flex_1().min_w(0))
      .when(on && !ship.busy, (e) => e.child(Button(c, "ship-stop", "Stop", (_e, cx) => { ship.stopAsk = true; cx.notify(); }, { small: true })))
      .when(Boolean(ship.busy), (e) => e.child(div().id("ship-busy").text_size(12.5).text_color(c.grey500).child(`${ship.busy}...`))))
    .child(h_flex().gap(8).items_center().child(dot(c, w.tone)).child(line(c, "dep-state", w.text, "navy")))
    // Unhealthy is said here, at the top, in words: what is wrong, and since when.
    .when(ship.healthD?.status === "unhealthy" && on, (e) => e.child(v_flex().id("dep-unhealthy").p(10).gap(4).bg(c.failLight)
      .child(line(c, "dep-unhealthy-text", ship.healthD.text, "fail"))
      .child(div().id("dep-unhealthy-open").text_size(12.5).text_color(c.blue).cursor_pointer()
        .on_click((_e, cx) => { ship.more = "health"; cx.notify(); }).child("See its health and your targets"))))
    // A failed check is a reason not to put it in production: said here, at the top, in words, not only behind More.
    .when(lastCheck(ship)?.status === "fail", (e) => e.child(v_flex().id("dep-check-failed").p(10).gap(4).bg(c.failLight)
      .child(line(c, "dep-check-failed-text", `It is not using the same numbers it was trained on, so it may rank worse than its tests said. ${lastCheck(ship).text ?? ""}`.trim(), "fail"))
      .child(div().id("dep-check-failed-open").text_size(12.5).text_color(c.blue).cursor_pointer()
        .on_click((_e, cx) => { ship.more = "check"; cx.notify(); }).child("See the check"))))
    .when(Boolean(ship.stopAsk) && on, (e) => e.child(h_flex().id("ship-stop-ask").p(10).gap(10).items_center().bg(c.surface2).border_1().border_color(c.rule)
      .child(div().flex_1().min_w(0).text_size(12.5).text_color(c.grey900).child("Stop it? The address goes away. What it answered is kept."))
      .child(Button(c, "ship-stop-yes", "Stop it", (_e, cx) => ship.stop(cx), { small: true }))
      .child(Button(c, "ship-stop-no", "Keep it running", (_e, cx) => { ship.stopAsk = false; cx.notify(); }, { small: true }))))
    .when(on, (e) => e
      .child(h_flex().id("ship-reach").gap(8).items_center().flex_wrap()
        // A long address is cut with an ellipsis, never run off the card. Copy takes the whole of it.
        .child(div().id("ship-address").min_w(0).max_w_full().truncate().font_family(MONO).text_size(12.5).text_color(c.navy).tooltip(address).child(address))
        .child(Button(c, "ship-copy-address", "Copy", (_e, cx) => ship.copy(cx, address, "The address is on your clipboard."), { small: true }))
        .when(Boolean(k?.test_request), (x) => x.child(Button(c, "ship-copy-test", "Copy a test request", (_e, cx) => ship.copy(cx, k.test_request, "A test request is on your clipboard. Paste it in a terminal."), { small: true })))
        .when(Boolean(s.needs_key), (x) => x.child(Button(c, "ship-copy-key", "Copy its key", (_e, cx) => ship.copyKey(cx), { small: true })))
        .child(div().id("ship-where").text_size(12.5).text_color(c.grey500).child(s.kind === "local" ? "Only this computer can reach it." : "Your site can call it.")))
      // Try a search: what it returns, beside the model it is compared with.
      .child(v_flex().id("dep-try").gap(6)
        .child(div().text_size(12.5).font_medium().text_color(c.navy).child("Try a search"))
        .child(h_flex().gap(8)
          .child(div().id("ship-try-box").flex_1().h(32).flex().items_center().px(10).bg(c.surface).border_1().border_color(c.grey300).cursor_text()
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(ship.box).w_full().text_size(12.5).text_color(c.navy)))
          .child(Button(c, "ship-try-go", ship.asking ? "Searching..." : "Search", (_e, cx) => ship.ask(cx), { disabled: ship.asking })))
        .when(Boolean(base) && !r, (x) => x.child(div().id("dep-try-ideas").text_size(12.5).text_color(c.blue).cursor_pointer()
          .on_click((_e, cx) => { ship.more = "differ"; cx.notify(); }).child(`Not sure what to type? See where this model and ${base} disagree`)))
        .when(Boolean(ship.askError), (x) => x.child(div().id("ship-try-error").text_size(12.5).text_color(c.fail).child(ship.askError)))
        .when(Boolean(r), (x) => x
          .child(h_flex().id("ship-try-results").flex_wrap().items_start().gap(10)
            .child(ship.side(c, r.staged, "This model", "ship-try-staged"))
            .when(Boolean(r.baseline), (y) => y.child(ship.side(c, r.baseline, `Compared with ${base ?? "the other model"}`, "ship-try-baseline"))))
          .child(div().id("ship-try-summary").text_size(12.5).text_color(c.grey500).child(
            (r.baseline ? (r.same_order ? "Both return the same results in the same order for this search. " : `${r.moved} of the top ${r.staged.results.length} differ from ${base ?? "the model it's compared with"}. `) : "")
            + "Searches typed here are tests. They never count as shoppers.")))))
    .child(numbers(ship, c))
    .child(costLine(ship, c))
    .when(Boolean(ship.status?.warning), (e) => e.child(line(c, "ship-line-warning", ship.status.warning, "fail")))
    .when(Boolean(ship.note), (e) => e.child(note(c, { ...ship.note, text: ship.w(ship.note.text) })))
    .when(Boolean(ship.problem), (e) => e.child(line(c, "ship-problem", ship.problem, "fail")));
  const more = v_flex().id("dep-more").w_full().gap(0).bg(c.surface).border_1().border_color(c.rule)
    .child(div().px(14).pt(10).pb(4).text_size(12.5).font_medium().text_color(c.grey500).child("More"))
    .children(items.map((it) => h_flex().id(`dep-more-${it.id}`).px(14).py(7).gap(12).items_start().cursor_pointer().border_t(1).border_color(c.rule)
      .when(open?.id === it.id, (e) => e.bg(c.blueLight)).hover((e) => e.bg(c.surface2))
      .on_click((_e, cx) => { ship.more = open?.id === it.id ? null : it.id; ship.tab = it.id; loadPlain(ship, cx); cx.notify(); })
      .child(div().w(210).flex_none().text_size(12.5).font_medium().text_color(c.blue).child(it.label))
      .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.grey900).child(it.text))));
  return v_flex().id("deployed-view").w_full().gap(12)
    .child(top)
    .child(more)
    // Hand what is deployed to a coding agent, as the other screens do.
    .when(!ship.busy, (e) => e.child(h_flex().child(SendToAgent(c, "ship-send", { ref: `staging:${ship.pid()}`, kind: "staging", label: `what is deployed for ${ship.page.project()?.name ?? ship.pid()}` }))))
    .when(Boolean(open), (e) => e.child(v_flex().id(`dep-open-${open.id}`).w_full().gap(6)
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().text_size(15).font_semibold().text_color(c.navy).child(open.label))
        .child(Button(c, "dep-open-close", "Close", (_e, cx) => { ship.more = null; cx.notify(); }, { small: true })))
      .child(line(c, "dep-open-text", open.text, "grey500"))
      .child(moreBody(ship, c, open.id))))
    .when(!on && !ship.busy, (e) => e.child(ShipSection(ship, c)));
}
