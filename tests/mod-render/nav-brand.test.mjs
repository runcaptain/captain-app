// The sidebar mod is mounted twice: once as the sidebar and once as the small brand mark in the title bar. Only
// the sidebar shows the Explorer and the team's views. Both used to start them, so every read was made twice at
// every start: the Explorer's tree, the project folder's views, and MLflow's models, which took 5 to 11 seconds.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/nav-brand.test.mjs
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const asked = [];
globalThis.fetch = async (url) => {
  const p = new URL(url).pathname;
  asked.push(p);
  const data = p === '/api/workspace' ? { name: 'demo' } : p === '/api/mlflow' ? { configured: true, uri: 'https://mlflow.example.test' } : p === '/api/changes' ? { seq: 1, changed: [], reset: false } : [];
  return { status: 200, ok: true, text: async () => JSON.stringify(data) };
};
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://127.0.0.1:9', token: 't' }, 'captain.workspace': 'demo' };
globalThis.__nextCommand = () => new Promise(() => {});
const { default: Nav } = await import('../../defaults/mods/captain-nav/main.js');
const mount = async (props) => {
  asked.length = 0;
  globalThis.__props = props;
  const h = harness(Nav);
  await h.start();
  h.mod.explorer.fold = h.mod.explorer.fold ?? {};
  for (let i = 0; i < 4; i++) { await h.layoutLists({}); await h.idle(40); }
  return [...asked];
};
const isExplorers = (p) => p.startsWith('/api/mlflow') || p.startsWith('/api/extensions') || p.startsWith('/api/explorer') || p.startsWith('/api/scopes');

const brand = await mount({ part: 'brand' });
assert.deepEqual(brand.filter(isExplorers), [], `the brand mark reads nothing of the Explorer's, the team's views or MLflow: ${brand.filter(isExplorers)}`);
const side = await mount({});
assert.ok(side.some(isExplorers), `the sidebar itself does: ${[...new Set(side)].join(' ')}`);

// ---- the models are asked for once at a start, also when the live channel only connects a moment later -------------
// The channel calls its watchers when it reconnects. The MLflow list read again then, which asked the tracking
// server's models a second time at every start, one second after the first.
{
  let feedUp = false;
  const real = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const p = new URL(url).pathname;
    if (p === '/api/changes' && !feedUp) { asked.push(p); return { status: 503, ok: false, text: async () => '{}' }; }
    return real(url);
  };
  asked.length = 0;
  globalThis.__props = {};
  const h = harness(Nav);
  await h.start();
  h.mod.explorer.openMlflow(h.cx);                       // its heading was left open: read at the start
  for (let i = 0; i < 6; i++) { await h.layoutLists({}); await h.idle(60); }   // the channel is down: reconnecting
  feedUp = true;
  for (let i = 0; i < 30; i++) { await h.layoutLists({}); await h.idle(120); } // and now it connects
  const models = asked.filter((p) => p === '/api/mlflow/models').length;
  assert.equal(models, 1, `the registered models were asked for ${models} times at one start`);
  globalThis.fetch = real;
}
console.log('nav brand: ok');
process.exit(0);
