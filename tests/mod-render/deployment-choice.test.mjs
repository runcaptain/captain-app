// Where a model can be staged, on the deployment page's header, with a faked backend (no services, no cloud):
//   a cloud login with serving set up   two choices side by side, each with its cost, and the cloud's confirm
//   a cloud login without it            "Set up serving in your AWS", which opens what is needed, not nothing
//   no cloud login                      this computer, and one line on how to connect a cloud
//   staged on this computer             where it runs now, and "Stage in your AWS account" with its hourly estimate
// The last choice is remembered per project and offered first.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/deployment-choice.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-project');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };

const EST = { kind: 'staging', where: 'sm', free: false, available: true, low: 0.066, high: 0.066, unit: 'hour', basis: ['1 x ml.t2.medium'],
  text: 'About $0.07 an hour on sm, about $1.58 a day while it is up. Estimate, prices as of 2026-11-10.' };
const POLICY = { Version: '2012-10-17', Statement: [{ Sid: 'ServingRoles', Effect: 'Allow', Action: ['iam:CreateRole'], Resource: ['arn:aws:iam::000000000000:role/captain-serving'] }] };
let world, sent;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url, opts = {}) => {
  const p = new URL(url).pathname;
  const body = opts.body ? JSON.parse(opts.body) : null;
  sent.push([opts.method ?? 'GET', p, body]);
  if (p === '/api/compute') return reply(200, world.targets);
  if (p === '/api/cloud/summary') return reply(200, { active: world.cloud });
  if (p === '/api/costs/estimate') return reply(200, body.where === 'local' ? { free: true, available: true, text: 'Runs on this computer. No cloud cost.', basis: [] } : EST);
  if (p === '/api/serving/setup' && opts.method === 'DELETE') return reply(200, { job: 'remove-1' });
  if (p === '/api/jobs/remove-1') return reply(200, { status: 'done', result: { status: 'removed', text: "Serving setup is removed. Nothing of Captain's is left in the account." } });
  if (p === '/api/serving/setup' && (opts.method ?? 'GET') === 'GET' && world.serving && !new URL(url).searchParams.get('profile')) return reply(200, world.serving);
  if (p === '/api/serving/setup' && (opts.method ?? 'GET') === 'GET') return reply(200, { status: 'none', created: [], plan: { profile: 'aws-test', region: 'us-west-1',
    creates: [{ resource: 'IAM role', name: 'captain-serving', why: 'The endpoint runs as this. It can read the serving bundle, write the request log, pull the serving image and delete its own endpoint. Nothing else.' },
      { resource: 'ECR repository', name: 'captain-serving', why: "Holds Captain's serving image in your account." }],
    cost: { text: 'Setting up costs about $0.03 once: the image build runs about 6 minutes in AWS CodeBuild.' }, login_policy: POLICY } });
  if (p === '/api/serving/setup') return reply(200, { job: 'setup-1' });
  if (p === '/api/jobs/setup-1') return reply(200, { status: 'failed', message: "Your AWS login can't create the role captain-serving: it lacks iam:CreateRole. Ask an admin to grant the policy below, then try again.", policy: POLICY });
  if (p.endsWith('/staging')) return reply(200, world.staging);
  if (p.endsWith('/staging/connect')) return reply(200, { reachable: false, connected: false, address: 'http://127.0.0.1:5001', text: 'Running on this computer. Only this computer can reach it.', live: 'Waiting for the first request.', real: 0, test: 0, simulated: 0, running: 0, evidence_label: null, snippets: {}, key_note: '', fallback: '', test_request: 'curl -s http://127.0.0.1:5001/rank' });
  if (p.endsWith('/ab')) return reply(200, { status: 'none', experiment: null, can_measure: ['clicks'] });
  if (p.endsWith('/stage') && body.dry_run) return reply(200, { dry_run: true, local_fit: world.fit ?? { outcome: 'fits', text: 'Needs about 180 MB of memory, and this computer has 30 GB free. It loads in about 1 s.' } });
  if (p.endsWith('/stage')) return body.confirm ? reply(200, { job: 'j1', plan: { kind: 'aws-sagemaker' } })
    : reply(409, { needs_confirm: true, detail: 'Confirm the cost first.', plan: { target: 'sm', kind: 'aws-sagemaker', model_version: 'shop/starter-ranker@v2',
      cost: { instance_type: 'ml.t2.medium', instance_count: 1, estimate: EST, text: EST.text }, warnings: ["This endpoint can't stop itself when idle. It will keep billing until you stop it from Captain."], preflight: { note: null } } });
  return reply(200, {});
};

const { Ship } = await import(path.join(dir, 'ship.js'));
const { palette } = await import(path.join(dir, 'kit/tokens.js'));
const { View } = await import('gpui-kit');
const page = { pid: () => 'shop', open: null, project: () => ({ name: 'Shop' }), reload() {}, openDeployment() {},
  stages: { ship: { steps: [] } }, exps: { champion: 'shop/keyword-baseline@v1', experiments: [{ id: 'exp-1', pipeline: 'shop/starter-ranker@v2', is_champion: false, evaluation: {} }] } };
class Dep extends View {
  init(_p, cx) { this.ship = new Ship(page); this.ship.sync(cx, world.staging.staged ? { staging: world.staging.staged } : {}); }
  render() { return this.ship.deployment(palette(false)); }
}
const open = async (w) => {
  world = w; sent = [];
  const h = harness(Dep);
  await h.start();
  for (let i = 0; i < 40 && !h.mod.ship.places; i++) await h.idle(30);
  await h.idle(60);
  return h;
};
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const NONE = { status: 'none', staged: null, text: 'Nothing is staged.' };
const SM = { name: 'sm', kind: 'aws-sagemaker', purposes: ['serve'], config: { instance_type: 'ml.t2.medium', instance_count: '1' } };
const AWS = { name: 'aws-test', provider: 'aws', label: 'Amazon Web Services', region: 'us-west-1' };

// A cloud login with serving set up: both places, side by side, each with what it costs.
let h = await open({ targets: [{ name: 'local', kind: 'local' }, SM], cloud: AWS, staging: NONE });
let t = h.text();
assert.ok(t.includes('Where should starter-ranker@v2 run?'), `the question: ${t}`);
assert.ok(t.includes('On this computer') && t.includes('Free. Only you can reach it.'), 'this computer, free');
assert.ok(t.includes('In your AWS account') && t.includes('A site can reach it.') && t.includes(EST.text) && t.includes('This keeps billing until you stop it.'), 'the cloud, with the shared estimate');
assert.deepEqual(sent.find((x) => x[1] === '/api/costs/estimate')[2], { kind: 'staging', where: 'sm', size: { instances: 1, instance_type: 'ml.t2.medium' } }, 'the estimate is asked of the shared estimator');
assert.equal(byId(h, 'ship-stage').length + byId(h, 'ship-stage-cloud').length, 2, 'one button each');
assert.ok(h.byIdPrefix('ship-choice-').findIndex((e) => e.call('id')[0] === 'ship-choice-local') < h.byIdPrefix('ship-choice-').findIndex((e) => e.call('id')[0] === 'ship-choice-cloud'), 'this computer first, the first time');
// Staging in the cloud asks first: the cost, the warning the role check found, and nothing starts without a yes.
await h.click('ship-stage-cloud');
for (let i = 0; i < 20 && !byId(h, 'ship-confirm').length; i++) await h.idle(30);
t = h.text();
assert.deepEqual(sent.find((x) => x[1] === '/api/projects/shop/stage' && !x[2].dry_run)[2], { candidate: 'exp-1', target: 'sm' });
assert.ok(t.includes('Start starter-ranker@v2 in your own cloud?') && t.includes(EST.text) && t.includes('This keeps billing until you stop it.'), `the confirm: ${t}`);
assert.ok(t.includes("This endpoint can't stop itself when idle. It will keep billing until you stop it from Captain."), 'the warning before the yes');
assert.equal(sent.filter((x) => x[1].endsWith('/stage') && !x[2].dry_run).length, 1, 'nothing started yet');
assert.equal(store['stage-where:shop'], 'cloud', 'the choice is remembered for this project');
// Next time the cloud comes first.
h = await open({ targets: [SM], cloud: AWS, staging: NONE });
assert.ok(h.byIdPrefix('ship-choice-').findIndex((e) => e.call('id')[0] === 'ship-choice-cloud') < h.byIdPrefix('ship-choice-').findIndex((e) => e.call('id')[0] === 'ship-choice-local'), 'the last choice is offered first');

// A cloud login, but serving isn't set up there: the button says so and opens what is needed.
delete store['stage-where:shop'];
h = await open({ targets: [{ name: 'batch', kind: 'aws-batch', purposes: ['backtest'] }], cloud: AWS, staging: NONE });
t = h.text();
assert.ok(t.includes("Serving isn't set up in your AWS account yet.") && byId(h, 'ship-setup-cloud').length === 1 && byId(h, 'ship-stage-cloud').length === 0, `set up first: ${t}`);
await h.click('ship-setup-cloud');
for (let i = 0; i < 30 && !byId(h, 'ship-setup-go').length; i++) await h.idle(30);
t = h.text();
assert.equal(byId(h, 'ship-setup-sheet').length, 1, 'a sheet, not nothing');
assert.ok(sent.some((x) => x[0] === 'GET' && x[1] === '/api/serving/setup'), 'what it would create is asked of the services, for the connected login');
assert.ok(t.includes('Set up serving in your AWS account') && t.includes('captain-serving') && t.includes('delete its own endpoint') && t.includes('about $0.03 once') && t.includes('Nothing is created until you say so.'), `what it creates and costs, before anything happens: ${t}`);
assert.ok(!sent.some((x) => x[0] === 'POST' && x[1] === '/api/serving/setup'), 'nothing created yet');
// The login lacks a permission: one sentence says which, and the policy for an admin is one click away.
globalThis.__exec = async () => ({ code: 0, stdout: '', stderr: '' });
let clip = null;
h.cx.write_to_clipboard = (x) => { clip = x; };
await h.click('ship-setup-go');
for (let i = 0; i < 60 && !byId(h, 'ship-setup-policy-copy').length; i++) await h.idle(30);
t = h.text();
assert.deepEqual(sent.find((x) => x[0] === 'POST' && x[1] === '/api/serving/setup')[2], { profile: 'aws-test', region: 'us-west-1', confirm: true });
assert.ok(t.includes("Your AWS login can't create the role captain-serving: it lacks iam:CreateRole."), `the step and the action: ${t}`);
await h.click('ship-setup-policy-copy');
assert.deepEqual(JSON.parse(clip), POLICY, 'the exact policy is copied for an admin');

// No cloud login: this computer, and how to connect a cloud.
h = await open({ targets: [], cloud: null, staging: NONE });
t = h.text();
assert.ok(byId(h, 'ship-stage').length === 1 && h.byIdPrefix('ship-choice-cloud').length === 0 && t.includes('connect a cloud login'), `this computer only: ${t}`);
await h.click('ship-stage');
assert.deepEqual(sent.find((x) => x[1] === '/api/projects/shop/stage' && !x[2].dry_run)[2], { candidate: 'exp-1', target: 'local' }, 'staged here, by name');

// Staged on this computer with a cloud connected: where it runs now, and the way online with its hourly estimate.
const RUN = { status: 'running', text: 'shop/starter-ranker@v2 is running on this computer. Only this computer can reach it.', health: { stops_in_s: 14000 },
  staged: { id: 'stg-1', status: 'running', kind: 'local', target: 'local', serve: 'candidate', model_version: 'shop/starter-ranker@v2', needs_key: true,
    arms: { candidate: 'shop/starter-ranker@v2', baseline: 'shop/keyword-baseline@v1' }, endpoint: { url: 'http://127.0.0.1:5001' }, cost: { text: 'Runs on this computer.' }, idle_minutes: 240 } };
h = await open({ targets: [SM], cloud: AWS, staging: RUN });
for (let i = 0; i < 30 && !byId(h, 'ship-stage-cloud').length; i++) await h.idle(30);
t = h.text();
assert.equal(byId(h, 'ship-address')[0].kids.join(''), 'http://127.0.0.1:5001');
assert.ok(t.includes('Only this computer can reach it.') && t.includes('This computer') && t.includes('1 copy live'), `where it runs now: ${t}`);
assert.equal(byId(h, 'ship-stage-cloud').length, 1, 'Stage in your AWS account');
assert.equal(byId(h, 'ship-stage-cloud-cost')[0].kids.join(''), EST.text, 'with the hourly estimate beside it');
assert.equal(h.byIdPrefix('ship-choices').length, 0, 'the choices give way to the running endpoint');
// Will it fit here? Fits: said quietly. Tight: allowed, with a warning. Won't fit: not a normal choice, with the reason.
h = await open({ targets: [SM], cloud: AWS, staging: NONE });
assert.ok(h.text().includes('Needs about 180 MB of memory, and this computer has 30 GB free.'), 'what it needs, when it fits');
h = await open({ targets: [SM], cloud: AWS, staging: NONE, fit: { outcome: 'tight', text: 'This catalog needs about 9.0 GB of memory to serve, and this computer has 12 GB free. It will run, but other work on this computer may slow down. Your cloud has more room.' } });
assert.ok(h.text().includes('It will run, but other work on this computer may slow down.') && byId(h, 'ship-stage')[0].call('on_click'), 'tight: allowed, with the warning');
h = await open({ targets: [SM], cloud: AWS, staging: NONE, fit: { outcome: 'wont-fit', text: 'This catalog needs about 9.0 GB of memory to serve, and this computer has 3.0 GB free. Stage it in your cloud.' } });
assert.ok(h.text().includes('This catalog needs about 9.0 GB of memory to serve, and this computer has 3.0 GB free. Stage it in your cloud.'), 'the reason, in plain words');
assert.equal(byId(h, 'ship-stage')[0].call('on_click'), undefined, "won't fit: the local button is switched off");
assert.ok(byId(h, 'ship-stage-cloud')[0].call('on_click'), 'and the cloud is still offered');

// Over the size limit for this computer: switched off with the reason and a way to change the limit, and the
// cloud comes first. A big model is staged in the cloud even when this computer has the memory.
const OVER = { outcome: 'over-limit', limit_mb: 1024, text: 'This model needs about 2.6 GB to serve, which is more than the 1 GB limit for staging on this computer. Stage it in your cloud.' };
h = await open({ targets: [SM], cloud: AWS, staging: NONE, fit: OVER });
t = h.text();
assert.ok(t.includes(OVER.text) && t.includes('Change the limit in Settings'), `the reason and the way to change it: ${t}`);
assert.equal(byId(h, 'ship-stage')[0].call('on_click'), undefined, 'the local button is switched off');
assert.ok(h.byIdPrefix('ship-choice-').findIndex((e) => e.call('id')[0] === 'ship-choice-cloud') < h.byIdPrefix('ship-choice-').findIndex((e) => e.call('id')[0] === 'ship-choice-local'), 'the cloud card comes first');
globalThis.__runs = [];
await h.click('ship-fit-limit');
assert.ok(JSON.stringify(globalThis.__runs).includes('app.settings'), 'the link opens Settings');
// Over the limit with no cloud connected: say how to connect one.
h = await open({ targets: [], cloud: null, staging: NONE, fit: OVER });
assert.ok(h.text().includes('No cloud is connected. Connect a cloud login to stage this model'), `how to get a cloud: ${h.text()}`);

// What Captain set up in the account is listed, and one action removes it, after a confirm.
h = await open({ targets: [SM], cloud: AWS, staging: NONE, serving: { status: 'ready', created: [{ type: 'iam-role', name: 'captain-serving' }, { type: 'ecr-repository', name: 'captain-serving' }], text: 'Serving is set up: iam-role captain-serving, ecr-repository captain-serving.' } });
assert.ok(h.text().includes('Serving is set up: iam-role captain-serving, ecr-repository captain-serving.'), 'listed on the card');
await h.click('ship-serving-remove');
assert.ok(h.text().includes('Remove everything Captain created for serving in your account?') && !sent.some((x) => x[0] === 'DELETE'), 'it asks first');
await h.click('ship-serving-yes');
for (let i = 0; i < 60 && !h.text().includes("Nothing of Captain's is left in the account."); i++) await h.idle(30);
assert.ok(sent.some((x) => x[0] === 'DELETE' && x[1] === '/api/serving/setup') && h.text().includes("Nothing of Captain's is left in the account."), `removed, and it says so: ${h.text().slice(0, 600)}`);
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bpipeline/i, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad}`);
console.log('deployment choice render test: ok');
process.exit(0);
