// captain-ui: what the person is looking at, kept current for agents.
//
// Each screen says what it shows in one small object (its project, tab, selected item, the "Next:" line), under
// shared "captain.view.<name>". The window puts those together with the section shown, the workspace and the open
// dialogs (`captain-app view`), and `cst app context` prints the result with what the services know about it.
// An agent in a Captain terminal runs that instead of asking "which project do you mean?".
//
// Rules: never call these from render. Never publish a secret: fields() drops them, and cst checks again.
import { shared, setShared, layout } from "herdr";

/** Section ids, in the words the screens use (defaults/AGENTS.md, "Words"). */
export const SCREENS = {
  project: "Projects", data: "Data", notebooks: "Notebooks", runs: "Runs", deployments: "Deployments", team: "Team",
};

// Field names that hold a secret, whatever the form calls them. The services' own list (commands.py SECRET_NAMES),
// widened: a wrong guess here only hides a harmless value.
// A session id in a row of search events names a shopper's visit, not a login: it is shown.
const SECRET_KEY = /pass(word|phrase)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization|bearer|cookie|\bdsn\b|\bpat\b/i;
// Values that are a secret by their shape: a long opaque string, a known key prefix, a PEM block.
// The same pattern is in the services (cli/appctl.py SECRET_VALUE): change both.
const SECRET_VALUE = /^(?:(?:sk|pk|rk)[-_][-_A-Za-z0-9]{16,}|gh[pous]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[-A-Za-z0-9]{10,}|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[-_A-Za-z0-9]{30,}|ya29\.[-_A-Za-z0-9.]{20,}|eyJ[-_A-Za-z0-9]{10,}\.[-_A-Za-z0-9.]{10,}|dapi[a-f0-9]{24,}|hf_[A-Za-z0-9]{20,}|glpat-[-_A-Za-z0-9]{16,}|shpat_[a-f0-9]{20,})$|-----BEGIN [A-Z ]*PRIVATE KEY-----/;
const HIDDEN = "(hidden)";

/** A value safe to show an agent: a URL keeps its host and loses its login, a secret-shaped string is hidden,
 *  long text is cut. */
export function safeValue(v, depth = 0) {
  if (v === null || v === undefined) return v;
  if (typeof v === "number" || typeof v === "boolean") return v;
  // A slice is small and flat. Anything nested deeper than this is not one (a record with a loop in it, say).
  if (depth > 5) return null;
  if (Array.isArray(v)) return v.slice(0, 20).map((x) => safeValue(x, depth + 1));
  if (typeof v === "object") return safeFields(v, [], depth + 1);
  let s = String(v);
  if (SECRET_VALUE.test(s.trim())) return HIDDEN;
  // scheme://user:password@host and scheme://token@host
  s = s.replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+@/gi, `$1${HIDDEN}@`);
  // ?token=..., &password=..., ;Password=...
  s = s.replace(/([?&;]\s*[^=&;\s]*(?:pass(?:word)?|pwd|secret|token|key|sig(?:nature)?|credential)[^=&;\s]*=)[^&;\s]+/gi, `$1${HIDDEN}`);
  return s.length > 300 ? `${s.slice(0, 300)}...` : s;
}

/** A form's filled fields, safe to show an agent. `secret` names the keys the form itself marks secret. Empty
 *  fields are left out; a secret that is filled reads "(hidden)", so the agent knows it was given. */
export function safeFields(values, secret = [], depth = 0) {
  const out = {};
  const marked = new Set(secret);
  for (const [k, v] of Object.entries(values ?? {}).slice(0, 60)) {
    if (v === undefined || v === null || v === "" || typeof v === "function") continue;
    out[k] = marked.has(k) || SECRET_KEY.test(k) ? HIDDEN : safeValue(v, depth);
  }
  return out;
}

const LAST = {};

/** Say what this screen shows now. `name` is the section id ("project", "data", ...) or "dialog" for a form over
 *  the page. `slice` is small and plain: {project, tab, selected, next, ...}; null clears it. Writes only when it
 *  changed, so calling it often is cheap. Never call it from render. */
export function publishView(name, slice) {
  const key = `captain.view.${name}`;
  const value = slice ? { ...safeValue(slice), workspace: shared("captain.workspace") ?? null } : null;
  const text = JSON.stringify(value);
  if (LAST[key] === undefined) LAST[key] = JSON.stringify(stripAt(shared(key)));
  if (LAST[key] === text) return false;
  LAST[key] = text;
  setShared(key, value ? { ...value, at: Date.now() } : null);
  return true;
}

function stripAt(v) {
  if (!v || typeof v !== "object") return v ?? null;
  const { at: _at, ...rest } = v;
  return rest;
}

/** Keep this screen's part current: `read()` builds the slice from the view's own state (no requests), and it is
 *  published when it differs from the last one. At most one write every 700 ms, and none while nothing changes.
 *  Nothing runs inside the caller's init: the first reading is on the timer too, so a screen starts as fast as before. */
export function watchView(cx, name, read) {
  cx.timer.every(700, () => { try { publishView(name, read()); } catch { /* a screen mid-load: try again next time */ } });
}

function find(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = find(child, id);
    if (found) return found;
  }
  return null;
}

/** The section shown: the active child of the layout's "sections" stack. */
export function currentSection() {
  const node = find(layout(), "sections");
  return node?.active ?? node?.children?.[0]?.id ?? null;
}

/** What the person is looking at, from this mod's side: {workspace, scope, section, screen, view, dialog}. */
export function currentView() {
  const section = currentSection();
  const ws = shared("captain.workspace") ?? null;
  const mine = (v) => (v && (v.workspace ?? ws) === ws ? v : null);
  const scope = shared("captain.scope") ?? null;
  return { workspace: ws, scope: scope?.id ? { kind: scope.kind, id: scope.id, label: scope.label ?? null } : null, section, screen: SCREENS[section] ?? section,
    view: mine(shared(`captain.view.${section}`)), dialog: mine(shared("captain.view.dialog")) };
}

const line = (label, v) => (v === undefined || v === null || v === "" ? null : `- ${label}: ${typeof v === "object" ? JSON.stringify(v) : v}`);

/** The same, as a few lines to put at the top of a prompt. Empty when nothing is known yet. */
export function viewSnapshot() {
  const v = currentView();
  if (!v.workspace && !v.section) return "";
  const s = v.view ?? {};
  const project = s.project ?? (v.scope?.kind === "project" ? { id: v.scope.id, name: v.scope.label } : null);
  const rest = Object.entries(s).filter(([k]) => !["project", "next", "workspace", "at", "tab"].includes(k));
  const rows = [
    line("Workspace", v.workspace),
    line("Project", project ? `${project.name ?? project.id}${project.name && project.id && project.name !== project.id ? ` (id ${project.id})` : ""}${project.kind ? `, ${project.kind}` : ""}` : null),
    !project && v.scope ? line("Explorer pick", `${v.scope.kind} ${v.scope.label ?? v.scope.id}`) : null,
    line("Screen", [v.screen, s.tab].filter(Boolean).join(", tab ")),
    ...rest.map(([k, val]) => line(k[0].toUpperCase() + k.slice(1).replace(/_/g, " "), val)),
    v.dialog ? line("Open form", `${v.dialog.title ?? v.dialog.form}${Object.keys(v.dialog.fields ?? {}).length ? ` ${JSON.stringify(v.dialog.fields)}` : ""}`) : null,
    s.next?.text ? line("Next", `${s.next.text}${s.next.label ? ` (button: ${s.next.label})` : ""}`) : null,
  ].filter(Boolean);
  if (!rows.length) return "";
  return `What the person has open in Captain right now:\n${rows.join("\n")}\nFor the live details run \`cst app context\`, and \`cst app shot\` for a picture of the window.`;
}

/** A prompt that starts with what the person is looking at. Prompts that already carry it are left alone. */
export function withView(prompt) {
  const text = String(prompt ?? "");
  if (!text || text.includes("What the person has open in Captain right now:")) return text;
  const snap = viewSnapshot();
  if (!snap) return text;
  // A prompt the services wrote starts by naming the cst to run: that stays first, and the screen comes next.
  const cut = text.startsWith("Use this cst:") ? text.indexOf("\n\n") : -1;
  return cut < 0 ? `${snap}\n\n${text}` : `${text.slice(0, cut)}\n\n${snap}${text.slice(cut)}`;
}
