// CAPTAIN: Deployments: every deployed model across projects and clouds, as each cloud reports it. The place to
// see what is deployed, where, how much traffic it takes and what it costs, and to stop, start, go back a version
// or remove one. GET /api/deployed asks each connected cloud and corrects Captain's records from the answer, so
// this screen and the cloud cannot disagree. Opening a row shows that model's page (model.js).
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, props, run, setShared } from "herdr";
import { api, watchWorkspace } from "./kit/cst.js";
import { palette, isDark, FONT } from "./kit/tokens.js";
import { Button, Empty, Problem, plainError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { ago, money, ms, count, Spark } from "./words.js";
import { ModelPage } from "./model.js";

const UP = ["running", "asleep", "starting", "unknown"];
const SHOW = [["running", "Running"], ["stopped", "Stopped"], ["all", "All"]];

export default class CaptainDeployments extends View {
  init(_props, cx) {
    this.d = null;            // GET /api/deployed
    this.problem = null;
    this.created = null;      // GET /api/deployed/created
    this.nums = {};           // project -> the last 24 hours of numbers
    this.show = "running";
    this.cloud = null;        // a cloud to show only, or null for all
    this.project = null;
    this.ask = null;          // an open question: {kind, id, text, yes}
    this.busy = {};           // row id -> what is being done to it, in words
    this.note = null;         // what the last action said
    this.projects = null;     // for "Link to a project"
    this.page = null;         // the model page, when a row is open
    // The picture check hands the screen its rows (props.demo), so every state can be drawn with nothing deployed
    // and no cloud asked. A screen drawn this way does nothing: its buttons reach no service.
    this.demo = props()?.demo ?? null;
    if (this.demo) {
      this.d = this.demo.deployed;
      this.created = this.demo.created ?? null;
      this.nums = this.demo.numbers ?? {};
      this.show = this.demo.show ?? "running";
      if (this.demo.open) this.openRow(this.d.rows.find((r) => r.id === this.demo.open), cx, this.demo.part ?? null);
      return;
    }
    this.load(cx);
    watchWorkspace(cx, (cx) => { this.page = null; this.load(cx); });
    cx.timer.every(15000, (cx) => this.load(cx));
  }

  load(cx, fresh = false) {
    if (this.demo) return;
    cx.spawn(async (cx) => {
      const r = await api(`/api/deployed${fresh ? "?fresh=true" : ""}`, { timeout: 60 });
      if (r.ok) { this.d = r.data; this.problem = null; } else if (!this.d) this.problem = r;
      cx.notify();
      if (!r.ok) return;
      const made = await api("/api/deployed/created");
      if (made.ok) this.created = made.data;
      // Traffic and cost for each row that belongs to a project. The services keep each answer for a minute.
      for (const row of r.data.rows.filter((x) => x.link !== "unlinked" && x.project)) {
        const n = await api(`/api/projects/${encodeURIComponent(row.project)}/deployed/numbers?range=24h&env=${encodeURIComponent(row.env ?? "staging")}`, { timeout: 60 });
        if (n.ok) this.nums[row.id] = n.data;
        cx.notify();
      }
    });
  }

  // ---- doing things to a deployment -----------------------------------------------------------------

  async act(cx, row, words, call) {
    this.ask = null;
    if (this.demo) return cx.notify();
    this.busy[row.id] = words;
    this.note = null;
    cx.notify();
    const r = await call();
    delete this.busy[row.id];
    this.note = r.ok ? (r.data?.text ?? null) : plainError(r.error, "That did not work.");
    this.load(cx, true);
  }

  stop(row, cx) {
    cx.spawn((cx) => this.act(cx, row, "Stopping", () => api(`/api/projects/${encodeURIComponent(row.project)}/unstage`, { method: "POST", body: { env: row.env }, timeout: 600 })));
  }

  start(row, cx) {
    const cost = this.nums[row.id]?.cost?.rate_usd;
    this.ask = { id: row.id, yes: "Start it again",
      text: `Start ${row.model_version} again on ${row.where}?` + (cost ? ` It costs about ${money(cost)} an hour while it is up.` : ""),
      go: (cx) => cx.spawn((cx) => this.act(cx, row, "Starting", () => api(`/api/projects/${encodeURIComponent(row.project)}/deploy`,
        { method: "POST", body: { host: row.cloud, login: row.login, candidate: row.model_version, env: row.env, confirm: true }, timeout: 60 }))) };
    cx.notify();
  }

  remove(row, cx) {
    const found = row.link === "unlinked";
    this.ask = { id: row.id, yes: "Remove it from the cloud",
      text: found ? `Remove ${row.name} from your ${row.cloud_label} account? It cannot be undone.`
        : `Remove ${row.project_name}'s model from ${row.cloud_label}? It stops answering, and what Captain made for it there is deleted. It cannot be undone.`,
      go: (cx) => cx.spawn((cx) => this.act(cx, row, "Removing", () => found
        ? api("/api/deployed/remove", { method: "POST", body: { id: row.id, confirm: true }, timeout: 600 })
        : api(`/api/projects/${encodeURIComponent(row.project)}/deploy?env=${encodeURIComponent(row.env ?? "staging")}`, { method: "DELETE", timeout: 600 }))) };
    cx.notify();
  }

  linkTo(row, cx) {
    this.ask = { id: row.id, pick: true, text: `Which project should ${row.name} belong to?` };
    cx.notify();
    if (!this.projects) cx.spawn(async (cx) => { const r = await api("/api/projects"); this.projects = r.ok ? (r.data.projects ?? r.data.rows ?? r.data ?? []) : []; cx.notify(); });
  }

  removeSetup(place, cx) {
    const row = { id: `setup-${place.cloud}` };
    this.ask = { id: row.id, yes: "Remove everything",
      text: `Remove everything Captain created in your ${place.cloud_label} account? The next deploy there makes it again, which takes a few minutes.`,
      go: (cx) => cx.spawn((cx) => this.act(cx, row, "Removing", () => api(`/api/serving/setup?cloud=${place.cloud}`, { method: "DELETE", timeout: 600 }))) };
    cx.notify();
  }

  openProject(row) {
    setShared("captain.scope", { kind: "project", id: row.project, label: row.project_name ?? row.project });
    return run("layout.select", { stack: props()?.stack ?? "sections", child: "project" });
  }

  // ---- drawing ---------------------------------------------------------------------------------------

  // An open question takes the screen's one filled button.
  question(c, cx) {
    const a = this.ask;
    const box = v_flex().id("dep-ask").gap(8).p(12).border_1().border_color(c.rule).bg(c.surface)
      .child(div().text_size(13).line_height("19px").text_color(c.navy).child(a.text));
    if (a.pick) {
      const list = this.projects;
      return box.child(h_flex().gap(8).flex_wrap()
        .children((list ?? []).map((p) => Button(c, `dep-link-${p.id}`, p.name ?? p.id, (_e, cx) => cx.spawn((cx) => this.act(cx, { id: a.id }, "Linking",
          () => api("/api/deployed/link", { method: "POST", body: { id: a.id, project: p.id } }))), { small: true })))
        .when(list && !list.length, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("There are no projects yet. Make one first.")))
        .when(!list, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("Reading your projects...")))
        .child(Button(c, "dep-ask-no", "Cancel", (_e, cx) => { this.ask = null; cx.notify(); }, { small: true })));
    }
    return box.child(h_flex().gap(8)
      .child(Button(c, "dep-ask-yes", a.yes, (_e, cx) => a.go(cx), { kind: "primary", small: true }))
      .child(Button(c, "dep-ask-no", "Cancel", (_e, cx) => { this.ask = null; cx.notify(); }, { small: true })));
  }

  row(c, r) {
    const n = this.nums[r.id];
    const s = n?.summary ?? {};
    const busy = this.busy[r.id];
    const up = UP.includes(r.state);
    const found = r.link === "unlinked";
    const wrong = r.state === "failed" || r.state === "gone";
    // The last hour, from the newest twelve five-minute counts of the day's numbers.
    const hour = (n?.buckets ?? []).slice(-12).map((b) => b.requests ?? 0);
    const buttons = busy ? [div().id(`dep-busy-${r.id}`).text_size(12.5).text_color(c.grey500).child(`${busy}...`)]
      : found ? [Button(c, `dep-link-${r.id}`, "Link to a project", (_e, cx) => this.linkTo(r, cx), { small: true }),
        Button(c, `dep-remove-${r.id}`, "Remove from the cloud", (_e, cx) => this.remove(r, cx), { small: true })]
        : r.link === "unknown" ? [Button(c, `dep-open-${r.id}`, "Open", (_e, cx) => this.openRow(r, cx), { small: true })]
          : up ? [Button(c, `dep-open-${r.id}`, "Open", (_e, cx) => this.openRow(r, cx), { small: true }),
            Button(c, `dep-stop-${r.id}`, "Stop", (_e, cx) => this.stop(r, cx), { small: true }),
            Button(c, `dep-back-${r.id}`, "Go back to an earlier version", (_e, cx) => this.openRow(r, cx, "versions"), { small: true })]
            : [Button(c, `dep-open-${r.id}`, "Open", (_e, cx) => this.openRow(r, cx), { small: true }),
              ...(r.model_version ? [Button(c, `dep-start-${r.id}`, "Start again", (_e, cx) => this.start(r, cx), { small: true })] : []),
              Button(c, `dep-remove-${r.id}`, "Remove from the cloud", (_e, cx) => this.remove(r, cx), { small: true })];
    const when = ago(r.state === "stopped" || r.state === "gone" ? r.stopped_at ?? r.since : r.since);
    const who = r.by ? [`Deployed by ${r.by}`, when].filter(Boolean).join(", ") : when ? `Made ${when}` : null;
    const numbers = found || !n ? null : [
      s.requests != null ? `${count(s.requests)} request${s.requests === 1 ? "" : "s"} in 24 hours` : null,
      s.requests ? (s.errors ? `${count(s.errors)} failed` : "none failed") : null,
      s.p50_ms != null ? `typically ${ms(s.p50_ms)}` : null,
    ].filter(Boolean).join(", ");
    const cost = found || !n?.cost ? null : [
      n.cost.hourly_usd ? `${money(n.cost.hourly_usd)} an hour` : up ? null : "Costs nothing now",
      n.cost.month_usd != null ? `${money(n.cost.month_usd)} this month` : null,
    ].filter(Boolean).join(", ");
    return v_flex().id(`dep-row-${r.id}`).gap(6).py(12).border_b(1).border_color(c.rule)
      .child(h_flex().gap(10).items_center()
        .child(r.cloud === "local" ? svg(icon("monitor")).size(16).text_color(c.navy) : Brand(c, r.cloud, 16))
        .child(div().id(`dep-name-${r.id}`).text_size(14).font_semibold().text_color(c.navy).child(found ? r.name : (r.project_name ?? r.project)))
        .child(div().min_w(0).truncate().text_size(13).text_color(c.grey500).child(found ? "" : (r.model_version ?? "Model version not known")))
        .child(div().flex_1())
        .when(hour.length > 1 && !found, (e) => e.child(Spark(c, `dep-spark-${r.id}`, hour))))
      .child(div().id(`dep-state-${r.id}`).text_size(13).line_height("19px").text_color(wrong ? c.fail : c.navy).child(r.state_text))
      .child(div().text_size(12.5).line_height("18px").text_color(c.grey500)
        .child([r.where, numbers, cost, who].filter(Boolean).join(". ") + "."))
      .children((r.corrected ?? []).map((x, i) => div().id(`dep-fixed-${r.id}-${i}`).text_size(12.5).line_height("18px").text_color(c.grey500).child(x)))
      .when(this.ask?.id === r.id, (e) => e.child(this.question(c)))
      .when(this.ask?.id !== r.id, (e) => e.child(h_flex().gap(8).flex_wrap().children(buttons)));
  }

  openRow(r, cx, part = null) {
    this.page = new ModelPage(r, part, () => { this.page = null; }, this.demo?.model ?? null);
    this.page.load(cx);
    cx.notify();
  }

  setup(c, place) {
    const id = `setup-${place.cloud}`;
    return v_flex().id(`dep-setup-${place.cloud}`).gap(6).py(12).border_b(1).border_color(c.rule)
      .child(h_flex().gap(10).items_center()
        .child(Brand(c, place.cloud, 16))
        .child(div().text_size(14).font_semibold().text_color(c.navy).child(place.cloud_label))
        .child(div().text_size(13).text_color(c.grey500).child([place.region, place.login].filter(Boolean).join(", "))))
      .children(place.things.map((t, i) => h_flex().id(`dep-thing-${place.cloud}-${i}`).gap(8).items_start()
        .child(div().w(260).flex_none().truncate().text_size(12.5).text_color(c.navy).child(t.name))
        .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(c.grey500).child(t.what_for))))
      .when(Boolean(place.in_use_by?.length), (e) => e.child(div().text_size(12.5).text_color(c.grey500)
        .child(`In use by ${place.in_use_by.join(", ")}. Stop that model first to remove these.`)))
      .when(this.ask?.id === id, (e) => e.child(this.question(c)))
      .when(this.ask?.id !== id && place.can_remove, (e) => e.child(h_flex().child(this.busy[id]
        ? div().text_size(12.5).text_color(c.grey500).child("Removing...")
        : Button(c, `dep-setup-remove-${place.cloud}`, "Remove everything", (_e, cx) => this.removeSetup(place, cx), { small: true }))));
  }

  filters(c, rows) {
    const clouds = [...new Set(rows.map((r) => r.cloud))];
    const projects = [...new Map(rows.filter((r) => r.project && r.link !== "unlinked").map((r) => [r.project, r.project_name ?? r.project])).entries()];
    const pick = (id, label, on, click) => div().id(id).h(26).px(10).flex().items_center().text_size(12.5).cursor_pointer()
      .border_1().border_color(on ? c.navy : c.rule).text_color(on ? c.navy : c.grey500).when(on, (e) => e.font_semibold())
      .hover((s) => s.text_color(c.navy)).on_click(click).child(label);
    const set = (k, v) => (_e, cx) => { this[k] = v; cx.notify(); };
    // One line for each question: which state, which cloud, which project. A line with one answer is left out.
    const line = (...kids) => h_flex().gap(6).flex_wrap().items_center().children(kids);
    return v_flex().id("dep-filters").gap(6)
      .child(line(...SHOW.map(([id, label]) => pick(`dep-show-${id}`, label, this.show === id, set("show", id)))))
      .when(clouds.length > 1, (e) => e.child(line(pick("dep-cloud-all", "Every cloud", this.cloud === null, set("cloud", null)),
        ...clouds.map((k) => pick(`dep-cloud-${k}`, rows.find((r) => r.cloud === k).cloud_label, this.cloud === k, set("cloud", k))))))
      .when(projects.length > 1, (e) => e.child(line(pick("dep-project-all", "Every project", this.project === null, set("project", null)),
        ...projects.map(([id, name]) => pick(`dep-project-${id}`, name, this.project === id, set("project", id))))));
  }

  render(cx) {
    const c = palette(isDark(theme()));
    const frame = (body) => v_flex().id("deployments").size_full().bg(c.canvas).font_family(FONT).text_color(c.navy).child(body);
    if (this.page) return frame(this.page.render(c, cx));
    const d = this.d;
    if (!d) return frame(div().p(24).child(Problem(c, this.problem ?? { starting: true }, (cx) => this.load(cx))));
    const rows = d.rows.filter((r) => (this.show === "all" || (UP.includes(r.state) || r.link === "unlinked") === (this.show === "running"))
      && (!this.cloud || r.cloud === this.cloud) && (!this.project || r.project === this.project));
    const notAsked = d.asked.filter((a) => !a.ok);
    const places = this.created?.places ?? [];
    const head = h_flex().h(56).flex_none().px(24).gap(12).items_center().border_b(1).border_color(c.rule)
      .child(div().text_size(18).font_semibold().text_color(c.navy).child("Deployments"))
      // With nothing deployed the page itself says so: the line beside the title would only repeat it.
      .child(div().id("dep-summary").flex_1().min_w(0).truncate().text_size(13).text_color(c.grey500).child(d.rows.length ? d.text : ""))
      .child(Button(c, "dep-refresh", "Ask the clouds again", (_e, cx) => this.load(cx, true), { small: true }));
    const body = v_flex().gap(18)
      .when(Boolean(this.note), (e) => e.child(div().id("dep-note").text_size(13).line_height("19px").text_color(c.navy).child(this.note)))
      .children(notAsked.map((a, i) => div().id(`dep-not-asked-${i}`).text_size(13).line_height("19px").text_color(c.fail)
        .child(`Captain could not ask ${a.cloud_label}${a.login ? ` (${a.login})` : ""} just now: ${plainError(a.error, "no reason given")}. What is shown for it is Captain's own record.`)))
      .child(d.rows.length ? this.filters(c, d.rows) : div())
      .child(v_flex().id("dep-list")
        .children(rows.map((r) => this.row(c, r)))
        .when(!d.rows.length, (e) => e.child(Empty(c, "Nothing is deployed", "Deploy a model from a project's page, and it shows here with its traffic and cost.",
          Button(c, "dep-go-projects", "Open a project to deploy a model", () => run("layout.select", { stack: props()?.stack ?? "sections", child: "project" }), { kind: "primary" }))))
        .when(Boolean(d.rows.length) && !rows.length, (e) => e.child(div().id("dep-none-shown").py(16).text_size(13).text_color(c.grey500)
          .child(this.show === "running" ? "Nothing is running. Stopped ones are under Stopped." : "Nothing matches. Change what is shown above."))))
      .when(Boolean(places.length), (e) => e.child(v_flex().id("dep-created")
        .child(div().text_size(15).font_semibold().text_color(c.navy).child("What Captain created in your accounts"))
        .child(div().pt(3).text_size(12.5).line_height("18px").text_color(c.grey500).child("Made by the first deploy to each cloud. Remove them when you no longer deploy there."))
        .children(places.map((p) => this.setup(c, p)))));
    return frame(v_flex().size_full().child(head).child(div().id("dep-body").flex_1().min_h(0).overflow_y_scroll().px(24).py(18).child(body)));
  }
}
