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
let world, sent;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url, opts = {}) => {
  const p = new URL(url).pathname;
  const body = opts.body ? JSON.parse(opts.body) : null;
  sent.push([opts.method ?? 'GET', p, body]);
  if (p === '/api/compute') return reply(200, world.targets);
  if (p === '/api/cloud/summary') return reply(200, { active: world.cloud });
  if (p === '/api/costs/estimate') return reply(200, body.where === 'local' ? { free: true, available: true, text: 'Runs on this computer. No cloud cost.', basis: [] } : EST);
  if (p.endsWith('/staging')) return reply(200, world.staging);
  if (p.endsWith('/staging/connect')) return reply(200, { reachable: false, connected: false, address: 'http://127.0.0.1:5001', text: 'Running on this computer. Only this computer can reach it.', live: 'Waiting for the first request.', real: 0, test: 0, simulated: 0, running: 0, evidence_label: null, snippets: {}, key_note: '', fallback: '', test_request: 'curl -s http://127.0.0.1:5001/rank' });
  if (p.endsWith('/ab')) return reply(200, { status: 'none', experiment: null, can_measure: ['clicks'] });
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
const AWS = { provider: 'aws', label: 'aws-test', region: 'us-west-1' };

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
assert.deepEqual(sent.find((x) => x[1] === '/api/projects/shop/stage')[2], { candidate: 'exp-1', target: 'sm' });
assert.ok(t.includes('Start starter-ranker@v2 in your own cloud?') && t.includes(EST.text) && t.includes('This keeps billing until you stop it.'), `the confirm: ${t}`);
assert.ok(t.includes("This endpoint can't stop itself when idle. It will keep billing until you stop it from Captain."), 'the warning before the yes');
assert.equal(sent.filter((x) => x[1].endsWith('/stage')).length, 1, 'nothing started yet');
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
t = h.text();
assert.equal(byId(h, 'ship-setup-sheet').length, 1, 'a sheet, not nothing');
assert.ok(t.includes('What serving in AWS needs') && t.includes('delete its own endpoint so it can stop itself') && t.includes("container registry") && t.includes('Open Cloud and compute'), `what is needed: ${t}`);

// No cloud login: this computer, and how to connect a cloud.
h = await open({ targets: [], cloud: null, staging: NONE });
t = h.text();
assert.ok(byId(h, 'ship-stage').length === 1 && h.byIdPrefix('ship-choice-cloud').length === 0 && t.includes('connect a cloud login'), `this computer only: ${t}`);
await h.click('ship-stage');
assert.deepEqual(sent.find((x) => x[1] === '/api/projects/shop/stage')[2], { candidate: 'exp-1', target: 'local' }, 'staged here, by name');

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
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bpipeline/i, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad}`);
console.log('deployment choice render test: ok');
process.exit(0);
