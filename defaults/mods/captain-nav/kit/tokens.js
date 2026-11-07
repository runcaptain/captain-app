// captain-ui tokens: the Captain Search desktop app's colors (app/src/index.css), light and dark.
import { theme } from "herdr";

export const FONT = "Inter";
/** The corner radius of every dialog and popup card (the window draws its border at the same radius, src/window.rs).
 *  A dialog's root fill uses it too: GPUI clips children to a rectangle, so a square root would poke out. */
export const RADIUS = 10;
export const MONO = "JetBrains Mono";

/** Light or dark, from the window's background color (the app's theme decides). */
export function isDark(t = theme()) {
  const hex = String(t.background ?? "#ffffff").replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5;
}

/** Captain's colors for the current theme. */
export function palette(dark = isDark()) {
  return dark
    ? { dark: true, canvas: "#060d22", surface: "#0a1430", surface2: "#0e1a3a", navy: "#e6ebf5", blue: "#2f6bff", blueLight: "#13234d",
        grey900: "#b3bccd", grey500: "#8590a6", grey300: "#2b3858", rule: "#e6ebf51f", fail: "#f97066", failLight: "#3a1518",
        good: "#32d583", goodLight: "#0d2b1f", warn: "#fdb022", warnLight: "#3a2a0a", selection: "#1e3a80", onBlue: "#ffffff" }
    : { dark: false, canvas: "#f6f7fa", surface: "#ffffff", surface2: "#fafbfd", navy: "#000f2e", blue: "#0043ca", blueLight: "#f1f5ff",
        grey900: "#4b515c", grey500: "#6b7280", grey300: "#dcdcdc", rule: "#000f2e1a", fail: "#b42318", failLight: "#fef3f2",
        good: "#067647", goodLight: "#ecfdf3", warn: "#b54708", warnLight: "#fffaeb", selection: "#dbe5ff", onBlue: "#ffffff" };
}
