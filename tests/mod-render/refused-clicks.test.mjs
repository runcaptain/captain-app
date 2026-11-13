// A click that can't be taken because something else is under way is answered with one plain sentence, or kept and
// done after. It is never dropped in silence. (A screen can be a moment behind what is running: a health target
// picked while a check ran was ignored, and nothing said so.) A second press of the thing already running is the
// one exception: it is under way, and the screen says so.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/refused-clicks.test.mjs
import assert from 'node:assert/strict';

const { Ship, waitWords } = await import('../../defaults/mods/captain-project/ship.js');
const { deployTo, healthDo } = await import('../../defaults/mods/captain-project/ship-view.js');
const { default: Page } = await import('../../defaults/mods/captain-project/main.js');

assert.equal(waitWords('Stopping it'), 'Captain is still busy: stopping. Try again when that is done.');
assert.equal(waitWords('Deploying to AWS'), 'Captain is still busy: deploying to AWS. Try again when that is done.');
assert.equal(waitWords(null), 'Captain is still busy with the last thing. Try again in a moment.');
assert.ok(!/[←-⇿—]/.test(waitWords('Rolling back to version 2')));

let spawned = 0, drawn = 0;
const cx = { notify() { drawn++; }, spawn() { spawned++; } };
const ship = () => Object.assign(Object.create(Ship.prototype), { note: null, dnote: null, page: { running() { return this.s.busy; } } });
const said = (s) => s.note?.tone === 'wait' && s.note.text.startsWith('Captain is still busy: ');

// ---- Ship: Stop, roll back, and the small writes ---------------------------------------------------------------
let s = ship(); s.page.s = s; s.busy = 'Rolling back to version 2';
s.stop(cx);
assert.ok(said(s) && s.note.text.includes('rolling back to version 2') && spawned === 0, `Stop during a roll back is answered: ${s.note?.text}`);
s = ship(); s.page.s = s; s.busy = 'Stopping it';
s.stop(cx);
assert.equal(s.note, null, 'Stop pressed again while stopping: the one stop is under way');
s.rollBackTo(cx, 1);
assert.ok(said(s) && spawned === 0, 'roll back during a stop is answered');
s = ship(); s.page.s = s; s.busy = 'Stopping it';
s.act(cx, '/x', {}, 'done');
assert.ok(said(s) && spawned === 0, 'a small write during a stop is answered');
// Deploy pressed while something else runs: answered where the deploy's own words go.
s = ship(); s.page.s = s; s.busy = 'Stopping it'; s.pick = () => null;
deployTo(s, cx, { id: 'local', host: 'local', label: 'This computer' });
assert.ok(s.dnote?.tone === 'wait' && s.dnote.text.includes('stopping') && spawned === 0, 'Deploy during a stop is answered');
// A health target picked while a check runs is kept, and saved when the check is done.
s = ship(); s.healthBusy = true;
healthDo(s, cx, { p95_ms: 200 });
assert.deepEqual(s.healthNext, { p95_ms: 200 }, 'kept for after the check');
assert.equal(spawned, 0);

// ---- the project page: every write it starts ----------------------------------------------------------------------
const page = () => Object.assign(Object.create(Page.prototype), { busy: true, busyWhat: 'Judging demo-tuned', note: null, shipPath: {}, assist: { proposal: {} } });
for (const [name, call] of [['act', (p) => p.act(cx, '/x', {})], ['baselineStart', (p) => p.baselineStart(cx)], ['removeInput', (p) => p.removeInput(cx, { slot: 'events', label: 'Events' })],
  ['startAssist', (p) => p.startAssist(cx)], ['accept', (p) => p.accept(cx)], ['change', (p) => p.change(cx, {}, 'x')]]) {
  const p = page();
  call(p);
  assert.ok(p.note?.tone === 'wait' && p.note.text === 'Captain is still busy: judging demo-tuned. Try again when that is done.', `${name} while something runs is answered: ${p.note?.text}`);
}
assert.equal(spawned, 0, 'and nothing was started');
assert.ok(drawn > 0);
console.log('refused clicks: ok');
process.exit(0);
