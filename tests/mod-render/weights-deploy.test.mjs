// "Deploy this version" on the Weights page, against a fake of the services' deploy route (POST
// /api/projects/{project}/deploy with a session): one press saves the session as a new version and deploys it to
// this computer; a line says where it stands, then the address and a link to its card on Deployments; a refusal
// is the services' own sentence; and the page hears the deploy finish on the change feed, not on a clock.
//   node --import ./tests/mod-render/stubs/register.mjs --test tests/mod-render/weights-deploy.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';
import { fakeFeed } from './fixtures/captain-weights/reference.mjs';

const { default: Weights } = await import('../../defaults/mods/captain-weights/main.js');
const { weightsTransport } = await import('../../defaults/mods/captain-weights/adapter.js');
const { localReset } = await import('../../defaults/mods/captain-weights/local.js');
const { deployWords, addressOf } = await import('../../defaults/mods/captain-weights/deploy.js');
const { starter, TINY } = await import('../../defaults/mods/captain-weights/starters.js');
const { liveTransport, liveReset } = await import('../../defaults/mods/captain-weights/kit/live.js');

const LEDGER = { raw_weights: true, weights_read: 0, sandbox: false, forward_allowed: 50, forward_used: 0, changed: 1, total: 13, weights_alone: false };
let feed, asked, state;
// The fake services: one network "tiny" in the project "shop", and a deploy that is running until told otherwise.
weightsTransport(async (path, opts) => {
  const method = opts?.method ?? (opts?.body !== undefined ? 'POST' : 'GET');
  asked.push([method, path, opts?.body]);
  const p = path.split('?')[0], net = starter(TINY);
  if (p === '/api/projects') return { ok: true, data: state.projects ?? [] };
  if (p === '/api/weights/models') return { ok: true, data: [{ id: 'tiny', name: 'tiny', sizes: [2, 3, 1], project: state.project }] };
  if (p === '/api/weights/models/tiny/session') return { ok: true, data: { id: 'ws-1', model: 'tiny', ledger: LEDGER, events: [], saved: state.deploy === 'none' ? [] : ['tiny@v2'] } };
  if (p === '/api/weights/models/tiny') return { ok: true, data: { ...net, id: 'tiny', name: 'tiny', test: [] } };
  if (p === '/api/projects/shop/deploy' && method === 'POST') {
    if (state.refuse) return { ok: false, status: 400, error: state.refuse };
    state.deploy = 'running';
    return { ok: true, data: { job: 'j1', run: 'r1', plan: { host: 'local', label: 'This computer' }, saved: { ref: 'tiny@v2', base: 'tiny@v1', changed: 1, text: 'Saved tiny@v2.' }, session: 'ws-1' } };
  }
  if (p === '/api/projects/shop/deploy') {
    if (state.deploy === 'running') return { ok: true, data: { project: 'shop', env: 'staging', status: 'running', steps: [{ id: 'check', title: 'Checking the model', status: 'done' }, { id: 'start', title: 'Starting it', status: 'running' }] } };
    if (state.deploy === 'failed') return { ok: true, data: { project: 'shop', env: 'staging', status: 'failed', error: 'The model did not answer its first request.' } };
    return { ok: true, data: { project: 'shop', env: 'staging', status: 'done', staged: { endpoint: { url: 'http://127.0.0.1:8123' } } } };
  }
  return { ok: false, error: 'Not Found' };
});
async function page(model = 'tiny') {
  localReset(); liveReset(); asked = []; state = { project: 'shop', deploy: 'none', refuse: null };
  feed = fakeFeed(liveTransport);
  globalThis.__props = { model }; globalThis.__shared = {}; globalThis.__runs = [];
  const h = harness(Weights);
  await h.start();
  return h;
}
const textOf = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')[0] === id).flatMap((e) => e.kids.filter((k) => typeof k === 'string')).join(' ');

test('one press saves the session and deploys it to this computer, and the line follows it to its address', async () => {
  const h = await page();
  assert.equal(h.byIdPrefix('wt-deploy-go').length, 1, 'one outlined button in the header');
  assert.equal(h.byIdPrefix('wt-deploy').filter((e) => e.call('id')[0] === 'wt-deploy').length, 0, 'no line until something is deployed');
  await h.click('wt-deploy-go');
  await h.idle(30);
  const sent = asked.find(([m, p]) => m === 'POST' && p === '/api/projects/shop/deploy');
  assert.deepEqual(sent[2], { session: 'ws-1', host: 'local', by: 'you' }, 'the session and this computer, in the services\' shape');
  assert.equal(textOf(h, 'wt-deploy-text'), 'Saved as tiny, version 2, and deploying it to this computer. Now: Starting it.');
  assert.ok(!h.byIdPrefix('wt-deploy-go')[0].call('on_click'), 'it cannot be pressed again while it deploys');
  // Nothing more is asked until the feed says the deployment changed.
  const before = asked.length;
  await h.idle(400);
  assert.equal(asked.filter(([m, p]) => p === '/api/projects/shop/deploy').length, asked.slice(0, before).filter(([m, p]) => p === '/api/projects/shop/deploy').length, 'no asking on a clock');
  state.deploy = 'done';
  feed.tell('deployment:shop');
  await h.layoutLists({});
  await h.idle(30);
  assert.equal(textOf(h, 'wt-deploy-text'), 'tiny, version 2 is deployed on this computer, at');
  assert.equal(textOf(h, 'wt-deploy-address'), 'http://127.0.0.1:8123');
  // The link goes to its card on Deployments.
  await h.click('wt-deploy-open');
  assert.deepEqual([globalThis.__shared['captain.open'].section, globalThis.__shared['captain.open'].open], ['deployments', 'shop:staging']);
  assert.deepEqual(globalThis.__runs.at(-1), ['layout.select', { stack: 'sections', child: 'deployments' }]);
  assert.ok(h.byIdPrefix('wt-deploy-go')[0].call('on_click'), 'and it can be deployed again');
  await h.click('wt-deploy-close');
  assert.equal(h.byIdPrefix('wt-deploy-text').length, 0);
});

test('a refusal is the services\' own sentence, and a deploy that fails says why', async () => {
  let h = await page();
  state.refuse = 'This model will not fit on this computer: it needs 9 GB and 4 GB is free.';
  await h.click('wt-deploy-go');
  await h.idle(30);
  assert.equal(textOf(h, 'wt-deploy-text'), 'This model will not fit on this computer: it needs 9 GB and 4 GB is free.');
  assert.equal(h.byIdPrefix('wt-deploy-open').length, 0, 'nothing was deployed: no link');
  // A raw error is never shown.
  state.refuse = 'HTTP 500';
  await h.click('wt-deploy-go');
  await h.idle(30);
  assert.equal(textOf(h, 'wt-deploy-text'), "Captain couldn't start that deploy.");
  // A deploy that starts and then fails.
  h = await page();
  await h.click('wt-deploy-go');
  await h.idle(30);
  state.deploy = 'failed';
  feed.tell('deployed');
  await h.layoutLists({});
  await h.idle(30);
  assert.equal(textOf(h, 'wt-deploy-text'), 'The model did not answer its first request.');
  assert.equal(h.byIdPrefix('wt-deploy-open').length, 1, 'its card on Deployments has the rest');
});

test('a deployment belongs to a project: the network\'s own, else the one picked in the Explorer, else a plain ask', async () => {
  let h = await page();
  state.project = null;
  await h.click('wt-deploy-go');
  await h.idle(30);
  assert.equal(textOf(h, 'wt-deploy-text'), 'A deployment belongs to a project. Make a project first, then deploy again.');
  assert.ok(!asked.some(([m]) => m === 'POST'), 'nothing was sent');
  // Several projects and none picked: the page asks which, right there, and goes on with the answer.
  state.projects = [{ id: 'garden', name: 'Garden centre' }, { id: 'shop', name: 'Demo shop' }];
  await h.click('wt-deploy-go');
  await h.idle(30);
  assert.equal(textOf(h, 'wt-deploy-text'), 'Deploy into which project?');
  assert.deepEqual(h.byIdPrefix('wt-deploy-into-').map((e) => e.call('id')[0]), ['wt-deploy-into-garden', 'wt-deploy-into-shop']);
  assert.ok(!asked.some(([m]) => m === 'POST'), 'still nothing sent');
  await h.click('wt-deploy-into-shop');
  await h.idle(30);
  assert.ok(asked.some(([m, p]) => m === 'POST' && p === '/api/projects/shop/deploy'), 'the project chosen on the page');
  // One project: no question to ask.
  h = await page();
  asked.length = 0; state.deploy = 'none'; state.projects = [{ id: 'shop', name: 'Demo shop' }];
  await h.click('wt-deploy-go');
  await h.idle(30);
  assert.ok(asked.some(([m, p]) => m === 'POST' && p === '/api/projects/shop/deploy'), 'the only project');
  h = await page();
  asked.length = 0; state.deploy = 'none'; state.projects = [];
  globalThis.__shared['captain.scope'] = { kind: 'project', id: 'shop', label: 'Demo shop' };
  await h.click('wt-deploy-go');
  await h.idle(30);
  assert.ok(asked.some(([m, p]) => m === 'POST' && p === '/api/projects/shop/deploy'), 'the project picked in the Explorer');
});

test('the tiny network the page keeps itself cannot be deployed: it lives only while the page is open', async () => {
  const h = await page(TINY);
  assert.equal(h.byIdPrefix('wt-deploy-go').length, 0);
});

test('the words, and where the address is read from', () => {
  assert.equal(deployWords(null), null);
  assert.equal(deployWords({ state: 'asking' }).text, 'Saving this session as a new version and starting its deploy.');
  assert.equal(deployWords({ state: 'done', ref: 'digits@v3' }).text, 'digits, version 3 is deployed on this computer.');
  assert.equal(addressOf({ staged: { endpoint: { url: 'http://127.0.0.1:9' } } }), 'http://127.0.0.1:9');
  assert.equal(addressOf({ staged: {} }), null);
});
