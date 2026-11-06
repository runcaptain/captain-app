// captain-ui icons: Lucide glyphs the app installs in the home's icons/ folder
// (defaults/icons/captain-<name>.svg), so every mod uses the same ones without
// carrying them. svg() tints them with the text color.
//   import { icon } from "./kit/icons.js";   svg(icon("database")).size(16)
export const ICONS = [
  "search", "database", "play", "git-compare", "send", "settings", "terminal", "plus", "panel-left", "sun", "moon",
  "monitor", "flask-conical", "waypoints", "tag", "square-check", "rewind", "workflow", "compass", "rocket", "layers",
  "file-text", "table", "bar-chart-3", "chart-line", "activity", "bot", "user", "copy", "refresh-cw", "triangle-alert",
  "circle-check", "circle-x", "x", "check", "chevron-right", "chevron-left", "chevron-down", "external-link", "link",
  "clock", "filter", "arrow-up-down", "boxes", "sparkles",
];

/** The home path of a Captain icon, for svg(). Unknown names fall back to "search". */
export function icon(name) {
  return `icons/captain-${ICONS.includes(name) ? name : "search"}.svg`;
}
