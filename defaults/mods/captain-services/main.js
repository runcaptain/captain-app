// CAPTAIN: Settings > Connected services. Every outside service this workspace uses, in one place, from
// GET /api/services (names and kinds only, never a secret): storage, database connections, compute (and which one
// backtests and serving use by default, PUT /api/settings/compute), model registries, MLflow and model keys.
// "Check all" asks each one whether it answers now (?check=true). Edit and Add open the designed forms (captain-forms).
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, props, openOverlay, run } from "herdr";
import { api, watchWorkspace, watchConnections } from "./kit/cst.js";
import { palette, isDark, FONT, MONO } from "./kit/tokens.js";
import { Badge, Button, Skeleton } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand, engineTitle } from "./kit/brands.js";
import { ICONS } from "./kit/icons.js";

const form = (task, values = {}) => openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task, values } });
const PURPOSE = { backtest: "Offline evaluations run on", serve: "Models are served on" };

export default class CaptainServices extends View {
  init(_props, cx) {
    this.data = null;
    this.problem = null;
    this.checking = false;
    this.checked = false;
    this.saving = null;
    this.addOpen = Boolean(props()?.addCompute);
    this.load(cx, Boolean(props()?.check));
    watchWorkspace(cx, (cx) => this.load(cx));
    watchConnections?.(cx, (cx) => this.load(cx));
  }

  load(cx, check = false) {
    if (check) { this.checking = true; cx.notify(); }
    cx.spawn(async (cx) => {
      const r = await api(`/api/services${check ? "?check=true" : ""}`, { timeout: check ? 180 : 30 });
      if (r.ok) { this.data = r.data; this.problem = null; if (check) this.checked = true; }
      else this.problem = "Couldn't load your services. Try again in a moment.";
      this.checking = false;
      cx.notify();
    });
  }

  setDefault(cx, purpose, name) {
    this.saving = purpose;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/settings/compute", { method: "PUT", body: { [purpose]: name } });
      this.saving = null;
      if (!r.ok) this.problem = `Couldn't save where ${purpose === "serve" ? "models are served" : "offline evaluations run"}. ${r.error ?? ""}`.trim();
      this.load(cx);
    });
  }

  ic(name, size, color) {
    // The shared icons by name; anything else is a product (an engine, a cloud, MLflow): its mark.
    if (ICONS.includes(name)) return svg(icon(name)).size(size).flex_none().text_color(color);
    return Brand(this.c, name, size);
  }

  /** A status in words: from the last check, or what's known without one. */
  status(c, st, fallback) {
    if (st) return st.ok
      ? h_flex().gap(4).items_center().text_color(c.good).child(svg(icon("circle-check")).size(12).text_color(c.good)).child(div().truncate().child(st.detail ? `Answered, ${st.detail}` : "Answered"))
      : h_flex().gap(4).items_center().text_color(c.fail).child(svg(icon("circle-x")).size(12).text_color(c.fail)).child(div().truncate().child("Didn't answer. Check its address and sign-in."));
    return div().truncate().text_color(c.grey500).child(fallback ?? (this.checked ? "Not checked" : "Not checked yet. Check all asks each one."));
  }

  row(c, id, { ic, name, sub, status, chips = [], edit }) {
    return h_flex().id(`svc-${id}`).w_full().min_h(48).px(12).py(8).gap(10).items_center().border_b(1).border_color(c.rule)
      .child(div().size(28).flex_none().flex().items_center().justify_center().border_1().border_color(c.rule).child(this.ic(ic, 15, c.navy)))
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(h_flex().gap(6).items_center().min_w(0)
          .child(div().truncate().text_size(13).font_medium().font_family(MONO).text_color(c.navy).child(name))
          .children(chips.map((x) => Badge(c, x, "blue"))))
        .child(h_flex().gap(8).text_size(11.5).min_w(0).text_color(c.grey500)
          .when(!!sub, (e) => e.child(div().flex_none().child(sub)))
          .child(div().flex_1().min_w(0).child(status))))
      .when(!!edit, (e) => e.child(Button(c, `svc-${id}-edit`, "Edit", () => edit(), { small: true })));
  }

  section(c, id, title, about, rows, action) {
    return v_flex().id(`sec-${id}`).w_full().gap(6)
      .child(h_flex().gap(8).items_center()
        .child(v_flex().flex_1().min_w(0)
          .child(div().text_size(14).font_semibold().text_color(c.navy).child(title))
          .child(div().text_size(12).text_color(c.grey500).child(about)))
        .when(!!action, (e) => e.child(action)))
      .child(v_flex().w_full().border_1().border_color(c.rule).bg(c.surface)
        .children(rows.length ? rows : [div().px(12).py(10).text_size(12.5).text_color(c.grey500).child("None yet.")]));
  }

  compute(c, comp) {
    const targets = comp.targets ?? [];
    const pick = (purpose) => {
      const able = targets.filter((t) => (t.purposes ?? []).includes(purpose));
      return h_flex().id(`pick-${purpose}`).flex_wrap().gap(6).items_center()
        .child(div().w(170).flex_none().text_size(12.5).text_color(c.grey900).child(PURPOSE[purpose]))
        .when(!able.length, (e) => e.child(div().text_size(12).text_color(c.grey500).child(purpose === "serve" ? "Nothing that can serve yet. Add compute that serves models." : "Nothing that can run offline evaluations yet.")))
        .children(able.map((t) => {
          const on = comp.defaults?.[purpose] === t.name;
          return h_flex().id(`pick-${purpose}-${t.name}`).h(28).px(10).gap(6).items_center().cursor_pointer().border_1()
            .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5).font_family(MONO)
            .on_click((_e, cx) => { if (!on && !this.saving) this.setDefault(cx, purpose, t.name); })
            .child(t.name);
        }))
        .when(this.saving === purpose, (e) => e.child(div().text_size(12).text_color(c.grey500).child("Saving...")));
    };
    const rows = targets.map((t) => this.row(c, `compute-${t.name}`, {
      ic: "boxes", name: t.name, sub: [t.label, t.region].filter(Boolean).join(", "),
      chips: (t.default_for ?? []).map((p) => (p === "serve" ? "default for serving" : "default for offline evaluations")),
      status: this.status(c, t.status, t.live_tested ? null : "Not yet tried on a real account"),
      edit: () => form("compute", { kind: t.kind, name: t.name }),
    }));
    const add = v_flex().id("add-compute").w_full().border_1().border_color(c.rule).bg(c.surface)
      .children((comp.kinds ?? []).map((k) => h_flex().id(`add-${k.kind}`).w_full().min_h(40).px(12).py(6).gap(10).items_center().cursor_pointer()
        .border_b(1).border_color(c.rule).hover((s) => s.bg(c.surface2))
        .on_click(() => form("compute", { kind: k.kind }))
        .child(this.ic("boxes", 14, c.grey500))
        .child(v_flex().flex_1().min_w(0)
          .child(div().text_size(12.5).text_color(c.navy).child(k.label ?? k.kind))
          .child(div().text_size(11).text_color(c.grey500).child([(k.purposes ?? []).map((p) => (p === "serve" ? "serves models" : "runs offline evaluations")).join(" and "),
            k.live_tested ? null : "Not yet tried on a real account"].filter(Boolean).join(". ")))
        )
        .child(this.ic("plus", 13, c.blue))));
    return v_flex().gap(10)
      .child(this.section(c, "compute", "Compute", "Where Captain's work runs. Each offline evaluation or model version can name its own, otherwise it uses these.", rows,
        Button(c, "add-compute-toggle", this.addOpen ? "Close" : "Add compute", (_e, cx) => { this.addOpen = !this.addOpen; cx.notify(); }, { small: true })))
      .when(this.addOpen, (e) => e.child(add))
      .child(v_flex().gap(8).p(12).border_1().border_color(c.rule).child(pick("backtest")).child(pick("serve")));
  }

  render(cx) {
    const t = theme();
    const c = palette(isDark(t));
    this.c = c;
    const d = this.data;
    const root = v_flex().id("services").size_full().bg(c.surface2).text_color(c.navy).font_family(FONT);
    const head = h_flex().flex_none().gap(10).items_center().px(20).py(14).border_b(1).border_color(c.rule).bg(c.surface)
      .child(v_flex().flex_1().min_w(0)
        .child(div().text_size(17).font_semibold().child("Connected services"))
        .child(div().text_size(12.5).text_color(c.grey500).child("Everything this workspace connects to. Names and kinds only: no secret is ever shown here.")))
      .child(Button(c, "check-all", this.checking ? "Checking..." : "Check all", (_e, cx) => { if (!this.checking) this.load(cx, true); }, { kind: "primary", small: true }));
    if (!d) return root.child(head).child(this.problem
      ? div().p(20).text_size(12.5).text_color(c.fail).child(this.problem)
      : div().p(20).child(Skeleton(c, 8)));
    const conns = (d.connections ?? []).map((x) => this.row(c, `conn-${x.name}`, {
      ic: x.kind ?? "server", name: x.name,
      sub: [engineTitle(x.kind), x.writable ? "can write" : "read only", x.network && x.network !== "direct" ? `through ${x.network}` : null].filter(Boolean).join(", "),
      status: x.error ? div().text_color(c.fail).child("Couldn't read its settings.") : this.status(c, x.status),
      edit: () => form("database", { name: x.name, kind: x.kind }),
    }));
    const st = d.storage ?? {};
    const mf = d.mlflow ?? {};
    return root.child(head).child(v_flex().id("services-body").flex_1().min_h(0).overflow_y_scrollbar().p(20).gap(22)
      .when(!!this.problem, (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.problem)))
      .child(this.section(c, "storage", "Storage", "Where this workspace keeps its datasets, versions and results.", [this.row(c, "storage", {
        ic: String(st.uri ?? "").startsWith("s3://") ? "aws" : "hard-drive", name: st.workspace ?? "workspace", sub: st.uri,
        status: div(), edit: () => run("mod.open", { name: "captain-cloud" }),
      })]))
      .child(this.section(c, "connections", "Database connections", "Databases Captain reads from (and writes to, where allowed).", conns,
        Button(c, "add-database", "Connect a database", () => form("database"), { small: true })))
      .child(this.compute(c, d.compute ?? {}))
      .child(this.section(c, "registries", "Model registries", "Where your trained models live, by brand.", (d.registries ?? []).map((r, i) => this.row(c, `reg-${i}`, {
        ic: r.provider ?? r.kind ?? "cloud", name: r.name ?? r.uri ?? `registry ${i + 1}`, sub: [r.kind, r.brand].filter(Boolean).join(", "),
        status: div().text_color(c.grey500).child(r.uri ?? ""),
      }))))
      .child(this.section(c, "mlflow", "MLflow", "Runs and registered models from your MLflow server, shown beside Captain's.", [this.row(c, "mlflow", {
        ic: "mlflow", name: mf.configured ? "MLflow" : "MLflow isn't connected", sub: mf.configured ? (mf.uri ?? (mf.databricks_connection ? `through ${mf.databricks_connection}` : null)) : null,
        status: mf.configured ? this.status(c, mf.status) : div().text_color(c.grey500).child("Connect it to see its runs and models here."),
        edit: () => form("mlflow"),
      })]))
      .child(this.section(c, "keys", "Model keys", "Keys for the models that judge, explain and grade. Read from this computer: your shell or keychain.", (d.model_keys ?? []).map((k) => this.row(c, `key-${k.name}`, {
        ic: "bot", name: k.label, sub: k.name,
        status: k.set ? div().text_color(c.good).child("Set") : div().text_color(c.grey500).child(`Not set. Add ${k.name} to your shell or keychain.`),
      })))));
  }
}
