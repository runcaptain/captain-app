// The sidebar's cloud chip says what is true (GET /api/cloud/summary): the login that holds the workspace, or the
// only one; "2 clouds connected" with the list in its tooltip when there are several and none holds the workspace;
// "No cloud connected" only when there is none. It used to say "No cloud connected" with two logins.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/cloud-chip.test.mjs
import assert from 'node:assert/strict';

const { cloudChipInfo, default: Nav } = await import('../../defaults/mods/captain-nav/main.js');
const { palette } = await import('../../defaults/mods/captain-nav/kit/tokens.js');
const AWS = { name: 'aws-main', provider: 'aws', label: 'Amazon Web Services', region: 'us-west-1', account_hint: 'team', used_by_storage: false, last_test: { ok: true, identity: 'arn:aws:iam::000000000000:user/dev' } };
const GCP = { name: 'gcp-main', provider: 'gcp', label: 'Google Cloud', region: 'us-central1', account_hint: 'shop-prod', used_by_storage: false, last_test: null };
const sum = (profiles, active = null) => ({ profiles, active });

// none
let k = cloudChipInfo(sum([]));
assert.deepEqual([k.text, k.open, k.dot, k.count], ['No cloud connected', 'connect', 'none', 0]);
assert.equal(cloudChipInfo(null).text, 'No cloud connected', 'before the answer arrives it claims nothing more');
// one: by name, as before
k = cloudChipInfo(sum([AWS], AWS));
assert.equal(k.text, 'AWS · team · us-west-1');
assert.ok(k.tip.includes('Signed in as arn:aws:iam::000000000000:user/dev') && k.open === 'cloud' && k.dot === 'good' && k.provider === 'aws');
// two, and neither holds the workspace: the count, with the list, and it opens the list
k = cloudChipInfo(sum([AWS, GCP]));
assert.equal(k.text, '2 clouds connected');
assert.ok(k.tip.includes('AWS · team · us-west-1 (signed in as') && k.tip.includes('GCP · shop-prod · us-central1 (not checked yet)'), `the list: ${k.tip}`);
assert.ok(k.tip.includes('This workspace is not kept in any of them.') && k.open === 'cloud' && k.count === 2 && k.provider === null);
assert.equal(k.dot, 'none', 'one of the two was never checked: not green');
assert.equal(cloudChipInfo(sum([AWS, { ...GCP, last_test: { ok: false, error: 'expired' } }])).dot, 'fail', 'a failed check shows');
assert.equal(cloudChipInfo(sum([AWS, { ...GCP, last_test: { ok: true, identity: 'dev@example.com' } }])).dot, 'good');
// two, and one holds the workspace: that one, and the tooltip names the other
const holder = { ...GCP, used_by_storage: true };
k = cloudChipInfo(sum([AWS, holder], holder));
assert.equal(k.text, 'GCP · shop-prod · us-central1');
assert.ok(k.tip.includes('It holds this workspace. Also connected: AWS · team · us-west-1.'), k.tip);

// drawn: the text, the tooltip, and where a click goes
const text = (e) => (e.kids ?? []).flatMap((x) => (typeof x === 'string' ? [x] : x && typeof x === 'object' ? text(x) : []));
const draw = (summary) => Nav.prototype.cloudChip.call({ cloud: summary, state: 'ready' }, palette(false), false);
let chip = draw(sum([AWS, GCP]));
assert.ok(text(chip).includes('2 clouds connected'), `drawn: ${text(chip)}`);
assert.ok(chip.call('tooltip')[0].startsWith('2 clouds connected: AWS'), 'the list is in the tooltip');
// A click opens the compute picker (Edgar, 2026-11-12: "select what compute environment I'm working in whenever I
// want"): the logins are one click further, from the picker. The chip says where runs go, under the login.
globalThis.__overlays = [];
chip.call('on_click')[0]();
assert.equal(globalThis.__overlays.at(-1)?.mod, 'captain-compute', 'a click opens the compute picker');
assert.ok(text(chip).includes('Runs on This computer'), `before anything is picked, runs go to this computer: ${text(chip)}`);
chip = Nav.prototype.cloudChip.call({ cloud: sum([AWS], AWS), state: 'ready', place: { value: 'aws-batch', label: 'aws-batch' } }, palette(false), false);
assert.ok(text(chip).includes('Runs on aws-batch') && text(chip).includes('AWS · team · us-west-1'), `the place picked, under the login: ${text(chip)}`);
assert.ok(chip.call('tooltip')[0].includes('Runs go to aws-batch'), 'and in the tooltip');
chip = draw(sum([]));
assert.ok(text(chip).includes('No cloud connected'));
chip.call('on_click')[0]();
assert.equal(globalThis.__overlays.at(-1)?.mod, 'captain-compute', 'with no cloud, the picker still opens: this computer is a place too');
assert.equal(Nav.prototype.cloudChip.call({ cloud: sum([AWS, GCP]), state: 'ready' }, palette(false), true).call('tooltip')[0].includes('2 clouds connected'), true, 'the narrow sidebar keeps the tooltip');
console.log('cloud chip render test: ok');
