// Tables: "Read lineage from Snowflake" on a Snowflake connection (and not on Postgres), with 30 or 90 days, and the
// result in words: "Read 12 table links and 40 column links from Snowflake, as of ...".
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/tables-lineage.test.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { harness } from './harness.mjs';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
globalThis.__fixtures = path.join(here, 'fixtures/captain-data-tables');
globalThis.__routes = [
  [/^\/api\/lineage\/import\/wh$/, 'job-start', 'POST'],
  [/^\/api\/jobs\//, 'job-done'],
  [/^\/api\/tables$/, 'tables'],
  [/^\/api\/tables\/wh$/, 'tree'],
  [/^\/api\/tables\/wh\/.+/, 'table'],
];
globalThis.__shared = {};
globalThis.__props = { tab: 'tables' };
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'captain-data-tl-'));
fs.cpSync(path.join(root, 'defaults/mods/captain-data'), work, { recursive: true });
fs.copyFileSync(path.join(root, 'defaults/captain/kit/cst.js'), path.join(work, 'kit/real-cst.js'));
fs.copyFileSync(path.join(here, 'fixture-cst.js'), path.join(work, 'kit/cst.js'));
const { default: Mod } = await import(path.join(work, 'main.js'));
const h = harness(Mod);
await h.start();
await h.idle();
let t = h.text();
assert.ok(t.includes('Read lineage from Snowflake'), 'offered on Snowflake');
assert.equal((t.match(/Read lineage from/g) ?? []).length, 1, 'not on Postgres');
await h.click('lin-days-wh-90');
await h.click('lin-wh');
const post = globalThis.__bodies.filter(([p]) => p === '/api/lineage/import/wh').pop();
assert.deepEqual(post?.[1]?.body, { days: 90 }, 'asks for 90 days');
await h.layoutLists({});
await h.idle();
t = h.text();
assert.ok(/Read 12 table links and 40 column links from Snowflake, as of /.test(t), `summary: ${t.slice(0, 400)}`);
console.log('tables-lineage render test: ok');
