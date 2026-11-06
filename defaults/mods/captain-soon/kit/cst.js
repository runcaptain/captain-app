// captain-ui: the Captain Search daemon (`cst serve`), from a mod.
// The app starts or reuses the daemon and keeps shared "captain.daemon" current:
// {state: "ready", base, token}, or {state: "starting" | "missing" | "failed", error}.
// Older homes fall back to ~/.config/captain-search/serve.json.
import { exec, readTextFile, home, shared } from "herdr";

/** {base, token} of the running daemon; {waiting, error} while it isn't up. */
export async function daemon() {
  const d = shared("captain.daemon");
  if (d?.base && d?.token) return { base: d.base, token: d.token };
  if (d?.state === "missing" || d?.state === "failed") return { waiting: false, error: d.error };
  const text = await readTextFile(`${home()}/.config/captain-search/serve.json`);
  try {
    const j = text ? JSON.parse(text) : null;
    if (j?.port && j?.token) return { base: `http://127.0.0.1:${j.port}`, token: j.token };
  } catch {}
  return { waiting: true, error: "Captain's engine is starting." };
}

/** Call the daemon. Resolves {ok, status, data, error, starting}; never rejects. */
export async function api(path, { method = "GET", body, timeout = 20 } = {}) {
  const d = await daemon();
  if (!d.base) return { ok: false, starting: Boolean(d.waiting), error: d.error };
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
    // 7: refused. The daemon is still binding its port, or it went away.
    return { ok: false, starting: r.code === 7, error: r.code === 7 ? "Captain's engine is starting." : r.stderr.trim() || `curl exited ${r.code}` };
  }
  const cut = r.stdout.lastIndexOf("\n");
  const status = Number(r.stdout.slice(cut + 1)) || 0;
  const text = r.stdout.slice(0, cut);
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  const ok = status >= 200 && status < 300;
  return { ok, status, data, error: ok ? null : (data && (data.error || data.detail)) || `HTTP ${status}` };
}

/** A query string from an object, skipping empty values. */
export function query(params) {
  const parts = Object.entries(params ?? {}).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}
