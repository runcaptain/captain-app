// captain-ui: the scope model (Edgar's spec, 2026-11-06), on the services' routes (backend/docs/api.md, "Scope model,
// engines and the Explorer"). A workspace holds scopes of types it defines (Brand and Site by default, any type may
// also sit at the top level); engines are search or recommendation systems linked to scopes; Projects span them.
//   GET /api/explorer       {workspace, model_revision, tree: [nodes], actions: [{type, label}]}
//     nodes: {kind: "view", id, label} | {kind: "group", type, label, children} | {kind: "scope", id, type, name,
//            label (the type's word), children} | {kind: "engine", id, name, type, pipelines} | {kind: "project", id,
//            name} | {kind: "settings", id: "scope-model", label}
//   GET/PUT /api/scope-model {revision, types: [{key, singular, plural, description, parents, fields}], explorer:
//            {order, hidden}, engine_types}; PUT takes expect_revision and is refused with reasons.
//   POST /api/scopes {type, name, parent?, fields?}; PUT /api/scopes/{id} {name?, parent? (null: top level), archived?}
//   POST /api/engines {name, type, scopes}; PUT /api/engines/{id} {name?, scopes?, archived?}
// The Explorer sets shared "captain.scope" = {kind: scope|engine|project|view, id, label}; the views filter to it.
// Until a workspace's services answer, the Explorer shows only what every workspace has. SAMPLE is the same shape,
// for tests (shared "captain.explorer.sample": true) and never shown otherwise.
import { shared } from "herdr";
import { api } from "./cst.js";

export const DEFAULT_MODEL = {
  revision: 1,
  types: [
    { key: "brand", singular: "Brand", plural: "Brands", description: "A business or company you build search for.", parents: [], fields: [] },
    { key: "site", singular: "Site", plural: "Sites", description: "A website or app. An identity, not just a hostname.", parents: ["brand"], fields: [] },
  ],
  explorer: { order: ["brand", "site"], hidden: [] },
  engine_types: ["search", "recommendation"],
};

const ENGINE_WORDS = { singular: "Engine", plural: "Engines", description: "A search or recommendation system you measure." };

/** Words for a type key (or "engine", "project"): singular, plural, "Add brand", description. */
export function words(model, key) {
  if (key === "engine") return { ...ENGINE_WORDS, add: "Add engine" };
  if (key === "project") return { singular: "Project", plural: "Projects", add: "Add project", description: "A workstream that can span engines and brands." };
  const t = ((model ?? DEFAULT_MODEL).types ?? []).find((x) => x.key === key);
  const singular = t?.singular ?? String(key ?? "");
  return { singular, plural: t?.plural ?? `${singular}s`, add: `Add ${singular.toLowerCase()}`, description: t?.description ?? "" };
}

/** Type keys that may be made inside a scope of type `parentType` (null: the top level), then "engine". */
export function childKinds(model, parentType) {
  const m = model ?? DEFAULT_MODEL;
  const order = m.explorer?.order ?? [];
  const types = (m.types ?? []).filter((t) => (parentType ? (t.parents ?? []).includes(parentType) : true));
  types.sort((a, b) => (order.indexOf(a.key) + 1 || 99) - (order.indexOf(b.key) + 1 || 99));
  return [...types.map((t) => t.key), "engine"];
}

/** True when letting type `key` sit inside type `parent` would make a loop. */
export function makesCycle(model, key, parent) {
  if (key === parent) return true;
  const parentsOf = (k) => ((model?.types ?? []).find((t) => t.key === k)?.parents ?? []);
  const seen = new Set();
  const walk = (k) => {
    if (k === key) return true;
    if (seen.has(k)) return false;
    seen.add(k);
    return parentsOf(k).some(walk);
  };
  return walk(parent);
}

/** The name a node shows: a scope's or engine's own name, else its label. */
export const nodeName = (n) => n.name ?? n.label ?? "";

/** A stable key for a node: groups have no id, so their type and place stand in. */
export const nodeKey = (n, path) => n.key ?? n.id ?? `${path}/${n.kind}:${n.type ?? ""}`;

/** A glyph for a node (kit/icons.js names). */
export function nodeIcon(n) {
  if (n.kind === "engine") return n.type === "recommendation" ? "sparkles" : "search";
  if (n.kind === "scope") return { brand: "tag", site: "monitor" }[n.type] ?? "layers";
  if (n.kind === "view") return n.id === "all-experiments" ? "flask-conical" : "boxes";
  return { group: "folder", project: "workflow", settings: "waypoints" }[n.kind] ?? "folder";
}

export const SAMPLE = {
  workspace: "Acme",
  model: DEFAULT_MODEL,
  model_revision: 1,
  actions: [{ type: "brand", label: "Add brand" }, { type: "site", label: "Add site" }, { type: "engine", label: "Add engine" }],
  tree: [
    { kind: "view", id: "all-engines", label: "All engines" },
    { kind: "view", id: "all-experiments", label: "All offline evaluations" },
    { kind: "group", type: "brand", label: "Brands", children: [
      { kind: "scope", id: "sc-acme-outdoors", type: "brand", name: "Acme Outdoors", label: "Brand", children: [
        { kind: "group", type: "site", label: "Sites", children: [
          { kind: "scope", id: "sc-us", type: "site", name: "US storefront", label: "Site", children: [
            { kind: "group", type: "engine", label: "Engines", children: [
              { kind: "engine", id: "en-product-search", name: "Product search", type: "search", pipelines: 2 },
              { kind: "engine", id: "en-recs", name: "Recommendations", type: "recommendation", pipelines: 1 },
            ] },
          ] },
        ] },
      ] },
    ] },
    { kind: "group", type: "project", label: "Projects", children: [{ kind: "project", id: "pr-session", name: "Session-aware ranking" }] },
    { kind: "settings", id: "scope-model", label: "Scope model" },
  ],
};

/** What every workspace has, before its services answer. */
export function baseTree() {
  return [
    { kind: "view", id: "all-engines", label: "All engines" },
    { kind: "view", id: "all-experiments", label: "All offline evaluations" },
    { kind: "group", type: "project", label: "Projects", children: [] },
    { kind: "settings", id: "scope-model", label: "Scope model" },
  ];
}

/** {tree, model, revision, actions, live, error}: the services' Explorer, else the base tree (SAMPLE for tests). */
export async function loadExplorer() {
  if (shared("captain.explorer.sample")) return { ...SAMPLE, revision: SAMPLE.model_revision, live: false };
  const [t, m] = await Promise.all([api("/api/explorer", { scoped: false }), api("/api/scope-model", { scoped: false })]);
  const model = m.ok && m.data ? m.data : DEFAULT_MODEL;
  if (t.ok && Array.isArray(t.data?.tree)) {
    return { tree: t.data.tree, model, revision: t.data.model_revision ?? model.revision, actions: t.data.actions ?? [], live: true };
  }
  return { tree: baseTree(), model, revision: model.revision, actions: [], live: false, error: t.status && t.status !== 404 ? t : null };
}

/** The Explorer's pick, or null for the whole workspace. */
export function currentScope() {
  return shared("captain.scope") ?? null;
}

/** The one place the instance actions reach the services.
 *  add {kind, type, name, parent, fields, engineType} | rename {node, name} | move {node, parent} | archive {node}.
 *  `node` is {kind: "scope" | "engine", id}; `parent` a scope id or null for the top level. */
export async function scopeAction(action, p) {
  // An existing node says what it is; only an add names its kind.
  const engine = p.node ? p.node.kind === "engine" : p.kind === "engine";
  const path = engine ? `/api/engines/${encodeURIComponent(p.node?.id ?? "")}` : `/api/scopes/${encodeURIComponent(p.node?.id ?? "")}`;
  if (action === "add") {
    return engine
      ? api("/api/engines", { body: { name: p.name, type: p.engineType ?? "search", scopes: p.parent ? [p.parent] : [] } })
      : api("/api/scopes", { body: { type: p.type, name: p.name, ...(p.parent ? { parent: p.parent } : {}), fields: p.fields ?? {} } });
  }
  if (action === "rename") return api(path, { method: "PUT", body: { name: p.name } });
  if (action === "move") return api(path, { method: "PUT", body: engine ? { scopes: p.parent ? [p.parent] : [] } : { parent: p.parent ?? null } });
  if (action === "archive") return api(path, { method: "PUT", body: { archived: true } });
  return { ok: false, error: `Unknown action ${action}.` };
}

/** Where a node may move: the top level (scopes may always sit there; engines then belong to no scope), and every
 *  scope its type allows (any scope for an engine), never the node itself or anything inside it. */
export function moveTargets(tree, model, node) {
  const allowed = node.kind === "engine" ? null : ((model?.types ?? []).find((t) => t.key === node.type)?.parents ?? []);
  const out = [{ id: null, label: "Top level" }];
  const walk = (n, path, inside) => {
    const here = inside || (n.kind === node.kind && n.id === node.id);
    if (!here && n.kind === "scope" && (allowed === null || allowed.includes(n.type))) out.push({ id: n.id, label: [...path, n.name].join(" / ") });
    const next = n.kind === "scope" ? [...path, n.name] : path;
    for (const ch of n.children ?? []) walk(ch, next, here);
  };
  for (const n of tree ?? []) walk(n, [], false);
  return out;
}
