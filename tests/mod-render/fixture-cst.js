// Stands in for a mod's ./cst.js in the render tests: answers from tests/mod-render/fixtures/<mod>/, keeps the
// real palette, and records every path asked.
import fs from 'node:fs';
import path from 'node:path';
export * from './real-cst.js';
const dir = globalThis.__fixtures;
const routes = globalThis.__routes ?? [];
export const asked = (globalThis.__asked ??= []);
export async function api(p, opts = {}) {
  asked.push(p);
  // The backend is down: what kit/cst.js answers then (tests/mod-render/backend-down.test.mjs).
  if (globalThis.__down) return { ...globalThis.__down };
  // The engine is not up yet: what kit/cst.js answers while the app starts the daemon.
  if (globalThis.__starting) return { ok: false, starting: true, error: "Captain's engine is starting." };
  for (const [re, file] of routes) {
    if (re.test(p)) {
      const data = JSON.parse(fs.readFileSync(path.join(dir, `${file}.json`), 'utf8'));
      const ok = !(data && typeof data === 'object' && !Array.isArray(data) && 'error' in data && Object.keys(data).length === 1);
      return ok ? { ok: true, status: 200, data, error: null } : { ok: false, status: 400, data, error: data.error };
    }
  }
  return { ok: false, status: 404, data: null, error: `no fixture for ${p}` };
}
