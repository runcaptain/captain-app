// Ship as a person meets it (ship-view.js), with a faked backend:
//   no cloud connected    this computer, and one prompt to connect a cloud. No card for a cloud with no login.
//   one cloud connected   a card for it with Deploy, and a smaller "Connect a cloud" card
//   Deploy on a cloud     ONE confirm: what is made the first time, the cost, how long. Nothing starts without a yes.
//   deploying             one progress view, every step named
//   deployed              a plain screen: the state in words, the address, a search box, numbers, cost, Stop, More
//   asleep, stopped       said in words
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/ship-section.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-project');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };

// The page sleeps between two looks at a running deploy. Here a sleep is 20 ms, so the run can be watched.
globalThis.__exec = async () => { await new Promise((r) => setTimeout(r, 20)); return { code: 0, stdout: '', stderr: '' }; };

const LOCAL = { host: 'local', id: 'local', label: 'This computer', login: null, can_deploy: true, why_not: null, first_time: false, sleeps: false, reachable: false,
  estimate: { free: true, text: 'Free.' }, fit: { outcome: 'fits', text: 'Needs about 180 MB of memory.' } };
const AWS = { host: 'aws', id: 'aws:aws-main', label: 'AWS', login: 'aws-main', login_label: 'Amazon Web Services', region: 'us-west-1', can_deploy: true, why_not: null, first_time: true,
  proven: '2026-11-11', proven_text: 'Ran on a real account on 2026-11-11.', sleeps: false, reachable: true,
  estimate: { text: 'About $0.07 an hour, about $48 a month if it runs all the time.', hourly_usd: 0.066 },
  machine: { name: 'ml.t2.medium', memory_mb: 4096, hourly_usd: 0.066, by: 'smallest', text: 'ml.t2.medium: this model needs about 205 MB, and this has 4 GB. The smallest that leaves room to spare.' }, minutes_text: 'About 4 minutes the first time, 2 after.' };
// The plan is the host's own. This one has two groups and five things: the page must not assume a number.
const PLAN = { project: 'shop', host: 'aws', login: 'aws-main', label: 'AWS', model_version: 'shop/starter-ranker@v2', beside: 'shop/keyword-baseline@v1', first_time: true,
  creates: { short: 'Two roles, an image repository, an image build and the endpoint.', count: 5, groups: [
    { title: 'First time only', summary: 'Permissions and a home for the image, kept for later deploys', items: [{ resource: 'IAM role', name: 'captain-serving', why: 'The endpoint runs as this' },
      { resource: 'IAM role', name: 'captain-serving-build', why: 'The image build runs as this' }, { resource: 'ECR repository', name: 'captain-serving', why: "Holds Captain's serving image" },
      { resource: 'CodeBuild project', name: 'captain-serving-build', why: 'Builds the image in your account' }] },
    { title: 'For this model', summary: 'One small machine (ml.t2.medium)', items: [{ resource: 'SageMaker endpoint', name: 'captain-shop-live', why: 'Answers requests' }] }] },
  machine: { name: 'ml.t2.medium', hourly_usd: 0.066, by: 'smallest', text: 'ml.t2.medium: this model needs about 205 MB, and this has 4 GB. The smallest that leaves room to spare.' },
  exists: [], cost: { hourly_usd: 0.066, monthly_usd: 48.2, setup_usd: 0.03, sleeps: false, text: 'About $0.07 an hour, about $48 a month if it runs all the time. $0.03 once to build the image.' },
  minutes: { low: 3, high: 5, text: 'About 4 minutes: 2 to set up, 2 to start.' },
  steps: [{ id: 'check', title: 'Checking your account' }, { id: 'access', title: 'Creating permissions' }, { id: 'image', title: "Building the model's image" }, { id: 'start', title: 'Starting the model' }, { id: 'ready', title: 'Checking it answers' }],
  warnings: [], login_policy: null };
const STEPS = [{ id: 'check', title: 'Checking your account', status: 'done', seconds: 2 }, { id: 'access', title: 'Creating permissions', status: 'skipped' }, { id: 'image', title: "Building the model's image", status: 'done', seconds: 78 },
  { id: 'start', title: 'Starting the model', status: 'running', seconds: 40 }, { id: 'ready', title: 'Checking it answers', status: 'pending' }];
const NONE = { status: 'none', staged: null, text: 'Nothing is deployed.' };
const staged = (kind, extra = {}) => ({ id: 'stg-1', status: 'running', kind, host: kind === 'local' ? 'local' : 'aws', login: kind === 'local' ? null : 'aws-main', target: kind === 'local' ? 'local' : 'aws-serving', serve: 'candidate', model_version: 'shop/starter-ranker@v2', needs_key: true,
  arms: { candidate: 'shop/starter-ranker@v2', baseline: 'shop/keyword-baseline@v1' }, endpoint: kind === 'local' ? { url: 'http://127.0.0.1:5001' } : { name: 'captain-shop-live', region: 'us-west-1' }, idle_minutes: 240, ...extra });
const RUN = { status: 'running', text: '', health: { stops_in_s: 14000, uptime_s: 400, running: 0, idle_s: 5 }, staged: staged('aws-sagemaker', { url: 'https://runtime.sagemaker.us-west-1.amazonaws.com/endpoints/captain-shop-live/invocations' }) };
const STOPPED = { status: 'stopped', text: 'Stopped by maya.', staged: { ...staged('aws-sagemaker'), status: 'stopped', spent: { text: 'Cost under $0.01 (5 min on aws-serving)' } } };
let world, sent, jobReads;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url), p = u.pathname;
  const body = opts.body ? JSON.parse(opts.body) : null;
  sent.push([opts.method ?? 'GET', p + u.search, body]);
  if (p.endsWith('/deploy/hosts')) return reply(200, { project: 'shop', model_version: 'shop/starter-ranker@v2', beside: 'shop/keyword-baseline@v1', hosts: world.places });
  if (p.endsWith('/deploy') && (opts.method ?? 'GET') === 'GET') { jobReads += 1; return reply(200, world.job(jobReads)); }
  if (p.endsWith('/deploy')) {
    if (body.host === 'local' || body.confirm) return reply(200, { job: 'dep-1', run: 'run-1', plan: body.host === 'local' ? { steps: [] } : PLAN });
    return reply(409, { needs_confirm: true, detail: 'Confirm first.', plan: PLAN });
  }
  if (p.endsWith('/staging/versions')) return reply(200, { text: '2 versions staged so far.', live: 'shop/starter-ranker@v2', rows: [
    { n: 2, model_version: 'shop/starter-ranker@v2', at: '2026-11-12T19:00:00+00:00', by: 'dev', live: true, can_roll_back: false },
    { n: 1, model_version: 'shop/keyword-baseline@v1', at: '2026-11-12T18:00:00+00:00', by: 'dev', live: false, can_roll_back: true }] });
  if (p.endsWith('/rollback')) return reply(200, { job: 'rb-1' });
  if (p.endsWith('/staging/rollbacks/auto')) { world.backs = { ...world.backs, auto: body.minutes ? { minutes: body.minutes, on: true, text: `On: if a new version is unhealthy in its first ${body.minutes} minutes, Captain puts the previous one back and tells you.` } : { minutes: null, on: false, text: 'Off: an unhealthy version stays up until you roll it back.' } }; return reply(200, world.backs.auto); }
  if (p.endsWith('/staging/rollbacks')) return reply(200, world.backs);
  if (p.endsWith('/staging/live')) return reply(200, world.live);
  if (p.endsWith('/staging/health/check')) return reply(200, { ...world.health, checked_at: '2026-11-12T19:31:00+00:00', changed: false, alert: null });
  if (p.endsWith('/staging/health/target')) { world.health = { ...world.health, target: { p95_ms: body.p95_ms ?? null, error_rate: body.error_rate ?? 0.05, set: true } }; return reply(200, world.health); }
  if (p.endsWith('/staging/health')) return reply(200, world.health);
  if (p.endsWith('/staging/metrics')) return reply(200, { range_text: 'the last hour', buckets: Array.from({ length: 30 }, (_x, i) => ({ requests: i === 29 ? 62 : 0, errors: 0 })), markers: [],
    summary: { requests: 62, errors: 0, p50: 0.2, p95: 0.6, site_reported: 0 }, text: '62 requests in the last hour, no errors.' });
  if (p.endsWith('/staging/cost')) return reply(200, { free: false, month_text: '$0.79', rate_text: 'About $0.07 an hour while it is up, about $1.58 a day.' });
  if (p.endsWith('/staging/requests')) return reply(200, { rows: [{ request_id: 'r1', ts: '2026-11-11T16:06:02.118Z', query: 'jacket', arm: 'candidate', ms: 2.7, traffic: 'real' }], total: 1, text: '1 request since this version was deployed.' });
  if (p.endsWith('/staging/connect')) return reply(200, { connected: world.real > 0, real: world.real, unlabelled: 0, test: 0, simulated: 0, running: 0, snippets: {}, test_request: 'curl https://example.invalid/rank', live: 'Waiting for the first request.', text: '' });
  if (p.endsWith('/staging/differ')) return reply(200, { checked: 40, same: 10, mostly_same: false, text: 'It ranks differently from the other model on 30 of 40 searches checked.',
    queries: [{ query: 'rain jacket under 50', moved: 9 }, { query: 'trail shoes', moved: 4 }] });
  if (p.endsWith('/staging/check')) { world.staging = { ...world.staging, staged: { ...world.staging.staged, check: world.check } }; return reply(200, world.check); }
  if (p.endsWith('/shadow') || p.endsWith('/ab/options')) return reply(200, { status: 'none', ready: false, shares: [], text: '' });
  if (p.endsWith('/staging/query')) return reply(200, { query: body.query, same_order: false, moved: 2, staged: { model_version: 'shop/starter-ranker@v2', ms: 2.7, results: [{ id: 'p1', position: 1, title: 'Rain Jacket' }] },
    baseline: { model_version: 'shop/keyword-baseline@v1', ms: 0.3, results: [{ id: 'p2', position: 1, title: 'Wind Jacket' }] } });
  if (p.endsWith('/staging')) return reply(200, world.staging);
  if (p.endsWith('/ab')) return reply(200, { status: 'none', experiment: null, can_measure: ['clicks'] });
  return reply(200, {});
};

const { Ship } = await import(path.join(dir, 'ship.js'));
const { palette } = await import(path.join(dir, 'kit/tokens.js'));
const { View } = await import('gpui-kit');
let opened = 0;
const page = { pid: () => 'shop', open: null, deployOpen: false, project: () => ({ name: 'Shop' }), reload() {}, openDeployment() { opened += 1; },
  stages: { ship: { steps: [] } }, exps: { champion: 'shop/keyword-baseline@v1', experiments: [{ id: 'exp-1', pipeline: 'shop/starter-ranker@v2', is_champion: false, evaluation: {} }] } };
class Dep extends View {
  init(_p, cx) { this.ship = new Ship(page); this.ship.sync(cx, world.staging.staged ? { staging: world.staging.staged } : {}); }
  render() { return page.deployOpen ? this.ship.deployed(palette(false)) : this.ship.section(palette(false)); }
}
const open = async (w, deployOpen = false) => {
  world = { places: [LOCAL], staging: NONE, live: { state: 'handling', label: 'Handling requests' }, real: 0,
    backs: { rows: [{ at: '2026-11-12T18:30:00+00:00', by: 'Captain', from: 'shop/starter-ranker@v1', to: 'shop/keyword-baseline@v1', version: 1, auto: true, reason: 'It turned unhealthy 4 minutes after it was deployed. It is not answering.' }],
      auto: { minutes: null, on: false, text: 'Off: an unhealthy version stays up until you roll it back.' }, text: '1 roll back so far.' },
    health: { project: 'shop', status: 'healthy', text: 'Healthy. 0 of its last 62 requests failed, in the last 15 minutes. 95 in 100 took under 0.6 ms at the endpoint.', checked_at: '2026-11-12T19:30:00+00:00', reasons: [], target: { p95_ms: null, error_rate: 0.05, set: false } }, job: () => ({ status: 'running', host: 'aws', login: 'aws-main', steps: STEPS }), ...w };
  sent = []; jobReads = 0; page.deployOpen = deployOpen;
  const h = harness(Dep);
  await h.start();
  for (let i = 0; i < 40 && !h.mod.ship.dp; i++) await h.idle(30);
  await h.idle(60);
  return h;
};
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const wait = async (h, ok, what) => { for (let i = 0; i < 60 && !ok(); i++) await h.idle(30); assert.ok(ok(), what); };

// No cloud connected: this computer, and one prompt. No card for any cloud.
let h = await open({});
let t = h.text();
assert.ok(t.includes('Where should starter-ranker@v2 run?') && t.includes('This computer') && t.includes('Only you can reach it.') && t.includes('Free.'), `this computer: ${t}`);
assert.ok(t.includes('Connect a cloud to deploy where a site can reach it'), 'the prompt, said once and plainly');
assert.deepEqual(h.byIdPrefix('ship-deploy-').map((e) => e.call('id')[0]), ['ship-deploy-local'], 'one card: no card for a cloud that is not connected');
assert.ok(!/Modal|Azure|Google/.test(t.replace('Connect AWS, Google Cloud, Azure or Modal', '')), 'no cloud is offered as a place before it is connected');
assert.equal(h.byIdPrefix('ship-deploy-').length, 1, 'one button, Deploy');
globalThis.__overlays = [];
await h.click('ship-connect-cloud-go');
assert.deepEqual([globalThis.__overlays.at(-1).mod, globalThis.__overlays.at(-1).props.task], ['captain-forms', 'cloud'], 'the prompt opens the form that connects a cloud');
assert.ok(!/[Ss]taging|Stage it|Set up serving|launch review|registry/.test(t), `none of the words that needed explaining: ${t}`);

// One cloud connected: its card with Deploy, and a smaller way to connect another.
h = await open({ places: [LOCAL, AWS] });
t = h.text();
assert.ok(t.includes('AWS') && t.includes('aws-main · us-west-1') && t.includes('A site can reach it.') && t.includes('About $0.07 an hour, about $48 a month if it runs all the time.'), `the cloud card: ${t}`);
assert.deepEqual(h.byIdPrefix('ship-deploy-').map((e) => e.call('id')[0]), ['ship-deploy-local', 'ship-deploy-aws:aws-main'], 'Deploy on each, and nothing else to understand');
assert.ok(t.includes('Connect a cloud') && !t.includes('Connect a cloud to deploy where a site can reach it'), 'connecting another is a small card now');
assert.ok(!t.includes("isn't set up"), 'a connected cloud never says it is not set up');
// The card names the machine its price is for, in the services' own sentence.
assert.ok(t.includes('ml.t2.medium: this model needs about 205 MB, and this has 4 GB. The smallest that leaves room to spare.'), `the machine, beside its price: ${t}`);

// A login named after its cloud isn't said twice: "AWS  aws" is just "AWS", with the region. Two logins of one
// cloud are told apart by name.
{
  const one = await open({ places: [LOCAL, { ...AWS, id: 'aws:aws', login: 'aws' }] });
  assert.ok(one.text().includes('us-west-1') && !/\baws \u00b7/.test(one.text()), `the login name is not repeated: ${one.text()}`);
  const two = await open({ places: [LOCAL, { ...AWS, id: 'aws:aws', login: 'aws' }, { ...AWS, id: 'aws:aws-eu', login: 'aws-eu', region: 'eu-west-1' }] });
  assert.ok(two.text().includes('aws \u00b7 us-west-1') && two.text().includes('aws-eu \u00b7 eu-west-1'), `two logins of one cloud are named: ${two.text()}`);
}

// Deploy on the cloud: one confirm, and nothing starts without a yes.
await h.click('ship-deploy-aws:aws-main');
await wait(h, () => byId(h, 'ship-confirm').length === 1, 'the confirm opens');
t = h.text();
assert.ok(t.includes('Deploy starter-ranker@v2 to AWS?') && t.includes('The first time, Captain makes this in your account. Two roles, an image repository, an image build and the endpoint.'), `the confirm, in the plan's own words: ${t}`);
assert.ok(t.includes('First time only') && t.includes('Permissions and a home for the image, kept for later deploys') && t.includes('For this model') && t.includes('One small machine (ml.t2.medium)'), 'grouped and short');
assert.ok(!t.includes('captain-serving-build') && t.includes('Show everything (5 things)'), 'the full list is behind Show everything, counted from the plan');
assert.ok(t.includes('About $0.07 an hour, about $48 a month if it runs all the time. $0.03 once to build the image. It bills until you stop it.') && t.includes('About 4 minutes: 2 to set up, 2 to start.'), 'the cost and how long');
assert.equal(sent.filter((x) => x[0] === 'POST' && x[1].endsWith('/deploy') && x[2].confirm).length, 0, 'nothing was started');
assert.ok(byId(h, 'ship-confirm-machine')[0].kids.join('') === 'ml.t2.medium: this model needs about 205 MB, and this has 4 GB. The smallest that leaves room to spare.', 'the confirm names the same machine, in the same words, without opening anything');
await h.click('ship-confirm-all');
assert.ok(h.text().includes('captain-serving-build') && h.text().includes('Show less'), 'Show everything lists each thing by name');

assert.equal(h.byIdPrefix('ship-place-').length, 0, 'the confirm stands alone: one question at a time');

// Yes: one progress view, each step named.
await h.click('ship-confirm-yes');
await wait(h, () => byId(h, 'ship-progress').length === 1 && h.mod.ship.djob?.steps.some((x) => x.status === 'running'), 'the progress view');
t = h.text();
assert.deepEqual(sent.filter((x) => x[0] === 'POST' && x[1].endsWith('/deploy')).at(-1)[2], { host: 'aws', login: 'aws-main', candidate: 'exp-1', confirm: true });
assert.ok(t.includes('Deploying to AWS') && t.includes('Checking your account') && t.includes('Creating permissions') && t.includes('already there') && t.includes("Building the model's image") && t.includes('1 min 18 s') && t.includes('Starting the model') && t.includes('Checking it answers'), `every step, named: ${t}`);
assert.equal(byId(h, 'ship-confirm').length, 0);
// It finishes: the note, and the place is remembered.
world.staging = RUN; world.job = () => ({ status: 'done', steps: STEPS.map((s) => ({ ...s, status: 'done' })) });
await wait(h, () => !h.mod.ship.djob && h.mod.ship.running(), 'the run ends');
t = h.text();
assert.ok(t.includes('Deployed to AWS.') && store['deploy-where:shop'] === 'aws:aws-main', `done: ${t}`);
assert.ok(t.includes('starter-ranker@v2') && t.includes('Running on AWS. Answering requests.') && t.includes('Open it'), 'the project page now says what is deployed');

// A step fails for a missing permission: the sentence, the exact policy, and a way to try again.
h = await open({ places: [LOCAL, AWS], job: () => ({ status: 'failed', error: "Your AWS login can't create the role captain-serving: it lacks iam:CreateRole.", failed_step: { id: 'access', title: 'Creating permissions' },
  steps: [{ ...STEPS[0] }, { ...STEPS[1], status: 'failed' }, ...STEPS.slice(2).map((s) => ({ ...s, status: 'pending', seconds: undefined }))], can: ['retry', 'remove'],
  policy: { Version: '2012-10-17', Statement: [{ Effect: 'Allow', Action: ['iam:CreateRole'], Resource: ['arn:aws:iam::000000000000:role/captain-serving'] }] } }) });
await h.click('ship-deploy-aws:aws-main');
await wait(h, () => byId(h, 'ship-confirm').length === 1, 'confirm');
await h.click('ship-confirm-yes');
await wait(h, () => Boolean(h.mod.ship.djob?.failed), 'the failure is shown');
t = h.text();
assert.ok(t.includes('Deploying to AWS stopped') && t.includes("it lacks iam:CreateRole") && t.includes('iam:CreateRole') && t.includes('Copy the policy') && t.includes('Deploy again'), `the failure: ${t}`);

// The deploy finished, but a step after it did not (saving to the registry): said in words, and it is still deployed.
h = await open({ places: [LOCAL, AWS], job: () => ({ status: 'done', steps: [...STEPS.map((x) => ({ ...x, status: 'done' })), { id: 'match', title: 'Checking it matches training', status: 'done', lines: ['Served features match training: 100% of 40 checked'] },
  { id: 'publish', title: 'Saving this version to your model registry', status: 'failed' }], soft_failed: [{ id: 'publish', title: 'Saving this version to your model registry', error: "Captain can't write to s3://registry-bucket with this login." }] }) });
await h.click('ship-deploy-aws:aws-main');
await wait(h, () => byId(h, 'ship-confirm').length === 1, 'confirm');
world.staging = RUN;
await h.click('ship-confirm-yes');
await wait(h, () => !h.mod.ship.djob && Boolean(h.mod.ship.dnote), 'the run ends');
t = h.text();
assert.ok(t.includes("Deployed to AWS, but one step didn't finish. Saving this version to your model registry: Captain can't write to s3://registry-bucket with this login."), `a step that didn't finish is said: ${t}`);
assert.ok(t.includes('Running on AWS.'), 'and the model is up');

// This computer needs no confirm.
h = await open({ places: [LOCAL, AWS], job: () => ({ status: 'done', steps: [] }) });
await h.click('ship-deploy-local');
await wait(h, () => sent.some((x) => x[0] === 'POST' && x[1].endsWith('/deploy') && x[2].host === 'local'), 'it starts at once');
assert.equal(byId(h, 'ship-confirm').length, 0);
// A model too big for this computer: the reason, and no way to press Deploy.
h = await open({ places: [{ ...LOCAL, can_deploy: false, why_not: 'This model needs about 2.6 GB to serve, which is more than the 1 GB limit for this computer. Deploy it to a cloud.', fit: { outcome: 'over-limit' } }, AWS] });
assert.ok(h.text().includes('more than the 1 GB limit for this computer') && h.text().includes('Change the limit in Settings'));
assert.equal(byId(h, 'ship-deploy-local')[0].call('on_click'), undefined, 'Deploy is switched off there');

// Deployed: one plain screen.
h = await open({ places: [LOCAL, AWS], staging: RUN, real: 0 }, true);
await wait(h, () => Boolean(h.mod.ship.met) && Boolean(h.mod.ship.costD) && Boolean(h.mod.ship.conn), 'the plain screen has its numbers');
t = h.text();
assert.ok(t.includes('Running on AWS. Answering requests.'), `the state in words: ${t}`);
assert.ok(t.includes('https://runtime.sagemaker.us-west-1.amazonaws.com/endpoints/captain-shop-live/invocations') && t.includes('Copy a test request') && t.includes('Your site can call it.'), 'the address');
assert.ok(t.includes('Try a search') && t.includes('It has answered 62 requests in the last hour. Usually in under 1 ms at the endpoint, almost always within 1 ms.'), 'a search box, and how much and how fast in a sentence');
assert.ok(t.includes('About $0.07 an hour while it is up, about $1.58 a day. $0.79 so far this month.') && byId(h, 'ship-stop').length === 1, 'the cost and Stop');
const labels = h.byIdPrefix('dep-more-').map((e) => e.kids[0].kids[0]);
assert.deepEqual(labels, ['Requests it has answered', 'Where the two models disagree', 'Is it healthy?', 'Check it uses the same numbers it was trained on', 'Send it real traffic', 'Earlier versions', 'Logs', 'Settings'], 'More, with labels that say what each is for');
assert.ok(!labels.includes('Review before launch'), 'the review is not offered before there is real traffic or a test result');
assert.ok(t.includes('Each request it answered: what was asked, what it returned, and how long it took.'), 'each with a sentence');
assert.equal(h.byIdPrefix('dep-tab-').length, 0, 'no tabs');
assert.ok(!/\bMetrics\b|\bVersions\b|\bCopies\b|[Ss]taging|Stage it|Try it\b/.test(t), `no bare nouns and no old words: ${t}`);
// "What should I type?": a link under the box to the searches where the two models disagree. Picking one runs it.
await wait(h, () => Boolean(h.mod.ship.diff), 'the comparison is read');
assert.ok(h.text().includes('Not sure what to type? See where this model and keyword-baseline disagree'), 'the link sits under the search box');
await h.click('dep-try-ideas');
t = h.text();
assert.ok(byId(h, 'dep-open-differ').length === 1 && t.includes('Searches where this model and keyword-baseline return the most different results.'), `it opens with its sentence: ${t}`);
assert.ok(t.includes('rain jacket under 50') && t.includes('9 of the top 10 in another place') && t.includes('It ranks differently from the other model on 30 of 40 searches checked.'), 'the list, worst first');
await h.click('dep-differ-0');
await wait(h, () => h.mod.ship.result?.query === 'rain jacket under 50', 'picking one runs that search');
assert.equal(byId(h, 'dep-open-differ').length, 0, 'and the list gets out of the way of the results');
h.mod.ship.result = null;

// The check that it uses the numbers it was trained on: a launch gate, with its last result and Check now.
await h.click('dep-more-check');
t = h.text();
assert.ok(t.includes('This compares the two. It must pass before launch.') && t.includes('Not checked yet.') && byId(h, 'dep-check-now').length === 1, `the check, before it has run: ${t}`);
assert.equal(byId(h, 'dep-check-failed').length, 0);
world.check = { status: 'fail', text: 'Served features differ from training on 4% of 250 values checked.', checked: 250, match_rate: 0.96, features: [{ feature: 'title_bm25', mismatch_rate: 0.09 }, { feature: 'price_rank', mismatch_rate: 0.03 }] };
await h.click('dep-check-now');
await wait(h, () => h.mod.ship.checkResult?.status === 'fail', 'the check answers');
t = h.text();
assert.ok(sent.some((x) => x[0] === 'POST' && x[1].endsWith('/staging/check')), 'Check now asks the services');
assert.ok(t.includes('Served features differ from training on 4% of 250 values checked.') && t.includes('Most different: title_bm25, price_rank.'), `the result in words: ${t}`);
// A failed check is said at the top of the screen, not only behind More.
await h.click('dep-open-close');
t = h.text();
assert.ok(byId(h, 'dep-check-failed').length === 1 && t.includes('It is not using the same numbers it was trained on, so it may rank worse than its tests said. Served features differ from training on 4% of 250 values checked.'), `at the top: ${t}`);
await h.click('dep-check-failed-open');
assert.equal(byId(h, 'dep-open-check').length, 1, 'and it leads to the check');
await h.click('dep-open-close');
world.check = { status: 'pass', text: 'Served features match training: 100% of 250 checked', checked: 250, match_rate: 1 };
await h.click('dep-more-check');
await h.click('dep-check-now');
await wait(h, () => h.mod.ship.checkResult?.status === 'pass', 'it passes');
assert.ok(byId(h, 'dep-check-failed').length === 0 && h.text().includes('Served features match training: 100% of 250 checked'), 'a pass clears the warning');
await h.click('dep-open-close');

h.mod.ship.box.set_value('jacket');
await h.click('ship-try-go');
await wait(h, () => Boolean(h.mod.ship.result), 'the search answers');
t = h.text();
assert.ok(t.includes('This model') && t.includes('Compared with keyword-baseline') && t.includes('2 of the top 1 differ from keyword-baseline.') && t.includes('Searches typed here are tests.'), `the other model by its own name: ${t}`);
assert.ok(!/today's search/i.test(h.text()), 'the compared model is never called the search that runs today');
await h.click('dep-more-requests');
await wait(h, () => Boolean(h.mod.ship.reqs), 'More opens what was asked for');
assert.ok(byId(h, 'dep-open-requests').length === 1 && byId(h, 'dep-requests').length === 1 && h.text().includes('jacket'), 'the request list, under its sentence');
await h.click('dep-open-close');
assert.equal(byId(h, 'dep-open-requests').length, 0);
await h.click('ship-stop');
assert.ok(h.text().includes('Stop it? The address goes away. What it answered is kept.'), 'Stop asks first');
// With real traffic, the review appears, last.
h = await open({ places: [LOCAL, AWS], staging: RUN, real: 12 }, true);
await wait(h, () => Boolean(h.mod.ship.conn), 'traffic is read');
assert.equal(h.byIdPrefix('dep-more-').map((e) => e.kids[0].kids[0]).at(-1), 'Review before launch', 'the review appears once there is real traffic');

// The screen rules on the deployed screen and on everything behind More: no filled button (the next step's is
// the banner above this screen), and the code font only on what a person copies: the address, a request's id,
// the code a site pastes, and log lines.
{
  const { palette, MONO } = await import(path.join(dir, 'kit/tokens.js'));
  const solid = palette(false).solid;
  const COPIED = /^(ship-address|ship-connect-address|ship-connect-snippet|dep-request-id|dep-log-\d+|dep-request-line-\d+)$/;
  const idOf = (e) => e.call('id')?.[0] ?? '';
  const counts = (hh) => {
    const all = hh.byIdPrefix('');
    const filled = all.filter((e) => e.call('bg')?.[0] === solid && e.call('on_click')).map(idOf);
    // a code-font element counts by the nearest id at or above it
    const mono = [];
    const walk = (e, near) => { if (!e || typeof e !== 'object' || typeof e.call !== 'function') return; const id = idOf(e) || near; if (e.call('font_family')?.[0] === MONO) mono.push(id); for (const k of e.kids ?? []) walk(k, id); };
    walk(hh.mod.render(), '');
    return { filled, stray: mono.filter((id) => !COPIED.test(id)) };
  };
  const hh = await open({ places: [LOCAL, AWS], staging: RUN, real: 12 }, true);
  await wait(hh, () => Boolean(hh.mod.ship.conn) && Boolean(hh.mod.ship.met), 'the screen is read');
  const seen = {};
  seen.screen = counts(hh);
  for (const id of ['requests', 'differ', 'health', 'check', 'traffic', 'versions', 'logs', 'settings', 'review']) {
    await hh.click(`dep-more-${id}`);
    await hh.idle(120);
    seen[id] = counts(hh);
    await hh.click('dep-open-close');
  }
  for (const [where, k] of Object.entries(seen)) {
    assert.deepEqual(k.filled, [], `no filled button on ${where}`);
    assert.deepEqual(k.stray, [], `the code font only on what a person copies, on ${where}`);
  }
  console.log(`  counted on ${Object.keys(seen).length} views: 0 filled buttons, code font only on copied text`);
  // Inside "Send it real traffic" the words are the page's: this model, and the other model by its own name.
  await hh.click('dep-more-traffic');
  await hh.idle(120);
  const tt = hh.text();
  assert.ok(!/\b(the |The |a )?(candidate|baseline)\b(?!-)/.test(tt.replace(/keyword-baseline/g, '')), `no "candidate" or "baseline" inside Send it real traffic: ${tt.match(/.{60}\b(candidate|baseline)\b.{40}/)?.[0]}`);
  assert.ok(tt.includes("Your site keeps showing keyword-baseline's results. This model ranks the same requests silently"), `shadow mode in the page's words: ${tt.slice(-900)}`);
}

// Health: the last recorded check in words, the targets a person sets, and Check now. Unhealthy is said at the top
// of the screen and on the project page, in words, with what is wrong.
h = await open({ places: [LOCAL, AWS], staging: RUN }, true);
await wait(h, () => Boolean(h.mod.ship.healthD), 'health is read');
assert.equal(byId(h, 'dep-unhealthy').length, 0, 'nothing is said at the top while it is healthy');
await h.click('dep-more-health');
t = h.text();
assert.ok(t.includes('Captain checks once a minute that it answers') && t.includes('Healthy. 0 of its last 62 requests failed') && t.includes('Last checked 19:30 UTC.'), `its health, under its sentence: ${t.slice(-900)}`);
assert.ok(t.includes('No target is set yet.') && t.includes('Unhealthy when 95 in 100 requests take longer than') && t.includes('Unhealthy when more requests than this fail'), 'the targets, in words');
await h.click('dep-health-p95-200');
await wait(h, () => h.mod.ship.healthD?.target?.p95_ms === 200, 'the target is saved');
assert.deepEqual(sent.find((x) => x[0] === 'PUT' && x[1].endsWith('/staging/health/target'))[2], { p95_ms: 200, error_rate: 0.05 });
assert.ok(h.text().includes('Saved. The next check uses it.') && !h.text().includes('No target is set yet.'));
await h.click('dep-health-check');
await wait(h, () => sent.some((x) => x[0] === 'POST' && x[1].endsWith('/staging/health/check')), 'Check now asks the services');
const BAD = { project: 'shop', status: 'unhealthy', text: 'Unhealthy. 9 of its last 200 requests failed (4.5%), over the 1% target.', since: '2026-11-12T19:20:00+00:00', checked_at: '2026-11-12T19:30:00+00:00',
  reasons: ['9 of its last 200 requests failed (4.5%), over the 1% target.'], target: { p95_ms: 200, error_rate: 0.01, set: true } };
h = await open({ places: [LOCAL, AWS], staging: RUN, health: BAD }, true);
await wait(h, () => h.mod.ship.healthD?.status === 'unhealthy', 'health is read');
t = h.text();
assert.ok(byId(h, 'dep-unhealthy').length === 1 && t.includes('Unhealthy. 9 of its last 200 requests failed (4.5%), over the 1% target.'), `said at the top: ${t.slice(0, 700)}`);
await h.click('dep-unhealthy-open');
assert.equal(byId(h, 'dep-open-health').length, 1, 'and it leads to its health');
h = await open({ places: [LOCAL, AWS], staging: RUN, health: BAD }, false);
await wait(h, () => h.mod.ship.healthD?.status === 'unhealthy', 'the project page reads it too');
assert.ok(byId(h, 'ship-deployed-unhealthy').length === 1 && h.text().includes('over the 1% target.'), `the project page says so: ${h.text()}`);

// Roll back: it asks why before it can be confirmed, sends the reason, and shows who rolled back last and why.
h = await open({ places: [LOCAL, AWS], staging: RUN }, true);
await h.click('dep-more-versions');
await wait(h, () => Boolean(h.mod.ship.vers) && Boolean(h.mod.ship.backs), 'versions and roll backs are read');
t = h.text();
assert.ok(t.includes('Captain put keyword-baseline@v1 back in place of starter-ranker@v1. It turned unhealthy 4 minutes after it was deployed.'), `who rolled back and why: ${t.slice(-900)}`);
assert.ok(t.includes('Roll back by itself when a new version is unhealthy') && t.includes('Off: an unhealthy version stays up until you roll it back.'), 'automatic roll back, off, in words');
await h.click('dep-version-1-back');
assert.ok(h.text().includes('What runs now keeps answering until it does. Say why, so the next person knows.'));
assert.ok(!byId(h, 'dep-version-yes')[0].call('on_click'), 'no reason, no roll back: the button cannot be pressed yet');
await h.click('dep-version-why-1');
await h.click('dep-version-yes');
await wait(h, () => sent.some((x) => x[1].endsWith('/staging/versions/1/rollback')), 'the roll back is asked for');
assert.deepEqual(sent.find((x) => x[1].endsWith('/staging/versions/1/rollback'))[2], { reason: 'It is too slow' }, 'with its reason');
await h.click('dep-auto-30');
await wait(h, () => h.mod.ship.backs?.auto?.on === true, 'automatic roll back is turned on');
assert.ok(h.text().includes('On: if a new version is unhealthy in its first 30 minutes, Captain puts the previous one back and tells you.'));
assert.deepEqual(sent.find((x) => x[0] === 'PUT' && x[1].endsWith('/staging/rollbacks/auto'))[2], { minutes: 30 });

// Asleep, and stopped: said in words.
h = await open({ places: [LOCAL, AWS], staging: { ...RUN, asleep: true }, live: { state: 'asleep', label: 'Asleep' } }, true);
assert.ok(h.text().includes('Asleep on AWS. It wakes on the next request, and costs nothing asleep.'), `asleep: ${h.text()}`);
h = await open({ places: [LOCAL, AWS], staging: STOPPED }, true);
t = h.text();
assert.ok(t.includes('Stopped. Nothing is running and nothing is billing.') && byId(h, 'ship-stop').length === 0 && byId(h, 'ship-address').length === 0, `stopped: ${t}`);
assert.ok(t.includes('Where should starter-ranker@v2 run?') && byId(h, 'ship-deploy-aws:aws-main').length === 1, 'and it can be deployed again from the same screen');

for (const w of [{}, { places: [LOCAL, AWS] }]) assert.ok(!/[←-⇿—–]/.test((await open(w)).text()), 'plain punctuation only');
console.log('ship section render test: ok');
