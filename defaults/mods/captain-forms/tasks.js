// CAPTAIN: the things a person makes in Captain, as tasks in plain words. The New menu,
// the palette and onboarding all open these. The CLI is for agents; nothing here shows
// a command name or a flag.
import { api } from "./kit/cst.js";
import { exec, home } from "herdr";

const list = async (path, map) => {
  const r = await api(path);
  return r.ok && Array.isArray(r.data) ? r.data.map(map) : [];
};
// Errors in plain words: no CLI advice ("Pass --replace to change it"), no SHOUTED_CONSTANTS.
export function plain(text) {
  return String(text ?? "That didn't work.")
    .replace(/\s*Pass --[a-z-]+[^.]*\.?/g, "")
    .replace(/\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|\b[A-Z]{4,}\b/g, (w) => w.toLowerCase().replace(/_/g, " "))
    .trim();
}
const ok = (r, done) => {
  if (r.ok && !r.data?.error && !r.data?.exit_code) return { ok: true, message: done };
  if (r.failed) return { ok: false, error: r.error };
  if (r.status >= 500) return { ok: false, error: `Captain's services hit a problem (error ${r.status}). Try again; if it keeps happening, tell us.` };
  return { ok: false, error: plain(r.data?.error ?? r.error) };
};
// Through the services' command runner, for the few tasks without their own route.
const run = (path, args) => api("/api/commands/run", { body: { path, args }, timeout: 590 });

// A file import, after checking the file is there (the services' own error is a database message).
async function importFile(v) {
  const path = v.file.trim().replace(/^~(?=\/|$)/, home());
  let there = false;
  try { there = (await exec("test", ["-f", path])).code === 0; } catch {}
  if (!there) return { ok: false, error: `There's no file at ${v.file.trim()}.` };
  return ok(await run(["data", "snapshot"], { path, name: v.name, kind: v.kind }), `Imported ${v.name}.`);
}

export const TASKS = [
  {
    id: "investigation", label: "New investigation", icon: "search",
    about: "Ask a question about your search or recommendations, and gather the evidence to answer it.",
    fields: [
      { key: "title", label: "Question", placeholder: "Does the typo-tolerant retriever help tail queries?", required: true },
      { key: "hypothesis", label: "What you expect", kind: "long", help: "Optional. You can refine it later; every change is kept as a revision." },
      { key: "baseline", label: "Compare against", kind: "choice", help: "The version in production today, usually.", optional: true,
        options: () => list("/api/pipelines", (p) => ({ value: `${p.name}@v${p.latest}`, label: p.name, chip: `v${p.latest}` })) },
    ],
    submit: async (v) => ok(await api("/api/investigations", { body: { title: v.title, hypothesis: v.hypothesis || undefined, baseline: v.baseline || undefined } }), "Investigation opened."),
  },
  {
    id: "dataset", label: "Import a dataset", icon: "database",
    about: "Bring in a catalog, queries, judgments or logs, from a file on this computer or a SQL query.",
    fields: [
      { key: "from", label: "From", kind: "choice", default: "file", options: [{ value: "file", label: "A file" }, { value: "sql", label: "A SQL query" }] },
      { key: "name", label: "Name", placeholder: "makro-catalog", required: true, help: "Letters, digits and dashes. Each import becomes a new version of it." },
      { key: "kind", label: "What it holds", kind: "choice", default: "catalog",
        options: [{ value: "catalog", label: "Catalog" }, { value: "queries", label: "Queries" }, { value: "judgments", label: "Judgments" }, { value: "events", label: "Events or logs" }, { value: "generic", label: "Something else" }] },
      { key: "file", label: "File", placeholder: "~/Downloads/catalog.parquet", required: true, when: (v) => v.from !== "sql", help: "CSV, TSV, Parquet or JSON lines." },
      { key: "conn", label: "Database", kind: "choice", required: true, when: (v) => v.from === "sql",
        options: () => list("/api/connections", (c) => ({ value: c.name, label: c.name, chip: c.engine })), empty: "No database connected yet. Connect one first." },
      { key: "sql", label: "Query", kind: "long", required: true, when: (v) => v.from === "sql", placeholder: "select * from products" },
    ],
    submit: async (v) => (v.from === "sql"
      ? ok(await api("/api/sql", { body: { conn: v.conn, sql: v.sql, save: v.name, kind: v.kind } }), `Saved ${v.name}.`)
      : await importFile(v)),
  },
  {
    id: "database", label: "Connect a database", icon: "server",
    about: "Postgres, MySQL, Snowflake, BigQuery or Redshift, to import data with SQL.",
    fields: [
      { key: "name", label: "Name", placeholder: "warehouse", required: true },
      { key: "db", label: "Kind", kind: "choice", default: "postgres",
        options: ["postgres", "mysql", "snowflake", "bigquery", "redshift"].map((e) => ({ value: e, label: { postgres: "PostgreSQL", mysql: "MySQL", snowflake: "Snowflake", bigquery: "BigQuery", redshift: "Redshift" }[e] })) },
      { key: "url", label: "Address", placeholder: "postgresql://reader@db.internal:5432/shop", required: true, help: "Without the password: it goes below and stays in your keychain." },
      { key: "password", label: "Password", kind: "secret", optional: true },
      { key: "writable", label: "Allow writes", kind: "toggle", default: false, help: "Off keeps Captain read-only on this database." },
    ],
    submit: async (v) => {
      const taken = (await api("/api/connections")).data ?? [];
      if (taken.some((c) => c.name === v.name)) return { ok: false, error: `A database named ${v.name} is already connected. Pick another name.` };
      let url = v.url.trim();
      if (v.password) {
        // The password goes to the keychain (POST /api/secrets) and nowhere else; the address names it.
        const secret = `${v.name.toUpperCase().replace(/[^A-Z0-9]+/g, "_")}_PASSWORD`;
        const m = url.match(/^([a-z0-9+]+:\/\/)([^@/:]+)(?::[^@/]*)?@(.*)$/i);
        if (!m) return { ok: false, error: "Put the user name in the address, like postgresql://reader@db.internal:5432/shop, so Captain knows where the password goes." };
        const s = await api("/api/secrets", { body: { name: secret, value: v.password } });
        if (!s.ok) return { ok: false, error: s.error ?? "Couldn't save the password to your keychain." };
        url = `${m[1]}${m[2]}:{secret:${secret}}@${m[3]}`;
      }
      // cst calls the database kind its engine.
      return ok(await run(["conn", "add"], { name: v.name, engine: v.db, url, writable: Boolean(v.writable) }), `Connected ${v.name}.`);
    },
  },
  {
    id: "cloud", label: "Connect a cloud account", icon: "cloud",
    about: "AWS, Google Cloud or Azure, for your data and to run work there. Secrets stay in your keychain.",
    load: async () => ({ providers: (await api("/api/cloud/providers")).data ?? [] }),
    fields: [
      { key: "provider", label: "Cloud", kind: "choice", required: true, options: (ctx) => (ctx.providers ?? []).map((p) => ({ value: p.provider, label: { aws: "AWS", gcp: "Google Cloud", azure: "Azure" }[p.provider] ?? p.provider })) },
      { key: "auth", label: "Sign in with", kind: "choice", required: true, when: (v) => Boolean(v.provider),
        options: (ctx, v) => ((ctx.providers ?? []).find((p) => p.provider === v.provider)?.auth ?? []).map((a) => ({ value: a.mode, label: a.label })) },
      { key: "name", label: "Name", placeholder: "work-aws", required: true, help: "How you'll pick this login later." },
      { key: "region", label: "Region", placeholder: "us-west-1", optional: true },
      // The sign-in mode's own fields come from the services (cloud.providers), secrets masked.
      { key: "*auth-fields", kind: "dynamic", fields: (ctx, v) => ((ctx.providers ?? []).find((p) => p.provider === v.provider)?.auth ?? []).find((a) => a.mode === v.auth)?.fields ?? [] },
    ],
    submit: async (v, ctx) => {
      const spec = ((ctx.providers ?? []).find((p) => p.provider === v.provider)?.auth ?? []).find((a) => a.mode === v.auth)?.fields ?? [];
      if (((await api("/api/cloud/profiles")).data ?? []).some((p) => p.name === v.name)) return { ok: false, error: `You already have a cloud account named ${v.name}. Pick another name.` };
      const fields = {}, secrets = {};
      for (const f of spec) if (v[`af:${f.name}`]) (f.secret ? secrets : fields)[f.name] = v[`af:${f.name}`];
      const r = await api("/api/cloud/profiles", { body: { name: v.name, provider: v.provider, auth: v.auth, region: v.region || undefined, fields, secrets } });
      if (r.ok) await api(`/api/cloud/profiles/${encodeURIComponent(v.name)}/test`, { method: "POST", body: {}, timeout: 60 });
      return ok(r, `Connected ${v.name}.`);
    },
  },
  {
    id: "compute", label: "Add a place to run work", icon: "boxes",
    about: "Run backtests and serve candidates in your cloud instead of on this computer.",
    load: async () => ({ kinds: (await api("/api/compute/kinds")).data ?? [], profiles: (await api("/api/cloud/profiles")).data ?? [] }),
    fields: [
      { key: "kind", label: "Where", kind: "choice", required: true, options: (ctx) => (ctx.kinds ?? []).map((k) => ({ value: k.kind, label: k.label ?? k.kind, chip: k.provider })) },
      { key: "name", label: "Name", placeholder: "aws-batch", required: true },
      { key: "profile", label: "Cloud account", kind: "choice", optional: true, empty: "None yet. Connect a cloud account first, or leave this empty for this computer.", options: (ctx) => (ctx.profiles ?? []).map((p) => ({ value: p.name, label: p.name, chip: p.provider })) },
      { key: "region", label: "Region", optional: true, placeholder: "us-west-1" },
      { key: "*kind-fields", kind: "dynamic", fields: (ctx, v) => ((ctx.kinds ?? []).find((k) => k.kind === v.kind)?.fields ?? []) },
    ],
    submit: async (v, ctx) => {
      if (((await api("/api/compute")).data ?? []).some((t) => t.name === v.name)) return { ok: false, error: `There's already a place to run work named ${v.name}. Pick another name.` };
      const spec = (ctx.kinds ?? []).find((k) => k.kind === v.kind)?.fields ?? [];
      const config = {};
      for (const f of spec) if (v[`af:${f.name}`] !== undefined && v[`af:${f.name}`] !== "") config[f.name] = v[`af:${f.name}`];
      return ok(await api("/api/compute", { body: { name: v.name, kind: v.kind, profile: v.profile || undefined, region: v.region || undefined, config } }), `Added ${v.name}.`);
    },
  },
  {
    id: "handoff", label: "Hand off a candidate", icon: "send",
    about: "Check a version is ready and hand it to your own deployment process. Captain deploys nothing itself.",
    fields: [
      { key: "pipeline", label: "Candidate", kind: "choice", required: true, options: () => list("/api/pipelines", (p) => ({ value: `${p.name}@v${p.latest}`, label: p.name, chip: `v${p.latest}` })) },
      { key: "env", label: "To", kind: "choice", required: true, options: () => list("/api/environments", (e) => ({ value: e.name, label: e.name, chip: e.tier })) },
      { key: "reason", label: "If a readiness check fails, hand off anyway because", optional: true, help: "Leave empty to stop when a check fails. A reason is recorded with the handoff." },
    ],
    submit: async (v) => {
      const r = await api("/api/deploy/promote", { body: { pipeline: v.pipeline, env: v.env, confirm: true, override_reason: v.reason || undefined } });
      if (r.status === 409) {
        const failed = (r.data?.gate_results ?? []).filter((g) => !g.ok).map((g) => `${String(g.gate).replace(/_/g, " ")}: ${plain(g.detail ?? "failed")}`);
        return { ok: false, error: `Not ready. ${failed.join(". ")}` };
      }
      return ok(r, "Handed off. Record the deployment once your process has run it.");
    },
  },
  // Opened from a record (props.args.id), not listed in New.
  {
    id: "decision", label: "Record a decision", icon: "circle-check", hidden: true,
    about: "Only a person records the decision. Offline evidence gets the limitation \"not proof of online or profit lift\" added for you.",
    fields: [
      { key: "outcome", label: "Decision", kind: "choice", required: true,
        options: [{ value: "continue", label: "Continue" }, { value: "online-test", label: "Run an online test" }, { value: "reject", label: "Reject" }, { value: "inconclusive", label: "Inconclusive" }] },
      { key: "claim", label: "What the evidence shows", kind: "choice", required: true,
        options: [{ value: "offline-improvement", label: "Better offline" }, { value: "offline-regression", label: "Worse offline" }, { value: "no-detectable-change", label: "No detectable change" },
          { value: "online-lift", label: "Better online, on real traffic" }, { value: "none", label: "Nothing yet" }],
        help: "Better online needs results from real traffic attached to the runs." },
      { key: "interpretation", label: "What it means", kind: "long", required: true },
      { key: "limitations", label: "What it doesn't show", kind: "long", optional: true },
      { key: "next_step", label: "Next step", optional: true },
    ],
    submit: async (v, ctx, args) => ok(await api(`/api/investigations/${encodeURIComponent(args.id)}/decide`, { body: { outcome: v.outcome, claim: v.claim, interpretation: v.interpretation || undefined, limitations: v.limitations || undefined, next_step: v.next_step || undefined } }), "Decision recorded."),
  },
  {
    id: "evidence", label: "Attach evidence", icon: "link", hidden: true,
    about: "Point this investigation at what supports or questions it.",
    fields: [
      { key: "kind", label: "What it is", kind: "choice", required: true,
        options: ["dataset", "run", "slice", "query", "signal", "label", "replay", "handoff", "note"].map((k) => ({ value: k, label: k.charAt(0).toUpperCase() + k.slice(1) })) },
      { key: "ref", label: "Which one", required: true, when: (v) => v.kind !== "note", placeholder: "makro-catalog", help: "Its name. Add a version like @v3 to pin one." },
      { key: "note", label: "Why it matters", kind: "long", required: true, when: (v) => v.kind === "note" },
      { key: "note", label: "Why it matters", kind: "long", optional: true, when: (v) => v.kind !== "note" },
    ],
    submit: async (v, ctx, args) => ok(await api(`/api/investigations/${encodeURIComponent(args.id)}/evidence`, { body: { kind: v.kind, ref: v.ref || undefined, note: v.note || undefined } }), "Evidence attached."),
  },
  {
    id: "record-deployment", label: "Record a deployment", icon: "rocket", hidden: true,
    about: "Your deployment process ran this candidate. Recording it tells Captain what's live.",
    fields: [
      { key: "system", label: "Deployed with", kind: "choice", required: true, options: ["argo", "spinnaker", "sagemaker", "github-actions", "other"].map((s) => ({ value: s, label: { argo: "Argo", spinnaker: "Spinnaker", sagemaker: "SageMaker", "github-actions": "GitHub Actions", other: "Something else" }[s] })) },
      { key: "deployment_id", label: "Deployment id in that system", required: true },
      { key: "url", label: "Link to it", optional: true, placeholder: "https://argo.example.com/deployments/123" },
      { key: "live", label: "It's serving traffic now", kind: "toggle", default: true },
    ],
    submit: async (v, ctx, args) => ok(await api(`/api/handoffs/${encodeURIComponent(args.id)}/link`, { body: { system: v.system, deployment_id: v.deployment_id, url: v.url || undefined, live: Boolean(v.live) } }), "Deployment recorded."),
  },
  {
    id: "add-outcome", label: "Add an online result", icon: "chart-line", hidden: true,
    about: "A result from your experiment system. It counts as real-traffic evidence.",
    fields: [
      { key: "metric", label: "Metric", required: true, placeholder: "conversion rate" },
      { key: "value", label: "Change", required: true, placeholder: "0.012", help: "The measured lift as a number, e.g. 0.012 for +1.2%." },
      { key: "ci_low", label: "Interval low", optional: true },
      { key: "ci_high", label: "Interval high", optional: true },
      { key: "source", label: "Where it comes from", required: true, placeholder: "the Optimizely report for test 42" },
      { key: "experiment", label: "Experiment", optional: true },
    ],
    submit: async (v, ctx, args) => {
      const num = (x) => (x === undefined || x === "" ? undefined : Number(x));
      if (!Number.isFinite(num(v.value))) return { ok: false, error: "Change needs to be a number, like 0.012." };
      return ok(await api(`/api/handoffs/${encodeURIComponent(args.id)}/outcome`, { body: { metric: v.metric, value: num(v.value), ci_low: num(v.ci_low), ci_high: num(v.ci_high), source: v.source, experiment: v.experiment || undefined } }), "Result added.");
    },
  },
  {
    id: "demo", label: "Try the demo store", icon: "sparkles",
    about: "Fill this workspace with a small synthetic store: a catalog, queries, judgments and a baseline. Every number in it is simulated.",
    fields: [],
    submit: async () => ok(await run(["demo", "seed"], {}), "The demo store is ready."),
  },
];
