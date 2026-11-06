// CAPTAIN: the Captain Search daemon (`cst serve`), from a mod.
// gpui-kit mods can't import outside their own folder, so every captain-* mod
// carries this same file as ./cst.js. tests/captain_mods.rs keeps the copies
// identical to defaults/captain/cst.js: edit that one, then copy it.
//
// The daemon writes ~/.config/captain-search/serve.json {port, token, pid} when
// it starts. Captain starts one itself when a web view needs it (src/cst.rs).
import { exec, readTextFile, home } from "herdr";

/** {base, token} of the running daemon, or null while it isn't up. */
export async function daemon() {
  const text = await readTextFile(`${home()}/.config/captain-search/serve.json`);
  if (!text) return null;
  try {
    const j = JSON.parse(text);
    if (j.port && j.token) return { base: `http://127.0.0.1:${j.port}`, token: j.token, pid: j.pid };
  } catch {}
  return null;
}

/** Call the daemon. Resolves {ok, status, data} or {ok: false, starting} when no
 *  daemon is up yet, or {ok: false, error} with what went wrong. Never rejects. */
export async function api(path, { method = "GET", body, timeout = 20 } = {}) {
  const d = await daemon();
  if (!d) return { ok: false, starting: true, error: "Captain's engine is starting." };
  const args = ["-sS", "--max-time", String(timeout), "-H", `Authorization: Bearer ${d.token}`, "-w", "\n%{http_code}"];
  if (body !== undefined) args.push("-X", method === "GET" ? "POST" : method, "-H", "Content-Type: application/json", "--data-binary", JSON.stringify(body));
  else if (method !== "GET") args.push("-X", method);
  args.push(d.base + path);
  let r;
  try {
    r = await exec("curl", args, { timeout: (timeout + 5) * 1000 });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
  if (r.code !== 0) {
    // Refused: the daemon in serve.json is gone, or still binding its port.
    return { ok: false, starting: r.code === 7, error: r.stderr.trim() || `curl exited ${r.code}` };
  }
  const cut = r.stdout.lastIndexOf("\n");
  const status = Number(r.stdout.slice(cut + 1)) || 0;
  const text = r.stdout.slice(0, cut);
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  const ok = status >= 200 && status < 300;
  return { ok, status, data, error: ok ? null : (data && data.error) || data?.detail || `HTTP ${status}` };
}

/** Light or dark, from the window's background color. */
export function isDark(t) {
  const hex = String(t.background ?? "#ffffff").replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5;
}

/** Captain's own colors, the same tokens as the Captain Search desktop app (index.css). */
export function palette(dark) {
  return dark
    ? { surface: "#0a1430", surface2: "#0e1a3a", navy: "#e6ebf5", blue: "#2f6bff", blueLight: "#13234d", grey900: "#b3bccd", grey500: "#8590a6", grey300: "#2b3858", rule: "#e6ebf51f", fail: "#f97066", onBlue: "#ffffff" }
    : { surface: "#ffffff", surface2: "#fafbfd", navy: "#000f2e", blue: "#0043ca", blueLight: "#f1f5ff", grey900: "#4b515c", grey500: "#6b7280", grey300: "#dcdcdc", rule: "#000f2e1a", fail: "#b42318", onBlue: "#ffffff" };
}
