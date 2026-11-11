// The app's live data, fixed: a light theme and nothing running.
export const theme = () => globalThis.__theme ?? { background: '#ffffff', foreground: '#000f2e', border: '#dcdcdc', muted: '#4b515c', dim: '#6b7280', accent: '#0043ca', surface: '#ffffff', raised: '#fafbfd' };
export const run = (name, args) => (globalThis.__runs ??= []).push([name, args]);
export const props = () => globalThis.__props ?? {};
export const shared = (k) => globalThis.__shared?.[k];
export const setShared = (k, v) => ((globalThis.__shared ??= {})[k] = v);
export const layout = () => globalThis.__layout ?? { id: 'root', children: [] };
export const agents = () => globalThis.__agentList ?? [];
// Terminal commands a mod runs (hc send ...), recorded.
export const hc = async (args) => { (globalThis.__hc ??= []).push(args); return { code: 0, stdout: '', stderr: '' }; };
export const openOverlay = (o) => { (globalThis.__overlays ??= []).push(o); return 1; };
export const closeOverlay = (id) => { (globalThis.__closed ??= []).push(id ?? null); };
export const notify = (n) => { (globalThis.__toasts ??= []).push(n); return String(globalThis.__toasts.length); };
// A test that needs real programs (forms-e2e.mjs) sets globalThis.__exec.
export const exec = async (...a) => (globalThis.__exec ? globalThis.__exec(...a) : { code: 1, stdout: '', stderr: 'no exec in tests' });
export const readTextFile = async (p) => (globalThis.__files ?? {})[p] ?? null;
// As the app does (src/mod_files.rs): a file is only replaced when the caller says what text it replaces, and a
// new file is only made when none is there. Anything else is refused, in the app's own words.
export const writeTextFile = async (p, text, expected) => {
  const now = (globalThis.__files ?? {})[p] ?? null;
  if (now !== (expected ?? null)) throw new Error("The file changed while saving. Reload before saving.");
  (globalThis.__files ??= {})[p] = text; (globalThis.__written ??= []).push(p); return null;
};
export const home = () => '/tmp';
export const window = () => ({ platform: 'macos', fullscreen: false });
export const focused = () => ({ space: globalThis.__space ?? "s1" });
export const knownAgents = async () => globalThis.__agents ?? [];
export const spaces = () => globalThis.__spaces ?? [];
export const pickFolder = async () => globalThis.__pickFolder ?? null;
export const appBinary = () => "/usr/bin/false";
// The palette (frame/palette): the app's commands, layouts and key bindings.
export const commands = () => globalThis.__commands ?? [];
export const layouts = () => globalThis.__layouts ?? [];
export const keys = () => globalThis.__keys ?? [];
// captain-nav's sidebar.
export const appHome = () => '/tmp/captain-app';
export const machines = () => globalThis.__machines ?? [];
// A test that starts a mod with a command loop (captain-nav) sets __nextCommand to one that waits, as the app's does.
export const nextCommand = () => (globalThis.__nextCommand ? globalThis.__nextCommand() : Promise.resolve(null));
