// No screen keeps a timer to re-read Captain's data. A screen reads once when it shows, again the moment its topics
// change on the one live channel (kit/live.js), and again right after its own action returns. Timers are only for
// what ticks on the page itself.
//
// Every `timer.every` in Captain's mods is listed here with what it is for. A timer that is not listed fails this
// test, so a new one has to be argued for. `todo` marks a timer that still reads data and has not been moved to
// the channel yet: the list of those may only shrink.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/no-data-timers.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname);
const mods = path.resolve(here, '../../defaults/mods');

// [file, a piece of the timer's line, what it is, kind]
//   clock     ticks on the page: elapsed time, "Updated 4 s ago", a spinner. Reads nothing.
//   typing    waits on the person: a search asked a moment after typing stops, a field checked as it is typed.
//   signal    reads a value another screen set in the app (a link to open, a place picked): not Captain's data.
//   outside   asks a service that is not Captain's on that service's own pace, or one that charges per read.
//   todo      still re-reads Captain's data on a timer.
const LEDGER = [
  ['captain-deployments/main.js', 'this.giveUp(cx); cx.notify();', 'the page clock: seconds on a deploying row, "Updated 4 s ago", giving up on a silent job', 'clock'],
  ['captain-deployments/main.js', 'this.page?.refresh(cx)', "an open model's traffic and cost from the cloud's own metrics, which the cloud charges for by the read", 'outside'],
  ['captain-deployments/main.js', 'this.takeOpen(cx)', 'a link from another screen to open a model', 'signal'],
  ['captain-data/main.js', 'this.catalog.tick(cx)', 'the search box: asked a moment after typing stops', 'typing'],
  ['captain-data/main.js', 'shared("captain.open")', 'a link from another screen to open a table', 'signal'],
  ['captain-runs/main.js', 'this.priceNow(cx)', 'the price of a run where it is set to run, asked again when the choice changes', 'typing'],
  ['captain-runs/main.js', 'placeChanged()', 'the place to run work picked on the cloud chip', 'signal'],
  ['captain-runs/main.js', 'this.cmp.tick(cx)', "the comparison's search box, and the pages its grid asked for while drawing", 'typing'],
  ['captain-runs/main.js', 'this.live.clock(cx)', "the page clock: a running run's and a running job's elapsed time; a finished run's cost asked a few times", 'clock'],
  ['captain-runs/main.js', 'shared("captain.open")', 'a link from another screen to open a run', 'signal'],
  ['captain-notebooks/main.js', 'this.nb.tick(cx)', "saving a draft a moment after typing stops, a running cell's clock, a typed path looked for", 'typing'],
  ['captain-notebooks/main.js', 'shared("captain.open")', 'a link from another screen to open a notebook', 'signal'],
  ['captain-project/main.js', 'shared("captain.open")', 'a link from another screen to open a project', 'signal'],
  ['captain-project/main.js', 'captain.explorer.rev', 'a form said the Explorer changed', 'signal'],
  ['captain-nav/main.js', 'captain.context.changed', 'the things pinned for agents: a shared value, and their file re-read every 3 seconds', 'todo'],
  ['captain-nav/main.js', 'captain.agent.open', 'a request to open the agent pane', 'signal'],
  ['captain-nav/main.js', 'placeChanged()', 'the place to run work picked on the cloud chip', 'signal'],
  ['captain-nav/explorer.js', 'this.openMlflow(cx)', 'the MLflow heading opened before its list had loaded', 'signal'],
  ['captain-nav/explorer.js', 'captain.explorer.rev', 'a form said the Explorer changed', 'signal'],
  ['captain-nav/explorer.js', 'captain.scope', "the Explorer's own pick changed", 'signal'],
  ['captain-nav/teamviews.js', 'this.sync(cx)', "the team's shared views: checked every 2 seconds", 'todo'],
  ['captain-team/main.js', 'this.login', 'a sign-in in progress, asked about at the pace the sign-in service sets', 'outside'],
  ['captain-engine/main.js', 'this.asking', 'the price of a training run while its form is open', 'typing'],
  ['captain-engine/main.js', 'captain.explorer.rev', 'a form said the Explorer changed', 'signal'],
  ['captain-evaluation/main.js', 'this.previewDue', 'a table previewed a moment after its path is typed, and the price of the run', 'typing'],
  ['captain-import/main.js', 'this.tick(cx)', 'the import wizard: a price and a name checked as they are typed', 'typing'],
  ['captain-forms/main.js', 'this.priceNow(cx)', 'the price of what a form is about to start', 'typing'],
  ['captain-forms/main.js', 'this.watch(cx)', 'every field looked at a few times a second, to check it as it is typed', 'typing'],
];
// A mod moved to the live channel has nothing left to do: no `todo` of its own.
const DONE = ['captain-deployments', 'captain-data', 'captain-runs', 'captain-import', 'captain-notebooks', 'captain-project', 'captain-cloud', 'captain-handoff', 'captain-team'];
// The most timers still reading data. It only goes down.
const TODO_MAX = 2;

const found = [];
for (const mod of fs.readdirSync(mods).filter((m) => m.startsWith('captain-')).sort()) {
  for (const f of fs.readdirSync(path.join(mods, mod)).filter((f) => f.endsWith('.js')).sort()) {
    const lines = fs.readFileSync(path.join(mods, mod, f), 'utf8').split('\n');
    // A timer whose body starts on the next line is matched on that line and the two after it.
    lines.forEach((line, i) => { if (/\btimer\.every\(/.test(line)) found.push({ file: `${mod}/${f}`, line: (/=>\s*\{\s*$/.test(line) ? lines.slice(i, i + 3) : [line]).map((x) => x.trim()).join(' '), n: i + 1 }); });
  }
}
const used = new Set();
for (const t of found) {
  const order = LEDGER.map((e, i) => i).sort((a, b) => LEDGER[b][1].length - LEDGER[a][1].length);
  const k = order.find((i) => !used.has(i) && LEDGER[i][0] === t.file && t.line.includes(LEDGER[i][1])) ?? -1;
  assert.ok(k >= 0, `${t.file}:${t.n} has a timer that is not in the ledger. If it re-reads Captain's data, use watchLive (kit/live.js). If it ticks on the page, list it with what it is for:\n  ${t.line.slice(0, 160)}`);
  used.add(k);
}
LEDGER.forEach(([file, piece], i) => assert.ok(used.has(i), `the ledger lists a timer that is gone: ${file} "${piece}". Remove the entry.`));
const todo = LEDGER.filter(([, , , kind]) => kind === 'todo');
assert.ok(todo.length <= TODO_MAX, `${todo.length} timers still re-read data; the most allowed is ${TODO_MAX}`);
for (const [file] of todo) assert.ok(!DONE.some((m) => file.startsWith(`${m}/`)), `${file} is a converted mod and may not have a timer that reads data`);
// Every mod that reads Captain's lists watches the channel.
for (const m of ['captain-deployments', 'captain-data', 'captain-runs', 'captain-notebooks', 'captain-project', 'captain-nav', 'captain-cloud', 'captain-team', 'captain-handoff']) {
  const src = fs.readFileSync(path.join(mods, m, 'main.js'), 'utf8');
  assert.ok(/watchLive\(cx,|keepFresh\(cx,/.test(src), `${m} watches the live channel`);
}
const byKind = {};
for (const [, , , kind] of LEDGER) byKind[kind] = (byKind[kind] ?? 0) + 1;
console.log(`timers in Captain's mods: ${found.length} (${Object.entries(byKind).map(([k, n]) => `${n} ${k}`).join(', ')})`);
for (const [file, , what] of todo) console.log(`  still reads data on a timer: ${file}: ${what}`);
console.log('no data timers: ok');
process.exit(0);
