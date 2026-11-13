// Connect a cloud account signs in with the browser. The owner, looking at "No Modal sign-in on this computer yet.
// Run `modal token new` in a terminal, then come back." in red, twice: "I should be able to get a modal log in
// with a link." So: one outlined button under the choice, the link opened for the person, a line that says it is
// waiting, the end heard on the live channel, and never a command to type. With a faked backend.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/cloud-signin.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-forms');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
const LINK = 'https://modal.example/token-flow/tf-demo123';
const RAW = 'AuthError: Traceback (most recent call last): token flow was rejected';
let sent = [];
let world = {};
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url), p = u.pathname, m = opts.method ?? 'GET', body = opts.body ? JSON.parse(opts.body) : null;
  sent.push([m, p + u.search, body]);
  // The change feed: a request held open until the test says something changed.
  if (p === '/api/changes') {
    world.listened = (u.searchParams.get('topics') ?? '').split(',').filter(Boolean);
    if (!Number(u.searchParams.get('since'))) return reply(200, { seq: world.seq = (world.seq ?? 1000) + 1, changed: [] });
    return new Promise((answer) => { world.tell = (changed) => answer(reply(200, { seq: world.seq += 1, changed })); });
  }
  if (p === '/api/cloud/providers') return reply(200, [{ provider: 'aws', label: 'Amazon Web Services', auth: [{ mode: 'profile', label: 'AWS CLI profile', fields: [] }] },
    { provider: 'modal', label: 'Modal', auth: [{ mode: 'local', label: "This computer's Modal sign-in", fields: [
      { name: 'environment', label: 'Modal environment (blank for your default)', secret: false, required: false },
      { name: 'modal_profile', label: 'Modal profile', secret: false, required: false, hidden: true }] }] }]);
  if (p === '/api/names/propose') { const label = u.searchParams.get('label'), id = label.trim().toLowerCase().replace(/\s+/g, '-'); return reply(200, { label, id, taken: false, next: id, text: `Captain will call it ${id}.` }); }
  if (p === '/api/cloud/signin') {
    if (u.searchParams.get('provider') !== 'modal') return reply(200, { found: null, text: null, why: null, browser: false });
    const found = world.signedIn || (world.kept ?? []).includes(u.searchParams.get('profile'));
    return reply(200, found ? { found: true, text: null, why: null, browser: true } : { found: false, text: 'No Modal sign-in on this computer yet.', why: 'Sign in to Modal first.', browser: true });
  }
  if (p === '/api/cloud/signin/start') return world.start ? reply(200, world.start) : reply(500, { detail: RAW });
  if (/^\/api\/cloud\/signin\/jobs\/[^/]+\/stop$/.test(p)) return reply(200, { id: 'j1', state: 'stopped' });
  if (p.startsWith('/api/cloud/signin/jobs/')) return world.job ? reply(200, world.job) : reply(404, { detail: 'gone' });
  if (p === '/api/cloud/profiles' && m === 'POST') return reply(200, { name: body.name });
  if (m !== 'GET') return reply(200, {});
  return reply(200, []);
};
const { default: Forms } = await import(path.join(dir, 'main.js'));
const { liveReset } = await import(path.join(dir, 'kit/live.js'));
const { palette } = await import(path.join(dir, 'kit/tokens.js'));
const { MONO } = await import(path.join(dir, 'kit/tokens.js'));
const { looksRaw } = await import(path.join(dir, 'kit/checks.js'));
const c = palette(false);
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const text = (h, id) => { const e = byId(h, id)[0]; const t = (x) => (typeof x === 'string' ? x : (x?.kids ?? []).map(t).join('')); return e ? t(e) : null; };
const type = async (h, key, value) => { h.mod.states[key].set_value(value); h.mod.values[key] = value; await h.layoutLists({}); };
const pause = async (h, ms = 450) => { await new Promise((r) => setTimeout(r, ms)); await h.layoutLists({}); await h.idle(30); await h.layoutLists({}); };
const canPress = (h) => Boolean(byId(h, 'form-go')[0].call('on_click'));
const count = (hay, needle) => hay.split(needle).length - 1;
const filled = (h) => h.byIdPrefix('').filter((e) => e.call('bg')?.[0] === c.solid).length;
const WAITING = { id: 'j1', provider: 'modal', label: 'Modal', state: 'waiting', link: LINK, code: 'ABCD-1234', text: null, result: null, install: null };
// The dialog on Modal with a name typed: the state Edgar's picture shows.
const open = async (w) => {
  liveReset(); world = w; sent = [];
  globalThis.__props = { task: 'cloud', overlay: 7, values: {} };
  const h = harness(Forms);
  await h.start();
  h.opened = []; h.copied = [];
  h.cx.open_url = (url) => h.opened.push(url);
  h.cx.write_to_clipboard = (s) => h.copied.push(s);
  for (let i = 0; i < 40 && !byId(h, 'form-cancel').length; i++) await h.idle(30);
  await h.click('f-provider-modal');
  for (let i = 0; i < 20 && h.mod.values.auth === undefined; i++) await h.idle(30);
  await type(h, 'name', 'gpu cloud');
  await pause(h);
  return h;
};
const starts = () => sent.filter((x) => x[0] === 'POST' && x[1] === '/api/cloud/signin/start');
const until = async (h, f) => { for (let i = 0; i < 60 && !f(); i++) await h.idle(30); await h.layoutLists({}); };
// The live channel says the sign-in changed: the dialog reads the job once.
const told = async (h, job) => { await until(h, () => world.tell && world.listened?.includes('signin:j1')); world.job = job; const tell = world.tell; world.tell = null; tell([{ topic: 'signin:j1', seq: 1 }]); await h.idle(120); await h.layoutLists({}); };

// ---- no sign-in on this computer: a task, said once, and never a command ---------------------------------------
let h = await open({ signedIn: false, start: WAITING });
let t = h.text();
assert.equal(text(h, 'f-auth-problem'), 'No Modal sign-in on this computer yet.', t);
assert.equal(count(t, 'No Modal sign-in on this computer yet.'), 1, `said once, not under the choice and at the bottom as well: ${t}`);
assert.equal(text(h, 'form-why'), 'Sign in to Modal first.', 'the bottom line says why the button is off in its own few words');
for (const gone of ['token new', 'terminal', 'Run `', 'then come back']) assert.ok(!t.includes(gone), `no command to type: "${gone}" in ${t}`);
assert.notEqual(byId(h, 'f-auth-problem')[0].call('text_color')?.[0], c.fail, 'not signed in yet is not a mistake: it is not in red');
assert.equal(text(h, 'f-auth-signin-go'), 'Sign in with your browser');
assert.notEqual(byId(h, 'f-auth-signin-go')[0].call('bg')?.[0], c.solid, 'an outlined button');
assert.equal(filled(h), 0, 'and the main button is off, so nothing is filled');
assert.ok(!canPress(h));
assert.equal(byId(h, 'f-af:modal_profile').length, 0, 'the profile Captain keeps is not a field a person fills');

// ---- pressing it: the link is opened, and the dialog says it is waiting ---------------------------------------
await h.click('f-auth-signin-go');
await until(h, () => h.mod.signin?.state === 'waiting');
assert.deepEqual(starts().at(-1)[2], { provider: 'modal' });
assert.deepEqual(h.opened, [LINK], 'the link is opened for the person, once');
assert.equal(text(h, 'f-auth-signin-waiting'), 'Waiting for you to finish signing in in your browser.');
assert.equal(text(h, 'f-auth-signin-code'), 'ABCD-1234');
assert.equal(byId(h, 'f-auth-signin-code')[0].call('font_family')?.[0], MONO, 'the code is in the code font');
assert.equal(byId(h, 'f-auth-signin-waiting')[0].call('font_family'), undefined, 'and nothing else is');
await h.click('f-auth-signin-copy');
assert.deepEqual(h.copied, ['ABCD-1234']);
assert.equal(text(h, 'f-auth-signin-open'), 'Open the link again');
await h.click('f-auth-signin-open');
assert.deepEqual(h.opened, [LINK, LINK]);
assert.equal(text(h, 'f-auth-signin-stop'), 'Stop waiting');
assert.equal(byId(h, 'f-auth-problem').length, 0, 'while it waits, the "no sign-in yet" line is not shown as well');
assert.ok(!h.text().includes(LINK), 'the address is opened, not printed');
assert.ok(!canPress(h));

// ---- it finishes in the browser: heard on the live channel, and the dialog goes on by itself --------------------
const before = sent.length;
world.kept = ['captain'];
await told(h, { ...WAITING, state: 'done', text: 'Signed in to Modal, workspace acme.', result: { profile: 'captain', workspace: 'acme', active: false } });
await pause(h);
assert.equal(h.mod.signin, null);
assert.equal(text(h, 'f-auth-note'), 'Signed in to Modal, workspace acme.', h.text());
assert.equal(byId(h, 'f-auth-problem').length, 0);
assert.ok(canPress(h) && text(h, 'form-why') === '', 'the other fields were fine, so the main button is on');
assert.equal(filled(h), 1, 'one filled button');
assert.equal(sent.slice(before).filter((x) => x[1].startsWith('/api/cloud/signin/jobs/')).length, 1, 'the job was read once, when the channel said so: not on a clock');
assert.equal(text(h, 'f-auth-signin-go'), 'Sign in to another account');
await h.click('form-go');
await until(h, () => sent.some((x) => x[0] === 'POST' && x[1] === '/api/cloud/profiles'));
assert.deepEqual(sent.find((x) => x[0] === 'POST' && x[1] === '/api/cloud/profiles')[2],
  { name: 'gpu-cloud', label: 'gpu cloud', provider: 'modal', auth: 'local', fields: { modal_profile: 'captain' }, secrets: {} }, 'the sign-in just made is the one this account uses');

// The first sign-in on a computer becomes the one it uses: nothing extra is kept on the login.
h = await open({ signedIn: false, start: WAITING });
await h.click('f-auth-signin-go');
await until(h, () => h.mod.signin?.state === 'waiting');
world.signedIn = true;
await told(h, { ...WAITING, state: 'done', text: 'Signed in to Modal, workspace acme.', result: { profile: 'captain', workspace: 'acme', active: true } });
await pause(h);
assert.equal(text(h, 'f-auth-note'), 'Signed in to Modal, workspace acme.');
assert.equal(h.mod.values['af:modal_profile'], undefined);
assert.ok(canPress(h));

// ---- it fails, or runs out of time: one plain sentence and "Try again" -----------------------------------------
for (const [state, said] of [['failed', 'Modal did not finish the sign-in. Try again.'], ['timed_out', 'The sign-in was not finished within 10 minutes. Try again.']]) {
  h = await open({ signedIn: false, start: WAITING });
  await h.click('f-auth-signin-go');
  await until(h, () => h.mod.signin?.state === 'waiting');
  await told(h, { ...WAITING, state, text: said });
  t = h.text();
  assert.equal(text(h, 'f-auth-signin-problem'), said, t);
  assert.equal(count(t, said), 1);
  assert.equal(byId(h, 'f-auth-problem').length, 0, 'one sentence, not two');
  assert.ok(!looksRaw(t) && !t.includes('Traceback'), t);
  assert.equal(text(h, 'f-auth-signin-again'), 'Try again');
  assert.ok(!canPress(h) && filled(h) === 0);
  const n = starts().length;
  await h.click('f-auth-signin-again');
  await until(h, () => h.mod.signin?.state === 'waiting');
  assert.equal(starts().length, n + 1, 'Try again starts it again');
}

// The services can't start it: Captain's own sentence, never what came back.
h = await open({ signedIn: false, start: null });
await h.click('f-auth-signin-go');
await until(h, () => h.mod.signin?.state === 'failed');
t = h.text();
assert.equal(text(h, 'f-auth-signin-problem'), "Captain couldn't start the sign-in. Try again.");
assert.ok(!t.includes('AuthError') && !t.includes('Traceback') && !looksRaw(t), t);

// ---- stop waiting ------------------------------------------------------------------------------------------
h = await open({ signedIn: false, start: WAITING });
await h.click('f-auth-signin-go');
await until(h, () => h.mod.signin?.state === 'waiting');
await h.click('f-auth-signin-stop');
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'POST' && x[1] === '/api/cloud/signin/jobs/j1/stop'), 'the services are told to stop the sign-in');
assert.equal(text(h, 'f-auth-signin-go'), 'Sign in with your browser');
assert.equal(text(h, 'f-auth-problem'), 'No Modal sign-in on this computer yet.');
// Closing the dialog while it waits stops it too.
await h.click('f-auth-signin-go');
await until(h, () => h.mod.signin?.state === 'waiting');
sent = [];
h.mod.close();
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'POST' && x[1] === '/api/cloud/signin/jobs/j1/stop'), 'a closed dialog leaves no sign-in running');

// ---- the cloud's tool is not on this computer ---------------------------------------------------------------
h = await open({ signedIn: false, start: { id: 'j2', provider: 'modal', label: 'Modal', state: 'missing_tool', link: null, code: null, text: 'Captain needs the Modal tool to sign in.', result: null, install: 'pip install modal' } });
await h.click('f-auth-signin-go');
await until(h, () => h.mod.signin?.state === 'missing_tool');
assert.equal(text(h, 'f-auth-signin-problem'), 'Captain needs the Modal tool to sign in.');
assert.equal(text(h, 'f-auth-signin-install'), 'Copy the install line');
assert.ok(!h.text().includes('pip install'), 'the line is copied, not shown as something to type');
await h.click('f-auth-signin-install');
assert.deepEqual(h.copied, ['pip install modal']);
assert.deepEqual(h.opened, []);

// ---- a sign-in is already here: nothing is in the way, and another account is one button ------------------------
h = await open({ signedIn: true, start: WAITING });
assert.equal(byId(h, 'f-auth-problem').length, 0);
assert.ok(canPress(h));
assert.equal(text(h, 'f-auth-signin-go'), 'Sign in to another account');
assert.equal(filled(h), 1, 'one filled button');

// A cloud Captain has no browser sign-in for shows no such button.
await h.click('f-provider-aws');
for (let i = 0; i < 20 && h.mod.values.auth !== 'profile'; i++) await h.idle(30);
await pause(h);
assert.equal(byId(h, 'f-auth-signin-go').length, 0);

console.log('cloud sign-in render test: ok');
process.exit(0);
