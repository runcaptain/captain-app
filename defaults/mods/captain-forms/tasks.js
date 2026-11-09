// CAPTAIN: the things a person makes in Captain, as tasks in plain words. The New menu,
// the palette and onboarding all open these. The CLI is for agents; nothing here shows
// a command name or a flag.
import { api, connectionsChanged } from "./kit/cst.js";
import { exec, home, shared, setShared, openOverlay, run as runCommand } from "herdr";

const list = async (path, map, scoped = true) => {
  const r = await api(path, { scoped });
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
// A failed readiness check's fixing action: the task that makes the missing evidence, pre-filled.
export function fixFor(gates, pipeline) {
  const names = new Set(gates.map((g) => g.gate));
  const sharedLabels = gates.some((g) => g.gate === "evidence" && /own labeler/.test(String(g.detail ?? "")));
  if (names.has("backtest")) return { label: "Run a backtest", task: "backtest", values: { kind: "versions", b: pipeline } };
  if (names.has("evidence") || names.has("complete"))
    return { label: sharedLabels ? "Grade the holdout by hand" : "Start grading", task: "grading", values: { a: pipeline } };
  if (names.has("primary_metric")) return { label: "Run a backtest", task: "backtest", values: { kind: "versions", b: pipeline } };
  if (names.has("human_approval")) return { label: "Open Investigations", section: "investigations" };
  return null;
}
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
// A project's inputs: each slot's title and the dataset kinds it takes (project_state.SLOTS).
const SLOT_WORDS = { catalog: ["Catalog", ["catalog"]], events: ["Events", ["events", "logs"]], queries: ["Queries", ["queries"]],
  labels: ["Labels", ["judgments"]], holdout: ["Sealed holdout", ["queries"]] };
const IMPORT_NOW = "__import__";
/** Datasets of these kinds, by kind or by their class's role, across the workspace (not the Explorer's pick). */
async function datasetsOf(kinds) {
  const [cl, ds] = await Promise.all([api("/api/dataset-classes", { scoped: false }), api("/api/datasets", { scoped: false })]);
  const roles = new Map((cl.ok ? cl.data?.classes ?? [] : []).map((c) => [c.name, c.role]));
  return (ds.ok && Array.isArray(ds.data) ? ds.data : [])
    .filter((d) => kinds.includes(d.kind) || (d.classes ?? []).some((c) => kinds.includes(roles.get(c))))
    .map((d) => ({ value: `${d.name}@v${d.latest}`, label: d.name, chip: `v${d.latest}` }));
}
/** Wait for a job the services started (an import, a publish), up to two minutes. */
async function waitJob(id) {
  if (!id) return { ok: false, error: "It didn't start." };
  for (let i = 0; i < 240; i++) {
    const r = await api(`/api/jobs/${encodeURIComponent(id)}`, { scoped: false });
    if (r.ok && r.data?.status === "done") return { ok: true, result: r.data.result };
    if (r.ok && r.data?.status === "failed") return { ok: false, error: plain(r.data.message) };
    try { await exec("sleep", ["0.5"]); } catch { break; }
  }
  return { ok: false, error: "It's still running. Check Runs, Jobs, in a minute." };
}
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
    id: "project", label: "New project", icon: "workflow",
    about: "A project holds one search you're improving: its catalog, the labels that grade it, the experiments you train and the champion you ship. Nothing moves or is copied.",
    fields: [
      { key: "name", label: "Name", placeholder: "Outdoor store search", required: true },
      { key: "description", label: "What it's for", optional: true, placeholder: "Product search for the outdoor storefront" },
      { key: "catalog", label: "Catalog", kind: "choice", optional: true, options: async () => [...await datasetsOf(["catalog"]), { value: IMPORT_NOW, label: "Import one now" }],
        help: "The products this search ranks. It's pinned to its exact version, and you can change it later." },
    ],
    submit: async (v) => {
      const r = await api("/api/projects", { body: { name: v.name.trim(), ...(v.description ? { description: v.description.trim() } : {}) } });
      const made = ok(r, "");
      if (!made.ok) return made;
      const id = r.data?.id;
      setShared("captain.explorer.rev", Date.now()); // the Explorer lists it now
      setShared("captain.scope", { kind: "project", id, label: r.data?.name ?? v.name.trim() });
      runCommand("layout.select", { stack: "sections", child: "project" });
      if (v.catalog === IMPORT_NOW) {
        openOverlay({ mod: "captain-import", kind: "dialog", width: 900, height: 640, props: { project: id } });
        return { ok: true, message: `Project ${v.name.trim()} made. Import its catalog, then choose it in Inputs.` };
      }
      if (v.catalog) {
        const p = await api(`/api/projects/${encodeURIComponent(id)}/pin`, { body: { catalog: v.catalog }, scoped: false });
        if (!p.ok) return { ok: true, message: `Project ${v.name.trim()} made, but its catalog wasn't pinned: ${plain(p.data?.error ?? p.error)}` };
      }
      return { ok: true, message: `Project ${v.name.trim()} made.` };
    },
  },
  // ---- a project's own tasks: opened from its page with its values preset (project, pins, champion, registry) ----
  {
    id: "pin", label: "Choose an input", icon: "tag", hidden: true,
    about: "Pin the project to an exact version. Every experiment trains and is judged on what's pinned, so results stay comparable.",
    fields: [
      { key: "project", label: "Project", kind: "choice", required: true, when: (_v, ctx) => Boolean(ctx.args?.pickProject),
        options: () => list("/api/projects", (p) => ({ value: p.id, label: p.name }), false), empty: "No projects yet. Make one with New project." },
      { key: "slot", label: "Input", kind: "choice", required: true, options: Object.entries(SLOT_WORDS).map(([value, [label]]) => ({ value, label })) },
      { key: "ref", label: "Version", kind: "choice", required: true, options: (_ctx, v) => datasetsOf(SLOT_WORDS[v.slot]?.[1] ?? []), empty: "Nothing of this kind yet. Import one first.",
        help: (_ctx, v) => (v.slot === "holdout" ? "A query set that isn't sealed yet gets sealed now: half its queries are kept back for judging, and no model trains on them." : v.slot === "labels" ? "These grade the sealed holdout. A model trained on the same labels can only show it agrees with them." : null) },
    ],
    submit: async (v) => {
      const pin = (ref) => api(`/api/projects/${encodeURIComponent(v.project)}/pin`, { body: { [v.slot]: ref }, scoped: false });
      let r = await pin(v.ref);
      if (!r.ok && v.slot === "holdout" && /isn't sealed/.test(String(r.data?.error ?? ""))) {
        const sealed = await api("/api/queries/seal", { body: { ref: v.ref, holdout: 0.5 }, scoped: false });
        if (!sealed.ok) return ok(sealed, "");
        r = await pin(`${sealed.data.name}@v${sealed.data.version}`);
      }
      return ok(r, `${SLOT_WORDS[v.slot]?.[0] ?? "Input"} pinned.`);
    },
  },
  {
    id: "experiment", label: "New experiment", icon: "flask-conical", hidden: true,
    about: "Train a new version of a ranker on the project's pinned inputs. It's judged against the champion once it lands.",
    load: async (v) => {
      if (!v?.base) return {};
      const r = await api(`/api/pipelines/${(v.base).split("/").map(encodeURIComponent).join("/")}`, { scoped: false });
      return r.ok ? { pipeline: r.data, defaults: { command: r.data?.spec?.train?.command ?? "" } } : {};
    },
    fields: [
      { key: "base", label: "Ranker", kind: "choice", required: true, options: () => list("/api/pipelines", (p) => ({ value: p.name, label: p.name, chip: `v${p.latest}` })),
        help: "The new version keeps its search setup and changes only what training changes." },
      { key: "command", label: "Training command", required: true, placeholder: "python train.py",
        help: "Runs on this computer, in a fresh folder. It gets the exact inputs, the params and a folder to write the trained model into." },
      { key: "labels", label: "Training labels", kind: "choice", required: true, options: () => datasetsOf(["judgments"]),
        help: (_ctx, v) => (v.labels && v.labels === v.pins?.labels ? "These are the labels that grade the holdout too, so the result can only say it agrees with its own labeler. Pick labels from another source to get a real verdict." : "Labels from another source than the holdout's give a verdict you can trust.") },
      { key: "params", label: "Params", kind: "long", optional: true, placeholder: "learning_rate = 0.05", help: "One per line, name = value." },
      { key: "as_of", label: "Train on data before", optional: true, placeholder: "2026-10-01", help: "A date. Leave empty to use everything pinned." },
    ],
    submit: async (v, ctx) => {
      if (!v.project) return { ok: false, error: "Open this from a project." };
      const name = v.base;
      const cur = ctx.pipeline && ctx.pipeline.name === name ? ctx.pipeline : (await api(`/api/pipelines/${name.split("/").map(encodeURIComponent).join("/")}`, { scoped: false })).data;
      if (!cur?.spec) return { ok: false, error: `Couldn't read ${name}.` };
      const train = cur.spec.train ?? {};
      if (String(train.command ?? "") !== v.command.trim()) {
        // A new training command is a new version of the ranker's setup, so the old one stays reproducible.
        const draft = { name, engine: cur.engine, catalog: cur.catalog ?? undefined, description: `training command set from the project page`,
          spec: { ...cur.spec, train: { ...train, command: v.command.trim() } }, registry_ref: cur.registry_ref ?? undefined };
        const pub = await api("/api/pipelines", { body: draft, scoped: false });
        if (!pub.ok) return ok(pub, "");
      }
      const pins = v.pins ?? {};
      const holdout = pins.holdout ? pins.holdout.replace(/:holdout$/, ":train") : null;
      const inputs = [pins.catalog && `catalog=${pins.catalog}`, pins.events && `events=${pins.events}`,
        holdout ? `queries=${holdout}` : pins.queries && `queries=${pins.queries}`, `labels=${v.labels}`].filter(Boolean);
      const params = {};
      for (const line of String(v.params ?? "").split("\n")) {
        const m = line.match(/^\s*([^=\s]+)\s*=\s*(.*?)\s*$/);
        if (m) params[m[1]] = /^-?\d+(\.\d+)?$/.test(m[2]) ? Number(m[2]) : m[2];
      }
      const started = await api(`/api/pipelines/${name.split("/").map(encodeURIComponent).join("/")}/retrain`, { body: { inputs, params, as_of: v.as_of || undefined, project: v.project }, scoped: false });
      if (started.ok && started.data?.live) {
        setShared("captain.open", { section: "runs", live: started.data.live, at: Date.now() });
        runCommand("layout.select", { stack: "sections", child: "runs" });
      }
      return ok(started, started.data?.live ? "Training. It's open in Runs, live, and joins the project's experiments when it lands." : "Training. It joins the project's experiments when it lands.");
    },
  },
  {
    id: "import-version", label: "Import a model version", icon: "upload", hidden: true,
    about: "Bring a model trained in MLflow or your own registry into the project as an experiment, with its metrics and where it came from.",
    fields: [
      { key: "pipeline", label: "Ranker it becomes a version of", kind: "choice", required: true, options: () => list("/api/pipelines", (p) => ({ value: p.name, label: p.name, chip: `v${p.latest}` })),
        help: "Its search setup is copied from that ranker's newest version." },
      { key: "from", label: "Where it was trained", kind: "choice", required: true, default: "mlflow",
        options: [{ value: "mlflow", label: "An MLflow model version" }, { value: "mlflow_run", label: "An MLflow run" }, { value: "registry_path", label: "A model registry folder" }] },
      { key: "source", label: "Which one", required: true, placeholder: "ranker@4",
        help: (_ctx, v) => (v.from === "mlflow_run" ? "The run's id." : v.from === "registry_path" ? "Architecture, brand and version, like arch-v2/outdoor/v3." : "The registered model's name and version, like ranker@4.") },
      { key: "labels", label: "Labels it was trained on", kind: "choice", optional: true, options: () => datasetsOf(["judgments"]),
        help: "Optional. Without it, the experiment's label independence reads unknown." },
    ],
    submit: async (v) => {
      const r = await api("/api/models/import-version", { body: { pipeline: v.pipeline, [v.from]: v.source.trim() }, scoped: false });
      if (!r.ok) return ok(r, "");
      const job = await waitJob(r.data?.job);
      if (!job.ok) return job;
      const ref = job.result?.pipeline;
      if (v.project && ref) {
        const e = await api(`/api/projects/${encodeURIComponent(v.project)}/experiments`, { body: { pipeline: ref, labels: v.labels || undefined, trainer: `imported from ${v.from === "registry_path" ? "a registry" : "MLflow"}` }, scoped: false });
        if (!e.ok) return { ok: true, message: `Imported ${ref}, but it wasn't added to the project: ${plain(e.data?.error ?? e.error)}` };
      }
      return { ok: true, message: `Imported ${ref ?? "the version"}${v.project ? " as an experiment" : ""}.` };
    },
  },
  {
    id: "publish", label: "Publish to the registry", icon: "package", hidden: true,
    about: "Write this version to your model registry as a new write-once version, with its weights, code, inputs and results. Nothing already there changes.",
    fields: [
      { key: "pipeline", label: "Version", kind: "choice", required: true, options: (_ctx, v) => [{ value: v.pipeline, label: v.pipeline }].filter((o) => o.value) },
      { key: "arch", label: "Architecture", required: true, placeholder: "arch-v2", help: "The registry folder for this kind of model. Asked once, then kept on the project." },
      { key: "brand", label: "Brand", required: true, placeholder: "outdoor" },
      { key: "registry", label: "Registry", optional: true, placeholder: "s3://your-models-bucket", help: "Leave empty to use the registry this workspace already has." },
    ],
    submit: async (v) => {
      if (v.project) {
        const saved = await api(`/api/projects/${encodeURIComponent(v.project)}/pin`, { body: { arch: v.arch.trim(), brand: v.brand.trim(), ...(v.registry ? { registry: v.registry.trim() } : {}) }, scoped: false });
        if (!saved.ok) return ok(saved, "");
      }
      const r = await api("/api/models/publish", { body: { pipeline: v.pipeline, arch: v.arch.trim(), brand: v.brand.trim(), registry: v.registry?.trim() || undefined }, scoped: false });
      return ok(r, `Publishing ${v.pipeline}. It shows in Data and signals, Models, when it's done.`);
    },
  },
  {
    id: "champion", label: "Make champion", icon: "trophy", hidden: true,
    about: "The champion is the version a project ships. Every experiment is judged against it on the project's sealed holdout.",
    fields: [
      { key: "pipeline", label: "Version", kind: "choice", required: true, options: (_ctx, v) => [{ value: v.pipeline, label: v.pipeline, chip: v.evidence }].filter((o) => o.value) },
      { key: "project", label: "Project", kind: "choice", required: true, options: () => list("/api/projects", (p) => ({ value: p.id, label: p.name }), false) },
    ],
    submit: async (v) => {
      const r = await api(`/api/projects/${encodeURIComponent(v.project)}/champion`, { body: { pipeline: v.pipeline }, scoped: false });
      if (r.ok) setShared("captain.explorer.rev", Date.now());
      return ok(r, `${v.pipeline} is the champion now.`);
    },
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
    // MLflow, read-only (backend/docs/api.md, "MLflow bridge"): a Databricks connection's managed MLflow, or any
    // tracking server. Saved, then read once, so a refused login says so here, in the services' words.
    id: "mlflow", label: "Connect MLflow", icon: "flask-conical",
    about: "See your MLflow experiments, runs and registered models in Captain, read-only, and draw them into lineage. Captain never changes anything in MLflow.",
    load: async () => ({ conns: ((await api("/api/connections")).data ?? []).filter((k) => (k.engine ?? k.kind) === "databricks") }),
    fields: [
      { key: "from", label: "Read it from", kind: "choice", default: "databricks", required: true,
        options: [{ value: "databricks", label: "A Databricks connection" }, { value: "uri", label: "A tracking server" }] },
      { key: "conn", label: "Databricks connection", kind: "choice", required: true, when: (v) => v.from === "databricks",
        options: (ctx) => (ctx.conns ?? []).map((k) => ({ value: k.name, label: k.name, brand: "databricks" })),
        empty: "No Databricks connection yet. Connect one first, or use a tracking server.",
        help: "Its host and token are reused. The token needs the mlflow scope." },
      { key: "uri", label: "Tracking server", placeholder: "https://mlflow.example.com", required: true, when: (v) => v.from === "uri" },
      { key: "token", label: "Token", kind: "secret", optional: true, when: (v) => v.from === "uri", help: "Kept in your keychain. Leave it empty if the server needs no login." },
    ],
    submit: async (v) => {
      let tokenSecret;
      if (v.from === "uri" && v.token) {
        tokenSecret = "MLFLOW_TOKEN";
        const s = await api("/api/secrets", { body: { name: tokenSecret, value: String(v.token) } });
        if (!s.ok) return { ok: false, error: "Couldn't save the token to your keychain." };
      }
      const r = await api("/api/mlflow", { method: "PUT", body: v.from === "databricks" ? { databricks_connection: v.conn } : { uri: String(v.uri).trim(), token_secret: tokenSecret } });
      if (!r.ok) return ok(r, "");
      setShared("captain.mlflow.changed", Date.now()); // an open MLflow list reads again
      const t = await api("/api/mlflow/experiments", { timeout: 60 });
      if (t.ok) return { ok: true, message: `Connected MLflow: ${t.data.length} experiment${t.data.length === 1 ? "" : "s"}.` };
      return { ok: false, error: `Saved, but Captain couldn't read MLflow. ${plain(t.data?.error ?? t.error)}` };
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
    about: "Compare two versions of your search on the same queries, or try other settings of one version, and see whether the difference is real.",
    fields: [
      { key: "kind", label: "What to test", kind: "choice", default: "versions",
        options: [{ value: "versions", label: "Two versions" }, { value: "settings", label: "Other settings of one version" }] },
      { key: "name", label: "Name", placeholder: "tuned-vs-current", required: true, help: "Letters, digits and dashes.", when: (v) => v.kind !== "settings" },
      { key: "a", label: "Compare", kind: "choice", required: true, options: pipelines, empty: "No pipelines yet.", when: (v) => v.kind !== "settings" },
      { key: "b", label: "With", kind: "choice", required: true, options: pipelines, empty: "No pipelines yet.", when: (v) => v.kind !== "settings" },
      { key: "pipeline", label: "Version", kind: "choice", required: true, options: pipelines, empty: "No pipelines yet.", when: (v) => v.kind === "settings",
        help: "Captain tries other values of its numeric settings, a quick pass on a sample of queries first." },
      { key: "mode", label: "Shoppers", kind: "choice", default: "simulated", when: (v) => v.kind !== "settings",
        options: [{ value: "simulated", label: "Simulated shoppers" }, { value: "historic", label: "Replay logged searches" }] },
      { key: "queries", label: "Queries", kind: "choice", required: true, options: () => byRole("queries"), empty: "No query sets yet. Import one first." },
      { key: "judgments", label: "Judgments", kind: "choice", required: true, options: () => byRole("judgments"), empty: "No judgments yet. Import or grade some first." },
      { key: "logs", label: "Logged searches", kind: "choice", required: true, when: (v) => v.mode === "historic", options: () => byRole("events"), empty: "No logs yet. Import some first." },
      { key: "hypothesis", label: "What do you expect, and why", kind: "long", required: true, placeholder: "The tuned ranker finds more relevant products for long queries",
        help: "Written down before the run, so the result can't move the goalposts.", when: (v) => v.kind !== "settings" },
      { key: "mde", label: "Smallest gain worth acting on", default: "0.003", help: "In nDCG@10. Smaller differences count as noise.", when: (v) => v.kind !== "settings" },
    ],
    submit: async (v) => {
      if (v.kind === "settings") {
        openOverlay({ mod: "captain-sandbox", kind: "dialog", width: 0.9, height: 0.9, props: { pipeline: v.pipeline, queries: v.queries, judgments: v.judgments } });
        return { ok: true, message: `Trying settings of ${String(v.pipeline).split("@")[0]}` };
      }
      if (v.a === v.b) return { ok: false, error: "Pick two different versions to compare." };
      const plan = await api("/api/backtests", { body: {
        name: v.name.trim(), arms: [v.a, v.b], queries: v.queries, judgments: v.judgments, hypothesis: v.hypothesis.trim(),
        primary: "ndcg@10", mde: Number(v.mde) || 0.003, mode: v.mode, logs: v.mode === "historic" ? v.logs : undefined,
      } });
      const planned = ok(plan, "");
      if (!planned.ok) return planned;
      const started = await api(`/api/backtests/${v.name.trim().split("/").map(encodeURIComponent).join("/")}/run`, { body: {} });
      // Watch it as it goes: Runs opens on its live view.
      if (started.ok && started.data?.live) {
        setShared("captain.open", { section: "runs", live: started.data.live, at: Date.now() });
        runCommand("layout.select", { stack: "sections", child: "runs" });
      }
      return ok(started, started.data?.live ? "Started. It's open in Runs, live." : "Started. Follow it in Runs.");
    },
  },
  {
    id: "grading", label: "Start grading", icon: "square-check",
    about: "Grade the products your search returns, so backtests have judgments people trust.",
    // The team's rubrics (captain/rubrics/<domain>.md): graders see the same words beside each pair.
    load: async () => ({ rubrics: (await api("/api/rubrics")).data ?? [] }),
    fields: [
      { key: "name", label: "Name", placeholder: "spring-grading", required: true, help: "Letters, digits and dashes." },
      { key: "queries", label: "Queries", kind: "choice", required: true, options: () => byRole("queries"), empty: "No query sets yet. Import one first." },
      { key: "a", label: "Results from", kind: "choice", required: true, options: pipelines, empty: "No pipelines yet." },
      { key: "b", label: "And from", kind: "choice", optional: true, options: pipelines },
      { key: "depth", label: "Results per query", default: "10" },
      { key: "sample", label: "How many queries", optional: true, placeholder: "all of them" },
      { key: "rubric", label: "Rubric", kind: "choice", optional: true,
        options: (ctx) => (ctx.rubrics ?? []).map((r) => ({ value: r.name, label: r.name })),
        empty: "No rubrics yet. A rubric is captain/rubrics/<domain>.md in your project: what makes a result relevant here.",
        help: (ctx, v) => { const r = (ctx.rubrics ?? []).find((x) => x.name === v.rubric); return r ? `${String(r.preview ?? "").split("\n").find((l) => l.trim() && !l.startsWith("#")) ?? ""}` : null; } },
    ],
    submit: async (v) => ok(await api("/api/grading", { body: {
      name: v.name.trim(), queries: v.queries, pipelines: [v.a, v.b].filter(Boolean).filter((x, i, a) => a.indexOf(x) === i).join(","),
      depth: Number(v.depth) || 10, ...(Number(v.sample) > 0 ? { sample: Number(v.sample) } : {}), ...(v.rubric ? { rubric: v.rubric } : {}),
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
        const gates = (r.data?.gate_results ?? []).filter((g) => !g.ok);
        const failed = gates.map((g) => `${String(g.gate).replace(/_/g, " ")}: ${plain(g.detail ?? "failed")}`);
        return { ok: false, error: `Not ready. ${failed.join(". ")}`, next: fixFor(gates, v.pipeline) };
      }
      return ok(r, "Handed off. Record the deployment once your process has run it.");
    },
  },
  // Opened from a record (props.args.id), not listed in New.
  {
    id: "decision", label: "Record a decision", icon: "circle-check", hidden: true,
    about: "Only a person records the decision. Offline evidence gets the limitation \"not proof of online or profit lift\" added for you.",
    fields: [
      { key: "investigation", label: "Investigation", kind: "choice", required: true, when: (_v, ctx) => !ctx.args?.id,
        options: () => list("/api/investigations", (i) => ({ value: i.id, label: i.title ?? i.id, chip: i.state })),
        empty: "No investigations yet. Open one first: a decision answers its question.",
        help: (_ctx, v) => (v.run ? `The decision is about ${v.run}.` : null) },
      { key: "outcome", label: "Decision", kind: "choice", required: true,
        options: [{ value: "continue", label: "Continue" }, { value: "online-test", label: "Run an online test" }, { value: "reject", label: "Reject" }, { value: "inconclusive", label: "Inconclusive" }] },
      { key: "claim", label: "What the evidence shows", kind: "choice", required: true,
        options: [{ value: "offline-improvement", label: "Better offline" }, { value: "offline-regression", label: "Worse offline" }, { value: "no-detectable-change", label: "No detectable change" },
          { value: "online-lift", label: "Better online, on real traffic" }, { value: "none", label: "Nothing yet" }],
        help: "Better online needs results from real traffic attached to the runs." },
      { key: "interpretation", label: "What it means", kind: "long", required: true },
      { key: "limitations", label: "What it doesn't show", kind: "long", optional: true },
      { key: "next_step", label: "Next step", required: true, placeholder: "Hand it off to staging", help: "Needed, so someone else can act on the decision." },
    ],
    submit: async (v, ctx, args) => {
      const id = args.id ?? v.investigation;
      if (v.run && !args.id) await api(`/api/investigations/${encodeURIComponent(id)}/evidence`, { body: { kind: "run", ref: v.run }, scoped: false });
      const r = await api(`/api/investigations/${encodeURIComponent(id)}/decide`, { body: { outcome: v.outcome, claim: v.claim, interpretation: v.interpretation || undefined, limitations: v.limitations || undefined, next_step: v.next_step || undefined, ...(v.run ? { runs: [v.run] } : {}) } });
      const done = ok(r, "Decision recorded.");
      // A decision to go on is followed by its handoff.
      if (done.ok && ["continue", "online-test"].includes(v.outcome)) done.next = { label: "Hand off a candidate", task: "handoff", values: v.candidate ? { pipeline: v.candidate } : {} };
      return done;
    },
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
    id: "demo", label: "Load Sample North Pole", icon: "sparkles",
    about: "Open a separate sample workspace with three catalogs, six lists, search queries and illustrative manual judgments. Prices, stock and specifications are synthetic. Your current data stays separate.",
    fields: [],
    submit: async () => {
      const r = await run(["demo", "seed"], {});
      const result = ok(r, "Sample North Pole is ready.");
      const name = r.data?.output?.workspace;
      if (result.ok && name) {
        const selected = await api(`/api/workspaces/${encodeURIComponent(name)}/use`, { method: "POST" });
        if (!selected.ok) return { ok: false, error: "Sample data loaded, but the workspace could not be selected. Choose sample-north-pole in the workspace switcher." };
        setShared("captain.workspace", name);
        connectionsChanged();
      }
      return result;
    },
  },
  {
    id: "remove-sample", label: "Remove Sample North Pole", icon: "trash-2",
    about: "Delete the Sample North Pole workspace. Added or edited files prevent removal so your work is preserved. You can load a fresh sample later.",
    fields: [],
    submit: async () => {
      const r = await run(["demo", "remove-sample"], {});
      const result = ok(r, "Sample North Pole removed.");
      if (result.ok && shared("captain.workspace") === "sample-north-pole") {
        const current = r.data?.output?.current;
        if (current) {
          const selected = await api(`/api/workspaces/${encodeURIComponent(current)}/use`, { method: "POST" });
          if (!selected.ok) return { ok: false, error: "Sample removed. Select another workspace in the workspace switcher." };
        }
        setShared("captain.workspace", current ?? null);
        connectionsChanged();
      }
      return result;
    },
  },
];
