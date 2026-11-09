// A project's icon, against a real backend: the picker (an icon found by name, in a palette color; a brand mark, the
// ones matching the name first; the organization's own logo, dropped in), and the icon wherever the project shows:
// the Explorer row, Home's card and the project page's header. A logo is drawn from a small cached copy.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/project-icon-live.test.mjs
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
if (!cst) { console.log(`project icon render test: skipped (${why})`); process.exit(0); }
const work = tidy(fs.mkdtempSync(path.join(os.tmpdir(), 'cst-icon-')));
const env = { ...process.env, CST_HOME: path.join(work, 'cst') };
delete env.CLAUDECODE;
spawnSync(cst, ['init', 'ic', '--storage', path.join(work, 'store')], { env });
const serve = spawn(cst, ['serve'], { env, stdio: ['ignore', 'pipe', 'ignore'] });
const stop = () => { try { serve.kill('SIGTERM'); } catch {} };
process.on('exit', stop);
process.on('uncaughtException', (e) => { stop(); console.error(e); process.exit(1); });
const line = await new Promise((res, rej) => { let b = ''; serve.stdout.on('data', (d) => { b += d; if (b.includes('\n')) res(b.split('\n')[0]); }); serve.on('exit', () => rej(new Error('cst serve exited'))); });
const j = JSON.parse(line);
const base = `http://127.0.0.1:${j.port}`;
const H = { Authorization: `Bearer ${j.token}`, 'Content-Type': 'application/json' };
const post = async (p, body) => { const r = await fetch(base + p, { method: 'POST', headers: H, body: JSON.stringify(body) }); assert.ok(r.ok, `${p}: ${r.status}`); return r.json(); };
const get = async (p) => (await fetch(base + p, { headers: H })).json();
const iconOf = async (id) => (await get('/api/projects')).find((p) => p.id === id)?.icon ?? null;
await post('/api/projects', { name: 'Outdoor' });
await post('/api/projects', { name: 'Snowflake analytics' });
// The stub's app home: where the logo's cached copy goes.
const cache = '/tmp/captain-app/cache/project-logos';
process.on('exit', () => { try { fs.rmSync(cache, { recursive: true, force: true }); } catch {} });

globalThis.__shared = { 'captain.daemon': { state: 'ready', base, token: j.token } };
const copy = (m) => { const d = tidy(fs.mkdtempSync(path.join(os.tmpdir(), `${m}-`))); fs.cpSync(path.join(root, 'defaults/mods', m), d, { recursive: true }); return d; };
const { default: Picker } = await import(path.join(copy('captain-project-icon'), 'main.js'));
const open = async (project) => { globalThis.__props = { project }; const h = harness(Picker); await h.start(); return h; };
const settle = async (h, ok, what) => { for (let i = 0; i < 40 && !(await ok()); i++) await h.idle(100); assert.ok(await ok(), what); };

// An icon by name, in a palette color.
let h = await open({ id: 'outdoor', name: 'Outdoor', icon: null });
let t = h.text();
assert.ok(t.includes('Icon for Outdoor') && t.includes('Brand mark') && t.includes('Logo'), 'the three ways');
h.mod.search.set_value('store');
h.mod.search.handlers.change?.({}, h.cx);
await h.idle(20);
assert.ok(h.byIdPrefix('pi-g-').length >= 1 && h.byIdPrefix('pi-g-store').length === 1 && h.byIdPrefix('pi-g-database').length === 0, 'search narrows the icons');
await h.click('pi-g-store');
await h.click('pi-c-059669');
await h.click('pi-save');
await settle(h, async () => (await iconOf('outdoor'))?.name === 'store', 'saved');
assert.deepEqual(await iconOf('outdoor'), { kind: 'glyph', name: 'store', color: '#059669' });

// A brand mark: the one in the project's name comes first.
h = await open({ id: 'snowflake-analytics', name: 'Snowflake analytics', icon: null });
await h.click('pi-tab-brand');
const first = h.byIdPrefix('pi-b-').map((e) => e.call('id')[0]).find((x) => !x.startsWith('pi-b-pad'));
assert.equal(first, 'pi-b-snowflake', 'the name match first');
assert.ok(h.text().includes('name match'));
await h.click('pi-b-snowflake');
await h.click('pi-save');
await settle(h, async () => (await iconOf('snowflake-analytics'))?.slug === 'snowflake', 'brand saved');

// The organization's own logo, dropped in: into the workspace store, then the project's icon.
const logo = path.join(work, 'org-logo.png');
const py = path.join(path.dirname(cst), 'python');
assert.equal(spawnSync(py, ['-c', `from PIL import Image; Image.new("RGBA", (400, 200), (30, 90, 200, 255)).save("${logo}")`]).status, 0);
h = await open({ id: 'outdoor', name: 'Outdoor', icon: await iconOf('outdoor') });
await h.click('pi-tab-logo');
assert.ok(h.text().includes("doesn't ship anyone's logo"), 'says where a logo lives');
await h.fire('pi-drop', 'on_drop', { paths: [logo] });
await settle(h, async () => (await iconOf('outdoor'))?.kind === 'logo', 'logo saved');

// Everywhere the project shows: the project page's header, Home's card, the Explorer row. The logo draws from the
// cached copy (a small square PNG).
globalThis.__shared['captain.scope'] = { kind: 'project', id: 'outdoor', label: 'Outdoor' };
const { default: Project } = await import(path.join(copy('captain-project'), 'main.js'));
const pg = harness(Project);
await pg.start();
const imageOf = (hh) => { const out = []; const walk = (e) => { if (!e || typeof e !== 'object') return; if (e.kind === 'image') out.push(e.args?.[0]); for (const k of e.kids ?? []) walk(k); }; walk(hh.mod.render(hh.cx)); return out; };
for (let i = 0; i < 60 && !imageOf(pg).some((x) => String(x).startsWith('cache/project-logos/')); i++) { await pg.layoutLists({}); await pg.idle(100); }
const drawn = imageOf(pg).find((x) => String(x).startsWith('cache/project-logos/'));
assert.ok(drawn, 'the header draws the logo');
assert.ok(fs.existsSync(path.join('/tmp/captain-app', drawn)), 'from its cached copy');
globalThis.__shared['captain.scope'] = null;
pg.mod.allProjects(pg.cx);
for (let i = 0; i < 60 && !(pg.text().includes('Snowflake analytics') && pg.mod.summaries?.outdoor); i++) { await pg.layoutLists({}); await pg.idle(100); }
assert.ok(imageOf(pg).some((x) => String(x).startsWith('cache/project-logos/')), 'Home cards draw it too');

const { Explorer } = await import(path.join(copy('captain-nav'), 'explorer.js'));
const ex = new Explorer();
const ecx = { spawn: (f) => f(ecx), timer: { every() {}, after() {} }, notify() {} };
ex.start(ecx);
for (let i = 0; i < 40 && !ex.tree; i++) await new Promise((r) => setTimeout(r, 100));
const { palette } = await import(path.join(copy('captain-nav'), 'kit/tokens.js'));
const walkKinds = (e, out = []) => { if (e && typeof e === 'object') { if (e.kind === 'image' || e.kind === 'svg') out.push(e.args?.[0]); for (const k of e.kids ?? []) walkKinds(k, out); } return out; };
// The first draw asks for the logo's cached copy; the app draws again when it lands (shared captain.projecticons).
let marks = walkKinds(ex.render(palette(false)));
for (let i = 0; i < 40 && !marks.some((x) => String(x).startsWith('cache/project-logos/')); i++) { await new Promise((r) => setTimeout(r, 100)); marks = walkKinds(ex.render(palette(false))); }
assert.ok(marks.some((x) => String(x).startsWith('cache/project-logos/')), 'the Explorer row draws the logo');
assert.ok(marks.includes('icons/brand-snowflake.svg'), 'and the brand mark');
const acts = ex.actions({ kind: 'project', id: 'outdoor', name: 'Outdoor', icon: null });
assert.equal(acts[0].label, 'Change icon');
assert.equal(acts[0].overlay.mod, 'captain-project-icon');
console.log('project icon render test: ok');
process.exit(0);
