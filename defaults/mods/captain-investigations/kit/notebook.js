// captain-ui: notebooks as cells. Files are nbformat 4 with Captain's metadata on each cell:
//   cell.metadata.captain = {kind: "sql" | "python" | "markdown" | "chart", conn?, var?, chart?}
// Outputs keep nbformat's shape, so a saved revision opens in Jupyter too. A SQL table is a display_data with Captain's
// table mime type and a text/plain fallback. Ported from the desktop app's notebook (app/src/notebook/nb.ts).

export const TABLE_MIME = "application/vnd.captain.table+json";
const SAVED_ROWS = 200;

export const newId = () => Math.random().toString(16).slice(2, 10);
const join = (s) => (Array.isArray(s) ? s.join("") : s ?? "");
const ansi = (s) => String(s ?? "").replace(/\u001b\[[0-9;]*m/g, "");

function outFromNb(o) {
  if (o.output_type === "stream") return { kind: "stream", name: o.name === "stderr" ? "stderr" : "stdout", text: join(o.text) };
  if (o.output_type === "error") return { kind: "error", ename: String(o.ename ?? "Error"), evalue: String(o.evalue ?? ""), traceback: (o.traceback ?? []).map(ansi) };
  if (o.output_type === "display_data" || o.output_type === "execute_result") {
    const data = o.data ?? {};
    if (data[TABLE_MIME]) return { kind: "table", table: data[TABLE_MIME] };
    for (const mime of ["text/markdown", "text/plain", "text/html", "image/png", "image/svg+xml"]) {
      if (mime in data) return { kind: "display", mime, data: mime.startsWith("text/") || mime === "image/svg+xml" ? join(data[mime]) : data[mime] };
    }
  }
  return null;
}

function outToNb(o) {
  if (o.kind === "stream") return { output_type: "stream", name: o.name, text: o.text };
  if (o.kind === "error") return { output_type: "error", ename: o.ename, evalue: o.evalue, traceback: o.traceback };
  if (o.kind === "table") {
    const t = { ...o.table, rows: (o.table.rows ?? []).slice(0, SAVED_ROWS) };
    return { output_type: "display_data", metadata: {}, data: { [TABLE_MIME]: t, "text/plain": `${o.table.row_count} rows x ${(o.table.columns ?? []).length} columns` } };
  }
  return { output_type: "display_data", metadata: {}, data: { [o.mime]: o.data } };
}

/** Cells from a notebook file. A plain Jupyter cell (no metadata.captain) runs as Python; raw cells stay text. */
export function fromNotebook(nb) {
  return (nb?.cells ?? []).map((c, i) => {
    const { captain, ...extraMeta } = c.metadata ?? {};
    const m = captain ?? {};
    const raw = c.cell_type === "raw";
    const kind = raw ? "markdown" : m.kind ?? (c.cell_type === "markdown" ? "markdown" : "python");
    return {
      id: c.id ?? `cell-${i}-${newId()}`,
      kind,
      source: join(c.source),
      meta: { ...m, kind },
      outputs: (c.outputs ?? []).map(outFromNb).filter(Boolean),
      execCount: c.execution_count ?? null,
      ...(Object.keys(extraMeta).length ? { extraMeta } : {}),
      ...(raw ? { raw } : {}),
    };
  });
}

/** The notebook file for these cells, keeping the original's metadata. */
export function toNotebook(cells, base) {
  return {
    nbformat: 4,
    nbformat_minor: Math.max(5, base?.nbformat_minor ?? 5),
    metadata: base?.metadata ?? { kernelspec: { name: "python3", display_name: "Python 3", language: "python" } },
    cells: cells.map((c) => {
      const metadata = { ...(c.extraMeta ?? {}), captain: { ...c.meta, kind: c.kind } };
      if (c.raw) return { id: c.id, cell_type: "raw", metadata: c.extraMeta ?? {}, source: c.source };
      if (c.kind === "markdown") return { id: c.id, cell_type: "markdown", metadata, source: c.source };
      return { id: c.id, cell_type: "code", metadata, source: c.source, outputs: c.outputs.map(outToNb), execution_count: c.execCount };
    }),
  };
}

/** A new cell. SQL cells get the default connection and the next free df_N name; charts start as bars. */
export function newCell(kind, cells, conn) {
  const meta = { kind };
  if (kind === "sql") {
    const used = new Set(cells.map((c) => c.meta?.var).filter(Boolean));
    let n = 1;
    while (used.has(`df_${n}`)) n++;
    Object.assign(meta, { conn: conn ?? null, var: `df_${n}` });
  }
  if (kind === "chart") meta.chart = { mark: "bar" };
  return { id: newId(), kind, source: "", meta, outputs: [], execCount: null };
}

/** The newest table bound to each variable, for chart cells. */
export function tablesByVar(cells) {
  const out = new Map();
  for (const c of cells) for (const o of c.outputs) if (o.kind === "table") {
    const v = o.table.var ?? c.meta?.var;
    if (v) out.set(v, o.table);
  }
  return out;
}

/** An engine or Python error as one sentence for people. The traceback goes behind Copy details. */
export function errorSentence(ename, evalue) {
  const first = ansi(evalue).split("\n").map((s) => s.trim()).find(Boolean) ?? "";
  const known = {
    KeyboardInterrupt: "Stopped.",
    ModuleNotFoundError: first ? `${first.replace(/^No module named /, "Python can't find the module ")}. Install it in Captain's Python, or ask an agent to.` : null,
    NameError: first ? `${first.replace(/^name '([^']+)' is not defined/, "Nothing is called $1 yet: run the cell that makes it first")}.` : null,
  }[ename];
  if (known) return known.replace(/\.\.$/, ".");
  const text = first || ename || "That didn't work.";
  const s = `${text[0].toUpperCase()}${text.slice(1)}`;
  return /[.!?]$/.test(s) ? s : `${s}.`;
}
