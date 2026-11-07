// CAPTAIN: run any cst command natively. Opened as an overlay:
//   props {path: ["backtest","plan"], args?}  a form for that command's params (args prefill)
//                                      (GET /api/commands), Run posts
//                                      POST /api/commands/run; the result shows here
//                                      and as a toast.
//   props {menu: "all"}                every cst command, for power users ("Run a cst
//                                      command..." in the palette). People use New and
//                                      the screens; agents use the CLI.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, notify, shared } from "herdr";
import { api } from "./kit/cst.js";
import { Skeleton } from "./kit/ui.js";
import { isDark, palette, RADIUS } from "./kit/tokens.js";

const title = (path) => `cst ${path.join(" ")}`;

export default class CaptainCommands extends View {
  init(_props, cx) {
    this.path = props()?.path ?? null;
    this.command = null;
    this.inputs = {};
    this.values = {};
    this.result = null;
    this.busy = false;
    this.error = null;
    if (this.path) this.load(cx);
    if (props()?.menu) this.loadAll(cx);
  }

  close() {
    closeOverlay(props().overlay);
  }

  pick(path, cx) {
    this.path = path;
    this.load(cx);
    cx.notify();
  }

  load(cx) {
    cx.spawn(async (cx) => {
      const r = await api("/api/commands");
      if (!r.ok) {
        this.problem = r;
        this.error = r.starting ? null : r.error;
      } else {
        this.command = (r.data ?? []).find((c) => c.path.join(" ") === this.path.join(" ")) ?? null;
        if (!this.command) this.error = `${title(this.path)} isn't a command Captain knows.`;
        // props.args prefills (an investigation's id for Decide, for example).
        const given = props()?.args ?? {};
        for (const p of this.command?.params ?? []) {
          const start = given[p.name] ?? p.default;
          if (p.kind === "flag" || p.kind === "choice") {
            this.values[p.name] = start ?? (p.kind === "flag" ? false : null);
          } else {
            const s = InputState.new({ placeholder: p.help ?? p.name });
            if (start != null) s.set_value(String(start));
            this.inputs[p.name] = s;
          }
        }
      }
      cx.notify();
    });
  }

  args() {
    const out = {};
    for (const p of this.command?.params ?? []) {
      let v = this.inputs[p.name] ? this.inputs[p.name].value().trim() : this.values[p.name];
      if (v === "" || v === null || v === undefined) continue;
      if (p.kind === "int") v = parseInt(v, 10);
      if (p.kind === "float") v = parseFloat(v);
      if (p.multiple && typeof v === "string") v = v.split(",").map((s) => s.trim()).filter(Boolean);
      out[p.name] = v;
    }
    return out;
  }

  missing() {
    const a = this.args();
    return (this.command?.params ?? []).filter((p) => p.required && a[p.name] === undefined).map((p) => p.name);
  }

  submit(cx) {
    if (this.busy || !this.command) return;
    const missing = this.missing();
    if (missing.length) { this.error = `Fill in: ${missing.join(", ")}`; cx.notify(); return; }
    this.busy = true; this.error = null; this.result = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api("/api/commands/run", { body: { path: this.path, args: this.args() }, timeout: 590 });
      this.busy = false;
      const body = r.data ?? {};
      const failed = !r.ok || body.exit_code || body.error;
      this.result = { failed, command: body.command ?? title(this.path), text: failed ? String(body.error || r.error || `exit ${body.exit_code}`) : JSON.stringify(body.output ?? "done", null, 2) };
      notify({ title: failed ? `${title(this.path)} failed` : `${title(this.path)} finished`, body: this.result.text.slice(0, 200), level: failed ? "error" : "info" });
      cx.notify();
    });
  }

  field(c, p) {
    const label = h_flex().gap(6).items_baseline().child(div().text_size(12.5).font_medium().text_color(c.navy).child(p.name))
      .when(p.required, (e) => e.child(div().text_size(11).text_color(c.blue).child("required")))
      .when(Boolean(p.help), (e) => e.child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(p.help)));
    let control;
    if (p.kind === "flag") {
      const on = Boolean(this.values[p.name]);
      control = h_flex().id(`f-${p.name}`).gap(8).items_center().cursor_pointer().on_click((_e, cx) => { this.values[p.name] = !on; cx.notify(); })
        .child(div().size(14).border_1().border_color(on ? c.blue : c.grey300).bg(on ? c.blue : c.surface))
        .child(div().text_size(12.5).text_color(c.grey900).child(on ? "on" : "off"));
    } else if (p.kind === "choice") {
      control = h_flex().flex_wrap().gap(6).children((p.choices ?? []).map((ch) => {
        const on = this.values[p.name] === ch;
        return div().id(`f-${p.name}-${ch}`).px(8).h(24).flex().items_center().cursor_pointer().border_1()
          .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).text_color(on ? c.blue : c.navy).text_size(12)
          .on_click((_e, cx) => { this.values[p.name] = on ? null : ch; cx.notify(); }).child(String(ch));
      }));
    } else {
      control = div().id(`f-${p.name}`).h(30).px(8).flex().items_center().border_1().border_color(c.grey300).cursor_text()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation()).child(Input.new(this.inputs[p.name]).w_full().text_size(12.5).text_color(c.navy));
    }
    return v_flex().gap(4).child(label).child(control);
  }

  // Every cst command, for people who want the CLI's full reach (agents use the CLI itself).
  loadAll(cx) {
    if (this.all || this.loadingAll) return;
    this.loadingAll = true;
    cx.spawn(async (cx) => {
      const r = await api("/api/commands");
      this.all = r.ok && Array.isArray(r.data) ? r.data : [];
      this.loadingAll = false;
      cx.notify();
    });
  }

  menu(c) {
    const list = this.all ?? [];
    const groups = new Map();
    for (const cmd of list) {
      const g = cmd.path[0];
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(cmd);
    }
    return v_flex().size_full().bg(c.surface).rounded(RADIUS).text_color(c.navy).font_family("Inter")
      .child(h_flex().h(48).flex_none().px(16).items_center().border_b(1).border_color(c.rule)
        .child(div().flex_1().text_size(14).font_semibold().child("Run a cst command"))
        .child(div().id("cmd-close").cursor_pointer().text_color(c.grey500).hover((s) => s.text_color(c.navy)).on_click(() => this.close()).child("Close")))
      .child(div().px(16).py(8).text_size(12).text_color(c.grey500).child("Everything the command line can do, as forms. Most people use New and the screens; agents use these in a terminal."))
      .when(!this.all, (e) => e.child(div().px(16).py(8).child(Skeleton(c, 6))))
      .child(v_flex().id("all-list").flex_1().min_h(0).overflow_y_scroll().children([...groups.entries()].map(([g, cmds]) => v_flex()
        .child(div().px(16).pt(8).pb(2).text_size(11).text_color(c.grey500).child(g))
        .children(cmds.map((cmd) => h_flex().id(`all-${cmd.path.join("-")}`).h(30).px(16).gap(8).items_center().cursor_pointer()
          .hover((s) => s.bg(c.surface2)).on_click((_e, cx) => this.pick(cmd.path, cx))
          .child(div().font_family("JetBrains Mono").text_size(12.5).child(cmd.path.join(" ")))
          .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(c.grey500).child(cmd.help ?? "")))))));
  }

  render() {
    const c = palette(isDark(theme()));
    if (!this.path && props()?.menu) return this.menu(c);
    const cmd = this.command;
    return v_flex().size_full().bg(c.surface).rounded(RADIUS).text_color(c.navy).font_family("Inter")
      .child(h_flex().h(48).flex_none().px(16).gap(10).items_center().border_b(1).border_color(c.rule)
        .child(div().text_size(14).font_semibold().font_family("JetBrains Mono").child(title(this.path ?? [])))
        .child(div().flex_1()).child(div().id("cmd-close").cursor_pointer().text_color(c.grey500).hover((s) => s.text_color(c.navy)).on_click(() => this.close()).child("Close")))
      .child(v_flex().id("cmd-body").flex_1().min_h(0).overflow_y_scroll().p(16).gap(12)
        .when(Boolean(cmd?.help), (e) => e.child(div().text_size(12.5).text_color(c.grey900).child(cmd.help)))
        .when(!cmd && !this.error, (e) => e.child(div().text_size(12.5).text_color(c.grey500).child("Loading...")))
        .children((cmd?.params ?? []).filter((p) => !p.secret).map((p) => this.field(c, p)))
        .when(Boolean(this.error), (e) => e.child(div().text_size(12.5).text_color(c.fail).child(this.error)))
        .when(Boolean(this.result), (e) => e.child(v_flex().gap(4)
          .child(div().text_size(11.5).text_color(this.result.failed ? c.fail : c.grey500).font_family("JetBrains Mono").child(this.result.command))
          .child(div().p(10).bg(c.surface2).border_1().border_color(c.rule).font_family("JetBrains Mono").text_size(11.5)
            .text_color(this.result.failed ? c.fail : c.navy).child(this.result.text.slice(0, 6000))))))
      .child(h_flex().h(56).flex_none().px(16).gap(8).items_center().justify_end().border_t(1).border_color(c.rule)
        .child(div().id("cmd-run").h(32).px(16).flex().items_center().cursor_pointer()
          .bg(cmd && !this.busy ? c.blue : c.grey300).text_color(c.onBlue).text_size(13).font_medium()
          .on_click((_e, cx) => this.submit(cx)).child(this.busy ? "Running..." : "Run")));
  }
}
