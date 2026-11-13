// No screen waits on a clock, and no screen uses a timer the app does not have.
//
// tests/mod-render/no-data-timers.test.mjs lists every `timer.every` in Captain's mods. This covers what that
// leaves: the shared kit and the frame, every one-shot `timer.after`, every sleep, and Node's own timers.
//   - A sleep in a loop is a poll: a wait on a job or a deploy listens to the live channel (hear, jobDone in
//     kit/live.js). A row said "Stopping" for two hours because a wait like that stopped asking.
//   - `timer.after` that arms itself again is a poll too. Each one-shot is listed with what it is for.
//   - setTimeout, setInterval and setImmediate do not exist in the app's script engine.
// An entry that matches nothing fails, so the list cannot rot.
//   node tests/mod-render/no-polling-waits.test.mjs
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');

// Why a timer may stay. Nothing else is a reason.
const WHY = {
  clock: 'what ticks on the page as time passes: elapsed time, a spinner, words that depend on the time',
  window: "the window's own state: a shared value another screen set (a link, the picked project, a form that saved), which view is shown",
  typing: 'what the person is typing: a debounce before a check or a search, a draft saved after a pause, an estimate that follows the form',
  starting: 'asking again until the services first answer, or after they stopped. It ends with the first answer',
  channel: "the live channel's own pump: one request held open by the services, not a read on a clock",
  once: 'a one-shot delay for the window itself: focus a field, close an overlay, select a view',
  ration: 'a read a cloud charges for, which has no topic: kept to once a minute on purpose',
  outside: 'a sign-in at an outside service, which cannot tell the feed',
  // The two below ARE reads on a clock. They are named here so they are seen, not hidden: each is a file on this
  // computer, outside the workspace, that a terminal writes and nothing tells the feed about yet.
  'local-file': 'DEBT: a file on this computer outside the workspace, read on a clock because nothing tells the feed about it',
};

// [file under defaults/, text found on the timer's line or the three after it, why]
const ALLOWED = [
  // ---- the kit
  ['captain/kit/live.js', 'cx.timer.every(PUMP_MS, (cx) => livePump(cx));', 'channel'],
  ['captain/kit/cst.js', 'const now = workspace();', 'window'],                       // watchWorkspace
  ['captain/kit/cst.js', 'const now = shared("captain.records.changed");', 'window'],             // watchRecords
  ['captain/kit/cst.js', 'const now = shared("captain.connections.changed") ?? null;', 'window'], // watchConnections
  ['captain/kit/cst.js', 'const now = key();', 'window'],                                          // watchScope
  ['captain/kit/cst.js', 'const now = viewShown(section);', 'window'],                             // keepFresh: is this view the one shown
  ['captain/kit/grid.js', 'if (this.loading) cx.notify();', 'clock'],
  ['captain/kit/mlflow.js', 'shared("captain.mlflow.changed")', 'window'],
  ['captain/kit/view.js', 'publishView(name, read())', 'window'],                     // what this page shows, for agents
  // ---- one-shots and first-answer retries in the mods
  ['mods/captain-weights/main.js', 'cx.timer.after(PULSE_BEAT_MS', 'clock'],          // a pulse's next beat: armed only while something pulses
  ['mods/captain-data/main.js', 'if (p.importWith) cx.timer.after(1500', 'once'],
  ['mods/captain-data/tables.js', 'if (seq === this.loadSeq) this.load(cx);', 'starting'],
  ['mods/captain-runs/regression.js', 'if (seq === this.seq) this.load(cx);', 'starting'],
  ['mods/captain-notebooks/notebooks.js', 'if (seq === this.connSeq) this.loadConns(cx);', 'starting'],
  ['mods/captain-nav/main.js', 'run("layout.select", { stack: props()?.stack ?? "sections", child: "project" })', 'once'],
  ['mods/captain-import/main.js', 'this.loadSources(cx)', 'starting'],
  ['mods/captain-evaluation/main.js', 'if ([o, p, d].some((r) => r.starting))', 'starting'],
  ['mods/captain-rankings/main.js', 'if (Date.now() >= this.due) this.validate(cx);', 'typing'],
  ['mods/captain-compute/main.js', 'else cx.timer.after(1000, (cx) => this.load(cx));', 'starting'],
  ['mods/captain-go/main.js', 'closeOverlay(p.overlay)', 'once'],
  // ---- the frame
  ['frame/palette/main.js', 'this.input.focus()', 'once'],
  ['frame/whatsnew/main.js', 'this.handle.focus()', 'once'],
  ['frame/settings/main.js', 'if (updates().checking) this.asked = Date.now();', 'window'],             // the app's own update check
  ['frame/settings/main.js', 'await this.loadMachines();', 'window'],                                   // the window's machines, not Captain's data
];

// Every screen's own source: the Captain mods, the shared kit, and the frame. A mod's kit/ folder is a copy of the
// shared kit, checked by tests/captain_mods.rs, so it is read once.
const files = [];
const walk = (dir, rel) => {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) { if (e.name !== 'kit' && e.name !== 'icons' && e.name !== 'node_modules') walk(path.join(dir, e.name), `${rel}/${e.name}`); }
    else if (e.name.endsWith('.js')) files.push(`${rel}/${e.name}`);
  }
};
for (const m of fs.readdirSync(path.join(root, 'defaults/mods')).filter((m) => m.startsWith('captain-'))) walk(path.join(root, 'defaults/mods', m), `mods/${m}`);
walk(path.join(root, 'defaults/frame'), 'frame');
for (const f of fs.readdirSync(path.join(root, 'defaults/captain/kit')).filter((f) => f.endsWith('.js'))) files.push(`captain/kit/${f}`);

const TIMER = /\btimer\.(every|after)\(/;
const SLEEP = /exec\(\s*["']sleep["']/;
const NODE_ONLY = /(^|[^.\w])(setTimeout|setInterval|setImmediate)\(/;
const used = new Set();
const problems = [];
let sites = 0;
for (const rel of files) {
  const lines = fs.readFileSync(path.join(root, 'defaults', rel), 'utf8').split('\n');
  lines.forEach((line, i) => {
    const code = line.replace(/\/\/.*$/, '');
    if (SLEEP.test(code)) problems.push(`${rel}:${i + 1} sleeps in a loop. Wait on the live channel: hear() or jobDone() in kit/live.js.`);
    if (NODE_ONLY.test(code)) problems.push(`${rel}:${i + 1} uses a timer the app's script engine does not have. Use cx.timer.`);
    if (!TIMER.test(code)) return;
    // `timer.every` in a mod is the other test's to list.
    if (rel.startsWith('mods/') && /\btimer\.every\(/.test(code)) return;
    sites++;
    const near = lines.slice(i, i + 4).join('\n');
    const hit = ALLOWED.findIndex(([file, has]) => file === rel && near.includes(has));
    if (hit < 0) problems.push(`${rel}:${i + 1} has a timer that is not on the list: ${line.trim().slice(0, 110)}`);
    else used.add(hit);
  });
}
ALLOWED.forEach(([file, has, why], i) => {
  if (!WHY[why]) problems.push(`the list names a reason that is not one: ${why} (${file})`);
  if (!used.has(i)) problems.push(`the list has an entry that matches no timer any more: ${file}: ${has}`);
});
assert.deepEqual(problems, [], `\n${problems.join('\n')}\n\nA screen reads Captain's data when the live channel says it changed (watchLive, keepFresh, hear, jobDone in kit/live.js), ` +
  'not on a clock. If this timer is a clock, a debounce, the window\'s own state or a retry until the services answer, add it to ALLOWED in this file with its reason.');

// The reasons, counted, so the debt is in plain sight.
const by = {};
for (const i of used) by[ALLOWED[i][2]] = (by[ALLOWED[i][2]] ?? 0) + 1;
console.log(`no polling waits: ok (${sites} timers in ${files.length} files: ${Object.entries(by).map(([k, n]) => `${n} ${k}`).join(', ')})`);
