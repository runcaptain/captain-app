// The shell every designed form shares (captain-forms/main.js), with a faked backend: one Cancel, at the bottom
// beside the main button. The header has only a close mark, named "Close" for screen readers. Every form closes both
// ways (the mark and Cancel). Escape is the dialog's own, handled by the app.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/form-shell.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-forms');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url) => {
  const p = new URL(url).pathname;
  if (p === '/api/cloud/providers') return reply(200, [{ provider: 'aws', label: 'Amazon Web Services', auth: [{ mode: 'profile', label: 'AWS CLI profile', fields: [] }] },
    { provider: 'modal', label: 'Modal', auth: [{ mode: 'local', label: "This computer's Modal sign-in", fields: [] }] }]);
  // Every other read answers with an empty list: a form with nothing to pick from still draws its shell.
  return reply(200, []);
};
const { default: Forms } = await import(path.join(dir, 'main.js'));
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const texts = (e) => (e.kids ?? []).flatMap((x) => (typeof x === 'string' ? [x] : x && typeof x === 'object' ? texts(x) : []));

const { TASKS } = await import(path.join(dir, 'tasks.js'));
assert.ok(TASKS.length >= 25, 'every designed form is checked');
for (const task of TASKS.map((t) => t.id)) {
  for (const how of ['mark', 'cancel']) {
    globalThis.__props = { task, overlay: 7, values: { project: 'shop' } };
    globalThis.__closed = [];
    const h = harness(Forms);
    await h.start();
    for (let i = 0; i < 40 && !byId(h, 'form-cancel').length; i++) await h.idle(30);
    assert.equal(byId(h, 'form-cancel').length, 1, `${task}: the form drew`);
    // One Cancel, at the bottom. The header's close is a mark with no word, and a name for screen readers.
    assert.equal(h.text().split(' | ').filter((t) => t === 'Cancel').length, 1, `${task}: one Cancel, not two: ${h.text().slice(0, 300)}`);
    const mark = byId(h, 'form-close')[0];
    assert.deepEqual(texts(mark), [], `${task}: the header's close shows no word`);
    assert.deepEqual(mark.call('accessibility_label'), ['Close'], `${task}: and is named for screen readers`);
    assert.deepEqual(mark.call('tooltip'), ['Close']);
    assert.deepEqual(texts(byId(h, 'form-cancel')[0]), ['Cancel']);
    if (how === 'mark') await h.click('form-close');
    else await h.click('form-cancel');
    assert.deepEqual(globalThis.__closed, [7], `${task}: ${how} closes the form`);
  }
}
console.log(`form shell render test: ok (${TASKS.length} forms, both ways to close)`);
process.exit(0);
