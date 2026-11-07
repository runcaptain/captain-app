// The Explorer's MLflow heading, on fixtures: after Projects and before Scope model, closed at first, a click right
// after launch ignored (the launch-click guard), MLflow read only when the heading first opens, and the open state
// remembered per workspace.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/explorer-mlflow.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-nav-mlflow');
globalThis.__routes = [
  [/^\/api\/explorer/, 'explorer'], [/^\/api\/scope-model/, 'scope-model'], [/^\/api\/engines/, 'empty'], [/^\/api\/backtests/, 'empty'],
  [/^\/api\/mlflow\/experiments$/, 'experiments'], [/^\/api\/mlflow\/models$/, 'models'], [/^\/api\/mlflow$/, 'mlflow'],
];
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://127.0.0.1:9', token: 't' }, 'captain.workspace': 'w1' };
const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-nav-ml-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-nav'), dir, { recursive: true });
fs.copyFileSync(path.join(dir, 'kit/cst.js'), path.join(dir, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(dir, 'kit/cst.js'));
const { Explorer } = await import(path.join(dir, 'explorer.js'));
const { palette } = await import(path.join(dir, 'kit/tokens.js'));

const tasks = [];
const cx = { spawn: (f) => tasks.push(f(cx)), notify() {}, timer: { every() {}, after() {} }, stop_propagation() {} };
const flush = async () => { for (let i = 0; i < 5; i++) { const t = tasks.splice(0); await Promise.all(t); } };
const c = palette(true);
const texts = (e, out = []) => { if (typeof e === 'string') out.push(e); else if (e && typeof e === 'object') for (const k of e.kids ?? []) texts(k, out); return out; };
const find = (e, id) => { if (!e || typeof e !== 'object') return null; if (e.call?.('id')?.[0] === id) return e; for (const k of e.kids ?? []) { const f = find(k, id); if (f) return f; } return null; };
const asked = () => globalThis.__asked ?? [];

const ex = new Explorer();
ex.start(cx);
await flush();
let t = texts(ex.render(c)).join(' | ');
assert.ok(/Projects \|.*Acme \| MLflow \| Scope model/.test(t), `after Projects, before Scope model: ${t}`);
assert.ok(!t.includes('/rankers'), 'closed at first');
assert.ok(!asked().some((p) => p.startsWith('/api/mlflow')), 'MLflow not read while closed');
// A click right after launch is ignored.
find(ex.render(c), 'ex-mlflow').call('on_click')[0]({}, cx);
await flush();
assert.equal(ex.isOpen('mlflow'), false, 'the launch click is ignored');
// A meant click opens it and reads MLflow, once.
ex.firstAt = Date.now() - 5000;
ex.movedAt = 0; // the tree arrived a while ago
find(ex.render(c), 'ex-mlflow').call('on_click')[0]({}, cx);
await flush();
t = texts(ex.render(c)).join(' | ');
assert.ok(t.includes('/rankers') && t.includes('ranker') && t.includes('Read-only'), `open, with MLflow in it: ${t}`);
assert.equal(asked().filter((p) => p === '/api/mlflow').length, 1);
const folds = globalThis.__shared['captain.explorer.folds'];
assert.equal(Object.keys(folds).length, 1, 'one workspace');
assert.deepEqual(Object.values(folds)[0], { mlflow: true }, 'remembered for this workspace');
// Closed and opened again: not read again.
find(ex.render(c), 'ex-mlflow').call('on_click')[0]({}, cx);
find(ex.render(c), 'ex-mlflow').call('on_click')[0]({}, cx);
await flush();
assert.equal(asked().filter((p) => p === '/api/mlflow').length, 1, 'read once');
// A new Explorer for this workspace starts with it open, and reads it then.
const again = new Explorer();
again.start(cx);
await flush();
assert.ok(texts(again.render(c)).join(' | ').includes('/rankers'), 'left open, read at start');
console.log('explorer-mlflow render test: ok');
