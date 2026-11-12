// The list of cloud logins (Cloud and compute), with a faked backend: each login is one row with its cloud's own
// mark, its cloud's name, the login's name, how it signs in and whether the last check worked, all in plain words
// and one typeface. Modal is a login like the others. One filled button on the screen: "Add a login".
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/cloud-logins.test.mjs
import assert from 'node:assert/strict';
import path from 'node:path';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const dir = path.resolve(here, '../../defaults/mods/captain-cloud');
globalThis.__shared = { 'captain.daemon': { state: 'ready', base: 'http://captain.test', token: 't' } };
let world, sent;
const reply = (status, data) => ({ status, ok: status < 300, text: async () => JSON.stringify(data) });
globalThis.fetch = async (url, opts = {}) => {
  const p = new URL(url).pathname;
  sent.push([opts.method ?? 'GET', p]);
  if (p === '/api/cloud/summary') return reply(200, world);
  return reply(200, {});
};
const AWS = { name: 'work-aws', provider: 'aws', auth: 'profile', label: 'Amazon Web Services', region: 'us-west-1', account_hint: 'team', used_by_storage: true,
  last_test: { ok: true, identity: 'arn:aws:iam::000000000000:user/dev', at: new Date().toISOString() } };
const GCP = { name: 'shop-gcp', provider: 'gcp', auth: 'adc', label: 'Google Cloud', region: null, account_hint: 'shop-prod', last_test: { ok: false, error: 'the sign-in has expired', at: new Date().toISOString() } };
const AZ = { name: 'az', provider: 'azure', auth: 'default', label: 'Microsoft Azure', region: 'eastus', last_test: null };
const MODAL = { name: 'modal', provider: 'modal', auth: 'local', label: 'Modal', region: null, last_test: { ok: true, identity: "this computer's Modal sign-in", at: new Date().toISOString() } };
const { default: Cloud } = await import(path.join(dir, 'main.js'));
const open = async (w) => { world = w; sent = []; const h = harness(Cloud); await h.start(); for (let i = 0; i < 40 && !h.mod.summary; i++) await h.idle(30); await h.idle(30); return h; };
const byId = (h, id) => h.byIdPrefix(id).filter((e) => e.call('id')?.[0] === id);

let h = await open({ workspace: 'shop', storage: 's3://team-bucket/captain/shop', storage_provider: 'aws', storage_profile: 'work-aws', active: AWS,
  profiles: [AWS, GCP, AZ, MODAL], compute: [{ name: 'local', kind: 'local', provider: 'local', purposes: ['backtest'] },
    { name: 'modal-serving', kind: 'modal-endpoint', provider: 'modal', purposes: ['serve'] }], can_run_in_cloud: true, notes: [] });
let t = h.text();
for (const [name, cloud] of [['work-aws', 'AWS'], ['shop-gcp', 'Google Cloud'], ['az', 'Azure'], ['modal', 'Modal']]) {
  assert.equal(byId(h, `cloud-login-${name}`).length, 1, `a row for ${name}`);
  assert.ok(byId(h, `cloud-login-${name}`)[0] && h.text().includes(cloud), `${cloud} by its name`);
}
assert.ok(t.includes('AWS CLI profile, us-west-1, team') && t.includes("This computer's Google sign-in, shop-prod") && t.includes("This computer's Modal sign-in"), `how each signs in, in words: ${t}`);
assert.ok(!/\badc\b|\bdefault\b|\blocal\b/.test(t), `no sign-in ids or internal names on screen: ${t}`);
assert.ok(t.includes('Signed in as arn:aws:iam::000000000000:user/dev. Checked just now.') && t.includes('The last check failed just now: the sign-in has expired') && t.includes('Not checked yet.'), 'the check, in a sentence');
assert.ok(t.includes('Holds this workspace') && byId(h, 'cloud-login-work-aws-used').length === 1 && byId(h, 'cloud-login-modal-used').length === 0, 'which login holds the workspace');
assert.ok(t.includes('offline evaluations') && t.includes('hosting') && !t.includes('backtest') && !t.includes('modal-endpoint'), 'what a place is for, in screen words, and no internal kind names');
assert.ok(!t.includes('Not run on a real account yet'), 'a login is not a host: that line belongs on a place that has not run, nowhere else');
// One typeface: this screen never asks for the code font.
const src = (await import('node:fs')).readFileSync(path.join(dir, 'main.js'), 'utf8');
assert.ok(!src.includes('MONO') && (src.match(/font_family\(/g) ?? []).length === 1, 'one typeface, set once at the top');
// The check button asks the services and says so while it waits.
await h.click('test-shop-gcp');
assert.ok(sent.some((x) => x[0] === 'POST' && x[1] === '/api/cloud/profiles/shop-gcp/test'), 'Check asks the services');

h = await open({ workspace: 'shop', storage: '/Users/someone/captain', storage_provider: 'local', active: null, profiles: [], compute: [], can_run_in_cloud: false, notes: [] });
t = h.text();
assert.ok(t.includes('No cloud connected') && t.includes('Add a login for AWS, Google Cloud, Azure or Modal.') && t.includes('This computer'), `none connected: ${t}`);
for (const bad of [/\bengine\b/i, /\bdaemon\b/i, /\bpipeline/i, /[←-⇿]/, /—/]) assert.ok(!bad.test(t), `no ${bad}`);
console.log('cloud logins render test: ok');
process.exit(0);
