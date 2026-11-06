// CAPTAIN: run the New class, Run a backtest and Start grading forms' real submits against a live backend, with the
// values a person would pick from their pickers. Not in CI: it needs a seeded scratch backend (cst demo seed).
//   CAPTAIN_BASE=http://127.0.0.1:<port> CAPTAIN_TOKEN=<token> node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/forms-mine-e2e.mjs
import assert from "node:assert/strict";

globalThis.__shared = { "captain.daemon": { base: process.env.CAPTAIN_BASE, token: process.env.CAPTAIN_TOKEN } };
const { TASKS } = await import("../../defaults/mods/captain-forms/tasks.js");
const task = (id) => TASKS.find((t) => t.id === id);
const field = (t, k) => t.fields.find((f) => f.key === k);
const pick = async (t, k) => { const o = await field(t, k).options({}, {}); assert.ok(o.length, `${t.id}.${k} has choices`); return o; };
const stamp = Date.now() % 100000;

// New class: roles come from the backend.
const cls = task("dataset-class");
const roles = await pick(cls, "role");
assert.ok(roles.some((r) => r.value === "queries"), "roles listed");
let r = await cls.submit({ name: `reviews-${stamp}`, description: "Reviews shoppers left", role: "generic", color: "#059669", columns: "id, text" });
assert.ok(r.ok, `class: ${r.error}`);
r = await cls.submit({ name: `reviews-${stamp}`, description: "again", role: "generic", color: "#059669", columns: "" });
assert.ok(!r.ok && !/--|Traceback/.test(r.error), `duplicate class says so plainly: ${r.error}`);
console.log("class:", r.error);

// Run a backtest: pipelines and datasets by role, as the pickers offer them.
const bt = task("backtest");
const pipes = await pick(bt, "a");
const queries = await pick(bt, "queries");
const judgments = await pick(bt, "judgments");
console.log("pickers:", pipes.map((p) => p.value).join(" "), "|", queries.map((p) => p.value).join(" "), "|", judgments.map((p) => p.value).join(" "));
r = await bt.submit({ name: `form-bt-${stamp}`, a: pipes[0].value, b: pipes[0].value, mode: "simulated", queries: queries[0].value, judgments: judgments[0].value, hypothesis: "x", mde: "0.003" });
assert.ok(!r.ok && /two different/.test(r.error), "same arm twice refused");
r = await bt.submit({ name: `form-bt-${stamp}`, a: pipes[0].value, b: pipes[1].value, mode: "simulated", queries: queries[0].value, judgments: judgments[0].value,
  hypothesis: "The tuned ranker finds more relevant products", mde: "0.003" });
assert.ok(r.ok, `backtest: ${r.error}`);
console.log("backtest:", r.message);

// Start grading: through the command runner.
const gr = task("grading");
r = await gr.submit({ name: `form-grade-${stamp}`, queries: queries[0].value, a: pipes[0].value, b: pipes[1].value, depth: "5", sample: "5" });
assert.ok(r.ok, `grading: ${r.error}`);
console.log("grading:", r.message);
console.log("forms-mine e2e: ok");
