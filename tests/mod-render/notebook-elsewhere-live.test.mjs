// An open notebook follows changes made somewhere else, and never overwrites what a person is typing.
//   - Nothing of the person's own on screen: it reloads by itself when its file changes elsewhere.
//   - Typing that is not saved: nothing is touched. One quiet line says "This notebook changed elsewhere. Reload, or
//     keep yours." Reload takes the other change. Keep mine leaves the screen as it is.
// Against the real services. "Elsewhere" is `cst notebook cell add` in a second process, as in a terminal.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/notebook-elsewhere-live.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { cstBin, tidy } from './backend.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { bin: cst, why } = cstBin();
if (!cst) { console.log(`notebook elsewhere: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-nbelse-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst'), CAPTAIN_APP_HOME: path.join(work, 'app-home') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'ne', '--storage', path.join(work, 'store')], { env });
spawnSync(cst, ['demo', 'seed', '--legacy'], { env });
const term = (args) => { const r = spawnSync(cst, args, { env, encoding: 'utf8' }); assert.equal(r.status, 0, `cst ${args.join(' ')}: ${r.stderr.slice(-400)}`); return r.stdout; };
term(['notebook', 'new', 'returns-review']);
term(['notebook', 'cell', 'add', 'returns-review', '--kind', 'markdown', '--source', 'Why are returns up?']);
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
globalThis.__layout = { id: 'root', children: [{ id: 'sections', active: 'notebooks', children: [{ id: 'notebooks' }, { id: 'data' }] }] };
const dir = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'captain-notebooks-ne-')));
fs.cpSync(path.join(root, 'defaults/mods/captain-notebooks'), dir, { recursive: true });
const { default: Mod } = await import(path.join(dir, 'main.js'));
const h = harness(Mod);
await h.start();
const nb = h.mod.nb;
const until = async (ok, what, ms = 8000) => {
  const t0 = performance.now();
  while (!ok() && performance.now() - t0 < ms) { await h.layoutLists({}); await h.idle(30); }
  assert.ok(ok(), `${what}: ${h.text().slice(0, 700)}`);
  return Math.round(performance.now() - t0);
};
const sources = () => nb.cells.map((c) => String(c.source ?? ''));
const byId = (id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);

await until(() => (nb.list ?? []).some((n) => n.name === 'returns-review'), 'the list');
nb.openNotebook(h.cx, 'returns-review');
await until(() => nb.open?.stamp && sources().some((s) => s.includes('Why are returns up?')), 'the notebook opens');
for (let i = 0; i < 8; i++) { await h.layoutLists({}); await h.idle(120); } // the channel holds a request open

// ---- nothing of the person's own on screen: it reloads by itself --------------------------------------------------
term(['notebook', 'cell', 'add', 'returns-review', '--kind', 'markdown', '--source', 'Added from a terminal.']);
let ms = await until(() => sources().some((s) => s.includes('Added from a terminal.')), 'a cell added in a terminal shows in the open notebook');
assert.ok(ms < 1000, `shown ${ms} ms after the command ended`);
assert.equal(byId('nb-elsewhere').length, 0, 'nothing is asked: there was nothing of the person\'s to lose');
assert.ok(h.text().includes('This notebook changed elsewhere. It was read again.'), 'and it says it read it again');
console.log(`  no typing here: reloaded by itself in ${ms} ms`);

// ---- typing that is not saved: nothing is touched, one line asks -----------------------------------------------------
const first = nb.cells[0];
first.source = 'Why are returns up? My own note, not saved yet.';
nb.edited();
nb.lastEdit = Date.now() + 3600000; // the person is still typing: the draft is not written yet
const mine = sources();
term(['notebook', 'cell', 'add', 'returns-review', '--kind', 'markdown', '--source', 'A second line from a terminal.']);
ms = await until(() => byId('nb-elsewhere').length === 1, 'the line that asks');
assert.ok(ms < 1000, `asked ${ms} ms after the command ended`);
assert.ok(h.text().includes('This notebook changed elsewhere. Reload, or keep yours.'), 'in these words');
assert.deepEqual(sources(), mine, 'what the person typed is still on screen, untouched');
assert.ok(byId('nb-elsewhere-reload').length === 1 && byId('nb-elsewhere-keep').length === 1, 'two choices');
for (let i = 0; i < 6; i++) { await h.layoutLists({}); await h.idle(100); }
assert.deepEqual(sources(), mine, 'and it stays untouched however long the line is left');
console.log(`  typing here: asked in ${ms} ms, nothing overwritten`);

// Keep mine: the line goes, the screen is as it was, and the same change is not asked about again.
await h.click('nb-elsewhere-keep');
assert.equal(byId('nb-elsewhere').length, 0);
for (let i = 0; i < 6; i++) { await h.layoutLists({}); await h.idle(100); }
assert.equal(byId('nb-elsewhere').length, 0, 'not asked again about the change that was kept over');
assert.deepEqual(sources(), mine);

// Another change elsewhere asks again. Reload takes it, and the person's unsaved typing is let go because they chose.
term(['notebook', 'cell', 'add', 'returns-review', '--kind', 'markdown', '--source', 'A third line from a terminal.']);
await until(() => byId('nb-elsewhere').length === 1, 'a new change asks again');
await h.click('nb-elsewhere-reload');
await until(() => sources().some((s) => s.includes('A third line from a terminal.')), 'Reload shows the notebook as changed elsewhere');
assert.ok(!sources().some((s) => s.includes('My own note, not saved yet.')), 'the unsaved typing is gone, by the person\'s choice');
assert.equal(byId('nb-elsewhere').length, 0);

// What this screen saves itself is never "elsewhere".
nb.cells[0].source = 'Saved from this screen.';
nb.edited();
nb.lastEdit = 0;
nb.flushDraft(h.cx);
for (let i = 0; i < 10; i++) { await h.layoutLists({}); await h.idle(100); }
assert.equal(byId('nb-elsewhere').length, 0, 'its own draft does not ask');
assert.ok(sources()[0].includes('Saved from this screen.'), 'and its own draft is not reloaded over');
console.log('notebook elsewhere: ok');
process.exit(0);
