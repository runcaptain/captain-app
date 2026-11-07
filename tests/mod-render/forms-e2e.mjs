// CAPTAIN: run every designed form's real submit (defaults/mods/captain-forms/tasks.js)
// against a live Captain Search backend, the way the form does. Not in CI: it needs one.
//   CST_HOME=<scratch> <Captain.app>/Contents/Resources/cst/bin/cst serve --port 0   (note port, token)
//   CAPTAIN_BASE=http://127.0.0.1:<port> CAPTAIN_TOKEN=<token> node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/forms-e2e.mjs
// It cleans up the keychain secret and the cloud login it makes.
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { spawnSync } from "node:child_process";
globalThis.__exec = async (cmd, args = []) => { const r = spawnSync(cmd, args, { encoding: "utf8" }); return { code: r.status ?? 1, stdout: r.stdout ?? "", stderr: r.stderr ?? "" }; };
globalThis.__shared = { "captain.daemon": { base: process.env.CAPTAIN_BASE, token: process.env.CAPTAIN_TOKEN } };
const { TASKS } = await import("../../defaults/mods/captain-forms/tasks.js");
const { api } = await import("../../defaults/mods/captain-forms/kit/cst.js");
const task = (id) => TASKS.find((t) => t.id === id);
const results = [];
async function step(name, id, values, args = {}, expectOk = true) {
  const t = task(id);
  const ctx = t.load ? await t.load() : {};
  let r;
  try { r = await t.submit(values, ctx, args); } catch (e) { r = { ok: false, error: `threw: ${e}` }; }
  const pass = Boolean(r.ok) === expectOk;
  results.push({ name, pass, r });
  console.log(`${pass ? "PASS" : "FAIL"} ${name}: ${r.ok ? r.message : r.error}`);
  return r;
}
// Options a picker would offer, to choose from like a person would.
async function options(id, key, values = {}) {
  const t = task(id);
  const ctx = t.load ? await t.load() : {};
  const f = t.fields.find((x) => x.key === key);
  return typeof f.options === "function" ? await f.options(ctx, values) : f.options;
}

const dir = mkdtempSync(join(tmpdir(), "captain-forms-"));
const csv = join(dir, "tiny.csv");
writeFileSync(csv, "product_id,title,price\n1,Red mug,9.5\n2,Blue mug,9.5\n3,Teapot,24\n");

await step("New investigation", "investigation", { title: "Forms e2e: does the blue mug rank?", hypothesis: "It should rank above the teapot for 'mug'." });
const pipes = await options("investigation", "baseline");
await step("New investigation with a baseline", "investigation", { title: "Forms e2e with baseline", baseline: pipes[0]?.value });
await step("Import a dataset from a file", "dataset", { from: "file", name: "forms-e2e-tiny", kind: "catalog", file: csv });
await step("Import a dataset from a missing file (plain error)", "dataset", { from: "file", name: "forms-e2e-missing", kind: "catalog", file: join(dir, "nope.csv") }, {}, false);
// The form is typed (GET /api/connections/kinds): the kind, then that kind's fields ("af:<kind>.<field>"). It saves,
// puts the password in the keychain, then tests the connection. Nothing listens on port 1, so it says it saved but
// couldn't reach it, which is the plain error a person sees.
const dbName = `forms-e2e-db-${Date.now() % 100000}`;
const pg = { "af:postgres.host": "127.0.0.1", "af:postgres.port": "1", "af:postgres.database": "shop", "af:postgres.user": "reader" };
const saved = await step("Connect a database it can't reach yet (saved, plain error)", "database", { kind: "postgres", name: dbName, ...pg, "af:postgres.password": "s3cret-e2e", writable: false }, {}, false);
if (!/^Saved .*couldn't reach it/.test(saved.error ?? "")) { results.at(-1).pass = false; console.log(`     expected "Saved ..., but Captain couldn't reach it", got: ${saved.error}`); }
await step("Connect a database that is already connected (plain error)", "database", { kind: "postgres", name: dbName, ...pg }, {}, false);
await step("Connect a database without picking one (plain error)", "database", { name: "forms-e2e-db2" }, {}, false);
const conns = await api("/api/connections");
const conn = (conns.data ?? []).find((c) => c.name.startsWith("forms-e2e-db"));
console.log(`     connection stored: ${conn ? JSON.stringify(conn) : "missing"}`);
const secrets = await api("/api/secrets");
console.log(`     secret listed by name only: ${JSON.stringify(secrets.data)}`);

const providers = await options("cloud", "provider");
console.log(`     cloud providers offered: ${providers.map((p) => p.label).join(", ")}`);
const auths = await options("cloud", "auth", { provider: "aws" });
console.log(`     AWS sign-in modes: ${auths.map((a) => a.label).join(", ")}`);
const kinds = await options("compute", "kind");
console.log(`     compute kinds: ${kinds.map((k) => k.label).join(", ")}`);

await step("Connect a cloud account (AWS default chain)", "cloud", { provider: "aws", auth: auths.find((a) => /Default/.test(a.label))?.value, name: "forms-e2e-aws", region: "us-west-1" });
// The second add reuses the first's name, so it already exists.
const localName = `forms-e2e-local-${Date.now() % 100000}`;
await step("Add a place to run work (this machine)", "compute", { kind: kinds.find((k) => /This machine/.test(k.label))?.value, name: localName });
await step("Add a place that already exists (plain error)", "compute", { kind: kinds.find((k) => /This machine/.test(k.label))?.value, name: localName }, {}, false);
const envs = await options("handoff", "env");
await step("Hand off a candidate that isn't ready (plain error)", "handoff", { pipeline: pipes.find((p) => /tuned/.test(p.value))?.value ?? pipes[0]?.value, env: envs.find((e) => e.chip === "live")?.value ?? envs[0]?.value }, {}, false);
await step("Hand off with a reason", "handoff", { pipeline: pipes.find((p) => /tuned/.test(p.value))?.value ?? pipes[0]?.value, env: envs.find((e) => e.chip === "live")?.value ?? envs[0]?.value, reason: "forms e2e, scratch workspace" });

const inv = (await api("/api/investigations")).data?.find((i) => i.title === "Forms e2e: does the blue mug rank?");
await step("Attach evidence", "evidence", { kind: "dataset", ref: "forms-e2e-tiny", note: "the tiny catalog" }, { id: inv?.id });
await step("Record a decision", "decision", { outcome: "inconclusive", claim: "no-detectable-change", interpretation: "Too little data to say.", next_step: "Grade more queries." }, { id: inv?.id });
const ho = (await api("/api/handoffs")).data?.[0];
await step("Record a deployment", "record-deployment", { system: "argo", deployment_id: "e2e-123", url: "https://argo.example/e2e", live: true }, { id: ho?.id });
await step("Add an online result", "add-outcome", { metric: "conversion rate", value: "0.012", ci_low: "0.002", ci_high: "0.02", source: "e2e experiment report" }, { id: ho?.id });
await step("Add an online result that isn't a number (plain error)", "add-outcome", { metric: "ctr", value: "lots", source: "x" }, { id: ho?.id }, false);

// Clean up what lives outside the scratch workspace: the keychain secret.
for (const n of (await api("/api/secrets")).data?.names ?? []) if (n.startsWith("FORMS_E2E_DB")) await api(`/api/secrets/${n}`, { method: "DELETE" });
await api("/api/cloud/profiles/forms-e2e-aws", { method: "DELETE" });
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length ? 1 : 0);
