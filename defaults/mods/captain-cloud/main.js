// CAPTAIN: Cloud and compute: which cloud this workspace works with. Opened from the
// sidebar's cloud chip and the palette. GET /api/cloud/summary (no cloud calls), and
// Test runs POST /api/cloud/profiles/{name}/test. Profile fields carry no secrets, and
// none are shown.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, props, closeOverlay, openOverlay } from "herdr";
import { api, watchWorkspace } from "./kit/cst.js";
import { watchLive } from "./kit/live.js";
import { palette, isDark, FONT, RADIUS } from "./kit/tokens.js";
import { Card, Button, Empty, Problem } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";

const SHORT = { aws: "AWS", gcp: "Google Cloud", azure: "Azure", modal: "Modal", local: "This computer" };
// How each kind of sign-in is said, in place of its id ("adc", "default", "local").
const SIGN_IN = { profile: "AWS CLI profile", keys: "Access keys", role: "Assumed role", default: "This computer's sign-in", adc: "This computer's Google sign-in",
  "service-account": "Service account key", "service-principal": "Service principal", "connection-string": "Storage connection string", local: "This computer's Modal sign-in" };

function ago(t) {
  if (!t) return null;
  const s = (Date.now() - new Date(t).getTime()) / 1000;
  if (!isFinite(s)) return String(t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} days ago`;
}

// The designed forms (captain-forms), never the generic command form.
const task = (id) => () => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: id } });

export default class CaptainCloud extends View {
  init(_props, cx) {
    this.summary = null;
    this.problem = null;
    this.testing = {};
    this.load(cx);
    watchWorkspace(cx, (cx) => this.load(cx));
    // Live, by the one channel (kit/live.js): read again the moment a login, a place to run work or a setting
    // changes. No timer re-reads them.
    watchLive(cx, ["logins", "compute", "settings"], (cx) => this.load(cx));
  }

  load(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/cloud/summary");
      if (r.ok) { this.summary = r.data; this.problem = null; } else this.problem = r;
      cx.notify();
    });
  }

  test(name, cx) {
    this.testing[name] = true;
    cx.notify();
    cx.spawn(async (cx) => {
      await api(`/api/cloud/profiles/${encodeURIComponent(name)}/test`, { method: "POST", body: {}, timeout: 60 });
      this.testing[name] = false;
      this.load(cx);
    });
  }

  login(c, p, active) {
    const t = p.last_test;
    const status = t ? (t.ok ? `Signed in as ${t.identity ?? "you"}. Checked ${ago(t.at) ?? "earlier"}.` : `The last check failed ${ago(t.at) ?? ""}: ${t.error ?? ""}`) : "Not checked yet.";
    // One row: the cloud's own mark, its name, and one plain line under it. The check's result is words, in the
    // failing colour only when it failed. One quiet button.
    return v_flex().id(`cloud-login-${p.name}`).gap(3).pb(10).border_b(1).border_color(c.rule)
      .child(h_flex().gap(8).items_center()
        .child(Brand(c, p.provider, 16))
        .child(div().text_size(13.5).font_semibold().text_color(c.navy).child(SHORT[p.provider] ?? p.label ?? p.provider))
        .child(div().text_size(13).text_color(c.grey500).child(p.name))
        .when(active, (e) => e.child(div().id(`cloud-login-${p.name}-used`).text_size(12).text_color(c.grey500).child("Holds this workspace")))
        .child(div().flex_1())
        .child(Button(c, `test-${p.name}`, this.testing[p.name] ? "Checking..." : "Check", (_e, cx) => this.test(p.name, cx), { small: true, disabled: Boolean(this.testing[p.name]) })))
      .child(div().pl(24).text_size(12.5).text_color(c.grey900).child([SIGN_IN[p.auth] ?? p.auth_label ?? p.auth, p.region, p.account_hint].filter(Boolean).join(", ")))
      .child(div().id(`cloud-login-${p.name}-status`).pl(24).text_size(12.5).text_color(t && !t.ok ? c.fail : c.grey500).child(status));
  }

  render() {
    const c = palette(isDark(theme()));
    const s = this.summary;
    const head = h_flex().h(52).flex_none().px(20).gap(10).items_center().border_b(1).border_color(c.rule)
      .child(div().flex_1().text_size(16).font_semibold().text_color(c.navy).child(`Cloud and compute${s?.workspace ? `: ${s.workspace}` : ""}`))
      .when(Boolean(props()?.overlay), (e) => e.child(div().id("cloud-close").cursor_pointer().text_size(13).text_color(c.grey500).hover((x) => x.text_color(c.navy)).on_click(() => closeOverlay(props().overlay)).child("Close")));
    let body;
    if (!s) body = Problem(c, this.problem ?? { starting: true }, (cx) => this.load(cx));
    else {
      const profiles = s.profiles ?? [];
      const compute = s.compute ?? [];
      body = v_flex().gap(14)
        .child(Card(c, { title: "Storage" },
          h_flex().gap(8).items_center()
            .child(s.storage_provider && s.storage_provider !== "local" ? Brand(c, s.storage_provider, 16) : svg(icon("server")).size(15).text_color(c.navy))
            .child(div().text_size(13.5).font_semibold().text_color(c.navy).child(SHORT[s.storage_provider] ?? s.storage_provider_label ?? "This computer"))
            .child(div().id("cloud-storage").flex_1().min_w(0).truncate().text_size(12.5).text_color(c.grey500).child(String(s.storage ?? "")))
            .when(Boolean(s.storage_profile), (e) => e.child(div().text_size(12).text_color(c.grey500).child(`through ${s.storage_profile}`)))))
        .child(Card(c, { title: `Logins (${profiles.length})`, actions: [Button(c, "cloud-add", "Add a login", task("cloud"), { small: true, kind: "primary" })] },
          ...(profiles.length ? profiles.map((p) => this.login(c, p, p.name === s.active?.name))
            : [Empty(c, "No cloud connected", "Add a login for AWS, Google Cloud, Azure or Modal. Secrets stay in your keychain.")])))
        .child(Card(c, { title: `Compute targets (${compute.length})`, actions: [Button(c, "compute-add", "Add a place to run work", task("compute"), { small: true })] },
          ...(compute.length ? compute.map((t) => h_flex().gap(8).items_center().pb(8).border_b(1).border_color(c.rule)
            .child(t.provider && t.provider !== "local" ? Brand(c, t.provider, 14) : svg(icon("server")).size(14).text_color(c.navy))
            .child(div().text_size(13).font_semibold().text_color(c.navy).child(t.kind === "local" ? "This computer" : t.name))
            .child(div().flex_1().text_size(12).text_color(c.grey500).child([t.kind === "local" ? null : SHORT[t.provider] ?? t.provider, t.region, t.profile && `login ${t.profile}`].filter(Boolean).join(", ")))
            .child(div().text_size(12).text_color(c.grey500).child((t.purposes ?? []).map((x) => ({ backtest: "offline evaluations", serve: "hosting" }[x] ?? x)).join(", "))))
            : [div().text_size(12.5).text_color(c.grey500).child("No compute targets yet.")]),
          ...(s.can_run_in_cloud ? [] : [div().text_size(12.5).text_color(c.grey900).child(`Offline evaluations run on this computer. Add a compute target to run them in ${SHORT[s.active?.provider] ?? "the cloud"}.`)])))
        .children((s.notes ?? []).map((n) => div().text_size(12.5).line_height("18px").text_color(c.grey900).child(String(n))));
    }
    return v_flex().size_full().bg(c.canvas).rounded(RADIUS).font_family(FONT).text_color(c.navy)
      .child(head)
      .child(div().id("cloud-body").flex_1().min_h(0).overflow_y_scroll().p(20).child(body));
  }
}
