// captain-ui: a notebook's Jupyter kernel, from a mod. One session per open notebook, over the backend's kernel
// socket (POST /api/kernels, then ws /api/kernels/{session}). Messages, one JSON object each:
//   {type: "status", state: "busy" | "idle"}
//   {type: "stream", cell_id, name: "stdout" | "stderr", text}
//   {type: "table", cell_id, var, columns: [{name, type}], rows: [[...]], row_count, truncated}
//   {type: "display", cell_id, mime, data}
//   {type: "error", cell_id, ename, evalue, traceback: [...]}
//   {type: "done", cell_id, ms}
//   {type: "page", cell_id: req, columns, rows, offset, limit, has_more, total?, ms, filtered}: a page of a result,
//   asked for with page() (the notebook grid). Pages never reach onMsg.
// Executes are serialized: execute() resolves {ok, ms} on the cell's "done", so Run all can stop at a failure.
import { WebSocket } from "websocket";
import { api, services } from "./cst.js";

export class Kernel {
  /** onMsg(msg, cx) for every message; onState(state) on "off" | "connecting" | "idle" | "busy" | "lost". */
  constructor(notebook, onMsg, onState) {
    Object.assign(this, { notebook, onMsg, onState });
    this.state = "off";
    this.sock = null;
    this.session = null;
    this.opening = null;
    this.waiters = new Map();
    this.pages = new Map(); // req -> {resolve, data, error}
    this.reqs = 0;
  }

  set(state) {
    this.state = state;
    this.onState?.(state);
  }

  /** Opens the session and its socket once, and keeps reading on cx's task. */
  open(cx) {
    if (this.sock) return Promise.resolve(this.sock);
    if (this.opening) return this.opening;
    this.set("connecting");
    this.opening = (async () => {
      const d = await services();
      if (!d.base) throw new Error("Captain's services aren't running.");
      const r = await api("/api/kernels", { body: { notebook: this.notebook } });
      if (!r.ok) throw new Error(r.error || "Couldn't start Python for this notebook.");
      this.session = r.data.session;
      const url = `${d.base.replace(/^http/, "ws")}/api/kernels/${encodeURIComponent(this.session)}?token=${encodeURIComponent(d.token)}`;
      this.sock = await WebSocket.connect(url);
      this.set("idle");
      cx.spawn(async (cx) => this.read(cx));
      return this.sock;
    })().catch((e) => {
      this.set("lost");
      throw e;
    }).finally(() => {
      this.opening = null;
    });
    return this.opening;
  }

  async read(cx) {
    const sock = this.sock;
    while (sock && this.sock === sock) {
      let raw;
      try {
        raw = await sock.read();
      } catch {
        break;
      }
      let m;
      try { m = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw)); } catch { continue; }
      // A page the grid asked for: kept until its done, never shown as a cell's output.
      const pg = this.pages.get(m.cell_id);
      if (pg) {
        if (m.type === "page") pg.data = m;
        else if (m.type === "error") pg.error = m.evalue || m.ename;
        else if (m.type === "done") { this.pages.delete(m.cell_id); pg.resolve(pg.data ? { ok: true, status: 200, data: pg.data, error: null } : { ok: false, status: 400, data: null, error: pg.error ?? "Couldn't read that page." }); }
        cx.notify();
        continue;
      }
      if (m.type === "status") this.set(m.state);
      if (m.type === "error") { const w = this.waiters.get(m.cell_id); if (w) w.failed = true; }
      this.onMsg?.(m, cx);
      if (m.type === "done") {
        const w = this.waiters.get(m.cell_id);
        this.waiters.delete(m.cell_id);
        w?.resolve({ ok: !w.failed, ms: m.ms });
      }
      cx.notify();
    }
    if (this.sock === sock) {
      this.sock = null;
      this.set("lost");
      for (const [, w] of this.waiters) w.resolve({ ok: false, ms: 0, lost: true });
      this.waiters.clear();
      for (const [, p] of this.pages) p.resolve({ ok: false, status: 0, data: null, error: "Python stopped. Run the cell again to see its rows." });
      this.pages.clear();
      cx.notify();
    }
  }

  /** Runs one cell: {cell_id, kind: "sql" | "python", code, n, conn?, var?}. Resolves {ok, ms} when it's done. */
  async execute(cx, cell) {
    const sock = await this.open(cx);
    return new Promise((resolve) => {
      this.waiters.set(cell.cell_id, { resolve, failed: false });
      sock.write(JSON.stringify({ type: "execute", ...cell })).catch(() => {
        this.waiters.delete(cell.cell_id);
        resolve({ ok: false, ms: 0, lost: true });
      });
    });
  }

  /** A page of a result held in Python ({var, offset, limit, order?, desc?, where?, count?}). Resolves like api():
   *  {ok, data: one row shape} or {ok: false, error}, so the kit grid can page it like any rows route. */
  async page(cx, q) {
    if (!this.sock) return { ok: false, status: 0, data: null, error: "Python isn't running. Run the cell again to see its rows." };
    const req = `page-${++this.reqs}`;
    return new Promise((resolve) => {
      this.pages.set(req, { resolve, data: null, error: null });
      this.sock.write(JSON.stringify({ type: "page", req, ...q, desc: q.desc === "true" || q.desc === true, count: q.count === "true" || q.count === true })).catch(() => {
        this.pages.delete(req);
        resolve({ ok: false, status: 0, data: null, error: "Python stopped. Run the cell again to see its rows." });
      });
    });
  }

  /** Stops the running cell (a KeyboardInterrupt in Python; the query is abandoned). */
  interrupt() {
    this.sock?.write(JSON.stringify({ type: "interrupt" })).catch(() => {});
  }

  /** Ends the session: the socket, then the kernel process. */
  async close() {
    const sock = this.sock;
    this.sock = null;
    try { await sock?.close(); } catch {}
    if (this.session) await api(`/api/kernels/${encodeURIComponent(this.session)}`, { method: "DELETE" });
    this.session = null;
    this.set("off");
  }
}
