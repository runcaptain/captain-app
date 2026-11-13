// The rule every form follows, enforced in the shell (captain-forms/main.js, checks.js) so no form can skip it:
// fields are checked as the person types, a problem sits under its own field, the main button is off while a field
// has a problem and says why, a name takes any words, and nothing raw from the services reaches the screen.
// With a faked backend.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/forms-rule.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-forms');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
const RAW = "bad name 'gpu cloud': use lowercase letters, digits, . _ - and up to 8 folder levels split by /";
const sent = [];
let world = {};
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url), p = u.pathname, body = opts.body ? JSON.parse(opts.body) : null;
  sent.push([opts.method ?? 'GET', p + u.search, body]);
  if (p === '/api/cloud/providers') return reply(200, [{ provider: 'aws', label: 'Amazon Web Services', auth: [{ mode: 'profile', label: 'AWS CLI profile', fields: [] }] },
    { provider: 'modal', label: 'Modal', auth: [{ mode: 'local', label: "This computer's Modal sign-in", fields: [] }] }]);
  if (p === '/api/names/propose') {
    const label = u.searchParams.get('label'), id = label.trim().toLowerCase().replace(/[\s_]+/g, '-').replace(/[^a-z0-9-]/g, '');
    const taken = (world.taken ?? []).includes(id);
    return reply(200, { label, id, taken, next: taken ? `${id}-2` : id, text: taken ? `You already have a cloud account called ${id}. Captain will call this one ${id}-2.` : `Captain will call it ${id}.` });
  }
  if (p === '/api/cloud/signin') return reply(200, world.signedIn ? { found: true, text: null } : { found: false, text: 'No Modal sign-in on this computer yet. Run `modal token new` in a terminal, then come back.' });
  if (p === '/api/cloud/profiles' && opts.method === 'POST') return world.refuse ? reply(400, { error: world.refuse }) : reply(200, { name: body.name });
  if ((opts.method ?? 'GET') !== 'GET') return reply(400, { error: RAW });   // every other write is refused with the raw sentence
  return reply(200, []);
};
const { default: Forms } = await import(path.join(dir, 'main.js'));
const { TASKS } = await import(path.join(dir, 'tasks.js'));
const { idFrom, isId, sayError, looksRaw } = await import(path.join(dir, 'checks.js'));
const { palette } = await import(path.join(dir, 'kit/tokens.js'));
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const text = (h, id) => { const e = byId(h, id)[0]; const t = (x) => (typeof x === 'string' ? x : (x?.kids ?? []).map(t).join('')); return e ? t(e) : null; };
const open = async (task, values = {}) => {
  globalThis.__props = { task, overlay: 7, values };
  const h = harness(Forms);
  await h.start();
  for (let i = 0; i < 40 && !byId(h, 'form-cancel').length; i++) await h.idle(30);
  return h;
};
// A person typing, then pausing: the box changes, time passes, and the form looks again (its 250 ms timer).
const type = async (h, key, value) => { h.mod.states[key].set_value(value); h.mod.values[key] = value; await h.layoutLists({}); };
const pause = async (h, ms = 450) => { await new Promise((r) => setTimeout(r, ms)); await h.layoutLists({}); await h.idle(30); await h.layoutLists({}); };
const canPress = (h) => Boolean(byId(h, 'form-go')[0].call('on_click'));

// ---- the id rule is the services' rule ---------------------------------------------------------------------------
for (const [typed, made] of [['gpu cloud', 'gpu-cloud'], ['GPU cloud', 'gpu-cloud'], ['  GPU   Cloud ', 'gpu-cloud'], ['gpu_cloud', 'gpu-cloud'], ['Work (us-west)', 'work-us-west'], ['a/b\\c', 'abc'], ['--x--', 'x']]) {
  assert.equal(idFrom(typed), made, typed);
  assert.ok(isId(made));
}
assert.equal(idFrom('!!!', 'login'), 'login', 'nothing usable is never an error');

// ---- Connect a cloud account: the name Edgar typed --------------------------------------------------------------
world = { signedIn: true };
let h = await open('cloud');
await h.click('f-provider-modal');
for (let i = 0; i < 20 && h.mod.values.auth === undefined; i++) await h.idle(30);
await h.layoutLists({});
let t = h.text();
assert.ok(t.includes("This computer's Modal sign-in") && !t.includes('modal token new'), `no command in a label: ${t}`);
// Before anything is typed: the button is off and says why, and nothing is red.
assert.ok(!canPress(h), 'the main button is off while the name is empty');
assert.equal(text(h, 'form-why'), 'Give it a name.', 'and the reason is one line away');
assert.equal(byId(h, 'f-name-problem').length, 0, 'a field nobody has touched shows no problem');
// Typing "gpu cloud": no rejection. Captain says what it will call it, by itself, after a pause.
await type(h, 'name', 'gpu cloud');
assert.equal(byId(h, 'f-name-problem').length, 0, 'a name is never refused for its spelling');
await pause(h);
assert.equal(text(h, 'f-name-id-text'), 'Captain will call it gpu-cloud.', `the id, shown small under the field: ${h.text()}`);
assert.ok(sent.some((x) => x[1].startsWith('/api/names/propose?label=gpu%20cloud&kind=cloud')), 'asked by itself, with no button pressed');
assert.ok(canPress(h) && text(h, 'form-why') === '', 'and the button is on');
// Clearing the name: the problem is under the field at once, and goes the moment it is fixed.
await type(h, 'name', '');
assert.equal(text(h, 'f-name-problem'), 'Give it a name.');
assert.ok(!canPress(h));
await type(h, 'name', 'gpu cloud');
assert.equal(byId(h, 'f-name-problem').length, 0, 'it clears the moment it is fixed');
await pause(h);
// The id can be opened and changed, and a bad one is said under the field without leaving the form.
await h.click('f-name-id-change');
h.mod.idStates.name.set_value('GPU Cloud!');
await h.layoutLists({});
assert.equal(text(h, 'f-name-problem'), "Captain's own name for it uses lower-case letters, digits and hyphens, like gpu-cloud.");
assert.ok(!canPress(h));
await h.click('f-name-id-change');                       // back to Captain's
await h.layoutLists({});
assert.ok(canPress(h));
await h.click('form-go');
for (let i = 0; i < 30 && !sent.some((x) => x[0] === 'POST' && x[1] === '/api/cloud/profiles'); i++) await h.idle(30);
assert.deepEqual(sent.find((x) => x[0] === 'POST' && x[1] === '/api/cloud/profiles')[2], { name: 'gpu-cloud', label: 'gpu cloud', provider: 'modal', auth: 'local', fields: {}, secrets: {} },
  'the id Captain made is sent, with the words typed as the label');

// A name that is taken: said under the field, with the next free one, and that one is sent.
world = { signedIn: true, taken: ['gpu-cloud'] };
sent.length = 0;
h = await open('cloud');
await h.click('f-provider-modal');
for (let i = 0; i < 20 && h.mod.values.auth === undefined; i++) await h.idle(30);
await type(h, 'name', 'GPU cloud');
await pause(h);
assert.equal(text(h, 'f-name-id-text'), 'You already have a cloud account called gpu-cloud. Captain will call this one gpu-cloud-2.');
await h.click('form-go');
for (let i = 0; i < 30 && !sent.some((x) => x[0] === 'POST' && x[1] === '/api/cloud/profiles'); i++) await h.idle(30);
assert.equal(sent.find((x) => x[0] === 'POST' && x[1] === '/api/cloud/profiles')[2].name, 'gpu-cloud-2');

// The Modal sign-in is looked for by itself. Not found: one muted line under the choice, and the button is off.
world = { signedIn: false };
h = await open('cloud');
await h.click('f-provider-modal');
for (let i = 0; i < 20 && h.mod.values.auth === undefined; i++) await h.idle(30);
await type(h, 'name', 'gpu cloud');
await pause(h);
assert.equal(text(h, 'f-auth-problem'), 'No Modal sign-in on this computer yet. Run `modal token new` in a terminal, then come back.', `under "Sign in with": ${h.text()}`);
assert.ok(!canPress(h));
// The person signs in from a terminal and comes back: it is found by itself, with nothing pressed.
world = { signedIn: true };
h.mod.asked.auth.at = 0;                                  // the few seconds between looks have passed
await pause(h, 50);
assert.equal(byId(h, 'f-auth-problem').length, 0, 'found on the next look');
assert.ok(canPress(h));

// ---- what comes back after the button is never raw, and goes under its field ----------------------------------
world = { signedIn: true, refuse: RAW };
h = await open('cloud');
await h.click('f-provider-modal');
for (let i = 0; i < 20 && h.mod.values.auth === undefined; i++) await h.idle(30);
await type(h, 'name', 'gpu cloud');
await pause(h);
await h.click('form-go');
for (let i = 0; i < 30 && !h.mod.result; i++) await h.idle(30);
await h.idle(30);
t = h.text();
assert.ok(!t.includes('bad name') && !t.includes('folder levels') && !looksRaw(t), `the raw sentence never reaches the screen: ${t}`);
assert.equal(text(h, 'f-name-problem'), "That name can't be used as it is. Change it and try again.", 'it is said under the name field, in plain words');

// sayError: which field, and never raw.
assert.deepEqual(sayError(RAW, [{ key: 'name', label: 'Name', name: 'cloud' }]), { field: 'name', text: "That name can't be used as it is. Change it and try again." });
assert.deepEqual(sayError('Region is required', [{ key: 'region', label: 'Region' }]), { field: 'region', text: 'Region is required.' });
for (const raw of ['bad request: nope', 'Traceback (most recent call last): boom', 'KeyError: \'x\'', '{"detail": "x"}', 'Internal Server Error (error 500)', 'HTTP 502: upstream'])
  assert.ok(!looksRaw(sayError(raw, []).text) && sayError(raw, []).text.length > 10, `${raw} -> ${sayError(raw, []).text}`);
assert.equal(sayError("Captain couldn't reach that address.", []).text, "Captain couldn't reach that address.", 'a sentence that is already plain is left alone');

// ---- every form: the button is off while something required is empty, and no form shows raw text or a red box --
const c = palette(false);
const boxes = (h) => h.byIdPrefix('').filter((e) => e.call('bg')?.[0] === c.failLight && e.call('border_color')?.[0] === c.fail).length;
const submitOnly = [];
for (const task of TASKS) {
  const required = task.fields.filter((f) => f.required && !f.when);
  h = await open(task.id, { project: 'shop' });
  await h.layoutLists({});
  const missing = required.filter((f) => { const v = h.mod.values[f.key]; return v === undefined || v === null || String(v).trim() === ''; });
  if (missing.length) {
    assert.ok(!canPress(h), `${task.id}: the main button is off while ${missing[0].key} is empty`);
    assert.ok((text(h, 'form-why') ?? '').length > 3, `${task.id}: and the reason is said: "${text(h, 'form-why')}"`);
  }
  // Whatever its submit answers, the screen never shows it raw, and never in a red box at the bottom.
  h.mod.task = { ...h.mod.task, blocked: undefined, submit: async () => ({ ok: false, error: RAW }) };
  h.mod.blocked = () => null;
  await h.mod.submit(h.cx);
  for (let i = 0; i < 20 && !h.mod.result; i++) await h.idle(20);
  await h.idle(20);
  t = h.text();
  assert.ok(!looksRaw(t) && !t.includes('folder levels'), `${task.id}: nothing raw on screen: ${t.slice(-300)}`);
  assert.equal(boxes(h), 0, `${task.id}: no red box at the bottom`);
  // For the report: a form whose own submit() refuses something it could have said while typing.
  const src = String(TASKS.find((x) => x.id === task.id).submit);
  if (/return \{ ok: false, error:/.test(src)) submitOnly.push(task.id);
}
console.log(`  ${TASKS.length} forms checked. Forms whose submit still refuses something by itself: ${submitOnly.join(', ') || 'none'}`);
console.log('forms rule render test: ok');
process.exit(0);
