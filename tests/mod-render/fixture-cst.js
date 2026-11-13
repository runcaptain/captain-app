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
  (globalThis.__bodies ??= []).push([p, opts]);
  // The backend is down: what kit/cst.js answers then (tests/mod-render/backend-down.test.mjs).
  if (globalThis.__down) return { ...globalThis.__down };
  // The engine is not up yet: what kit/cst.js answers while the app starts the daemon.
  if (globalThis.__starting) return { ok: false, starting: true, error: "Captain's engine is starting." };
  // The change feed (kit/live.js). Quiet by default, as a workspace nobody is touching. A test says something
  // changed by putting topics in globalThis.__changed: the next request for the feed answers with them, once.
  if (p.startsWith('/api/changes') && !routes.some(([re]) => re.test(p))) {
    const feed = (globalThis.__feed ??= { seq: 1000 });
    const changed = (globalThis.__changed ?? []).splice(0);
    if (changed.length) feed.seq += 1;
    return { ok: true, status: 200, data: { seq: feed.seq, changed: changed.map((topic) => ({ topic, seq: feed.seq })), reset: false, partial: [] }, error: null };
  }
  // A route may name a method as its third item: [/path/, 'file', 'POST'].
  const method = opts.method ?? (opts.body !== undefined ? 'POST' : 'GET');
  for (const [re, file, want] of routes) {
    if (want && want !== method) continue;
    if (re.test(p)) {
      const data = JSON.parse(fs.readFileSync(path.join(dir, `${file}.json`), 'utf8'));
      // {"error": ...} answers 400; add "__status" for another code (404 for a missing record).
      const failed = data && typeof data === 'object' && !Array.isArray(data) && 'error' in data && Object.keys(data).filter((k) => k !== '__status').length === 1;
      return !failed ? { ok: true, status: 200, data, error: null } : { ok: false, status: data.__status ?? 400, data, error: data.error };
    }
  }
  return { ok: false, status: 404, data: null, error: `no fixture for ${p}` };
}
