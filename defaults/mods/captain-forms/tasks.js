// CAPTAIN: the things a person makes in Captain, as tasks in plain words. The New menu,
// the palette and onboarding all open these. The CLI is for agents; nothing here shows
// a command name or a flag.
import { api, connectionsChanged } from "./kit/cst.js";
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
const CLOUDS = ["aws", "gcp", "azure"];
const kindOf = (ctx, v) => (ctx.kinds ?? []).find((k) => k.kind === v.kind);
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


// Datasets whose class plays this role (catalog, queries, judgments, events), newest version, as name plus chip.
async function byRole(role) {
  const [cl, ds] = await Promise.all([api("/api/dataset-classes"), api("/api/datasets")]);
  const roles = new Map((cl.ok ? cl.data?.classes ?? [] : []).map((c) => [c.name, c.role]));
  return (ds.ok && Array.isArray(ds.data) ? ds.data : [])
    .filter((d) => (d.classes ?? []).some((c) => roles.get(c) === role))
    .map((d) => ({ value: `${d.name}@v${d.latest}`, label: d.name, chip: `v${d.latest}` }));
}
const pipelines = () => list("/api/pipelines", (p) => ({ value: `${p.name}@v${p.latest}`, label: p.name, chip: `v${p.latest}` }));
const COLORS = [["#0043CA", "Blue"], ["#7C3AED", "Violet"], ["#059669", "Green"], ["#D97706", "Amber"], ["#DC2626", "Red"], ["#64748B", "Slate"]];

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
    about: "Snowflake, Databricks, BigQuery, Postgres and more, to read tables and import data with SQL. Passwords and tokens stay in your keychain.",
    // Every kind and its own fields come from the services (GET /api/connections/kinds), so a new kind needs no
    // change here.
    load: async () => ({ kinds: (await api("/api/connections/kinds")).data ?? [] }),
    fields: [
      { key: "kind", label: "Database", kind: "choice", grid: true, required: true,
        options: (ctx) => (ctx.kinds ?? []).map((k) => ({ value: k.kind, label: k.label, brand: k.icon })),
        help: (ctx, v) => {
          const k = (ctx.kinds ?? []).find((x) => x.kind === v.kind);
          if (!k) return null;
          if (k.installed === false) return `Captain can't reach ${k.label} on this computer yet: its driver isn't installed.`;
          return k.note ?? null;
        } },
      { key: "name", label: "Name", placeholder: "warehouse", required: true, when: (v) => Boolean(v.kind), help: "How you'll pick this database later." },
      { key: "*kind-fields", kind: "dynamic", when: (v) => Boolean(v.kind),
        fields: (ctx, v) => ((ctx.kinds ?? []).find((k) => k.kind === v.kind)?.fields ?? [])
          .map((f) => ({ ...f, key: `${v.kind}.${f.name}`, optional: !f.required })) },
      { key: "writable", label: "Allow writes", kind: "toggle", default: false, when: (v) => Boolean(v.kind), help: "Off keeps Captain read-only on this database." },
    ],
    submit: async (v, ctx) => {
      const kind = (ctx.kinds ?? []).find((k) => k.kind === v.kind);
      if (!kind) return { ok: false, error: "Pick a database." };
      const name = String(v.name ?? "").trim();
      // Submitting again after a failed test replaces what this form saved, and nothing else.
      if (ctx.saved !== name && ((await api("/api/connections")).data ?? []).some((c) => c.name === name)) {
        return { ok: false, error: `A database named ${name} is already connected. Pick another name.` };
      }
      const fields = {};
      for (const f of kind.fields ?? []) {
        const val = v[`af:${v.kind}.${f.name}`];
        if (val === undefined || val === "") continue;
        if (f.type === "secret") {
          // The keychain, and nowhere else (POST /api/secrets), under the name the services expect.
          const secret = `${name}_${f.name}`.toUpperCase().replace(/[^A-Z0-9_]/g, "_");
          const s = await api("/api/secrets", { body: { name: secret, value: String(val) } });
          if (!s.ok) return { ok: false, error: s.error ?? `Couldn't save the ${f.label.toLowerCase()} to your keychain.` };
          fields[`${f.name}_set`] = true;
        } else {
          fields[f.name] = f.type === "number" ? Number(val) : val;
        }
      }
      const r = await api("/api/connections", { body: { name, kind: v.kind, fields, writable: Boolean(v.writable), ...(ctx.saved === name ? { replace: true } : {}) } });
      if (!r.ok) return ok(r, "");
      ctx.saved = name;
      connectionsChanged(); // Tables, Notebooks and the import wizard relist now, not at their next start
      const t = await api(`/api/connections/${encodeURIComponent(name)}/test`, { body: {}, timeout: 60 });
      if (t.ok && t.data?.ok) return { ok: true, message: `Connected ${name}. It answered in ${Math.round(t.data.ms ?? 0)} ms.` };
      return { ok: false, error: `Saved ${name}, but Captain couldn't reach it: ${t.data?.error ?? t.error ?? "no answer"}. Fix the details and connect again.` };
    },
  },
  {
    id: "cloud", label: "Connect a cloud account", icon: "cloud",
    about: "AWS, Google Cloud or Azure, for your data and to run work there. Secrets stay in your keychain.",
    load: async () => ({ providers: (await api("/api/cloud/providers")).data ?? [] }),
    fields: [
      { key: "provider", label: "Cloud", kind: "choice", required: true, options: (ctx) => (ctx.providers ?? []).map((p) => ({ value: p.provider, label: { aws: "AWS", gcp: "Google Cloud", azure: "Azure" }[p.provider] ?? p.provider, brand: p.provider })) },
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
    about: "Run backtests and serve candidates on this computer or in your cloud.",
    // Every kind comes from the services (GET /api/compute/kinds), including a team's own from its captain/ folder.
    load: async () => ({ kinds: (await api("/api/compute/kinds")).data ?? [], profiles: (await api("/api/cloud/profiles")).data ?? [] }),
    fields: [
      { key: "kind", label: "Where", kind: "choice", grid: true, columns: 2, required: true,
        options: (ctx) => (ctx.kinds ?? []).map((k) => ({ value: k.kind, label: k.label ?? k.kind, brand: k.provider === "local" ? "local" : k.provider, chip: k.live_tested ? null : "not tested yet" })),
        help: (ctx, v) => {
          const k = (ctx.kinds ?? []).find((x) => x.kind === v.kind);
          if (!k) return null;
          return k.live_tested ? `Tested on a real account on ${k.live_tested}.` : "Not yet tested on a real account. It should work; tell us if it doesn't.";
        } },
      { key: "name", label: "Name", placeholder: "aws-batch", required: true, when: (v) => Boolean(v.kind), help: "How you'll pick this place later." },
      // The login comes from the kind's own cloud; this computer and Modal need none.
      { key: "profile", label: "Cloud login", kind: "choice", when: (v, ctx) => Boolean(v.kind) && CLOUDS.includes(kindOf(ctx, v)?.provider),
        empty: "None yet for this cloud. Connect a cloud account first, from Cloud and compute.",
        options: (ctx, v) => (ctx.profiles ?? []).filter((p) => p.provider === kindOf(ctx, v)?.provider).map((p) => ({ value: p.name, label: p.name, brand: p.provider })) },
      { key: "region", label: "Region", optional: true, placeholder: "us-west-1", when: (v, ctx) => Boolean(v.kind) && CLOUDS.includes(kindOf(ctx, v)?.provider) },
      { key: "*kind-fields", kind: "dynamic", when: (v) => Boolean(v.kind),
        fields: (ctx, v) => (kindOf(ctx, v)?.fields ?? []).map((f) => ({ ...f, key: `${v.kind}.${f.name}`, optional: !f.required })) },
    ],
    submit: async (v, ctx) => {
      const k = kindOf(ctx, v);
      if (!k) return { ok: false, error: "Pick where it runs." };
      if (CLOUDS.includes(k.provider) && !v.profile) return { ok: false, error: `${k.label} needs a cloud login. Connect one first, from Cloud and compute.` };
      if (((await api("/api/compute")).data ?? []).some((t) => t.name === v.name)) return { ok: false, error: `There's already a place to run work named ${v.name}. Pick another name.` };
      const config = {};
      for (const f of k.fields ?? []) { const val = v[`af:${v.kind}.${f.name}`]; if (val !== undefined && val !== "") config[f.name] = val; }
      const r = await api("/api/compute", { body: { name: v.name, kind: v.kind, profile: v.profile || undefined, region: v.region || undefined, config } });
      if (!r.ok) return ok(r, "");
      // Try it at once, and say what happened either way: it's saved, and Cloud and compute can test it again.
      const t = await api(`/api/compute/${encodeURIComponent(v.name)}/test`, { body: {}, timeout: 60 });
      if (t.ok && t.data?.ok) return { ok: true, message: `Added ${v.name}, and it answered.` };
      return { ok: true, message: `Added ${v.name}, but its test didn't pass: ${plain(t.data?.detail ?? t.data?.error ?? t.error ?? "no answer")}. Test it again from Cloud and compute once that's fixed.` };
    },
  },
  {
    // Its own screen (captain-rankings): a file, a check, an import. The New menu and the palette open it.
    id: "rankings", label: "Compare your own rankings", icon: "git-compare",
    about: "Import your own system's ranked results and compare them like any backtest.",
    opens: { mod: "captain-rankings", kind: "dialog", width: 760, height: 660 },
    fields: [],
  },
  {
    id: "dataset-class", label: "New dataset class", icon: "tag",
    about: "A class groups datasets by what they are for, and says which experiments can use them.",
    fields: [
      { key: "name", label: "Name", placeholder: "product-reviews", required: true, help: "Letters, digits and dashes." },
      { key: "description", label: "What it's for", kind: "long", required: true, placeholder: "Reviews shoppers left, used to find quality signals" },
      { key: "role", label: "Experiments can use it as", kind: "choice", default: "generic",
        options: async () => {
          const r = await api("/api/dataset-classes");
          const roles = r.ok ? r.data?.roles ?? [] : [];
          return [{ value: "generic", label: "Nothing in particular" }, ...roles.map((x) => ({ value: x, label: `${x[0].toUpperCase()}${x.slice(1)}` }))];
        } },
      { key: "color", label: "Color", kind: "choice", default: COLORS[0][0], options: COLORS.map(([value, label]) => ({ value, label })) },
      { key: "columns", label: "Columns every dataset in it must have", optional: true, placeholder: "id, title", help: "Separate with commas." },
    ],
    submit: async (v) => ok(await api("/api/dataset-classes", { body: {
      name: v.name.trim(), description: v.description.trim(), color: v.color, role: v.role === "generic" ? null : v.role,
      required_columns: String(v.columns ?? "").split(",").map((x) => x.trim()).filter(Boolean),
    } }), `Added the ${v.name.trim()} class.`),
  },
  {
    id: "backtest", label: "Run a backtest", icon: "flask-conical",
    about: "Compare two versions of your search on the same queries, and see whether the difference is real.",
    fields: [
      { key: "name", label: "Name", placeholder: "tuned-vs-current", required: true, help: "Letters, digits and dashes." },
      { key: "a", label: "Compare", kind: "choice", required: true, options: pipelines, empty: "No pipelines yet." },
      { key: "b", label: "With", kind: "choice", required: true, options: pipelines, empty: "No pipelines yet." },
      { key: "mode", label: "Shoppers", kind: "choice", default: "simulated",
        options: [{ value: "simulated", label: "Simulated shoppers" }, { value: "historic", label: "Replay logged searches" }] },
      { key: "queries", label: "Queries", kind: "choice", required: true, options: () => byRole("queries"), empty: "No query sets yet. Import one first." },
      { key: "judgments", label: "Judgments", kind: "choice", required: true, options: () => byRole("judgments"), empty: "No judgments yet. Import or grade some first." },
      { key: "logs", label: "Logged searches", kind: "choice", required: true, when: (v) => v.mode === "historic", options: () => byRole("events"), empty: "No logs yet. Import some first." },
      { key: "hypothesis", label: "What do you expect, and why", kind: "long", required: true, placeholder: "The tuned ranker finds more relevant products for long queries",
        help: "Written down before the run, so the result can't move the goalposts." },
      { key: "mde", label: "Smallest gain worth acting on", default: "0.003", help: "In nDCG@10. Smaller differences count as noise." },
    ],
    submit: async (v) => {
      if (v.a === v.b) return { ok: false, error: "Pick two different versions to compare." };
      const plan = await api("/api/backtests", { body: {
        name: v.name.trim(), arms: [v.a, v.b], queries: v.queries, judgments: v.judgments, hypothesis: v.hypothesis.trim(),
        primary: "ndcg@10", mde: Number(v.mde) || 0.003, mode: v.mode, logs: v.mode === "historic" ? v.logs : undefined,
      } });
      const planned = ok(plan, "");
      if (!planned.ok) return planned;
      return ok(await api(`/api/backtests/${v.name.trim().split("/").map(encodeURIComponent).join("/")}/run`, { body: {} }), "Started. Follow it in Runs.");
    },
  },
  {
    id: "grading", label: "Start grading", icon: "square-check",
    about: "Grade the products your search returns, so backtests have judgments people trust.",
    fields: [
      { key: "name", label: "Name", placeholder: "spring-grading", required: true, help: "Letters, digits and dashes." },
      { key: "queries", label: "Queries", kind: "choice", required: true, options: () => byRole("queries"), empty: "No query sets yet. Import one first." },
      { key: "a", label: "Results from", kind: "choice", required: true, options: pipelines, empty: "No pipelines yet." },
      { key: "b", label: "And from", kind: "choice", optional: true, options: pipelines },
      { key: "depth", label: "Results per query", default: "10" },
      { key: "sample", label: "How many queries", optional: true, placeholder: "all of them" },
    ],
    submit: async (v) => ok(await api("/api/grading", { body: {
      name: v.name.trim(), queries: v.queries, pipelines: [v.a, v.b].filter(Boolean).filter((x, i, a) => a.indexOf(x) === i).join(","),
      depth: Number(v.depth) || 10, ...(Number(v.sample) > 0 ? { sample: Number(v.sample) } : {}),
    } }), "Queue ready. Grade it in Runs, under Grading."),
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
