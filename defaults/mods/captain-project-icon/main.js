// CAPTAIN: a project's icon (Edgar, 2026-11-09: "make it so I can change the icons of the projects, I want the ...
// projects to have different icons for organizations"). Opened as a dialog with props {project: {id, name, icon?}}:
//   Icon        an icon from the app's set, found by name, in one of the palette's colors
//   Brand mark  a mark the app ships (a data platform or cloud), the ones matching the project's name first
//   Logo        the organization's own PNG, JPEG or SVG, dropped here or by its path. It goes into the workspace
//               store (POST /api/projects/{id}/logo), so teammates see it. Nothing is shipped.
// Saves with PUT /api/projects/{id}/icon, then the Explorer, Home and the project page draw it.
import { View, div, svg } from "gpui-kit";
import { h_flex, v_flex, InputState, Input } from "gpui-base";
import { theme, props, closeOverlay, setShared, notify, home } from "herdr";
import { palette, isDark, FONT, RADIUS } from "./kit/tokens.js";
import { api } from "./kit/cst.js";
import { Button, Badge } from "./kit/ui.js";
import { icon } from "./kit/icons.js";
import { Brand, BRANDS } from "./kit/brands.js";
import { ProjectIcon, ProjectBadge, ICON_COLORS } from "./kit/projecticon.js";

// The icons that make sense for a project, with the words people search for them by.
export const PICKABLE = [
  ["workflow", "project default flow"], ["building-2", "company organization office business"], ["store", "shop retail store storefront"],
  ["shopping-bag", "shop retail ecommerce bag"], ["globe", "web global international site"], ["briefcase", "business client work"],
  ["gift", "gift present holiday"], ["zap", "fast energy power"], ["star", "star favorite featured"], ["sparkles", "ai magic new"],
  ["trophy", "winner champion"], ["package", "package product box"], ["boxes", "products inventory catalog"], ["layers", "layers stack"],
  ["database", "data database warehouse"], ["server", "server infrastructure"], ["cloud", "cloud"], ["rocket", "launch ship"],
  ["compass", "explore discovery"], ["search", "search find"], ["tag", "tag label brand"], ["flask-conical", "experiment lab research"],
  ["activity", "events activity signals"], ["chart-line", "metrics growth chart"], ["bar-chart-3", "analytics report chart"],
  ["users", "team people customers"], ["user", "person customer"], ["bot", "agent bot ai"], ["folder", "folder files"],
  ["table", "table data"], ["file-text", "document file"], ["monitor", "screen desktop"], ["waypoints", "network graph"],
];
const COLOR_NAMES = { "#0043CA": "Blue", "#7C3AED": "Violet", "#059669": "Green", "#D97706": "Amber", "#DC2626": "Red", "#0891B2": "Teal", "#DB2777": "Pink", "#64748B": "Slate" };
const LOGO_EXT = /\.(png|jpe?g|svg)$/i;
const expand = (p) => String(p ?? "").trim().replace(/^~(?=\/|$)/, home());

export default class ProjectIconPicker extends View {
  init(_props, cx) {
    const p = props()?.project ?? {};
    this.project = p;
    const ic = p.icon ?? null;
    this.tab = ic?.kind === "brand" ? "brand" : ic?.kind === "logo" ? "logo" : "glyph";
    this.glyph = ic?.kind === "glyph" ? ic.name : "workflow";
    this.color = ic?.kind === "glyph" ? ic.color : ICON_COLORS[0];
    this.brand = ic?.kind === "brand" ? ic.slug : null;
    this.search = InputState.new({ placeholder: "Find an icon, like store or globe" });
    this.search.on("change", (_e, cx) => cx.notify());
    this.path = InputState.new({ placeholder: "~/Downloads/logo.svg" });
    this.path.on("change", (_e, cx) => { this.note = null; cx.notify(); });
    this.busy = false; this.note = null;
  }

  close() { closeOverlay(props().overlay); }

  /** The icon as it would be saved now, to preview. */
  current() {
    if (this.tab === "brand") return this.brand ? { kind: "brand", slug: this.brand } : this.project.icon ?? null;
    if (this.tab === "logo") return this.project.icon?.kind === "logo" ? this.project.icon : null;
    return { kind: "glyph", name: this.glyph, color: this.color };
  }

  save(cx, iconValue, message) {
    if (this.busy) return;
    this.busy = true; this.note = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${encodeURIComponent(this.project.id)}/icon`, { method: "PUT", body: { icon: iconValue }, scoped: false });
      this.busy = false;
      if (!r.ok) { this.note = { tone: "fail", text: String(r.data?.error ?? r.error ?? "Couldn't save it.") }; cx.notify(); return; }
      setShared("captain.explorer.rev", Date.now());
      notify({ title: message, level: "info" });
      this.close();
    });
  }

  /** A logo file: into the workspace store, then the project's icon. */
  upload(cx, path) {
    path = expand(path);
    if (!LOGO_EXT.test(path)) { this.note = { tone: "fail", text: "A logo is a PNG, JPEG or SVG file." }; cx.notify(); return; }
    this.busy = true; this.note = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await api(`/api/projects/${encodeURIComponent(this.project.id)}/logo`, { body: { path }, scoped: false });
      this.busy = false;
      if (!r.ok) { this.note = { tone: "fail", text: String(r.data?.error ?? r.error ?? "Couldn't use that file.") }; cx.notify(); return; }
      setShared("captain.explorer.rev", Date.now());
      notify({ title: `${this.project.name} uses your logo`, body: "It's kept in this workspace, so your team sees it too.", level: "info" });
      this.close();
    });
  }

  tabs(c) {
    const t = [["glyph", "Icon", "star"], ["brand", "Brand mark", "tag"], ["logo", "Logo", "upload"]];
    return h_flex().id("pi-tabs").flex_none().h(38).px(20).gap(18).border_b(1).border_color(c.rule).children(t.map(([id, label, ic]) => {
      const on = this.tab === id;
      return h_flex().id(`pi-tab-${id}`).h_full().gap(6).items_center().cursor_pointer().border_b(on ? 2 : 0).border_color(c.blue)
        .text_color(on ? c.blue : c.grey900).on_click((_e, cx) => { this.tab = id; this.note = null; cx.notify(); })
        .child(svg(icon(ic)).size(13)).child(div().text_size(12.5).font_medium().child(label));
    }));
  }

  glyphTab(c) {
    const q = this.search.value().trim().toLowerCase();
    const list = PICKABLE.filter(([n, words]) => !q || n.includes(q) || words.includes(q));
    return v_flex().gap(12)
      .child(h_flex().h(30).px(8).gap(6).items_center().border_1().border_color(c.grey300)
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
        .child(svg(icon("search")).size(13).text_color(c.grey500))
        .child(Input.new(this.search).flex_1().text_size(12.5).text_color(c.navy)))
      .when(!list.length, (e) => e.child(div().text_size(12).text_color(c.grey500).child("No icon by that name. Try store, globe or company.")))
      // Wraps to the dialog's width, so no cell spills past its edge.
      .child(h_flex().id("pi-glyphs").flex_wrap().gap(6)
        .children(list.map(([n]) => {
          const on = this.glyph === n;
          return div().id(`pi-g-${n}`).size(40).flex_none().flex().items_center().justify_center().cursor_pointer().border_1()
            .border_color(on ? c.blue : c.rule).bg(on ? c.blueLight : c.surface).hover((s) => s.border_color(c.blue)).tooltip(n.replace(/-\d$/, "").replace(/-/g, " "))
            .on_click((_e, cx) => { this.glyph = n; cx.notify(); })
            .child(svg(icon(n)).size(18).text_color(this.color));
        })))
      .child(v_flex().gap(6)
        .child(div().text_size(12.5).font_medium().child("Color"))
        .child(h_flex().gap(8).children(ICON_COLORS.map((hex) => {
          const on = this.color === hex;
          return div().id(`pi-c-${hex.slice(1)}`).size(26).flex_none().flex().items_center().justify_center().cursor_pointer().border_2()
            .border_color(on ? c.navy : "#00000000").tooltip(COLOR_NAMES[hex] ?? hex).on_click((_e, cx) => { this.color = hex; cx.notify(); })
            .child(div().size(18).bg(hex));
        }))));
  }

  brandTab(c) {
    const name = String(this.project.name ?? "").toLowerCase();
    const all = Object.entries(BRANDS).map(([slug, b]) => ({ slug, ...b, match: name.includes(b.title.toLowerCase()) || name.includes(slug) }))
      .sort((a, b) => Number(b.match) - Number(a.match) || a.title.localeCompare(b.title));
    const rows = [];
    for (let i = 0; i < all.length; i += 3) rows.push(all.slice(i, i + 3));
    return v_flex().gap(8)
      .child(div().text_size(12).text_color(c.grey900).child("Marks Captain ships for data platforms and clouds. For your company's own logo, use Logo."))
      .children(rows.map((row, r) => h_flex().gap(6).children([...row.map((b) => {
        const on = this.brand === b.slug;
        return h_flex().id(`pi-b-${b.slug}`).flex_1().min_w(0).h(38).px(10).gap(8).items_center().cursor_pointer().border_1()
          .border_color(on ? c.blue : c.grey300).bg(on ? c.blueLight : c.surface).hover((s) => s.border_color(c.blue))
          .on_click((_e, cx) => { this.brand = b.slug; cx.notify(); })
          .child(Brand(c, b.slug, 16))
          .child(div().flex_1().min_w(0).truncate().text_size(12.5).text_color(on ? c.blue : c.navy).child(b.title))
          .when(b.match, (e) => e.child(Badge(c, "name match", "blue")));
      }), ...Array(3 - row.length).fill(0).map((_, k) => div().id(`pi-b-pad-${r}-${k}`).flex_1())])));
  }

  logoTab(c) {
    const has = this.project.icon?.kind === "logo";
    return v_flex().gap(12)
      .child(v_flex().id("pi-drop").h(120).gap(6).items_center().justify_center().border_1().border_dashed().border_color(c.grey300)
        .drag_over((s) => s.border_color(c.blue).bg(c.blueLight))
        .on_drop((e, cx) => { const p = (e.paths ?? []).find((x) => LOGO_EXT.test(x)) ?? e.paths?.[0]; if (p) this.upload(cx, p); })
        .child(svg(icon("upload")).size(20).text_color(c.grey500))
        .child(div().text_size(13).font_medium().child("Drop your logo here"))
        .child(div().text_size(12).text_color(c.grey500).child("PNG, JPEG or SVG, 2 MB at most. Square ones look best.")))
      .child(h_flex().gap(8).items_center()
        .child(h_flex().flex_1().min_w(0).h(30).px(8).items_center().border_1().border_color(c.grey300)
          .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
          .child(Input.new(this.path).w_full().text_size(12.5).text_color(c.navy)))
        .child(Button(c, "pi-use-file", this.busy ? "Saving" : "Use this file", (_e, cx) => this.upload(cx, this.path.value()), { kind: "primary", small: true, disabled: this.busy || !this.path.value().trim() })))
      .child(div().text_size(12).line_height("17px").text_color(c.grey500)
        .child("It's kept in this workspace's storage, so your team sees it. Captain doesn't ship anyone's logo."))
      .when(has, (e) => e.child(h_flex().gap(8).items_center().child(ProjectIcon(c, this.project, 20)).child(div().text_size(12).text_color(c.grey900).child("Your logo is in use now."))));
  }

  render() {
    const c = palette(isDark(theme()));
    const cur = this.current();
    const shown = { ...this.project, icon: cur };
    const canSave = this.tab === "glyph" || (this.tab === "brand" && this.brand);
    return v_flex().id("project-icon").size_full().bg(c.surface).rounded(RADIUS).font_family(FONT).text_color(c.navy)
      .child(h_flex().h(64).flex_none().px(20).gap(12).items_center().border_b(1).border_color(c.rule)
        .child(ProjectBadge(c, shown, 36))
        .child(v_flex().flex_1().min_w(0)
          .child(div().truncate().text_size(16).font_semibold().child(`Icon for ${this.project.name ?? "this project"}`))
          .child(div().text_size(12).text_color(c.grey500).child("Shown wherever the project is.")))
        // How it reads at the sizes it's drawn: the Explorer (14), cards (16) and the sidebar (20).
        .child(h_flex().gap(10).items_center().children([14, 16, 20].map((s) => ProjectIcon(c, shown, s))))
        .child(div().id("pi-close").cursor_pointer().text_color(c.grey500).on_click(() => this.close()).child(svg(icon("x")).size(14))))
      .child(this.tabs(c))
      .child(v_flex().id("pi-body").flex_1().min_h(0).overflow_y_scroll().p(20).gap(12)
        .child(this.tab === "glyph" ? this.glyphTab(c) : this.tab === "brand" ? this.brandTab(c) : this.logoTab(c))
        .when(Boolean(this.note), (e) => e.child(div().p(10).bg(c.failLight).text_size(12.5).text_color(c.fail).child(this.note.text))))
      .child(h_flex().h(60).flex_none().px(20).gap(8).items_center().border_t(1).border_color(c.rule)
        .child(Button(c, "pi-default", "Use the default", (_e, cx) => this.save(cx, null, `${this.project.name} uses the default icon`), { kind: "quiet", small: true, disabled: this.busy || !this.project.icon }))
        .child(div().flex_1())
        .child(Button(c, "pi-cancel", "Cancel", () => this.close()))
        .when(this.tab !== "logo", (e) => e.child(Button(c, "pi-save", this.busy ? "Saving" : "Save", (_e, cx) => this.save(cx, cur, `${this.project.name} has its new icon`), { kind: "primary", disabled: this.busy || !canSave }))));
  }
}
