// Samples, fixtures and tests use made-up project names only, from one list: tests/demo-names.json. The same two
// checks as backend/core/tests/test_demo_names.py, so the rule holds whichever suite is run. Why: on 2026-11-12 a
// customer's project name reached a sample row from a real account, in a public repository.
//   node tests/mod-render/demo-names.test.mjs
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const names = JSON.parse(fs.readFileSync(path.join(root, 'tests/demo-names.json'), 'utf8'));
const ids = new Set(names.ids), labels = new Set(names.labels);

// 1. The picture check's sample rows, generated and read row by row.
const bad = [];
for (const view of ['empty', 'leftover', 'stopped', 'stopped-open', 'one', 'several', 'failed', 'found', 'busy', 'model', 'model-details']) {
  const out = execFileSync('python3', [path.join(here, 'deployments-demo.py'), view], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  for (const r of JSON.parse(out).demo.deployed.rows) {
    if (!ids.has(r.project)) bad.push(`${view}: project ${r.project}`);
    if (r.project_name != null && !labels.has(r.project_name)) bad.push(`${view}: project name ${r.project_name}`);
    if (r.model_version && !ids.has(r.model_version.split('/')[0])) bad.push(`${view}: model version ${r.model_version}`);
  }
}
assert.deepEqual(bad, [], `not on the list of demo names (tests/demo-names.json): ${bad.join('; ')}`);

// 2. Every sample, fixture and test file here, searched for a project given by name.
const given = /\bproject(?:_name|Name|_id|_label)?["']?\s*[:=]\s*["']([A-Za-z0-9][A-Za-z0-9 _.-]{0,40})["']/g;
const skip = new Set(['demo-names.test.mjs']);
const files = fs.readdirSync(here).filter((f) => /\.(mjs|py|json)$/.test(f) && !skip.has(f)).map((f) => path.join(here, f));
const off = [];
for (const f of files) {
  fs.readFileSync(f, 'utf8').split('\n').forEach((line, i) => {
    for (const m of line.matchAll(given)) if (!ids.has(m[1]) && !labels.has(m[1])) off.push(`${path.basename(f)}:${i + 1}: ${m[1]}`);
  });
}
assert.deepEqual(off, [], `a project name that is not on the list of demo names. Use one from tests/demo-names.json, never one from a real account: ${off.join('; ')}`);

// 3. The check catches a name that is not on the list.
assert.deepEqual([...'row({ project: "real-customer" })'.matchAll(given)].map((m) => m[1]), ['real-customer']);
assert.ok(!ids.has('real-customer'));
console.log('demo names test: ok');
