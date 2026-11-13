// A running run never stands still on screen. The owner, deploying to AWS: "freezes on deploying a model, doesn't
// show live time". The deploy was fine; the screen showed "15 s in" for a minute and a half, an empty bar and no
// new line while the image built. The page now counts on its own clock from the run's start and its step's start.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/run-clock.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const { clock, stillWords, liveKind, Live } = await import('../../defaults/mods/captain-runs/live.js');
const { hideAccount, clockWords } = await import('../../defaults/captain/kit/words.js');

// The deploy's run as the services record it: its steps with what each usually takes, the step it is on and when
// that step began (seconds since the run started).
const t0 = Date.parse('2026-11-12T17:00:00Z');
const steps = [
  { title: 'Checking your account', seconds: 1, usually: 'under a minute' },
  { title: 'Creating permissions', seconds: 2, usually: 'under a minute' },
  { title: "Building the model's image", seconds: 92, usually: 'about a minute and a half' },
  { title: 'Starting the model', seconds: 134, usually: 'about 2 minutes' },
  { title: 'Checking it answers', seconds: 1, usually: 'under a minute' },
];
const head = { kind: 'deploy', title: 'Deploying starter-ranker, version 2 to AWS', status: 'running', started: '2026-11-12T17:00:00Z',
  steps, stages: steps.map((s) => s.title), stage: "Building the model's image", stage_at: 3, progress: 0.4 };

// 1. Elapsed time counts up from the start on the page's clock, with nothing new heard since second 15.
let k = clock(head, 15, t0 + 15000);
assert.equal(clockWords(k.elapsed), '15 s');
k = clock(head, 15, t0 + 51000);
assert.equal(clockWords(k.elapsed), '51 s', 'it moved, though the last thing heard was at 15 s');
assert.equal(clockWords(clock(head, 15, t0 + 92000).elapsed), '1 min 32 s');
// 2. The running step has its own time and what is usual for it.
assert.deepEqual([k.step.title, clockWords(k.stepElapsed), k.usually], ["Building the model's image", '48 s', 'about a minute and a half']);
// 3. The quiet line while the step is silent, from the page's clock alone.
assert.equal(stillWords(k), "Still building the model's image. 48 s so far, usually about a minute and a half.");
assert.equal(stillWords(clock({ ...head, steps: undefined }, 15, t0 + 51000)), "Still building the model's image. 48 s so far.", 'no usual time known: none claimed');
assert.equal(stillWords(clock({ ...head, status: 'done' }, 230, t0 + 999000)), null, 'nothing once it is over');
// 4. The bar: the steps done, plus the running step's share by what it usually takes. Never empty, never ahead.
const at = (sec) => clock({ ...head, progress: null }, 0, t0 + sec * 1000).progress;
assert.ok(Math.abs(at(3) - 3 / 230) < 0.001, 'two steps done');
assert.ok(Math.abs(at(49) - (3 + 46) / 230) < 0.001, 'half way through the image');
assert.ok(at(60) > at(30), 'it moves while the step is quiet');
assert.ok(Math.abs(at(400) - (3 + 92 * 0.95) / 230) < 0.001, 'a step that runs long stops short of done');
assert.equal(clock(head, 0, t0 + 4000).progress, 0.4, 'what the run itself said wins when it is ahead');
assert.equal(clock({ ...head, status: 'done' }, 230).progress, 1);
// Steps with no usual time: each counts the same, and the running one creeps.
const plain = { ...head, progress: null, steps: undefined, stage: 'Starting the model', stage_at: 10 };
assert.ok(clock(plain, 0, t0 + 10000).progress === 3 / 5 && clock(plain, 0, t0 + 70000).progress > 3 / 5);
// A finished run shows the time it took, not a clock still running.
assert.equal(clock({ ...head, status: 'done' }, 230, t0 + 9e6).elapsed, 230);

// 5. Its kind is the word "Deploy", and its title a sentence in the page's font with no "RUN" tag.
assert.equal(liveKind({ kind: 'deploy' }), 'Deploy');
assert.equal(liveKind({ kind: 'custom' }), 'Run');
const src = fs.readFileSync(path.join(root, 'defaults/mods/captain-runs/live.js'), 'utf8');
assert.ok(src.includes('.when(h.kind !== "deploy", (x) => x.font_family(MONO))') && src.includes('.when(h.kind !== "deploy", (x) => x.child(this.host.badge(c, kindWord)))'));

// 6. A log line shows an account's number as its last four digits only, wherever it sits.
assert.equal(hideAccount('Uploading to s3://captain-serving-123456789012-us-west-1/bundles/x.tar'), 'Uploading to s3://captain-serving-xxxxxxxx9012-us-west-1/bundles/x.tar');
assert.equal(hideAccount('arn:aws:iam::123456789012:role/captain-serving'), 'arn:aws:iam::xxxxxxxx9012:role/captain-serving');
assert.equal(hideAccount('123456789012 is the account'), 'xxxxxxxx9012 is the account');
assert.equal(hideAccount('took 1234 ms, 20261112 rows, id 1234567890123'), 'took 1234 ms, 20261112 rows, id 1234567890123', 'shorter and longer numbers are left alone');
assert.equal(hideAccount(null), '');
for (const f of ['live.js', 'jobs.js']) assert.ok(fs.readFileSync(path.join(root, 'defaults/mods/captain-runs', f), 'utf8').includes('hideAccount('), `${f} draws log lines through it`);

// 7. Drawn: the header's time, the step's chip, the bar and the quiet line, a second apart with nothing heard.
{
  const realNow = Date.now;
  const host = { icon: () => ({ kind: 'icon' }), badge: (_c, w) => w };
  const live = Object.assign(Object.create(Live.prototype), { host, sel: 'r1', list: [],
    run: { head, last: 15, logs: [{ at: 15, text: 'Building the image in account 123456789012' }], metrics: new Map(), shoppers: [], stages: [], cost: null } });
  const { palette } = await import('../../defaults/mods/captain-runs/kit/tokens.js');
  const text = (e) => (e?.kids ?? []).flatMap((x) => (typeof x === 'string' ? [x] : x && typeof x === 'object' ? text(x) : [])).join(' | ');
  try {
    Date.now = () => t0 + 51000;
    const a = text(live.detail(palette(false), 900));
    Date.now = () => t0 + 52000;
    const b = text(live.detail(palette(false), 900));
    assert.ok(a.includes('51 s in') && b.includes('52 s in'), `the header counts: ${a.slice(0, 200)}`);
    assert.ok(a.includes('48 s, usually about a minute and a half') && b.includes('49 s, usually about a minute and a half'), 'the step chip counts');
    assert.ok(a.includes("Still building the model's image. 48 s so far, usually about a minute and a half."), 'the quiet line');
    assert.ok(a.includes('account xxxxxxxx9012') && !a.includes('123456789012'), 'the log line hides the account');
    assert.ok(a.includes('Deploying starter-ranker, version 2 to AWS') && !a.includes(' | Run | '), 'no Run tag');
    let ticks = 0;
    live.busy = true;
    live.tick({ notify: () => ticks++ });
    assert.equal(ticks, 1, 'the one-second timer redraws a running run even when nothing is fetched');
  } finally { Date.now = realNow; }
}
console.log('run clock: ok');
process.exit(0);
