// Data and signals, Models tab, against fixtures: attached registries with Sync and Detach (a second click confirms),
// attaching a new one (address, login, name) as a job, and every model by architecture with what it was built from.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/models.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data-models');
globalThis.__routes = [
  [/^\/api\/registries$/, 'job-start', 'POST'],
  [/^\/api\/registries\?uri=/, 'detached', 'DELETE'],
  [/^\/api\/registries$/, 'registries'],
  [/^\/api\/models(\?.*)?$/, 'models'],
  [/^\/api\/explorer$/, 'explorer'],
  [/^\/api\/projects\/shop\/lineage$/, 'project-lineage'],
  [/^\/api\/cloud\/profiles$/, 'profiles'],
  [/^\/api\/jobs\//, 'job-done'],
];
// The Explorer has the project picked: Models opens on it.
globalThis.__shared = { 'captain.scope': { kind: 'project', id: 'shop', label: 'Shop project' } };
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-models-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();
await h.click('tab-models');
await h.idle();

let t = h.text();
assert.ok(t.includes('Team registry') && t.includes('s3://example-models/registry') && /3 models, synced/.test(t), `registry row: ${t.slice(0, 500)}`);
assert.ok(t.includes('arch-v1') && t.includes('arch-v2'), 'models by architecture');
assert.ok(t.includes('shop v2') && t.includes('built from') && t.includes('shop@v1'), 'lineage: what a model was built from');

// Click a model: it and its parent light up.
await h.click('model-shop@v2');
assert.equal(h.mod.models.focus, 'shop@v2');

// Attach: address, login, name, then the job finishes and the list reloads.
await h.click('reg-attach');
h.mod.models.uri.set_value('s3://new-bucket/models');
h.mod.models.label.set_value('New registry');
await h.click('reg-prof-gcp-team');
await h.click('reg-attach-go');
await h.idle();
const post = globalThis.__bodies.filter(([p, o]) => p === '/api/registries' && o?.body).pop();
assert.deepEqual(post[1].body, { uri: 's3://new-bucket/models', profile: 'gcp-team', label: 'New registry' }, 'attach body');
await h.layoutLists({});
assert.ok(h.text().includes('Attached s3://new-bucket/models.'), `attach note: ${h.text().slice(0, 400)}`);

// A bad address is refused before any request.
await h.click('reg-attach');
h.mod.models.uri.set_value('not-an-address');
await h.click('reg-attach-go');
assert.ok(h.text().includes("Type the registry's address, like s3://bucket/models."), 'address check');

// Detach asks for a second click.
await h.click('reg-detach-0');
assert.ok(h.text().includes('Detach? Click again'), 'confirm');
await h.click('reg-detach-0');
await h.idle();
assert.ok(globalThis.__asked.some((p) => /^\/api\/registries\?uri=s3%3A%2F%2Fexample-models%2Fregistry$/.test(p)), 'detach request');
assert.ok(/Detached Team registry/.test(h.text()), 'detach note');

// Whose models: the Explorer's pick by default, the project's lineage drawn, and a brand filters the list.
assert.equal(h.mod.models.owner.id, 'shop', 'defaults to the Explorer pick');
assert.ok(globalThis.__asked.includes('/api/models?project=shop'), 'models for the project');
t = h.text();
assert.ok(t.includes('Shop project: models, what they were built from, and where they run') && t.includes('shop-ranker'), `project lineage drawn: ${t.slice(0, 300)}`);
await h.click(`owner-${h.mod.models.owners.findIndex((o) => o.id === 'brand-shop-1a2b')}`);
await h.idle();
assert.ok(globalThis.__asked.includes('/api/models?scope=scope%3Abrand-shop-1a2b'), 'models for the brand');
assert.ok(!h.text().includes('Shop project: models'), 'no project lineage for a brand');
console.log('models render test: ok');
