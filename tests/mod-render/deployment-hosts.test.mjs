// The other hosts on the deployment page, with a faked backend (no services, no cloud): Modal, Google Cloud, Azure.
//   Modal, never set up        its card, "Set up serving in your Modal workspace", the sheet with what is made, what
//                              each staged model adds and the cost, and nothing created before "Create these"
//   Modal, set up and proven   its card with the estimate by the hour awake, and one button that stages there
//   Google Cloud, a login      its card says it has not run on a real account; the sheet lists the roles a login
//                              needs, and a refused setup copies them for an admin
//   Azure, no login            no card: there is nothing to set it up with
//   staged on Modal, asleep    the header says Asleep, where it runs, and that it costs nothing
//   what is set up             listed per host, with the one action that removes it
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/deployment-hosts.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-project');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };

const MODAL_EST = { kind: 'staging', where: 'modal-serving', free: false, available: true, sleeps: true, low: 0, high: 0.063, unit: 'hour', basis: [],
  text: 'About $0.06 an hour while a copy is awake on modal-serving, nothing while it sleeps. Estimate, prices as of 2026-11-11.' };
const NO_EST = { kind: 'staging', where: 'gcp-serving', available: false, text: 'No estimate for Google Cloud Run (jobs and services) yet.', basis: [] };
const ROLES = { project: 'demo-project', roles: [{ role: 'roles/run.admin', why: 'create, change and delete the Cloud Run service' }], commands: ['gcloud projects add-iam-policy-binding demo-project --member=user:<the login> --role=roles/run.admin'] };
const host = (cloud, label, kind, target, more = {}) => ({ cloud, label, kind, target, login: cloud === 'modal' ? null : cloud, scale_to_zero: true, proven: null,
  proven_text: 'Not run on a real account yet. Tested against a stand-in only.', storage: '', address: '', options: [], setup: { status: 'none', target: null, text: '' }, ...more });
const HOSTS = (over = {}) => [
  { cloud: 'local', label: 'This computer', kind: 'local', target: 'local', scale_to_zero: false, proven: null, proven_text: null, setup: { status: 'ready' } },
  { cloud: 'aws', label: 'AWS', kind: 'aws-sagemaker', target: 'aws-serving', login: 'aws', scale_to_zero: false, proven: '2026-11-11', setup: { status: 'none' } },
  host('modal', 'Modal', 'modal-endpoint', 'modal-serving', over.modal), host('gcp', 'Google Cloud', 'gcp-cloud-run', 'gcp-serving', over.gcp),
  host('azure', 'Azure', 'azure-container-apps', 'azure-serving', over.azure)];
const PLANS = {
  modal: { cloud: 'modal', environment: 'search', creates: [{ resource: 'Modal volume', name: 'captain-serving', why: 'Holds the serving bundle (the model and its catalog) and the request log. The endpoint mounts it, so no keys are needed.' },
    { resource: 'Captain compute target', name: 'modal-serving', why: 'Modal web endpoint, 1 CPU and 2 GB a copy, at most 1. Staging uses it.' }],
    each_deploy: [{ resource: 'Modal app', name: 'captain-<project>-staging', why: 'The endpoint. Made when you stage a model, stopped when you unstage it.' }],
    cost: { text: 'Setting up costs nothing. A copy costs about $0.063 an hour while it is awake and nothing while it sleeps.' }, login_policy: null,
    login_text: 'Modal has no policy to grant. Whoever runs this needs to be signed in to Modal on this computer.' },
  gcp: { cloud: 'gcp', profile: 'gcp-test', region: 'us-central1', creates: [{ resource: 'API switched on', name: 'run.googleapis.com', why: 'Cloud Run runs the endpoint.' },
    { resource: 'Service account', name: 'captain-run', why: 'The endpoint runs as this. It may read and add objects in the serving bucket. Nothing else.' }],
    each_deploy: [{ resource: 'Cloud Run service', name: 'captain-<project>-staging', why: 'The endpoint.' }],
    cost: { text: 'Setting up costs about $0.02 once. A copy costs about $0.104 an hour while it is answering and nothing between requests.' }, login_policy: ROLES,
    login_text: 'Grant these roles on the project to the login that sets this up. Owner or Editor has them already.' } };
let world, sent;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url), p = u.pathname, method = opts.method ?? 'GET';
  const body = opts.body ? JSON.parse(opts.body) : null;
  sent.push([method, p + (u.search && p === '/api/serving/setup' ? u.search : ''), body]);
  if (p === '/api/compute') return reply(200, world.targets);
  if (p === '/api/cloud/summary') return reply(200, { active: world.profiles.length === 1 ? world.profiles[0] : null, profiles: world.profiles });
  if (p === '/api/serving/hosts') return reply(200, world.hosts);
  if (p === '/api/costs/estimate') return reply(200, body.where === 'modal-serving' ? MODAL_EST : body.where === 'local' ? { free: true, available: true, text: 'Runs on this computer. No cloud cost.', basis: [] } : NO_EST);
  if (p === '/api/serving/setup' && method === 'DELETE') return reply(200, { job: 'remove-1' });
  if (p === '/api/jobs/remove-1') return reply(200, { status: 'done', result: { status: 'removed', text: "Serving setup is removed. Nothing of Captain's is left in your Modal account." } });
  if (p === '/api/serving/setup' && method === 'GET' && u.searchParams.get('cloud')) return reply(200, { status: 'none', created: [], plan: PLANS[u.searchParams.get('cloud')] });
  if (p === '/api/serving/setup' && method === 'GET') return reply(200, { status: 'none', created: [] });
  if (p === '/api/serving/setup') return reply(200, { job: `setup-${body.cloud}` });
  if (p === '/api/jobs/setup-modal') return reply(200, { status: 'done', result: { status: 'ready', text: 'Serving is set up on Modal.' } });
  if (p === '/api/jobs/setup-gcp') return reply(200, { status: 'failed', policy: ROLES,
    message: "Your Google Cloud login can't create the bucket captain-serving-demo-project: it lacks storage.buckets.create. Ask an admin to grant what is below, then try again." });
  if (p.endsWith('/staging')) return reply(200, world.staging);
  if (p.endsWith('/staging/connect')) return reply(200, { reachable: true, connected: false, address: 'https://w--captain-shop-staging.modal.run', text: 'Running on Modal.', live: 'Waiting for the first request.', snippets: {}, real: 0, test: 0 });
  if (p.endsWith('/ab')) return reply(200, { status: 'none', experiment: null, can_measure: ['clicks'] });
  if (p.endsWith('/stage') && body.dry_run) return reply(200, { dry_run: true, local_fit: { outcome: 'fits', text: 'Needs about 180 MB of memory, and this computer has 30 GB free.' } });
  if (p.endsWith('/stage')) return reply(409, { needs_confirm: true, detail: 'Confirm the cost first.', plan: { target: body.target, kind: 'modal-endpoint', model_version: 'shop/starter-ranker@v2',
    cost: { sleeps: true, estimate: MODAL_EST, text: MODAL_EST.text }, warnings: [], preflight: null } });
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
  for (const k of Object.keys(store)) delete store[k];
  const h = harness(Dep);
  await h.start();
  for (let i = 0; i < 40 && !(h.mod.ship.places && h.mod.ship.hosts.length); i++) await h.idle(30);
  await h.idle(60);
  return h;
};
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const until = async (h, id) => { for (let i = 0; i < 60 && !byId(h, id).length; i++) await h.idle(30); };
const NONE = { status: 'none', staged: null, text: 'Nothing is staged.' };
const GCP_LOGIN = { name: 'gcp-test', provider: 'gcp', label: 'Google Cloud', region: null };
globalThis.__exec = async () => ({ code: 0, stdout: '', stderr: '' });

// Nothing set up, a Google Cloud login connected: this computer, Modal and Google Cloud. No Azure card, no AWS card.
let h = await open({ targets: [{ name: 'local', kind: 'local' }], profiles: [GCP_LOGIN], hosts: HOSTS(), staging: NONE });
let t = h.text();
assert.deepEqual(h.byIdPrefix('ship-choice-').map((e) => e.call('id')[0]).filter((id) => /^ship-choice-[a-z]+$/.test(id)), ['ship-choice-local', 'ship-choice-modal', 'ship-choice-gcp'], `the cards: ${t}`);
assert.ok(t.includes('On Modal') && t.includes("Serving isn't set up on Modal yet.") && t.includes('It sleeps when nobody calls it, and costs nothing asleep.'), `Modal's card: ${t}`);
assert.ok(t.includes('In your Google Cloud project') && t.includes("Serving isn't set up in your Google Cloud project yet."), 'Google Cloud, by its login');
assert.equal(byId(h, 'ship-choice-modal-unproven').length + byId(h, 'ship-choice-gcp-unproven').length, 2, 'a host that has not run on a real account says so on its card');
assert.ok(t.includes('Not run on a real account yet. Tested against a stand-in only.'), 'in those words');
assert.ok(!t.includes('connect a cloud login'), 'a cloud login is connected, so that is not asked for');

// The Modal sheet: what is made, what each staged model adds, the cost, and nothing created until a yes.
await h.click('ship-setup-modal');
await until(h, 'ship-host-go');
t = h.text();
assert.ok(t.includes('Set up serving in your Modal workspace') && t.includes('Modal volume') && t.includes('captain-serving') && t.includes('so no keys are needed'), `the sheet: ${t}`);
assert.ok(t.includes('Each model you stage there adds:') && t.includes('Modal app') && t.includes('nothing while it sleeps') && t.includes('Modal has no policy to grant'), 'what a staged model adds, the cost, and who can run it');
assert.ok(sent.some((x) => x[0] === 'GET' && x[1] === '/api/serving/setup?cloud=modal&plan=true'), 'the plan is asked of the services');
assert.ok(!sent.some((x) => x[0] === 'POST' && x[1] === '/api/serving/setup'), 'nothing created yet');
assert.equal(byId(h, 'ship-host-policy-copy').length, 0, 'Modal has nothing to copy for an admin');
await h.click('ship-host-go');
for (let i = 0; i < 60 && !h.text().includes('You can stage there now.'); i++) await h.idle(30);
assert.deepEqual(sent.find((x) => x[0] === 'POST' && x[1] === '/api/serving/setup')[2], { cloud: 'modal', confirm: true, environment: 'search' });
assert.ok(h.text().includes('Serving is set up in your Modal workspace. You can stage there now.') && byId(h, 'ship-host-sheet').length === 0, 'done, and the sheet closes');

// The Google Cloud sheet: the roles a login needs are there to copy before it is tried, and a refusal says the step.
await h.click('ship-setup-gcp');
await until(h, 'ship-host-go');
t = h.text();
assert.ok(t.includes('Set up serving in your Google Cloud') && t.includes('in us-central1') && t.includes('API switched on') && t.includes('run.googleapis.com'), `the sheet: ${t}`);
assert.ok(sent.some((x) => x[0] === 'GET' && x[1] === '/api/serving/setup?cloud=gcp&plan=true&profile=gcp-test&region=us-central1'), 'with the connected login and a region');
let clip = null;
h.cx.write_to_clipboard = (x) => { clip = x; };
await h.click('ship-host-go');
await until(h, 'ship-host-denied');
t = h.text();
assert.ok(t.includes("Your Google Cloud login can't create the bucket captain-serving-demo-project: it lacks storage.buckets.create."), `the step and the action: ${t}`);
await h.click('ship-host-policy-copy');
assert.deepEqual(JSON.parse(clip), ROLES, 'the roles are copied for an admin');
assert.ok(h.text().includes('Send it to whoever manages your Google Cloud project.'), 'and who to send them to');

// Modal set up and proven: its estimate by the hour awake, one button that stages there, and its setup listed.
const MODAL_T = { name: 'modal-serving', kind: 'modal-endpoint', purposes: ['serve'], config: {} };
h = await open({ targets: [{ name: 'local', kind: 'local' }, MODAL_T], profiles: [], staging: NONE,
  hosts: HOSTS({ modal: { proven: '2026-11-11', proven_text: 'Ran on a real account on 2026-11-11.', setup: { status: 'ready', target: 'modal-serving', text: 'Serving is set up: modal-volume captain-serving, compute-target modal-serving.' } } }) });
t = h.text();
assert.ok(t.includes(MODAL_EST.text) && byId(h, 'ship-stage-modal').length === 1 && byId(h, 'ship-choice-modal-unproven').length === 0, `proven, priced, one button: ${t}`);
assert.deepEqual(sent.find((x) => x[1] === '/api/costs/estimate' && x[2].where === 'modal-serving')[2], { kind: 'staging', where: 'modal-serving', size: {} }, 'the shared estimator');
assert.equal(h.byIdPrefix('ship-choice-gcp').length + h.byIdPrefix('ship-choice-azure').length, 0, 'no login for those clouds, so no card');
assert.ok(t.includes('connect a cloud login'), 'and how to get one is still said');
await h.click('ship-stage-modal');
for (let i = 0; i < 20 && !byId(h, 'ship-confirm').length; i++) await h.idle(30);
assert.deepEqual(sent.find((x) => x[0] === 'POST' && x[1].endsWith('/stage') && !x[2].dry_run)[2].target, 'modal-serving', 'it stages on that target');
assert.ok(h.text().includes('nothing while it sleeps'), 'the confirm shows the cost by the hour awake');
// What is set up is listed, with the one action that removes it, after a second yes.
assert.ok(byId(h, 'ship-host-line-modal').length === 1 && h.text().includes('Modal. Serving is set up: modal-volume captain-serving'), 'listed');
await h.click('ship-host-remove-modal');
assert.ok(h.text().includes('Remove everything Captain created for serving in your Modal workspace?') && !sent.some((x) => x[0] === 'DELETE'), 'asks first');
await h.click('ship-host-yes-modal');
for (let i = 0; i < 60 && !h.text().includes("Nothing of Captain's is left"); i++) await h.idle(30);
assert.ok(sent.some((x) => x[0] === 'DELETE' && x[1] === '/api/serving/setup?cloud=modal') && h.text().includes("Nothing of Captain's is left in your Modal account."), 'removed, and it says so');

// Staged on Modal and asleep: the header says so, where it runs, and that it costs nothing. No hourly "keeps billing".
const STAGED = { id: 'stg-1', env: 'staging', status: 'running', kind: 'modal-endpoint', target: 'modal-serving', model_version: 'shop/starter-ranker@v2', serve: 'candidate', scale_to_zero: true,
  arms: { candidate: 'shop/starter-ranker@v2', baseline: 'shop/keyword-baseline@v1' }, endpoint: { name: 'captain-shop-staging', url: 'https://w--captain-shop-staging.modal.run' }, needs_key: true,
  cost: { sleeps: true, text: MODAL_EST.text, estimate: MODAL_EST }, idle_minutes: 240, resources: [] };
h = await open({ targets: [{ name: 'local', kind: 'local' }, MODAL_T], profiles: [], hosts: HOSTS({ modal: { proven: '2026-11-11', setup: { status: 'ready', target: 'modal-serving', text: 'Serving is set up.' } } }),
  staging: { status: 'running', staged: STAGED, asleep: true, copies: 0, health: null, text: 'shop/starter-ranker@v2 is running on Modal. Asleep: no copy is up, and it costs nothing. It wakes on the next request.' } });
for (let i = 0; i < 40 && !byId(h, 'ship-chip-where').length; i++) await h.idle(30);
t = h.text();
assert.ok(t.includes('Asleep') && byId(h, 'ship-asleep').length === 1 && t.includes('It wakes on the next request.'), `asleep, said plainly: ${t}`);
assert.ok(t.includes('It runs on Modal. Your site can call it.') && t.includes('https://w--captain-shop-staging.modal.run'), 'where it runs and its address');
assert.ok(!t.includes('1 copy live') && !t.includes('This keeps billing until you stop it.') && !t.includes('Your cloud account'), 'never drawn as a copy up and billing by the hour');
assert.ok(t.includes('It sleeps when nobody calls it, and costs nothing asleep.'), 'what it costs');

for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bpipeline/i, /[←-⇿]/, /—/, /\bserverless\b/i, /\bscale to zero\b/i]) assert.ok(!bad.test(h.text()), `no ${bad}`);
console.log('deployment hosts render test: ok');
process.exit(0);
