// captain-ui tokens: the Captain Search desktop app's colors (app/src/index.css), light and dark.
import { theme } from "herdr";

export const FONT = "Instrument Sans";
/** The corner radius of every dialog and popup card (the window draws its border at the same radius, src/window.rs).
 *  A dialog's root fill uses it too: GPUI clips children to a rectangle, so a square root would poke out. */
export const RADIUS = 10;
export const MONO = "Geist Mono";

/** Light or dark, from the window's background color (the app's theme decides). */
export function isDark(t = theme()) {
  const hex = String(t.background ?? "#ffffff").replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5;
}

/** Captain's colors for the current theme. */
export function palette(dark = isDark()) {
  return dark
    ? { dark: true, canvas: "#000000", surface: "#090b10", surface2: "#11141b", navy: "#ffffff", blue: "#6f9bff", solid: "#0043ca", blueLight: "#0043ca26",
        grey900: "#c0c3ca", grey500: "#9298a3", grey300: "#30343d", rule: "#ffffff24", fail: "#f97066", failLight: "#3a1518",
        good: "#32d583", goodLight: "#0d2b1f", warn: "#fdb022", warnLight: "#3a2a0a", selection: "#0043ca66", onBlue: "#ffffff" }
    : { dark: false, canvas: "#f6f7fa", surface: "#ffffff", surface2: "#fafbfd", navy: "#000f2e", blue: "#0043ca", solid: "#0043ca", blueLight: "#f1f5ff",
        grey900: "#4b515c", grey500: "#6b7280", grey300: "#dcdcdc", rule: "#000f2e1a", fail: "#b42318", failLight: "#fef3f2",
        good: "#067647", goodLight: "#ecfdf3", warn: "#b54708", warnLight: "#fffaeb", selection: "#dbe5ff", onBlue: "#ffffff" };
}
