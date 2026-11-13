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
let world, sent;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
const numbers = (requests, extra = {}) => ({ range: '24h', range_text: 'the last 24 hours', bucket_seconds: 300, source: 'cloud', source_text: "From AWS's own metrics (CloudWatch). They arrive a minute or two late.", not_read: [],
  buckets: Array.from({ length: 288 }, (_, i) => ({ t: String(i), requests: requests / 288, errors: 0, p50_ms: 3, p95_ms: 9, cpu: 20, memory: 5, copies: 1 })),
  summary: { requests, errors: 0, per_minute: requests / 1440, per_minute_peak: 12, p50_ms: 3.1, p95_ms: 9.4, cpu: 20, cpu_peak: 35, memory: 5, memory_peak: 6, copies_now: 1, copies_peak: 1 },
  cost: { hourly_usd: 0.066, rate_usd: 0.066, today_usd: 0.92, month_usd: 14.3, text: 'About $0.07 an hour for each copy while it is up. Your AWS bill is the final word.' },
  text: `${requests.toLocaleString('en-US')} requests in the last 24 hours, no errors. Median 3.1 ms, 95th percentile 9.4 ms at the endpoint.`, ...extra });
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url), p = u.pathname, m = opts.method ?? 'GET';
  sent.push([m, p + u.search, opts.body ? JSON.parse(opts.body) : null]);
  if (p === '/api/deployed') return reply(200, world.deployed);
  if (p === '/api/deployed/created') return reply(200, world.created ?? { places: [], text: '' });
  if (p === '/api/projects') return reply(200, { projects: [{ id: 'shop', name: 'Demo shop' }, { id: 'trail', name: 'Trail gear' }] });
  if (p.endsWith('/deployed/numbers')) return reply(200, world.numbers?.[p.split('/')[3]] ?? numbers(0, { source: 'none', text: 'No requests in the last 24 hours.', cost: null }));
  if (p.endsWith('/staging/versions')) return reply(200, { rows: [{ n: 2, model_version: 'shop/starter-ranker@v2', at: new Date().toISOString(), by: 'dana', live: true },
    { n: 1, model_version: 'shop/keyword-baseline@v1', at: new Date(Date.now() - 86400000).toISOString(), by: 'dana', live: false, can_roll_back: true }] });
  if (p.endsWith('/staging/requests')) return reply(200, { rows: [{ request_id: 'r1', ts: new Date().toISOString(), query: 'trail running shoes', model_version: 'shop/starter-ranker@v2', ms: 3.2 }], total: 1 });
  if (p.endsWith('/staging/logs')) return reply(200, { rows: [{ ts: '2026-11-12T10:00:00+00:00', text: 'Loaded 480 products.', level: 'info' }] });
  if (p.endsWith('/staging/scale')) return reply(200, { text: 'Keeps 1 up at all times. It never sleeps, and bills for each copy by the hour. 1 up now.' });
  return reply(200, { text: 'Done.' });
};
const row = (o) => ({ env: 'staging', login: 'work-aws', copies: { now: 1, min: null, max: null }, since: new Date(Date.now() - 7200000).toISOString(), by: 'dana', link: 'linked',
  corrected: [], sleeps: false, address: null, state_label: 'Running', can: [], ...o });
const SHOP = row({ id: 'shop:staging', project: 'shop', project_name: 'Demo shop', model_version: 'shop/starter-ranker@v2', cloud: 'aws', cloud_label: 'AWS', region: 'us-west-1',
  machine: 'ml.t2.medium', where: 'AWS, us-west-1, ml.t2.medium', name: 'captain-shop-staging', state: 'running', state_text: 'Running on 1 copy.' });
const TRAIL = row({ id: 'trail:staging', project: 'trail', project_name: 'Trail gear', model_version: 'trail/starter-ranker@v1', cloud: 'modal', cloud_label: 'Modal', login: 'modal', region: null,
  machine: '1 CPU, 2 GB', where: 'Modal, 1 CPU, 2 GB', name: 'captain-trail-staging', state: 'asleep', state_text: 'Asleep. It wakes on the next request and bills nothing until then.',
  sleeps: true, address: 'https://demo--captain-trail-staging.modal.run' });
const OLD = row({ id: 'kids:staging', project: 'kids', project_name: 'Kids range', model_version: 'kids/keyword-baseline@v1', cloud: 'gcp', cloud_label: 'Google Cloud', login: 'g', region: 'us-central1',
  machine: '1 vCPU, 2 GB', where: 'Google Cloud, us-central1, 1 vCPU, 2 GB', name: 'captain-kids-staging', state: 'stopped', state_text: 'Stopped: no requests for 240 minutes.', stopped_at: new Date(Date.now() - 3 * 86400000).toISOString() });
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
assert.ok(t.includes('Deployments') && t.includes('Nothing is deployed') && byId(h, 'dep-go-projects').length === 1 && t.includes('Deploy a model'), `empty: ${t}`);
assert.equal(byId(h, 'dep-filters').length, 0, 'no filters over an empty list');
assert.ok(!t.includes('What Captain created in your accounts'), 'no second section when nothing was created');

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
assert.ok(t.includes('Nothing is deployed') && t.includes('Deploy a model') && t.includes('What Captain created in your accounts'), `nothing deployed, but not an empty page: ${t}`);
for (let i = 0; i < 5; i++) assert.equal(byId(h, `dep-thing-aws-${i}`).length, 1, `thing ${i} of the five is listed`);
assert.ok(t.includes("Holds the model's files and its request log.") && byId(h, 'dep-setup-remove-aws').length === 1 && t.includes('Remove everything'), 'each with what it is for, and Remove everything');
await h.click('dep-setup-remove-aws');
assert.ok(h.text().includes('Remove everything Captain created in your AWS account?'), 'it asks first');
await h.click('dep-ask-yes');
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'DELETE' && x[1] === '/api/serving/setup?cloud=aws'), 'then removes it');

// One model, stopped, and what its first deploy left in the account (what a person saw on 2026-11-12, when the
// screen said only "Nothing is running. Stopped ones are under Stopped." and offered nothing to do).
h = await open({ deployed: { rows: [OLD], asked, text: '0 deployed, 1 not running.', read_at: '' }, created: { text: '', places: [{ cloud: 'aws', cloud_label: 'AWS', login: 'aws', login_shown: null,
  region: 'us-west-1', in_use_by: [], can_remove: true, text: '', things: [
    { kind: 's3-object', kind_label: 'Build package', name: 's3://captain-serving-000000000000-us-west-1/build/serving.zip', short: 'serving.zip', what_for: "The files the model's image is built from. Kept so the image can be built again without sending them." },
    { kind: 'log-group', kind_label: 'Build log', name: '/aws/codebuild/captain-serving-build', short: 'captain-serving-build', what_for: 'What the image build printed while it ran. Kept so a build that failed can be read.' }] }] } });
t = h.text();
assert.ok(t.includes('Nothing is running') && byId(h, 'dep-go-projects').length === 1 && t.includes('Deploy a model'), `nothing running: says so, and offers to deploy: ${t}`);
assert.equal(shown(h), 'kids', 'the stopped model is right there, not behind a filter');
assert.ok(byId(h, `dep-start-${OLD.id}`).length === 1 && byId(h, `dep-remove-${OLD.id}`).length === 1, 'with Start again and Remove from the cloud');
assert.ok(t.includes('Build package') && t.includes('serving.zip') && t.includes('Build log') && !t.includes('| Made by the first deploy |') && !t.includes('s3://'), `each thing is named for what it is: ${t}`);
assert.ok(t.includes('us-west-1') && !/us-west-1, aws\b/.test(t), 'a login named for its cloud is not repeated');
await h.click(`dep-start-${OLD.id}`);
assert.ok(byId(h, 'dep-ask-yes').length === 1 && byId(h, 'dep-go-projects').length === 0, 'an open question holds the one filled button');
await h.click('dep-ask-no');

// ---- one running ----
h = await open({ deployed: { rows: [SHOP], asked, text: '1 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
t = h.text();
assert.ok(t.includes('Demo shop') && t.includes('shop/starter-ranker@v2') && t.includes('Running on 1 copy.'), `the model, its version and its state in words: ${t}`);
assert.ok(t.includes('AWS, us-west-1, ml.t2.medium') && t.includes('8,640 requests in 24 hours, none failed, typically 3.1 ms'), `where, and its traffic: ${t}`);
assert.ok(t.includes('$0.07 an hour, $14.30 this month') && t.includes('Deployed by dana, 2 h ago'), `cost and who: ${t}`);
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
h = await open({ deployed: { rows: [BAD, SHOP, TRAIL, FOUND, OLD], asked, text: '3 deployed, 2 not running, 1 found in your cloud with no project.', read_at: '' }, created,
  numbers: { shop: numbers(8640), trail: numbers(120, { source: 'log', not_read: ['Modal keeps no history.'] }) } });
t = h.text();
assert.ok(t.includes('3 deployed, 2 not running, 1 found in your cloud with no project.'), 'the summary from the services');
// Every deployment is listed at first, stopped and failed ones too. Running narrows it to what is up.
assert.equal(shown(h), 'shop trail kids gifts', 'everything at first');
assert.equal(byId(h, 'dep-none-running').length, 0, 'something is running, so nothing says otherwise');
await h.click('dep-show-running');
t = h.text();
assert.ok(shown(h) === 'shop trail' && t.includes('Asleep. It wakes on the next request and bills nothing until then.'), `Running shows what is up: ${shown(h)}`);
assert.ok(t.includes('captain-old-experiment-staging') && t.includes('Found in your AWS account, not linked to a project.'), 'something no project knows is listed');
assert.ok(t.includes('Link to a project') && byId(h, `dep-remove-${FOUND.id}`).length === 1, 'and can be linked or removed');
// One row of filters: the state as chips, the cloud and the project as two pickers. A picker's choices show only
// once it is opened.
assert.ok(byId(h, 'dep-pick-cloud').length === 1 && byId(h, 'dep-pick-project').length === 1 && byId(h, 'dep-cloud-aws').length === 0, 'two pickers, closed');
assert.ok(h.text().includes('Every cloud') && h.text().includes('Every project'), 'each says what it shows now');
await h.click('dep-show-stopped');
t = h.text();
assert.ok(shown(h) === 'kids gifts' && t.includes('Failed: The primary container did not pass the ping health check.'), `Stopped: ${shown(h)}`);
assert.ok(t.includes("Captain's record said it was running. The cloud says it failed, so the record now says failed."), 'what was corrected is said on the row');
assert.ok(t.includes('Start again') && t.includes('Remove from the cloud') && t.includes('Stopped: no requests for 240 minutes.'), 'a stopped model can be started again or removed');
await h.click('dep-show-all');
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

// A removal asks first, and the question holds the only filled button on the screen.
await h.click(`dep-remove-${OLD.id}`);
t = h.text();
assert.ok(t.includes("Remove Kids range's model from Google Cloud? It stops answering, and what Captain made for it there is deleted. It cannot be undone."), `the question: ${t}`);
assert.equal(byId(h, 'dep-ask-yes').length, 1);
assert.ok(!sent.some((x) => x[0] === 'DELETE'), 'nothing is removed before the answer');
await h.click('dep-ask-yes');
await h.idle(60);
assert.ok(sent.some((x) => x[0] === 'DELETE' && x[1].startsWith('/api/projects/kids/deploy')), 'then it is removed');
// Start again says what it will cost, and sends the same version to the same place.
await h.click(`dep-start-${BAD.id}`);
assert.ok(h.text().includes('Start gifts/starter-ranker@v3 again on AWS, us-west-1, ml.t2.medium?'), 'start asks first');
await h.click('dep-ask-yes');
await h.idle(60);
const started = sent.find((x) => x[0] === 'POST' && x[1] === '/api/projects/gifts/deploy');
assert.deepEqual(started?.[2], { host: 'aws', login: 'work-aws', candidate: 'gifts/starter-ranker@v3', env: 'staging', confirm: true }, 'the same version, the same place');
// Linking picks a project.
await h.click(`dep-link-${FOUND.id}`);
await h.idle(60);
assert.ok(h.text().includes('Which project should captain-old-experiment-staging belong to?') && byId(h, 'dep-link-trail').length === 1, 'pick a project');
await h.click('dep-link-trail');
await h.idle(60);
assert.deepEqual(sent.find((x) => x[1] === '/api/deployed/link')?.[2], { id: FOUND.id, project: 'trail' });

// What Captain created in each account, with what each thing is for.
t = h.text();
assert.ok(t.includes('What Captain created in your accounts') && t.includes("Holds the model's image in your account.") && t.includes('In use by Demo shop. Stop that model first to remove these.'), `the setup section: ${t}`);
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
assert.ok(t.includes("Captain could not ask AWS (work-aws) just now: the sign-in has expired. What is shown for it is Captain's own record."), `not asked: ${t}`);
assert.ok(byId(h, 'dep-stop-shop:staging').length === 0, 'nothing is done to a deployment Captain could not check');

// ---- a model's own page ----
h = await open({ deployed: { rows: [SHOP], asked, text: '1 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
await h.click('dep-open-shop:staging');
await h.idle(150);
t = h.text();
assert.ok(t.includes('All deployments') && t.includes('Demo shop') && t.includes('Running on 1 copy.') && t.includes('Its name in the cloud') && t.includes('captain-shop-staging'), `the page: ${t}`);
for (const label of ['15 minutes', '1 hour', '24 hours', '7 days', 'Requests a minute', 'Requests that failed', 'Response time', 'Processor in use', 'Memory in use', 'Copies running'])
  assert.ok(t.includes(label), `the model page has ${label}`);
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
assert.ok(h.text().includes('Ask the clouds again'), 'back to the list');
// "Go back to an earlier version" on a row opens the page at Details, where the versions are first.
await h.click('dep-back-shop:staging');
await h.idle(120);
assert.ok(h.text().includes('Put this version back') && h.text().indexOf('Earlier versions') < h.text().indexOf('Requests it answered'), 'straight to the versions');

// Another screen can ask for one model's page ("Open in Deployments" on a project's page).
globalThis.__props = { open: 'shop:staging' };
h = await open({ deployed: { rows: [SHOP], asked, text: '1 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
await h.idle(150);
assert.ok(h.text().includes('All deployments') && byId(h, 'submenu-traffic').length === 1, 'opens that model\'s page');
globalThis.__props = undefined;
// The way a link on another screen really asks: the shared "open" value, then this place is selected. Handled once.
h = await open({ deployed: { rows: [SHOP, TRAIL], asked, text: '2 deployed.', read_at: '' }, numbers: { shop: numbers(8640) } });
assert.ok(h.text().includes('Ask the clouds again'), 'the list first');
globalThis.__shared['captain.open'] = { section: 'deployments', open: 'trail:staging', at: 1001 };
await tick(h);
assert.ok(h.text().includes('All deployments') && h.text().includes('Trail gear') && byId(h, 'submenu-traffic').length === 1, `the shared value opens that model's page: ${h.text().slice(0, 200)}`);
await h.click('model-close');
await tick(h);
assert.ok(h.text().includes('Ask the clouds again'), 'closing it stays closed: the same request is not handled twice');
globalThis.__shared['captain.open'] = { section: 'runs', open: 'shop:staging', at: 1002 };
await tick(h);
assert.ok(h.text().includes('Ask the clouds again'), 'a request for another screen is not this screen\'s');
globalThis.__shared['captain.open'] = { section: 'deployments', open: 'shop:staging', at: 1003 };
await tick(h);
assert.ok(h.text().includes('All deployments') && h.text().includes('Demo shop'), 'a newer request opens the other model');
globalThis.__shared['captain.open'] = { section: 'deployments', at: 1004 };
await tick(h);
assert.ok(h.text().includes('Ask the clouds again') && byId(h, 'submenu-traffic').length === 0, '"All deployments" with no model shows the list');
delete globalThis.__shared['captain.open'];

// ---- the screen rules ----
for (const f of ['main.js', 'model.js', 'words.js']) {
  const src = fs.readFileSync(path.join(dir, f), 'utf8');
  assert.ok(!/[←-⇿—]/.test(src.replace(/\/\/.*$/gm, '')), `${f}: no arrows or long dashes in what is shown`);
  assert.ok(!/kind: "quiet"/.test(src), `${f}: no blue link buttons that do things`);
}
const main = fs.readFileSync(path.join(dir, 'main.js'), 'utf8'), model = fs.readFileSync(path.join(dir, 'model.js'), 'utf8');
assert.equal((main.match(/font_family\(/g) ?? []).length, 1, 'one typeface on the list, set once');
assert.equal((model.match(/font_family\(MONO\)/g) ?? []).length, 1, 'the code font only for the address a person copies');
assert.ok(!/model-part-/.test(model) && /Submenus\(c, PARTS/.test(model) && (model.match(/\{ id: "[a-z]+", label:/g) ?? []).length === 2, 'the model page declares two submenus');
assert.equal((main.match(/kind: "primary"/g) ?? []).length, 3, 'three filled buttons in the source, never two on screen: nothing deployed, nothing running, and an open question (which hides the others)');
assert.equal((model.match(/kind: "primary"/g) ?? []).length, 0, 'the model page has no filled button');
// A name in the cloud is shown as the cloud has it ("captain-shop-staging"). Captain's own words never say these.
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bpipeline/i, /\bendpoint config\b/i, /(^|[^-])\bstaging\b/i]) assert.ok(!bad.test(t), `no ${bad} on screen`);
console.log('deployments render test: ok');
process.exit(0);
