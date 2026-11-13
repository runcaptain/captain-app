// Stands in for a mod's ./cst.js in the render tests: answers from tests/mod-render/fixtures/<mod>/, keeps the
// real palette, and records every path asked.
import fs from 'node:fs';
import path from 'node:path';
export * from './real-cst.js';
const dir = globalThis.__fixtures;
const routes = globalThis.__routes ?? [];
export const asked = (globalThis.__asked ??= []);
let feedSeq = 100;
export async function api(p, opts = {}) {
  // The change feed (kit/live.js). Quiet unless a test says something changed: globalThis.__feedChanged = ['data']
  // is told once, on the next ask. Not recorded in `asked`: a test counts the screen's reads, not the channel's.
  if (p.startsWith('/api/changes')) {
    if (globalThis.__down) return { ...globalThis.__down };
    if (globalThis.__starting) return { ok: false, starting: true, error: null };
    // globalThis.__feedAlways = ['jobs'] says those topics changed on every ask: a test following a job tick by tick.
    const changed = [...(globalThis.__feedChanged ?? []).splice(0), ...(globalThis.__feedAlways ?? [])];
    if (changed.length) feedSeq += 1;
    return { ok: true, status: 200, data: { seq: feedSeq, changed: changed.map((topic) => ({ topic, seq: feedSeq })), reset: false }, error: null };
  }
  asked.push(p);
  (globalThis.__bodies ??= []).push([p, opts]);
  // The backend is down: what kit/cst.js answers then (tests/mod-render/backend-down.test.mjs).
  if (globalThis.__down) return { ...globalThis.__down };
  // The engine is not up yet: what kit/cst.js answers while the app starts the daemon.
  if (globalThis.__starting) return { ok: false, starting: true, error: "Captain's engine is starting." };
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
