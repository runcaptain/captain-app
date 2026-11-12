// captain-ui: a project's icon, the same everywhere it shows (the Explorer, Home's cards, the project page, the
// sidebar's "in <project>"). A project's `icon` is one of:
//   {kind: "glyph", name, color}  an icon from icons.js in a palette color
//   {kind: "brand", slug}         a mark from brands.js
//   {kind: "logo", sha, ext}      the organization's own logo, kept in the workspace store
// A logo is drawn from a small copy the services write into <app home>/cache/project-logos (image() only reads from
// the app home), asked for once per logo and remembered for the session. When one arrives, shared
// "captain.projecticons" changes, which draws every screen again (a mod never keeps a cx for later).
//   import { ProjectIcon } from "./kit/projecticon.js";   ProjectIcon(c, project, 16)
import { div, svg, image } from "gpui-kit";
import { appHome, setShared } from "herdr";
import { api } from "./cst.js";
import { icon } from "./icons.js";
import { Brand } from "./brands.js";

// The colors a glyph can take: readable on light and dark, and distinct at 14 px. The services keep the same list.
export const ICON_COLORS = ["#0043CA", "#7C3AED", "#059669", "#D97706", "#DC2626", "#0891B2", "#DB2777", "#64748B"];

const files = new Map(); // `${pid}:${sha}` -> file name, or null while it's being written

/** The cached logo file for a project, or null until it's written (then every screen draws again). */
export function logoFile(pid, ic) {
  const key = `${pid}:${ic.sha}`;
  if (files.get(key)) return files.get(key);
  if (!files.has(key)) {
    files.set(key, null);
    api(`/api/projects/${encodeURIComponent(pid)}/logo/file`, { body: { dest: `${appHome()}/cache/project-logos` }, scoped: false }).then((r) => {
      if (r.ok && r.data?.file) { files.set(key, r.data.file); setShared("captain.projecticons", Date.now()); }
      else files.delete(key); // asked again next time
    });
  }
  return null;
}

/** A project's icon, `size` px square. `project` is {id, icon?}. */
export function ProjectIcon(c, project, size = 16) {
  const ic = project?.icon;
  if (ic?.kind === "glyph") return svg(icon(ic.name)).size(size).flex_none().text_color(ic.color ?? c.blue);
  if (ic?.kind === "brand") return Brand(c, ic.slug, size);
  if (ic?.kind === "logo" && project?.id) {
    const f = logoFile(project.id, ic);
    if (f) return image(`cache/project-logos/${f}`).size(size).flex_none();
    return div().size(size).flex_none();
  }
  return svg(icon("workflow")).size(size).flex_none().text_color(c.blue);
}

/** The same icon on a soft square, for headers and cards: `box` px, the icon inside at about 55%. */
export function ProjectBadge(c, project, box = 32) {
  const ic = project?.icon;
  const inner = Math.round(box * (ic?.kind === "logo" ? 0.75 : 0.5));
  return div().size(box).flex_none().flex().items_center().justify_center().bg(ic?.kind === "logo" ? c.surface : c.blueLight)
    .when(ic?.kind === "logo", (e) => e.border_1().border_color(c.rule))
    .child(ProjectIcon(c, project, inner));
}
