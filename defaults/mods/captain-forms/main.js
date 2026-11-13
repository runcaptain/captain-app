// CAPTAIN: a designed form for one task in plain words (tasks.js), or the list of
// tasks (the New menu). Opened as an overlay:
//   props {task: "dataset"}   that task's form
//   props {menu: true}        the New menu: every task, icon and one line each
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input, TextareaState, Textarea } from "gpui-base";
import { theme, props, closeOverlay, openOverlay, notify, shared, setShared, run as runCommand } from "herdr";
import { palette, isDark, FONT, RADIUS } from "./kit/tokens.js";
import { Button, Badge, ServiceError } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand } from "./kit/brands.js";
import { TASKS } from "./tasks.js";
import { idFrom, isId, sayError, FALLBACK } from "./kit/checks.js";
import { api } from "./kit/cst.js";
import { recordsChanged } from "./kit/cst.js";
import { watchView, publishView, safeFields } from "./kit/view.js";
import { costState, loadPlaces, refreshEstimate, needsConfirm, costSlice, RunsOn, LOCAL } from "./kit/cost.js";

const IMPORT = { mod: "captain-import", kind: "dialog", width: 900, height: 640, props: {} };

export default class CaptainForms extends View {
  init(_props, cx) {
    // The New menu; the test hook's entry "menu" opens it too.
    const previewMenu = !props()?.task && !props()?.menu && (() => { const p = shared("captain.forms.preview"); return Array.isArray(p) && p[(shared("captain.forms.preview.i") ?? 0) % p.length] === "menu"; })();
    if (previewMenu) setShared("captain.forms.preview.i", (shared("captain.forms.preview.i") ?? 0) + 1);
    this.isMenu = Boolean(props()?.menu) || previewMenu;
    if (this.isMenu) { this.sel = 0; this.menuFocus = cx.focus_handle(); this.menuFocus.focus(); }
    this.values = {};
    // Test hook: opened bare (`captain-app do mod.open`), shared "captain.forms.preview" picks a form,
    // so a test copy can show any form on screen. People never set it.
    // A list steps through its entries, one per open; an entry may preset values.
    let want = props()?.task ?? null;
    if (!want && !this.isMenu) {
      let p = shared("captain.forms.preview");
      if (Array.isArray(p)) {
        const i = shared("captain.forms.preview.i") ?? 0;
        setShared("captain.forms.preview.i", i + 1);
        p = p[i % p.length];
      }
      want = typeof p === "string" ? p : p?.task;
      Object.assign(this.values, p?.values ?? {});
    }
    // A caller may preset values too: openOverlay({mod: "captain-forms", props: {task: "database", values: {kind: "posthog"}}}).
    Object.assign(this.values, props()?.values ?? {});
    this.task = TASKS.find((t) => t.id === want) ?? null;
    this.states = {};
    this.options = {};
    this.ctx = { args: props()?.args ?? {} };
    this.errors = {};
    // Checked as the person types (checks.js): what each field's own check says, what the services say about it,
    // which fields have been touched, and the id Captain makes from each name.
    this.touched = {}; this.sig = {}; this.changedAt = {}; this.tried = false;
    this.asked = {}; this.checking = {}; this.said = {}; this.names = {}; this.idOpen = {}; this.idStates = {};
    this.serverErrors = {}; this.formError = null;
    // Where the work runs and what it costs there (kit cost.js), for a task with a "runs-on" field.
    this.cost = costState(this.values.compute || LOCAL);
    if (this.values.compute) this.cost.picked = true; // a place handed in with the form stays, whatever the default
    if (this.task) this.start(cx);
  }

  close() { if (this.task) publishView("dialog", null); closeOverlay(props().overlay); }

  start(cx) {
    // The form open over the page and what is filled in, for agents (cst app context). A secret field reads
    // "(hidden)": its value never leaves this form.
    watchView(cx, "dialog", () => ({ mod: "captain-forms", form: this.task.id, title: this.task.label,
      fields: safeFields(this.values, this.shown().filter((f) => f.kind === "secret").map((f) => f.key)),
      // The place the work is set to run and its estimate, so an agent can say what starting it would cost.
      ...(this.runsOn() ? { runs_on: costSlice(this.cost) } : {}) }));
    if (this.task.fields.some((f) => f.kind === "runs-on")) {
      cx.spawn(async (cx) => { await loadPlaces(this.cost); cx.notify(); });
      // The estimate follows what is filled in: asked again only when the place or the size changed.
      cx.timer.every(500, (cx) => this.priceNow(cx));
    }
    // Every field is looked at again a few times a second: what changed is marked touched, and a check that needs
    // the services runs by itself once the typing pauses. Nothing waits for the button.
    cx.timer.every(250, (cx) => this.watch(cx));
    // A default can depend on what the caller preset (New candidate opens on your own command when a ranker is named).
    for (const f of this.task.fields) if (f.default !== undefined) this.values[f.key] ??= typeof f.default === "function" ? f.default(this.values) : f.default;
    // Text inputs keep state, which gpui-kit only lets a view create outside render.
    this.ensureStates();
    cx.spawn(async (cx) => {
      // A task's load reads what the caller preset (a project page's pins); ctx.args is what it was opened on.
      this.ctx = { args: props()?.args ?? {}, ...(this.task.load ? await this.task.load(this.values) : {}) };
      // What load found fills fields nobody has filled yet (a ranker's current training command).
      for (const [k, v] of Object.entries(this.ctx.defaults ?? {})) {
        if (v === undefined || v === null || v === "" || (this.values[k] !== undefined && this.values[k] !== "")) continue;
        this.values[k] = v;
        this.states[k]?.set_value(String(v));
      }
      await this.loadOptions(cx);
    });
  }

  // ---- checked as the person types ---------------------------------------------------------------------------

  /** What is in a field now. A text box is read from the box itself. */
  valueOf(f) {
    if (f.kind === "rows") return (this.values[f.key] ?? []).map((r) => ({ ...r, label: this.rowStates?.[r.uid]?.value?.() ?? r.label }));
    return this.values[f.key];
  }

  /** The problem a field has by itself, with nothing asked of the services: a sentence, or null. Pure. */
  problemOf(f) {
    if (f.kind === "note" || f.kind === "runs-on") return null;
    const v = this.valueOf(f);
    if (f.kind === "rows") return f.check ? f.check(v, this.values, this.ctx) ?? null : null;
    const empty = v === undefined || v === null || String(v).trim() === "";
    if (empty) return f.required ? f.missing ?? (f.name ? "Give it a name." : `${f.label} is needed.`) : null;
    if (f.name && this.idOpen[f.key]) {
      const id = this.idStates[f.key]?.value?.() ?? "";
      if (!isId(id)) return "Captain's own name for it uses lower-case letters, digits and hyphens, like gpu-cloud.";
    }
    return f.check ? f.check(v, this.values, this.ctx) ?? null : null;
  }

  /** The id Captain will keep for a name field: the person's own if they opened it, else the next free one the
   *  services offered, else the one made here from the words. */
  idOf(f) {
    if (this.idOpen[f.key] && isId(this.idStates[f.key]?.value?.() ?? "")) return this.idStates[f.key].value();
    const n = this.names[f.key];
    const made = idFrom(this.values[f.key], FALLBACK[f.name] ?? "item");
    return n && n.id === made ? n.next ?? n.id : made;
  }

  /** What is shown under a field: what came back for it after the button, else its own problem once it has been
   *  touched, else what the services said about it. */
  shownProblem(f) {
    if (this.serverErrors[f.key]) return this.serverErrors[f.key];
    const own = this.problemOf(f);
    if (own && (this.touched[f.key] || this.tried)) return own;
    const said = this.said[f.key];
    return said?.problem ? said.text : null;
  }

  /** Why the main button is off: the first thing in the way, as one sentence. Null when nothing is. */
  blocked() {
    for (const f of this.shown()) {
      const own = this.problemOf(f);
      if (own) return own;
      if (this.said[f.key]?.problem) return this.said[f.key].text;
    }
    const b = this.task.blocked?.(this.values, this.ctx);
    return b ? (typeof b === "string" ? b : "Something above still needs choosing.") : null;
  }

  /** A few times a second: mark what changed as touched, and ask the services what only they know (is this name
   *  free, is this sign-in there), once the typing has paused. */
  watch(cx) {
    if (!this.task || this.busy || this.done) return;
    let redraw = false;
    const now = Date.now();
    for (const f of this.shown()) {
      if (f.kind === "note" || f.kind === "runs-on") continue;
      const sig = JSON.stringify([this.valueOf(f) ?? null, this.idOpen[f.key] ? this.idStates[f.key]?.value?.() : null]);
      if (this.sig[f.key] === undefined) this.sig[f.key] = sig;
      else if (this.sig[f.key] !== sig) {
        this.sig[f.key] = sig; this.touched[f.key] = true; this.changedAt[f.key] = now;
        if (this.serverErrors[f.key]) delete this.serverErrors[f.key];
        this.formError = null;
        redraw = true;
      }
      const ask = f.name ? "name" : f.checkAsync ? "check" : f.status ? "status" : null;
      if (!ask) continue;
      const v = this.values[f.key];
      const on = ask === "status" ? JSON.stringify(f.statusOn ? f.statusOn(this.values) : v ?? null) : String(v ?? "");
      const due = this.asked[f.key]?.on !== on || (f.recheck && now - (this.asked[f.key]?.at ?? 0) > f.recheck && !this.checking[f.key]);
      if (!due || now - (this.changedAt[f.key] ?? 0) < 400) continue;
      if (ask !== "status" && String(v ?? "").trim() === "") { this.asked[f.key] = { on, at: now }; if (this.said[f.key] || this.names[f.key]) { delete this.said[f.key]; delete this.names[f.key]; redraw = true; } continue; }
      const first = this.asked[f.key]?.on !== on;
      this.asked[f.key] = { on, at: now };
      if (first) { this.checking[f.key] = true; redraw = true; }
      cx.spawn(async (cx) => {
        let said = null, name = null;
        try {
          if (ask === "name") {
            const r = await api(`/api/names/propose?label=${encodeURIComponent(String(v))}&kind=${encodeURIComponent(f.name)}`, { scoped: false });
            // Taken with no other id offered is a problem to fix. Taken with the next free one offered is not.
            if (r.ok && r.data?.id) { name = r.data; said = { text: r.data.text, problem: Boolean(r.data.taken) && r.data.next === r.data.id }; }
          } else if (ask === "check") {
            const t = await f.checkAsync(v, this.values, this.ctx);
            said = t ? { text: t, problem: true } : null;
          } else {
            const t = await f.status(this.ctx, this.values);
            said = t?.text ? { text: t.text, problem: Boolean(t.problem) } : null;
          }
        } catch { said = null; }
        if (this.asked[f.key]?.on !== on) return; // the field moved on while this was asked
        this.checking[f.key] = false;
        if (said) this.said[f.key] = said; else delete this.said[f.key];
        if (ask === "name") { if (name) this.names[f.key] = name; else delete this.names[f.key]; }
        cx.notify();
      });
    }
    if (redraw) cx.notify();
  }

  /** Under a name field: the id Captain will keep, and a way to change it for those who want to. */
  nameLine(c, f) {
    const v = String(this.values[f.key] ?? "").trim();
    if (!v) return null;
    const open = Boolean(this.idOpen[f.key]);
    const said = this.said[f.key];
    // A name that is taken with nothing else offered is said once, as the field's problem, not here as well.
    const text = this.checking[f.key] && !this.names[f.key] ? "Checking..." : open ? "Captain will call it:" : said?.problem ? `Captain would call it ${this.idOf(f)}.` : said?.text ?? `Captain will call it ${this.idOf(f)}.`;
    return h_flex().id(`f-${f.key}-id`).gap(8).items_center().flex_wrap()
      .child(div().id(`f-${f.key}-id-text`).text_size(12).line_height("17px").text_color(c.grey500).child(text))
      .when(open, (e) => e.child(div().id(`f-${f.key}-id-box`).w(200).h(26).px(8).flex().items_center().bg(c.surface).border_1().border_color(c.grey300).cursor_text()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
        .child(Input.new(this.idStates[f.key]).w_full().text_size(12.5).text_color(c.navy))))
      .child(div().id(`f-${f.key}-id-change`).text_size(12).text_color(c.blue).cursor_pointer()
        .on_click((_e, cx) => {
          if (open) { this.idOpen[f.key] = false; } else {
            const st = this.idStates[f.key] ?? InputState.new({ placeholder: "" });
            st.set_value(this.idOf(f));
            this.idStates[f.key] = st; this.idOpen[f.key] = true;
          }
          cx.notify();
        }).child(open ? "Use Captain's" : "Change"));
  }

  /** The "runs-on" field shown now, if the task has one. */
  runsOn() { return this.task ? this.shown().find((f) => f.kind === "runs-on") ?? null : null; }

  priceNow(cx) {
    const f = this.runsOn();
    const want = f ? f.cost?.(this.values, this.ctx) ?? null : null;
    const fixed = f && (typeof f.fixed === "function" ? f.fixed(this.values, this.ctx) : f.fixed);
    if (fixed && this.cost.where !== LOCAL) this.cost.where = LOCAL;
    this.values.compute = this.cost.where === LOCAL ? undefined : this.cost.where;
    cx.spawn(async (cx) => { if (await refreshEstimate(this.cost, want ? { ...want, where: want.where ?? this.cost.where } : null)) cx.notify(); });
  }

  /** A choice clicked: show it now, then reload what depends on it. The click's cx is good only during the click, so
   *  the reload runs on its own task. Awaiting with the click's cx left the pick unshown: Edgar clicked Compare and
   *  With in "Run a backtest" and nothing changed. */
  pick(cx, f, o) {
    const changed = this.values[f.key] !== o.value;
    this.values[f.key] = o.value;
    delete this.errors[f.key];
    // A field whose starting value follows this pick (how a project trains follows what it builds) starts again.
    if (changed) for (const g of this.task.fields) if (g.resetOn === f.key && typeof g.default === "function") this.values[g.key] = g.default(this.values);
    cx.notify();
    cx.spawn(async (cx) => this.loadOptions(cx));
  }

  async loadOptions(cx) {
    // The services' own fields can be choices too (a database kind's), so walk what's shown, twice: a choice
    // can bring more fields with it.
    for (let pass = 0; pass < 2; pass++) {
      for (const f of this.shown()) {
        if (f.kind !== "choice") continue;
        const o = typeof f.options === "function" ? await f.options(this.ctx, this.values) : f.options;
        this.options[f.key] = o ?? [];
        if (this.values[f.key] === undefined && f.default !== undefined) this.values[f.key] = typeof f.default === "function" ? f.default(this.values) : f.default;
        if (this.values[f.key] === undefined && this.options[f.key].length === 1 && f.required) this.values[f.key] = this.options[f.key][0].value;
      }
    }
    // Fields the services describe (a sign-in's, a compute kind's) appear with the choice.
    this.ensureStates();
    cx.notify();
  }

  /** Make the input state for every text field that's shown now. Never call it from render. */
  ensureStates() {
    for (const f of this.shown()) {
      if (f.kind === "rows") { this.ensureRows(f); continue; }
      if (f.kind === "choice" || f.kind === "toggle" || f.kind === "note" || f.kind === "runs-on" || this.states[f.key]) continue;
      const long = f.kind === "long";
      // "e.g." so an example never reads as a value that's already filled in.
      const hint = f.placeholder ? `e.g. ${f.placeholder}` : "";
      const st = long ? TextareaState.new({ placeholder: hint, rows: 4 }) : InputState.new({ placeholder: hint });
      if (f.kind === "secret") st.set_masked?.(true);
      const start = this.values[f.key] ?? f.default;
      if (start !== undefined) st.set_value(String(start));
      st.on("change", () => { this.values[f.key] = st.value(); delete this.errors[f.key]; });
      this.states[f.key] = st;
    }
  }

  /** An editable list (a project's inputs): filled from what was picked above, and the person's from then on. It is
   *  filled again only when that pick changes. Each row has its own text state. Never call it from render. */
  ensureRows(f) {
    this.rowStates ??= {}; this.rowSeed ??= {}; this.rowUid ??= 0;
    const on = f.seedOn ? f.seedOn(this.values) : "";
    if (this.values[f.key] === undefined || this.rowSeed[f.key] !== on) {
      this.rowSeed[f.key] = on;
      this.values[f.key] = (f.seed?.(this.ctx, this.values) ?? []).map((r) => ({ ...r, uid: ++this.rowUid }));
    }
    for (const row of this.values[f.key]) {
      if (this.rowStates[row.uid]) continue;
      const st = InputState.new({ placeholder: f.placeholder ? `e.g. ${f.placeholder}` : "" });
      if (row.label) st.set_value(String(row.label));
      st.on("change", () => { row.label = st.value(); delete this.errors[f.key]; });
      this.rowStates[row.uid] = st;
    }
  }

  /** One row more, or one fewer. */
  rowAdd(cx, f) {
    (this.values[f.key] ??= []).push({ label: "", kind: f.kinds?.at(-1)?.value ?? "any", uid: ++this.rowUid });
    this.ensureRows(f);
    cx.notify();
  }

  rowRemove(cx, f, uid) {
    this.values[f.key] = (this.values[f.key] ?? []).filter((r) => r.uid !== uid);
    delete this.rowStates[uid];
    cx.notify();
  }

  rowsField(c, f) {
    const rows = this.values[f.key] ?? [];
    const kinds = f.kinds ?? [];
    return v_flex().id(`f-${f.key}`).gap(6)
      .children(rows.map((row, i) => v_flex().id(`f-${f.key}-row-${i}`).p(8).gap(6).border_1().border_color(c.rule).bg(c.surface)
        .child(h_flex().gap(8).items_center()
          .child(div().id(`f-${f.key}-row-${i}-name`).flex_1().min_w(0).h(30).px(10).flex().items_center().bg(c.surface).border_1().border_color(c.grey300).cursor_text()
            .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
            .child(this.rowStates?.[row.uid] ? Input.new(this.rowStates[row.uid]).w_full().text_size(13).text_color(c.navy) : div().h(14).w(120).bg(c.surface2)))
          .child(div().id(`f-${f.key}-row-${i}-remove`).flex_none().px(8).h(26).flex().items_center().cursor_pointer().text_size(12.5).text_color(c.grey900).border_1().border_color(c.grey300)
            .on_click((_e, cx) => this.rowRemove(cx, f, row.uid)).child("Remove")))
        .when(kinds.length > 0, (e) => e.child(h_flex().flex_wrap().gap(6).items_center()
          .child(div().text_size(12).text_color(c.grey500).child(f.kindLabel ?? "What it is"))
          .children(kinds.map((k) => {
            const on = row.kind === k.value;
            return div().id(`f-${f.key}-row-${i}-kind-${k.value}`).h(24).px(8).flex().flex_none().items_center().cursor_pointer().text_size(12).border_1()
              .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy)
              .on_click((_e, cx) => { row.kind = k.value; cx.notify(); }).child(k.label);
          }))))))
      .when(!rows.length, (e) => e.child(div().id(`f-${f.key}-empty`).text_size(12.5).text_color(c.grey500).child(f.empty ?? "Nothing yet.")))
      .child(h_flex().child(div().id(`f-${f.key}-add`).h(28).px(10).flex().items_center().cursor_pointer().text_size(12.5).text_color(c.navy).border_1().border_color(c.grey300)
        .on_click((_e, cx) => this.rowAdd(cx, f)).child(f.addLabel ?? "Add one")));
  }

  // Fields shown now, with the services' own fields (a sign-in's, a compute kind's) expanded.
  shown() {
    const out = [];
    for (const f of this.task.fields) {
      if (f.when && !f.when(this.values, this.ctx)) continue;
      if (f.kind === "dynamic") {
        for (const d of f.fields(this.ctx, this.values)) {
          const type = d.secret ? "secret" : d.type === "secret" ? "secret" : d.type === "choice" ? "choice" : "text";
          const choices = (d.choices ?? []).map((ch) => (typeof ch === "object" ? { value: ch.value, label: ch.label ?? String(ch.value) } : { value: ch, label: String(ch) }));
          out.push({ key: `af:${d.key ?? d.name}`, label: d.label ?? d.name, kind: type, required: Boolean(d.required), optional: Boolean(d.optional),
            default: d.default ?? undefined, placeholder: d.placeholder ?? undefined, help: d.help ?? undefined, ...(type === "choice" ? { options: choices } : {}) });
        }
      } else out.push(f);
    }
    return out;
  }

  // Many options (pipelines, environments, compute kinds): a short scrolling list, not a wall of chips.
  longChoice(c, f, opts) {
    return v_flex().id(`f-${f.key}-list`).max_h(184).overflow_y_scroll().border_1().border_color(c.grey300)
      .children(opts.map((o) => {
        const on = this.values[f.key] === o.value;
        return h_flex().id(`f-${f.key}-${o.value}`).h(30).flex_none().px(10).gap(8).items_center().cursor_pointer()
          .bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5).hover((s) => (on ? s : s.bg(c.surface2)))
          .on_click((_e, cx) => this.pick(cx, f, o))
          .when(Boolean(o.brand), (e) => e.child(Brand(c, o.brand, 14)))
          .child(div().flex_1().min_w(0).truncate().child(o.label))
          .when(Boolean(o.chip), (e) => e.child(Badge(c, o.chip, on ? "blue" : "neutral")));
      }));
  }

  /** Big choices with a line each (what you're building): a card per option, two to a row, its icon, title, what
   *  it is and what you'll need. */
  cardChoice(c, f, opts) {
    const rows = [];
    for (let i = 0; i < opts.length; i += 2) rows.push(opts.slice(i, i + 2));
    return v_flex().gap(8).children(rows.map((row, r) => h_flex().gap(8).items_start().children([...row.map((o) => {
      const on = this.values[f.key] === o.value;
      return v_flex().id(`f-${f.key}-${o.value}`).flex_1().min_w(0).min_h(112).p(12).gap(6).cursor_pointer().border_1()
        .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).hover((s) => (on ? s : s.border_color(c.blue)))
        .on_click((_e, cx) => this.pick(cx, f, o))
        .child(h_flex().gap(8).items_center()
          .child(div().size(24).flex_none().flex().items_center().justify_center().bg(on ? c.blue : c.blueLight).text_color(on ? c.onBlue : c.blue).child(svg(icon(o.icon ?? "workflow")).size(13)))
          .child(div().flex_1().min_w(0).text_size(13).font_semibold().text_color(on ? c.blue : c.navy).child(o.label))
          .when(Boolean(o.chip), (e) => e.child(Badge(c, o.chip, on ? "blue" : "neutral"))))
        .when(Boolean(o.about), (e) => e.child(div().text_size(12).line_height("16px").text_color(c.grey900).child(o.about)))
        .when(Boolean(o.needs), (e) => e.child(div().text_size(11.5).line_height("16px").text_color(c.grey500).child(`You'll need ${o.needs}`)));
    }), ...(row.length < 2 ? [div().id(`f-${f.key}-pad-${r}`).flex_1()] : [])])));
  }

  // A picker of products (databases, places to run work): a grid of tiles, each with its mark, `columns` to a row.
  gridChoice(c, f, opts) {
    const n = f.columns ?? 3;
    const rows = [];
    for (let i = 0; i < opts.length; i += n) rows.push(opts.slice(i, i + n));
    return v_flex().gap(6).children(rows.map((row, r) => h_flex().gap(6).children([...row.map((o) => {
      const on = this.values[f.key] === o.value;
      return h_flex().id(`f-${f.key}-${o.value}`).flex_1().min_w(0).h(40).px(10).gap(8).items_center().cursor_pointer().border_1()
        .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5)
        .hover((s) => (on ? s : s.border_color(c.blue)))
        .on_click((_e, cx) => this.pick(cx, f, o))
        .child(Brand(c, o.brand ?? o.value, 18))
        .child(div().flex_1().min_w(0).truncate().child(o.label))
        .when(Boolean(o.chip), (e) => e.child(div().text_size(10.5).text_color(c.grey500).child(o.chip)));
    }), ...Array(n - row.length).fill(0).map((_, k) => div().id(`f-${f.key}-pad-${r}-${k}`).flex_1())])));
  }

  field(c, f) {
    const err = this.errors[f.key] ?? this.shownProblem(f);
    const said = this.said[f.key];
    // What the services said about this field that is not a problem (a sign-in that was found), or that they are
    // being asked now.
    const note = f.name ? null : this.checking[f.key] ? "Checking..." : said && !said.problem ? said.text : null;
    // Help can depend on what's picked (a database kind's note: "Signs in with your AWS login").
    const help = typeof f.help === "function" ? f.help(this.ctx, this.values) : f.help;
    const label = h_flex().gap(6).items_baseline()
      .child(div().text_size(13).font_medium().text_color(c.navy).child(f.label))
      .when(Boolean(f.optional), (e) => e.child(div().text_size(11.5).text_color(c.grey500).child("optional")));
    let control;
    if (f.kind === "runs-on") {
      const fixed = typeof f.fixed === "function" ? f.fixed(this.values, this.ctx) : f.fixed;
      const note = typeof f.note === "function" ? f.note(this.values, this.ctx, this.cost) : f.note;
      return RunsOn(c, `f-${f.key}`, this.cost, {
        onPick: (p, _e, cx) => { if (this.cost.where !== p.value) this.cost.estimate = null; this.cost.where = p.value; this.cost.confirming = false; cx.notify(); this.priceNow(cx); },
        onToggle: (_e, cx) => { this.cost.open = !this.cost.open; cx.notify(); },
        onAdd: () => { this.close(); openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: "compute" } }); },
      }, { fixed: Boolean(fixed), note: note ?? null });
    }
    if (f.kind === "note") {
      // What this choice will do, in lines the services wrote: nothing to fill in. A line can warn.
      const lines = (f.lines(this.ctx, this.values) ?? []).filter((l) => l?.text);
      return v_flex().id(`f-${f.key}`).p(12).gap(6).bg(c.surface2).border_1().border_color(c.rule)
        .when(Boolean(f.label), (e) => e.child(div().text_size(12.5).font_semibold().text_color(c.navy).child(f.label)))
        .children(lines.map((l, k) => h_flex().id(`f-${f.key}-line-${k}`).gap(8).items_start()
          .child(svg(icon(l.tone === "warn" ? "triangle-alert" : l.tone === "bad" ? "circle-x" : l.icon ?? "check")).size(13).flex_none().mt(2)
            .text_color(l.tone === "warn" ? c.warn : l.tone === "bad" ? c.fail : c.grey500))
          .child(div().flex_1().min_w(0).text_size(12.5).line_height("18px").text_color(l.tone === "warn" ? c.warn : l.tone === "bad" ? c.fail : c.grey900).child(l.text))
          .when(Boolean(l.chip), (e) => e.child(Badge(c, l.chip, l.chip === "simulated" ? "warn" : "neutral")))));
    }
    if (f.kind === "rows") control = this.rowsField(c, f);
    else if (f.kind === "choice") {
      const opts = this.options[f.key];
      control = !opts ? div().h(30).w(160).bg(c.surface2)
        : !opts.length ? div().text_size(12.5).text_color(c.grey500).child(f.empty ?? "Nothing to pick yet.")
        : f.cards ? this.cardChoice(c, f, opts)
        : f.grid ? this.gridChoice(c, f, opts)
        : opts.length > 8 ? this.longChoice(c, f, opts)
        : h_flex().flex_wrap().gap(6).children(opts.map((o) => {
          const on = this.values[f.key] === o.value;
          return h_flex().id(`f-${f.key}-${o.value}`).h(30).px(10).gap(6).items_center().cursor_pointer().border_1()
            .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12.5)
            .on_click((_e, cx) => this.pick(cx, f, o))
            .when(Boolean(o.brand), (e) => e.child(Brand(c, o.brand, 14)))
            .child(o.label).when(Boolean(o.chip), (e) => e.child(Badge(c, o.chip, on ? "blue" : "neutral")));
        }));
    } else if (f.kind === "toggle") {
      const on = Boolean(this.values[f.key]);
      control = h_flex().id(`f-${f.key}`).gap(8).items_center().cursor_pointer().on_click((_e, cx) => { this.values[f.key] = !on; cx.notify(); })
        .child(div().w(30).h(16).p(2).bg(on ? c.blue : c.grey300).child(div().size(12).bg(c.surface).when(on, (e) => e.ml(14))))
        .child(div().text_size(12.5).text_color(c.grey900).child(on ? "On" : "Off"));
    } else {
      const long = f.kind === "long";
      control = div().id(`f-${f.key}`).when(!long, (e) => e.h(32).flex().items_center()).when(long, (e) => e.min_h(84).py(6)).px(10)
        .bg(c.surface).border_1().border_color(err ? c.fail : c.grey300).cursor_text()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
        .child(!this.states[f.key] ? div().h(14).w(120).bg(c.surface2)
          : long ? Textarea.new(this.states[f.key]).w_full().text_size(13).text_color(c.navy) : Input.new(this.states[f.key]).w_full().text_size(13).text_color(c.navy));
    }
    return v_flex().gap(5).child(label).child(control)
      .when(Boolean(f.name) && Boolean(this.nameLine(c, f)), (e) => e.child(this.nameLine(c, f)))
      .when(Boolean(err), (e) => e.child(div().id(`f-${f.key}-problem`).text_size(12).line_height("17px").text_color(c.fail).child(err)))
      .when(!err && Boolean(note), (e) => e.child(div().id(`f-${f.key}-note`).text_size(12).line_height("17px").text_color(c.grey500).child(note)))
      .when(!err && !note && Boolean(help), (e) => e.child(div().text_size(12).line_height("17px").text_color(c.grey500).child(help)));
  }

  submit(cx) {
    if (this.busy) return;
    this.errors = {}; this.serverErrors = {}; this.formError = null;
    for (const f of this.shown()) {
      // An editable list is read from its text boxes as they stand now, not from the last change heard.
      if (f.kind === "rows") for (const row of this.values[f.key] ?? []) if (this.rowStates?.[row.uid]) row.label = this.rowStates[row.uid].value();
      // The id Captain keeps for a name travels with the form: <key>_id beside the words typed.
      if (f.name) this.values[`${f.key}_id`] = this.idOf(f);
    }
    // Nothing is checked here for the first time: the button is off while a field has a problem. This is for a
    // key press that reaches it anyway.
    if (this.blocked()) { this.tried = true; cx.notify(); return; }
    // Above the amount set in Settings, the first click only repeats the number. The second starts it.
    if (this.runsOn() && needsConfirm(this.cost)) { this.cost.confirming = true; cx.notify(); return; }
    this.busy = true; this.result = null;
    cx.notify();
    cx.spawn(async (cx) => {
      let r;
      try { r = await this.task.submit(this.values, this.ctx, props()?.args ?? {}, cx); } catch (e) { r = { ok: false, error: String(e) }; }
      this.busy = false;
      this.result = r;
      if (!r.ok && !/couldn't start its services/.test(String(r.error ?? ""))) {
        // What came back is said in plain words, under the field it is about when that can be told.
        const e = sayError(r.error, this.shown());
        const at = r.field ?? e.field;
        if (at && this.shown().some((f) => f.key === at)) this.serverErrors[at] = e.text; else this.formError = e.text;
      }
      if (r.ok) {
        recordsChanged(this.task.id); // Handoff, Investigations... reload now
        if (r.next) { this.done = r; cx.notify(); return; } // stay open to offer the next step
        notify({ title: r.message ?? "Done", level: "info" });
        this.close();
      }
      cx.notify();
    });
  }

  /** What to do next after this form: a failed check's fix, or the step after a decision. Opens that task pre-filled. */
  nextStep(c, n) {
    return h_flex().gap(8).items_center()
      .child(svg(icon("chevron-right")).size(13).text_color(c.blue))
      .child(div().flex_1().text_size(12.5).text_color(c.grey900).child(this.done ? "Next:" : "To fix it:"))
      .child(Button(c, "form-next", n.label, () => {
        this.close();
        if (n.run) { n.run(); return; } // a step with no form of its own (judge the candidate just trained)
        if (n.section) { setShared("captain.open", { section: n.section, at: Date.now() }); runCommand("layout.select", { stack: "sections", child: n.section }); return; }
        openOverlay({ mod: "captain-forms", kind: "dialog", width: 560, props: { task: n.task, values: n.values ?? {} } });
      }, { kind: "primary", small: true }));
  }

  // The New menu: every task with its icon, keyboard up and down and Enter, scrolling inside its popup with a fade
  // at the bottom so nothing is cut off.
  menuItems() { return TASKS.filter((t) => !t.hidden); }

  openTask(t) {
    this.close();
    // Import a dataset is desktop-app's wizard (sources, tables, a preview); the form stays for agents.
    openOverlay(t.opens ?? (t.id === "dataset" ? IMPORT : { mod: "captain-forms", kind: "dialog", width: 560, props: { task: t.id } }));
  }

  menu(c) {
    const items = this.menuItems();
    const sel = Math.min(this.sel ?? 0, items.length - 1);
    return div().id("new-menu").size_full().relative().bg(c.surface).rounded(RADIUS).font_family(FONT).when(Boolean(this.menuFocus), (e) => e.track_focus(this.menuFocus))
      .on_key_down((e, cx) => {
        if (e.key === "down") { this.sel = (sel + 1) % items.length; cx.notify(); }
        else if (e.key === "up") { this.sel = (sel - 1 + items.length) % items.length; cx.notify(); }
        else if (e.key === "enter") this.openTask(items[sel]);
        else if (e.key === "escape") this.close();
      })
      .child(div().id("new-menu-list").size_full().overflow_y_scroll().child(v_flex().py(4).pb(18)
        .children(items.map((t, i) => {
          const on = i === sel;
          return h_flex().id(`new-${t.id}`).mx(4).px(10).py(6).gap(10).items_center().cursor_pointer()
            .bg(on ? c.blueLight : c.surface)
            .on_hover((hovered, cx) => { if (hovered && this.sel !== i) { this.sel = i; cx.notify(); } })
            .on_click(() => this.openTask(t))
            .child(div().size(26).flex_none().flex().items_center().justify_center().bg(on ? c.blue : c.blueLight).text_color(on ? c.onBlue : c.blue)
              .child(svg(icon(t.icon)).size(14)))
            .child(v_flex().flex_1().min_w(0).gap(1)
              .child(div().truncate().text_size(13).font_medium().text_color(on ? c.blue : c.navy).child(t.label))
              .child(div().truncate().text_size(11.5).text_color(c.grey500).child(t.about)));
        }))))
      // The fade: the list scrolls under it, so a row past the bottom shows it continues.
      .child(v_flex().absolute().bottom(0).left(0).right(0).children([0.35, 0.65, 0.9].map((o, i) => div().id(`new-fade-${i}`).h(6).w_full().bg(c.surface).opacity(o))));
  }


  render() {
    const c = palette(isDark(theme()));
    if (!this.task || this.isMenu) return this.menu(c);
    const t = this.task;
    const failed = this.result && !this.result.ok ? this.result.error : null;
    const why = this.blocked();
    return v_flex().size_full().bg(c.surface).rounded(RADIUS).font_family(FONT).text_color(c.navy)
      .child(h_flex().h(56).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
        .child(div().size(30).flex_none().flex().items_center().justify_center().bg(c.blueLight).text_color(c.blue).child(svg(icon(t.icon)).size(16)))
        .child(div().flex_1().text_size(16).font_semibold().child(t.label))
        // One Cancel, at the bottom beside the main button. Up here only a close mark, named for screen readers.
        .child(div().id("form-close").size(28).flex_none().flex().items_center().justify_center().cursor_pointer().text_color(c.grey500)
          .hover((s) => s.text_color(c.navy)).accessibility_label("Close").tooltip("Close").on_click(() => this.close()).child(svg(icon("x")).size(16))))
      .child(v_flex().id("form-body").flex_1().min_h(0).overflow_y_scroll().p(20).gap(16)
        .child(div().text_size(13).line_height("19px").text_color(c.grey900).child(t.about))
        .children(this.shown().map((f) => this.field(c, f)))
        .when(Boolean(failed) && /couldn't start its services/.test(failed), (e) => e.child(true ? ServiceError(c, failed) : div().p(10).bg(c.failLight).border_1().border_color(c.fail).text_size(12.5).line_height("18px").text_color(c.fail).child(failed)))
        .when(Boolean(this.done), (e) => e.child(div().p(10).bg(c.goodLight).border_1().border_color(c.good).text_size(12.5).line_height("18px").text_color(c.good).child(this.done.message ?? "Done.")))
        .when(Boolean(this.result?.next ?? this.done?.next), (e) => e.child(this.nextStep(c, this.result?.next ?? this.done?.next))))
      .child(h_flex().min_h(60).py(8).flex_none().px(20).gap(8).items_center().justify_end().border_t(1).border_color(c.rule)
        // One line: what came back that belongs to no field, else why the button is off.
        .child(div().id("form-why").flex_1().min_w(0).text_size(12).line_height("17px").text_color(this.formError ? c.fail : c.grey500)
          .child(this.done ? "" : this.formError ?? (this.busy ? "" : why ?? "")))
        .child(Button(c, "form-cancel", "Cancel", () => this.close()))
        .child(this.done ? Button(c, "form-done", "Close", () => this.close()) : Button(c, "form-go", this.busy ? (t.busy?.(this.values) ?? "Working...") : this.cost.confirming ? "Start anyway" : (t.go?.(this.values, this.ctx) ?? t.label), (_e, cx) => this.submit(cx), { kind: "primary", disabled: Boolean(this.busy) || Boolean(why) })));
  }
}
