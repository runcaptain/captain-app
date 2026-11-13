// "Ask an agent" on the Weights page, for a network Captain's services hold: a goal box with the three starters
// as goals to fill in, "Start the agent" (Captain's services start it and give the verdict), its progress in the
// strip and the timeline, and "Stop the agent". Nothing starts on Enter, and nothing is read on a clock.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-program.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { starter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { goalStarters, agentWorking, MAX_EDITS } = await import('../../defaults/mods/captain-weights/program.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

const SID = 'ws-1', JOB = 'job-7';
let asked = [], events = [], job = null, refuse = null, feed;
const ledger = { raw_weights: true, weights_read: 0, sandbox: false, forward_allowed: 20, forward_used: 0, changed: 0, total: 13 };
weightsTransport(async (path, opts) => {
  asked.push([path.split('?')[0], opts?.method ?? 'GET', opts?.body]);
  const p = path.split('?')[0];
  if (p === '/api/weights/models/tiny') return { ok: true, data: { ...starter(TINY), id: 'tiny', name: 'tiny', test: [] } };
  if (p === '/api/weights/models/tiny/session') return { ok: true, data: { id: SID, model: 'tiny', ledger, events } };
  if (p === `/api/weights/sessions/${SID}/program`) {
    if (refuse) return { ok: false, error: refuse };
    job = { status: 'running', message: 'The agent read the shape of the network.' };
    events = [...events, { id: 'e1', seq: 1, by: 'captain', kind: 'program', sentence: `Captain started the agent claude with the goal: ${opts.body.goal} It may make at most 5 edits.`, program: { agent: 'claude', goal: opts.body.goal } }];
    return { ok: true, data: { job: JOB, session: SID, agent: 'claude', goal: opts.body.goal } };
  }
  if (p === `/api/weights/sessions/${SID}/program/stop`) return job?.status === 'running' ? { ok: true, data: { session: SID, stopping: true } } : { ok: false, error: 'No agent is working in this session.' };
  if (p === `/api/jobs/${JOB}`) return { ok: true, data: job };
  return { ok: false, error: 'Not Found' };
});
async function page() {
  localReset(); liveReset(); feed = fakeFeed(liveTransport); asked = []; events = []; job = null; refuse = null;
  globalThis.__props = { model: 'tiny' }; globalThis.__files = {}; globalThis.__toasts = []; globalThis.__agents = []; globalThis.__agentList = [];
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');
const posts = () => asked.filter(([, m]) => m === 'POST');
/** The job says something, as the services would on the live channel. */
async function tell(h, next) { Object.assign(job, next); feed.tell(`job:${JOB}`); feed.tell(`weights-session:${SID}`); await h.idle(700); }

test('the goal box: the three starters fill it in, and nothing starts until "Start the agent"', async () => {
  const h = await page();
  await h.click('wt-in-3'); // "Both on": wrong
  assert.equal(h.byIdPrefix('wt-goal').length, 0);
  await h.click('wt-ask');
  assert.equal(h.mod.goalInput.placeholder, 'What should the agent fix?');
  assert.equal(h.byIdPrefix('wt-ask-').length, 3, 'the three starters');
  assert.ok(!h.byIdPrefix('wt-ask-detects')[0].call('on_click'), 'the first starter waits for a neuron');
  assert.equal(textOf(h, 'wt-agent-about'), `Judged on the input on screen (Both on). At most ${MAX_EDITS} edits. A result that does harm is taken back.`);
  // A starter fills the box in. It starts nothing.
  await h.click('wt-ask-fix');
  assert.equal(h.mod.goalInput.value(), 'It answers wrong on the input "Both on". Fix that with the smallest edit, and leave the other answers as they are.');
  await h.click('wt-n-1-1');
  await h.click('wt-ask-detects');
  assert.equal(h.mod.goalInput.value(), 'Find what hidden neuron 2 (layer 1, neuron 2) detects. Tell me in one sentence what it responds to and how sure you are. Change nothing.');
  assert.deepEqual(goalStarters(h.mod).map((s) => s.label), ['Find what this neuron detects', 'Fix this wrong answer with the smallest edit', 'Explain this layer']);
  // Enter in the box starts nothing: the box has no submit at all.
  assert.equal(h.mod.goalInput.handlers.submit, undefined);
  assert.equal(posts().length, 0);
  // An empty goal is refused in words.
  h.mod.goalInput.set_value('  ');
  await h.click('wt-agent-start');
  assert.equal(textOf(h, 'wt-agent-says'), 'Say what the agent should fix first.');
  assert.equal(posts().length, 0);
});

test('"Start the agent" starts one run, its progress shows, and Captain\'s verdict ends it', async () => {
  const h = await page();
  await h.click('wt-in-3');
  await h.click('wt-ask');
  h.mod.goalInput.set_value('Make "Both on" come out right.');
  await h.click('wt-agent-start');
  assert.deepEqual(posts(), [[`/api/weights/sessions/${SID}/program`, 'POST', { goal: 'Make "Both on" come out right.', max_edits: 5, target_input: [1, 1], target_output: [0] }]], 'one request, judged on the input on screen');
  assert.ok(agentWorking(h.mod));
  assert.equal(h.byIdPrefix('wt-agent-start').length, 0);
  assert.equal(h.byIdPrefix('wt-agent-stop').length, 1, '"Stop the agent" takes the button\'s place');
  assert.equal(textOf(h, 'wt-agent-says'), 'The agent read the shape of the network.');
  assert.ok(h.text().includes('Captain started the agent claude with the goal: Make "Both on" come out right.'), 'the start is in the timeline');
  assert.ok(!h.byIdPrefix('wt-ask-fix')[0].call('on_click'), 'no new goal while it works');
  // Progress: each line the job says, and each event the agent adds.
  const reads = asked.filter(([p]) => p === `/api/jobs/${JOB}`).length;
  await h.idle(120);
  assert.equal(asked.filter(([p]) => p === `/api/jobs/${JOB}`).length, reads, 'the job is not read while it says nothing');
  events = [...events, { id: 'e2', seq: 2, by: 'agent', kind: 'note', sentence: 'The agent thinks: the bias of hidden neuron 2 is too low.' }];
  await tell(h, { message: 'The agent thinks: the bias of hidden neuron 2 is too low.' });
  assert.equal(textOf(h, 'wt-agent-says'), 'The agent thinks: the bias of hidden neuron 2 is too low.');
  assert.ok(h.text().includes('The agent thinks: the bias of hidden neuron 2 is too low.'));
  // The verdict.
  events = [...events, { id: 'e3', seq: 3, by: 'captain', kind: 'program', sentence: "Captain's verdict: Kept. The target improved, and the rest held." }];
  await tell(h, { status: 'done', result: { kept: true, kept_in_session: true, saved: null, stopped: false, reason: 'Kept. The target improved, and the rest held.' } });
  assert.equal(textOf(h, 'wt-agent-says'), 'Kept. The target improved, and the rest held. It is in this session, and is not saved as a version.');
  assert.ok(!agentWorking(h.mod));
  assert.equal(h.byIdPrefix('wt-agent-start').length, 1);
  assert.ok(h.text().includes("Captain's verdict: Kept."));
  assert.equal(posts().length, 1);
});

test('"Stop the agent" stops it, and a refusal is said in plain words', async () => {
  const h = await page();
  await h.click('wt-ask');
  refuse = 'No agent is installed on this computer. Install Claude Code or Codex.';
  h.mod.goalInput.set_value('Fix it.');
  await h.click('wt-agent-start');
  assert.equal(textOf(h, 'wt-agent-says'), refuse);
  assert.ok(!agentWorking(h.mod) && h.byIdPrefix('wt-agent-start').length === 1);
  refuse = null;
  await h.click('wt-agent-start');
  await h.click('wt-agent-stop');
  assert.deepEqual(posts().at(-1).slice(0, 2), [`/api/weights/sessions/${SID}/program/stop`, 'POST']);
  assert.equal(textOf(h, 'wt-agent-says'), 'Stopping the agent.');
  assert.ok(!h.byIdPrefix('wt-agent-stop')[0].call('on_click'), 'not stopped twice');
  events = [...events, { id: 'e2', seq: 2, by: 'captain', kind: 'program', sentence: "Captain's verdict: Stopped. Nothing was kept." }];
  await tell(h, { status: 'done', result: { kept: false, stopped: true, reason: 'Stopped. Nothing was kept.' } });
  assert.equal(textOf(h, 'wt-agent-says'), 'Stopped. Nothing was kept.');
  assert.ok(!/HTTP|undefined|null/.test(h.text()));
});

test('an agent started somewhere else shows as working, with a way to stop it', async () => {
  const h = await page();
  events = [{ id: 'e1', seq: 1, by: 'captain', kind: 'program', sentence: 'Captain started the agent codex with the goal: tidy up. It may make at most 5 edits.', program: { agent: 'codex', goal: 'tidy up' } }];
  feed.tell(`weights-session:${SID}`);
  await h.idle(700);
  await h.click('wt-ask');
  assert.ok(agentWorking(h.mod));
  assert.equal(textOf(h, 'wt-agent-says'), 'An agent is working in this session.');
  assert.equal(h.byIdPrefix('wt-agent-stop').length, 1);
  events = [...events, { id: 'e2', seq: 2, by: 'captain', kind: 'program', sentence: "Captain's verdict: Taken back. The target did not improve." }];
  feed.tell(`weights-session:${SID}`);
  await h.idle(700);
  assert.ok(!agentWorking(h.mod) && h.byIdPrefix('wt-agent-start').length === 1);
});

test('the network kept in the page still hands over prompts: there are no services to start an agent', async () => {
  globalThis.__props = { model: TINY };
  localReset(); liveReset(); fakeFeed(liveTransport); asked = [];
  const h = harness(Weights);
  await h.start();
  await h.click('wt-ask');
  assert.equal(h.byIdPrefix('wt-goal').length + h.byIdPrefix('wt-agent-start').length, 0);
  assert.equal(h.byIdPrefix('wt-ask-').length, 3);
});
