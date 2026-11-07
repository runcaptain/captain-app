// captain-ui: the scope model (Edgar's spec, 2026-11-06). A workspace holds scopes: Brand and Site by default, plus
// custom types, arranged by allowed parents; engines hang off any scope (or the workspace root, since Brand and Site
// are optional), and Projects span engines and brands. The Explorer in captain-nav draws the tree, the section mods
// filter to shared "captain.scope" = {kind, id, label}, and captain-scope-model edits the model.
//
// Routes (the services): GET /api/explorer (the tree, stable ids), GET/PUT /api/scope-model, and the instance actions.
// Until a workspace's services have them, the Explorer shows only what every workspace has. SAMPLE is the same shape,
// for tests (shared "captain.explorer.sample": true) and never shown otherwise.
import { shared } from "herdr";
import { api } from "./cst.js";

/** The scope model: types, their words, who may hold whom, and how the Explorer groups them. */
export const DEFAULT_MODEL = {
  types: [
    { id: "brand", singular: "Brand", plural: "Brands", description: "A business or label with its own catalog.", parents: ["workspace"], builtin: true },
    { id: "site", singular: "Site", plural: "Sites", description: "A storefront, app or market a brand runs.", parents: ["workspace", "brand"], builtin: true },
  ],
  // Engines are always available and may sit under any scope type, or at the root.
  engine: { singular: "Engine", plural: "Engines", description: "A search or recommendation system you measure.", parents: ["workspace", "brand", "site"] },
  explorer: { order: ["brand", "site", "engine"], hidden: [] },
  fields: [],
};

/** Words for a kind: "Brand", "Brands", "Add brand". Unknown kinds read as themselves. */
export function words(model, kind) {
  const m = model ?? DEFAULT_MODEL;
  const t = kind === "engine" ? m.engine : (m.types ?? []).find((x) => x.id === kind);
  const singular = t?.singular ?? String(kind ?? "");
  return { singular, plural: t?.plural ?? `${singular}s`, add: `Add ${singular.toLowerCase()}`, description: t?.description ?? "" };
}

/** The kinds that may be made under a node of `kind` ("workspace" for the root), in the Explorer's order. */
export function childKinds(model, kind) {
  const m = model ?? DEFAULT_MODEL;
  const order = m.explorer?.order ?? [];
  const all = [...(m.types ?? []).map((t) => ({ id: t.id, parents: t.parents ?? [] })), { id: "engine", parents: m.engine?.parents ?? [] }];
  return all.filter((t) => t.parents.includes(kind))
    .sort((a, b) => (order.indexOf(a.id) + 1 || 99) - (order.indexOf(b.id) + 1 || 99))
    .map((t) => t.id);
}

/** True when letting `kind` sit under `parent` would make a loop in the hierarchy. */
export function makesCycle(model, kind, parent) {
  if (kind === parent) return true;
  const parentsOf = (k) => (k === "engine" ? [] : ((model?.types ?? []).find((t) => t.id === k)?.parents ?? []));
  const seen = new Set();
  const walk = (k) => {
    if (k === kind) return true;
    if (seen.has(k) || k === "workspace") return false;
    seen.add(k);
    return parentsOf(k).some(walk);
  };
  return walk(parent);
}

/** Where a node goes: a nav icon for the fixed nodes; scope types draw a glyph, engines their system's mark. */
export function nodeIcon(n) {
  return { workspace: "layers", "all-engines": "boxes", "all-experiments": "flask-conical", group: "folder", project: "workflow",
    settings: "settings", "scope-model": "waypoints", engine: "search", brand: "tag", site: "monitor" }[n.kind] ?? "folder";
}

export const SAMPLE = {
  model: DEFAULT_MODEL,
  tree: {
    id: "ws", kind: "workspace", label: "Acme", children: [
      { id: "all-engines", kind: "all-engines", label: "All engines" },
      { id: "all-experiments", kind: "all-experiments", label: "All experiments" },
      { id: "g-brand", kind: "group", of: "brand", label: "Brands", children: [
        { id: "b-acme-outdoors", kind: "brand", label: "Acme Outdoors", children: [
          { id: "g-site-1", kind: "group", of: "site", label: "Sites", children: [
            { id: "s-us", kind: "site", label: "US storefront", children: [
              { id: "g-engine-1", kind: "group", of: "engine", label: "Engines", children: [
                { id: "e-product-search", kind: "engine", label: "Product search", system: "elasticsearch" },
                { id: "e-recs", kind: "engine", label: "Recommendations" },
              ] },
            ] },
          ] },
        ] },
      ] },
      { id: "g-project", kind: "group", of: "project", label: "Projects", children: [
        { id: "p-session", kind: "project", label: "Session-aware ranking" },
      ] },
      { id: "settings", kind: "settings", label: "Settings", children: [
        { id: "scope-model", kind: "scope-model", label: "Scope model" },
      ] },
    ],
  },
};

/** What every workspace has, before its services know any scopes. */
export function baseTree(name) {
  return {
    id: "ws", kind: "workspace", label: name ?? "Workspace", children: [
      { id: "all-engines", kind: "all-engines", label: "All engines" },
      { id: "all-experiments", kind: "all-experiments", label: "All experiments" },
      { id: "g-project", kind: "group", of: "project", label: "Projects", children: [] },
      { id: "settings", kind: "settings", label: "Settings", children: [{ id: "scope-model", kind: "scope-model", label: "Scope model" }] },
    ],
  };
}

/** {tree, model, live}: the services' tree and model, else the base tree (or SAMPLE when a test asks). */
export async function loadExplorer(workspaceName) {
  if (shared("captain.explorer.sample")) return { ...SAMPLE, live: false };
  const [t, m] = await Promise.all([api("/api/explorer", { scoped: false }), api("/api/scope-model", { scoped: false })]);
  const model = m.ok && m.data ? m.data : DEFAULT_MODEL;
  if (t.ok && t.data) return { tree: t.data.tree ?? t.data, model, live: true };
  return { tree: baseTree(workspaceName), model, live: false, error: t.status && t.status !== 404 ? t : null };
}

/** The scope the views filter to, or null for the whole workspace. */
export function currentScope() {
  return shared("captain.scope") ?? null;
}

/** Every id under a node (its descendants), for filtering "this scope and below". */
export function descendants(node, out = new Set()) {
  for (const ch of node?.children ?? []) { out.add(ch.id); descendants(ch, out); }
  return out;
}

/** The one place the instance actions reach the services, so the route shapes live here only.
 *  action: add {kind, name, parent, fields} | rename {id, name} | move {id, parent} | archive {id}. */
export async function scopeAction(action, p) {
  const id = encodeURIComponent(p.id ?? "");
  if (action === "add") return api(p.kind === "engine" ? "/api/engines" : "/api/scopes", { body: { kind: p.kind, name: p.name, parent: p.parent ?? null, fields: p.fields ?? {} } });
  if (action === "rename") return api(`/api/scopes/${id}/rename`, { body: { name: p.name } });
  if (action === "move") return api(`/api/scopes/${id}/move`, { body: { parent: p.parent } });
  if (action === "archive") return api(`/api/scopes/${id}/archive`, { body: {} });
  return { ok: false, error: `Unknown action ${action}.` };
}

/** Nodes a `node` of `kind` may move under: allowed parent kinds, never itself or its own descendants. */
export function moveTargets(tree, model, node) {
  const allowed = node.kind === "engine" ? (model?.engine?.parents ?? []) : ((model?.types ?? []).find((t) => t.id === node.kind)?.parents ?? []);
  const out = [];
  const walk = (n, path, inside) => {
    const here = inside || n.id === node.id;
    const kind = n.kind === "workspace" ? "workspace" : n.kind;
    if (!here && n.kind !== "group" && allowed.includes(kind)) out.push({ id: n.id, kind, label: n.kind === "workspace" ? `${n.label} (top level)` : [...path, n.label].join(" / ") });
    const next = n.kind === "group" || n.kind === "workspace" ? path : [...path, n.label];
    for (const ch of n.children ?? []) walk(ch, next, here);
  };
  if (tree) walk(tree, [], false);
  return out;
}
