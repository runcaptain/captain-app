// The deployment page's Copies, Metrics, Logs, Settings and Cost tabs, the chart in its header, the live card that
// follows every tab, a request's own log lines, and the list of deployments, with a faked backend:
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/deployment-tabs.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-project');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };

const RUN = { status: 'running', text: 'shop/starter-ranker@v2 is running on this computer. Only this computer can reach it.',
  health: { stops_in_s: 14000, uptime_s: 400, running: 0, idle_s: 600 },
  staged: { id: 'stg-1', status: 'running', kind: 'local', target: 'local', serve: 'candidate', model_version: 'shop/starter-ranker@v2', needs_key: true,
    arms: { candidate: 'shop/starter-ranker@v2', baseline: 'shop/keyword-baseline@v1' }, endpoint: { url: 'http://127.0.0.1:5001' }, cost: { text: 'Runs on this computer.' }, idle_minutes: 240 } };
const bucket = (i, n, e = 0, site = null) => ({ at: `2026-11-11T16:${String(i).padStart(2, '0')}:00+00:00`, requests: n, errors: e, p50: n ? 0.2 : null, p95: n ? 0.6 : null, site_p50: site, site_p95: site ? site * 2 : null });
const metrics = (range, site) => ({ project: 'shop', range, range_text: range === '24h' ? 'the last 24 hours' : 'the last hour', ranges: ['15m', '1h', '24h', '7d'],
  start: '2026-11-11T15:10:00+00:00', end: '2026-11-11T16:10:00+00:00',
  buckets: Array.from({ length: 30 }, (_x, i) => bucket(i, i === 29 ? 40 : i === 12 ? 8 : 0, i === 12 ? 1 : 0, site && i === 29 ? 111 : null)),
  markers: [{ at: '2026-11-11T16:05:00+00:00', n: 1, model_version: 'shop/starter-ranker@v2', slot: 27, text: 'Version 1 staged: shop/starter-ranker@v2' }],
  summary: { requests: 48, errors: 1, error_rate: 0.0208, p50: 0.2, p95: 0.6, site_p50: site ? 111 : null, site_p95: site ? 201 : null, site_reported: site ? 40 : 0, copies: 1, copies_min: 0, copies_max: 1, memory_mb: 212.4, waiting: 0,
    endpoint_text: 'At the endpoint: median 0.2 ms, 95th percentile 0.6 ms.',
    site_text: site ? 'As your site sees it: median 111 ms, 95th percentile 201 ms, from 40 requests your site timed.'
      : 'As your site sees it: not reported yet. The network adds to the time at the endpoint, and only your site can measure it. Send client_ms with each outcome, as the Connect your site code does.' },
  text: `48 requests in ${range === '24h' ? 'the last 24 hours' : 'the last hour'}, 1 error. At the endpoint: median 0.2 ms, 95th percentile 0.6 ms.` });
const LINES = [
  { ts: '2026-11-11T16:05:49.231Z', text: 'Ready in 0.412 s: candidate is shop/starter-ranker@v2, baseline is shop/keyword-baseline@v1.', level: 'info' },
  { ts: '2026-11-11T16:06:02.118Z', text: "Ranked 'jacket' with the candidate in 2.7 ms: 10 results.", level: 'info', request_id: 'r1' },
  { ts: '2026-11-11T16:06:02.120Z', text: "Ranking 'tent' with the candidate failed: boom", level: 'error', request_id: 'r2' }];
let world, sent;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url, opts = {}) => {
  const u = new URL(url), p = u.pathname;
  const body = opts.body ? JSON.parse(opts.body) : null;
  sent.push([opts.method ?? 'GET', p + u.search, body]);
  if (p === '/api/compute') return reply(200, []);
  if (p === '/api/cloud/summary') return reply(200, { active: null });
  if (p === '/api/costs/estimate') return reply(200, { free: true, available: true, text: 'Runs on this computer. No cloud cost.', basis: [] });
  if (p === '/api/deployments') return reply(200, u.searchParams.get('which') === 'stopped'
    ? { which: 'stopped', text: '1 deployment.', rows: [{ project: 'old', name: 'Old shop', model_version: 'old/ranker@v1', state: 'stopped', status: 'Stopped', where: 'On aws-serving.', since_text: 'Stopped 2 h ago: stopped by maya', copies: 0, running: 0, requests_24h: 12, series: Array(30).fill(0), errors: Array(30).fill(0), line: 'Copies: 0 live. Requests: 0 running.' }] }
    : { which: 'live', text: '1 deployment.', rows: [{ project: 'shop', name: 'Shop', model_version: 'shop/starter-ranker@v2', state: 'idle', status: 'Idle', where: 'Running on this computer.', since_text: 'Staged by maya 6 min ago', copies: 1, running: 0, requests_24h: 48, series: Array.from({ length: 30 }, (_x, i) => (i === 29 ? 48 : 0)), errors: Array.from({ length: 30 }, (_x, i) => (i === 29 ? 1 : 0)), line: 'Copies: 1 live. Requests: 0 running.' }] });
  if (p.endsWith('/staging/live')) return reply(200, world.live);
  if (p.endsWith('/staging/metrics')) return reply(200, metrics(u.searchParams.get('range'), world.site));
  if (p.endsWith('/staging/copies')) return reply(200, { project: 'shop', minimum: 0, maximum: 1, text: 'One copy is running. Staging runs one copy, and none once it has been idle for 4 h. Stopping the copy stops the endpoint.',
    rows: [{ copy: 'a1b2c3d4', where: 'Running on this computer.', machine: 'this computer', model_version: 'shop/starter-ranker@v2', created_at: '2026-11-11T16:05:48+00:00', started_at: '2026-11-11T16:05:49.231Z',
      ready_s: 0.412, requests: 48, running: 0, errors: 1, memory_mb: 212.4, state: 'idle', status: 'Idle', can_stop: true,
      text: 'Idle and ready: nothing has called it for 10 min. It answers the moment a request arrives, and stops by itself in 3 h 50 min if none does.' }] });
  if (p.endsWith('/staging/logs')) {
    const q = (u.searchParams.get('q') ?? '').toLowerCase();
    const rows = LINES.filter((x) => !q || x.text.toLowerCase().includes(q));
    return reply(200, { project: 'shop', rows, total: 3, matching: rows.length, streaming: true, last: rows.at(-1)?.ts, note: 'Lines from requests handled at the same time can interleave. Times are UTC, to the millisecond.',
      text: q ? `${rows.length} of 3 lines match.` : '3 lines since this version was staged. New lines appear within about 15 seconds.' });
  }
  if (p.endsWith('/staging/settings')) {
    const idle = opts.method === 'PUT' ? body.idle_minutes : world.idle;
    world.idle = idle;
    return reply(200, { project: 'shop', idle_minutes: idle, idle_choices: [15, 60, 240, 720, 1440], can_edit: true, text: 'Only the idle time can be changed while it runs. To change the machine or where it runs, stage it again.',
      ...(opts.method === 'PUT' ? { saved: 'It now stops after 1 h with no requests.' } : {}),
      groups: [{ title: 'Where it runs', rows: [['Runs', 'Running on this computer. Only this computer can reach it.'], ['Machine', "This computer's own processor and memory"]] },
        { title: 'Copies', rows: [['Minimum', 'None. It stops by itself when idle.'], ['Maximum', '1']] },
        { title: 'Stopping when idle', edit: 'idle', rows: [['Stops after', idle === 60 ? '1 h with no requests' : '4 h with no requests']] }] });
  }
  if (p.endsWith('/staging/cost')) return reply(200, world.cost);
  if (p.endsWith('/staging/requests')) return reply(200, { project: 'shop', total: 1, text: '1 request since this version was staged.', rows: [{ request_id: 'r1', ts: '2026-11-11T16:06:02.118Z', query: 'jacket', arm: 'candidate', model_version: 'shop/starter-ranker@v2', ms: 2.7, results: 1, traffic: 'unlabelled', status: 'answered' }] });
  if (p.endsWith('/staging/requests/r1')) return reply(200, { request_id: 'r1', ts: '2026-11-11T16:06:02.118Z', query: 'jacket', arm: 'candidate', model_version: 'shop/starter-ranker@v2', ms: 2.7, traffic: 'unlabelled', site_ms: 111,
    time_text: '2.7 ms at the endpoint, 111 ms as your site saw it.', results: [{ id: 'p1', position: 1, title: 'Rain Jacket', signals: null, events: [] }], outcomes: [], shadow: null, lines: [LINES[1]] });
  if (p.endsWith('/staging')) return reply(200, world.staging);
  if (p.endsWith('/staging/connect')) return reply(200, { reachable: false, connected: false, address: 'http://127.0.0.1:5001', text: 'Running on this computer. Only this computer can reach it.', live: 'Waiting for the first request.', real: 0, unlabelled: 0, test: 0, simulated: 0, running: 0, snippets: {} });
  if (p.endsWith('/ab')) return reply(200, { status: 'none', experiment: null, can_measure: ['clicks'] });
  return reply(200, {});
};

const { Ship } = await import(path.join(dir, 'ship.js'));
const { palette } = await import(path.join(dir, 'kit/tokens.js'));
const { View } = await import('gpui-kit');
const picked = [];
const page = { pid: () => 'shop', open: null, deployOpen: true, project: () => ({ name: 'Shop' }), reload() {}, openDeployment() {}, pickProject: (_cx, p) => picked.push(p),
  stages: { ship: { steps: [] } }, exps: { champion: 'shop/keyword-baseline@v1', experiments: [{ id: 'exp-1', pipeline: 'shop/starter-ranker@v2', is_champion: false, evaluation: {} }] } };
class Dep extends View {
  init(_p, cx) { this.ship = new Ship(page); this.ship.sync(cx, { staging: world.staging.staged }); }
  render() { return this.ship.listOpen ? this.ship.deploymentsList(palette(false)) : this.ship.deployment(palette(false)); }
}
const IDLE = { project: 'shop', state: 'idle', label: 'Idle', text: 'Idle and ready.', copies: 1, running: 0, line: 'Copies: 1 live. Requests: 0 running.' };
const FREE = { project: 'shop', free: true, rows: [], text: 'Runs on this computer. No cloud cost.' };
const open = async (w = {}) => {
  world = { staging: RUN, live: IDLE, idle: 240, cost: FREE, site: false, ...w }; sent = [];
  const h = harness(Dep);
  await h.start();
  for (let i = 0; i < 40 && !(h.mod.ship.met && h.mod.ship.liveD); i++) await h.idle(30);
  return h;
};
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);
const tab = async (h, id, ready) => { await h.click(`dep-tab-${id}`); for (let i = 0; i < 40 && !ready(h.mod.ship); i++) await h.idle(30); };
const bars = (h, id) => byId(h, id)[0].kids.filter((k) => typeof k?.call === 'function');

// The header: the nine tabs in order, the chart over the chosen range, and the live card on every tab.
let h = await open();
let t = h.text();
assert.deepEqual(h.byIdPrefix('dep-tab-').map((e) => e.kids.find((k) => typeof k === 'string')), ['Requests', 'Try it', 'Traffic', 'Copies', 'Metrics', 'Versions', 'Logs', 'Settings', 'Cost'], 'the tabs, in the design order');
assert.ok(t.includes('48 requests in the last hour, 1 error.'), `the chart says what it shows: ${t}`);
// The chart is folded to one line on a tab other than Requests, so the tab's content is in view. Opening it is
// remembered, and it then stays open on every tab.
assert.equal(byId(h, 'dep-chart-bars').length, 0, 'folded on the Try it tab');
assert.ok(t.includes('Show the chart') && byId(h, 'dep-range').length === 0, 'one line, with a way to open it');
assert.equal(byId(h, 'dep-body').length, 1, "the tab's content scrolls by itself under a fixed header and tab bar");
await h.click('dep-chart-toggle');
t = h.text();
assert.equal(store['captain.deployment.chart'], 'open', 'the choice is kept on this computer');
assert.ok(bars(h, 'dep-chart-bars').length >= 4, 'two bars, the error mark and the version line');
assert.ok(t.includes('highest 40'), 'the largest slice is printed, so the height means something');
assert.ok(byId(h, 'dep-live-state')[0].kids.includes('Idle') && t.includes('1 copy live') && t.includes('0 requests running'), 'the live state in the header chips: idle is a state, not a fault');
// Folded by default: how long it has been up and the cost line of a local endpoint sit behind Details.
assert.deepEqual([byId(h, 'ship-status').length, byId(h, 'ship-line-cost').length, byId(h, 'ship-where').length], [0, 0, 1], 'a compact header');
await h.click('ship-details-toggle');
assert.deepEqual([byId(h, 'ship-status').length, byId(h, 'ship-line-cost').length, byId(h, 'ship-details-rows').length], [1, 1, 1], 'Details opens the rest');
await h.click('ship-details-toggle');
await h.click('dep-range-24h');
for (let i = 0; i < 40 && !h.mod.ship.met; i++) await h.idle(30);
assert.ok(sent.some((x) => x[1].endsWith('/staging/metrics?range=24h')) && h.text().includes('48 requests in the last 24 hours'), 'the time range is shared and asked again');

// With no choice made, the chart is open on Requests and folded elsewhere. A person who folds it keeps it folded.
delete store['captain.deployment.chart'];
await tab(h, 'requests', (x) => x.reqs);
assert.equal(byId(h, 'dep-chart-bars').length, 1, 'open on Requests by default');
await h.click('dep-chart-toggle');
assert.ok(store['captain.deployment.chart'] === 'folded' && byId(h, 'dep-chart-bars').length === 0 && h.text().includes('48 requests in the last 24 hours'), 'folded, and the sentence stays');
delete store['captain.deployment.chart'];

// Copies: the one copy with its startup time and what it handled, idle said in a sentence, and Stop asks first.
await tab(h, 'copies', (s) => s.copiesD);
t = h.text();
assert.ok(t.includes('a1b2c3d4') && t.includes('0.412 s') && t.includes('212 MB') && t.includes('Idle and ready: nothing has called it for 10 min.'), `the copy: ${t}`);
assert.ok(t.includes('Stopping the copy stops the endpoint.') && byId(h, 'dep-live-state').length === 1, 'what stopping means, and the live state is still in view');
await h.click('dep-copy-0-stop');
assert.ok(h.text().includes('Stop staging? The endpoint goes away and its log is kept.'), 'Stop asks before it stops');
h.mod.ship.stopAsk = false;

// Metrics: both response times, in words and as median and 95th percentile charts.
await tab(h, 'metrics', (s) => s.met);
t = h.text();
assert.ok(t.includes('Response time at the endpoint') && t.includes('At the endpoint: median 0.2 ms, 95th percentile 0.6 ms.'), `at the endpoint: ${t}`);
assert.ok(t.includes('Response time as your site sees it') && t.includes('As your site sees it: not reported yet.') && t.includes('Send client_ms with each outcome'), 'the site time is asked for, never invented');
assert.equal(byId(h, 'dep-m-site-p50').length, 0, 'no chart for a time nobody reported');
assert.ok(byId(h, 'dep-m-endpoint-p50').length === 1 && byId(h, 'dep-m-endpoint-p95').length === 1 && t.includes('Median') && t.includes('95th percentile'), 'median and 95th percentile');
assert.ok(t.includes('1 in the last 24 hours, 2.1% of requests.') && t.includes('1 running now. Minimum 0, maximum 1') && t.includes('212 MB at its peak') && t.includes('0 being answered right now.'), 'errors, copies, memory, waiting');
assert.ok(t.includes('The thin line marks where a version was staged: Version 1 staged: shop/starter-ranker@v2 at 16:05.'), 'the deploy marker is explained');
h = await open({ site: true });
await tab(h, 'metrics', (s) => s.met);
assert.ok(h.text().includes('As your site sees it: median 111 ms, 95th percentile 201 ms, from 40 requests your site timed.') && byId(h, 'dep-m-site-p50').length === 1, 'both times once the site reports');

// Logs: millisecond times, an error in the fail colour, the streaming marker, the interleave note, and the filter.
await tab(h, 'logs', (s) => s.logsD);
t = h.text();
assert.ok(t.includes('16:05:49.231') && t.includes('16:06:02.118') && t.includes("Ranked 'jacket' with the candidate in 2.7 ms: 10 results."), `lines to the millisecond: ${t}`);
assert.ok(t.includes('Streaming') && t.includes('Lines from requests handled at the same time can interleave.'), 'streaming, and the honest note');
h.mod.ship.logBox.set_value('tent');
await h.click('dep-logs-apply');
for (let i = 0; i < 40 && !h.mod.ship.logsD; i++) await h.idle(30);
t = h.text();
assert.ok(sent.some((x) => x[1].includes('/staging/logs?limit=300&q=tent')) && t.includes('1 of 3 lines match.') && !t.includes("Ranked 'jacket'"), 'the filter is asked of the services');

// Settings: grouped, and the idle time changes with no restart.
await tab(h, 'settings', (s) => s.setD);
t = h.text();
assert.ok(t.includes('Where it runs') && t.includes('Copies') && t.includes('Stopping when idle') && t.includes('4 h with no requests'), `grouped: ${t}`);
assert.equal(byId(h, 'dep-set-edit').length, 1, 'Edit is on the group that can change');
await h.click('dep-set-edit');
await h.click('dep-set-idle-60');
for (let i = 0; i < 40 && !h.mod.ship.setNote; i++) await h.idle(30);
t = h.text();
assert.deepEqual(sent.find((x) => x[0] === 'PUT')[2], { idle_minutes: 60 });
assert.ok(t.includes('It now stops after 1 h with no requests.') && t.includes('1 h with no requests'), 'saved, and shown');

// Cost: a local endpoint costs nothing and says so. A cloud one shows the month, the day, the rate and the machine.
await tab(h, 'cost', (s) => s.costD);
assert.ok(h.text().includes('Runs on this computer. No cloud cost.') && byId(h, 'dep-cost-month').length === 0, 'no cloud cost, and no empty figures');
h = await open({ cost: { project: 'shop', free: false, month_usd: 0.792, day_usd: 0.66, hourly_usd: 0.066, month_text: '$0.79', day_text: '$0.66', running: true,
  rate_text: 'About $0.07 an hour while it is up, about $1.58 a day.', rows: [{ machine: 'ml.t2.medium', usd: 0.792, hours: 12, text: 'ml.t2.medium: $0.79 for 12.0 hours' }],
  estimate: 'About $0.07 an hour on aws-serving.', note: "Worked out from how long each endpoint was up, at the list price. Your cloud's bill is the final word.",
  text: '$0.79 so far this month, $0.66 in the last 24 hours. About $0.07 an hour while it is up, about $1.58 a day.' } });
await tab(h, 'cost', (s) => s.costD);
t = h.text();
assert.ok(t.includes('So far this month') && t.includes('$0.79') && t.includes('Last 24 hours') && t.includes('$0.66') && t.includes('$0.07 an hour'), `the three figures: ${t}`);
assert.ok(t.includes('ml.t2.medium: $0.79 for 12.0 hours') && t.includes("Your cloud's bill is the final word.") && t.includes('The estimate shown before it was staged:'), 'by machine, the estimate, and whose number is final');

// A request's panel: unlabelled is said plainly, both times, and its own log lines.
await tab(h, 'requests', (s) => s.reqs);
await h.click('dep-request-0');
for (let i = 0; i < 40 && !h.mod.ship.reqOpen?.data; i++) await h.idle(30);
t = h.text();
assert.ok(t.includes('2.7 ms at the endpoint, 111 ms as your site saw it.') && t.includes("Unlabelled: it didn't say it was a shopper."), `the panel: ${t}`);
assert.ok(t.includes('Log lines of this request') && byId(h, 'dep-request-line-0').length === 1, "the request's own log lines");

// The list of deployments: live cards with a chart and counts, stopped ones still findable, and a card opens its page.
h.mod.ship.openList(h.cx);
for (let i = 0; i < 40 && !h.mod.ship.listD; i++) await h.idle(30);
t = h.text();
assert.ok(t.includes('Live') && t.includes('Stopped') && t.includes('Shop') && t.includes('Staged by maya 6 min ago') && t.includes('Running on this computer.'), `the live card: ${t}`);
assert.ok(t.includes('Copies: 1 live. Requests: 0 running. 48 in 24 hours.') && bars(h, 'deps-card-0-bars').length === 2, 'counts, and a chart with its error mark');
await h.click('deps-tab-stopped');
for (let i = 0; i < 40 && !h.mod.ship.listD; i++) await h.idle(30);
assert.ok(h.text().includes('Old shop') && h.text().includes('Stopped 2 h ago: stopped by maya'), 'stopped ones stay findable');
await h.click('deps-card-0');
assert.deepEqual(picked.at(-1), { id: 'old', name: 'Old shop' });
assert.ok(!h.mod.ship.listOpen && page.deployOpen, "a card opens that project's deployment page");

// No Unicode arrows or long dashes anywhere on these tabs.
for (const id of ['copies', 'metrics', 'logs', 'settings', 'cost']) { await tab(h, id, () => true); await h.idle(40); assert.ok(!/[←-⇿—–]/.test(h.text()), `${id}: plain punctuation only`); }
console.log('deployment tabs render test: ok');
