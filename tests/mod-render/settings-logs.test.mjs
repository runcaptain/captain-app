// Settings, Privacy: the "Logs" group. Two outlined buttons that run the app's commands, and nothing of the log
// itself on the screen: "Copy what happened last time" (logs.copy_last) and "Save logs for a bug report"
// (logs.save_bundle). The app does the copying and the saving, and says where the zip went.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/settings-logs.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { harness } from './harness.mjs';

const { default: Settings } = await import('../../defaults/frame/settings/main.js');

async function privacy() {
  globalThis.__props = { section: 'privacy' };
  globalThis.__runs = [];
  const h = harness(Settings);
  await h.start();
  return h;
}

test('Privacy has a Logs group with the two buttons', async () => {
  const h = await privacy();
  const text = h.text();
  assert.match(text, /LOGS/, 'the group heading');
  for (const label of ['Copy what happened last time', 'Save logs for a bug report']) assert.ok(text.includes(label), `${label}: ${text}`);
  const buttons = h.byIdPrefix('st-logs-');
  assert.deepEqual(buttons.map((b) => b.call('id')[0]), ['st-logs-copy', 'st-logs-save']);
  // Outlined, like every other button in Settings: a border, and the page's raised colour rather than a fill of
  // the accent (a page has at most one filled button, and this group adds none).
  for (const b of buttons) {
    assert.ok(b.call('border_1'), 'a border');
    assert.equal(b.call('bg')?.[0], '#fafbfd', 'the raised page colour, not a fill of the accent');
  }
});

test('each button runs its command and shows nothing raw', async () => {
  const h = await privacy();
  await h.click('st-logs-copy');
  await h.click('st-logs-save');
  assert.deepEqual(globalThis.__runs.map((r) => r[0]), ['logs.copy_last', 'logs.save_bundle']);
  // No log line, path of a log file or error text is drawn by the screen.
  assert.doesNotMatch(h.text(), /app\.log|services\.log|crash-|Traceback|ERROR|WARN /);
});

test('the words are plain', () => {
  const src = fs.readFileSync(new URL('../../defaults/frame/settings/main.js', import.meta.url), 'utf8');
  const group = src.slice(src.indexOf('  logs(t) {'), src.indexOf('  privacy(t) {'));
  assert.ok(group.length > 200, 'found the group');
  const shown = [...group.matchAll(/"([^"]{12,})"/g)].map((m) => m[1]);
  assert.ok(shown.length >= 4);
  for (const s of shown) {
    assert.doesNotMatch(s, /[—–←-⇿]/, `no dashes or arrows: ${s}`);
    assert.doesNotMatch(s, /\b(engine|daemon)\b/i, `plain words: ${s}`);
  }
});
