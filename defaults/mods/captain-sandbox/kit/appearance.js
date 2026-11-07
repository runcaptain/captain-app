// captain-ui: the window's Light, Dark or System appearance (settings.json `appearance`).
// One switch for everything: Captain's sidebar, terminals and dialogs follow it,
// and so does any mod reading tokens.js.
import { appHome, readTextFile, writeTextFile } from "herdr";

const file = () => `${appHome()}/settings.json`;

/** "light", "dark" or "system". */
export async function getAppearance() {
  try {
    const p = JSON.parse((await readTextFile(file())) ?? "{}");
    return p.appearance === "light" || p.appearance === "dark" ? p.appearance : "system";
  } catch {
    return "system";
  }
}

/** Set it, keeping the rest of settings.json (retries if someone else saved meanwhile). */
export async function setAppearance(value) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const raw = await readTextFile(file());
    let p = {};
    try { p = raw ? JSON.parse(raw) : {}; } catch { return false; }
    if (value === "light" || value === "dark") p.appearance = value;
    else delete p.appearance;
    try {
      await writeTextFile(file(), JSON.stringify(p, null, 2) + "\n", raw);
      return true;
    } catch {}
  }
  return false;
}
