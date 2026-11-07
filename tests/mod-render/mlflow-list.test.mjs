// The kit's MLflow list in its edge cases, on fixtures: not connected (one line and "Connect MLflow", nothing read),
// a status read that times out, MLflow refusing the login (said once, not twice), and services that never finish
// starting. In none of them does it sit on the loading placeholder. The title shows once, or not at all under a
// heading.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/mlflow-list.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/mlflow-list');
globalThis.__asked = [];
// fixture-cst.js reads the route list once, at import: keep one list and change what's in it.
const ROUTES = [];
globalThis.__routes = ROUTES;
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://127.0.0.1:9', token: 't' } };
const kit = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-kit-mll-')));
fs.cpSync(path.join(root, 'defaults/captain/kit'), kit, { recursive: true });
fs.copyFileSync(path.join(kit, 'cst.js'), path.join(kit, 'real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(kit, 'cst.js'));
const { MlflowList } = await import(path.join(kit, 'mlflow.js'));
const { palette } = await import(path.join(kit, 'tokens.js'));
const c = palette(true);

// A fake cx: spawned work and timers run when flushed.
const tasks = [];
const cx = { spawn: (f) => tasks.push(f(cx)), notify() {}, timer: { every() {}, after: (_ms, f) => tasks.push(Promise.resolve().then(() => f(cx))) } };
const flush = async () => { for (let i = 0; i < 40 && tasks.length; i++) await Promise.all(tasks.splice(0)); };
const texts = (e, out = []) => { if (typeof e === 'string') out.push(e); else if (e && typeof e === 'object') for (const k of e.kids ?? []) texts(k, out); return out; };
const find = (e, id) => { if (!e || typeof e !== 'object') return null; if (e.call?.('id')?.[0] === id) return e; for (const k of e.kids ?? []) { const f = find(k, id); if (f) return f; } return null; };
const run = async (routes, opts = {}) => {
  ROUTES.splice(0, ROUTES.length, ...routes);
  globalThis.__asked.length = 0;
  const ml = new MlflowList({ id: 'ml', ...opts });
  ml.load(cx);
  await flush();
  return { ml, t: texts(ml.render(c, 300)).join(' | ') };
};

// 1. Not connected: one line and the way to connect, nothing else read, the title once (or not, under a heading).
let setup = 0;
let { ml, t } = await run([[/^\/api\/mlflow$/, 'off']], { onSetup: () => { setup++; } });
assert.ok(t.includes("MLflow isn't connected") && t.includes('Connect MLflow'), t);
assert.equal(t.split(' | ').filter((x) => x === 'MLflow').length, 1, 'title once');
assert.ok(!globalThis.__asked.some((p) => p.startsWith('/api/mlflow/')), 'nothing else read');
find(ml.render(c, 300), 'ml-setup').call('on_click')[0]({}, cx);
assert.equal(setup, 1, 'Connect MLflow opens the form');
({ t } = await run([[/^\/api\/mlflow$/, 'off']], { header: false, onSetup: () => {} }));
assert.ok(!t.split(' | ').includes('MLflow') && t.includes("MLflow isn't connected"), 'no title under a heading');

// 2. The status read times out: the kit's sentence, not the placeholder.
globalThis.__down = { ok: false, timedOut: true, error: 'This took too long.' };
({ ml, t } = await run([]));
delete globalThis.__down;
assert.ok(t.includes('This took too long.') && ml.status?.error, `timed out: ${t}`);

// 3. MLflow refuses the login: the services' sentence, once, for experiments and models alike.
({ t } = await run([[/^\/api\/mlflow$/, 'on'], [/^\/api\/mlflow\/(experiments|models)$/, 'refused']]));
assert.equal(t.split(' | ').filter((x) => x.startsWith('MLflow refused the login')).length, 1, `said once: ${t}`);

// 4. Services that never finish starting: asked again, then a sentence instead of loading for ever.
globalThis.__starting = true;
({ ml, t } = await run([]));
delete globalThis.__starting;
assert.ok(t.includes("Captain's services aren't answering yet"), `gave up waiting: ${t}`);
assert.equal(ml.tries, 16, 'asked again 15 times first');
console.log('mlflow-list render test: ok');
