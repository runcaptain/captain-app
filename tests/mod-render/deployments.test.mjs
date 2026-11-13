// The Deployments screen, with a faked backend: one row per deployed model across projects and clouds, as the
// cloud reports it. Empty, one running, several across clouds, one failed, one found in the cloud with no project,
// the actions, the filters, what Captain created in each account, and a model's own page.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/deployments.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-deployments');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
let world, sent, feedSeq = 0;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
const numbers = (requests, extra = {}) => ({ range: '24h', range_text: 'the last 24 hours', bucket_seconds: 300, source: 'cloud', source_text: "From AWS's own metrics (CloudWatch). They arrive a minute or two late.", not_read: [],
  buckets: Array.from({ length: 288 }, (_, i) => ({ t: String(i), requests: requests / 288, errors: 0, p50_ms: 3, p95_ms: 9, cpu: 20, memory: 5, copies: null })),
  summary: { requests, errors: 0, per_minute: requests / 1440, per_minute_peak: 12, p50_ms: 3.1, p95_ms: 9.4, cpu: 20, cpu_peak: 35, memory: 5, memory_peak: 6, copies_now: 1, copies_min: 1, copies_max: 3, copies_text: '1 copy up now. It may run 1 to 3.' },
  cost: { hourly_usd: 0.066, rate_usd: 0.066, today_usd: 0.92, month_usd: 14.3, text: 'About $0.07 an hour for each copy while it is up. Your AWS bill is the final word.' },
  text: `${requests.toLocaleString('en-US')} requests in the last 24 hours, no errors. Median 3.1 ms, 95th percentile 9.4 ms at the endpoint.`, ...extra });
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url), p = u.pathname, m = opts.method ?? 'GET';
  sent.push([m, p + u.search, opts.body ? JSON.parse(opts.body) : null]);
  // The change feed (kit/live.js). This stand-in says every asked-for topic changed each time it is asked, so each
  // tick of the test is "something changed": the screen reads again, as it does when the services say so.
  if (p === '/api/changes') { feedSeq += 1; return world.feedDown ? reply(500, { detail: 'no answer' }) : reply(200, { seq: feedSeq, changed: (u.searchParams.get('topics') ?? '').split(',').filter(Boolean).map((topic) => ({ topic, seq: feedSeq })), reset: false }); }
  if (p === '/api/deployed' && world.deployedFail) return reply(500, { detail: 'no answer' });
  if (p === '/api/deployed') return reply(200, world.deployed);
  // Work the services do as a job: `world.jobs[id]` is the list of answers it gives, one per ask, the last repeating.
  if (p.startsWith('/api/jobs/') && world.jobsFail) return reply(500, { detail: 'no answer' });
  if (p.startsWith('/api/jobs/')) { const steps = world.jobs?.[p.split('/')[3]] ?? [{ status: 'done' }]; return reply(200, steps.length > 1 ? steps.shift() : steps[0]); }
  if (m === 'POST' && p.endsWith('/unstage') && world.stopJob) return reply(200, { job: world.stopJob });
  // The plan for a deploy: what the confirm is built from. `world.plan` overrides it, or fails it.
  if (m === 'POST' && p.endsWith('/deploy') && JSON.parse(opts.body ?? '{}').dry_run) return world.planFails ? reply(400, { detail: world.planFails })
    : reply(200, world.plan ?? { host: 'gcp', label: 'Google Cloud', first_time: false, needs_confirm: true, minutes: { text: 'About 2 to 6 minutes.' },
      cost: { text: 'About $0.10 an hour while a copy is awake, and nothing while it sleeps.' } });
  if (p === '/api/deployed/created') return reply(200, world.created ?? { places: [], text: '' });
  if (p === '/api/projects') return reply(200, { projects: [{ id: 'shop', name: 'Demo shop' }, { id: 'trail', name: 'Trail gear' }] });
  if (p.endsWith('/deployed/numbers')) return reply(200, world.numbers?.[p.split('/')[3]] ?? numbers(0, { source: 'none', text: 'No requests in the last 24 hours.', cost: null }));
  if (p.endsWith('/staging/versions')) return reply(200, { rows: [{ n: 2, model_version: 'shop/starter-ranker@v2', at: new Date().toISOString(), by: 'dana', live: true },
    { n: 1, model_version: 'shop/keyword-baseline@v1', at: new Date(Date.now() - 86400000).toISOString(), by: 'dana', live: false, can_roll_back: true }] });
  if (p.endsWith('/staging/requests')) return reply(200, { rows: [{ request_id: 'r1', ts: new Date().toISOString(), query: 'trail running shoes', model_version: 'shop/starter-ranker@v2', ms: 3.2 }], total: 1 });
  if (p.endsWith('/staging/logs')) return reply(200, { rows: [{ ts: '2026-11-12T10:00:00+00:00', text: 'Loaded 480 products.', level: 'info' }] });
  if (p.endsWith('/deployed/where')) return reply(200, world.where ?? { from_cloud: true, text: 'Read from AWS just now.', facts: [
    { id: 'address', label: 'Address', value: 'https://runtime.sagemaker.us-west-1.amazonaws.com/endpoints/captain-shop/invocations', copy: true, source: 'cloud', source_text: 'From AWS' },
    { id: 'call', label: 'How to call it', value: '# Get a ranking.\naws sagemaker-runtime invoke-endpoint --region us-west-1 --endpoint-name captain-shop \\\n  --body \'{"key": "\'"$CAPTAIN_STAGING_KEY"\'", "traffic": "test"}\' /dev/stdout', copy: true, source: 'record', source_text: "From Captain's record" },
    { id: 'cloud', label: 'Cloud and account', value: 'AWS, account ending 1234', copy: false, source: 'cloud', source_text: 'From AWS' },
    { id: 'region', label: 'Region', value: 'us-west-1', copy: false, source: 'record', source_text: "From Captain's record" },
    { id: 'machine', label: 'Machine', value: 'ml.t2.medium: 2 virtual CPUs, 4 GB memory, about $0.07 an hour for each copy', copy: false, source: 'cloud', source_text: 'From AWS' },
    { id: 'copies', label: 'Copies', value: '1 up now. Fewest 1, most 3', copy: false, source: 'cloud', source_text: 'From AWS' },
    { id: 'image', label: 'Image', value: '000000000000.dkr.ecr.us-west-1.amazonaws.com/captain-serving:1, built 5 hours ago', copy: false, source: 'cloud', source_text: 'From AWS' },
    { id: 'files', label: 'Model files', value: 's3://captain-serving-000000000000-us-west-1/demo/staging/shop/bundle.json', copy: false, source: 'record', source_text: "From Captain's record" },
    { id: 'role', label: 'Role it runs as', value: 'arn:aws:iam::000000000000:role/captain-serving', copy: false, source: 'cloud', source_text: 'From AWS' },
    { id: 'logs', label: 'Logs', value: '/aws/sagemaker/Endpoints/captain-shop', copy: true, source: 'cloud', source_text: 'From AWS' },
    { id: 'stops', label: 'Stops by itself after', value: '240 minutes with no requests', copy: false, source: 'record', source_text: "From Captain's record" },
    { id: 'by', label: 'Deployed by', value: 'dana, 2 hours ago', copy: false, source: 'record', source_text: "From Captain's record" },
    { id: 'version', label: 'Version', value: 'starter-ranker, version 2 (shop/starter-ranker@v2)', copy: false, source: 'record', source_text: "From Captain's record" }] });
  if (p.endsWith('/staging/scale')) return reply(200, { text: 'Keeps 1 up at all times. It never sleeps, and bills for each copy by the hour. 1 up now.' });
  return reply(200, { text: 'Done.' });
};
const row = (o) => ({ env: 'staging', login: 'work-aws', copies: { now: 1, min: null, max: null }, since: new Date(Date.now() - 7200000).toISOString(), by: 'dana@example.com', by_name: 'dana', link: 'linked',
  corrected: [], sleeps: false, address: null, state_label: 'Running', can: [], in_cloud: true, remembered_text: null, ...o });
const SHOP = row({ id: 'shop:staging', project: 'shop', project_name: 'Demo shop', model_version: 'shop/starter-ranker@v2', model_label: 'starter-ranker, version 2', cloud: 'aws', cloud_label: 'AWS', region: 'us-west-1',
  machine: 'ml.t2.medium', where: 'AWS, us-west-1, ml.t2.medium', name: 'captain-shop', state: 'running', state_text: 'Running on 1 copy.',
  address: 'https://runtime.sagemaker.us-west-1.amazonaws.com/endpoints/captain-shop/invocations', account: 'account ending 1234',
  machine_words: 'ml.t2.medium: 2 virtual CPUs, 4 GB memory', copies_words: '1 copy up now, may run 1 to 3' });
const TRAIL = row({ id: 'trail:staging', project: 'trail', project_name: 'Trail gear', model_version: 'trail/starter-ranker@v1', cloud: 'modal', cloud_label: 'Modal', login: 'modal', region: null,
  machine: '1 CPU, 2 GB', where: 'Modal, 1 CPU, 2 GB', name: 'captain-trail-staging', state: 'asleep', state_text: 'Asleep. It wakes on the next request and bills nothing until then.',
  sleeps: true, address: 'https://demo--captain-trail-staging.modal.run' });
const OLD = row({ id: 'kids:staging', project: 'kids', project_name: 'Kids range', model_version: 'kids/keyword-baseline@v1', cloud: 'gcp', cloud_label: 'Google Cloud', login: 'g', region: 'us-central1',
  machine: '1 vCPU, 2 GB', where: 'Google Cloud, us-central1, 1 vCPU, 2 GB', name: 'captain-kids-staging', state: 'stopped', state_text: 'Stopped by itself, 3 days ago: no requests for 240 minutes.', in_cloud: false,
  model_label: 'keyword-baseline, version 1', remembered_text: 'Nothing of it is running in Google Cloud and nothing is billing. Captain remembers it so you can start it again.', stopped_at: new Date(Date.now() - 3 * 86400000).toISOString() });
const BAD = row({ id: 'gifts:staging', project: 'gifts', project_name: 'Gift finder', model_version: 'gifts/starter-ranker@v3', cloud: 'aws', cloud_label: 'AWS', region: 'us-west-1', machine: 'ml.t2.medium',
  where: 'AWS, us-west-1, ml.t2.medium', name: 'captain-gifts-staging', state: 'failed', state_text: 'Failed: The primary container did not pass the ping health check.',
  corrected: ["Captain's record said it was running. The cloud says it failed, so the record now says failed."] });
const FOUND = row({ id: 'cloud:aws:work-aws:us-west-1:captain-old-experiment-staging', project: 'an-older-project', project_name: null, model_version: null, cloud: 'aws', cloud_label: 'AWS', region: 'us-west-1',
  machine: 'ml.m5.large', where: 'AWS, us-west-1, ml.m5.large', name: 'captain-old-experiment-staging', state: 'running', link: 'unlinked', by: null,
  state_text: 'Found in your AWS account, not linked to a project.' });
const asked = [{ cloud: 'aws', cloud_label: 'AWS', login: 'work-aws', region: 'us-west-1', ok: true, error: null, found: 1 }];

const { default: Deployments } = await import(path.join(dir, 'main.js'));
const open = async (w) => { world = w; sent = []; const h = harness(Deployments); await h.start(); for (let i = 0; i < 60 && !h.mod.d; i++) await h.idle(30); await h.idle(120); return h; };
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
// The harness runs a screen's timers only when asked: this is one tick of them, then whatever they started.
const tick = async (h) => { await h.layoutLists({}); await h.idle(80); };
const shown = (h) => ['shop', 'trail', 'kids', 'gifts'].filter((p) => byId(h, `dep-row-${p}:staging`).length === 1).join(' ');
const filled = (h) => h.byIdPrefix('').filter((e) => e.calls?.some?.((x) => x[0] === 'bg' && x[1]?.[0] === h.palette?.solid));

// ---- nothing deployed ----
let h = await open({ deployed: { rows: [], asked: [], text: 'Nothing is deployed. Deploy a model from a project\'s page to see it here.', read_at: '' } });
let t = h.text();
assert.ok(t.includes('Deployments') && t.includes('Models') && !t.includes('No models are running.') && byId(h, 'dep-go-projects').length === 1 && t.includes('Deploy a model'), `empty: ${t}`);
assert.equal(byId(h, 'dep-filters').length, 0, 'no filters over an empty list');
// Nothing at all: one muted line says what the screen is for, so the page is not bare.
assert.ok(byId(h, 'dep-what-for').length === 1 && t.includes("A model you deploy from a project's page shows here"), `what it is for: ${t}`);
assert.equal(byId(h, 'dep-created').length, 0, 'nothing about an account when nothing was created');

// Nothing deployed, and things from an earlier deploy still in the account: the page is not empty. It says nothing
// is deployed, offers to deploy, and lists what Captain created there with "Remove everything".
h = await open({ deployed: { rows: [], asked: [], text: 'Nothing is deployed.', read_at: '' }, created: { text: '', places: [{ cloud: 'aws', cloud_label: 'AWS', login: 'work-aws', region: 'us-west-1',
  in_use_by: [], can_remove: true, text: '', things: [
    { kind: 'role', name: 'captain-serving', what_for: 'What a running model is allowed to do: read its own files and write its request log. Nothing else.' },
    { kind: 'role', name: 'captain-serving-build', what_for: "Builds the model's image inside your account. It runs only when an image is built." },
    { kind: 'ecr-repository', name: 'captain-serving', what_for: "Holds the model's image in your account." },
    { kind: 'codebuild-project', name: 'captain-serving-build', what_for: "Builds the model's image inside your account. It runs only when an image is built." },
    { kind: 'bucket', name: 'captain-serving-us-west-1', what_for: "Holds the model's files and its request log." }] }] } });
t = h.text();
// The screen is a list of models first. What is in the account is one closed line at the bottom, until opened.
assert.ok(!t.includes('No models are running.') && t.includes('Deploy a model'), `no models: the title says so once, and Models offers to deploy: ${t}`);
assert.equal(byId(h, 'dep-what-for').length, 0, 'with an account line on the page, the page says enough');
assert.ok(t.includes('Set up in your AWS account: 5 things Captain created so it can deploy there.') && byId(h, 'dep-setup-show-aws').length === 1, `one line for the account: ${t}`);
assert.ok(t.indexOf('Models') < t.indexOf('Set up in your AWS account'), 'the models come first');
assert.equal(byId(h, 'dep-thing-aws-0').length, 0, 'closed: the things are not listed');
assert.equal(byId(h, 'dep-setup-remove-aws').length, 0, 'closed: nothing to remove by accident');
await h.click('dep-setup-show-aws');
t = h.text();
for (let i = 0; i < 5; i++) assert.equal(byId(h, `dep-thing-aws-${i}`).length, 1, `thing ${i} of the five is listed`);
assert.ok(t.includes('Hide'), 'and it can be closed again');
assert.ok(t.includes("Holds the model's files and its request log.") && byId(h, 'dep-setup-remove-aws').length === 1 && t.includes('Remove everything'), 'each with what it is for, and Remove everything');
await h.click('dep-setup-remove-aws');
assert.ok(h.text().includes('Remove everything Captain created in your AWS account?'), 'it asks first');
await h.click('dep-ask-yes');
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'DELETE' && x[1] === '/api/serving/setup?cloud=aws'), 'then removes it');

// One model, stopped earlier, and what its first deploy left in the account. A person stopped their model and asked
// "why is it still listed?": nothing of it was in the cloud, and the list showed Captain's memory of it as a
// deployment. Now the list is what is in a cloud. A stopped model sits under one closed line.
h = await open({ deployed: { rows: [OLD], asked, text: 'No models running.', stopped_earlier: 1, read_at: '' }, created: { text: '', places: [{ cloud: 'aws', cloud_label: 'AWS', login: 'aws', login_shown: null,
  region: 'us-west-1', in_use_by: [], can_remove: true, text: '', things: [
    { kind: 's3-object', kind_label: 'Build package', name: 's3://captain-serving-000000000000-us-west-1/build/serving.zip', short: 'serving.zip', what_for: "The files the model's image is built from. Kept so the image can be built again without sending them." },
    { kind: 'log-group', kind_label: 'Build log', name: '/aws/codebuild/captain-serving-build', short: 'captain-serving-build', what_for: 'What the image build printed while it ran. Kept so a build that failed can be read.' }] }] } });
t = h.text();
assert.ok(t.includes('No models running.') && t.includes('Models') && !t.includes('No models are running.') && byId(h, 'dep-go-projects').length === 1, `nothing running: says so, and offers to deploy: ${t}`);
assert.equal(shown(h), '', 'a stopped model is not in the list of what is deployed');
assert.ok(t.includes('Stopped earlier: 1 model.') && byId(h, 'dep-earlier-show').length === 1, `it has its own closed line: ${t}`);
assert.ok(t.indexOf('Stopped earlier') < t.indexOf('Set up in your AWS account') && t.indexOf('Models') < t.indexOf('Stopped earlier'), 'models, then stopped earlier, then the account');
for (const gone of ['dep-show-running', 'dep-show-stopped', 'dep-show-all']) assert.equal(byId(h, gone).length, 0, 'no chips for the state');
await h.click('dep-earlier-show');
t = h.text();
assert.equal(shown(h), 'kids', 'opened: the stopped model');
assert.ok(t.includes('Kids range') && t.includes('keyword-baseline, version 1'), 'by its project and its model');
assert.ok(t.includes('Stopped by itself, 3 days ago: no requests for 240 minutes. Nothing of it is running in Google Cloud and nothing is billing. Captain remembers it so you can start it again.'), `what it is, plainly: ${t}`);
assert.ok(byId(h, `dep-start-${OLD.id}`).length === 1 && byId(h, `dep-forget-${OLD.id}`).length === 1, 'Start again and Forget it');
assert.ok(byId(h, `dep-remove-${OLD.id}`).length === 0 && !t.includes('Remove from the cloud') && byId(h, `dep-open-${OLD.id}`).length === 0, 'nothing of it is in the cloud, so nothing offers to remove it from one');
assert.equal(byId(h, `dep-spark-${OLD.id}`).length, 0, 'no small chart for a model with no requests in the last hour');
assert.ok(!t.includes('$0.00 this month'), 'a cost that is not known is not shown as nothing');
// Forgetting asks first, says nothing in the cloud changes, and removes only Captain's record.
await h.click(`dep-forget-${OLD.id}`);
assert.ok(h.text().includes("Forget Kids range's stopped model? Captain removes its own record of it. Nothing in the cloud changes, because nothing of it is there."), `the question: ${h.text()}`);
assert.ok(byId(h, 'dep-ask-yes').length === 1 && byId(h, 'dep-go-projects').length === 0, 'an open question holds the one filled button');
assert.ok(!sent.some((x) => x[1] === '/api/deployed/forget'), 'nothing is forgotten before the answer');
await h.click('dep-ask-yes');
await h.idle(60);
assert.deepEqual(sent.find((x) => x[1] === '/api/deployed/forget')?.[2], { project: 'kids', env: 'staging', confirm: true }, 'then Captain forgets it');
assert.ok(!sent.some((x) => x[0] === 'DELETE'), 'and nothing is removed from any cloud');
// Start again says what it will do, and sends the same version to the same place.
// Start again asks the services where it will run first, then asks the person: the place by name, how long, the cost.
await h.click(`dep-start-${OLD.id}`);
await h.idle(80);
assert.deepEqual(sent.find((x) => x[0] === 'POST' && x[1] === '/api/projects/kids/deploy')?.[2], { host: 'gcp', login: 'g', candidate: 'kids/keyword-baseline@v1', env: 'staging', dry_run: true }, 'the plan is asked for, for the place it was in');
assert.ok(h.text().includes('Start keyword-baseline, version 1 again on Google Cloud? About 2 to 6 minutes. About $0.10 an hour while a copy is awake, and nothing while it sleeps.'), `start names the place, the time and the cost: ${h.text()}`);
assert.ok(!sent.some((x) => x[1].endsWith('/deploy') && x[2]?.confirm), 'nothing is started before the answer');
await h.click('dep-ask-yes');
await h.idle(60);
assert.deepEqual(sent.filter((x) => x[0] === 'POST' && x[1] === '/api/projects/kids/deploy').pop()?.[2], { host: 'gcp', login: 'g', candidate: 'kids/keyword-baseline@v1', env: 'staging', confirm: true }, 'the same version, the same place');
// When that place no longer has what the first deploy set up, the question says it will be set up again.
world.plan = { host: 'gcp', label: 'Google Cloud', first_time: true, needs_confirm: true, minutes: { text: 'About 5 to 12 minutes.' }, cost: { text: 'About $0.10 an hour while a copy is awake.' } };
await h.click(`dep-start-${OLD.id}`);
await h.idle(80);
assert.ok(h.text().includes('Start keyword-baseline, version 1 again on Google Cloud? This will set up Google Cloud again. About 5 to 12 minutes. About $0.10 an hour while a copy is awake.'), `it says the cloud will be set up again: ${h.text()}`);
assert.ok(h.text().includes('Set up Google Cloud and start it'), 'and the button says so');
await h.click('dep-ask-no');
// When it can't be planned for that place, nothing is asked and nothing is started anywhere else. The reason is shown.
world.plan = null; world.planFails = 'That place is not connected in Captain any more. Connect its cloud login again, or deploy somewhere else.';
const before = sent.length;
await h.click(`dep-start-${OLD.id}`);
await h.idle(80);
assert.ok(h.text().includes('That place is not connected in Captain any more.') && byId(h, 'dep-ask').length === 0, `the reason, and no question: ${h.text()}`);
assert.ok(!sent.slice(before).some((x) => x[2]?.confirm || x[2]?.host === 'local'), 'nothing is started, and never on this computer');
world.planFails = null;
// "Deploy a model" with several projects asks which, and goes to that project.
await h.click('dep-go-projects');
await h.idle(80);
assert.ok(h.text().includes("Which project's model do you want to deploy?") && byId(h, 'dep-deploy-in-shop').length === 1 && byId(h, 'dep-deploy-in-trail').length === 1, `asks which project: ${h.text().slice(0, 300)}`);
await h.click('dep-deploy-in-trail');
assert.deepEqual(globalThis.__shared['captain.scope'], { kind: 'project', id: 'trail', label: 'Trail gear' }, 'and goes to it');
assert.equal(byId(h, 'dep-ask').length, 0, 'the question closes');
// The account line under it, opened: each thing named for what it is.
await h.click('dep-setup-show-aws');
t = h.text();
assert.ok(t.includes('Build package') && t.includes('serving.zip') && t.includes('Build log') && !t.includes('| Made by the first deploy |') && !t.includes('s3://'), `each thing is named for what it is: ${t}`);
assert.ok(t.includes('us-west-1') && !/us-west-1, aws\b/.test(t), 'a login named for its cloud is not repeated');

// ---- one running ----
h = await open({ deployed: { rows: [SHOP], asked, text: '1 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
t = h.text();
assert.ok(t.includes('Demo shop') && t.includes('starter-ranker, version 2') && !t.includes('shop/starter-ranker@v2') && t.includes('Running on 1 copy.'), `the model without its project said twice, and its state in words: ${t}`);
assert.ok(t.includes('Deployed by dana, 2 h ago') && !t.includes('@example.com'), 'a person by the name before the @');
// Where it lives, on the row: the address a site calls, to copy, and one line about the system it runs on.
assert.equal(byId(h, 'dep-address-shop:staging').length, 1);
assert.ok(t.includes('https://runtime.sagemaker.us-west-1.amazonaws.com/endpoints/captain-shop/invocations') && byId(h, 'dep-copy-shop:staging').length === 1, `the address, with Copy: ${t}`);
assert.ok(t.includes('AWS, account ending 1234. us-west-1. ml.t2.medium: 2 virtual CPUs, 4 GB memory. 1 copy up now, may run 1 to 3. Deployed by dana, 2 h ago.'), `the system in one line: ${t}`);
assert.ok(t.includes('8,640 requests in 24 hours, none failed, typically 3.1 ms. $0.07 an hour, about $14.30 this month.'), `its traffic and cost: ${t}`);
await h.click('dep-copy-shop:staging');
assert.ok(h.text().includes('Copied'), 'Copy says it copied');
assert.equal(byId(h, 'dep-spark-shop:staging').length, 1, 'a small chart of the last hour');
for (const id of ['dep-open-shop:staging', 'dep-stop-shop:staging', 'dep-back-shop:staging']) assert.equal(byId(h, id).length, 1, `a running model has ${id}`);
assert.ok(t.includes('Go back to an earlier version') && !t.includes('Start again') && !t.includes('Remove from the cloud'), 'a running model is stopped before it is removed');
assert.ok(sent.some((x) => x[1].startsWith('/api/projects/shop/deployed/numbers?range=24h')), 'numbers are read per row');
await h.click('dep-stop-shop:staging');
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'POST' && x[1] === '/api/projects/shop/unstage'), 'Stop stops it');
assert.ok(sent.filter((x) => x[1] === '/api/deployed?fresh=true').length >= 1, 'and the clouds are asked again afterwards');

// ---- several across clouds, one failed, one found in the cloud ----
const created = { places: [{ cloud: 'aws', cloud_label: 'AWS', login: 'work-aws', region: 'us-west-1', in_use_by: ['Demo shop'], can_remove: false, text: '',
  things: [{ kind: 'role', name: 'captain-serving', what_for: 'What a running model is allowed to do: read its own files and write its request log. Nothing else.' },
    { kind: 'ecr-repository', name: 'captain-serving', what_for: "Holds the model's image in your account." }] },
{ cloud: 'gcp', cloud_label: 'Google Cloud', login: 'g', region: 'us-central1', in_use_by: [], can_remove: true, text: '',
  things: [{ kind: 'bucket', name: 'captain-serving-demo', what_for: "Holds the model's files and its request log." }] }], text: '' };
h = await open({ deployed: { rows: [BAD, SHOP, TRAIL, FOUND, OLD], asked, text: '2 models running, 1 failed, 1 found in your cloud with no project.', stopped_earlier: 1, read_at: '' }, created,
  numbers: { shop: numbers(8640), trail: numbers(120, { source: 'log', not_read: ['Modal keeps no history.'] }) } });
t = h.text();
assert.ok(t.includes('2 models running, 1 failed, 1 found in your cloud with no project.'), 'the title line from the services');
// The list is what is in a cloud now: running, asleep and failed. The one stopped earlier is under its own line.
assert.equal(shown(h), 'shop trail gifts', 'what is in a cloud');
assert.ok(t.includes('Asleep. It wakes on the next request and bills nothing until then.') && t.includes('Failed: The primary container did not pass the ping health check.'), `asleep and failed are states of something that exists: ${shown(h)}`);
assert.ok(t.includes("Captain's record said it was running. The cloud says it failed, so the record now says failed."), 'what was corrected is said on the row');
assert.ok(t.includes('captain-old-experiment-staging') && t.includes('Found in your AWS account, not linked to a project.'), 'something no project knows is listed');
assert.ok(t.includes('Link to a project') && byId(h, `dep-remove-${FOUND.id}`).length === 1, 'and can be linked or removed');
assert.ok(t.includes('Stopped earlier: 1 model.') && byId(h, `dep-start-${OLD.id}`).length === 0, 'the stopped one is behind its line');
// Something in a cloud that failed: it can be opened and removed from the cloud. Not started "again": it is there.
assert.ok(byId(h, `dep-open-${BAD.id}`).length === 1 && byId(h, `dep-remove-${BAD.id}`).length === 1 && byId(h, `dep-start-${BAD.id}`).length === 0, 'a failed one in the cloud');
// Two pickers at the right, and no chips for the state. A picker's choices show only once it is opened.
assert.ok(byId(h, 'dep-pick-cloud').length === 1 && byId(h, 'dep-pick-project').length === 1 && byId(h, 'dep-cloud-aws').length === 0 && byId(h, 'dep-show-all').length === 0, 'two pickers, closed');
assert.ok(h.text().includes('Every cloud') && h.text().includes('Every project'), 'each says what it shows now');
await h.click('dep-pick-cloud');
assert.ok(byId(h, 'dep-cloud-aws').length === 1 && byId(h, 'dep-cloud-modal').length === 1 && byId(h, 'dep-project-shop').length === 0, 'the cloud picker lists the clouds');
await h.click('dep-cloud-modal');
assert.equal(shown(h), 'trail', 'one cloud at a time');
assert.ok(byId(h, 'dep-cloud-aws').length === 0 && h.text().includes('Modal'), 'the picker closes and says what was picked');
await h.click('dep-pick-cloud');
await h.click('dep-cloud-all');
await h.click('dep-pick-project');
await h.click('dep-project-shop');
assert.equal(shown(h), 'shop', 'one project at a time');
await h.click('dep-pick-project');
await h.click('dep-project-all');

// A removal from the cloud asks first, and the question holds the only filled button on the screen.
await h.click(`dep-remove-${BAD.id}`);
t = h.text();
assert.ok(t.includes("Remove Gift finder's model from AWS? It stops answering, and what Captain made for it there is deleted. It cannot be undone."), `the question: ${t}`);
assert.equal(byId(h, 'dep-ask-yes').length, 1);
assert.ok(!sent.some((x) => x[0] === 'DELETE'), 'nothing is removed before the answer');
await h.click('dep-ask-yes');
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'DELETE' && x[1].startsWith('/api/projects/gifts/deploy')), 'then it is removed');
// Linking picks a project.
await h.click(`dep-link-${FOUND.id}`);
await h.idle(60);
assert.ok(h.text().includes('Which project should captain-old-experiment-staging belong to?') && byId(h, 'dep-link-trail').length === 1, 'pick a project');
await h.click('dep-link-trail');
await h.idle(60);
assert.deepEqual(sent.find((x) => x[1] === '/api/deployed/link')?.[2], { id: FOUND.id, project: 'trail' });

// What Captain created in each account: one line each, opened to see what each thing is for.
await h.click('dep-setup-show-aws');
await h.click('dep-setup-show-gcp');
t = h.text();
assert.ok(t.includes('Set up in your AWS account: 2 things') && t.includes('Set up in your Google Cloud account: 1 thing Captain created') && t.includes("Holds the model's image in your account.") && t.includes('In use by Demo shop. Stop that model first to remove these.'), `the setup lines: ${t}`);
assert.equal(byId(h, 'dep-setup-remove-aws').length, 0, 'in use: no removal offered');
assert.equal(byId(h, 'dep-setup-remove-gcp').length, 1, 'not in use: Remove everything');
await h.click('dep-setup-remove-gcp');
assert.ok(h.text().includes('Remove everything Captain created in your Google Cloud account?'), 'asks first');
await h.click('dep-ask-yes');
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'DELETE' && x[1] === '/api/serving/setup?cloud=gcp'));

// A cloud that could not be asked is said, in the failing colour, above the list.
h = await open({ deployed: { rows: [row({ ...SHOP, state: 'unknown', link: 'unknown', state_text: "Captain couldn't ask AWS just now: the sign-in has expired" })],
  asked: [{ ...asked[0], ok: false, error: 'the sign-in has expired' }], text: '1 deployed. Could not ask AWS just now.', read_at: '' } });
t = h.text();
assert.ok(t.includes('AWS could not be reached. Trying again.') && !t.includes('Updated just now'), `the line by the title says so, in words: ${t}`);
assert.ok(t.includes("AWS (work-aws): the sign-in has expired. What is shown for it is Captain's own record."), `and the reason, once: ${t}`);
assert.ok(byId(h, 'dep-stop-shop:staging').length === 0, 'nothing is done to a deployment Captain could not check');

// ---- a model's own page ----
h = await open({ deployed: { rows: [SHOP], asked, text: '1 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
await h.click('dep-open-shop:staging');
await h.idle(150);
t = h.text();
assert.ok(t.includes('All deployments') && t.includes('Demo shop') && t.includes('Running on 1 copy.'), `the page: ${t}`);
// The page keeps itself current too: when it was read, and the same small refresh icon. No button to press.
assert.ok(t.includes('Updated just now') && byId(h, 'model-updated-refresh')[0]?.call('accessibility_label')?.[0] === 'Refresh', `the page says when it was read: ${t.slice(0, 200)}`);
// "Where it runs" is first on the page: a small table, each value saying whether it was read from the cloud.
assert.ok(sent.some((x) => x[1].startsWith('/api/projects/shop/deployed/where')), 'it is read from the services');
assert.ok(t.indexOf('Where it runs') < t.indexOf('Traffic and cost'), 'before the numbers');
for (const label of ['Address', 'How to call it', 'Cloud and account', 'Region', 'Machine', 'Copies', 'Image', 'Model files', 'Role it runs as', 'Logs', 'Stops by itself after', 'Deployed by', 'Version'])
  assert.ok(t.includes(label), `Where it runs has ${label}`);
assert.ok(t.includes('AWS, account ending 1234') && t.includes('ml.t2.medium: 2 virtual CPUs, 4 GB memory, about $0.07 an hour for each copy') && t.includes('1 up now. Fewest 1, most 3'), `the system: ${t}`);
assert.ok(t.includes('From AWS') && t.includes("From Captain's record") && t.includes('Read from AWS just now.'), 'each value says where it came from');
// What a person copies has Copy: the address, a test request, the log's name. A request of several lines shows its first.
for (const id of ['address', 'call', 'logs']) assert.equal(byId(h, `model-where-${id}-copy`).length, 1, `${id} can be copied`);
assert.equal(byId(h, 'model-where-region-copy').length, 0, 'a region is read, not copied');
assert.ok(t.includes('aws sagemaker-runtime invoke-endpoint --region us-west-1 --endpoint-name captain-shop') && !t.includes('# Get a ranking.'), 'the request by its first real line');
await h.click('model-where-call-copy');
assert.ok(h.text().includes('Copied'), 'Copy says it copied');
for (const label of ['15 minutes', '1 hour', '24 hours', '7 days', 'Requests a minute', 'Requests that failed', 'Response time', 'Processor in use', 'Memory in use'])
  assert.ok(t.includes(label), `the model page has ${label}`);
// Copies are said in words, from the cloud's count now and its settings. No chart: no history of them is kept.
assert.ok(t.includes('1 copy up now. It may run 1 to 3.') && byId(h, 'model-chart-copies').length === 0 && !t.includes('Copies running'), `copies in words: ${t}`);
// Two submenus and no more, drawn with the kit's control.
assert.ok(byId(h, 'submenu-traffic').length === 1 && byId(h, 'submenu-details').length === 1 && h.byIdPrefix('submenu-').filter((e) => /^submenu-[a-z]+$/.test(e.call('id')?.[0] ?? '')).length === 2, 'two submenus');
assert.ok(t.includes('Traffic and cost') && t.includes('Details'), 'named for what is in them');
// What it costs is the first thing on the page, as three figures.
assert.ok(byId(h, 'model-cost-hour').length === 1 && t.includes('$0.07') && t.includes('an hour now') && t.includes('$0.92') && t.includes('today') && t.includes('$14.30') && t.includes('this month'), `cost first: ${t}`);
assert.ok(t.indexOf('an hour now') < t.indexOf('Requests a minute'), 'cost is above the charts');
assert.ok(t.includes('8,640 requests in the last 24 hours, none failed. A request typically takes 3.1 ms.') && !t.includes('95th percentile') && !t.includes('Median'), `no jargon in the headline: ${t}`);
assert.ok(t.includes('Typically 3.1 ms, 19 in 20 within 9.4 ms') && t.includes("From AWS's own metrics"), `numbers in words: ${t}`);
assert.equal(byId(h, 'model-charts').length, 1, 'the charts share rows');
await h.click('model-range-7d');
await h.idle(100);
assert.ok(sent.some((x) => x[1].startsWith('/api/projects/shop/deployed/numbers?range=7d')), 'a time range asks for that range');
// Details: four sections on one page, each under its heading.
await h.click('submenu-details');
t = h.text();
for (const heading of ['Earlier versions', 'Requests it answered', 'Its log', 'Copies and machine']) assert.ok(t.includes(heading), `Details has ${heading}`);
assert.ok(t.includes('Version 2') && t.includes('Running now') && t.includes('Put this version back') && t.includes('trail running shoes') && t.includes('Loaded 480 products.') && t.includes('Keeps 1 up at all times.'), `all on one page: ${t}`);
// Putting a version back asks why, and sends the reason: a roll back is recorded with it.
await h.click('model-back-1');
assert.ok(h.text().includes('Why? It is kept with the record of the change.') && byId(h, 'model-back-why-1').length === 1, 'asks why');
for (const why of ['It gives worse results', 'It is too slow', 'It is failing requests', 'It was deployed by mistake', 'Another reason']) assert.ok(h.text().includes(why), `offers "${why}"`);
assert.ok(!sent.some((x) => x[1].endsWith('/rollback')), 'nothing is put back before a reason is given');
await h.click('model-back-why-1');
await h.idle(60);
assert.deepEqual(sent.find((x) => x[0] === 'POST' && x[1] === '/api/projects/shop/staging/versions/1/rollback')?.[2], { reason: 'It is too slow' }, 'the reason goes with it');
assert.equal(h.byIdPrefix('model-back-why-').length, 0, 'the question closes once answered');
await h.click('model-close');
assert.ok(byId(h, 'dep-updated-refresh').length === 1, 'back to the list');
// "Go back to an earlier version" on a row opens the page at Details, where the versions are first.
await h.click('dep-back-shop:staging');
await h.idle(120);
assert.ok(h.text().includes('Put this version back') && h.text().indexOf('Earlier versions') < h.text().indexOf('Requests it answered'), 'straight to the versions');

// What a real endpoint looked like on 2026-11-12: AWS had every request number and had published no processor or
// memory numbers at all. Those two charts are left out and a sentence says why. Nothing is drawn empty.
h = await open({ deployed: { rows: [SHOP], asked, text: '1 model running.', read_at: '' }, numbers: { shop: numbers(8640, {
  buckets: Array.from({ length: 288 }, (_, i) => ({ t: String(i), requests: 30, errors: 0, p50_ms: 3, p95_ms: 9, cpu: null, memory: null, copies: null })),
  not_read: ['AWS has not published processor and memory numbers for this endpoint.'] }) } });
await h.click('dep-open-shop:staging');
await h.idle(150);
t = h.text();
assert.ok(byId(h, 'model-chart-requests').length === 1 && byId(h, 'model-chart-time').length === 1, 'requests and response time are drawn');
assert.ok(byId(h, 'model-chart-cpu').length === 0 && byId(h, 'model-chart-memory').length === 0 && !t.includes('Processor in use') && !t.includes('Memory in use'), 'no empty processor or memory chart');
assert.ok(t.includes('AWS has not published processor and memory numbers for this endpoint.'), `and it says why: ${t}`);

// Another screen can ask for one model's page ("Open in Deployments" on a project's page).
globalThis.__props = { open: 'shop:staging' };
h = await open({ deployed: { rows: [SHOP], asked, text: '1 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
await h.idle(150);
assert.ok(h.text().includes('All deployments') && byId(h, 'submenu-traffic').length === 1, 'opens that model\'s page');
globalThis.__props = undefined;
// The way a link on another screen really asks: the shared "open" value, then this place is selected. Handled once.
h = await open({ deployed: { rows: [SHOP, TRAIL], asked, text: '2 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
assert.ok(byId(h, 'dep-updated-refresh').length === 1, 'the list first');
globalThis.__shared['captain.open'] = { section: 'deployments', open: 'trail:staging', at: 1001 };
await tick(h);
assert.ok(h.text().includes('All deployments') && h.text().includes('Trail gear') && byId(h, 'submenu-traffic').length === 1, `the shared value opens that model's page: ${h.text().slice(0, 200)}`);
await h.click('model-close');
await tick(h);
assert.ok(byId(h, 'dep-updated-refresh').length === 1, 'closing it stays closed: the same request is not handled twice');
globalThis.__shared['captain.open'] = { section: 'runs', open: 'shop:staging', at: 1002 };
await tick(h);
assert.ok(byId(h, 'dep-updated-refresh').length === 1, 'a request for another screen is not this screen\'s');
globalThis.__shared['captain.open'] = { section: 'deployments', open: 'shop:staging', at: 1003 };
await tick(h);
assert.ok(h.text().includes('All deployments') && h.text().includes('Demo shop'), 'a newer request opens the other model');
globalThis.__shared['captain.open'] = { section: 'deployments', at: 1004 };
await tick(h);
assert.ok(byId(h, 'dep-updated-refresh').length === 1 && byId(h, 'submenu-traffic').length === 0, '"All deployments" with no model shows the list');
// The sidebar's rule: clicking Deployments always opens the list, every time, even on the entry already shown, and a
// model's page with whatever was open on it is forgotten. The sidebar sends {section, home: true, at}.
await h.click('dep-open-shop:staging');
await h.idle(120);
await h.click('submenu-details');
assert.ok(byId(h, 'submenu-details').length === 1 && h.text().includes('Earlier versions'), 'on a model\'s page, in Details');
globalThis.__shared['captain.open'] = { section: 'deployments', home: true, at: 2001 };
await tick(h);
assert.ok(byId(h, 'dep-updated-refresh').length === 1 && byId(h, 'submenu-details').length === 0 && h.mod.page === null, 'home: the list, and the page is forgotten');
// Opening the same model again starts on its first part, not where the last look left off.
await h.click('dep-open-shop:staging');
await h.idle(120);
assert.ok(h.text().includes('Requests a minute') && !h.text().includes('Put this version back'), 'a fresh look at the page');
// A second click on the entry already shown goes home again.
globalThis.__shared['captain.open'] = { section: 'deployments', home: true, at: 2002 };
await tick(h);
assert.equal(h.mod.page, null, 'home again');
// Home wins over a model asked for a moment before, and drops a question or a picker left open.
h.mod.wanted = 'trail:staging';
await h.click('dep-pick-cloud');
globalThis.__shared['captain.open'] = { section: 'deployments', home: true, open: 'trail:staging', at: 2003 };
await tick(h);
assert.ok(h.mod.page === null && h.mod.wanted === null && h.mod.menu === null && byId(h, 'dep-cloud-aws').length === 0, 'home wins, and nothing is left open');
delete globalThis.__shared['captain.open'];

// ---- live: the screen shows the truth without a click ----
h = await open({ deployed: { rows: [SHOP], asked, text: '1 model running.', read_at: '' }, numbers: { shop: numbers(8640) } });
// No "Ask the clouds again" button. A quiet line says when it was read, with a small refresh icon named for a
// screen reader and on hover.
assert.ok(h.text().includes('Updated just now') && !h.text().includes('Ask the clouds again') && byId(h, 'dep-refresh').length === 0, `it says when it was read: ${h.text().slice(0, 200)}`);
const icon = byId(h, 'dep-updated-refresh')[0];
assert.ok(icon && icon.call('accessibility_label')?.[0] === 'Refresh' && icon.call('tooltip')?.[0] === 'Refresh', 'the refresh icon is named Refresh');
const asks = sent.filter((x) => x[1] === '/api/deployed?fresh=true').length;
await h.click('dep-updated-refresh');
await h.idle(80);
assert.equal(sent.filter((x) => x[1] === '/api/deployed?fresh=true').length, asks + 1, 'and it asks the clouds now');
// A model deployed from another screen or a terminal shows by itself: the change feed says what is deployed
// changed, and the list is read again. No timer of the screen's re-reads it.
await tick(h);  // the channel learns where the feed is
let reads = sent.filter((x) => x[1].startsWith('/api/deployed') && !x[1].includes('created')).length;
world.deployed = { rows: [SHOP, TRAIL], asked, text: '2 models running.', read_at: '' };
await tick(h);
assert.ok(sent.filter((x) => x[1].startsWith('/api/deployed') && !x[1].includes('created')).length > reads, 'the list is read again when the feed says it changed');
assert.ok(sent.some((x) => x[1].startsWith('/api/changes?topics=') && decodeURIComponent(x[1]).includes('deployed') && decodeURIComponent(x[1]).includes('jobs')), 'through the one live channel');
{
  const src = fs.readFileSync(path.join(dir, 'main.js'), 'utf8');
  const timers = [...src.matchAll(/cx\.timer\.every\((\d+), \(cx\) => ([^\n]*)/g)].map((m) => [Number(m[1]), m[2]]);
  assert.ok(!timers.some(([, body]) => /this\.load\(|this\.follow\(/.test(body)), `no timer re-reads the list or a job: ${JSON.stringify(timers)}`);
}
assert.ok(shown(h) === 'shop trail' && h.text().includes('2 models running.'), `the new model is there without a click: ${shown(h)}`);
// The numbers are not read again that soon: a cloud charges for each read of those.
const numberReads = () => sent.filter((x) => x[1].includes('/deployed/numbers')).length;
const beforeNumbers = numberReads();
await tick(h);
assert.equal(numberReads(), beforeNumbers, 'the numbers stay on their one-minute ration');
// ---- an action is followed on the screen's timer, and the list never stops being read ----
// A person pressed Stop on a running AWS model. The cloud removed it and Captain's record said stopped, but the
// screen said "Stopping: stopping the deployed model of <id>..." for good, with "Updated 3 min ago". The follower
// waited between looks with a timer the app's script engine does not have, died after its first look, and the
// list read was switched off while an action ran.
const { readFileSync, readdirSync } = await import('node:fs');
for (const f of readdirSync('defaults/mods/captain-deployments').filter((x) => x.endsWith('.js')))
  assert.ok(!/\bset(Timeout|Interval)\s*\(/.test(readFileSync(`defaults/mods/captain-deployments/${f}`, 'utf8')), `${f} waits on a timer the app does not have`);
const listReads = () => sent.filter((x) => x[1].startsWith('/api/deployed') && !x[1].includes('created')).length;
const STOPPED = { rows: [{ ...SHOP, state: 'stopped', state_text: 'Stopped by dana, just now.', in_cloud: false, remembered_text: 'Nothing of it is running in AWS and nothing is billing. Captain remembers it so you can start it again.' }, TRAIL],
  asked, text: '1 model running.', stopped_earlier: 1, read_at: '' };
const RUNNING2 = { rows: [SHOP, TRAIL], asked, text: '2 models running.', read_at: '' };
// The job as the services really make it: its title is its first message and its first log line.
const TITLE = 'stopping the deployed model of shop';
world.stopJob = 'job-1';
world.jobs = { 'job-1': [{ status: 'running', message: TITLE, log: [TITLE] }, { status: 'running', message: TITLE, log: [TITLE, 'Removing the endpoint.'] },
  { status: 'done', message: 'done', log: [TITLE, 'Removing the endpoint.', 'done: {}'], result: { text: 'Stopped. It no longer answers or bills.' } }] };
await h.click('dep-stop-shop:staging');
await h.idle(100);
assert.ok(h.text().includes('Stopping on AWS...') && !h.text().includes(TITLE) && byId(h, 'dep-busy-shop:staging').length === 1, `it says what is being done, in words, not the job's title: ${h.text().slice(0, 500)}`);
for (const id of ['dep-stop-shop:staging', 'dep-open-shop:staging', 'dep-back-shop:staging']) assert.equal(byId(h, id).length, 0, `no ${id} while it runs`);
const readsAtStop = listReads();
await tick(h);
assert.ok(!h.text().includes(TITLE), `the job's title is never shown as a step: ${h.text().slice(0, 500)}`);
assert.ok(listReads() > readsAtStop, 'the list is still read while the action runs');
for (let i = 0; i < 4 && !h.text().includes('Removing the endpoint'); i++) await tick(h);
assert.ok(h.text().includes('Stopping on AWS: Removing the endpoint...') || h.text().includes('Stopped. It no longer answers or bills.'), `then the step it is on: ${h.text().slice(0, 500)}`);
world.deployed = STOPPED;
await tick(h); await tick(h);
assert.ok(h.text().includes('Stopped. It no longer answers or bills.') && byId(h, 'dep-busy-shop:staging').length === 0, `it says what was done: ${h.text().slice(0, 300)}`);
assert.ok(shown(h) === 'trail' && h.text().includes('Stopped earlier: 1 model.') && h.text().includes('1 model running.'), `and the list is the truth, without a click: ${shown(h)}`);
assert.ok(sent.some((x) => x[1] === '/api/deployed?fresh=true'), 'read fresh after the action');

// A job that is over before the first look at it.
world.deployed = RUNNING2; await tick(h); await tick(h);
assert.equal(shown(h), 'shop trail');
world.jobs = { 'job-1': [{ status: 'done', message: 'done', log: [TITLE, 'done: {}'], result: {} }] };
await h.click('dep-stop-shop:staging');
await h.idle(100);
world.deployed = STOPPED;
await tick(h); await tick(h);
assert.ok(byId(h, 'dep-busy-shop:staging').length === 0 && shown(h) === 'trail' && !h.text().includes('Stopping'), `a job that ended at once is seen: ${h.text().slice(0, 300)}`);

// The list is the truth: the cloud says the model is gone while the job still says running. The row is settled.
world.deployed = RUNNING2; await tick(h); await tick(h);
world.jobs = { 'job-1': [{ status: 'running', message: TITLE, log: [TITLE] }] };
await h.click('dep-stop-shop:staging');
await h.idle(100);
world.deployed = STOPPED;
await tick(h); await tick(h);
assert.ok(byId(h, 'dep-busy-shop:staging').length === 0 && shown(h) === 'trail' && h.text().includes('Stopped by dana, just now.'), `settled from the list, whatever the job says: ${h.text().slice(0, 400)}`);

// A job that can't be asked after: for two minutes the row waits, then it gives up and shows what the list says.
world.deployed = RUNNING2; await tick(h); await tick(h);
world.jobs = { 'job-1': null };
world.jobsFail = true;
await h.click('dep-stop-shop:staging');
await h.idle(100);
await tick(h);
assert.equal(byId(h, 'dep-busy-shop:staging').length, 1, 'not heard yet: it waits');
const realNow = Date.now;
Date.now = () => realNow() + 121000;
await tick(h); await tick(h);
Date.now = realNow;
await tick(h);
assert.ok(byId(h, 'dep-busy-shop:staging').length === 0 && h.text().includes('Captain could not tell how that ended. The list shows where it stands now.') && byId(h, 'dep-stop-shop:staging').length === 1,
  `given up on, with one line, and the row has its buttons back: ${h.text().slice(0, 400)}`);
world.jobsFail = false; world.stopJob = null; world.jobs = null;

// The screen never looks fine while it is stale. With the change feed out of reach it says "Reconnecting" where
// "Updated" was, re-reads on the two-second fallback, and goes back to a time when the feed answers again.
world.feedDown = true;
await tick(h); await tick(h);
assert.ok(h.text().includes('Reconnecting') && !/Updated (just now|\d+ (s|min) ago)/.test(h.text()), `it says so: ${h.text().slice(0, 200)}`);
world.feedDown = false;
await h.idle(700);  // the feed is tried again after a short wait
for (let i = 0; i < 6 && h.text().includes('Reconnecting'); i++) await tick(h);
assert.ok(!h.text().includes('Reconnecting') && h.text().includes('Updated just now'), `and recovers when the feed answers: ${h.text().slice(0, 200)}`);

// ---- requests come from the request log, and the cloud's chart is named for what it is ----
// Proven on a real endpoint: 29 requests for results, and 178 calls counted by the cloud, most of them Captain's own checks.
const CALLS = '178 calls reached the endpoint in the last hour. That counts Captain\'s own checks (asking the model whether it is well, and to write its log) as well as requests for results.';
h = await open({ deployed: { rows: [SHOP], asked, text: '1 model running.', read_at: '' }, numbers: { shop: numbers(29, {
  text: '29 requests in the last hour, no errors. Median 2.6 ms, 95th percentile 6.9 ms.',
  source_text: 'Requests, failures and response time are from Captain\'s request log, which holds only requests for results.' }) } });
world.numbers.shop.summary = { ...world.numbers.shop.summary, requests: 29, calls: 178, counted_from: 'log', calls_text: CALLS };
await h.click('dep-open-shop:staging');
await h.idle(150);
t = h.text();
assert.ok(t.includes('Calls a minute at the endpoint') && t.includes(CALLS) && !t.includes('Requests a minute'), `the cloud's chart is of calls, and says they include Captain's checks: ${t.slice(0, 900)}`);
assert.ok(t.includes('request log, which holds only requests for results'), 'where the count comes from');
// With no request log the cloud's count is the count, and the chart keeps its name.
h = await open({ deployed: { rows: [SHOP], asked, text: '1 model running.', read_at: '' }, numbers: { shop: numbers(8640) } });
world.numbers.shop.summary = { ...world.numbers.shop.summary, calls: 8640, counted_from: 'cloud', calls_text: 'x' };
await h.click('dep-open-shop:staging');
await h.idle(150);
assert.ok(h.text().includes('Requests a minute') && !h.text().includes('Calls a minute at the endpoint'), 'one count, one name');

// ---- only the refresh control asks the model to write its log ----
h = await open({ deployed: { rows: [SHOP], asked, text: '1 model running.', read_at: '' }, numbers: { shop: numbers(8640) } });
await h.click('dep-open-shop:staging');
await h.idle(150);
const logReads = () => sent.filter((x) => String(x[1]).includes('/staging/requests'));
assert.ok(logReads().length > 0 && !logReads().some((x) => String(x[1]).includes('refresh=true')), 'opening the page does not call the model');
await h.click('model-updated-refresh');
await h.idle(150);
assert.ok(logReads().some((x) => String(x[1]).includes('refresh=true')), 'the refresh control does');

// ---- a deploy under way is a row, and counts its own seconds ----
const started = new Date(Date.now() - 48000).toISOString();
const going = { label: 'AWS', project: 'gifts', env: 'staging', started_at: new Date(Date.now() - 52000).toISOString(),
  step: { id: 'image', title: "Building the model's image", started_at: started, typical_s: 92, typical: 'about a minute and a half' },
  steps: [{ id: 'check', title: 'Checking your account', status: 'done', seconds: 1 }, { id: 'access', title: 'Creating permissions', status: 'done', seconds: 2 },
    { id: 'image', title: "Building the model's image", status: 'running', typical: 'about a minute and a half', started_at: started },
    { id: 'start', title: 'Starting the model', status: 'pending', typical: 'about 2 minutes' }, { id: 'ready', title: 'Checking it answers', status: 'pending', typical: 'under a minute' }],
  last_line: 'Still building the image: 40 s. Building.' };
const DEPLOYING = row({ id: 'gifts:staging', project: 'gifts', project_name: 'Gift finder', model_version: 'gifts/starter-ranker@v3', model_label: 'starter-ranker, version 3', cloud: 'aws',
  cloud_label: 'AWS', region: null, machine: null, where: 'AWS', name: null, state: 'deploying', state_label: 'Deploying', state_text: 'stale text from the services', address: null, deploying: going });
h = await open({ deployed: { rows: [DEPLOYING], asked, text: 'No models running, 1 deploying.', read_at: '' } });
t = h.text();
assert.equal(shown(h), 'gifts', 'a deploy under way is in the list from the moment it is accepted');
assert.ok(t.includes('No models running, 1 deploying.') && !t.includes('No models are running.') && byId(h, 'dep-go-projects').length === 0, `the list is not empty while a model is being made: ${t}`);
assert.ok(/Deploying to AWS: Building the model's image, (4[89]|5\d) s so far \(usually about a minute and a half\)\./.test(t) && !t.includes('stale text'), `what is being done, how long so far, how long it usually takes, on the page's clock: ${t}`);
// Each step with its state: what is done and how long it took, what is running now, what comes next and how long it usually takes.
for (const part of ['Done', 'Checking your account', '1 s', 'Now', "Building the model's image", 'Next', 'Starting the model', 'usually about 2 minutes', 'Checking it answers'])
  assert.ok(t.includes(part), `the steps say ${part}`);
assert.ok(t.includes('Still building the image: 40 s. Building.') && t.includes('Started by dana'), 'the last thing the job said, and who started it');
// Nothing is offered that would act on a model still being made.
for (const id of ['dep-open-gifts:staging', 'dep-stop-gifts:staging', 'dep-remove-gifts:staging', 'dep-start-gifts:staging', 'dep-forget-gifts:staging']) assert.equal(byId(h, id).length, 0, `no ${id} while it deploys`);
// The seconds tick on the page's own clock, with no read from the services.
const readsBefore = sent.length;
const secondsOf = () => Number(/, (\d+) s so far/.exec(h.text())?.[1]);
const first = secondsOf();
await new Promise((r) => setTimeout(r, 1200));
await tick(h);
assert.ok(secondsOf() > first, `the time went on: ${first} then ${secondsOf()}`);
// When it finishes the row becomes the running model, on the next read, without a click.
world.deployed = { rows: [{ ...SHOP, id: 'gifts:staging', project: 'gifts', project_name: 'Gift finder' }], asked, text: '1 model running.', read_at: '' };
await tick(h);
assert.ok(h.text().includes('Running on 1 copy.') && h.text().includes('1 model running.') && byId(h, 'dep-step-gifts:staging-0').length === 0 && sent.length > readsBefore, `it became the running model: ${h.text().slice(0, 300)}`);
// A new version going onto a model that is running: the row stays, with its buttons, and says what is happening.
h = await open({ deployed: { rows: [{ ...SHOP, deploying: { ...going, label: 'AWS', step: { ...going.step, title: 'Starting the model', typical: 'about 2 minutes' } } }], asked, text: '1 model running.', read_at: '' }, numbers: { shop: numbers(8640) } });
assert.ok(/A new version is on its way\. Deploying to AWS: Starting the model, \d+ s so far \(usually about 2 minutes\)\./.test(h.text()) && byId(h, 'dep-stop-shop:staging').length === 1, `a redeploy says so on the running row: ${h.text().slice(0, 400)}`);

// ---- the screen rules ----
for (const f of ['main.js', 'model.js', 'words.js']) {
  const src = fs.readFileSync(path.join(dir, f), 'utf8');
  assert.ok(!/[←-⇿—]/.test(src.replace(/\/\/.*$/gm, '')), `${f}: no arrows or long dashes in what is shown`);
  assert.ok(!/kind: "quiet"/.test(src), `${f}: no blue link buttons that do things`);
}
const main = fs.readFileSync(path.join(dir, 'main.js'), 'utf8'), model = fs.readFileSync(path.join(dir, 'model.js'), 'utf8');
assert.equal((main.match(/font_family\(FONT\)/g) ?? []).length, 1, 'one typeface on the list, set once');
assert.equal((main.match(/font_family\(MONO\)/g) ?? []).length, 1, 'the code font on the list only for the address a person copies');
assert.equal((model.match(/font_family\(MONO\)/g) ?? []).length, 1, 'the code font on the page only for what a person copies');
assert.ok(!/model-part-/.test(model) && /Submenus\(c, PARTS/.test(model) && (model.match(/\{ id: "[a-z]+", label:/g) ?? []).length === 2, 'the model page declares two submenus');
assert.equal((main.match(/kind: "primary"/g) ?? []).length, 2, 'two filled buttons in the source, never both on screen: Deploy a model when nothing is answering, and an open question, which hides it');
assert.equal((model.match(/kind: "primary"/g) ?? []).length, 0, 'the model page has no filled button');
// A name in the cloud and a path in storage are shown as they are ("captain-shop-staging", ".../staging/shop/...").
// Captain's own words never say these.
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bpipeline/i, /\bendpoint config\b/i, /(^|[^-/_])\bstaging\b(?!\/)/i]) assert.ok(!bad.test(t), `no ${bad} on screen`);
console.log('deployments render test: ok');
process.exit(0);
