// The app's live data, fixed: a light theme and nothing running.
export const theme = () => globalThis.__theme ?? { background: '#ffffff', foreground: '#000f2e', border: '#dcdcdc', muted: '#4b515c', dim: '#6b7280', accent: '#0043ca', surface: '#ffffff', raised: '#fafbfd' };
export const run = (name, args) => (globalThis.__runs ??= []).push([name, args]);
export const props = () => globalThis.__props ?? {};
export const shared = () => undefined;
export const setShared = () => {};
export const layout = () => ({ id: 'root', children: [] });
export const agents = () => [];
export const openOverlay = () => 1;
export const closeOverlay = () => {};
export const notify = () => {};
export const exec = async () => ({ code: 1, stdout: '', stderr: 'no exec in tests' });
export const readTextFile = async () => null;
export const home = () => '/tmp';
export const window = () => ({ platform: 'macos', fullscreen: false });
